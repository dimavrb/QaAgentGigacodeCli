#!/usr/bin/env python3
"""Run an explicitly authorized, bounded content review/correction cycle."""
import argparse
import execution_metrics
import json
import time
from pathlib import Path
import review_cycle
import task_exchange


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--plan', type=Path, required=True)
    sub.add_parser('tick')
    sub.add_parser('show')
    serve = sub.add_parser('serve')
    serve.add_argument('--max-seconds', type=float, default=60)
    cancel = sub.add_parser('cancel')
    cancel.add_argument('--reason', required=True)
    resume = sub.add_parser('resume')
    resume.add_argument('--decision', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'init':
            state = review_cycle.initialize(args.state, task_exchange.read_json(args.plan), args.plan.resolve().parent)
        elif args.command == 'cancel':
            state = review_cycle.cancel(args.state, args.reason)
        elif args.command == 'resume':
            state = review_cycle.resume(args.state, args.decision)
        elif args.command == 'show':
            state = review_cycle.read(args.state)
        else:
            duration = args.max_seconds if args.command == 'serve' else 0
            if not 0 <= duration <= 60:
                raise ValueError('serve duration must be 0..60 seconds; repeat with the same state')
            deadline = time.monotonic() + duration
            while True:
                state = review_cycle.tick(args.state)
                if state['status'] in ('accepted', 'blocked', 'cancelled') or time.monotonic() >= deadline:
                    break
                time.sleep(.1)
        print(json.dumps(dict(task_id=state['task_id'], status=state['status'], round=state['round'],
            reason=state.get('reason'), result_request_id=state.get('result_request_id'),
            review_request_ids=state.get('review_request_ids', []), active_seconds=review_cycle.active_seconds(state),
            metrics=execution_metrics.aggregate([review_cycle.supervisor.read(c['path']) for c in state['children'] if Path(c['path']).exists()]),
            discussions={key: dict(status=log['status'], rounds=len(log['rounds']))
                         for key, log in state.get('discussions', {}).items()}), ensure_ascii=False, indent=2))
        return 0 if state['status'] != 'blocked' else 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Review cycle error: {exc}')
        return 2
    except KeyboardInterrupt:
        review_cycle.cancel(args.state, 'Interrupted by user')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
