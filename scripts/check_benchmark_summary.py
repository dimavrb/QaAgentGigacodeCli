#!/usr/bin/env python3
"""Reject summary facts, numbers or verdicts not produced from this session's sources."""
import argparse
import json
from pathlib import Path
from benchmark_summary import check_summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--session-id', required=True)
    args = parser.parse_args()
    try:
        result = check_summary(args.input, args.session_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['baseline']['stage_closed'] else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Summary acceptance error: {exc}'); return 2


if __name__ == '__main__': raise SystemExit(main())
