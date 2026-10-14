"""Update a locally installed QA extension before a session, under a shared session lock."""
from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import zipfile
import secrets
from pathlib import Path

from package_agent import ROOT_FILES, TREES, SKIP
from qa_project import atomic_write, package_info, project_path, read_json
from qa_files import directory, project_lock
from qa_release import ci_passed, probe, unpack, validate
from qa_recover import inventory, load as load_recovery, save as save_recovery, digest

REMOTE = 'https://github.com/dimavrb/QaAgentGigacodeCli.git'
UPDATE_CONFIG = Path('.qa-agent/update.local.json')
STATE_FIELDS = {'schema_version', 'projects', 'pending'}


class UpdateError(ValueError):
    pass


class SessionLock:
    """Exclusive during updates; shared for the full lifetime of each QA session."""
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory.resolve()
        path = self.directory / 'session.lock'
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.stream = os.fdopen(descriptor, 'r+')
        self.exclusive = False

    def acquire(self):
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.exclusive = True
        except BlockingIOError:
            try:
                fcntl.flock(self.stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                self.close()
                raise UpdateError('another updater is applying a version; retry after it finishes')
        return self

    @classmethod
    def inherited(cls, directory: Path, descriptor: int):
        self = cls.__new__(cls)
        self.directory = directory.resolve()
        expected = os.stat(self.directory / 'session.lock', follow_symlinks=False)
        actual = os.fstat(descriptor)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise UpdateError('inherited session lock does not match the update profile')
        self.stream = os.fdopen(descriptor, 'r+')
        os.set_inheritable(descriptor, False)
        self.exclusive = True
        return self

    def share(self):
        fcntl.flock(self.stream, fcntl.LOCK_SH)
        self.exclusive = False

    def close(self):
        self.stream.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *unused):
        self.close()


def run(arguments, *, cwd=None, timeout=20):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    process = subprocess.Popen(arguments, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        output, errors = process.communicate(timeout=timeout)
    except BaseException:
        # Timeout/interrupt/termination must not release the lease while children write.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    return subprocess.CompletedProcess(arguments, process.returncode, output, errors)


def git(source: Path, *arguments) -> str:
    result = run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsync=all', '-c', 'maintenance.auto=false',
                  '-c', 'gc.auto=0', '-C', str(source), *arguments])
    if result.returncode:
        # Do not print remote URLs/credential helpers or command output into logs.
        raise UpdateError('Git operation failed: ' + arguments[0])
    return result.stdout.strip()


def version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise UpdateError('invalid package version')
    return tuple(int(part) for part in value.split('.'))


def source_head(source: Path, expected_remote: str = REMOTE) -> str:
    installed_git = re.search(r'git version (\d+)\.(\d+)', git(source, '--version'))
    if not installed_git or tuple(map(int, installed_git.groups())) < (2, 36):
        raise UpdateError('automatic updates require Git 2.36+ with core.fsync support')
    if Path(git(source, 'rev-parse', '--show-toplevel')).resolve() != source.resolve():
        raise UpdateError('launcher must run from the root of its local Git clone')
    if git(source, 'remote', 'get-url', 'origin').rstrip('/') != expected_remote.rstrip('/'):
        raise UpdateError('origin differs from the authorized QA repository')
    if git(source, 'symbolic-ref', '--short', 'HEAD') != 'main':
        raise UpdateError('automatic updates require the main branch')
    if git(source, 'status', '--porcelain', '--untracked-files=all'):
        raise UpdateError('local source has changes; automatic update deferred')
    return git(source, 'rev-parse', 'HEAD')


