"""Registry and evidence gates for synthetic stubs; no live sbermock/model."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import context_builder
import stub_contract as stubs
import task_exchange as exchange
from self_review_fixtures import review_for


class StubHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / 'stubs #fixture').resolve()
        shutil.copytree(ROOT / 'examples/stubs', self.root)
        self.state = self.root / 'task.json'
        self.scope_path = self.root / 'assignment.json'
        self.scope = stubs.read_stub_json(self.scope_path)
        self.message = exchange.read_json(self.root / 'result.json')
        self.message['self_review'] = review_for()
        self.profile = exchange.read_json(self.root / 'profile.json')
        self.proof = exchange.read_json(self.root / 'verification.json')
        self.assignment = dict(task_id=self.scope['task_id'], request_id=self.scope['request_id'],
            agent='qa-stub-builder', stage='stubs', goal='Check synthetic REST/Kafka fixture only',
            acceptance_criteria=['Intent and observations agree'], stop_condition='Evidence or blocker',
            expected_artifact='QA JSON', result_path=str(self.root / 'result.json'), inputs=[], depends_on=[],
            scenario_ids=self.scope['scenario_ids'], criterion_ids=['C-STUB'], stub_assignment=str(self.scope_path))
        exchange.init_task(self.state, self.scope['task_id'], 'Synthetic stub handoff', dict(schema_version=1,
            criteria=[dict(criterion_id='C-STUB', description='Synthetic observations support stub readiness',
                           source='requirements.md', required=True)]))
        self.rid = self.assignment['request_id']
        self.save()

    def save(self):
        # Only fresh temporary test copies are changed; shipped historical bytes stay intact.
        exchange.atomic_write(self.scope_path, self.scope)
        self.profile['assignment_sha256'] = self.proof['assignment_sha256'] = stubs.assignment_digest(self.scope)
        exchange.atomic_write(self.root / 'profile.json', self.profile)
        self.proof['profile_sha256'] = hashlib.sha256((self.root / 'profile.json').read_bytes()).hexdigest()
        exchange.atomic_write(self.root / 'verification.json', self.proof)
        exchange.atomic_write(self.assignment['result_path'], self.message)

    def submit(self):
        exchange.assign(self.state, self.assignment, self.root, None)
        exchange.prepare_handoff(self.state, self.rid, None)
        exchange.submit_result(self.state, self.rid, None)

    def accept(self):
        self.submit()
        exchange.review_result(self.state, self.rid, 'Synthetic evidence read; no MCP operations', True, None)

    def consumer(self, ids=('TC-REST',), role='qa-analyst'):
        return dict(task_id=self.scope['task_id'], request_id='consumer', agent=role, stage='consumer',
            goal='Use confirmed scope only', acceptance_criteria=['Source ready'], stop_condition='QA result',
            expected_artifact='JSON', result_path=str(self.root / 'consumer.json'), inputs=[],
            depends_on=[self.rid], scenario_ids=list(ids))

    def test_scope_is_frozen_and_guard_cannot_be_disabled(self):
        assignment = dict(self.assignment, stub_evidence_required=False)
        exchange.assign(self.state, assignment, self.root, None)
        row = exchange.read_state(self.state)['requests'][self.rid]
        self.assertTrue(row['assignment']['stub_evidence_required'])
        self.assertIn(str(self.scope_path), row['assignment']['inputs'])
        self.assertIn(str(self.scope_path), row['input_hashes'])
        handoff = exchange.prepare_handoff(self.state, self.rid, None)['result']
        context = context_builder.assemble(handoff, {})
        self.assertIn(str(self.scope_path), context['input_contents'])

    def test_preflight_rejects_wrong_task_request_scenarios_and_role_atomically(self):
        for key, value in (('task_id', 'other'), ('request_id', 'other'), ('scenario_ids', ['other']),
                           ('agent', 'qa-analyst')):
            assignment = dict(self.assignment, **{key: value})
            before = self.state.read_bytes()
            with self.subTest(key=key), self.assertRaises(ValueError):
                exchange.assign(self.state, assignment, self.root, None)
            self.assertEqual(before, self.state.read_bytes())

    def test_malformed_intent_is_rejected_before_worker_handoff(self):
        self.scope['cases'][0].update(allowed_operations=['updated'], target_stub_id=None)
        self.save()
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, 'explicitly assigned target'):
            exchange.assign(self.state, self.assignment, self.root, None)
        self.assertEqual(before, self.state.read_bytes())

    def test_scope_cannot_change_between_assign_and_prepare(self):
        exchange.assign(self.state, self.assignment, self.root, None)
        self.scope['namespace'] = 'other-namespace'
        self.save()
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, 'изменились'):
            exchange.prepare_handoff(self.state, self.rid, None)
        self.assertEqual(before, self.state.read_bytes())

    def test_resealing_report_cannot_replace_coordinator_intent_before_submit(self):
        exchange.assign(self.state, self.assignment, self.root, None)
        exchange.prepare_handoff(self.state, self.rid, None)
        # Even a self-consistent rewrite cannot change the frozen approved intent.
        self.scope['cases'][0]['purpose'] = 'Changed purpose after handoff'
        self.profile['stubs'][0]['purpose'] = self.scope['cases'][0]['purpose']
        self.save()
        self.assertEqual([], stubs.validate_stub_bundle(self.message, self.root, self.scope))
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, 'изменились'):
            exchange.submit_result(self.state, self.rid, None)
        self.assertEqual(before, self.state.read_bytes())

    def test_nested_readback_and_probe_files_are_registered_and_can_prove_criterion(self):
        self.accept()
        row = exchange.read_state(self.state)['requests'][self.rid]
        for case in self.proof['checks']:
            for kind in ('readback', 'positive', 'negative'):
                self.assertIn(str((self.root / case[kind]['artifact']).resolve()), row['result']['artifact_hashes'])
        proof = str(self.root / self.proof['checks'][0]['positive']['artifact'])
        exchange.assess_criteria(self.state, dict(assessments=[dict(criterion_id='C-STUB', status='verified',
            decision='Synthetic primary observation read', request_ids=[self.rid], evidence=[proof])]), None)
        exchange.finish_task(self.state, 'passed', 'Synthetic stub protocol completed', None)
        self.assertEqual('completed', exchange.read_state(self.state)['execution_status'])

    def test_observation_changed_after_cli_validation_is_detected_atomically(self):
        exchange.assign(self.state, self.assignment, self.root, None)
        exchange.prepare_handoff(self.state, self.rid, None)
        real_run = subprocess.run
        path = self.root / self.proof['checks'][0]['positive']['artifact']
        def race(*args, **kwargs):
            result = real_run(*args, **kwargs)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            path.write_bytes(path.read_bytes() + b'\n')
            return result
        before = self.state.read_bytes()
        with patch.object(exchange.subprocess, 'run', race), self.assertRaisesRegex(ValueError, 'Артефакты изменились'):
            exchange.submit_result(self.state, self.rid, None)
        self.assertEqual(before, self.state.read_bytes())

    def test_partial_handoff_opens_only_verified_scenarios(self):
        self.message.update(event='stubs_partial')
        self.message['payload'].update(scenario_ids=['TC-REST'], blocked_scenarios=['TC-KAFKA'])
        self.proof['checks'][1] = dict(case_id='KAFKA-READY', status='blocked', reason='Synthetic capture unavailable')
        self.save()
        self.accept()
        with self.assertRaisesRegex(ValueError, 'сценариев'):
            exchange.assign(self.state, self.consumer(['TC-KAFKA']), self.root, None)
        exchange.assign(self.state, self.consumer(['TC-REST']), self.root, None)

    def test_configured_source_cannot_open_product_execution(self):
        self.scope['mode'] = 'configure_only'
        self.message['event'] = 'stubs_configured'
        self.message['payload']['configured_scenarios'] = self.message['payload'].pop('scenario_ids')
        for check in self.proof['checks']:
            check.update(status='configured', reason='Synthetic setup only; no runtime probes')
            check.pop('positive')
            check.pop('negative')
        self.save()
        self.accept()
        with self.assertRaisesRegex(ValueError, 'не готов'):
            exchange.assign(self.state, self.consumer(role='qa-feature-tester'), self.root, None)
        exchange.assign(self.state, self.consumer(role='qa-stub-builder'), self.root, None)
        row = exchange.read_state(self.state)['requests'][self.rid]
        self.assertNotIn(str(self.root / 'observations/rest-positive.json'), row['result']['artifact_hashes'])

    def test_relative_evidence_uses_artifact_base_when_result_is_elsewhere(self):
        reports = self.root / 'reports'
        reports.mkdir()
        self.assignment.update(result_path=str(reports / 'result.json'), artifact_base=str(self.root))
        self.save()
        self.submit()
        row = exchange.read_state(self.state)['requests'][self.rid]
        self.assertIn(str(self.root / 'observations/kafka-negative.json'), row['result']['artifact_hashes'])

    def test_legacy_assignment_preserves_accepted_hash_set_without_migration(self):
        exchange.assign(self.state, self.assignment, self.root, None)
        state = exchange.read_state(self.state)
        state['requests'][self.rid]['assignment'].pop('stub_evidence_required')
        exchange.atomic_write(self.state, state)
        exchange.prepare_handoff(self.state, self.rid, None)
        exchange.submit_result(self.state, self.rid, None)
        exchange.review_result(self.state, self.rid, 'Synthetic legacy inspection', True, None)
        saved = exchange.read_state(self.state)['requests'][self.rid]['result']['artifact_hashes']
        self.assertNotIn(str(self.root / 'observations/rest-positive.json'), saved)
        exchange.assign(self.state, self.consumer(), self.root, None)
        self.assertEqual(saved, exchange.read_state(self.state)['requests'][self.rid]['result']['artifact_hashes'])

    def test_discovery_and_blocked_remain_possible_without_execution_scope(self):
        self.assignment.pop('stub_assignment')
        self.message = exchange.read_json(self.root / 'plan-result.json')
        self.message['self_review'] = review_for()
        exchange.atomic_write(self.assignment['result_path'], self.message)
        self.accept()
        with self.assertRaisesRegex(ValueError, 'не готов'):
            exchange.assign(self.state, self.consumer(role='qa-stub-builder'), self.root, None)

    def test_scope_reviewer_receives_subject_contract(self):
        for assignment in (self.assignment, dict(agent='qa-result-reviewer', review_target=dict(scope='stubs'))):
            self.assertIn('docs/STUBS.md', context_builder.profile(assignment, {})['documents'])

    def test_restore_snapshot_is_registered_as_primary_material(self):
        stub = self.profile['stubs'][0]
        self.scope['cases'][0].update(allowed_operations=['updated'], target_stub_id=stub['stub_id'])
        before = exchange.read_json(self.root / 'observations/rest-readback.json')
        before.update(definition={'response': {'status': 503}},
            observed_at=(stubs.instant(self.scope['issued_at']) + timedelta(seconds=30)).isoformat())
        path = self.root / 'observations/rest-before.json'
        exchange.atomic_write(path, before)
        stub.update(operation='updated', cleanup=dict(action='restore', reason='Restore only assigned owned revision',
            previous_definition=before['definition'], before=dict(artifact='observations/rest-before.json',
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())))
        self.save()
        self.submit()
        self.assertIn(str(path), exchange.read_state(self.state)['requests'][self.rid]['result']['artifact_hashes'])

    def test_missing_scope_can_be_reported_as_blocked_without_readiness(self):
        self.assignment.pop('stub_assignment')
        self.message = dict(task_id=self.scope['task_id'], request_id=self.rid, agent='qa-stub-builder',
            event='blocked', summary='Synthetic MCP/assignment unavailable', artifacts=[],
            blockers=['Provide an approved stub assignment'], payload={}, self_review=review_for(limited=True))
        exchange.atomic_write(self.assignment['result_path'], self.message)
        self.accept()
        with self.assertRaises(ValueError):
            exchange.assign(self.state, self.consumer(), self.root, None)
        consumer = self.consumer()
        consumer['dependency_modes'] = {self.rid: 'context'}
        exchange.assign(self.state, consumer, self.root, None)

    def test_cli_accepts_explicit_artifact_base_and_restricts_it_to_stub_role(self):
        reports = self.root / 'reports'
        reports.mkdir()
        result = reports / 'result.json'
        exchange.atomic_write(result, self.message)
        command = [sys.executable, str(ROOT / 'scripts/validate_qa_result.py'), '--input', str(result),
            '--task-id', self.scope['task_id'], '--request-id', self.rid, '--agent', 'qa-stub-builder',
            '--stub-assignment', str(self.scope_path), '--scenario-id', 'TC-REST', '--scenario-id', 'TC-KAFKA']
        missing_base = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(2, missing_base.returncode)
        run = subprocess.run(command + ['--stub-evidence-base', str(self.root)], capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        wrong_role = subprocess.run([sys.executable, str(ROOT / 'scripts/validate_qa_result.py'),
            '--input', str(result), '--task-id', self.scope['task_id'], '--request-id', self.rid,
            '--agent', 'qa-analyst', '--stub-evidence-base', str(self.root)], capture_output=True, text=True)
        self.assertEqual(2, wrong_role.returncode)
        self.assertIn('only for qa-stub-builder', wrong_role.stderr)

    def test_future_scope_is_rejected_by_registry_and_cli_preflight(self):
        self.scope['issued_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.save()
        with self.assertRaisesRegex(ValueError, 'future'):
            exchange.assign(self.state, self.assignment, self.root, None)
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/validate_stub_assignment.py'),
            '--input', str(self.scope_path), '--task-id', self.scope['task_id'], '--request-id', self.rid,
            '--scenario-id', 'TC-REST', '--scenario-id', 'TC-KAFKA'], capture_output=True, text=True)
        self.assertEqual(2, run.returncode)
        self.assertIn('future', run.stdout)

    def test_future_profile_preparation_verification_and_resolution_are_rejected(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for key in ('prepared_at', 'verification_started_at', 'resolved_at'):
            original = copy.deepcopy(self.profile)
            if key == 'resolved_at': self.profile['stubs'][0][key] = future
            else: self.profile[key] = future
            self.save()
            with self.subTest(key=key):
                self.assertTrue(any('future' in err for err in stubs.validate_stub_bundle(self.message, self.root, self.scope)))
            self.profile = original

    def test_future_readback_and_probes_are_rejected_even_with_correct_hashes(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for key in ('readback', 'positive', 'negative'):
            ref = self.proof['checks'][0][key]
            path = self.root / ref['artifact']
            original, old_hash = path.read_bytes(), ref['sha256']
            value = exchange.read_json(path)
            value['observed_at'] = future
            exchange.atomic_write(path, value)
            ref['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.save()
            with self.subTest(key=key):
                self.assertTrue(any('future' in err for err in stubs.validate_stub_bundle(self.message, self.root, self.scope)))
            path.write_bytes(original)
            ref['sha256'] = old_hash

    def test_future_kafka_capture_interval_cannot_prove_a_completed_check(self):
        start = datetime.now(timezone.utc) + timedelta(days=1)
        ref = self.proof['checks'][1]['negative']
        path = self.root / ref['artifact']
        value = exchange.read_json(path)
        value['capture'].update(started_at=start.isoformat(), ended_at=(start + timedelta(seconds=1)).isoformat())
        value['observed_at'] = value['capture']['ended_at']
        exchange.atomic_write(path, value)
        ref['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.save()
        errors = stubs.validate_stub_bundle(self.message, self.root, self.scope)
        self.assertTrue(any('Kafka observation interval' in err for err in errors), errors)


if __name__ == '__main__':
    unittest.main()
