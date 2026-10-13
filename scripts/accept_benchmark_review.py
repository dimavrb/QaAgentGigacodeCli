#!/usr/bin/env python3
"""Read a saved evaluator file by absolute path, gate it, then record acceptance."""
import argparse
import json
from pathlib import Path
from benchmark_grading import accept_review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--pack', required=True, type=Path)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--session-id', required=True)
    args = parser.parse_args()
    try:
        if not args.input.is_absolute():
            raise ValueError('acceptance requires an absolute evaluator result path')
        receipt = accept_review(args.run, args.pack, args.input, args.session_id)
        print(json.dumps(receipt, ensure_ascii=False))
        # File acceptance is not all-pass: partial/failing results remain visible.
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Review acceptance error: {exc}'); return 2


if __name__ == '__main__': raise SystemExit(main())
