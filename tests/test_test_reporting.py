"""Reporting facts, missing coverage, repeat selection and source integrity; no live CLI."""
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from test_reporting import build_report, check_bundle, render_markdown
from qa_contract import validate_message


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'inputs'
        shutil.copytree(ROOT / 'examples/test-reporting', self.base)
        self.scope = self.load('scope.json')
        self.runs = self.load('runs.json')

    def load(self, name):
        return json.loads((self.base / name).read_text())

    def save(self, name, value):
        (self.base / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    def build(self):
        self.save('scope.json', self.scope)
        self.save('runs.json', self.runs)
        return build_report(self.base / 'scope.json', self.base / 'runs.json', 'report-v1', '2020-02-01T00:00:00+00:00')

    def add_run(self, identifier='RUN-2', statuses=None, supersedes='RUN-1'):
        statuses = statuses or {'TC-PAY': 'failed'}
        message = self.load('feature.json')
        message['request_id'] = identifier
        rows = [copy.deepcopy(r) for r in message['payload']['scenario_results'] if r['scenario_id'] in statuses]
        for row in rows:
            row['status'] = statuses[row['scenario_id']]
            row['steps'] = ['Учебное действие'] if row['status'] in ('passed', 'failed') else []
            row['actual_result'] = 'Учебный повтор: ' + row['status']
        message['event'] = 'blocked' if all(r['status'] == 'blocked' for r in rows) else 'feature_test_complete'
        message['payload']['scenario_results'] = rows
        message['payload']['uncovered_scenarios'] = [r['scenario_id'] for r in rows if r['status'] in ('blocked', 'not_run')]
        name = identifier + '.json'
        self.save(name, message)
        run = copy.deepcopy(self.runs['runs'][0])
        run.update(run_id=identifier, scenario_ids=list(statuses), result_artifact=name,
                   observed_at='2020-01-01T11:00:00+00:00', supersedes=supersedes, review={'status': 'pending'})
        self.runs['runs'].append(run)
        return run

    def bundle(self):
        report = self.build()
        directory = Path(self.temp.name) / 'report'
        directory.mkdir()
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False))
        (directory / 'report.md').write_text(render_markdown(report))
        return directory, report

    def test_example_has_partial_business_coverage_and_explicit_off_gap(self):
        report = self.build()
        self.assertEqual({'planned': 3, 'required': 3, 'executed': 1, 'target_executed': 1,
                          'passed': 1, 'failed': 0, 'accepted_passed': 1}, report['counts'])
        self.assertEqual(['incomplete', 'incomplete'], [case['status'] for case in report['business_cases']])
        self.assertEqual('not_run', report['checks'][1]['execution_status'])
        self.assertEqual('blocked', report['checks'][2]['execution_status'])
        self.assertEqual('not_assessed', report['release_decision'])
        rendered = render_markdown(report)
        self.assertIn('Учебные синтетические данные', rendered)
        self.assertIn('new\\_checkout', rendered)
        self.assertIn('CHECKOUT-OFF', rendered)
        self.assertIn('Синтетический сервис возврата недоступен', rendered)

    def test_detailed_report_explains_function_and_limits_without_opening_scenarios(self):
        self.runs = self.load('detailed-runs.json')
        report = self.build()
        rendered = render_markdown(report)
        primary = self.load('detailed-feature.json')['payload']['scenario_results'][0]
        business_section = rendered.split('## Покрытие сценариев и конфигураций')[0]
        self.assertIn('Завершить оплату своего заказа картой', business_section)
        self.assertIn('Бизнес-правило из требования', business_section)
        for value in primary['steps'] + [primary['expected_result'], primary['actual_result']]:
            self.assertIn(value, business_section)
        self.assertIn('new\\_checkout=OFF', business_section)
        self.assertIn('действие и бизнес-эффект не проверены', business_section)
        self.assertIn('ожидаемый бизнес-эффект не подтверждён', business_section)
        self.assertEqual(1, report['counts']['accepted_passed'])
        self.assertEqual(['incomplete', 'incomplete'], [c['status'] for c in report['business_cases']])

    def test_legacy_scope_missing_details_is_readable_without_invented_rules(self):
        original_counts = self.build()['counts']
        for check in self.scope['checks']:
            check.pop('intent')
        for case in self.scope['business_cases']:
            case.pop('business_goal')
        report = self.build()
        rendered = render_markdown(report)
        self.assertEqual(original_counts, report['counts'])
        self.assertIn('Не описана в исходных материалах', rendered)
        self.assertIn('нельзя восстановить их из ID или статуса', rendered)
        self.assertIn('Учебная запись действия оплаты', rendered)
        self.assertNotIn('Списание денег', rendered)

    def test_blocked_partial_actions_and_not_run_plan_are_not_full_execution(self):
        feature = self.load('feature.json')
        cancel = feature['payload']['scenario_results'][1]
        cancel['steps'] = ['Открыт оплаченный заказ; до подтверждения отмены обнаружен блокер.']
        self.runs['runs'][0]['review'] = {'status': 'pending'}
        self.save('feature.json', feature)
        rendered = render_markdown(self.build())
        self.assertIn(cancel['steps'][0], rendered)
        self.assertIn('Сценарий не завершён', rendered)
        self.assertIn('ожидаемый бизнес-эффект не подтверждён', rendered)
        cancel['status'] = 'not_run'
        cancel['steps'] = ['План, который не выполнялся: подтвердить отмену.']
        self.save('feature.json', feature)
        report = self.build()
        self.assertNotIn(cancel['steps'][0], render_markdown(report))
        self.assertEqual(cancel['steps'], report['checks'][2]['result']['steps'])
        self.assertIn('запись шагов не считается выполнением', render_markdown(report))
        self.assertEqual(1, report['counts']['executed'])

    def test_business_description_does_not_hide_wrong_conditions_or_conflicting_reruns(self):
        self.runs['runs'][0]['review'] = {'status': 'pending'}
        self.runs['runs'][0]['context']['role'] = 'administrator'
        rendered = render_markdown(self.build())
        self.assertIn('Запись относится к другим условиям', rendered)
        self.assertIn('Учебная запись: заказ оплачен', rendered)
        self.add_run(supersedes=None)
        report = self.build()
        business_section = render_markdown(report).split('## Покрытие сценариев и конфигураций')[0]
        self.assertIn('актуальный результат не выбран', business_section)
        self.assertNotIn('Учебная запись: заказ оплачен', business_section)
        self.assertNotIn('Учебный повтор: failed', business_section)

    def test_scope_descriptions_require_nonempty_sourced_fields(self):
        original = copy.deepcopy(self.scope)
        for field in ('action', 'business_rule', 'expected_effect'):
            self.scope = copy.deepcopy(original)
            self.scope['checks'][0]['intent'][field] = ''
            with self.assertRaisesRegex(ValueError, 'check.intent'):
                self.build()
        self.scope = copy.deepcopy(original)
        self.scope['business_cases'][0]['business_goal'] = None
        with self.assertRaisesRegex(ValueError, 'business_goal'):
            self.build()

    def test_business_description_remains_bound_to_scope_snapshot(self):
        directory, report = self.bundle()
        self.assertEqual(report, check_bundle(directory))
        self.scope['checks'][0]['intent']['business_rule'] = 'Несогласованное новое бизнес-правило'
        self.save('scope.json', self.scope)
        with self.assertRaisesRegex(ValueError, 'differs from sources'):
            check_bundle(directory)

    def test_flag_unknown_fallback_missing_and_remote_source_do_not_cover_target(self):
        original = copy.deepcopy(self.runs)
        for flag in (None, {'value': None, 'status': 'unknown'},
                     {'value': True, 'status': 'fallback', 'source': 'snapshot.json'},
                     {'value': True, 'status': 'confirmed', 'source': 'https://example.invalid/flag'}):
            with self.subTest(flag=flag):
                self.runs = copy.deepcopy(original)
                self.runs['runs'][0]['review'] = {'status': 'pending'}
                self.runs['runs'][0]['feature_flags'] = {} if flag is None else {'new_checkout': flag}
                report = self.build()
                self.assertEqual('configuration_unknown', report['checks'][0]['coverage_status'])
                self.assertEqual(1, report['counts']['executed'])
                self.assertEqual(0, report['counts']['target_executed'])
                self.assertEqual(0, report['counts']['accepted_passed'])

    def test_wrong_role_build_environment_requirement_or_flag_is_not_a_target_pass(self):
        scope, runs = copy.deepcopy(self.scope), copy.deepcopy(self.runs)
        for field in ('role', 'product_version', 'environment_id', 'requirements_revision', 'feature_flags'):
            with self.subTest(field=field):
                self.scope, self.runs = copy.deepcopy(scope), copy.deepcopy(runs)
                target = self.scope['configurations'][0]
                if field == 'role':
                    target['context']['role'] = 'administrator'
                elif field == 'feature_flags':
                    target[field]['new_checkout'] = False
                else:
                    target[field] = 'different'
                report = self.build()
                self.assertEqual('configuration_mismatch', report['checks'][0]['coverage_status'])
                self.assertEqual(0, report['counts']['passed'])
                self.assertTrue(report['checks'][0]['reasons'])

    def test_boolean_false_is_not_numeric_zero(self):
        self.scope['configurations'][0]['feature_flags']['new_checkout'] = False
        self.runs['runs'][0]['feature_flags']['new_checkout']['value'] = 0
        self.runs['runs'][0]['review'] = {'status': 'pending'}
        self.assertEqual('configuration_mismatch', self.build()['checks'][0]['coverage_status'])

    def test_missing_context_or_snapshot_remains_unknown(self):
        self.runs['runs'][0]['review'] = {'status': 'pending'}
        for key in ('context', 'configuration_evidence'):
            original = copy.deepcopy(self.runs)
            self.runs['runs'][0].pop(key)
            self.assertEqual('configuration_unknown', self.build()['checks'][0]['coverage_status'])
            self.runs = original

    def test_pending_review_does_not_hide_successful_execution(self):
        self.scope['checks'].pop(1)
        self.scope['business_cases'][0]['check_ids'] = ['PAY-ON']
        self.runs['runs'][0]['review'] = {'status': 'pending'}
        report = self.build()
        self.assertEqual(1, report['counts']['passed'])
        self.assertEqual(0, report['counts']['accepted_passed'])
        self.assertEqual('needs_review', report['business_cases'][0]['status'])

    def test_recorded_review_must_match_result_hash_and_have_all_checks(self):
        review = self.load('review.json')
        for mutation in ('hash', 'decision', 'task', 'request', 'self-review', 'self-report-review', 'check', 'note'):
            modified = copy.deepcopy(review)
            if mutation == 'hash':
                modified['result_sha256'] = '0' * 64
            elif mutation == 'decision':
                modified['decision'] = 'changes_requested'
            elif mutation == 'task':
                modified['task_id'] = 'another-task'
            elif mutation == 'request':
                modified['request_id'] = 'another-request'
            elif mutation == 'self-review':
                modified['reviewer'] = 'qa-feature-tester'
            elif mutation == 'self-report-review':
                modified['reviewer'] = 'qa-test-report-writer'
            elif mutation == 'check':
                modified['checks']['configuration_context']['status'] = 'unknown'
            else:
                modified['checks']['evidence_support']['note'] = ''
            self.save('review.json', modified)
            with self.assertRaises(ValueError, msg=mutation):
                self.build()
        self.save('review.json', review)

    def test_new_snapshot_cannot_reuse_review_after_evidence_or_execution_metadata_changes(self):
        (self.base / 'snapshot.json').write_text('Changed flag observation')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            self.build()
        shutil.copyfile(ROOT / 'examples/test-reporting/snapshot.json', self.base / 'snapshot.json')
        self.runs['runs'][0]['context']['role'] = 'administrator'
        with self.assertRaisesRegex(ValueError, 'execution metadata'):
            self.build()

    def test_missing_review_source_fingerprints_cannot_be_accepted(self):
        review = self.load('review.json')
        review['artifact_sha256'] = {}
        self.save('review.json', review)
        with self.assertRaisesRegex(ValueError, 'fingerprints'):
            self.build()

    def test_partial_repeat_replaces_only_its_checks_and_preserves_history(self):
        self.add_run()
        report = self.build()
        self.assertEqual(2, len(report['checks'][0]['history']))
        self.assertEqual('failed', report['checks'][0]['execution_status'])
        self.assertEqual('RUN-2', report['checks'][0]['selected_run_id'])
        self.assertEqual('RUN-1', report['checks'][2]['selected_run_id'])
        self.assertEqual(0, report['counts']['passed'])
        self.assertEqual(1, report['counts']['failed'])
        self.assertEqual('failed', report['business_cases'][0]['status'])

    def test_multiple_runs_require_explicit_supersession_and_branch_conflicts_stay_visible(self):
        run = self.add_run(statuses={'TC-PAY': 'passed'}, supersedes=None)
        self.assertEqual('conflict', self.build()['checks'][0]['coverage_status'])
        run['supersedes'] = 'RUN-1'
        self.add_run('RUN-3', supersedes='RUN-1')
        report = self.build()
        self.assertEqual('conflict', report['checks'][0]['coverage_status'])
        self.assertIsNone(report['checks'][0]['selected_run_id'])
        self.assertEqual(0, report['counts']['target_executed'])

    def test_supersession_cannot_cross_configuration_reverse_time_or_cycle(self):
        run = self.add_run()
        for field, value in (('configuration_id', 'CHECKOUT-OFF'), ('observed_at', '2019-01-01T00:00:00+00:00'), ('supersedes', 'missing')):
            original = run[field]
            run[field] = value
            with self.assertRaises(ValueError):
                self.build()
            run[field] = original
        self.runs['runs'][0]['supersedes'] = 'RUN-2'
        with self.assertRaises(ValueError):
            self.build()

    def test_out_of_scope_execution_is_preserved_without_inflating_coverage(self):
        message = self.load('feature.json')
        extra = copy.deepcopy(message['payload']['scenario_results'][0])
        extra['scenario_id'] = 'TC-EXTRA'
        message['payload']['scenario_results'].append(extra)
        self.save('feature.json', message)
        self.runs['runs'][0]['scenario_ids'].append('TC-EXTRA')
        self.runs['runs'][0]['review'] = {'status': 'pending'}
        report = self.build()
        self.assertEqual(1, len(report['out_of_scope']))
        self.assertEqual(1, report['counts']['executed'])
        self.assertEqual(3, report['counts']['planned'])

    def test_empty_run_manifest_is_a_valid_unexecuted_report(self):
        self.runs['runs'] = []
        report = self.build()
        self.assertEqual(0, report['counts']['executed'])
        self.assertTrue(all(row['execution_status'] == 'not_run' for row in report['checks']))
        self.assertTrue(all(case['status'] == 'incomplete' for case in report['business_cases']))

    def test_bad_scope_duplicate_assignment_missing_evidence_and_wrong_task_are_rejected(self):
        scope, runs = copy.deepcopy(self.scope), copy.deepcopy(self.runs)
        for mutation in ('duplicate-check', 'unmapped-check', 'duplicate-run', 'wrong-task', 'missing-evidence', 'wrong-assignment'):
            self.scope, self.runs = copy.deepcopy(scope), copy.deepcopy(runs)
            if mutation == 'duplicate-check':
                self.scope['checks'].append(copy.deepcopy(self.scope['checks'][0]))
            elif mutation == 'unmapped-check':
                self.scope['business_cases'].pop()
            elif mutation == 'duplicate-run':
                self.runs['runs'].append(copy.deepcopy(self.runs['runs'][0]))
            elif mutation == 'wrong-task':
                self.runs['task_id'] = 'wrong-task'
            elif mutation == 'wrong-assignment':
                self.runs['runs'][0]['scenario_ids'] = ['TC-PAY']
            else:
                self.runs['runs'][0]['feature_flags']['new_checkout']['source'] = 'missing.json'
            with self.assertRaises((ValueError, OSError), msg=mutation):
                self.build()

    def test_bundle_verification_detects_counter_markdown_and_evidence_changes(self):
        directory, report = self.bundle()
        self.assertEqual(report, check_bundle(directory))
        modified = copy.deepcopy(report)
        modified['counts']['passed'] = 100
        (directory / 'report.json').write_text(json.dumps(modified))
        with self.assertRaisesRegex(ValueError, 'differs'):
            check_bundle(directory)
        (directory / 'report.json').write_text(json.dumps(report))
        (directory / 'report.md').write_text('Everything passed')
        with self.assertRaisesRegex(ValueError, 'Markdown'):
            check_bundle(directory)
        (directory / 'report.md').write_text(render_markdown(report))
        (self.base / 'evidence.txt').write_text('Changed evidence')
        with self.assertRaisesRegex(ValueError, 'artifact changed'):
            check_bundle(directory)

    def test_cli_build_check_no_overwrite_and_report_handoff(self):
        self.build()
        directory = Path(self.temp.name) / 'cli-report'
        command = [sys.executable, str(ROOT / 'scripts/build_test_report.py'), '--scope', str(self.base / 'scope.json'),
                   '--runs', str(self.base / 'runs.json'), '--revision', 'cli-v1', '--output-dir', str(directory)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(2, subprocess.run(command, capture_output=True).returncode)
        checked = subprocess.run([sys.executable, str(ROOT / 'scripts/build_test_report.py'), '--check', str(directory)], capture_output=True)
        self.assertEqual(0, checked.returncode)
        report = json.loads((directory / 'report.json').read_text())
        message = dict(task_id=report['task_id'], request_id='report-1', agent='qa-test-report-writer', event='test_report_ready',
                       summary='Учебный частичный отчёт', artifacts=['report.json', 'report.md'], blockers=[],
                       payload=dict(report_artifact='report.json', summary_artifact='report.md', report_revision='cli-v1', limitations=report['limitations']))
        self.assertEqual([], validate_message(message, report['task_id'], 'report-1', message['agent']))
        message_path = directory / 'result.json'
        message_path.write_text(json.dumps(message))
        validation = [sys.executable, str(ROOT / 'scripts/validate_qa_result.py'), '--input', str(message_path), '--task-id', report['task_id'], '--request-id', 'report-1', '--agent', message['agent']]
        self.assertEqual(0, subprocess.run(validation, capture_output=True).returncode)
        message['payload']['limitations'] = []
        message_path.write_text(json.dumps(message))
        self.assertEqual(2, subprocess.run(validation, capture_output=True).returncode)

    def test_plans_are_not_execution_and_malformed_metadata_is_rejected(self):
        run = self.runs['runs'][0]
        for field, value in (('feature_flags', []), ('context', []), ('review', {}), ('observed_at', '2020-01-01'), ('configuration_id', ['wrong'])):
            original = copy.deepcopy(run[field])
            run[field] = value
            with self.assertRaises(ValueError):
                self.build()
            run[field] = original
        message = self.load('feature.json')
        message['event'] = 'plan_ready'
        self.save('feature.json', message)
        with self.assertRaisesRegex(ValueError, 'not plans'):
            self.build()


if __name__ == '__main__':
    unittest.main()
