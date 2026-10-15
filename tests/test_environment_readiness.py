import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import environment_readiness as environment
import task_exchange as exchange
import context_builder
from environment_fixtures import bind_environment, attach_environment_checks
from self_review_fixtures import review_for


class EnvironmentReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'task.json'
        exchange.init_task(self.state, 'QA-ENV', 'Synthetic admin guide readiness')
        self.proof = self.root / 'probe.txt'
        self.proof.write_text('Synthetic observations; no live stand checked')
        self.assignment = dict(task_id='QA-ENV', request_id='env', agent='qa-test-env-configurator',
            stage='environment', goal='Execute every synthetic guide point',
            acceptance_criteria=['Every guide point is verified'], stop_condition='Return evidence or blocker',
            expected_artifact='QA JSON', inputs=[], depends_on=[], scenario_ids=['TC-1', 'TC-2'],
            result_path=str(self.root / 'result.json'))
        bind_environment(self.assignment, self.root)
        self.scope_path = Path(self.assignment['environment_assignment'])

    def message(self, event='environment_ready'):
        result = dict(task_id='QA-ENV', request_id='env', agent='qa-test-env-configurator',
            event=event, summary='Synthetic admin guide result', artifacts=[],
            blockers=['Synthetic missing condition'] if event == 'blocked' else [],
            self_review=review_for(limited=event == 'blocked'),
            payload=dict(environment_id='fixture', product_version='v1',
                verified_capabilities=['Synthetic only'], limitations=[]))
        if event == 'environment_partial':
            result['payload'].update(ready_scenarios=['TC-1'], blocked_scenarios=['TC-2'])
        return attach_environment_checks(result, self.assignment, [str(self.proof)])

    def check(self, message):
        return environment.check_result(message, self.root, self.scope_path, self.assignment['result_path'])

    def submit(self, message=None, assignment=None):
        exchange.assign(self.state, assignment or self.assignment, self.root, None)
        exchange.prepare_handoff(self.state, 'env', None)
        exchange.atomic_write(self.assignment['result_path'], message or self.message())
        return exchange.submit_result(self.state, 'env', None)

    def test_all_points_pass_with_shared_observation_file(self):
        paths = self.check(self.message())
        self.assertIn(str(self.proof), paths)
        self.assertIn(str(self.scope_path), paths)

    def test_missing_duplicate_and_foreign_points_cannot_be_ready(self):
        for mutation in ('missing', 'duplicate', 'foreign'):
            message = self.message()
            rows = message['payload']['guide_checks']
            if mutation == 'missing':
                rows.pop()
            elif mutation == 'duplicate':
                rows.append(copy.deepcopy(rows[0]))
            else:
                rows[0]['point_id'] = 'not-in-guide'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.check(message)

    def test_each_unfinished_status_prevents_full_readiness(self):
        for status in ('failed', 'blocked', 'not_run'):
            message = self.message()
            message['payload']['guide_checks'][0]['status'] = status
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'every admin guide point'):
                self.check(message)

    def test_successful_command_without_actions_observation_or_evidence_is_insufficient(self):
        for key, value in (('actions', []), ('actual_result', ''), ('evidence', [])):
            message = self.message()
            message['payload']['guide_checks'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check(message)

    def test_guide_snapshot_report_and_remote_link_are_not_execution_evidence(self):
        scope = json.loads(self.scope_path.read_text())
        for ref in (scope['admin_guide']['path'], str(self.scope_path), self.assignment['result_path'],
                    'https://example.invalid/probe', str(self.root / 'missing.txt')):
            message = self.message()
            message['payload']['guide_checks'][0]['evidence'] = [ref]
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                self.check(message)

    def test_context_and_scope_hash_must_match_coordinator(self):
        for key in ('environment_id', 'product_version', 'environment_assignment_sha256'):
            message = self.message()
            message['payload'][key] = 'other'
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check(message)
        message = self.message()
        message['task_id'] = 'other'
        with self.assertRaises(ValueError):
            self.check(message)

    def test_old_future_and_unzoned_observations_are_rejected(self):
        now = datetime.now(timezone.utc)
        for stamp in ((now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat(),
                      '2026-10-15T10:00:00', 'invalid'):
            message = self.message()
            message['payload']['guide_checks'][0]['observed_at'] = stamp
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                self.check(message)

    def test_partial_readiness_is_derived_from_all_related_points(self):
        message = self.message('environment_partial')
        self.check(message)
        message['payload'].update(ready_scenarios=['TC-2'], blocked_scenarios=['TC-1'])
        with self.assertRaisesRegex(ValueError, 'partial readiness differs'):
            self.check(message)

    def test_unfinished_shared_point_blocks_every_scenario(self):
        scope = json.loads(self.scope_path.read_text())
        guide = Path(scope['admin_guide']['path'])
        guide.write_text(guide.read_text() + '\nShared dependency')
        scope['admin_guide']['sha256'] = hashlib.sha256(guide.read_bytes()).hexdigest()
        scope['points'].append(dict(point_id='COMMON', quote='Shared dependency',
                                    expected_result='Shared dependency works'))
        exchange.atomic_write(self.scope_path, scope)
        message = self.message('environment_partial')
        message['payload']['guide_checks'][-1]['status'] = 'blocked'
        with self.assertRaisesRegex(ValueError, 'proven ready subset'):
            self.check(message)

    def test_snapshot_requires_real_quotes_nonempty_points_and_scenario_coverage(self):
        original = json.loads(self.scope_path.read_text())
        for mutation in ('quote', 'empty', 'duplicate', 'coverage', 'version-type'):
            scope = copy.deepcopy(original)
            if mutation == 'quote': scope['points'][0]['quote'] = 'Invented guide instruction'
            elif mutation == 'empty': scope['points'] = []
            elif mutation == 'duplicate': scope['points'].append(scope['points'][0])
            elif mutation == 'coverage': scope['points'].pop()
            else: scope['schema_version'] = True
            exchange.atomic_write(self.scope_path, scope)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                environment.load_assignment(self.scope_path)

    def test_guide_and_snapshot_are_frozen_inputs_and_nested_evidence_is_hashed(self):
        self.submit()
        row = exchange.read_state(self.state)['requests']['env']
        self.assertIn(str(self.scope_path), row['input_hashes'])
        self.assertIn(str(self.proof), row['result']['artifact_hashes'])
        exchange.review_result(self.state, 'env', 'Synthetic guide evidence read', True, None)
        self.proof.write_text('Changed observation')
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, 'изменились'):
            exchange.assign(self.state, dict(self.assignment, request_id='consumer',
                result_path=str(self.root / 'consumer.json'), depends_on=['env']), self.root, None)
        self.assertEqual(before, self.state.read_bytes())

    def test_changed_guide_cannot_be_prepared(self):
        exchange.assign(self.state, self.assignment, self.root, None)
        scope = json.loads(self.scope_path.read_text())
        Path(scope['admin_guide']['path']).write_text('Changed guide')
        with self.assertRaisesRegex(ValueError, 'изменились'):
            exchange.prepare_handoff(self.state, 'env', None)

    def test_new_assignment_cannot_disable_guide_gate(self):
        assignment = dict(self.assignment, environment_checks_required=False)
        del assignment['environment_assignment']
        exchange.assign(self.state, assignment, self.root, None)
        self.assertTrue(exchange.read_state(self.state)['requests']['env']['assignment']['environment_checks_required'])
        exchange.prepare_handoff(self.state, 'env', None)
        message = self.message()
        del message['payload']['guide_checks']
        del message['payload']['environment_assignment_sha256']
        exchange.atomic_write(self.assignment['result_path'], message)
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, 'coordinator environment_assignment'):
            exchange.submit_result(self.state, 'env', None)
        self.assertEqual(before, self.state.read_bytes())

    def test_missing_guide_can_be_reported_as_blocked(self):
        assignment = dict(self.assignment)
        del assignment['environment_assignment']
        message = dict(task_id='QA-ENV', request_id='env', agent='qa-test-env-configurator',
            event='blocked', summary='Admin guide missing', artifacts=[], blockers=['Provide complete admin guide'],
            payload={}, self_review=review_for(limited=True))
        self.submit(message, assignment)
        exchange.review_result(self.state, 'env', 'Missing guide confirmed', True, None)
        with self.assertRaisesRegex(ValueError, 'незавершённые'):
            exchange.finish_task(self.state, 'passed', 'Guide remains missing', None)

    def test_cli_requires_independent_scope_for_new_result(self):
        exchange.atomic_write(self.assignment['result_path'], self.message())
        command = [sys.executable, str(ROOT / 'scripts/validate_qa_result.py'), '--input', self.assignment['result_path'],
                   '--task-id', 'QA-ENV', '--request-id', 'env', '--agent', 'qa-test-env-configurator',
                   '--require-environment-checks', '--scenario-id', 'TC-1', '--scenario-id', 'TC-2']
        missing = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(0, missing.returncode)
        self.assertNotIn('Traceback', missing.stderr)
        valid = subprocess.run(command + ['--environment-assignment', str(self.scope_path)], capture_output=True, text=True)
        self.assertEqual(0, valid.returncode, valid.stdout + valid.stderr)

    def test_bare_environment_context_contains_readiness_contract(self):
        self.assertIn('docs/ENVIRONMENT_READINESS.md', context_builder.profile(self.assignment, {})['documents'])

    def test_historical_environment_result_is_context_not_new_readiness(self):
        assignment = dict(self.assignment)
        del assignment['environment_assignment']
        exchange.assign(self.state, assignment, self.root, None)
        # Simulate a persisted pre-guide registry without migrating its observations.
        state = exchange.read_state(self.state)
        del state['requests']['env']['assignment']['environment_checks_required']
        exchange.atomic_write(self.state, state)
        exchange.prepare_handoff(self.state, 'env', None)
        message = self.message()
        del message['payload']['guide_checks']
        del message['payload']['environment_assignment_sha256']
        exchange.atomic_write(assignment['result_path'], message)
        exchange.submit_result(self.state, 'env', None)
        exchange.review_result(self.state, 'env', 'Historical structural audit', True, None)
        consumer = dict(assignment, request_id='next', result_path=str(self.root / 'next.json'), depends_on=['env'])
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, 'админгайду'):
            exchange.assign(self.state, consumer, self.root, None)
        self.assertEqual(before, self.state.read_bytes())
        consumer['dependency_modes'] = {'env': 'context'}
        exchange.assign(self.state, consumer, self.root, None)

    def test_relative_evidence_uses_assignment_base_even_when_result_is_elsewhere(self):
        result_dir = self.root / 'reports'
        result_dir.mkdir()
        self.assignment.update(result_path=str(result_dir / 'result.json'), artifact_base=str(self.root))
        message = self.message()
        for row in message['payload']['guide_checks']:
            row['evidence'] = [self.proof.name]
        self.submit(message)
        self.assertIn(str(self.proof), exchange.read_state(self.state)['requests']['env']['result']['artifact_hashes'])

    def test_documented_demo_reaches_acceptance_and_task_completion(self):
        output = self.root / 'demo'
        run = subprocess.run([sys.executable, str(ROOT / 'examples/environment-readiness/build_demo.py'),
                              '--output-dir', str(output)], capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)
        state = output / 'task.json'
        assignment = exchange.read_json(output / 'assignment.json')
        exchange.init_task(state, 'QA-ENV-DEMO', 'Synthetic demo', exchange.read_json(output / 'criteria.json'))
        exchange.assign(state, assignment, output, 1)
        exchange.prepare_handoff(state, 'env-1', 2)
        exchange.submit_result(state, 'env-1', 3)
        accepted = exchange.review_result(state, 'env-1', 'Synthetic guide and observations read', True, 4)
        message = exchange.read_json(output / 'result.json')
        exchange.assess_criteria(state, dict(assessments=[dict(criterion_id='C-GUIDE', status='verified',
            decision='Only synthetic protocol accepted', request_ids=['env-1'],
            evidence=message['payload']['guide_checks'][0]['evidence'])]), accepted['revision'])
        exchange.finish_task(state, 'passed', 'Synthetic protocol completed without product actions',
                             exchange.read_state(state)['revision'])
        self.assertEqual('completed', exchange.read_state(state)['execution_status'])


if __name__ == '__main__':
    unittest.main()
