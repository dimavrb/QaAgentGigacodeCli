#!/usr/bin/env python3
"""Preview/install a complete QA release inside a project; no QA MCP server."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import zipfile

from qa_files import atomic_bytes, directory, project_lock, read_bytes

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path('.qa-agent/package')
INSTALL = Path('.qa-agent/install.json')
TRANSACTION = Path('.qa-agent/install.local')
PENDING = TRANSACTION / 'pending.json'
SESSION = Path('.qa-agent/session.local.lock')
BOOTSTRAP = Path('.qa-agent/bootstrap.local')
CONTEXT = Path('.qa-agent/APP_CONTEXT.md')
START, END = '<!-- qa-agent:begin -->', '<!-- qa-agent:end -->'
BLOCK = (START + '\nQA-пакет проекта: `.qa-agent/package/`.\n'
         'Инструкции QA: @.qa-agent/package/docs/COORDINATOR.md\n' + END)
SHA = re.compile(r'[a-f0-9]{64}')


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def optional(path):
    try:
        return read_bytes(path)
    except FileNotFoundError:
        return None


def safe_name(name):
    if not isinstance(name, str):
        raise ValueError('invalid installation path')
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or '..' in path.parts
            or '\\' in name or path.as_posix() != name or name == '.'):
        raise ValueError('invalid installation path: ' + str(name))
    return path


def owned(name):
    path = safe_name(name)
    return (path.is_relative_to(PurePosixPath(PACKAGE.as_posix()))
            or any(path.is_relative_to(PurePosixPath('.gigacode') / tree)
                   for tree in ('agents', 'skills', 'commands')))


def metadata(project, *, verify=True, pending_ok=False):
    if not pending_ok and optional(project / PENDING) is not None:
        raise ValueError('unfinished QA installation; run qa_install.py --recover before QA work')
    raw = optional(project / INSTALL)
    if raw is None:
        return None
    from qa_project import read_json
    value = read_json(project / INSTALL)
    if (set(value) != {'schema_version', 'package_name', 'package_version', 'package_root', 'files', 'memory_sha256'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['package_name'] != 'qa-agent' or value['package_root'] != PACKAGE.as_posix()
            or not isinstance(value['package_version'], str)
            or not re.fullmatch(r'\d+\.\d+\.\d+', value['package_version'])
            or not isinstance(value['files'], dict) or not value['files']
            or not isinstance(value['memory_sha256'], str) or not SHA.fullmatch(value['memory_sha256'])):
        raise ValueError('invalid portable QA installation manifest')
    for name, sha in value['files'].items():
        if not owned(name) or not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise ValueError('invalid managed file in installation manifest')
    mandatory = [PACKAGE / 'VERSION', PACKAGE / 'gigacode-extension.json',
                 PACKAGE / 'scripts/qa_install.py', PACKAGE / 'scripts/qa_project.py',
                 Path('.gigacode/commands/qa.md')]
    if any(p.as_posix() not in value['files'] for p in mandatory):
        raise ValueError('installation manifest is missing required files')
    if verify:
        changed = [name for name, sha in value['files'].items() if digest(optional(project / name)) != sha]
        if changed:
            raise ValueError('QA installation differs from its manifest: ' + ', '.join(changed))
        if read_bytes(project / PACKAGE / 'VERSION').decode().strip() != value['package_version']:
            raise ValueError('installed QA version differs from manifest')
        text = read_bytes(project / 'GIGACODE.md').decode('utf-8')
        if digest(memory_block(text).encode()) != value['memory_sha256']:
            raise ValueError('managed QA block in GIGACODE.md differs from installation manifest')
    return value


def binding(project):
    info = metadata(project)
    if info is None:
        return None
    return dict(schema_version=1, package_name='qa-agent', package_version=info['package_version'],
                package_root=str(project / PACKAGE), project_root=str(project))


@contextmanager
def session_lock(project, *, exclusive=False, descriptor=None):
    inherited = descriptor is not None
    with directory(project / SESSION.parent, create=True) as parent:
        if inherited:
            expected = os.stat(SESSION.name, dir_fd=parent, follow_symlinks=False)
            actual = os.fstat(descriptor)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError('inherited QA session lock belongs to another project')
            os.set_inheritable(descriptor, False)
        else:
            descriptor = os.open(SESSION.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 0o600, dir_fd=parent)
        try:
            import stat
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError('QA session lock must be a regular file')
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SessionBusy('QA session/update is active; close it before updating') from exc
            yield descriptor
        finally:
            os.close(descriptor)


class SessionBusy(ValueError):
    pass


def ensure_idle(project, *, bootstrap=False):
    from qa_auto_update import active_projects, other_cli_running
    if active_projects([str(project)]) or (not bootstrap and other_cli_running()):
        raise ValueError('close GIGACODE sessions and finish active QA assignments/plans before installing')


def source_files(source):
    from package_agent import build_package, verify_archive
    from qa_project import package_info
    info = package_info(source)
    extension = json.loads(read_bytes(source / 'gigacode-extension.json'))
    if extension.get('mcpServers'):
        raise ValueError('project installation requires a release without the QA MCP server')
    with tempfile.TemporaryDirectory(prefix='qa-install-source-') as temporary:
        archive = build_package(source, Path(temporary), date.today().isoformat())
        manifest = verify_archive(archive)
        with zipfile.ZipFile(archive) as bundle:
            files = {item['path']: bundle.read('qa-agent/' + item['path']) for item in manifest['files']}
    # A distributed release has an independent inventory; do not silently bless edits to it.
    previous = optional(source / 'PACKAGE_MANIFEST.json')
    if previous is not None:
        previous = json.loads(previous)
        expected = {item['path']: item['sha256'] for item in previous['files']}
        if previous.get('version') != info['package_version'] or expected != {
                name: digest(data) for name, data in files.items()}:
            raise ValueError('source differs from PACKAGE_MANIFEST.json')
    return info, files


def memory_block(text):
    if text.count(START) != text.count(END) or text.count(START) > 1:
        raise ValueError('invalid managed QA block in GIGACODE.md')
    if START in text:
        start, end = text.index(START), text.index(END)
        if end < start:
            raise ValueError('invalid managed QA block order')
        return text[start:end + len(END)]
    return ''


def memory(text, expected=None):
    block = memory_block(text)
    if expected is not None and digest(block.encode()) != expected:
        raise ValueError('managed QA block in GIGACODE.md changed locally; review it before updating')
    if block:
        if expected is None and block != BLOCK:
            raise ValueError('unknown managed QA block in GIGACODE.md; review it before installing')
        start = text.index(START)
        return text[:start] + BLOCK + text[start + len(block):]
    return text + ('\n' if text and not text.endswith('\n') else '') + BLOCK + '\n'


def plan(project, source):
    from qa_project import IGNORE_LINES
    info, files = source_files(source)
    previous = metadata(project, verify=False)
    old = previous['files'] if previous else {}
    if previous is None:
        # Adopt only established, verified role baselines from the former installer.
        # Its absolute-path local history is preserved, not copied into portable metadata.
        from qa_agent_state import load_state
        legacy = load_state(project)
        if legacy:
            if legacy['pending']:
                raise ValueError('finish the pending legacy role transaction before project migration')
            old = {'.gigacode/agents/' + name: base['sha256'] for name, base in legacy['roles'].items()}
    incoming = {(PACKAGE / name).as_posix(): data for name, data in files.items()}
    for name, data in files.items():
        if any(name.startswith('.gigacode/' + tree + '/') for tree in ('agents', 'skills', 'commands')):
            incoming[name] = data
    conflicts, operations = [], []
    for name in sorted(old.keys() | incoming.keys()):
        before, after = optional(project / name), incoming.get(name)
        if before == after:
            continue
        if (name in old and digest(before) != old[name]) or (name not in old and before is not None):
            conflicts.append(name)
        else:
            operations.append((name, before, after))
    config = dict(schema_version=1, package_name='qa-agent', package_version=info['package_version'],
                  package_root=PACKAGE.as_posix(), files={name: digest(data) for name, data in incoming.items()},
                  memory_sha256=digest(BLOCK.encode()))
    # Shared project files are merged explicitly; they are never claimed as vendor-owned files.
    for name in ('.gitignore', 'GIGACODE.md', CONTEXT.as_posix()):
        before = optional(project / name)
        text = before.decode('utf-8') if before is not None else ''
        if name == '.gitignore':
            missing = [line for line in IGNORE_LINES if line not in text.splitlines()]
            after = (text + ('\n' if text and not text.endswith('\n') else '')
                     + ('\n# Local QA state\n' + '\n'.join(missing) + '\n' if missing else '')).encode()
        elif name == 'GIGACODE.md':
            after = memory(text, previous['memory_sha256'] if previous else None).encode()
        else:
            after = before if before is not None else files['examples/qa-only/APP_CONTEXT.template.md']
        if before != after:
            operations.append((name, before, after))
    before, after = optional(project / INSTALL), encoded(config)
    if before != after:
        operations.append((INSTALL.as_posix(), before, after))  # Completion marker is always last.
    return info, config, operations, conflicts


def publish(project, name, before, after):
    if optional(project / name) != before:
        raise ValueError('file changed during installation: ' + name)
    if after is None:
        with directory((project / name).parent) as parent:
            os.unlink(Path(name).name, dir_fd=parent)
            os.fsync(parent)
    elif not atomic_bytes(project / name, after, replace=before is not None):
        raise ValueError('file appeared during installation: ' + name)


def recover(project):
    raw = optional(project / PENDING)
    if raw is None:
        return {'status': 'nothing_to_recover'}
    journal = json.loads(raw)
    if set(journal) != {'schema_version', 'operations'} or journal['schema_version'] != 1:
        raise ValueError('invalid installation recovery journal')
    operations = []
    seen = set()
    for op in journal['operations']:
        name = op['path']
        if (set(op) != {'path', 'before', 'after'} or name in seen
                or not (owned(name) or name in (INSTALL.as_posix(), CONTEXT.as_posix(), 'GIGACODE.md', '.gitignore'))):
            raise ValueError('invalid recovery operation')
        seen.add(name)
        values = []
        for sha in (op['before'], op['after']):
            if sha is None:
                values.append(None)
            else:
                if not isinstance(sha, str) or not SHA.fullmatch(sha):
                    raise ValueError('invalid recovery checksum')
                data = read_bytes(project / TRANSACTION / sha)
                if digest(data) != sha:
                    raise ValueError('damaged installation recovery file')
                values.append(data)
        current = optional(project / name)
        if current not in values:
            raise ValueError('file changed after interrupted installation: ' + name)
        operations.append((name, current, values[0]))
    for name, current, original in reversed(operations):
        if current != original:
            publish(project, name, current, original)
    # Only remove our known journal and hash blobs, never an unrelated directory tree.
    clean_transaction(project)
    return {'status': 'recovered'}


def clean_transaction(project):
    journal = json.loads(read_bytes(project / PENDING))
    blobs = {op[key] for op in journal['operations'] for key in ('before', 'after') if op[key] is not None}
    with directory(project / TRANSACTION) as parent:
        os.unlink('pending.json', dir_fd=parent)
        os.fsync(parent)
        # The completion marker is durable before discarding backups, so interruption
        # during cleanup cannot leave a pending transaction with missing backups.
        for sha in blobs:
            if not SHA.fullmatch(sha):
                raise ValueError('invalid backup name')
            try:
                os.unlink(sha, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.fsync(parent)


def install(project, source=ROOT, *, apply=False, recovery=False, bootstrap=False, session_lock_fd=None):
    from qa_project import project_path
    project, source = Path(project).resolve(), Path(source).resolve()
    project_path(project, source)
    installed = project / PACKAGE
    staged = source.is_relative_to(project / BOOTSTRAP)
    if source == installed or installed in source.parents or (project in source.parents and not staged):
        raise ValueError('use a new release outside the project or inside .qa-agent/bootstrap.local')
    with project_lock(project):
        if bootstrap and (not staged or recovery or optional(project / INSTALL) is not None or installed.exists()):
            raise ValueError('--bootstrap is only for a first installation from .qa-agent/bootstrap.local')
        if recovery:
            if not apply:
                raise ValueError('--recover requires --apply')
            ensure_idle(project)
            with session_lock(project, exclusive=True, descriptor=session_lock_fd):
                return recover(project)
        if optional(project / PENDING) is not None:
            raise ValueError('unfinished QA installation; run --recover --apply first')
        info, config, operations, conflicts = plan(project, source)
        result = dict(status='conflict' if conflicts else 'preview', package_version=info['package_version'],
                      project_root=str(project), package_root=str(project / PACKAGE),
                      changed_files=[name for name, _, _ in operations], conflicts=conflicts,
                      live_gigacode_verified=False)
        if conflicts or not apply:
            return result
        if not operations:
            return {**result, 'status': 'unchanged'}
        ensure_idle(project, bootstrap=bootstrap)
        with session_lock(project, exclusive=True, descriptor=session_lock_fd):
            # Persist both sides before touching any managed file, including the manifest.
            records = []
            for name, before, after in operations:
                for data in (before, after):
                    if data is not None:
                        path = project / TRANSACTION / digest(data)
                        if not atomic_bytes(path, data, replace=False) and read_bytes(path) != data:
                            raise ValueError('installation backup checksum differs')
                records.append(dict(path=name, before=digest(before), after=digest(after)))
            if not atomic_bytes(project / PENDING, encoded(dict(schema_version=1, operations=records)), replace=False):
                raise ValueError('another installation journal appeared; inspect it before recovery')
            try:
                for name, before, after in operations:
                    publish(project, name, before, after)
                metadata(project, pending_ok=True)
            except Exception:
                recover(project)
                raise
            clean_transaction(project)
        return {**result, 'status': 'installed'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--recover', action='store_true', help='Restore files from an interrupted installation')
    parser.add_argument('--bootstrap', action='store_true',
                        help='First install from project-local staging while GIGACODE is open; active QA work is still forbidden')
    parser.add_argument('--session-lock-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        result = install(args.project_root, apply=args.apply, recovery=args.recover,
                         bootstrap=args.bootstrap, session_lock_fd=args.session_lock_fd)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result['status'] == 'conflict' else 0
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print(f'QA installation error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
