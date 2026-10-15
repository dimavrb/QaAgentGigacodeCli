#!/usr/bin/env python3
"""Build a QA-agent release or verify its ZIP manifest without extracting it."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath

from extension_resources import RESOURCE_TREES, check_resources, native_layout

ROOT_FILES = ('.gitignore', 'VERSION', 'CHANGELOG.md', 'README.md', 'GIGACODE.md', 'AGENTS.md', 'qa-runtime.json', 'LICENSE',
              'gigacode-extension.json', 'GIGACODE_EXTENSION.md',
              '.gigacode/settings.json', '.gigacode/playwright.config.json')
TREES = ('.gigacode/agents', '.gigacode/commands', '.gigacode/skills',
         *RESOURCE_TREES, 'docs', 'examples', 'scripts', 'tests')
SKIP = {'__pycache__', '.DS_Store', '.git', '.idea', '.venv', 'node_modules'}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_archive(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate ZIP entries')
        for entry in bundle.infolist():
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or '..' in path.parts or '\\' in entry.filename
                    or not entry.filename.startswith('qa-agent/') or entry.is_dir()
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError(f'Unsafe ZIP entry: {entry.filename}')
        manifest = json.loads(bundle.read('qa-agent/PACKAGE_MANIFEST.json'))
        if manifest.get('package') != 'qa-agent' or manifest.get('format_version') != 1:
            raise ValueError('Unsupported package manifest')
        expected = {'qa-agent/PACKAGE_MANIFEST.json'}
        for item in manifest['files']:
            name = 'qa-agent/' + item['path']
            if name in expected:
                raise ValueError(f'Duplicate manifest path: {name}')
            expected.add(name)
            data = bundle.read(name)
            if len(data) != item['size'] or digest(data) != item['sha256']:
                raise ValueError(f'Manifest mismatch: {name}')
        if set(names) != expected:
            raise ValueError('Archive contents differ from manifest')
        if bundle.read('qa-agent/VERSION').decode().strip() != manifest['version']:
            raise ValueError('VERSION differs from manifest')
        if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', manifest['version']):
            raise ValueError('Invalid release version')
        if 'qa-agent/gigacode-extension.json' in names:
            extension = json.loads(bundle.read('qa-agent/gigacode-extension.json'))
            if extension.get('name') != 'qa-agent' or extension.get('version') != manifest['version']:
                raise ValueError('Extension version differs from package manifest')
        if native_layout(manifest['version']):
            for tree in RESOURCE_TREES:
                source_prefix, native_prefix = f'.gigacode/{tree}/', f'{tree}/'
                source = {item['path'][len(source_prefix):]: item['sha256'] for item in manifest['files']
                          if item['path'].startswith(source_prefix)}
                native = {item['path'][len(native_prefix):]: item['sha256'] for item in manifest['files']
                          if item['path'].startswith(native_prefix)}
                if not native or native != source:
                    raise ValueError(f'Native extension resources differ from .gigacode sources: {tree}')
        return manifest


def build_package(root: Path, output: Path, packaged_on: str) -> Path:
    release_date = date.fromisoformat(packaged_on)
    if not 1980 <= release_date.year <= 2107:
        raise ValueError('Release date is outside the ZIP timestamp range')
    version = (root / 'VERSION').read_text().strip()
    if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', version):
        raise ValueError('VERSION must contain a numeric release version')
    if native_layout(version):
        check_resources(root)
    if (root / '.gigacode').is_symlink():
        raise ValueError('Cannot package symlink: .gigacode')
    files = [root / name for name in ROOT_FILES]
    for tree in TREES:
        directory = root / tree
        # Trusted validation can still inspect releases predating native exports.
        if tree in RESOURCE_TREES and not native_layout(version) and not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f'Missing or symlinked package directory: {tree}')
        for path in sorted(directory.rglob('*')):
            relative = path.relative_to(root)
            if any(part in SKIP for part in relative.parts) or path.suffix in {'.pyc', '.pyo'}:
                continue
            if path.is_symlink():
                raise ValueError(f'Cannot package symlink: {relative}')
            if path.is_file():
                files.append(path)
    entries = {}
    for path in sorted(files):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Missing or symlinked package file: {path}')
        entries[path.relative_to(root).as_posix()] = path.read_bytes()
    extension = json.loads(entries['gigacode-extension.json'])
    if extension.get('name') != 'qa-agent' or extension.get('version') != version:
        raise ValueError('Extension version differs from VERSION')
    settings = json.loads(entries['.gigacode/settings.json'])
    mcp_args = settings['mcpServers']['playwright']['args']
    mcp_version = next(arg.split('@')[-1] for arg in mcp_args if arg.startswith('@playwright/mcp@'))
    manifest = dict(package='qa-agent', version=version, format_version=1,
                    packaged_on=packaged_on, mcp_version=mcp_version,
                    files=[dict(path=name, sha256=digest(data), size=len(data)) for name, data in entries.items()])
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'qa-agent-{version}.zip'
    checksum = archive.with_suffix('.zip.sha256')
    if archive.exists() or checksum.exists():
        raise ValueError('Release files already exist; choose another output directory')
    entries['PACKAGE_MANIFEST.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    timestamp = date.fromisoformat(packaged_on)
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in entries.items():
            info = zipfile.ZipInfo('qa-agent/' + name, (timestamp.year, timestamp.month, timestamp.day, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            bundle.writestr(info, data)
    verify_archive(archive)
    checksum.write_text(f'{digest(archive.read_bytes())}  {archive.name}\n')
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('dist'))
    parser.add_argument('--date', help='Release date YYYY-MM-DD; required for building')
    parser.add_argument('--verify', type=Path, help='Verify an existing archive instead of building')
    args = parser.parse_args()
    if not args.verify and not args.date:
        parser.error('--date is required for building')
    try:
        if args.verify:
            manifest = verify_archive(args.verify)
            print(f"Valid qa-agent {manifest['version']}: {len(manifest['files'])} files")
        else:
            archive = build_package(Path(__file__).resolve().parents[1], args.output_dir, args.date)
            print(archive.resolve())
            print(archive.with_suffix('.zip.sha256').resolve())
    except (OSError, ValueError, KeyError, TypeError, StopIteration, zipfile.BadZipFile) as exc:
        print(f'Package error: {exc}')
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
