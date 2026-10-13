#!/usr/bin/env python3
"""Run one CLI automation task in a separate test repository."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from qa_contract import parse_message, validate_message
from self_review import validate_self_review
from failure_journal import append_entry
from read_test_data_guide import build_context, selected_sections
import agent_trace
import agent_runtime


def save_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def task_field(task: str, name: str) -> str | None:
    matches = re.findall(rf"^{name}:\s*(\S+)\s*$", task, flags=re.MULTILINE)
    return matches[0] if len(matches) == 1 else None


def duration_seconds(value: str) -> float:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh])", value)
    if not match:
        raise ValueError("Use a positive duration such as 30s, 20m or 1h")
    seconds = float(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]
    if seconds <= 0:
        raise ValueError("Duration must be greater than zero")
    return seconds


def captured_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="Existing test repository or isolated worktree")
    parser.add_argument("--task-file", required=True, type=Path, help="UTF-8 task description")
    parser.add_argument("--output", required=True, type=Path, help="Path for CLI JSON output")
    parser.add_argument("--metrics", type=Path, help="Path for run metrics (default: <output>.metrics.json)")
    parser.add_argument("--state", type=Path, help="Path for task state (default: beside task file, keyed by task ID and repository)")
    parser.add_argument("--task-state", type=Path, help="Shared task registry with a prepared qa-test-automation assignment")
    parser.add_argument("--failure-journal", type=Path, help="Shared incident JSONL (default: failures.jsonl beside task state)")
    parser.add_argument("--max-wall-time", default="20m", help="CLI run time limit")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow existing changes in test repository")
    agent_runtime.add_runtime_arguments(parser)
    args = parser.parse_args()

    try:
        runtime = agent_runtime.load_runtime(args.runtime_config, executable=args.cli)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    repo = args.repo.expanduser().resolve()
    task_file = args.task_file.expanduser().resolve()
    output = args.output.expanduser().resolve()
    metrics = args.metrics.expanduser().resolve() if args.metrics else output.with_name(output.name + ".metrics.json")
    trace_path = output.with_name(output.name + '.trace.jsonl')
    agent_file = Path(__file__).resolve().parents[1] / ".gigacode" / "agents" / "qa-test-automation.md"
    quality_guide = Path(__file__).resolve().parents[1] / "docs" / "AUTOTEST_QUALITY.md"
    naming_guide = Path(__file__).resolve().parents[1] / "docs" / "AUTOTEST_NAMING.md"
    case_style = Path(__file__).resolve().parents[1] / "docs" / "TEST_CASE_STYLE.md"
    data_guide = Path(__file__).resolve().parents[1] / "docs" / "TEST_DATA.md"
    data_checklist = data_guide.with_name("TEST_DATA_CHECKLIST.md")
    data_selector = Path(__file__).resolve().with_name("read_test_data_guide.py")
    self_review_guide = Path(__file__).resolve().parents[1] / 'docs' / 'SELF_REVIEW.md'
    check_quality = Path(__file__).resolve().parents[1] / '.gigacode/skills/qa-check-quality/SKILL.md'
    input_paths = (task_file, agent_file, quality_guide, naming_guide, case_style, data_guide,
                   data_checklist, data_selector, self_review_guide, check_quality)

    if not repo.is_dir():
        parser.error(f"Test repository does not exist: {repo}")
    if not task_file.is_file():
        parser.error(f"Task file does not exist: {task_file}")
    if not quality_guide.is_file():
        parser.error(f"Automation quality guide does not exist: {quality_guide}")
    if not self_review_guide.is_file():
        parser.error(f'Self-review guide does not exist: {self_review_guide}')
    for guide in (naming_guide, case_style):
        if not guide.is_file():
            parser.error(f"Automation naming guide does not exist: {guide}")
    if not data_guide.is_file():
        parser.error(f"Test data guide does not exist: {data_guide}")
    try:
        data_context = build_context(data_guide.parent.parent, "qa-test-automation")
    except (OSError, ValueError, UnicodeError) as exc:
        parser.error(f"Test data context is unavailable: {exc}")
    if output == metrics or any(path in input_paths for path in (output, metrics, trace_path)):
        parser.error("Output and metrics must have distinct paths and must not overwrite input files")
    try:
        wall_time_seconds = duration_seconds(args.max_wall_time)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        executable, cli_version = agent_runtime.preflight(runtime, agent_runtime.AUTOMATION_FLAGS)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))

    git_probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=repo, capture_output=True, text=True, check=False
    )
    if git_probe.returncode != 0 or Path(git_probe.stdout.strip()).resolve() != repo:
        parser.error("--repo must be the root of a Git checkout or isolated worktree")

    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=False)
    if status.returncode != 0:
        parser.error("Could not read Git status of test repository")
    if status.stdout.strip() and not args.allow_dirty:
        parser.error("Test repository has local changes; use an isolated worktree or --allow-dirty")

    task = task_file.read_text(encoding="utf-8").strip()
    if not task:
        parser.error("Task file is empty")
    task_id = task_field(task, "task_id")
    request_id = task_field(task, "request_id")
    if not task_id or not request_id:
        parser.error("Task file must contain unique task_id: and request_id: lines")
    state_key = hashlib.sha256(f"{repo}\0{task_id}".encode("utf-8")).hexdigest()[:20]
    state_path = args.state.expanduser().resolve() if args.state else task_file.parent / ".qa-agent-state" / f"{state_key}.json"
    task_state = args.task_state.expanduser().resolve() if args.task_state else None
    if state_path in (output, metrics, trace_path, *input_paths):
        parser.error("State must not overwrite output, metrics or input files")
    if state_path.is_relative_to(repo):
        parser.error("State must be outside the test repository; use --state with an external path")
    journal_path = args.failure_journal.expanduser().resolve() if args.failure_journal else state_path.parent / 'failures.jsonl'
    protected = {output, metrics, trace_path, *input_paths, state_path,
                 state_path.with_name(state_path.name + '.lock')}
    if task_state:
        if task_state.is_relative_to(repo) or task_state in protected or task_state.with_name(task_state.name + '.lock') in protected:
            parser.error('Shared task state must be outside the test repository and distinct from run files')
        protected.update((task_state, task_state.with_name(task_state.name + '.lock')))
    if journal_path.is_relative_to(repo) or journal_path in protected or journal_path.with_name(journal_path.name + '.lock') in protected:
        parser.error("Failure journal and its lock must be outside the test repository and distinct from run files")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Hold the lock through the run so two invocations cannot reserve or update the same state concurrently.
    state_lock = state_path.with_name(state_path.name + ".lock").open("a+")
    fcntl.flock(state_lock, fcntl.LOCK_EX)
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            parser.error(f"Could not read task state: {exc}")
        if (
            not isinstance(state, dict) or state.get("task_id") != task_id
            or state.get("repo") not in (None, str(repo))
            or not isinstance(state.get("requests"), dict)
        ):
            parser.error("State file belongs to another task or has an invalid format")
    else:
        state = {"task_id": task_id, "repo": str(repo), "requests": {}}
    state.setdefault("repo", str(repo))
    if request_id in state["requests"]:
        parser.error(f"request_id {request_id!r} was already started; inspect state before using a new request_id")
    registry = None
    registry_context = None
    if task_state:
        import task_exchange as exchange
        try:
            registry = exchange.read_state(task_state)
            row = registry['requests'].get(request_id)
            if not row or registry['task_id'] != task_id or row['assignment']['agent'] != 'qa-test-automation':
                raise ValueError('Runner assignment is not registered for this task and role')
            if row['status'] != 'dispatching' or row['assignment']['result_path'] != str(output):
                raise ValueError('Prepare this assignment first; its result_path must match --output')
            if row['assignment']['artifact_base'] != str(repo):
                raise ValueError('Runner assignment artifact_base must equal --repo')
            exchange.check_inputs(row)
            registry_context = dict(assignment=row['assignment'], sources=exchange.check_sources(registry, row['assignment']['depends_on']),
                                    answer_sources=exchange.check_answers(registry, row['assignment']['questionnaire_ids']))
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            parser.error(f'Invalid shared task assignment: {exc}')
    role = agent_file.read_text(encoding="utf-8")
    # Frontmatter configures named agents, not this standalone CLI session.
    # Pass only instructions to avoid paying for inert tool declarations.
    if role.startswith("---\n"):
        _, separator, body = role[4:].partition("\n---\n")
        if not separator:
            parser.error("Agent file has unterminated frontmatter")
        role = body.strip()
    # Keep the standalone run's contract small; the full workflow contract is for the coordinator.
    result_contract = (
        f"Верни только JSON-объект с task_id={task_id}, request_id={request_id}, "
        "agent=qa-test-automation, event, summary, artifacts (массив строк), "
        "blockers (массив строк) и payload (объект). "
        "В summary назови конкретное поведение или блокер и степень подтверждения; "
        "не выдумывай последствия и причину, подробности сохрани в артефактах. "
        "event: ready_for_review, automation_complete (изменения не нужны), "
        "needs_analysis, needs_environment, needs_test_data или blocked. "
        "Для automation_complete payload содержит coverage_reference, run_result, reason_no_change (строки). "
        "Для needs_* опиши конкретный запрос в payload; для blocked заполни blockers. "
        "Для ready_for_review payload содержит scenario_id, requirement_source, "
        "base_revision, current_revision, changed_files (массив строк), "
        "diff_artifact, run_result и limitations (массив строк). "
        "Укажи в run_result итог запуска или причину его отсутствия. "
        "Для названий тестов и Allure-шагов примени переданный стандарт именования; "
        "сохраняй scenario_id, связь с источником и assert. "
        "Проверку кода и фактического отображения различай; недоступное отображение укажи в limitations. "
        "Длинные логи и diff сохрани как артефакты; не копируй их в ответ."
        " Обязателен self_review по переданному стандарту: четыре проверки, исправления и оставшиеся риски."
    )
    prompt = (
        "Выполни это QA-задание в текущем репозитории автотестов. "
        "Сначала изучи применимые инструкции репозитория. "
        f"Стандарт качества автотестов доступен по абсолютному пути {quality_guide}; "
        "прочитай применимые разделы, используя этот путь вместо относительной ссылки роли. "
        f"Обязательный стандарт названий автотестов и Allure-шагов: {naming_guide}; "
        f"общий стандарт названия кейса: {case_style}. "
        "Чек-лист данных и раздел recipe уже переданы в системных инструкциях; не перечитывай их. "
        f"Справочник данных: {data_guide}; дополнительные разделы читай командой "
        f"python3 {data_selector} --role qa-test-automation --only-sections --section <ключ>. "
        "composition нужен при разработке набора, isolation при общих данных/повторах/времени, "
        "cleanup при изменении подготовки/очистки, profile при отсутствии профиля. "
        "Остальные стандарты читай только нужными разделами: качество по виду изменения, "
        "названия тестов/шагов и API используемого адаптера; общий стандарт кейса только при изменении названия/каталога. "
        f"Корень QA-пакета для ссылок docs/ и examples/ — {naming_guide.parent.parent}. "
        f"Перед возвратом выполни короткую самопроверку по {self_review_guide}; сохрани её в self_review. "
        f"Для существенных assert применяй общий skill качества по {check_quality}; передавай его reviewer. "
        "Верни изменённые файлы, результаты проверок, ограничения и материалы для ревью.\n\n"
        f"Задание:\n{task}"
    )
    system_prompt = f"{role}\n\nПодготовка данных — обязательное ядро:\n{data_context}\nКонтракт результата:\n{result_contract}"
    if registry_context:
        prompt += '\n\nПроверенное назначение координатора и ссылки на принятые источники:\n' + json.dumps(registry_context, ensure_ascii=False)
    command = agent_runtime.headless_command(executable, prompt, system_prompt,
        max_wall_time=args.max_wall_time)

    output.parent.mkdir(parents=True, exist_ok=True)
    metrics.parent.mkdir(parents=True, exist_ok=True)
    if registry:
        try:
            exchange.start_request(task_state, request_id, f'runner-pid:{os.getpid()}',
                                   registry['requests'][request_id]['delivery_id'], registry['revision'])
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            parser.error(f'Could not reserve shared assignment: {exc}')
    state["requests"][request_id] = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "output": str(output),
        "metrics": str(metrics),
    }
    save_json(state_path, state)
    if task_state:
        trace_context = agent_trace.registry_context(exchange.read_state(task_state), request_id)
    else:
        trace_context = agent_trace.context(task_id, request_id, agent_trace.secrets.token_hex(16), 'qa-test-automation', stage='automation')
    trace_context['instructions_sha256'] = hashlib.sha256(system_prompt.encode()).hexdigest()
    trace_context['model'] = None
    def emit_trace(kind, **fields):
        try:
            agent_trace.append(trace_path, trace_context, kind, **fields)
            return None
        except (OSError, ValueError, TypeError) as exc:
            print(f'Could not record trace: {exc}', file=sys.stderr)
            return str(exc)
    trace_error = emit_trace('runner_started', backend=runtime['backend'])
    if trace_error:
        state['requests'][request_id].update(status='failed', trace_error=trace_error)
        save_json(state_path, state)
        if task_state:
            current = exchange.read_state(task_state)
            exchange.fail_request(task_state, request_id, 'Trace unavailable before execution', current['revision'])
        return 2
    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False, timeout=wall_time_seconds)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        completed = subprocess.CompletedProcess(command, 124, captured_text(exc.stdout), captured_text(exc.stderr))
    except OSError as exc:
        completed = subprocess.CompletedProcess(command, 127, "", str(exc))
    except KeyboardInterrupt:
        emit_trace('runner_interrupted')
        state["requests"][request_id]["status"] = "interrupted"
        state["requests"][request_id]["finished_at"] = datetime.now(timezone.utc).isoformat()
        save_json(state_path, state)
        if task_state:
            try:
                current = exchange.read_state(task_state)
                exchange.fail_request(task_state, request_id, 'Runner interrupted', current['revision'])
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(f'Could not record shared interruption: {exc}', file=sys.stderr)
        print("CLI run interrupted", file=sys.stderr)
        return 130

    elapsed = time.monotonic() - started
    output.write_text(completed.stdout, encoding="utf-8")
    reported_usage = None
    message = None
    validation_errors = []
    try:
        message, result = parse_message(completed.stdout)
        validation_errors = validate_message(message, task_id, request_id, "qa-test-automation")
        review_errors = validate_self_review(message, registry_context['assignment'] if registry_context else None, required=True)
        validation_errors.extend(error for error in review_errors if error not in validation_errors)
        if isinstance(result.get("usage"), dict):
            reported_usage = result["usage"]
    except (json.JSONDecodeError, ValueError) as exc:
        validation_errors = [str(exc)]
        try:
            result = json.loads(completed.stdout)
            if isinstance(result, dict) and isinstance(result.get("usage"), dict):
                reported_usage = result["usage"]
        except json.JSONDecodeError:
            pass
    if timed_out:
        validation_errors.append(f"CLI exceeded the {args.max_wall_time} wall-time limit")
    registry_error = None
    if task_state:
        try:
            if completed.returncode == 0 and not validation_errors:
                exchange.submit_result(task_state, request_id)
            else:
                current = exchange.read_state(task_state)
                exchange.fail_request(task_state, request_id, 'Runner did not return a valid successful result', current['revision'])
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            registry_error = str(exc)
            validation_errors.append('Shared task exchange: ' + registry_error)
            try:
                exchange.fail_request(task_state, request_id, 'Shared result registration failed: ' + registry_error, None)
            except (OSError, ValueError, KeyError, TypeError):
                pass
    valid_result = completed.returncode == 0 and not validation_errors
    request_state = state["requests"][request_id]
    request_state["status"] = "reported" if valid_result else ("timed_out" if timed_out else "failed")
    request_state["finished_at"] = datetime.now(timezone.utc).isoformat()
    request_state["event"] = message.get("event") if valid_result else None
    request_state["validation_errors"] = validation_errors
    if valid_result:
        request_state["artifacts"] = message["artifacts"]
        if message["event"] == "ready_for_review":
            request_state["review_status"] = "pending"
            request_state["current_revision"] = message["payload"]["current_revision"]
    save_json(state_path, state)
    failure_incident_id = None
    failure_journal_error = None
    symptom = ('timeout' if timed_out else 'tool_error' if completed.returncode != 0 else
               'invalid_result' if not valid_result else
               'blocked_execution' if message['event'] == 'blocked' else None)
    if symptom:
        failure_incident_id = 'AUTO-' + hashlib.sha256(f'{repo}\0{task_id}\0{request_id}'.encode()).hexdigest()[:24]
        try:
            recorded_version = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip() or 'unknown'
        except OSError:
            recorded_version = 'unknown'
        entry = dict(schema_version=1, event_id=failure_incident_id + '-observed', incident_id=failure_incident_id,
                     occurred_at=request_state['finished_at'], actor='qa-runner', scope_kind='operational',
                     task_id=task_id, request_id=request_id, stage='automation', agent='qa-test-automation',
                     agent_version=recorded_version,
                     summary={'timeout': 'CLI не завершил назначение в установленное время',
                              'tool_error': 'Запуск CLI завершился с ненулевым кодом',
                              'invalid_result': 'Ответ CLI не прошёл структурную проверку',
                              'blocked_execution': 'Исполнитель сообщил блокировку назначения'}[symptom],
                     symptom=symptom, impact='Назначение не завершено принятым результатом; требуется оценка координатора',
                     scenario_ids=[], context=dict(repo=str(repo), exit_code=completed.returncode,
                                                   duration_seconds=round(elapsed, 3), max_wall_time=args.max_wall_time,
                                                   model=None, backend=runtime['backend'], cli_version=cli_version,
                                                   validation_error_count=len(validation_errors)),
                     evidence=[str(output)],
                     cause=dict(status='unknown', category='unknown', description=None, evidence=[]),
                     recovery=dict(outcome='not_attempted', action=None, result=None, evidence=[]))
        try:
            append_entry(journal_path, entry)
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
            failure_journal_error = str(exc)
            print('Could not record operational incident; preserve output and record it after repairing the journal.', file=sys.stderr)
    metrics.write_text(
        json.dumps(
            {
                "backend": runtime["backend"],
                "cli_version": cli_version,
                "duration_seconds": round(elapsed, 3),
                "exit_code": completed.returncode,
                "timed_out": timed_out,
                "explicit_input_characters": len(prompt) + len(system_prompt),
                "test_data_context_characters": len(data_context),
                "test_data_context_sections": list(selected_sections("qa-test-automation")),
                "cli_output_characters": len(completed.stdout),
                "reported_usage": reported_usage,
                "contract_valid": valid_result,
                "event": message.get("event") if valid_result else None,
                "validation_errors": validation_errors,
                "artifact_count": len(message["artifacts"]) if valid_result else None,
                "failure_incident_id": failure_incident_id,
                "failure_journal": str(journal_path) if failure_incident_id else None,
                "failure_journal_error": failure_journal_error,
                "task_state": str(task_state) if task_state else None,
                "task_exchange_error": registry_error,
                "trace_path": str(trace_path),
                "trace_context": trace_context,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    trace_error = emit_trace('runner_finished',
                       duration_seconds=round(elapsed, 3), exit_code=completed.returncode,
                       timed_out=timed_out, contract_valid=valid_result,
                       event=message.get('event') if valid_result else None,
                       reported_usage=reported_usage, validation_error_count=len(validation_errors),
                       output_sha256=hashlib.sha256(output.read_bytes()).hexdigest())
    if trace_error:
        saved_metrics = json.loads(metrics.read_text())
        saved_metrics['trace_error'] = trace_error
        save_json(metrics, saved_metrics)
    if timed_out:
        print(f"CLI exceeded {args.max_wall_time}; partial output: {output}", file=sys.stderr)
        return 124
    if completed.returncode != 0:
        print(f"CLI failed with exit code {completed.returncode}; output: {output}", file=sys.stderr)
        if completed.stderr:
            print(completed.stderr[-2000:], file=sys.stderr)
        return completed.returncode

    if validation_errors:
        print(f"Invalid QA result: {'; '.join(validation_errors)}; output: {output}", file=sys.stderr)
        return 2

    print(f"CLI result: {output}")
    print(f"Run metrics: {metrics}")
    print(f"Task state: {state_path}")
    print(f"Review changes in: {repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
