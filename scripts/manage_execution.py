#!/usr/bin/env python3
"""Initialize, supervise, stop and explicitly resume managed QA executions."""
import argparse
import execution_metrics
import json
from pathlib import Path
import subprocess
import sys

import execution_supervisor as supervisor
import task_exchange as exchange


def summary(state):
    def details(row):
        attempt = row['attempts'][-1] if row['attempts'] else {}
        worker = attempt.get('worker_identity') or {}
        return dict(status=row['status'], attempts=len(row['attempts']), checkpoint=row['checkpoint'],
                    reason=row.get('reason'), request_id=attempt.get('request_id'), category=attempt.get('category'),
                    worker_pid=worker.get('pid'), heartbeat_at=attempt.get('heartbeat_at'),
                    run_dir=attempt.get('run_dir'), failure_incident_id=attempt.get('failure_incident_id'))
    return dict(task_id=state['task_id'], status=state['status'], revision=state['revision'],
                metrics=execution_metrics.aggregate([state]),
                cancel_requested=state['cancel_requested'],
                active_seconds=round(supervisor.active_seconds(state),3), max_active_seconds=state['plan']['max_active_seconds'],
                steps={sid:details(row) for sid,row in state['steps'].items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--plan', type=Path, required=True)
    sub.add_parser('tick')
    show = sub.add_parser('show')
    show.add_argument('--full', action='store_true')
    serve = sub.add_parser('serve')
    serve.add_argument('--max-seconds', type=float, default=1200)
    serve.add_argument('--answers-poll-seconds', type=float, default=60)
    cancel = sub.add_parser('cancel')
    cancel.add_argument('--reason', required=True)
    resume = sub.add_parser('resume')
    resume.add_argument('--decision', required=True)
    retry = sub.add_parser('retry')
    retry.add_argument('--step-id', required=True)
    retry.add_argument('--decision', required=True)
    reconcile = sub.add_parser('reconcile-action')
    reconcile.add_argument('--step-id', required=True)
    reconcile.add_argument('--action-id', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'init':
            state = supervisor.initialize(args.state, exchange.read_json(args.plan), args.plan.resolve().parent)
        elif args.command == 'tick':
            state = supervisor.tick(args.state)
        elif args.command == 'cancel':
            state = supervisor.cancel(args.state, args.reason)
        elif args.command == 'resume':
            state = supervisor.resume(args.state, args.decision)
        elif args.command == 'retry':
            state = supervisor.retry_after_reconciliation(args.state, args.step_id, args.decision)
        elif args.command == 'reconcile-action':
            state = supervisor.read(args.state)
            step = next(step for step in state['plan']['steps'] if step['step_id'] == args.step_id)
            policy = args.state.parent / (args.state.name + '.runs') / args.step_id / 'readback-policy.json'
            exchange.atomic_write(policy, step['gateway_policy'])
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('mcp_action_gateway.py')),
                '--policy', str(policy), '--journal', str(supervisor.action_path(args.state, args.step_id)),
                '--task-id', state['task_id'], '--step-id', args.step_id, '--reconcile', args.action_id],
                capture_output=True, text=True, timeout=step['gateway_policy']['call_timeout_seconds'] + 15)
            if result.returncode:
                raise ValueError('Readback failed; outcome remains uncertain')
            print(result.stdout.strip())
            return 0
        elif args.command == 'serve':
            state = supervisor.serve(args.state, args.max_seconds, args.answers_poll_seconds)
        else:
            state = supervisor.read(args.state)
        print(json.dumps(state if args.command == 'show' and args.full else summary(state), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.TimeoutExpired) as exc:
        print(f'Execution error: {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        supervisor.cancel(args.state, 'Supervisor interrupted by user')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