def load_state(directory: Path) -> dict:
    path = directory / 'update-state.json'
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise UpdateError('update state must be a regular local file')
    state = read_json(path) if path.exists() else {'schema_version': 1, 'projects': [], 'pending': None}
    if (set(state) != STATE_FIELDS or type(state['schema_version']) is not int or state['schema_version'] != 1
            or not isinstance(state['projects'], list)
            or any(not isinstance(p, str) or not Path(p).is_absolute() for p in state['projects'])
            or (state['pending'] is not None and not isinstance(state['pending'], dict))):
        raise UpdateError('invalid automatic update state')
    if state['pending'] is not None:
        pending = state['pending']
        if (set(pending) != {'source_root', 'installed_root', 'commit', 'version', 'base_commit', 'base_version'}
                or any(not isinstance(pending[k], str) for k in pending)
                or not re.fullmatch(r'[a-f0-9]{40,64}', pending['commit'])
                or not re.fullmatch(r'[a-f0-9]{40,64}', pending['base_commit'])
                or not Path(pending['source_root']).is_absolute()
                or not Path(pending['installed_root']).is_absolute()):
            raise UpdateError('invalid pending update intent')
        version(pending['version'])
        version(pending['base_version'])
    return state


def save_state(directory: Path, state: dict):
    atomic_write(directory / 'update-state.json', json.dumps(state, ensure_ascii=False, indent=2) + '\n')


def register_project(directory: Path, project: Path):
    # Shared sessions register under a separate short lock, without losing peers.
    descriptor = os.open(directory / 'projects.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        state = load_state(directory)
        state['projects'] = sorted(set(state['projects'] + [str(project)]))
        save_state(directory, state)


def configure(project: Path, source: Path, installed: Path):
    with project_lock(project):
        return configure_locked(project, source, installed)


def configure_locked(project: Path, source: Path, installed: Path):
    project_path(project, installed)
    path = project / UPDATE_CONFIG
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise UpdateError('automatic update config must be a regular local file')
    config = {'schema_version': 1, 'source_root': str(source), 'installed_root': str(installed),
              'project_root': str(project), 'remote': REMOTE, 'branch': 'main'}
    if path.exists() and read_json(path) != config:
        raise UpdateError('update source or installation changed; review and recreate update.local.json explicitly')
    if not path.exists():
        ignore_path = project / '.gitignore'
        ignore = ignore_path.read_text(encoding='utf-8') if ignore_path.exists() else ''
        rule = '/' + UPDATE_CONFIG.as_posix()
        if rule not in ignore.splitlines():
            atomic_write(ignore_path, ignore + ('\n' if ignore and not ignore.endswith('\n') else '') + rule + '\n')
        atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + '\n')


def active_projects(projects: list[str]) -> bool:
    for project in projects:
        directory = Path(project) / '.qa-agent-state'
        if directory.is_symlink():
            raise UpdateError('managed state directory is symlinked; update deferred')
        if not directory.exists():
            continue
        for path in directory.rglob('*'):
            if path.is_symlink():
                raise UpdateError('managed state contains symlinks; update deferred')
            if path.is_file() and path.suffix == '.json':
                value = read_json(path)
                if (value.get('active_request_ids')
                        or ('execution_status' in value and value['execution_status'] != 'completed')
                        or ('plan' in value and 'steps' in value and value.get('status') not in ('completed', 'cancelled'))):
                    return True
    return False


def other_cli_running() -> bool:
    result = run(['ps', '-axo', 'pid=,args='], timeout=3)
    if result.returncode:
        raise UpdateError('cannot inspect CLI processes; automatic update deferred')
    return any(re.search(r'(?:^|[/\s])gigacode(?:\.js)?(?:\s|$)', line)
               for line in result.stdout.splitlines())


