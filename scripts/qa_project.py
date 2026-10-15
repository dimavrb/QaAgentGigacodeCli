#!/usr/bin/env python3
"""Bind a project to the installed QA extension without copying its source files."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from qa_files import atomic_text, project_lock, read_bytes
from qa_agent_state import package_roles, sync_roles
from extension_resources import check_resources, native_layout

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path('.qa-agent/project.local.json')
CONTEXT = Path('.qa-agent/APP_CONTEXT.md')
IGNORE_LINES = ('/.qa-agent/project.local.json', '/.qa-agent/update.local.json',
                '/.qa-agent-state/', '/.qa-agent/artifacts/',
                '/.qa-agent/agents.local.json', '/.qa-agent/agent-updates/')
CONFIG_FIELDS = {'schema_version', 'package_name', 'package_version', 'package_root', 'project_root'}


def read_json(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    try:
        value = json.loads(read_bytes(path).decode('utf-8'), object_pairs_hook=unique)
    except RecursionError as exc:
        raise ValueError('local JSON nesting exceeds the supported limit') from exc
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
    native = native_layout(version)
    if native:
        check_resources(root)
    resources = {key: key if native else '.gigacode/' + key for key in ('agents', 'skills', 'commands')}
    resources['contextFileName'] = 'GIGACODE_EXTENSION.md'
    for key, relative in resources.items():
        path = root / relative
        expected_type = path.is_file() if key == 'contextFileName' else path.is_dir()
        declared = manifest.get(key) == relative if not native or key == 'contextFileName' else True
        if not declared or path.is_symlink() or not expected_type:
            raise ValueError(f'invalid extension resource: {key}')
    for relative in ('docs/COORDINATOR.md', 'docs/EXTENSION_RUNTIME.md',
                     'scripts/qa_project.py', 'scripts/qa_extension_mcp.py', 'scripts/qa_tasks.py',
                     'scripts/qa_agent_state.py', 'scripts/qa_project_agents.py',
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
    atomic_text(path, text)


def binding_digest(binding):
    import hashlib
    return hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class RebindRequired(ValueError):
    def __init__(self, binding, new_root):
        self.binding, self.new_root = binding, new_root
        super().__init__('package path changed; inspect it and use --rebind explicitly\n'
                         f'Old package path: {binding["package_root"]}\nNew package path: {new_root}\n'
                         f'Expected binding SHA-256: {binding_digest(binding)}')


def initialize(project: Path, root: Path = ROOT, *, rebind: bool = False, expected_binding=None,
               expected_binding_sha256=None) -> dict:
    if rebind and expected_binding is None and expected_binding_sha256 is None:
        raise ValueError('rebind requires the reviewed binding or its SHA-256 digest')
    info = package_info(root)
    project = project_path(project, Path(info['package_root']))
    with project_lock(project):
        return initialize_locked(project, info, rebind=rebind, expected_binding=expected_binding,
                                 expected_binding_sha256=expected_binding_sha256)


def initialize_locked(project, info, *, rebind, expected_binding, expected_binding_sha256):
    project_path(project, Path(info['package_root']))
    config_path, context_path, ignore_path = project / CONFIG, project / CONTEXT, project / '.gitignore'
    previous = read_binding(config_path, project) if config_path.exists() else None
    if ((expected_binding is not None and previous != expected_binding)
            or (expected_binding_sha256 is not None and binding_digest(previous) != expected_binding_sha256)):
        raise ValueError('binding changed since review; show the current old/new paths again before rebind')
    if previous and previous['package_root'] != info['package_root'] and not rebind:
        raise RebindRequired(previous, info['package_root'])
    # Validate every existing file before changing any project file.
    if context_path.exists():
        read_bytes(context_path).decode('utf-8')
    context = (Path(info['package_root']) / 'examples/qa-only/APP_CONTEXT.template.md').read_text(encoding='utf-8')
    ignore = read_bytes(ignore_path).decode('utf-8') if ignore_path.exists() else ''
    missing = [line for line in IGNORE_LINES if line not in ignore.splitlines()]
    config = {key: info[key] for key in ('package_name', 'package_version', 'package_root')}
    config.update(schema_version=1, project_root=str(project))
    changed = []
    # Install ignore rules first, then context; binding is the final completion marker.
    if missing:
        atomic_write(ignore_path, ignore + ('\n' if ignore and not ignore.endswith('\n') else '')
                     + '\n# Local QA Agent state and generated evidence\n' + '\n'.join(missing) + '\n')
        changed.append('.gitignore')
    if atomic_text(context_path, context, replace=False):
        changed.append(CONTEXT.as_posix())
    else:
        # A non-cooperating editor may have created the context after validation.
        read_bytes(context_path).decode('utf-8')
    if previous != config:
        atomic_write(config_path, json.dumps(config, ensure_ascii=False, indent=2) + '\n')
        changed.append(CONFIG.as_posix())
    return {'status': 'configured', **config, 'changed_files': changed,
            'context_file': str(context_path), 'external_systems_configured': False}


def project_agent_differences(project: Path, package: Path) -> list[str]:
    info = package_info(package)
    result = sync_roles(project, package, info['package_version'], package_roles(package), managed_only=True)
    return [item['name'] for item in result['conflict_details']]


def doctor(project: Path, root: Path = ROOT) -> dict:
    info = package_info(root)
    project = project_path(project, Path(info['package_root']))
    checks = []
    def check(name, passed, detail):
        checks.append({'name': name, 'status': 'passed' if passed else 'failed', 'detail': detail})
    check('python', sys.version_info >= (3, 10), sys.version.split()[0])
    check('agents', info['agent_count'] == 14, str(info['agent_count']))
    project_agents = sync_roles(project, Path(info['package_root']), info['package_version'],
                               package_roles(Path(info['package_root'])), managed_only=True)
    check('project_agent_copies', not project_agents['needs_sync'] and project_agents['status'] != 'needs_attention',
          'Managed QA roles need synchronization/review: ' + ', '.join(project_agents['conflicts'])
          if project_agents['needs_sync'] or project_agents['status'] == 'needs_attention'
          else 'Project roles are synchronized; local adaptations: ' + ', '.join(project_agents['adapted_roles']))
    check('skills', info['skill_count'] == 6, str(info['skill_count']))
    check('commands', {'qa', 'qa:init', 'qa:doctor', 'qa:tasks', 'qa:resume'} <= set(info['commands']), ', '.join(info['commands']))
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
            'project_agents': project_agents,
            'live_extension_verified': False, 'external_systems_configured': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('info', 'init', 'doctor'))
    parser.add_argument('--project-root', type=Path, default=Path.cwd())
    parser.add_argument('--rebind', action='store_true', help='Allow a reviewed change of installed package path')
    parser.add_argument('--expected-binding-sha256', help='Binding digest shown in the preceding init error')
    args = parser.parse_args()
    try:
        if args.command == 'info':
            result = package_info()
        elif args.command == 'init':
            if args.rebind and not args.expected_binding_sha256:
                raise ValueError('--rebind requires --expected-binding-sha256 from the reviewed first step')
            result = initialize(args.project_root, rebind=args.rebind,
                                expected_binding_sha256=args.expected_binding_sha256)
        else:
            result = doctor(args.project_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get('status') == 'needs_attention' else 0
    except (OSError, ValueError, TypeError) as exc:
        print(f'QA project error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
