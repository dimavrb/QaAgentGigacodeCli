"""Update one portable project installation before starting its CLI session."""
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from qa_auto_update import REMOTE, UpdateError, active_projects, other_cli_running, run, version
from qa_files import atomic_bytes, directory, read_bytes
from qa_install import BOOTSTRAP, PACKAGE, SessionBusy, binding, metadata, session_lock
from qa_release import ci_passed, probe, validate


def update_project(project, current, descriptor):
    """Caller holds the project's exclusive session lock throughout the update."""
    result = dict(changed=False, package_version=current['package_version'])
    if active_projects([str(project)]):
        return {**result, 'status': 'deferred_active_work'}
    if other_cli_running():
        return {**result, 'status': 'deferred_active_session'}
    with directory(project / BOOTSTRAP, create=True):
        pass
    stage = Path(tempfile.mkdtemp(prefix='update.', dir=project / BOOTSTRAP))
    keep = False
    try:
        atomic_bytes(stage / '.gitignore', b'*\n', replace=False)
        source = stage / 'source'
        try:
            fetched = run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'maintenance.auto=false',
                           'clone', '--depth', '1', '--single-branch', '--branch', 'main', REMOTE, str(source)],
                          cwd=project, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return {**result, 'status': 'offline'}
        if fetched.returncode:
            return {**result, 'status': 'offline'}
        incoming = read_bytes(source / 'VERSION').decode('utf-8').strip()
        old_version, new_version = version(current['package_version']), version(incoming)
        if new_version <= old_version:
            return {**result, 'status': 'current' if new_version == old_version else 'newer_local_version'}
        if new_version[0] != old_version[0]:
            return {**result, 'status': 'manual_major_update', 'available_version': incoming}
        head = run(['git', '-C', str(source), 'rev-parse', 'HEAD'], cwd=project)
        import re
        commit = head.stdout.strip()
        if head.returncode or not re.fullmatch(r'[a-f0-9]{40,64}', commit):
            raise UpdateError('cannot identify the downloaded QA commit')
        if not ci_passed(commit):
            return {**result, 'status': 'unverified_release', 'available_version': incoming}
        info, _ = validate(source)
        if info['package_version'] != incoming:
            raise UpdateError('downloaded package version changed during validation')
        probe(source, run)
        # Run the new installer, not code loaded before the update. The inherited
        # lock excludes sessions until the new launcher has loaded and validated it.
        keep = True
        installed = run([sys.executable, '-B', str(source / 'scripts/qa_install.py'),
                         '--project-root', str(project), '--apply', '--session-lock-fd', str(descriptor)],
                        cwd=project, timeout=60, pass_fds=(descriptor,))
        try:
            report = json.loads(installed.stdout)
        except (ValueError, TypeError) as exc:
            raise UpdateError('QA update stopped; review conflicts/recovery with '
                              + str(source / 'scripts/qa_install.py')) from exc
        if isinstance(report, dict) and report.get('status') == 'conflict':
            raise UpdateError('QA update conflicts: ' + ', '.join(report.get('conflicts', []))
                              + '; inspect ' + str(source / 'scripts/qa_install.py'))
        if (not isinstance(report, dict) or installed.returncode
                or report.get('status') not in ('installed', 'unchanged') or report.get('package_version') != incoming):
            raise UpdateError('QA update stopped; inspect ' + str(source / 'scripts/qa_install.py'))
        if metadata(project)['package_version'] != incoming:
            raise UpdateError('installed package failed verification after update')
        keep = False
        return dict(status='updated', changed=True, package_version=incoming, commit=commit)
    finally:
        if not keep:
            try:
                shutil.rmtree(stage)
            except OSError:
                pass  # Ignored staging residue is not an active runtime.


def start_project(project, root, args):
    """Use an exclusive update gate, then keep a shared gate until CLI exits."""
    arguments = args.cli_args[1:] if args.cli_args[:1] == ['--'] else args.cli_args
    inherited = args.project_session_lock_fd
    if bool(args.project_resume_version) != (inherited is not None):
        raise UpdateError('project update resume requires its inherited session lock')
    if args.resume_after_update or args.session_lock_fd is not None:
        raise UpdateError('legacy update arguments do not apply to portable projects')
    cli = shutil.which('gigacode')
    if not cli and not args.update_only:
        raise UpdateError('GIGACODE unavailable; install/authenticate the CLI before qa-start')
    with ExitStack() as stack:
        try:
            descriptor = stack.enter_context(session_lock(project, exclusive=True, descriptor=inherited))
            exclusive = True
        except SessionBusy:
            if inherited is not None:
                raise
            descriptor = stack.enter_context(session_lock(project))
            exclusive = False
        current = binding(project)
        if current['package_root'] != str(root):
            raise UpdateError('run .qa-agent/package/scripts/qa_start.py from this project')
        if args.project_resume_version:
            if current['package_version'] != args.project_resume_version:
                raise UpdateError('QA version changed before the updated launcher resumed')
            result = dict(status='reloaded', changed=False, package_version=current['package_version'])
        elif args.no_update:
            result = dict(status='update_disabled', changed=False, package_version=current['package_version'])
        elif not exclusive:
            result = dict(status='deferred_active_session', changed=False, package_version=current['package_version'])
        else:
            result = update_project(project, current, descriptor)
        print('QA update: ' + json.dumps(result, ensure_ascii=False), file=sys.stderr, flush=True)
        if result['changed']:
            os.set_inheritable(descriptor, True)
            argv = [sys.executable, '-B', str(project / PACKAGE / 'scripts/qa_start.py'),
                    '--project-root', str(project), '--project-resume-version', result['package_version'],
                    '--project-session-lock-fd', str(descriptor)]
            if args.update_only:
                argv.append('--update-only')
            os.execv(sys.executable, [*argv, '--', *arguments])
            raise UpdateError('updated launcher did not restart')
        if args.update_only:
            return 0 if result['status'] in ('current', 'reloaded', 'update_disabled', 'newer_local_version') else 1
        # All imports and package checks above completed under the gate. No model
        # session exists while files are being replaced or the launcher re-execs.
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        print('QA project package: ' + current['package_version'], file=sys.stderr, flush=True)
        previous = signal.signal(signal.SIGINT, lambda *unused: None)
        try:
            return subprocess.call([cli, *arguments], cwd=project)
        finally:
            signal.signal(signal.SIGINT, previous)
