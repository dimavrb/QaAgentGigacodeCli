#!/usr/bin/env python3
"""Read-only preflight of stub intent before MCP mutations (stdlib only)."""
import argparse
from pathlib import Path

from stub_contract import read_stub_json, validate_assignment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--request-id', required=True)
    parser.add_argument('--scenario-id', required=True, action='append')
    args = parser.parse_args()
    try:
        assignment = read_stub_json(args.input)
        errors = validate_assignment(assignment)
        if not errors:
            if assignment['task_id'] != args.task_id or assignment['request_id'] != args.request_id:
                errors.append('intent task/request differs from assignment')
            if set(assignment['scenario_ids']) != set(args.scenario_id):
                errors.append('intent scenarios differ from assignment')
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
        errors = [str(exc)]
    if errors:
        print('\n'.join(errors))
        return 2
    print('Valid stub intent structure. No MCP actions performed; coordinator contract/scope assessment is still required.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
