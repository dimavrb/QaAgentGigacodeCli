#!/usr/bin/env python3
"""Manage a local QA task and prepare validated agent handoffs."""
import argparse
import json
import subprocess
from pathlib import Path

import task_exchange as exchange


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--task-id', required=True)
    init.add_argument('--title', required=True)
    scope = init.add_mutually_exclusive_group(required=True)
    scope.add_argument('--criteria', type=Path, help='Agreed task criteria JSON')
    scope.add_argument('--legacy-no-criteria', action='store_true', help='Explicit compatibility mode without task criteria')
    define = sub.add_parser('set-criteria', help='Define task scope before its first assignment')
    define.add_argument('--input', type=Path, required=True)
    assess = sub.add_parser('assess', help='Coordinator assessments of task criteria')
    assess.add_argument('--input', type=Path, required=True)
    show = sub.add_parser('show')
    show.add_argument('--full', action='store_true', help='Include assignments, hashes and complete event history')
    assign = sub.add_parser('assign')
    assign.add_argument('--input', type=Path, required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--request-id', required=True)
    prepare.add_argument('--output', type=Path,
                         help='Save full handoff to a new file; print only path, SHA256 and revision')
    start = sub.add_parser('start')
    start.add_argument('--request-id', required=True)
    start.add_argument('--owner-task-id', required=True)
    start.add_argument('--delivery-id', required=True)
    submit = sub.add_parser('submit')
    submit.add_argument('--request-id', required=True)
    review = sub.add_parser('review')
    review.add_argument('--request-id', required=True)
    review.add_argument('--decision', required=True)
    review.add_argument('--changes-requested', action='store_true')
    fail = sub.add_parser('fail')
    fail.add_argument('--request-id', required=True)
    fail.add_argument('--reason', required=True)
    finish = sub.add_parser('finish')
    finish.add_argument('--outcome', required=True)
    finish.add_argument('--decision', required=True)
    questions = sub.add_parser('questions')
    questions.add_argument('--input', type=Path, required=True)
    questions.add_argument('--answers', type=Path, required=True)
    notify = sub.add_parser('notify-answers', help='Register an offline answer or recover a missed notification')
    notify.add_argument('--input', type=Path, required=True)
    notify.add_argument('--answers', type=Path, required=True)
    apply = sub.add_parser('apply-answers')
    apply.add_argument('--questionnaire-id', required=True)
    apply.add_argument('--decision', required=True)
    ack = sub.add_parser('ack')
    ack.add_argument('--event-id', type=int, required=True)
    wait = sub.add_parser('wait')
    wait.add_argument('--after', type=int, default=0)
    wait.add_argument('--timeout', type=float, default=60)
    for command in (assign, prepare, start, submit, review, fail, finish, questions, apply, ack, define, assess):
        command.add_argument('--expected-revision', type=int, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'init':
            result = exchange.init_task(args.state, args.task_id, args.title,
                                        exchange.read_json(args.criteria) if args.criteria else None)
        elif args.command == 'set-criteria':
            result = exchange.set_criteria(args.state, exchange.read_json(args.input), args.expected_revision)
        elif args.command == 'assess':
            packet = exchange.read_json(args.input)
            if isinstance(packet, dict) and isinstance(packet.get('assessments'), list):
                for item in packet['assessments']:
                    if isinstance(item, dict) and isinstance(item.get('evidence'), list):
                        item['evidence'] = [str((args.input.resolve().parent / p).resolve()) for p in item['evidence']]
            result = exchange.assess_criteria(args.state, packet, args.expected_revision)
        elif args.command == 'show':
            state = exchange.read_state(args.state)
            result = state if args.full else exchange.state_summary(state)
        elif args.command == 'assign':
            result = exchange.assign(args.state, exchange.read_json(args.input), args.input.resolve().parent, args.expected_revision)
        elif args.command == 'prepare':
            result = exchange.prepare_handoff(args.state, args.request_id, args.expected_revision,
                                              output_path=args.output)
        elif args.command == 'start':
            result = exchange.start_request(args.state, args.request_id, args.owner_task_id, args.delivery_id, args.expected_revision)
        elif args.command == 'submit':
            result = exchange.submit_result(args.state, args.request_id, args.expected_revision)
        elif args.command == 'review':
            result = exchange.review_result(args.state, args.request_id, args.decision, not args.changes_requested, args.expected_revision)
        elif args.command == 'fail':
            result = exchange.fail_request(args.state, args.request_id, args.reason, args.expected_revision)
        elif args.command == 'finish':
            result = exchange.finish_task(args.state, args.outcome, args.decision, args.expected_revision)
        elif args.command == 'questions':
            result = exchange.register_questions(args.state, args.input, args.answers, args.expected_revision)
        elif args.command == 'notify-answers':
            from questionnaire import normalize_packet
            result = exchange.notify_answers(args.state, normalize_packet(exchange.read_json(args.input)), args.answers)
        elif args.command == 'apply-answers':
            result = exchange.apply_answers(args.state, args.questionnaire_id, args.decision, args.expected_revision)
        elif args.command == 'ack':
            result = exchange.acknowledge(args.state, args.event_id, args.expected_revision)
        else:
            result = exchange.wait_events(args.state, args.after, args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f'Ошибка обмена: {exc}')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