def payload_matches(source: Path, installed: Path) -> bool:
    for relative in ROOT_FILES:
        path = installed / relative
        if path.is_symlink() or not path.is_file() or path.read_bytes() != (source / relative).read_bytes():
            return False
    for tree in TREES:
        actual = set()
        for target in (installed / tree).rglob('*'):
            relative = target.relative_to(installed)
            if any(part in SKIP for part in relative.parts) or target.suffix in ('.pyc', '.pyo'):
                continue
            if target.is_symlink():
                return False
            if target.is_file():
                actual.add(relative)
        expected = set()
        for path in (source / tree).rglob('*'):
            relative = path.relative_to(source)
            if any(part in SKIP for part in relative.parts) or path.suffix in ('.pyc', '.pyo'):
                continue
            if path.is_symlink():
                raise UpdateError('source package contains a symlink')
            if path.is_file():
                expected.add(relative)
                target = installed / relative
                if target.is_symlink() or not target.is_file() or target.read_bytes() != path.read_bytes():
                    return False
        if actual != expected:
            return False
    return True


def candidate(source: Path, commit: str, current: str) -> str:
    new_version = git(source, 'show', f'{commit}:VERSION')
    manifest = json.loads(git(source, 'show', f'{commit}:gigacode-extension.json'))
    if (not isinstance(manifest, dict) or manifest.get('name') != 'qa-agent'
            or manifest.get('version') != new_version or version(new_version) <= version(current)):
        raise UpdateError('remote commit must increase VERSION and extension version together')
    modes = git(source, 'ls-tree', '-r', commit, '--', *ROOT_FILES, *TREES)
    if any(line.startswith(('120000 ', '160000 ')) for line in modes.splitlines()):
        raise UpdateError('remote package contains a symlink; automatic update deferred')
    with tempfile.TemporaryDirectory(prefix='qa-candidate-') as temporary:
        base = Path(temporary).resolve()
        archive, staged = base / 'candidate.zip', base / 'package'
        staged.mkdir()
        git(source, 'archive', '--format=zip', '--output=' + str(archive), commit)
        try:
            unpack(archive, staged)
            validate(staged)
            probe(staged, run)
        except (OSError, ValueError, TypeError, KeyError, RecursionError, SyntaxError, zipfile.BadZipFile) as exc:
            raise UpdateError('candidate package preflight failed: ' + str(exc)) from exc
    return new_version


