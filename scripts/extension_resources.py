#!/usr/bin/env python3
"""Check or regenerate native extension resources from the .gigacode sources."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

RESOURCE_TREES = ('agents', 'skills', 'commands')
SKIP = {'__pycache__', '.DS_Store'}


def native_layout(version: str) -> bool:
    return tuple(map(int, version.split('.'))) >= (2, 3, 12)


def tree_files(root: Path, relative: Path, *, missing=False) -> dict[Path, bytes]:
    directory = root / relative
    for part in (relative, *relative.parents):
        if (root / part).is_symlink():
            raise ValueError(f'symlinked extension resource: {relative}')
    if missing and not directory.exists():
        return {}
    if not directory.is_dir():
        raise ValueError(f'missing extension resource directory: {relative}')
    files = {}
    for path in sorted(directory.rglob('*')):
        name = path.relative_to(directory)
        if any(part in SKIP for part in name.parts) or path.suffix in ('.pyc', '.pyo'):
            continue
        if path.is_symlink():
            raise ValueError(f'symlinked extension resource: {relative / name}')
        if path.is_file():
            files[name] = path.read_bytes()
        elif not path.is_dir():
            raise ValueError(f'non-regular extension resource: {relative / name}')
    return files


def sync_resources(root: Path, *, apply=False) -> list[str]:
    # Validate all sources and targets before the first write, including symlinks.
    trees = [(name, tree_files(root, Path('.gigacode') / name),
              tree_files(root, Path(name), missing=apply)) for name in RESOURCE_TREES]
    changes = []
    for name, source, target in trees:
        changes.extend((Path(name) / path).as_posix() for path in sorted(source.keys() | target.keys())
                       if source.get(path) != target.get(path))
    if apply:
        for name, source, target in trees:
            (root / name).mkdir(exist_ok=True)
            for path in target.keys() - source.keys():
                (root / name / path).unlink()
            for path, data in source.items():
                if target.get(path) != data:
                    destination = root / name / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
    return changes


def check_resources(root: Path) -> None:
    changes = sync_resources(root)
    if changes:
        raise ValueError('native extension resources differ from .gigacode sources: '
                         + ', '.join(changes) + '; run scripts/extension_resources.py --apply')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Regenerate the three root resource trees')
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parents[1]
        if args.apply:
            sync_resources(root, apply=True)
        check_resources(root)
        print('Native extension resources match .gigacode sources')
        return 0
    except (OSError, ValueError) as exc:
        print(f'Extension resources error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
