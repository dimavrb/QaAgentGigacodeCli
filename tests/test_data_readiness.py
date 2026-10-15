"""Synthetic local contracts; no model quality or live application claims."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import context_builder
import data_readiness as data
import task_exchange as exchange
from qa_contract import validate_message

spec = importlib.util.spec_from_file_location('data_readiness_demo', ROOT / 'examples/data-readiness/build_demo.py')
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class DataReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = demo.build(Path(self.temp.name) / 'demo')
        self.assignment = exchange.read_json(self.root / 'assignment.json')
        self.message = exchange.read_json(self.root / 'result.json')
        self.scope_path = self.root / 'data-assignment.json'
        self.scope = exchange.read_json(self.scope_path)
        self.state = self.root / 'task.json'
        exchange.init_task(self.state, 'QA-DATA-DEMO', 'Synthetic data readiness',
                           exchange.read_json(self.root / 'criteria.json'))

    def check(self, message=None, **kwargs):
        return data.check_result(message or self.message, self.root, self.scope_path,
                                 self.assignment['result_path'], **kwargs)

    def submit(self, message=None, assignment=None):
        target = assignment or self.assignment
        exchange.assign(self.state, target, self.root, None)
        exchange.prepare_handoff(self.state, 'data-1', None)
        exchange.atomic_write(target['result_path'], message or self.message)
        exchange.submit_result(self.state, 'data-1', None)

    def accept(self):
        self.submit()
        exchange.review_result(self.state, 'data-1', 'Synthetic observations read', True, None)

    def consumer(self, ids=('TC-1',), mode='ready'):
        return dict(task_id='QA-DATA-DEMO', request_id='consumer', agent='qa-analyst',
            stage='consumer', goal='Use synthetic data', acceptance_criteria=['Source checked'],
            stop_condition='Result or blocker', expected_artifact='JSON', inputs=[],
            result_path=str(self.root / 'consumer.json'), depends_on=['data-1'],
            dependency_modes={'data-1': mode}, scenario_ids=list(ids))

    def cli(self, *extra):
        exchange.atomic_write(self.assignment['result_path'], self.message)
        return subprocess.run([sys.executable, str(ROOT / 'scripts/validate_qa_result.py'),
            '--input', self.assignment['result_path'], '--task-id', 'QA-DATA-DEMO', '--request-id', 'data-1',
            '--agent', 'qa-test-data-builder', '--require-data-checks', '--scenario-id', 'TC-1',
            '--scenario-id', 'TC-2', *extra], capture_output=True, text=True)

    def test_file_dataset_properties_relations_visibility_and_ready_handoff(self):
        paths = self.check()
        self.assertIn(str(self.root / 'D-OWNER.json'), paths)
        self.accept()
        row = exchange.read_state(self.state)['requests']['data-1']
        self.assertIn(str(self.scope_path), row['input_hashes'])
        self.assertIn(str(self.root / 'requirements.txt'), row['input_hashes'])
        self.assertIn(str(self.root / 'D-READ.json'), row['result']['artifact_hashes'])
        exchange.assign(self.state, self.consumer(), self.root, None)
        exchange.prepare_handoff(self.state, 'consumer', None)

    def test_missing_duplicate_foreign_or_unstructured_property_is_rejected(self):
        for mutation in ('missing', 'duplicate', 'foreign', 'string'):
            message = copy.deepcopy(self.message)
            rows = message['payload']['verified_properties']
            if mutation == 'missing': rows.pop()
            elif mutation == 'duplicate': rows.append(copy.deepcopy(rows[0]))
            elif mutation == 'foreign': rows[0]['requirement_id'] = 'foreign'
            else: rows[0] = 'looks ready'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.check(message)

    def test_all_unfinished_statuses_block_full_readiness(self):
        for status in ('failed', 'blocked', 'not_run'):
            message = copy.deepcopy(self.message)
            message['payload']['verified_properties'][0]['status'] = status
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'every property'):
                self.check(message)

    def test_recipe_or_creation_without_observation_is_not_ready(self):
        message = copy.deepcopy(self.message)
        message['payload'].pop('data_ids')
        message['payload']['recipe'] = 'Create the synthetic dataset'
        for field, value in (('evidence', []), ('actual_result', ''), ('observed_at', None)):
            invalid = copy.deepcopy(message)
            invalid['payload']['verified_properties'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check(invalid)
        self.check(message)  # A recipe plus actual checked observations is supported.

    def test_cleaned_or_unknown_dataset_cannot_be_ready(self):
        for state in ('cleaned', 'unknown', None):
            message = copy.deepcopy(self.message)
            message['payload']['data_state'] = state
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.check(message)

    def test_partial_is_derived_from_all_required_conditions(self):
        root = demo.build(Path(self.temp.name) / 'partial', partial=True)
        assignment = exchange.read_json(root / 'assignment.json')
        message = exchange.read_json(root / 'result.json')
        data.check_result(message, root, assignment['data_assignment'], assignment['result_path'])
        self.submit(message, assignment)
        exchange.review_result(self.state, 'data-1', 'Synthetic partial observed', True, None)
        with self.assertRaisesRegex(ValueError, 'сценариев'):
            exchange.assign(self.state, self.consumer(['TC-2']), self.root, None)
        exchange.assign(self.state, self.consumer(['TC-1']), self.root, None)
        message['payload'].update(scenario_ids=['TC-2'], blocked_scenarios=['TC-1'])
        with self.assertRaisesRegex(ValueError, 'ready scenarios differ'):
            data.check_result(message, root, assignment['data_assignment'], assignment['result_path'])

    def test_unverified_shared_visibility_blocks_every_scenario(self):
        message = copy.deepcopy(self.message)
        message['event'] = 'data_partial'
        message['payload'].update(scenario_ids=['TC-1'], blocked_scenarios=['TC-2'])
        message['payload']['verified_properties'][-1]['status'] = 'not_run'
        with self.assertRaisesRegex(ValueError, 'proven ready subset'):
            self.check(message)

    def test_task_environment_version_and_scope_hash_are_bound(self):
        for key in ('environment_id', 'product_version', 'data_assignment_sha256'):
            message = copy.deepcopy(self.message)
            message['payload'][key] = 'other'
            with self.subTest(key=key), self.assertRaises(ValueError): self.check(message)
        message = copy.deepcopy(self.message)
        message['task_id'] = 'other'
        with self.assertRaises(ValueError): self.check(message)

    def test_old_future_and_unzoned_observations_are_rejected(self):
        now = datetime.now(timezone.utc)
        for value in ((now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat(),
                      '2026-10-15T12:00:00', 'invalid'):
            message = copy.deepcopy(self.message)
            message['payload']['verified_properties'][0]['observed_at'] = value
            with self.subTest(value=value), self.assertRaises(ValueError): self.check(message)

    def test_expired_dataset_is_diagnostic_context_not_ready_source(self):
        self.accept()
        future = datetime.now(timezone.utc) + timedelta(days=1)
        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None): return future
        with patch.object(data, 'datetime', FutureClock), patch.object(exchange, 'datetime', FutureClock):
            with self.assertRaisesRegex(ValueError, 'expired'):
                exchange.assign(self.state, self.consumer(), self.root, None)
            exchange.assign(self.state, self.consumer(mode='context'), self.root, None)
            # A context-audited/cached source cannot bypass the ready check.
            result = exchange.inspect_result(exchange.read_state(self.state)['requests']['data-1']['assignment'],
                                             data_context_only=True)
            with self.assertRaisesRegex(ValueError, 'expired'):
                exchange.check_readiness(result, ['TC-1'], 'qa-feature-tester')

    def test_source_scope_report_remote_missing_empty_files_are_not_evidence(self):
        empty = self.root / 'empty.txt'
        empty.touch()
        for ref in ('requirements.txt', str(self.scope_path), self.assignment['result_path'],
                    'https://example.invalid/log', 'missing.txt', str(empty)):
            message = copy.deepcopy(self.message)
            message['payload']['verified_properties'][0]['evidence'] = [ref]
            with self.subTest(ref=ref), self.assertRaises((ValueError, OSError)): self.check(message)

    def test_changed_nested_evidence_or_dataset_blocks_transition_atomically(self):
        self.accept()
        for path in (self.root / 'D-PAID.json', self.root / 'dataset.json'):
            original = path.read_bytes()
            path.write_text('changed')
            before = self.state.read_bytes()
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'изменились'):
                exchange.assign(self.state, self.consumer(), self.root, None)
            self.assertEqual(before, self.state.read_bytes())
            path.write_bytes(original)

    def test_changed_requirement_source_blocks_preparation(self):
        exchange.assign(self.state, self.assignment, self.root, None)
        (self.root / 'requirements.txt').write_text('Different requirements')
        with self.assertRaisesRegex(ValueError, 'изменились'):
            exchange.prepare_handoff(self.state, 'data-1', None)

    def test_malformed_scope_missing_coverage_and_foreign_task_are_rejected(self):
        for mutation in ('quote', 'empty', 'duplicate', 'coverage', 'kind', 'time', 'scenario', 'bool-version'):
            scope = copy.deepcopy(self.scope)
            if mutation == 'quote': scope['requirements'][0]['quote'] = 'Invented'
            elif mutation == 'empty': scope['requirements'] = []
            elif mutation == 'duplicate': scope['requirements'].append(scope['requirements'][0])
            elif mutation == 'coverage': scope['requirements'] = scope['requirements'][:1]
            elif mutation == 'kind': scope['requirements'][0]['kind'] = []
            elif mutation == 'time': scope['expires_at'] = scope['created_at']
            elif mutation == 'scenario': scope['requirements'][0]['scenario_ids'] = ['foreign']
            else: scope['schema_version'] = True
            exchange.atomic_write(self.scope_path, scope)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): data.load_assignment(self.scope_path)
        scope = copy.deepcopy(self.scope)
        scope['task_id'] = 'foreign'
        exchange.atomic_write(self.scope_path, scope)
        with self.assertRaisesRegex(ValueError, 'task/scenario'):
            exchange.assign(self.state, self.assignment, self.root, None)

    def test_new_assignment_cannot_disable_data_checks(self):
        assignment = dict(self.assignment, data_checks_required=False)
        del assignment['data_assignment']
        exchange.assign(self.state, assignment, self.root, None)
        self.assertTrue(exchange.read_state(self.state)['requests']['data-1']['assignment']['data_checks_required'])
        exchange.prepare_handoff(self.state, 'data-1', None)
        self.message['payload']['verified_properties'] = ['created']
        self.message['payload'].pop('data_assignment_sha256')
        exchange.atomic_write(self.assignment['result_path'], self.message)
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, 'coordinator data_assignment'):
            exchange.submit_result(self.state, 'data-1', None)
        self.assertEqual(before, self.state.read_bytes())

    def test_missing_scope_can_be_reported_as_blocked(self):
        assignment = dict(self.assignment)
        del assignment['data_assignment']
        message = copy.deepcopy(self.message)
        message.update(event='blocked', blockers=['Provide data requirements'], payload={})
        for check in message['self_review']['checks']:
            check['evidence'] = ['report:/blockers', 'assignment:/acceptance_criteria']
        self.submit(message, assignment)
        exchange.review_result(self.state, 'data-1', 'Missing scope confirmed', True, None)
        with self.assertRaises(ValueError): exchange.assign(self.state, self.consumer(), self.root, None)
        exchange.assign(self.state, self.consumer(mode='context'), self.root, None)

    def test_legacy_ready_is_context_only_without_rewriting_history(self):
        assignment = dict(self.assignment)
        del assignment['data_assignment']
        exchange.assign(self.state, assignment, self.root, None)
        state = exchange.read_state(self.state)
        del state['requests']['data-1']['assignment']['data_checks_required']
        exchange.atomic_write(self.state, state)
        exchange.prepare_handoff(self.state, 'data-1', None)
        self.message['payload'].pop('data_assignment_sha256')
        self.message['payload']['verified_properties'] = ['Synthetic legacy declaration']
        exchange.atomic_write(self.assignment['result_path'], self.message)
        exchange.submit_result(self.state, 'data-1', None)
        exchange.review_result(self.state, 'data-1', 'Historical structural audit', True, None)
        with self.assertRaisesRegex(ValueError, 'Свойства данных'):
            exchange.assign(self.state, self.consumer(), self.root, None)
        exchange.assign(self.state, self.consumer(mode='context'), self.root, None)

    def test_data_ids_must_be_unique_nonempty_strings(self):
        for ids in ([None], [''], ['one', 'one'], [{}], [1]):
            message = copy.deepcopy(self.message)
            message['payload']['data_ids'] = ids
            with self.subTest(ids=ids):
                self.assertTrue(validate_message(message, 'QA-DATA-DEMO', 'data-1', 'qa-test-data-builder', ['TC-1', 'TC-2']))

    def test_cli_requires_independent_scope_and_matching_scenarios(self):
        missing = self.cli()
        self.assertEqual(2, missing.returncode)
        valid = self.cli('--data-assignment', str(self.scope_path))
        self.assertEqual(0, valid.returncode, valid.stdout + valid.stderr)
        wrong = self.cli('--data-assignment', str(self.scope_path), '--scenario-id', 'foreign')
        self.assertEqual(2, wrong.returncode)
        self.assertNotIn('Traceback', wrong.stderr)

    def test_relative_evidence_uses_assignment_artifact_base(self):
        reports = self.root / 'reports'
        reports.mkdir()
        self.assignment.update(result_path=str(reports / 'result.json'), artifact_base=str(self.root))
        self.submit()
        row = exchange.read_state(self.state)['requests']['data-1']
        self.assertIn(str(self.root / 'D-OWNER.json'), row['result']['artifact_hashes'])

    def test_bare_builder_and_data_reviewer_receive_protocol(self):
        for assignment in (self.assignment, dict(agent='qa-result-reviewer', review_target=dict(scope='data'))):
            self.assertIn('docs/DATA_READINESS.md', context_builder.profile(assignment, {})['documents'])

    def test_scoped_blocked_keeps_all_unverified_conditions_and_cannot_finish(self):
        message = copy.deepcopy(self.message)
        message.update(event='blocked', blockers=['Synthetic visibility unavailable'])
        for check in message['payload']['verified_properties']:
            check.update(status='not_run', actual_result='Synthetic check not run', evidence=[])
            check.pop('observed_at')
        message['payload']['data_state'] = 'unknown'
        self.submit(message)
        exchange.review_result(self.state, 'data-1', 'Missing availability observed', True, None)
        with self.assertRaises(ValueError):
            exchange.finish_task(self.state, 'passed', 'Still blocked', None)

    def test_expired_cli_requires_explicit_context_audit(self):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        self.scope.update(created_at=(past - timedelta(hours=1)).isoformat(), expires_at=past.isoformat())
        exchange.atomic_write(self.scope_path, self.scope)
        self.message['payload']['data_assignment_sha256'] = data.sha(self.scope_path)
        for row in self.message['payload']['verified_properties']:
            row['observed_at'] = (past - timedelta(minutes=1)).isoformat()
        expired = self.cli('--data-assignment', str(self.scope_path))
        self.assertEqual(2, expired.returncode)
        self.assertIn('expired', expired.stdout)
        diagnostic = self.cli('--data-assignment', str(self.scope_path), '--data-context-only')
        self.assertEqual(0, diagnostic.returncode, diagnostic.stdout + diagnostic.stderr)
        self.assertIn('Context audit only', diagnostic.stdout)

    def test_data_scope_is_restricted_to_data_role(self):
        assignment = dict(self.assignment, agent='qa-analyst')
        with self.assertRaisesRegex(ValueError, 'requires qa-test-data-builder'):
            exchange.assign(self.state, assignment, self.root, None)

    def test_documented_commands_reach_acceptance_and_verified_completion(self):
        for args in (['assign', '--input', str(self.root / 'assignment.json'), '--expected-revision', '1'],
                     ['prepare', '--request-id', 'data-1', '--expected-revision', '2'],
                     ['submit', '--request-id', 'data-1', '--expected-revision', '3'],
                     ['review', '--request-id', 'data-1', '--decision', 'Synthetic observations read', '--expected-revision', '4']):
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/manage_task.py'),
                                  '--state', str(self.state), *args], capture_output=True, text=True)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        exchange.assess_criteria(self.state, dict(assessments=[dict(criterion_id='C-DATA', status='verified',
            decision='Synthetic observations only', request_ids=['data-1'],
            evidence=[str(self.root / 'D-PAID.json')])]), 5)
        exchange.finish_task(self.state, 'passed', 'Synthetic protocol verified', 6)
        self.assertEqual('completed', exchange.read_state(self.state)['execution_status'])


if __name__ == '__main__':
    unittest.main()
