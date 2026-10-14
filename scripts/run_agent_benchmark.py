#!/usr/bin/env python3
"""Execute a bounded reasoning baseline with CLI; semantic grading is independent."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import agent_trace
import agent_runtime
from check_benchmark_review import SUITE, load_suite, make_template
from run_test_automation import captured_text, duration_seconds
from profile_skills import PROFILE_SKILLS

ROOT = Path(__file__).resolve().parents[1]
ROLES = {'selection': 'qa-test-plan-writer', 'unknown': 'qa-test-plan-writer',
         'assertion': 'qa-autotest-reviewer', 'message': None, 'blocker': 'qa-feature-tester'}
# Explicit deny rules, including discovery/delegation bridges. Bare mode disables automatic
# workspace memory, extensions and MCP. Unsupported flags are a preflight error, never relaxed.
DENIED_TOOLS = ['read_file', 'read_many_files', 'list_directory', 'glob', 'grep_search',
                'run_shell_command', 'write_file', 'edit', 'notebook_edit', 'web_fetch',
                'web_search', 'task', 'agent', 'skill', 'tool_search', 'tool_call',
                'send_message', 'workflow', 'monitor', 'save_memory', 'todo_write']
NEEDED_FLAGS = agent_runtime.ISOLATION_FLAGS


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def terminal_envelope(raw):
    value = json.loads(raw)
    if isinstance(value, list):
        if not value or not isinstance(value[-1], dict) or value[-1].get('type') != 'result':
            raise ValueError('Missing terminal result event')
        value = value[-1]
    if not isinstance(value, dict):
        raise ValueError('CLI returned an invalid envelope')
    return value


def terminal_response(raw):
    value = terminal_envelope(raw)
    if value.get('is_error') is True:
        raise ValueError('CLI returned an error or invalid envelope')
    response = value.get('response', value.get('result', value.get('structured_result')))
    if isinstance(response, dict):
        response = json.dumps(response, ensure_ascii=False, indent=2)
    if not isinstance(response, str) or not response.strip():
        raise ValueError('Empty response')
    # This baseline evaluates reasoning, so tool use invalidates comparability.
    stats = value.get('stats', {})
    if isinstance(stats, dict) and isinstance(stats.get('tools'), dict):
        if stats['tools'].get('totalCalls', 0) != 0:
            raise ValueError('Tool calls are outside the reasoning baseline')
    return response, value


def instructions(case_id, self_review, check_quality=True, role=None, profile_skills=False):
    paths = [ROOT / 'docs/COORDINATOR.md', ROOT / 'docs/TEST_DESIGN.md', ROOT / 'docs/TEST_CASE_STYLE.md']
    role = role or ROLES.get(case_id)
    if role:
        from qa_contract import ROLE_EVENTS
        if role not in ROLE_EVENTS:
            raise ValueError('Unknown benchmark role')
        paths.append(ROOT / '.gigacode/agents' / f'{role}.md')
    if role in ('qa-analyst', 'qa-test-plan-writer'):
        paths.append(ROOT / 'docs/TEST_PLAN.md')
        paths.append(ROOT / 'docs/TEST_CASE_SELECTION.md')
    if role == 'qa-test-plan-writer':
        paths.append(ROOT / '.gigacode/skills/qa-test-cases/SKILL.md')
    if role in ('qa-test-automation', 'qa-autotest-reviewer'):
        paths.append(ROOT / 'docs/AUTOTEST_QUALITY.md')
        paths.append(ROOT / 'docs/AUTOTEST_NAMING.md')
    if role == 'qa-test-data-builder':
        paths += [ROOT / 'docs/TEST_DATA_CHECKLIST.md', ROOT / 'docs/TEST_DATA.md']
    if role == 'qa-stub-builder':
        paths.append(ROOT / 'docs/STUBS.md')
    if role == 'qa-impact-analyst':
        paths.append(ROOT / 'docs/IMPACT_ANALYSIS.md')
    if profile_skills and role in PROFILE_SKILLS:
        paths.append(ROOT / PROFILE_SKILLS[role])
    if check_quality:
        paths.append(ROOT / '.gigacode/skills/qa-check-quality/SKILL.md')
    if self_review:
        paths.append(ROOT / 'docs/SELF_REVIEW.md')
    snapshots = {str(path.relative_to(ROOT)): path.read_text(encoding='utf-8') for path in paths}
    body = '\n\n'.join(f'Источник инструкций: {name}\n{text}' for name, text in snapshots.items())
    override = '''\n\nУсловия этого учебного reasoning benchmark имеют приоритет для запуска:
Используй только сведения задания. Инструменты, чтение файлов, внешние действия и делегирование отключены.
Не создавай реестр и файлы. Представь нужные каталоги/артефакты прямо в окончательном ответе.
Не заявляй реальных запусков и изменений. Не ищи контрольные ответы или рубрику.
Верни окончательный ответ в соответствии с заданием, включая существенные ограничения.
'''
    override += ('Перед ответом выполни самопроверку по переданному стандарту; сохрани существенные исправления и ограничения.'
                 if self_review else 'Для этой контрольной конфигурации отдельная самопроверка выключена; не выполняй дополнительный цикл self-review.')
    if not check_quality:
        override += '\nДля этой контрольной конфигурации общий qa-check-quality выключен; используй остальные переданные стандарты без загрузки этого skill.'
    return body + override, snapshots


def usage_tokens(envelope):
    usage = envelope.get('usage')
    if not isinstance(usage, dict):
        return None, None
    def get(*names):
        for name in names:
            value = usage.get(name)
            if type(value) is int and value >= 0:
                return value
        return None
    return get('input_tokens', 'prompt_tokens'), get('output_tokens', 'completion_tokens')


def run_benchmark(output, *, cli=None, repeats=1, model=None, max_wall_time='5m', self_review=True,
                  suite_path=SUITE, check_quality=True, profile_skills=False, measurement_kind='live', runtime_config=None):
    runtime = agent_runtime.load_runtime(runtime_config, executable=cli)
    seconds = duration_seconds(max_wall_time)
    if type(repeats) is not int or not 1 <= repeats <= 20:
        raise ValueError('repeats must be 1..20')
    if type(profile_skills) is not bool or measurement_kind not in ('live', 'fixture'):
        raise ValueError('Invalid profile_skills or measurement_kind')
    suite_path = Path(suite_path)
    suite, fingerprint = load_suite(suite_path)
    suite_raw = suite_path.read_bytes()
    prompts = {case['case_id']: (suite_path.parent / case['prompt_file']).read_bytes() for case in suite['cases']}
    digest = hashlib.sha256(suite_raw)
    for case in suite['cases']:
        digest.update(case['prompt_file'].encode())
        digest.update(prompts[case['case_id']])
    if digest.hexdigest() != fingerprint:
        raise ValueError('Suite changed while taking a snapshot')
    systems = {case['case_id']: instructions(case['case_id'], self_review, check_quality, case.get('agent'), profile_skills) for case in suite['cases']}
    output = Path(output).resolve()
    # Exclusive directory reservation prevents accidental replacement of a baseline.
    output.mkdir(parents=True, exist_ok=False)
    (output / 'suite.json').write_bytes(suite_raw)
    for case in suite['cases']:
        destination = output / case['prompt_file']
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(prompts[case['case_id']])
    trace_id = agent_trace.secrets.token_hex(16)
    ctx = agent_trace.context('benchmark:' + output.name, trace_id=trace_id, agent='benchmark-runner')
    manifest = dict(schema_version=1, status='preflight', mode='reasoning_only',
                    suite_sha256=fingerprint, repeats=repeats, model_requested=model,
                    self_review=self_review, check_quality=check_quality, profile_skills=profile_skills,
                    measurement_kind=measurement_kind, max_wall_time=max_wall_time,
                    memory='disabled', semantic_quality='not_reviewed',
                    trace_id=trace_id, trials=[], backend=runtime['backend'], **agent_trace.package_identity())
    write_json(output / 'run.json', manifest)
    trace = output / 'trace.jsonl'
    agent_trace.append(trace, ctx, 'benchmark_started', repeats=repeats)
    executable = shutil.which(runtime['executable'])
    manifest['cli_executable_sha256'] = sha(Path(executable).read_bytes()) if executable else None
    error = None
    cli_version = None
    if not executable:
        error = 'CLI unavailable: ' + runtime['executable']
    else:
        try:
            executable, cli_version = agent_runtime.preflight(runtime, NEEDED_FLAGS)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            error = f'CLI preflight failed: {exc}'
    manifest['cli_version'] = cli_version
    if error:
        manifest.update(status='unavailable', error=error)
        write_json(output / 'run.json', manifest)
        agent_trace.append(trace, ctx, 'benchmark_unavailable', error_category='preflight')
        return manifest
    manifest['status'] = 'running'
    write_json(output / 'run.json', manifest)
    successful = True
    for repeat in range(1, repeats + 1):
        directory = output / f'trial-{repeat}'
        directory.mkdir()
        review = make_template(suite, fingerprint, manifest['agent_version'])
        review['model'] = model  # Unknown actual provider/model is never guessed.
        review['configuration'] = f'reasoning_only;self_review={self_review};check_quality={check_quality};profile_skills={profile_skills};memory=disabled;cli={cli_version}'
        for row in review['cases']:
            cid = row['case_id']
            case = next(case for case in suite['cases'] if case['case_id'] == cid)
            prompt = prompts[cid].decode('utf-8')
            system, snapshots = systems[cid]
            case_dir = directory / cid
            case_dir.mkdir()
            write_json(case_dir / 'instructions.json', snapshots)
            (case_dir / 'prompt.md').write_text(prompt, encoding='utf-8')
            (case_dir / 'system.md').write_text(system, encoding='utf-8')
            attempt = agent_trace.secrets.token_hex(16)
            case_context = agent_trace.context(ctx['task_id'], f'{repeat}:{cid}', attempt,
                                               case.get('agent') or ROLES.get(cid) or 'qa-coordinator', trace_id, stage='benchmark')
            agent_trace.append(trace, case_context, 'runner_started', backend=runtime['backend'],
                               prompt_sha256=sha(prompt.encode()), instructions_sha256=sha(system.encode()))
            command = agent_runtime.headless_command(executable, prompt, system,
                max_wall_time=max_wall_time, model=model, isolated=True, denied_tools=DENIED_TOOLS)
            started = time.monotonic()
            status, error_message, envelope = 'captured', None, {}
            try:
                # No rubric, previous response or repository files in a case's working directory.
                with tempfile.TemporaryDirectory(prefix='qa-benchmark-case-') as workspace:
                    completed = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=seconds)
            except subprocess.TimeoutExpired as exc:
                completed = subprocess.CompletedProcess(command, 124, captured_text(exc.stdout), captured_text(exc.stderr))
                status, error_message = 'timed_out', 'Wall time exceeded'
            except OSError as exc:
                completed = subprocess.CompletedProcess(command, 127, '', '')
                status, error_message = 'failed', type(exc).__name__
            elapsed = round(time.monotonic() - started, 3)
            (case_dir / 'raw.json').write_text(completed.stdout, encoding='utf-8')
            (case_dir / 'stderr.txt').write_text(completed.stderr, encoding='utf-8')
            try:
                # Capture usage even when the final answer is empty or CLI reports failure.
                # Parsing and usability of the answer are separate gates.
                envelope = terminal_envelope(completed.stdout)
                if completed.returncode:
                    raise ValueError(f'CLI exit {completed.returncode}')
                response, envelope = terminal_response(completed.stdout)
                (case_dir / 'response.md').write_text(response, encoding='utf-8')
                row.update(output_artifact=f'{cid}/response.md', output_sha256=sha(response.encode()))
            except (ValueError, TypeError) as exc:
                if status == 'captured':
                    status, error_message = 'failed', str(exc)
                successful = False
            row['seconds'] = elapsed
            row['input_tokens'], row['output_tokens'] = usage_tokens(envelope)
            result = dict(trial=repeat, case_id=cid, status=status, seconds=elapsed,
                          exit_code=completed.returncode, error=error_message,
                          raw_sha256=sha(completed.stdout.encode()),
                          prompt_sha256=sha(prompt.encode()), instructions_sha256=sha(system.encode()),
                          reported_usage=envelope.get('usage'), reported_stats=envelope.get('stats'),
                          semantic_quality='not_reviewed')
            write_json(case_dir / 'execution.json', result)
            manifest['trials'].append(result)
            agent_trace.append(trace, case_context, 'runner_finished',
                               duration_seconds=elapsed, exit_code=completed.returncode,
                               capture_status=status, input_tokens=row['input_tokens'], output_tokens=row['output_tokens'])
            write_json(output / 'run.json', manifest)
        write_json(directory / 'review.json', review)
    manifest['status'] = 'captured' if successful else 'failed'
    write_json(output / 'run.json', manifest)
    agent_trace.append(trace, ctx, 'benchmark_finished', capture_status=manifest['status'],
                       semantic_quality='not_reviewed')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    agent_runtime.add_runtime_arguments(parser)
    parser.add_argument('--model')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--max-wall-time', default='5m')
    parser.add_argument('--self-review', choices=['on', 'off'], default='on')
    parser.add_argument('--suite', type=Path, default=SUITE)
    parser.add_argument('--check-quality', choices=['on', 'off'], default='on')
    parser.add_argument('--profile-skills', choices=['on', 'off'], default='off')
    parser.add_argument('--measurement-kind', choices=['live', 'fixture'], default='live',
                        help='Use fixture for synthetic CLI adapters; never count them as model measurements')
    args = parser.parse_args()
    try:
        result = run_benchmark(args.output_dir, cli=args.cli, model=args.model,
                               repeats=args.repeats, max_wall_time=args.max_wall_time,
                               self_review=args.self_review == 'on', suite_path=args.suite,
                               check_quality=args.check_quality == 'on', profile_skills=args.profile_skills == 'on',
                               measurement_kind=args.measurement_kind, runtime_config=args.runtime_config)
        print(json.dumps(dict(status=result['status'], semantic_quality=result['semantic_quality'],
                              output_dir=str(args.output_dir.resolve()), error=result.get('error')), ensure_ascii=False))
        return 0 if result['status'] == 'captured' else 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Benchmark error: {exc}')
        return 2
    except KeyboardInterrupt:
        manifest_path = args.output_dir / 'run.json'
        if manifest_path.is_file():
            result = json.loads(manifest_path.read_text())
            result['status'] = 'interrupted'
            write_json(manifest_path, result)
            ctx = agent_trace.context('benchmark:' + args.output_dir.resolve().name,
                                       trace_id=result['trace_id'], agent='benchmark-runner')
            agent_trace.append(args.output_dir / 'trace.jsonl', ctx, 'benchmark_interrupted')
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