def prepare_recovery(source, installed, directory_path, pending, expected_remote):
    previous = load_recovery(directory_path) if (directory_path / 'recovery.json').exists() else None
    if previous and previous['phase'] in ('prepared', 'activated', 'restoring'):
        if any(previous.get(key) != value for key, value in pending.items()):
            raise UpdateError('another recovery transaction is unfinished')
        return previous
    helper = directory_path / 'qa-recover.pyz'
    if helper.is_symlink() or (helper.exists() and not helper.is_file()):
        raise UpdateError('recovery helper must be a regular file')
    if not helper.exists():
        temporary = helper.with_name('.qa-recover-' + secrets.token_hex(12))
        with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr('__main__.py', 'from qa_recover import main\nraise SystemExit(main())\n')
            for name in ('qa_files', 'qa_recover'):
                bundle.writestr(name + '.py', (source / 'scripts' / (name + '.py')).read_bytes())
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, helper)
        with directory(directory_path) as descriptor:
            os.fsync(descriptor)
    elif previous and digest(helper) != previous.get('helper_sha256'):
        raise UpdateError('independent recovery helper checksum changed')
    elif not previous:
        with zipfile.ZipFile(helper) as bundle:
            expected = {'__main__.py': b'from qa_recover import main\nraise SystemExit(main())\n',
                        **{name + '.py': (source / 'scripts' / (name + '.py')).read_bytes()
                           for name in ('qa_files', 'qa_recover')}}
            if set(bundle.namelist()) != set(expected) or any(bundle.read(k) != v for k, v in expected.items()):
                raise UpdateError('orphan recovery helper differs from the trusted current package')
    base_files = inventory(installed)
    # target inventory is fetched from the exact commit, not inferred from VERSION.
    with tempfile.TemporaryDirectory(prefix='qa-recovery-target-') as temporary:
        root = Path(temporary).resolve()
        archive = root / 'target.zip'; staged = root / 'package'; staged.mkdir()
        git(source, 'archive', '--format=zip', '--output=' + str(archive), pending['commit'])
        unpack(archive, staged)
        _, target_files = validate(staged)
    backup = directory_path / 'rollback' / secrets.token_hex(16) / 'installed.zip'
    backup.parent.mkdir(parents=True, mode=0o700)
    with zipfile.ZipFile(backup, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in base_files:
            bundle.write(installed / path, path)
    with zipfile.ZipFile(backup) as bundle:
        import hashlib
        if any(hashlib.sha256(bundle.read(path)).hexdigest() != sha for path, sha in base_files.items()):
            raise UpdateError('installation changed while creating the rollback snapshot')
    with backup.open('rb') as stream:
        os.fsync(stream.fileno())
    with directory(backup.parent) as descriptor:
        os.fsync(descriptor)
    with directory(backup.parent.parent) as descriptor:
        os.fsync(descriptor)
    journal = {'schema_version': 1, 'phase': 'prepared', **pending,
               'remote': expected_remote, 'base_files': base_files, 'target_files': target_files,
               'base_modes': {path: (installed / path).stat().st_mode & 0o777 for path in base_files},
               'snapshot': backup.relative_to(directory_path).as_posix(),
               'snapshot_sha256': digest(backup), 'helper_sha256': digest(helper)}
    atomic_write(backup.parent / 'RECOVERY.json', json.dumps(journal, ensure_ascii=True, indent=2) + '\n')
    save_recovery(directory_path, journal)
    return journal


def update(source: Path, project: Path, installed: Path, cli: str, lock: SessionLock,
           *, expected_remote: str = REMOTE, process_check=other_cli_running) -> dict:
    """The caller keeps the lock until the interactive child session has ended."""
    source, project, installed = source.resolve(), project.resolve(), installed.resolve()
    if not lock.exclusive:
        return {'status': 'deferred_session', 'changed': False}
    state = load_state(lock.directory)
    state['projects'] = sorted(set(state['projects'] + [str(project)]))
    save_state(lock.directory, state)
    if active_projects(state['projects']) or process_check():
        return {'status': 'deferred_active_work', 'changed': False}
    if source == installed or source in installed.parents:
        return {'status': 'deferred_linked_installation', 'changed': False}
    head = source_head(source, expected_remote)
    info = package_info(source)
    pending = state['pending']
    if (lock.directory / 'recovery.json').exists():
        recovery = load_recovery(lock.directory)
        if recovery['phase'] == 'restoring':
            raise UpdateError('rollback is unfinished; use the independent recovery helper')
        if pending is None and recovery['phase'] in ('prepared', 'activated'):
            # Recovery journal is published before pending; restore an interrupted marker.
            pending = {key: recovery[key] for key in
                       ('source_root', 'installed_root', 'commit', 'version', 'base_commit', 'base_version')}
            state['pending'] = pending
            save_state(lock.directory, state)
    if pending and (pending['source_root'] != str(source) or pending['installed_root'] != str(installed)
                    or head not in (pending['commit'], pending['base_commit'])
                    or info['package_version'] != (pending['version'] if head == pending['commit'] else pending['base_version'])):
        raise UpdateError('pending update no longer matches the local source; reconcile it explicitly')
    if not pending:
        installed_info = package_info(installed)
        if installed_info['package_version'] != info['package_version']:
            raise UpdateError('source and installed copy differ; synchronize the local installation first')
        if not payload_matches(source, installed):
            return {'status': 'deferred_installed_changes', 'changed': False}
        # A failed fetch never causes a reset or replacement of the installed copy.
        try:
            git(source, 'fetch', '--no-tags', 'origin', '+refs/heads/main:refs/remotes/origin/main')
        except (UpdateError, subprocess.TimeoutExpired):
            return {'status': 'offline_cached', 'changed': False}
        target = git(source, 'rev-parse', 'refs/remotes/origin/main')
        if target == head:
            return {'status': 'up_to_date', 'changed': False}
        if (lock.directory / 'recovery.json').exists():
            recovered = load_recovery(lock.directory)
            if recovered['phase'] == 'rolled_back' and recovered['commit'] == target:
                raise UpdateError('this commit was rolled back; wait for a new verified release')
        if expected_remote == REMOTE and not ci_passed(target):
            return {'status': 'release_not_verified', 'changed': False}
        ancestry = run(['git', '-C', str(source), 'merge-base', '--is-ancestor', head, target])
        if ancestry.returncode:
            raise UpdateError('local main is ahead or diverged; automatic update deferred')
        new_version = candidate(source, target, info['package_version'])
        help_result = run([cli, 'extensions', 'update', '--help'], cwd=project)
        if help_result.returncode:
            raise UpdateError('CLI does not support extensions update; local source was not changed')
        if source_head(source, expected_remote) != head or active_projects(state['projects']) or process_check():
            return {'status': 'deferred_changed_conditions', 'changed': False}
        pending = {'source_root': str(source), 'installed_root': str(installed),
                   'commit': target, 'version': new_version,
                   'base_commit': head, 'base_version': info['package_version']}
        prepare_recovery(source, installed, lock.directory, pending, expected_remote)
        state['pending'] = pending
        save_state(lock.directory, state)
    elif not (lock.directory / 'recovery.json').exists():
        raise UpdateError('legacy pending activation has no safe rollback snapshot; reconcile it before upgrading')
    recovery = load_recovery(lock.directory)
    if any(recovery.get(key) != value for key, value in pending.items()) or recovery['phase'] == 'restoring':
        raise UpdateError('recovery transaction differs from pending activation; use independent rollback')
    if head == pending['base_commit']:
        # Intent is durable before changing source: a crash here is recoverable.
        if not payload_matches(source, installed):
            raise UpdateError('base installation changed before applying the saved update')
        candidate(source, pending['commit'], pending['base_version'])
        git(source, 'merge', '--ff-only', pending['commit'])
    # Read back first: a previous CLI update may have succeeded before a crash.
    if not payload_matches(source, installed):
        if active_projects(state['projects']) or process_check():
            return {'status': 'pending_activation', 'changed': False}
        result = run([cli, 'extensions', 'update', 'qa-agent'], cwd=project, timeout=60)
        if result.returncode or not payload_matches(source, installed):
            raise UpdateError('extension activation failed; pending intent retained, QA session was not started')
    installed_info = package_info(installed)
    if installed_info['package_version'] != pending['version']:
        raise UpdateError('installed version differs from pending update; QA session was not started')
    probe(source, run)
    probe(installed, run)
    recovery = load_recovery(lock.directory)
    recovery['target_files'] = inventory(installed)
    recovery['phase'] = 'activated'
    save_recovery(lock.directory, recovery)
    # Ensure payload bytes and directory entries precede the durable completion marker.
    for relative in recovery['target_files']:
        with (installed / relative).open('rb') as stream:
            os.fsync(stream.fileno())
    for path in sorted([installed, *[p for p in installed.rglob('*') if p.is_dir() and not p.is_symlink()]],
                       key=lambda p: len(p.parts), reverse=True):
        with directory(path) as descriptor:
            os.fsync(descriptor)
    state['pending'] = None
    save_state(lock.directory, state)
    recovery['phase'] = 'completed'
    save_recovery(lock.directory, recovery)
    return {'status': 'updated', 'changed': True, 'commit': pending['commit'], 'version': pending['version']}


def refresh_binding(project: Path, installed: Path):
    # Run the newly installed initializer rather than an old imported Python module.
    import sys
    result = run([sys.executable, str(installed / 'scripts/qa_project.py'), 'init',
                  '--project-root', str(project)], cwd=project)
    if result.returncode:
        raise UpdateError('project binding refresh failed; QA session was not started')
    return json.loads(result.stdout)
