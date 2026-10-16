#!/usr/bin/env python3
"""Preview or synchronize managed QA roles in a bound project's .gigacode/agents."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from qa_agent_state import AGENTS, package_roles, sync_roles
from qa_files import project_lock
from qa_project import CONFIG, ROOT, package_info, project_path, read_binding


def install_agents(project: Path, root: Path = ROOT, *, apply: bool = False, managed_only=False,
                   accept_local=None, restore=None) -> dict:
    info = package_info(root)
    package = Path(info['package_root'])
    project = project_path(project, package)
    from qa_install import INSTALL
    if (project / INSTALL).exists():
        raise ValueError('portable project profile is managed by qa_install.py; update the complete release')
    with project_lock(project):
        binding = read_binding(project / CONFIG, project)
        if (binding['package_root'] != str(package) or binding['package_version'] != info['package_version']):
            raise ValueError('project binding differs from this package; reconcile it through /qa:init first')
        payloads = package_roles(package)
        if len(payloads) != 14 or len(payloads) != info['agent_count']:
            raise ValueError('package must contain exactly 14 named qa-*.md agents')
        result = sync_roles(project, package, info['package_version'], payloads, apply=apply,
                            managed_only=managed_only, accept_local=accept_local, restore=restore)
        return {**info, **result, 'project_root': str(project), 'live_agents_verified': False,
                'next_step': 'Resolve conflicts, then start a new GIGACODE session and check /agents manage'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path.cwd())
    parser.add_argument('--apply', action='store_true', help='Apply safe per-role updates and record baselines')
    parser.add_argument('--managed-only', action='store_true', help='Do not opt an unconfigured project into role copies')
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.gigacode/qa-agent-updates',
                        help='The same session/update lock profile used by qa-start')
    parser.add_argument('--session-lock-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--accept-local', action='append', default=[], metavar='NAME:LOCAL_SHA256:INCOMING_SHA256',
                        help='Accept an explicitly reviewed merged/adapted role with these exact hashes')
    parser.add_argument('--restore', action='append', default=[], metavar='NAME', help='Restore a locally deleted managed role')
    args = parser.parse_args()
    lock = None
    try:
        accepted = {}
        for value in args.accept_local:
            name, local, incoming = value.split(':')
            if name in accepted:
                raise ValueError('duplicate local resolution: ' + name)
            accepted[name] = (local, incoming)
        if args.apply:
            preview = install_agents(args.project_root, managed_only=args.managed_only,
                                     accept_local=accepted, restore=args.restore)
            if preview['status'] != 'unmanaged':
                from qa_auto_update import SessionLock, active_projects, other_cli_running
                lock = (SessionLock.inherited(args.state_dir, args.session_lock_fd)
                        if args.session_lock_fd is not None else SessionLock(args.state_dir).acquire())
                if (not lock.exclusive or active_projects([str(args.project_root.resolve())]) or other_cli_running()):
                    raise ValueError('close active GIGACODE sessions and finish QA assignments before synchronizing roles')
        result = install_agents(args.project_root, apply=args.apply, managed_only=args.managed_only,
                                accept_local=accepted, restore=args.restore)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result['status'] == 'needs_attention' else 0
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(f'QA project agents error: {exc}', file=sys.stderr)
        return 2
    finally:
        if lock is not None:
            lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
