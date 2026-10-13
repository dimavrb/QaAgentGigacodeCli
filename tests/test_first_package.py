import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import agent_trace
import task_exchange as exchange
from export_task_trace import export_records
from self_review_fixtures import review_for
from run_agent_benchmark import run_benchmark, terminal_response, NEEDED_FLAGS
from check_benchmark_review import load_suite, validate_review, SUITE


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'task.json'
        self.packet = dict(schema_version=1, criteria=[dict(criterion_id='C-1', description='Доступ проверен',
                                                           source='R-1', required=True),
                                                      dict(criterion_id='C-2', description='Отчёт оформлен',
                                                           source='R-2', required=True)])
        exchange.init_task(self.state, 'QA-1', 'Первый пакет', self.packet)
        (self.root / 'proof.txt').write_text('Синтетическое доказательство')

    @property
    def revision(self):
        return exchange.read_state(self.state)['revision']

    def assignment(self, rid='env-1', **extra):
        return dict(task_id='QA-1', request_id=rid, agent='qa-test-env-configurator', stage=rid,
                    goal='Проверить доступ', acceptance_criteria=['Доступ проверен'], criterion_ids=['C-1'],
                    stop_condition='Вернуть отчёт', expected_artifact='JSON', result_path=str(self.root / (rid + '.json')),
                    inputs=[], depends_on=[], **extra)

    def accept(self, rid='env-1', kind='environment_ready', scenarios=None, criterion_ids=None):
        assignment = self.assignment(rid)
        if criterion_ids is not None:
            assignment['criterion_ids'] = criterion_ids
        if scenarios:
            assignment['scenario_ids'] = scenarios
        exchange.assign(self.state, assignment, self.root, self.revision)
        exchange.prepare_handoff(self.state, rid, self.revision)
        report = dict(task_id='QA-1', request_id=rid, agent='qa-test-env-configurator',
                      event=kind, summary='Учебный результат', artifacts=[str(self.root / 'proof.txt')],
                      blockers=['Нет доступа'] if kind == 'blocked' else [], self_review=review_for(limited=kind == 'blocked'),
                      payload=dict(environment_id='fixture', product_version='v1', verified_capabilities=['API'], limitations=[]))
        if kind == 'environment_partial':
            report['payload'].update(ready_scenarios=[scenarios[0]], blocked_scenarios=scenarios[1:])
        (self.root / (rid + '.json')).write_text(json.dumps(report))
        exchange.submit_result(self.state, rid, self.revision)
        exchange.review_result(self.state, rid, 'Доказательства прочитаны', True, self.revision)

    def assess(self, cid='C-1', status='verified', rids=None, proof=None):
        return exchange.assess_criteria(self.state, {'assessments': [dict(criterion_id=cid, status=status,
                decision='Учебные доказательства проверены', request_ids=rids or ['env-1'],
                evidence=proof or [str(self.root / 'proof.txt')])]}, self.revision)

    def test_blocked_accepted_report_cannot_start_execution_but_diagnostic_can_read_it(self):
        self.accept(kind='blocked')
        dependent = self.assignment('next')
        dependent['depends_on'] = ['env-1']
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, 'не готов'):
            exchange.assign(self.state, dependent, self.root, self.revision)
        self.assertEqual(before, self.state.read_bytes())
        dependent['dependency_modes'] = {'env-1': 'context'}
        exchange.assign(self.state, dependent, self.root, self.revision)
        handoff = exchange.prepare_handoff(self.state, 'next', self.revision)['result']
        self.assertEqual('blocked', handoff['sources'][0]['event'])
        self.assertFalse(handoff['sources'][0]['readiness']['complete'])

    def test_partial_readiness_allows_only_explicit_ready_subset(self):
        self.accept(kind='environment_partial', scenarios=['TC-1', 'TC-2'])
        for scenarios in ([], ['TC-2'], ['TC-1', 'TC-2']):
            dependent = self.assignment('next')
            dependent.update(depends_on=['env-1'], scenario_ids=scenarios)
            with self.assertRaisesRegex(ValueError, 'готов'):
                exchange.assign(self.state, dependent, self.root, self.revision)
        dependent['scenario_ids'] = ['TC-1']
        exchange.assign(self.state, dependent, self.root, self.revision)
        exchange.prepare_handoff(self.state, 'next', self.revision)

    def test_context_still_requires_acceptance_and_unchanged_proof(self):
        self.accept(kind='blocked')
        dependent = self.assignment('next')
        dependent.update(depends_on=['env-1'], dependency_modes={'env-1': 'context'})
        exchange.assign(self.state, dependent, self.root, self.revision)
        (self.root / 'proof.txt').write_text('Подменено')
        with self.assertRaisesRegex(ValueError, 'изменились'):
            exchange.prepare_handoff(self.state, 'next', self.revision)

    def test_unknown_dependency_modes_and_criteria_are_rejected(self):
        for change in ({'dependency_modes': {'missing': 'context'}}, {'criterion_ids': ['C-99']}):
            assignment = self.assignment()
            assignment.update(change)
            with self.assertRaises(ValueError):
                exchange.assign(self.state, assignment, self.root, self.revision)

    def test_missing_required_scope_cannot_be_hidden_by_successful_stage(self):
        self.accept()
        self.assess()
        with self.assertRaisesRegex(ValueError, 'C-2'):
            exchange.finish_task(self.state, 'passed', 'Готово', self.revision)
        self.accept('env-2', criterion_ids=['C-2'])
        self.assess('C-2', rids=['env-2'])
        exchange.finish_task(self.state, 'passed', 'Все критерии проверены', self.revision)
        self.assertEqual('completed', exchange.read_state(self.state)['execution_status'])

    def test_criteria_cannot_change_after_work_started(self):
        self.accept()
        with self.assertRaisesRegex(ValueError, 'до первого назначения'):
            exchange.set_criteria(self.state, self.packet, self.revision)

    def test_assessment_requires_accepted_matching_artifact_and_scope(self):
        self.accept()
        (self.root / 'unrelated.txt').write_text('Посторонний файл')
        for cid, rids, proof in [('C-2', ['env-1'], None), ('C-1', ['missing'], None),
                                  ('C-1', ['env-1'], [str(self.root / 'unrelated.txt')])]:
            before = self.state.read_bytes()
            with self.assertRaises(ValueError):
                self.assess(cid, rids=rids, proof=proof)
            self.assertEqual(before, self.state.read_bytes())

    def test_unknown_and_failed_required_criteria_do_not_finish_task(self):
        self.accept(criterion_ids=['C-1', 'C-2'])
        self.assess('C-2')
        for status in ('unknown', 'failed'):
            self.assess(status=status)
            with self.assertRaisesRegex(ValueError, 'C-1'):
                exchange.finish_task(self.state, 'passed', 'Готово', self.revision)

    def test_non_applicable_requires_explicit_evidence_and_decision(self):
        self.accept(criterion_ids=['C-1', 'C-2'])
        self.assess('C-1')
        self.assess('C-2', 'not_applicable')
        exchange.finish_task(self.state, 'not_applicable', 'Причина исключения проверена', self.revision)

    def test_independent_reviewer_cannot_be_replaced_by_author(self):
        self.packet['criteria'][0]['required_review_agent'] = 'qa-autotest-reviewer'
        exchange.set_criteria(self.state, self.packet, self.revision)
        self.accept()
        with self.assertRaisesRegex(ValueError, 'независимого'):
            self.assess()

    def automation_and_review(self, reviewed_revision='rev-2', review_kind='review_passed'):
        for rid, agent, kind, depends, payload in [
            ('auto', 'qa-test-automation', 'ready_for_review', [],
             dict(scenario_id='TC-1', requirement_source='R-1', base_revision='rev-1', current_revision='rev-2',
                  changed_files=[str(self.root / 'proof.txt')], diff_artifact=str(self.root / 'proof.txt'),
                  run_result='Учебное доказательство', limitations=[])),
            ('review', 'qa-autotest-reviewer', review_kind, ['auto'],
             dict(reviewed_revision=reviewed_revision, findings=[] if review_kind == 'review_passed' else ['Слабый assert'],
                  checks=['Сопоставлено с источником'], limitations=[]))]:
            assignment = self.assignment(rid)
            assignment.update(agent=agent, depends_on=depends, criterion_ids=['C-1', 'C-2'])
            exchange.assign(self.state, assignment, self.root, self.revision)
            handoff = exchange.prepare_handoff(self.state, rid, self.revision)['result']
            exchange.start_request(self.state, rid, 'fixture-owner-' + rid, handoff['delivery_id'], self.revision)
            report = dict(task_id='QA-1', request_id=rid, agent=agent, event=kind, summary='Учебный результат',
                          artifacts=[str(self.root / 'proof.txt')], blockers=[], self_review=review_for(), payload=payload)
            (self.root / (rid + '.json')).write_text(json.dumps(report))
            exchange.submit_result(self.state, rid, self.revision)
            exchange.review_result(self.state, rid, 'Доказательства прочитаны', True, self.revision)

    def test_independent_review_binds_exact_author_revision_and_finishes(self):
        self.packet['criteria'][0]['required_review_agent'] = 'qa-autotest-reviewer'
        exchange.set_criteria(self.state, self.packet, self.revision)
        self.automation_and_review()
        self.assess(rids=['auto', 'review'])
        self.assess('C-2', rids=['auto', 'review'])
        exchange.finish_task(self.state, 'passed', 'Ревизия и критерии проверены', self.revision)

    def test_legacy_dependency_review_cannot_share_author_owner(self):
        self.automation_and_review()
        state=exchange.read_state(self.state)
        state['requests']['review']['owner_task_id']=state['requests']['auto']['owner_task_id']
        exchange.atomic_write(self.state,state)
        self.assess(rids=['auto','review'])
        self.assess('C-2',rids=['auto','review'])
        with self.assertRaisesRegex(ValueError,'независимого'):
            exchange.finish_task(self.state,'passed','Same owner must not pass',self.revision)

    def test_review_of_wrong_revision_does_not_accept_criterion(self):
        self.packet['criteria'][0]['required_review_agent'] = 'qa-autotest-reviewer'
        exchange.set_criteria(self.state, self.packet, self.revision)
        self.automation_and_review(reviewed_revision='other-revision')
        with self.assertRaisesRegex(ValueError, 'Ревью не подтверждает'):
            self.assess(rids=['auto', 'review'])

    def test_accepted_changes_requested_is_not_execution_readiness(self):
        self.automation_and_review(review_kind='changes_requested')
        dependent = self.assignment('next')
        dependent['depends_on'] = ['review']
        with self.assertRaisesRegex(ValueError, 'не готов'):
            exchange.assign(self.state, dependent, self.root, self.revision)

    def test_wrong_revision_review_does_not_finish_without_optional_role_gate(self):
        self.automation_and_review(reviewed_revision='other-revision')
        self.assess('C-1', rids=['auto', 'review'])
        self.assess('C-2', rids=['auto', 'review'])
        with self.assertRaisesRegex(ValueError, 'review_passed'):
            exchange.finish_task(self.state, 'passed', 'Готово', self.revision)

    def test_mutated_superseded_review_cannot_cover_missing_valid_review(self):
        self.automation_and_review(reviewed_revision='wrong')
        assignment = self.assignment('review-2')
        assignment.update(agent='qa-autotest-reviewer', stage='review', depends_on=['auto'])
        exchange.assign(self.state, assignment, self.root, self.revision)
        exchange.prepare_handoff(self.state, 'review-2', self.revision)
        report = json.loads((self.root / 'review.json').read_text())
        report['request_id'] = 'review-2'
        (self.root / 'review-2.json').write_text(json.dumps(report))
        exchange.submit_result(self.state, 'review-2', self.revision)
        exchange.review_result(self.state, 'review-2', 'Учебный отчёт принят', True, self.revision)
        report = json.loads((self.root / 'review.json').read_text())
        report['payload']['reviewed_revision'] = 'rev-2'
        (self.root / 'review.json').write_text(json.dumps(report))
        self.assess('C-1', rids=['auto'])
        self.assess('C-2', rids=['auto'])
        with self.assertRaisesRegex(ValueError, 'review_passed'):
            exchange.finish_task(self.state, 'passed', 'Готово', self.revision)

    def test_criteria_are_rechecked_at_finish(self):
        self.accept(criterion_ids=['C-1', 'C-2'])
        self.assess('C-1')
        self.assess('C-2')
        (self.root / 'proof.txt').write_text('Подменено после оценки')
        with self.assertRaises(ValueError):
            exchange.finish_task(self.state, 'passed', 'Готово', self.revision)

    def test_handoff_contains_agreed_criteria_and_correlated_attempt(self):
        assignment = self.assignment()
        exchange.assign(self.state, assignment, self.root, self.revision)
        handoff = exchange.prepare_handoff(self.state, 'env-1', self.revision)['result']
        self.assertEqual('C-1', handoff['task_criteria']['C-1']['criterion_id'])
        self.assertEqual('env-1', handoff['trace_context']['request_id'])
        self.assertTrue(handoff['trace_context']['attempt_id'])
        events = export_records(exchange.read_state(self.state))
        self.assertEqual(handoff['trace_context']['trace_id'], events[-1]['trace_id'])
        self.assertEqual(handoff['trace_context']['attempt_id'], events[-1]['attempt_id'])
        self.assertNotIn(handoff['delivery_id'], json.dumps(events))

    def test_old_registry_stays_explicit_legacy_and_can_be_upgraded_before_assignment(self):
        legacy = self.root / 'legacy.json'
        exchange.init_task(legacy, 'LEGACY', 'Старая задача')
        value = exchange.read_state(legacy)
        for key in ('acceptance_mode', 'criteria', 'trace_id'):
            value.pop(key)
        legacy.write_text(json.dumps(value))
        self.assertEqual('legacy', exchange.state_summary(exchange.read_state(legacy))['acceptance_mode'])
        exchange.set_criteria(legacy, self.packet, 1)
        self.assertEqual('criteria', exchange.read_state(legacy)['acceptance_mode'])

    def test_cli_new_task_requires_explicit_scope(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/manage_task.py'), '--state',
                                 str(self.root / 'cli.json'), 'init', '--task-id', 'CLI', '--title', 'CLI'],
                                capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertFalse((self.root / 'cli.json').exists())


class BenchmarkExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gigacode = self.root / 'cli'
        self.gigacode.write_text('''#!/usr/bin/env python3
import json, sys, os, time
if '--help' in sys.argv:
    print(''' + repr(' '.join(NEEDED_FLAGS)) + ''')
elif '--version' in sys.argv:
    print('fixture-cli-1')
else:
    assert '--bare' in sys.argv and '--exclude-tools' in sys.argv
    assert not os.listdir(os.getcwd())
    prompt = sys.argv[sys.argv.index('-p') + 1]
    assert 'check_id' not in prompt and 'expectation' not in prompt
    if 'SLEEP' in os.environ: time.sleep(float(os.environ['SLEEP']))
    print(json.dumps([{'type': 'result', 'response': 'Учебный ответ без оценки качества',
                      'usage': {'input_tokens': 12, 'output_tokens': 7}}]))
''')
        self.gigacode.chmod(0o755)

    def test_executes_every_case_and_repeat_without_self_grading(self):
        output = self.root / 'run'
        result = run_benchmark(output, cli=str(self.gigacode), repeats=2, model='fixture-model')
        self.assertEqual('captured', result['status'])
        self.assertEqual(10, len(result['trials']))
        suite, fingerprint = load_suite(SUITE)
        _, saved_fingerprint = load_suite(output / 'suite.json')
        self.assertEqual(fingerprint, saved_fingerprint)
        for repeat in (1, 2):
            directory = output / f'trial-{repeat}'
            review = json.loads((directory / 'review.json').read_text())
            self.assertEqual([], validate_review(review, suite, fingerprint, directory))
            self.assertTrue(all(c['status'] == 'not_checked' for row in review['cases'] for c in row['checks']))
            self.assertTrue(all(row['input_tokens'] == 12 for row in review['cases']))
            check = subprocess.run([sys.executable, str(ROOT / 'scripts/check_benchmark_review.py'),
                                    '--suite', str(output / 'suite.json'), '--input', str(directory / 'review.json')], capture_output=True, text=True)
            self.assertEqual(1, check.returncode)
        records = [json.loads(line) for line in (output / 'trace.jsonl').read_text().splitlines()]
        self.assertEqual(10, sum(row['kind'] == 'runner_finished' for row in records))
        self.assertEqual(1, len({row['trace_id'] for row in records}))

    def test_missing_cli_is_recorded_without_fake_outputs(self):
        result = run_benchmark(self.root / 'run', cli=str(self.root / 'missing'))
        self.assertEqual('unavailable', result['status'])
        self.assertEqual([], result['trials'])
        self.assertFalse(list((self.root / 'run').glob('trial-*')))

    def test_custom_quality_suite_isolated_prompts_and_off_on_snapshots(self):
        suite_path=ROOT/'examples/benchmark/quality-suite.json'
        for enabled in (False,True):
            output=self.root/('on' if enabled else 'off')
            result=run_benchmark(output,cli=str(self.gigacode),suite_path=suite_path,check_quality=enabled)
            self.assertEqual('captured',result['status'])
            self.assertEqual(9,len(result['trials']))
            suite,fingerprint=load_suite(output/'suite.json')
            self.assertEqual(36,sum(len(c['checks']) for c in suite['cases']))
            review=json.loads((output/'trial-1/review.json').read_text())
            self.assertEqual([],validate_review(review,suite,fingerprint,output/'trial-1'))
            self.assertTrue(all(c['status']=='not_checked' for row in review['cases'] for c in row['checks']))
            snapshots=json.loads((output/'trial-1/observability/instructions.json').read_text())
            self.assertEqual(enabled,'.gigacode/skills/qa-check-quality/SKILL.md' in snapshots)
            for case in suite['cases']:
                prompt=(output/'trial-1'/case['case_id']/'prompt.md').read_text()
                self.assertNotIn('expectation',prompt)
                for check in case['checks']:self.assertNotIn(check['expectation'],prompt)

    def test_unsupported_cli_does_not_fall_back_to_less_restricted_mode(self):
        self.gigacode.write_text('#!/usr/bin/env python3\nprint("old-cli")\n')
        result = run_benchmark(self.root / 'run', cli=str(self.gigacode))
        self.assertEqual('unavailable', result['status'])
        self.assertIn('--bare', result['error'])

    def test_failed_case_is_preserved_and_other_cases_continue(self):
        self.gigacode.write_text(self.gigacode.read_text().replace("print(json.dumps([{'type': 'result'", "sys.exit(9)\n    print(json.dumps([{'type': 'result'"))
        result = run_benchmark(self.root / 'run', cli=str(self.gigacode))
        self.assertEqual('failed', result['status'])
        self.assertEqual(5, len(result['trials']))
        self.assertTrue(all(row['exit_code'] == 9 for row in result['trials']))

    def test_timeout_captures_failure_and_runs_remaining_cases(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'SLEEP': '.1'}):
            result = run_benchmark(self.root / 'run', cli=str(self.gigacode), max_wall_time='0.01s')
        self.assertEqual('failed', result['status'])
        self.assertEqual(5, len(result['trials']))
        self.assertTrue(all(row['status'] == 'timed_out' for row in result['trials']))

    def test_output_directory_never_overwrites_old_baseline(self):
        output = self.root / 'run'
        output.mkdir()
        (output / 'kept').write_text('Не менять')
        with self.assertRaises(FileExistsError):
            run_benchmark(output, cli=str(self.gigacode))
        self.assertEqual('Не менять', (output / 'kept').read_text())

    def test_error_empty_output_and_tool_use_are_not_successful_capture(self):
        for value in ({'is_error': True, 'response': 'error'}, {'response': ''},
                      {'response': 'text', 'stats': {'tools': {'totalCalls': 1}}}, []):
            with self.assertRaises(ValueError):
                terminal_response(json.dumps(value))

    def test_unknown_usage_is_not_estimated_from_text_length(self):
        self.gigacode.write_text(self.gigacode.read_text().replace("'usage': {'input_tokens': 12, 'output_tokens': 7}", "'stats': {}"))
        result = run_benchmark(self.root / 'run', cli=str(self.gigacode), self_review=False)
        self.assertEqual('captured', result['status'])
        review = json.loads((self.root / 'run/trial-1/review.json').read_text())
        self.assertTrue(all(row['input_tokens'] is None for row in review['cases']))


class TraceTests(unittest.TestCase):
    def test_parseable_record_without_newline_is_still_an_incomplete_tail(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'trace.jsonl'
            path.write_text('{"schema_version":1}')
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                agent_trace.append(path, agent_trace.context('QA-1'), 'runner_started')
            self.assertEqual('{"schema_version":1}', path.read_text())

    def test_success_failure_and_partial_tail(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'trace.jsonl'
            ctx = agent_trace.context('QA-1', 'REQ-1', 'ATT-1', 'qa-analyst')
            agent_trace.append(path, ctx, 'runner_started')
            agent_trace.append(path, ctx, 'runner_finished', exit_code=0)
            agent_trace.append(path, ctx, 'runner_finished', exit_code=3)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([0, 3], [r['exit_code'] for r in records if 'exit_code' in r])
            with path.open('a') as stream:
                stream.write('{broken')
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                agent_trace.append(path, ctx, 'runner_finished')
            self.assertEqual(before, path.read_bytes())
