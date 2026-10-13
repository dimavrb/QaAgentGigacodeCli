#!/usr/bin/env python3
"""Reserve local Playwright profiles and record verified cleanup; no MCP calls."""
import argparse
import json
from pathlib import Path

from browser_pool import BrowserPool, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pool', required=True, type=Path, help='One shared absolute path for every caller of these MCP resources')
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--config', required=True, type=Path)
    sub.add_parser('status')
    for command in ('acquire', 'begin-recovery'):
        child = sub.add_parser(command)
        for field in ('task-id', 'request-id', 'owner-task-id'):
            child.add_argument('--' + field, required=True)
        child.add_argument('--server', required=command == 'begin-recovery')
        child.add_argument('--ttl', type=int, default=300)
        if command == 'begin-recovery':
            child.add_argument('--evidence', required=True, type=Path)
    for command in ('check', 'heartbeat', 'begin-close', 'finish-close', 'quarantine'):
        child = sub.add_parser(command)
        child.add_argument('--server', required=True)
        child.add_argument('--lease-id', required=True)
        if command == 'finish-close':
            child.add_argument('--evidence', required=True, type=Path)
        if command == 'quarantine':
            child.add_argument('--reason', required=True)
    args = parser.parse_args()
    if not args.pool.is_absolute():
        parser.error('--pool must be absolute; different working directories must not create separate pools')
    pool = BrowserPool(args.pool)
    try:
        if args.command == 'init':
            result = pool.init(read_json(args.config.read_bytes()))
        elif args.command == 'status':
            result = pool.status()
        elif args.command == 'acquire':
            result = pool.acquire(args.task_id, args.request_id, args.owner_task_id, args.server, args.ttl)
        elif args.command == 'begin-recovery':
            result = pool.begin_recovery(args.server, args.task_id, args.request_id, args.owner_task_id, args.evidence, args.ttl)
        elif args.command in ('check', 'heartbeat'):
            result = pool.check(args.server, args.lease_id, heartbeat=args.command == 'heartbeat')
        elif args.command == 'begin-close':
            result = pool.begin_close(args.server, args.lease_id)
        elif args.command == 'finish-close':
            result = pool.finish_close(args.server, args.lease_id, args.evidence)
        else:
            result = pool.quarantine(args.server, args.lease_id, args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        print(json.dumps({'error': str(exc), 'mcp_actions_performed': False}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
