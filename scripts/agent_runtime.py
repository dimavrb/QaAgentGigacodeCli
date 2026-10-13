"""Portable CLI contract; the shipped runtime is GIGACODE CLI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'qa-runtime.json'
ISOLATION_FLAGS = ['--bare', '--exclude-tools', '--append-system-prompt',
                   '--output-format', '--max-wall-time', '--max-session-turns']
AUTOMATION_FLAGS = ['--append-system-prompt', '--output-format',
                    '--approval-mode', '--max-wall-time']


def load_runtime(path=None, *, executable=None):
    config = json.loads(Path(path or CONFIG).read_text(encoding='utf-8'))
    if not isinstance(config, dict) or config.get('schema_version') != 1:
        raise ValueError('Runtime needs schema_version=1')
    if config.get('protocol') != 'headless-json-v1':
        raise ValueError('Unsupported CLI protocol; use an explicit command adapter')
    for key in ('backend', 'executable'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f'Runtime needs {key}')
    if executable is not None:
        if not isinstance(executable, str) or not executable.strip():
            raise ValueError('CLI executable must not be empty')
        config['executable'] = executable
    return config


def add_runtime_arguments(parser):
    parser.add_argument('--cli', help='CLI executable/path (default: qa-runtime.json)')
    parser.add_argument('--runtime-config', type=Path, help='Explicit compatible runtime configuration')


def preflight(runtime, flags, *, expected_version=None):
    executable = shutil.which(runtime['executable'])
    if not executable:
        raise ValueError(f"CLI unavailable: {runtime['executable']}")
    help_result = subprocess.run([executable, '--help'], capture_output=True, text=True, timeout=15)
    # Match complete options: --bare-mode does not establish support for --bare.
    help_text = help_result.stdout + '\n' + help_result.stderr
    missing = [flag for flag in flags if not re.search(
        r'(?<![\w-])' + re.escape(flag) + r'(?![\w-])', help_text)]
    if help_result.returncode or missing:
        raise ValueError('Unsupported required CLI flags: ' + ', '.join(missing))
    version_result = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=15)
    version = version_result.stdout.strip() if version_result.returncode == 0 else None
    if expected_version is not None and version != expected_version:
        raise ValueError('CLI version differs from the authorized plan')
    return executable, version


def headless_command(executable, prompt, system, *, max_wall_time, model=None,
                     isolated=False, denied_tools=(), max_session_turns=4):
    command = [executable, '-p', prompt, '--append-system-prompt', system, '--output-format', 'json']
    if isolated:
        if not denied_tools:
            raise ValueError('Isolated execution requires an explicit tool denylist')
        command += ['--bare', '--exclude-tools', *denied_tools]
    else:
        command += ['--approval-mode', 'auto-edit']
    command += ['--max-wall-time', max_wall_time]
    if isolated:
        command += ['--max-session-turns', str(max_session_turns)]
    if model:
        command += ['--model', model]
    return command


def runtime_identity(manifest):
    """Old measurements remain explicitly attributed to their original runtime."""
    if 'cli_version' in manifest or 'cli_executable_sha256' in manifest:
        return (manifest.get('backend'), manifest.get('cli_version'),
                manifest.get('cli_executable_sha256'))
    return ('qwen', manifest.get('qwen_version'), manifest.get('qwen_executable_sha256'))


def assert_same_runtime(left, right):
    for manifest in (left, right):
        keys = ('backend', 'cli_version', 'cli_executable_sha256') if 'cli_version' in manifest else ('qwen_version', 'qwen_executable_sha256')
        if any(key not in manifest for key in keys):
            raise ValueError('Runtime identity is incomplete')
    if runtime_identity(left) != runtime_identity(right):
        raise ValueError('Comparison conditions differ: CLI runtime identity')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_runtime_arguments(parser)
    parser.add_argument('--mode', choices=('automation', 'isolated', 'gateway'), default='isolated')
    args = parser.parse_args()
    try:
        runtime = load_runtime(args.runtime_config, executable=args.cli)
        flags = list(AUTOMATION_FLAGS if args.mode == 'automation' else ISOLATION_FLAGS)
        if args.mode == 'gateway': flags += ['--mcp-config', '--allowed-tools']
        executable, version = preflight(runtime, flags)
        print(json.dumps(dict(backend=runtime['backend'], executable=executable,
                              cli_version=version, required_flags=flags, live_execution='not_started')))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Runtime preflight error: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
