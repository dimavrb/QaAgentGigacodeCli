"""Validate a staged QA release using trusted code, before replacing a working checkout."""
import ast
from datetime import date
import json
from pathlib import Path
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

from package_agent import ROOT_FILES, TREES, SKIP, build_package
from qa_project import package_info

ENTRYPOINTS = ('qa_start', 'qa_project', 'qa_project_agents', 'qa_extension_mcp', 'qa_recover')


def ci_passed(commit):
    url = ('https://api.github.com/repos/dimavrb/QaAgentGigacodeCli/actions/workflows/ci.yml/runs'
           f'?head_sha={commit}&event=push&per_page=20')
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                                  'User-Agent': 'qa-agent-release-check'})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return False
        value = json.loads(data)
        if not isinstance(value, dict) or not isinstance(value.get('workflow_runs'), list):
            return False
        runs = [run for run in value['workflow_runs'] if isinstance(run, dict)
                and run.get('head_sha') == commit and run.get('head_branch') == 'main'
                and run.get('event') == 'push']
        if not runs:
            return False
        latest = max(runs, key=lambda run: run.get('run_number', 0) if type(run.get('run_number', 0)) is int else -1)
        return latest.get('status') == 'completed' and latest.get('conclusion') == 'success'
    except (OSError, ValueError, RecursionError, urllib.error.URLError):
        return False


def unpack(archive, destination):
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.infolist()) > 10000 or sum(i.file_size for i in bundle.infolist()) > 64 * 1024 * 1024:
            raise ValueError('candidate archive exceeds release limits')
        seen = set()
        for item in bundle.infolist():
            relative = Path(item.filename)
            mode = item.external_attr >> 16
            if (relative.is_absolute() or '..' in relative.parts or '\\' in item.filename
                    or item.filename in seen or mode & 0o170000 == 0o120000):
                raise ValueError('unsafe candidate archive')
            seen.add(item.filename)
            target = destination / relative
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bundle.read(item))


def validate(root):
    info = package_info(root)
    if (info['agent_count'] < 14 or info['skill_count'] < 6 or
            not {'qa', 'qa:init', 'qa:doctor', 'qa:tasks', 'qa:resume'} <= set(info['commands'])):
        raise ValueError('candidate is missing required agents, skills or commands')
    scripts = root / 'scripts'
    trees = {}
    for tree in TREES:
        for path in (root / tree).rglob('*.py'):
            if not any(part in SKIP for part in path.parts):
                trees[path] = ast.parse(path.read_bytes(), filename=str(path))
    pending, checked = list(ENTRYPOINTS), set()
    while pending:
        name = pending.pop()
        if name in checked:
            continue
        checked.add(name)
        path = scripts / (name + '.py')
        if path not in trees:
            raise ValueError(f'missing launcher dependency: {name}')
        for node in ast.walk(trees[path]):
            imports = ([alias.name.split('.')[0] for alias in node.names] if isinstance(node, ast.Import)
                       else [node.module.split('.')[0]] if isinstance(node, ast.ImportFrom) and node.module else [])
            for dependency in imports:
                if dependency in sys.stdlib_module_names:
                    continue
                if not (scripts / (dependency + '.py')).is_file():
                    raise ValueError(f'missing launcher dependency: {dependency}')
                pending.append(dependency)
    with tempfile.TemporaryDirectory(prefix='qa-release-manifest-') as temporary:
        archive = build_package(root, Path(temporary).resolve(), date.today().isoformat())
        with zipfile.ZipFile(archive) as bundle:
            manifest = json.loads(bundle.read('qa-agent/PACKAGE_MANIFEST.json'))
    return info, {item['path']: item['sha256'] for item in manifest['files']}


def probe(root, runner):
    # Fresh interpreter, no project cwd, no inherited PYTHONPATH, no bytecode writes.
    code = ('import sys,json,io,contextlib; from pathlib import Path; '
            'sys.path.insert(0,str(Path(sys.argv[1])/"scripts")); '
            'import qa_start,qa_project_agents,qa_extension_mcp,qa_recover; '
            'from qa_project import package_info; root=Path(sys.argv[1]); '
            'sys.argv=[str(root/"scripts/qa_start.py"),"--help"];\n'
            'with contextlib.redirect_stdout(io.StringIO()):\n'
            ' try: qa_start.main()\n'
            ' except SystemExit as exc:\n'
            '  if exc.code: raise\n'
            'response=qa_extension_mcp.handle({"jsonrpc":"2.0","id":1,"method":"initialize",'
            '"params":{"protocolVersion":"2025-06-18"}}); '
            'assert "result" in response; print(json.dumps(package_info(root)))')
    result = runner([sys.executable, '-I', '-B', '-c', code, str(root)], cwd=root, timeout=20)
    try:
        info = json.loads(result.stdout)
        if result.returncode or info['package_root'] != str(root.resolve()):
            raise ValueError('wrong probe result')
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('fresh launcher/MCP import probe failed; release was not accepted') from exc
    return info
