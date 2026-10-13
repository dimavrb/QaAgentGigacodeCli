#!/usr/bin/env python3
"""Compute summary numbers and calibrated verdicts in the current evidence session."""
import argparse
import json
from pathlib import Path
from benchmark_grading import write
from benchmark_summary import command_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--calibration', type=Path)
    parser.add_argument('--changed-dimension')
    args = parser.parse_args()
    try:
        result = command_result(args.run, args.session_id, args.candidate, args.calibration, args.changed_dimension)
        write(args.output, result)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['baseline']['stage_closed'] else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Summary gate error: {exc}'); return 2


if __name__ == '__main__': raise SystemExit(main())
