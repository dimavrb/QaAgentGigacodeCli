#!/usr/bin/env python3
"""Independent local rollback, bundled with qa_files in the external recovery zipapp.

Never imports the installed extension, launcher or mutable source checkout.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import zipfile

from qa_files import atomic_text, directory, read_bytes

JOURNAL = 'recovery.json'
SKIP = {'__pycache__', '.DS_Store'}


def digest(path):
    value = hashlib.sha256()
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('recovery payload must be a regular file')
            for chunk in iter(lambda: stream.read(65536), b''):
                value.update(chunk)
    return value.hexdigest()


def inventory(root):
    if root.is_symlink() or not root.is_dir() or (root / '.git').exists():
        raise ValueError('installation must be a real directory without an embedded Git repository')
    result = {}
    for path in root.rglob('*'):
        relative = path.relative_to(root)
        if any(part in SKIP for part in relative.parts) or path.suffix in ('.pyc', '.pyo'):
            continue
        if path.is_symlink():
            raise ValueError('installation contains a symlink')
        if path.is_file():
            result[relative.as_posix()] = digest(path)
        elif not path.is_dir():
            raise ValueError('installation contains a non-regular file')
    return result


def save(directory_path, value):
    atomic_text(directory_path / JOURNAL, json.dumps(value, ensure_ascii=True, indent=2) + '\n')


def read_object(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate recovery JSON key')
            value[key] = item
        return value
    value = json.loads(read_bytes(path).decode('utf-8'), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError('invalid recovery object')
    return value


def load(profile):
    value = read_object(profile / JOURNAL)
    required = {'schema_version', 'phase', 'source_root', 'installed_root', 'remote',
                'base_commit', 'commit', 'snapshot', 'snapshot_sha256', 'base_files', 'target_files', 'base_modes',
                'helper_sha256', 'version', 'base_version'}
    if (not required <= set(value) or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['phase'] not in ('prepared', 'activated', 'completed', 'restoring', 'rolled_back')
            or any(not isinstance(value[k], str) or not Path(value[k]).is_absolute()
                   for k in ('source_root', 'installed_root'))
            or any(not isinstance(value[k], str) or not re.fullmatch(r'[a-f0-9]{40,64}', value[k])
                   for k in ('base_commit', 'commit'))):
        raise ValueError('invalid recovery journal')
    if any(not isinstance(value[k], str) or not re.fullmatch(r'\d+\.\d+\.\d+', value[k])
           for k in ('version', 'base_version')):
        raise ValueError('invalid recovery versions')
    relative = Path(value['snapshot'])
    if relative.is_absolute() or '..' in relative.parts or relative.parts[:1] != ('rollback',):
        raise ValueError('snapshot must be inside the recovery profile')
    for name in ('base_files', 'target_files'):
        if not isinstance(value[name], dict):
            raise ValueError('invalid recovery inventory')
        for path, sha in value[name].items():
            p = Path(path)
            if (p.is_absolute() or '..' in p.parts or '\\' in path or not p.parts
                    or not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha)):
                raise ValueError('invalid recovery inventory entry')
    if (not isinstance(value['base_modes'], dict) or set(value['base_modes']) != set(value['base_files'])
            or any(type(mode) is not int or not 0 <= mode <= 0o777 for mode in value['base_modes'].values())):
        raise ValueError('invalid snapshot permissions')
    if digest(profile / relative) != value['snapshot_sha256']:
        raise ValueError('rollback snapshot checksum differs; no files were restored')
    if digest(profile / 'qa-recover.pyz') != value['helper_sha256']:
        raise ValueError('independent recovery helper checksum differs')
    return value


def git(source, *arguments):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsync=all', '-c', 'maintenance.auto=false',
                             '-c', 'gc.auto=0', '-C', str(source), *arguments],
                            env=dict(os.environ, GIT_TERMINAL_PROMPT='0'), capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise ValueError('rollback Git operation failed: ' + arguments[0])
    return result.stdout.strip()


def active_work(projects):
    for project in projects:
        if not isinstance(project, str) or not Path(project).is_absolute():
            raise ValueError('invalid registered project')
        root = Path(project) / '.qa-agent-state'
        if root.is_symlink():
            raise ValueError('managed state is symlinked')
        if not root.exists():
            continue
        for path in root.rglob('*'):
            if path.is_symlink():
                raise ValueError('managed state contains a symlink')
            if path.suffix == '.json':
                state = read_object(path)
                if (state.get('active_request_ids') or state.get('execution_status') == 'running'
                        or ('plan' in state and 'steps' in state and state.get('status') not in ('completed', 'cancelled'))):
                    return True
    return False


def archive_displaced(profile, journal, displaced):
    if not displaced.exists():
        return
    if displaced.parent != Path(journal['installed_root']).parent or not displaced.name.startswith('.qa-displaced-'):
        raise ValueError('unexpected displaced installation path')
    files = inventory(displaced)
    if any(sha not in (journal['base_files'].get(path), journal['target_files'].get(path))
           for path, sha in files.items()):
        raise ValueError('displaced installation has unrelated changes; preserved in place')
    archive = (profile / journal['snapshot']).parent / ('failed-installation-' + secrets.token_hex(12) + '.zip')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(displaced / path, path)
    with zipfile.ZipFile(archive) as bundle:
        if any(hashlib.sha256(bundle.read(path)).hexdigest() != sha for path, sha in files.items()):
            raise ValueError('displaced installation changed during archival; preserved in place')
    with archive.open('rb') as stream:
        os.fsync(stream.fileno())
    with directory(archive.parent) as descriptor:
        os.fsync(descriptor)
    journal.update(failed_snapshot=archive.relative_to(profile).as_posix(), failed_snapshot_sha256=digest(archive))
    save(profile, journal)
    import shutil
    shutil.rmtree(displaced)
    with directory(displaced.parent) as descriptor:
        os.fsync(descriptor)


def rollback(profile):
    journal = load(profile)
    source, installed = Path(journal['source_root']), Path(journal['installed_root'])
    installed_git = re.search(r'git version (\d+)\.(\d+)', git(source, '--version'))
    if not installed_git or tuple(map(int, installed_git.groups())) < (2, 36):
        raise ValueError('rollback requires Git 2.36+')
    if source == installed or source in installed.parents or installed in source.parents:
        raise ValueError('source and installation must be independent directories')
    if (Path(git(source, 'rev-parse', '--show-toplevel')).resolve() != source
            or git(source, 'remote', 'get-url', 'origin') != journal['remote']
            or git(source, 'symbolic-ref', '--short', 'HEAD') != 'main'):
        raise ValueError('source identity changed; rollback refused')
    head = git(source, 'rev-parse', 'HEAD')
    if head not in (journal['base_commit'], journal['commit']) or git(source, 'status', '--porcelain', '--untracked-files=all'):
        raise ValueError('source has unrelated changes; rollback refused')
    state = read_object(profile / 'update-state.json')
    if active_work(state.get('projects', [])):
        raise ValueError('stop active assignments and managed execution before rollback')
    pending = state.get('pending')
    if pending and (pending.get('commit') != journal['commit'] or pending.get('source_root') != str(source)
                    or pending.get('installed_root') != str(installed)):
        raise ValueError('another update is pending; rollback refused')
    if installed.exists():
        for path, sha in inventory(installed).items():
            if sha not in (journal['base_files'].get(path), journal['target_files'].get(path)):
                raise ValueError('installation has unrelated local edits; rollback refused')
    elif journal['phase'] != 'restoring':
        raise ValueError('installation disappeared outside a saved rollback')
    if journal['phase'] == 'rolled_back' and head == journal['base_commit'] and inventory(installed) == journal['base_files']:
        return {'status': 'rolled_back', 'source_root': str(source), 'installed_root': str(installed),
                'commit': journal['base_commit'], 'rejected_commit': journal['commit']}
    # Verify and fully stage the backup before changing either live directory.
    staged = installed.with_name('.qa-restore-' + secrets.token_hex(12))
    staged.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(profile / journal['snapshot']) as bundle:
            if set(bundle.namelist()) != set(journal['base_files']):
                raise ValueError('snapshot inventory mismatch')
            for path, sha in journal['base_files'].items():
                data = bundle.read(path)
                if hashlib.sha256(data).hexdigest() != sha:
                    raise ValueError('snapshot contents differ from inventory')
                target = staged / path
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), journal['base_modes'][path])
                    os.fsync(stream.fileno())
        for path in sorted([staged, *[p for p in staged.rglob('*') if p.is_dir()]], key=lambda p: len(p.parts), reverse=True):
            with directory(path) as descriptor:
                os.fsync(descriptor)
        displaced = (Path(journal['displaced_path']) if journal['phase'] == 'restoring' and journal.get('displaced_path')
                     else installed.with_name('.qa-displaced-' + secrets.token_hex(12)))
        if displaced.parent != installed.parent or not displaced.name.startswith('.qa-displaced-'):
            raise ValueError('unexpected saved rollback path')
        journal.update(phase='restoring', displaced_path=str(displaced))
        save(profile, journal)
        # Reset ONLY the updater's recorded fast-forward, after checking clean HEAD.
        if head == journal['commit']:
            git(source, 'reset', '--hard', journal['base_commit'])
        for relative in git(source, 'ls-files', '-z').split('\0'):
            if relative:
                with directory((source / relative).parent) as parent:
                    fd = os.open(Path(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    try:
                        if not stat.S_ISREG(os.fstat(fd).st_mode):
                            raise ValueError('source checkout contains a non-regular file')
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        with directory(source) as descriptor:
            os.fsync(descriptor)
        already_restored = installed.exists() and inventory(installed) == journal['base_files']
        if not already_restored:
            with directory(installed.parent) as parent:
                if installed.exists():
                    if displaced.exists():
                        raise ValueError('displaced directory already exists; preserve and reconcile it')
                    os.rename(installed.name, displaced.name, src_dir_fd=parent, dst_dir_fd=parent)
                    os.fsync(parent)
                os.rename(staged.name, installed.name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
        if inventory(installed) != journal['base_files']:
            raise ValueError('restored installation readback failed')
        archive_displaced(profile, journal, displaced)
        state['pending'] = None
        atomic_text(profile / 'update-state.json', json.dumps(state, ensure_ascii=True, indent=2) + '\n')
        journal.update(phase='rolled_back')
        save(profile, journal)
        return {'status': 'rolled_back', 'source_root': str(source), 'installed_root': str(installed),
                'commit': journal['base_commit'], 'rejected_commit': journal['commit']}
    finally:
        # Keep backup/displaced directories for audit. Remove only our unused staging directory.
        if staged.exists():
            import shutil
            shutil.rmtree(staged)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('command', choices=('status', 'rollback'))
    args = parser.parse_args()
    profile = args.state_dir.resolve()
    fd = None
    try:
        if args.command == 'status':
            value = load(profile)
            print(json.dumps({k: value[k] for k in ('phase', 'source_root', 'installed_root', 'base_commit', 'commit')}, indent=2))
            return 0
        fd = os.open(profile / 'session.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        processes = subprocess.run(['ps', '-axo', 'pid=,args='], capture_output=True, text=True, timeout=3)
        if processes.returncode or any(re.search(r'(?:^|[/\s])gigacode(?:\.js)?(?:\s|$)', row)
                                       for row in processes.stdout.splitlines()):
            raise ValueError('close active GIGACODE work before rollback')
        print(json.dumps(rollback(profile), indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.TimeoutExpired, zipfile.BadZipFile) as exc:
        print(f'QA recovery error: {exc}', file=sys.stderr)
        return 2
    finally:
        if fd is not None:
            os.close(fd)


if __name__ == '__main__':
    raise SystemExit(main())
