#!/usr/bin/env python3
"""Validate saved investigation state or prepare a read-only continuation."""
import argparse
import json
from investigation_state import continuation, result_digest, review_digest, validate_state
from qa_contract import parse_message
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--current-context', type=Path)
    parser.add_argument('--request-id', help='New assignment ID when continuing')
    parser.add_argument('--result', type=Path, help='Report to fingerprint for coordinator assessment; does not approve it')
    args = parser.parse_args()
    if bool(args.current_context) != bool(args.request_id):
        parser.error('continuation requires both --current-context and --request-id')
    try:
        state = json.loads(args.input.read_text(encoding='utf-8'))
        errors = validate_state(state, args.task_id, args.input.parent)
        result = {'status': 'invalid', 'errors': errors} if errors else {'status': 'valid_state', 'review_digest': review_digest(state), 'note': 'Structure and local artifacts checked; coordinator content review is still required.'}
        if not errors and args.current_context:
            current = json.loads(args.current_context.read_text(encoding='utf-8'))
            result = continuation(state, current, args.request_id, args.input.parent)
        if not errors and args.result:
            message, _ = parse_message(args.result.read_text(encoding='utf-8'))
            result['result_digest'] = result_digest(message)
    except (OSError, ValueError) as exc:
        result = {'status': 'invalid', 'errors': [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result['status'] == 'invalid' else 3 if result['status'] == 'revalidate_conditions' else 0


if __name__ == '__main__':
    raise SystemExit(main())
