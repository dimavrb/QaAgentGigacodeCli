#!/usr/bin/env python3
"""Launch interactive GIGACODE after checking a trusted local QA extension source."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys

from qa_project import CONFIG, ROOT, package_info, project_path, read_binding
from qa_auto_update import (SessionLock, UpdateError, configure, load_state,
                            refresh_binding, register_project, source_head, update)


def synchronize_project_agents(project, installed, *, may_write, session_lock=None):
    command = [sys.executable, str(installed / 'scripts/qa_project_agents.py'),
               '--project-root', str(project), '--managed-only']
    def invoke(apply=False):
        arguments, descriptors = command + (['--apply'] if apply else []), ()
        if apply and session_lock is not None:
            descriptor = session_lock.stream.fileno()
            arguments += ['--state-dir', str(session_lock.directory), '--session-lock-fd', str(descriptor)]
            descriptors = (descriptor,)
        result = subprocess.run(arguments, cwd=project, capture_output=True, text=True,
                                timeout=30, pass_fds=descriptors)
        if result.returncode not in (0, 1):
            raise UpdateError('project role synchronization failed: ' + result.stderr.strip())
        report = json.loads(result.stdout)
        if (not isinstance(report, dict) or type(report.get('needs_sync')) is not bool
                or report.get('status') not in ('unmanaged', 'preview', 'installed', 'needs_attention')
                or (result.returncode == 1 and report['status'] != 'needs_attention')):
            raise UpdateError('invalid project role synchronization report')
        return report
    report = invoke()
    if report['needs_sync']:
        if not may_write:
            raise UpdateError('project QA roles need synchronization; close active sessions and retry qa-start')
        report = invoke(apply=True)
    print('QA project agents: ' + json.dumps(report, ensure_ascii=False), file=sys.stderr, flush=True)
    if report.get('status') == 'needs_attention' or report['needs_sync']:
        raise UpdateError('project QA roles have unresolved conflicts; review qa_project_agents.py output before a new session')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path.cwd())
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.gigacode/qa-agent-updates')
    parser.add_argument('--no-update', action='store_true', help='Use the installed package without a Git check')
    parser.add_argument('--update-only', action='store_true', help='Update and refresh binding without launching GIGACODE')
    parser.add_argument('--resume-after-update', help=argparse.SUPPRESS)
    parser.add_argument('--session-lock-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--project-resume-version', help=argparse.SUPPRESS)
    parser.add_argument('--project-session-lock-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('cli_args', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    lock = None
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        source = ROOT.resolve()
        project = args.project_root.resolve()
        from qa_install import INSTALL
        if (project / INSTALL).exists():
            from qa_project_update import start_project
            return start_project(project, source, args)
        if args.project_resume_version or args.project_session_lock_fd is not None:
            raise UpdateError('project update resume requires a portable installation')
        project_path(project, source)
        binding = read_binding(project / CONFIG, project)
        installed = Path(binding['package_root']).resolve()
        project_path(project, installed)
        cli = shutil.which('gigacode')
        if not cli:
            raise UpdateError('GIGACODE unavailable; install/authenticate the CLI before qa-start')
        lock = (SessionLock.inherited(args.state_dir, args.session_lock_fd)
                if args.session_lock_fd is not None else SessionLock(args.state_dir).acquire())
        register_project(lock.directory, project)
        state = load_state(lock.directory)
        if args.resume_after_update:
            if source_head(source) != args.resume_after_update or state['pending']:
                raise UpdateError('source changed after update; restart qa-start explicitly')
            result = {'status': 'reloaded', 'changed': False}
        elif args.no_update:
            if state['pending']:
                raise UpdateError('pending activation must be reconciled before --no-update')
            result = {'status': 'update_disabled', 'changed': False}
        else:
            if lock.exclusive:
                source_head(source)
                configure(project, source, installed)
            result = update(source, project, installed, cli, lock)
            if result['status'] == 'pending_activation' or load_state(lock.directory)['pending']:
                raise UpdateError('update activation pending; close active work and rerun qa-start')
        print('QA update: ' + json.dumps(result, ensure_ascii=False), file=sys.stderr, flush=True)
        if result['changed']:
            # Restart before using new instructions or initializer code. No session is running yet.
            descriptor = lock.stream.fileno()
            os.set_inheritable(descriptor, True)
            os.execv(sys.executable, [sys.executable, str(source / 'scripts/qa_start.py'),
                                    '--resume-after-update', result['commit'],
                                    '--session-lock-fd', str(descriptor), *sys.argv[1:]])
        info = package_info(installed)
        if info['package_root'] != binding['package_root']:
            raise UpdateError('installed path changed; rebind explicitly')
        # Never silently rewrite an existing managed task to fit changed instructions.
        from qa_auto_update import active_projects
        if binding['package_version'] != info['package_version'] and active_projects([str(project)]):
            raise UpdateError('binding version changed while a managed task is unfinished')
        refreshed = refresh_binding(project, installed)
        synchronize_project_agents(project, installed, may_write=lock.exclusive, session_lock=lock)
        print('QA package: ' + refreshed['package_version'], file=sys.stderr, flush=True)
        if args.update_only:
            return 0
        lock.share()
        arguments = args.cli_args[1:] if args.cli_args[:1] == ['--'] else args.cli_args
        # The child handles Ctrl+C; retain the shared lock until it really exits.
        previous_handler = signal.signal(signal.SIGINT, lambda *unused: None)
        try:
            return subprocess.call([cli, *arguments], cwd=project)
        finally:
            signal.signal(signal.SIGINT, previous_handler)
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f'QA start error: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('QA start interrupted; check saved activation/recovery state before retrying', file=sys.stderr)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        if lock is not None:
            lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
