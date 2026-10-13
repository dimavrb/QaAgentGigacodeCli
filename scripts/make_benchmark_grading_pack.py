#!/usr/bin/env python3
"""Create frozen evaluation copies outside the original run in one command."""
import argparse
import json
from pathlib import Path
from benchmark_grading import make_pack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = make_pack(args.run, args.output)
        print(json.dumps(dict(pack=str(args.output.resolve()), pack_id=manifest['pack_id'],
                              checks_per_trial=manifest['expected_checks_per_trial']), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Grading package error: {exc}'); return 2


if __name__ == '__main__': raise SystemExit(main())
