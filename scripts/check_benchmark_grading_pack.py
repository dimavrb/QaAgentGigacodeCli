#!/usr/bin/env python3
"""Verify grading copy hashes and their correspondence to original captures."""
import argparse
from pathlib import Path
from benchmark_grading import verify_pack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pack', required=True, type=Path)
    parser.add_argument('--run', required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest, _ = verify_pack(args.pack, args.run)
        print(f"Verified frozen package {manifest['pack_id']}")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Grading integrity error: {exc}'); return 2


if __name__ == '__main__': raise SystemExit(main())
