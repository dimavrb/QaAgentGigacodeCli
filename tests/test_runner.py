import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "run_test_automation.py"
sys.path.insert(0, str(RUNNER.parent))
import task_exchange as exchange
from self_review_fixtures import review_for


class RunnerTests(unittest.TestCase):
    def test_broken_trace_stops_before_cli_without_overwriting_tail(self):
        output = self.root / 'result.json'
        trace = output.with_name(output.name + '.trace.jsonl')
        trace.write_text('{partial')
        argv = self.root / 'args.json'
        self.env['FAKE_GIGACODE_ARGS'] = str(argv)
        result = self.run_task(output)
        self.assertEqual(2, result.returncode)
        self.assertFalse(argv.exists())
        self.assertEqual('{partial', trace.read_text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.task = self.root / "task.md"
        self.task.write_text("task_id: QA-1\nrequest_id: QA-1-auto-1\nцель: проверить фильтр\n", encoding="utf-8")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        fake_cli = bin_dir / "gigacode"
        fake_cli.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys, time, json\n"
            "if '--help' in sys.argv: print('--append-system-prompt --output-format --approval-mode --max-wall-time'); sys.exit(0)\n"
            "if '--version' in sys.argv: print('fixture-gigacode'); sys.exit(0)\n"
            "if os.getenv('FAKE_GIGACODE_ARGS'): open(os.environ['FAKE_GIGACODE_ARGS'], 'w').write(json.dumps(sys.argv))\n"
            "time.sleep(float(os.getenv('FAKE_GIGACODE_SLEEP', '0')))\n"
            "print(os.environ['FAKE_GIGACODE_OUTPUT'])\n"
            "sys.exit(int(os.getenv('FAKE_GIGACODE_EXIT', '0')))\n",
            encoding="utf-8",
        )
        fake_cli.chmod(0o755)
        self.env = os.environ.copy()
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        message = {
            "task_id": "QA-1", "request_id": "QA-1-auto-1", "agent": "qa-test-automation",
            "event": "automation_complete", "summary": "already covered", "artifacts": ["test.py"],
            "blockers": [],
            "self_review": review_for(),
            "payload": {"coverage_reference": "test.py", "run_result": "passed", "reason_no_change": "already covered"},
        }
        self.env["FAKE_GIGACODE_OUTPUT"] = json.dumps({"response": json.dumps(message)})

    def run_task(self, output: Path, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUNNER), "--repo", str(self.repo), "--task-file", str(self.task),
             "--output", str(output), *extra],
            env=self.env, capture_output=True, text=True,
        )

    def shared_assignment(self, output, prepare=True):
        state = self.root / 'shared-task.json'
        exchange.init_task(state, 'QA-1', 'Учебная автоматизация')
        evidence = self.root / 'evidence.txt'
        evidence.write_text('Учебный результат')
        message = json.loads(json.loads(self.env['FAKE_GIGACODE_OUTPUT'])['response'])
        message['artifacts'] = [str(evidence)]
        self.env['FAKE_GIGACODE_OUTPUT'] = json.dumps({'response': json.dumps(message)})
        assignment = dict(task_id='QA-1', request_id='QA-1-auto-1', agent='qa-test-automation', stage='automation',
                          goal='Проверить существующее покрытие', acceptance_criteria=['Запуск подтверждён'],
                          stop_condition='Вернуть отчёт', expected_artifact='JSON и доказательства',
                          result_path=str(output), artifact_base=str(self.repo), inputs=[str(self.task)])
        exchange.assign(state, assignment, self.root, 1)
        if prepare:
            exchange.prepare_handoff(state, 'QA-1-auto-1', 2)
        return state

    def test_shared_registry_records_ready_event_without_auto_accepting_result(self):
        output = self.root / 'shared-result.json'
        state_path = self.shared_assignment(output)
        result = self.run_task(output, '--task-state', str(state_path))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        state = exchange.read_state(state_path)
        row = state['requests']['QA-1-auto-1']
        self.assertEqual('reported', row['status'])
        self.assertIsNone(row['review'])
        self.assertTrue(row['owner_task_id'].startswith('runner-pid:'))
        self.assertEqual(1, len([e for e in state['events'] if e['kind'] == 'result_ready']))
        metrics = json.loads(output.with_name(output.name + '.metrics.json').read_text())
        self.assertEqual(str(state_path.resolve()), metrics['task_state'])
        self.assertIsNone(metrics['task_exchange_error'])
        records = [json.loads(line) for line in Path(metrics['trace_path']).read_text().splitlines()]
        self.assertEqual(['runner_started', 'runner_finished'], [row['kind'] for row in records])
        self.assertEqual(state['trace_id'], records[0]['trace_id'])
        self.assertEqual(row['attempt_id'], records[1]['attempt_id'])
        self.assertTrue(records[1]['contract_valid'])

    def test_shared_registry_blocks_unprepared_assignment_before_cli(self):
        output = self.root / 'shared-result.json'
        state_path = self.shared_assignment(output, prepare=False)
        argv = self.root / 'cli-args.json'
        self.env['FAKE_GIGACODE_ARGS'] = str(argv)
        result = self.run_task(output, '--task-state', str(state_path))
        self.assertEqual(2, result.returncode)
        self.assertIn('Prepare this assignment', result.stderr)
        self.assertFalse(argv.exists())
        self.assertEqual('queued', exchange.read_state(state_path)['requests']['QA-1-auto-1']['status'])

    def test_shared_registry_does_not_publish_missing_evidence_or_nonzero_exit(self):
        output = self.root / 'shared-result.json'
        state_path = self.shared_assignment(output)
        (self.root / 'evidence.txt').unlink()
        result = self.run_task(output, '--task-state', str(state_path))
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        state = exchange.read_state(state_path)
        self.assertEqual('failed', state['requests']['QA-1-auto-1']['status'])
        self.assertFalse(any(e['kind'] == 'result_ready' for e in state['events']))
        metrics = json.loads(output.with_name(output.name + '.metrics.json').read_text())
        self.assertFalse(metrics['contract_valid'])
        self.assertIsNotNone(metrics['task_exchange_error'])

    def test_shared_registry_preserves_failure_when_cli_exits_nonzero(self):
        output = self.root / 'shared-result.json'
        state_path = self.shared_assignment(output)
        self.env['FAKE_GIGACODE_EXIT'] = '53'
        result = self.run_task(output, '--task-state', str(state_path))
        self.assertEqual(53, result.returncode)
        state = exchange.read_state(state_path)
        self.assertEqual('failed', state['requests']['QA-1-auto-1']['status'])
        self.assertFalse(any(e['kind'] == 'result_ready' for e in state['events']))

    def test_shared_registry_cannot_be_overwritten_by_metrics(self):
        output = self.root / 'shared-result.json'
        state_path = self.shared_assignment(output)
        before = state_path.read_bytes()
        result = self.run_task(output, '--task-state', str(state_path), '--metrics', str(state_path))
        self.assertEqual(2, result.returncode)
        self.assertEqual(before, state_path.read_bytes())

    def test_same_request_is_rejected_when_output_path_changes(self):
        first = self.run_task(self.root / "first.json")
        self.assertEqual(0, first.returncode, first.stderr)
        second = self.run_task(self.root / "second.json")
        self.assertNotEqual(0, second.returncode)
        self.assertIn("already started", second.stderr)
        states = list((self.root / ".qa-agent-state").glob("*.json"))
        self.assertEqual(1, len(states))
        self.assertEqual("reported", json.loads(states[0].read_text())["requests"]["QA-1-auto-1"]["status"])

    def test_wall_time_limit_is_enforced_and_recorded(self):
        self.env["FAKE_GIGACODE_SLEEP"] = "2"
        output = self.root / "timeout.json"
        result = self.run_task(output, "--max-wall-time", "0.1s")
        self.assertEqual(124, result.returncode, result.stderr)
        self.assertIn("exceeded", result.stderr)
        metrics = json.loads((self.root / "timeout.json.metrics.json").read_text())
        self.assertTrue(metrics["timed_out"])
        state_path = next((self.root / ".qa-agent-state").glob("*.json"))
        self.assertEqual("timed_out", json.loads(state_path.read_text())["requests"]["QA-1-auto-1"]["status"])

    def test_event_array_preserves_usage_and_accepts_terminal_report(self):
        message = json.loads(json.loads(self.env['FAKE_GIGACODE_OUTPUT'])['response'])
        self.env['FAKE_GIGACODE_OUTPUT'] = json.dumps([
            {'type': 'assistant'},
            {'type': 'result', 'is_error': False, 'structured_result': message,
             'usage': {'input_tokens': 123, 'output_tokens': 45}},
        ])
        output = self.root / 'array.json'
        result = self.run_task(output)
        self.assertEqual(0, result.returncode, result.stderr)
        metrics = json.loads(output.with_suffix('.json.metrics.json').read_text())
        self.assertEqual(123, metrics['reported_usage']['input_tokens'])
        self.assertTrue(metrics['contract_valid'])

    def test_nonzero_exit_cannot_accept_a_valid_report(self):
        self.env['FAKE_GIGACODE_EXIT'] = '53'
        output = self.root / 'failed.json'
        result = self.run_task(output)
        self.assertEqual(53, result.returncode)
        metrics = json.loads(output.with_suffix('.json.metrics.json').read_text())
        self.assertFalse(metrics['contract_valid'])
        state_path = next((self.root / '.qa-agent-state').glob('*.json'))
        self.assertEqual('failed', json.loads(state_path.read_text())['requests']['QA-1-auto-1']['status'])

    def test_runner_records_observation_without_guessing_a_cause(self):
        self.env['FAKE_GIGACODE_EXIT'] = '53'
        output = self.root / 'incident.json'
        result = self.run_task(output)
        self.assertEqual(53, result.returncode)
        journal = self.root / '.qa-agent-state/failures.jsonl'
        entry = json.loads(journal.read_text())['entry']
        self.assertEqual('tool_error', entry['symptom'])
        self.assertEqual('unknown', entry['cause']['status'])
        self.assertEqual('not_attempted', entry['recovery']['outcome'])
        metrics = json.loads(output.with_suffix('.json.metrics.json').read_text())
        self.assertEqual(entry['incident_id'], metrics['failure_incident_id'])
        self.assertIsNone(metrics['failure_journal_error'])
        self.assertNotIn('цель: проверить фильтр', journal.read_text())

    def test_failure_to_write_journal_does_not_hide_execution_failure_or_change_history(self):
        self.env['FAKE_GIGACODE_EXIT'] = '53'
        journal = self.root / 'broken.jsonl'
        journal.write_text('incomplete record')
        result = self.run_task(self.root / 'failed-with-journal.json', '--failure-journal', str(journal))
        self.assertEqual(53, result.returncode)
        self.assertEqual('incomplete record', journal.read_text())
        self.assertIn('Could not record operational incident', result.stderr)
        metrics = json.loads((self.root / 'failed-with-journal.json.metrics.json').read_text())
        self.assertTrue(metrics['failure_journal_error'])

    def test_journal_cannot_overwrite_run_inputs_or_output(self):
        output = self.root / 'protected.json'
        for path in (output, self.task, self.repo / 'failures.jsonl'):
            result = self.run_task(output, '--failure-journal', str(path))
            self.assertEqual(2, result.returncode)
            self.assertIn('Failure journal', result.stderr)
        self.assertFalse(output.exists())

    def test_success_and_normal_information_requests_are_not_failure_incidents(self):
        output = self.root / 'success.json'
        result = self.run_task(output)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse((self.root / '.qa-agent-state/failures.jsonl').exists())
        self.task.write_text('task_id: QA-1\nrequest_id: QA-1-auto-2\n')
        message = dict(task_id='QA-1', request_id='QA-1-auto-2', agent='qa-test-automation',
                       event='needs_environment', summary='Нужен адрес стенда', artifacts=[], blockers=[],
                       payload=dict(request='Укажите разрешённый стенд'), self_review=review_for(limited=True))
        self.env['FAKE_GIGACODE_OUTPUT'] = json.dumps({'response': json.dumps(message)})
        result = self.run_task(self.root / 'needs.json')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse((self.root / '.qa-agent-state/failures.jsonl').exists())

    def test_invalid_output_is_logged_as_a_symptom_without_copying_its_text(self):
        self.env['FAKE_GIGACODE_OUTPUT'] = 'private invalid response content'
        result = self.run_task(self.root / 'invalid.json')
        self.assertEqual(2, result.returncode)
        raw = (self.root / '.qa-agent-state/failures.jsonl').read_text()
        self.assertEqual('invalid_result', json.loads(raw)['entry']['symptom'])
        self.assertNotIn('private invalid response content', raw)

    def test_blocked_is_logged_but_valid_report_keeps_its_exit_code(self):
        message = dict(task_id='QA-1', request_id='QA-1-auto-1', agent='qa-test-automation',
                       event='blocked', summary='Назначение заблокировано', artifacts=[], blockers=['Не хватает обязательного доступа'], payload={},
                       self_review=review_for(limited=True))
        self.env['FAKE_GIGACODE_OUTPUT'] = json.dumps({'response': json.dumps(message)})
        result = self.run_task(self.root / 'blocked.json')
        self.assertEqual(0, result.returncode, result.stderr)
        entry = json.loads((self.root / '.qa-agent-state/failures.jsonl').read_text())['entry']
        self.assertEqual('blocked_execution', entry['symptom'])
        self.assertEqual('unknown', entry['cause']['status'])

    def test_standalone_prompt_omits_named_agent_tool_metadata(self):
        args_file = self.root / 'cli-args.json'
        self.env['FAKE_GIGACODE_ARGS'] = str(args_file)
        result = self.run_task(self.root / 'result.json')
        self.assertEqual(0, result.returncode, result.stderr)
        args = json.loads(args_file.read_text())
        prompt = args[args.index('--append-system-prompt') + 1]
        self.assertNotIn('\ntools:', prompt)
        self.assertNotIn('\ndescription:', prompt)
        user_prompt = args[args.index('-p') + 1]
        self.assertIn(str(RUNNER.parent.parent / 'docs/SELF_REVIEW.md'), user_prompt)
        self.assertIn('self_review', prompt)
        self.assertIn('Ты работаешь в репозитории автотестов.', prompt)

    def test_missing_self_review_rejected_even_without_registry(self):
        message = json.loads(json.loads(self.env['FAKE_GIGACODE_OUTPUT'])['response'])
        del message['self_review']
        self.env['FAKE_GIGACODE_OUTPUT'] = json.dumps({'response': json.dumps(message)})
        result = self.run_task(self.root / 'missing-review.json')
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        metrics = json.loads((self.root / 'missing-review.json.metrics.json').read_text())
        self.assertIn('missing self_review', metrics['validation_errors'])

    def test_missing_self_review_never_publishes_shared_ready_event(self):
        output = self.root / 'missing-shared-review.json'
        path = self.shared_assignment(output)
        message = json.loads(json.loads(self.env['FAKE_GIGACODE_OUTPUT'])['response'])
        del message['self_review']
        self.env['FAKE_GIGACODE_OUTPUT'] = json.dumps({'response': json.dumps(message)})
        result = self.run_task(output, '--task-state', str(path))
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        state = exchange.read_state(path)
        self.assertEqual('failed', state['requests']['QA-1-auto-1']['status'])
        self.assertFalse(any(e['kind'] == 'result_ready' for e in state['events']))

    def test_external_repository_receives_accessible_quality_guide(self):
        args_file = self.root / 'quality-args.json'
        self.env['FAKE_GIGACODE_ARGS'] = str(args_file)
        result = self.run_task(self.root / 'quality.json')
        self.assertEqual(0, result.returncode, result.stderr)
        args = json.loads(args_file.read_text())
        prompt = args[args.index('-p') + 1]
        guide = RUNNER.resolve().parents[1] / 'docs/AUTOTEST_QUALITY.md'
        self.assertTrue(guide.is_file())
        self.assertIn(str(guide), prompt)
        for filename in ('AUTOTEST_NAMING.md', 'TEST_CASE_STYLE.md', 'TEST_DATA.md'):
            naming = RUNNER.resolve().parents[1] / 'docs' / filename
            self.assertTrue(naming.is_file())
            self.assertIn(str(naming), prompt)
        self.assertIn(str(RUNNER.resolve().parents[1]), prompt)
        selector = RUNNER.with_name('read_test_data_guide.py')
        self.assertIn(str(selector), prompt)
        self.assertIn('--only-sections', prompt)
        system_prompt = args[args.index('--append-system-prompt') + 1]
        self.assertIn('# Обязательный минимум подготовки данных', system_prompt)
        self.assertIn('## Рецепт и фикстура', system_prompt)
        self.assertNotIn('## Компактный профиль и передача', system_prompt)
        self.assertNotIn('## Изоляция, повтор и время', system_prompt)
        metrics = json.loads((self.root / 'quality.json.metrics.json').read_text())
        self.assertEqual(['recipe'], metrics['test_data_context_sections'])
        self.assertGreater(metrics['test_data_context_characters'], 0)
        self.assertEqual(len(prompt) + len(system_prompt), metrics['explicit_input_characters'])

    def test_naming_guides_cannot_be_overwritten_by_run_artifacts(self):
        for filename in ('docs/AUTOTEST_NAMING.md', 'docs/TEST_CASE_STYLE.md', 'docs/TEST_DATA.md',
                         'docs/TEST_DATA_CHECKLIST.md', 'scripts/read_test_data_guide.py', 'docs/SELF_REVIEW.md'):
            guide = RUNNER.resolve().parents[1] / filename
            before = guide.read_bytes()
            for flag in ('--output', '--metrics', '--state', '--failure-journal'):
                with self.subTest(filename=filename, flag=flag):
                    output = self.root / 'protected-naming.json'
                    result = (self.run_task(guide) if flag == '--output'
                              else self.run_task(output, flag, str(guide)))
                    self.assertEqual(2, result.returncode, result.stderr)
                    self.assertEqual(before, guide.read_bytes())
                    self.assertFalse(output.exists())

    def test_output_cannot_overwrite_quality_guide(self):
        guide = RUNNER.resolve().parents[1] / 'docs/AUTOTEST_QUALITY.md'
        before = guide.read_bytes()
        result = self.run_task(guide)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('must not overwrite input files', result.stderr)
        self.assertEqual(before, guide.read_bytes())


if __name__ == "__main__":
    unittest.main()
