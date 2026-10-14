#!/usr/bin/env python3
"""Bind a project to the installed QA extension without copying its source files."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path('.qa-agent/project.local.json')
CONTEXT = Path('.qa-agent/APP_CONTEXT.md')
IGNORE_LINES = ('/.qa-agent/project.local.json', '/.qa-agent-state/', '/.qa-agent/artifacts/')
CONFIG_FIELDS = {'schema_version', 'package_name', 'package_version', 'package_root', 'project_root'}


def read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError(f'{path}: expected a JSON object')
    return value


def package_info(root: Path = ROOT) -> dict:
    root = root.resolve()
    manifest = read_json(root / 'gigacode-extension.json')
    version = (root / 'VERSION').read_text(encoding='utf-8').strip()
    if (manifest.get('name') != 'qa-agent' or manifest.get('version') != version
            or not re.fullmatch(r'\d+\.\d+\.\d+', version)):
        raise ValueError('extension manifest and VERSION must identify the same QA package version')
    resources = {'agents': '.gigacode/agents', 'skills': '.gigacode/skills',
                 'commands': '.gigacode/commands', 'contextFileName': 'GIGACODE_EXTENSION.md'}
    for key, relative in resources.items():
        path = root / relative
        expected_type = path.is_file() if key == 'contextFileName' else path.is_dir()
        if manifest.get(key) != relative or path.is_symlink() or not expected_type:
            raise ValueError(f'invalid extension resource: {key}')
    for relative in ('docs/COORDINATOR.md', 'docs/EXTENSION_RUNTIME.md',
                     'scripts/qa_project.py', 'scripts/qa_extension_mcp.py',
                     'examples/qa-only/APP_CONTEXT.template.md'):
        if not (root / relative).is_file():
            raise ValueError(f'missing package resource: {relative}')
    return {'package_name': 'qa-agent', 'package_version': version, 'package_root': str(root),
            'coordinator': str(root / 'docs/COORDINATOR.md'),
            'project_tool': str(root / 'scripts/qa_project.py'),
            'agent_count': len(list((root / resources['agents']).glob('*.md'))),
            'skill_count': len(list((root / resources['skills']).glob('*/SKILL.md'))),
            'commands': sorted(path.relative_to(root / resources['commands']).with_suffix('').as_posix().replace('/', ':')
                               for path in (root / resources['commands']).rglob('*.md'))}


def project_path(project: Path, package_root: Path) -> Path:
    project = project.resolve()
    if not project.is_dir():
        raise ValueError('project directory must already exist')
    if project == package_root or package_root in project.parents:
        raise ValueError('project must be outside the installed package')
    for relative in (CONFIG, CONTEXT, Path('.gitignore')):
        candidate = project / relative
        for part in (candidate, *candidate.parents):
            if part == project:
                break
            if part.is_symlink():
                raise ValueError(f'symlinked project file is not supported: {part}')
            if part != candidate and part.exists() and not part.is_dir():
                raise ValueError(f'project directory must be a directory: {part}')
        if candidate.exists() and not candidate.is_file():
            raise ValueError(f'project file must be a regular file: {candidate}')
    return project


def read_binding(path: Path, project: Path) -> dict:
    config = read_json(path)
    if (set(config) != CONFIG_FIELDS or type(config.get('schema_version')) is not int
            or config['schema_version'] != 1 or config['package_name'] != 'qa-agent'
            or config['project_root'] != str(project)
            or not isinstance(config['package_root'], str) or not Path(config['package_root']).is_absolute()
            or not isinstance(config['package_version'], str)
            or not re.fullmatch(r'\d+\.\d+\.\d+', config['package_version'])):
        raise ValueError('invalid QA project binding; restore or migrate it explicitly')
    return config


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.qa-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def initialize(project: Path, root: Path = ROOT, *, rebind: bool = False) -> dict:
    info = package_info(root)
    project = project_path(project, Path(info['package_root']))
    config_path, context_path, ignore_path = project / CONFIG, project / CONTEXT, project / '.gitignore'
    previous = read_binding(config_path, project) if config_path.exists() else None
    if previous and previous['package_root'] != info['package_root'] and not rebind:
        raise ValueError('package path changed; inspect it and use --rebind explicitly')
    # Validate every existing file before changing any project file.
    if context_path.exists():
        context_path.read_text(encoding='utf-8')
    context = (Path(info['package_root']) / 'examples/qa-only/APP_CONTEXT.template.md').read_text(encoding='utf-8')
    ignore = ignore_path.read_text(encoding='utf-8') if ignore_path.exists() else ''
    missing = [line for line in IGNORE_LINES if line not in ignore.splitlines()]
    config = {key: info[key] for key in ('package_name', 'package_version', 'package_root')}
    config.update(schema_version=1, project_root=str(project))
    changed = []
    # Install ignore rules first, then context; binding is the final completion marker.
    if missing:
        atomic_write(ignore_path, ignore + ('\n' if ignore and not ignore.endswith('\n') else '')
                     + '\n# Local QA Agent state and generated evidence\n' + '\n'.join(missing) + '\n')
        changed.append('.gitignore')
    if not context_path.exists():
        atomic_write(context_path, context)
        changed.append(CONTEXT.as_posix())
    if previous != config:
        atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        changed.append(CONFIG.as_posix())
    return {'status': 'configured', **config, 'changed_files': changed,
            'context_file': str(context_path), 'external_systems_configured': False}


def doctor(project: Path, root: Path = ROOT) -> dict:
    info = package_info(root)
    project = project_path(project, Path(info['package_root']))
    checks = []
    def check(name, passed, detail):
        checks.append({'name': name, 'status': 'passed' if passed else 'failed', 'detail': detail})
    check('python', sys.version_info >= (3, 10), sys.version.split()[0])
    check('agents', info['agent_count'] == 14, str(info['agent_count']))
    check('skills', info['skill_count'] == 5, str(info['skill_count']))
    check('commands', {'qa', 'qa:init', 'qa:doctor'} <= set(info['commands']), ', '.join(info['commands']))
    config_path = project / CONFIG
    if config_path.exists():
        binding = read_binding(config_path, project)
        check('binding', binding['package_root'] == info['package_root']
              and binding['package_version'] == info['package_version'], 'run /qa:init after an extension update')
    else:
        check('binding', False, 'run /qa:init')
    check('context', (project / CONTEXT).is_file(), str(project / CONTEXT))
    ignore = (project / '.gitignore').read_text(encoding='utf-8') if (project / '.gitignore').exists() else ''
    check('ignore', all(line in ignore.splitlines() for line in IGNORE_LINES), 'local binding and QA evidence are ignored')
    executable = shutil.which('gigacode')
    cli_version = None
    if executable:
        try:
            result = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=10)
            cli_version = result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            pass
    check('gigacode', bool(cli_version), cli_version or 'CLI unavailable or version probe failed')
    return {'status': 'ready_for_cli_check' if all(c['status'] == 'passed' for c in checks) else 'needs_attention',
            **info, 'project_root': str(project), 'checks': checks,
            'live_extension_verified': False, 'external_systems_configured': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('info', 'init', 'doctor'))
    parser.add_argument('--project-root', type=Path, default=Path.cwd())
    parser.add_argument('--rebind', action='store_true', help='Allow a reviewed change of installed package path')
    args = parser.parse_args()
    try:
        if args.command == 'info':
            result = package_info()
        elif args.command == 'init':
            result = initialize(args.project_root, rebind=args.rebind)
        else:
            result = doctor(args.project_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('status') == 'needs_attention' else 0
    except (OSError, ValueError, TypeError) as exc:
        print(f'QA project error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
