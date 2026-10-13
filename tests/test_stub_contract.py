import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from qa_contract import validate_message
from stub_contract import assignment_digest, read_stub_json, validate_stub_bundle, validate_stub_plan


class StubContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'stubs'
        shutil.copytree(ROOT / 'examples/stubs', self.base)
        self.message = json.loads((self.base / 'result.json').read_text())
        self.profile = json.loads((self.base / 'profile.json').read_text())
        self.proof = json.loads((self.base / 'verification.json').read_text())
        self.assignment = json.loads((self.base / 'assignment.json').read_text())
        self.assigned = ['TC-REST', 'TC-KAFKA']

    def save(self, update_binding=True):
        (self.base / 'assignment.json').write_text(json.dumps(self.assignment, ensure_ascii=False))
        self.profile['assignment_sha256'] = self.proof['assignment_sha256'] = assignment_digest(self.assignment)
        (self.base / 'profile.json').write_text(json.dumps(self.profile, ensure_ascii=False))
        if update_binding:
            self.proof['profile_sha256'] = hashlib.sha256((self.base / 'profile.json').read_bytes()).hexdigest()
        (self.base / 'verification.json').write_text(json.dumps(self.proof, ensure_ascii=False))
        (self.base / 'result.json').write_text(json.dumps(self.message, ensure_ascii=False))

    def errors(self, update_binding=True):
        self.save(update_binding)
        return validate_message(self.message, 'QA-STUB-DEMO', 'QA-STUB-DEMO-1', 'qa-stub-builder', self.assigned) + validate_stub_bundle(self.message, self.base, self.assignment)

    def change_observation(self, index, key, change):
        ref = self.proof['checks'][index][key]
        path = self.base / ref['artifact']
        record = json.loads(path.read_text())
        change(record)
        path.write_text(json.dumps(record, ensure_ascii=False))
        ref['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()

    def block_kafka(self):
        self.message['event'] = 'stubs_partial'
        self.message['payload']['scenario_ids'] = ['TC-REST']
        self.message['payload']['blocked_scenarios'] = ['TC-KAFKA']
        self.proof['checks'][1] = {'case_id': 'KAFKA-READY', 'status': 'blocked', 'reason': 'Проверочное чтение Kafka недоступно'}

    def test_synthetic_rest_and_kafka_bundle(self):
        self.assertEqual([], validate_message(self.message, 'QA-STUB-DEMO', 'QA-STUB-DEMO-1', 'qa-stub-builder', self.assigned))
        self.assertEqual([], validate_stub_bundle(self.message, self.base, self.assignment))

    def test_partial_readiness_keeps_independent_scenario_ready(self):
        self.block_kafka()
        self.assertEqual([], self.errors())
        self.message['event'] = 'stubs_ready'
        self.assertTrue(any('appropriate blocked part' in e for e in self.errors()))

    def test_every_dependency_of_a_shared_stub_must_be_blocked(self):
        self.block_kafka()
        self.profile['stubs'][1]['scenario_ids'].append('TC-REST')
        self.assignment['cases'][1]['scenario_ids'].append('TC-REST')
        self.assertTrue(any('all unverified stub dependencies' in e for e in self.errors()))

    def test_pending_write_is_not_ready_and_does_not_require_retry(self):
        stub = self.profile['stubs'][1]
        stub.update(operation='pending', stub_id=None, resolved_at=None, cleanup={'action': 'inspect', 'reason': 'Timeout; сначала найти запись по namespace/case_id'})
        self.assertTrue(any('pending write cannot' in e for e in self.errors()))
        self.block_kafka()
        self.assertEqual([], self.errors())

    def test_definition_change_requires_new_readback_not_only_a_new_hash(self):
        self.profile['stubs'][0]['definition']['response']['status'] = 500
        self.assertTrue(any('profile hash differs' in e for e in self.errors(update_binding=False)))
        self.assignment['cases'][0]['definition']['response']['status'] = 500
        self.assertTrue(any('readback differs' in e for e in self.errors()))

    def test_wrong_rest_response_or_negative_match_cannot_be_ready(self):
        self.change_observation(0, 'positive', lambda r: r['actual']['body'].update(decision='declined'))
        self.assertTrue(any('positive: probe has not passed' in e for e in self.errors()))
        self.change_observation(0, 'negative', lambda r: r['actual'].update(matched=True, status=200))
        self.assertTrue(any('negative: probe has not passed' in e for e in self.errors()))

    def test_kafka_topic_correlation_and_duplicate_messages_are_checked(self):
        original = json.loads((self.base / self.proof['checks'][1]['positive']['artifact']).read_text())
        for change in (
            lambda r: r['actual']['messages'][0].update(topic='wrong-topic'),
            lambda r: r['actual']['messages'][0]['value'].update(correlation_id='old-correlation'),
            lambda r: r['actual']['messages'].append(copy.deepcopy(r['actual']['messages'][0])),
        ):
            with self.subTest(change=change):
                self.change_observation(1, 'positive', lambda r: (r.clear(), r.update(copy.deepcopy(original)), change(r)))
                self.assertTrue(any('positive: probe has not passed' in e for e in self.errors()))

    def test_other_project_namespace_or_stub_observation_cannot_be_reused(self):
        for field in ('project_id', 'environment_id', 'namespace', 'stub_id', 'kind'):
            with self.subTest(field=field):
                ref = self.proof['checks'][0]['readback']
                original = (self.base / ref['artifact']).read_bytes()
                self.change_observation(0, 'readback', lambda r: r.update({field: 'other'}))
                self.assertTrue(self.errors())
                (self.base / ref['artifact']).write_bytes(original)
                ref['sha256'] = hashlib.sha256(original).hexdigest()

    def test_missing_control_corrupted_proof_and_scalar_type_are_rejected(self):
        negative = self.proof['checks'][0].pop('negative')
        self.assertTrue(any('negative: needs artifact' in e for e in self.errors()))
        self.proof['checks'][0]['negative'] = negative
        path = self.base / negative['artifact']
        path.write_text(path.read_text() + '\n')
        self.assertTrue(any('artifact hash differs' in e for e in self.errors()))
        negative['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.change_observation(0, 'negative', lambda r: r['actual'].update(matched=0))
        self.assertTrue(any('negative: probe has not passed' in e for e in self.errors()))

    def test_cleanup_does_not_delete_reused_or_unrestorable_updated_stubs(self):
        stub = self.profile['stubs'][0]
        stub['operation'] = 'reused'
        self.assertTrue(any('cleanup must match' in e for e in self.errors()))
        stub['cleanup'] = {'action': 'none', 'reason': 'Чужая подходящая запись; не удалять'}
        self.assertEqual([], self.errors())
        stub['operation'] = 'updated'
        self.assignment['cases'][0].update(allowed_operations=['updated'], target_stub_id=stub['stub_id'])
        stub['cleanup'] = {'action': 'restore', 'reason': 'Разрешённое обновление с восстановлением снимка'}
        self.assertTrue(any('previous definition' in e for e in self.errors()))
        stub['cleanup']['previous_definition'] = {'response': {'status': 503}}
        before = json.loads((self.base / self.proof['checks'][0]['readback']['artifact']).read_text())
        before.update(observed_at='2026-10-07T11:59:10+03:00', definition=stub['cleanup']['previous_definition'])
        path = self.base / 'observations/rest-before.json'
        path.write_text(json.dumps(before))
        stub['cleanup']['before'] = {'artifact':'observations/rest-before.json', 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        self.assertEqual([], self.errors())

    def test_partial_cannot_hide_assigned_scenarios_or_missing_stub_checks(self):
        self.message['payload']['scenario_ids'] = ['TC-REST']
        self.assertTrue(any('differ from assignment' in e for e in self.errors()))
        self.message['payload']['scenario_ids'] = self.assigned.copy()
        self.proof['checks'].pop()
        self.assertTrue(any('verification cases differ' in e for e in self.errors()))

    def test_cli_requires_full_assignment_and_resolves_artifacts_from_result(self):
        args = [sys.executable, str(ROOT / 'scripts/validate_qa_result.py'), '--input', str(self.base / 'result.json'),
                '--task-id', 'QA-STUB-DEMO', '--request-id', 'QA-STUB-DEMO-1', '--agent', 'qa-stub-builder',
                '--stub-assignment', str(self.base / 'assignment.json')]
        result = subprocess.run(args, cwd=self.temp.name, text=True, capture_output=True)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn('--scenario-id', result.stdout)
        result = subprocess.run(args + ['--scenario-id', 'TC-REST'], cwd=self.temp.name, text=True, capture_output=True)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        result = subprocess.run(args + ['--scenario-id', 'TC-REST', '--scenario-id', 'TC-KAFKA'],
                                cwd=self.temp.name, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_probe_cannot_redefine_expected_to_match_a_wrong_actual_response(self):
        self.change_observation(0, 'positive', lambda r: (r['actual']['body'].update(decision='declined'), r['expected']['body'].update(decision='declined')))
        errors = self.errors()
        self.assertTrue(any('assigned input/expected' in e for e in errors))
        self.assertTrue(any('probe has not passed' in e for e in errors))

    def test_behavioral_matcher_cannot_gain_unplanned_nested_conditions(self):
        self.change_observation(0, 'readback', lambda r: r['definition']['request']['body'].update(must_be_admin=True))
        self.assertTrue(any('readback differs' in e for e in self.errors()))

    def test_old_probes_and_probes_before_current_readback_are_rejected(self):
        self.change_observation(0, 'positive', lambda r: r.update(observed_at='1999-01-01T00:00:00+00:00'))
        self.assertTrue(any('predates' in e for e in self.errors()))
        self.change_observation(0, 'positive', lambda r: r.update(observed_at='2026-10-07T11:59:50+03:00'))
        self.assertTrue(any('predates' in e for e in self.errors()))

    def test_malformed_operation_returns_errors_instead_of_crashing(self):
        for value in ([], {}, None, 1):
            with self.subTest(value=value):
                self.profile['stubs'][0]['operation'] = value
                self.assertTrue(any('unknown operation' in e for e in self.errors()))

    def test_configure_only_completes_without_probes_or_execution_readiness(self):
        self.assignment['mode'] = 'configure_only'
        self.message['event'] = 'stubs_configured'
        self.message['payload']['configured_scenarios'] = self.message['payload'].pop('scenario_ids')
        for check in self.proof['checks']:
            check['status'] = 'configured'
            check['reason'] = 'Назначение только на настройку; runtime-пробы не запускались'
            check.pop('positive')
            check.pop('negative')
        self.assertEqual([], self.errors())
        self.message['payload']['scenario_ids'] = ['TC-REST']
        self.assertTrue(any('execution-ready' in e for e in self.errors()))
        self.message['payload'].pop('scenario_ids')
        self.proof['checks'][0]['status'] = 'verified'
        self.assertTrue(any('configure_only' in e for e in self.errors()))
        self.proof['checks'][0]['status'] = 'blocked'
        self.proof['checks'][0]['positive'] = {'artifact': 'unexpected-probe.json', 'sha256': 'unused'}
        self.message['payload']['configured_scenarios'] = ['TC-KAFKA']
        self.message['payload']['blocked_scenarios'] = ['TC-REST']
        self.assertTrue(any('configure_only' in e for e in self.errors()))

    def test_kafka_absence_needs_real_interval_cursor_and_correlation(self):
        original = json.loads((self.base / self.proof['checks'][1]['negative']['artifact']).read_text())
        for change in (
            lambda r: r['capture'].update(ended_at='2026-10-07T12:00:00.500+03:00'),
            lambda r: r['capture'].update(correlation_id='old'),
            lambda r: r['capture'].update(position={}),
            lambda r: r['capture'].update(started_at='2026-10-07T11:59:59+03:00'),
        ):
            with self.subTest(change=change):
                self.change_observation(1, 'negative', lambda r: (r.clear(), r.update(copy.deepcopy(original)), change(r)))
                self.assertTrue(any('Kafka' in e for e in self.errors()))

    def test_scope_alias_and_explicit_update_authorization(self):
        stub = self.profile['stubs'][0]
        stub['operation'] = 'updated'
        self.assertTrue(any('outside the assigned scope' in e for e in self.errors()))
        self.assertTrue(any('explicitly assigned target' in e for e in self.errors()))
        stub['operation'] = 'created'
        self.assignment['mcp_server'] = 'sbermock-local-alias'
        self.profile['mcp_server'] = self.proof['mcp_server'] = self.message['payload']['mcp_server'] = 'sbermock-local-alias'
        for check in self.proof['checks']:
            for key in ('readback','positive','negative'):
                index = self.proof['checks'].index(check)
                self.change_observation(index, key, lambda r: r.update(mcp_server='sbermock-local-alias'))
        self.assertEqual([], self.errors())
        self.message['payload']['project_id'] = 'other-project'
        self.assertTrue(any('coordinator assignment' in e for e in self.errors()))

    def test_assignment_is_required_and_hash_binds_approved_probe_plan(self):
        self.assertTrue(any('assignment is required' in e for e in validate_stub_bundle(self.message, self.base)))
        self.assignment['cases'][0]['probes']['positive']['expected']['status'] = 503
        self.assertTrue(any('assignment hash differs' in e for e in validate_stub_bundle(self.message, self.base, self.assignment)))

    def test_empty_object_does_not_allow_arbitrary_response_body(self):
        self.assignment['cases'][0]['probes']['positive']['expected'] = {'body':{}}
        self.profile['stubs'][0]['probes'] = copy.deepcopy(self.assignment['cases'][0]['probes'])
        self.change_observation(0, 'positive', lambda r: r.update(expected={'body':{}}))
        self.assertTrue(any('probe has not passed' in e for e in self.errors()))

    def test_ambiguous_json_artifacts_are_rejected(self):
        path = self.base / 'invalid.json'
        for value in ('{"operation":"created","operation":"updated"}', '{"value":NaN}', '{"value":1e999}'):
            path.write_text(value)
            with self.assertRaises(ValueError):
                read_stub_json(path)

    def test_earlier_case_probes_cannot_survive_a_later_mutation_without_readback(self):
        self.profile['stubs'][1]['resolved_at']='2026-10-07T12:00:00.300+03:00'
        self.assertTrue(any('all resolved mutations' in e for e in self.errors()))
        self.profile['verification_started_at']='2026-10-07T12:00:00.300+03:00'
        self.assertTrue(any('predates' in e for e in self.errors()))

    def test_discovery_plan_is_reviewable_without_authorizing_mutations(self):
        payload = {k:self.assignment[k] for k in ('environment_id','provider','mcp_server','project_id','namespace','scenario_ids')}
        payload.update(assignment_artifact='assignment.json', limitations=['Учебный черновик, действий в MCP не было'])
        plan = dict(self.message, event='stubs_plan_ready', payload=payload, artifacts=['assignment.json'])
        self.assertEqual([], validate_message(plan, 'QA-STUB-DEMO', 'QA-STUB-DEMO-1', 'qa-stub-builder', self.assigned))
        self.assertEqual([], validate_stub_plan(plan, self.base))
        path=self.base/'plan-result.json'; path.write_text(json.dumps(plan))
        command=[sys.executable,str(ROOT/'scripts/validate_qa_result.py'),'--input',str(path),'--task-id','QA-STUB-DEMO',
                 '--request-id','QA-STUB-DEMO-1','--agent','qa-stub-builder','--scenario-id','TC-REST','--scenario-id','TC-KAFKA']
        result=subprocess.run(command,cwd=self.temp.name,text=True,capture_output=True)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertIn('No readiness',result.stdout)
        preflight=[sys.executable,str(ROOT/'scripts/validate_stub_assignment.py'),'--input',str(self.base/'assignment.json'),
                   '--task-id','QA-STUB-DEMO','--request-id','QA-STUB-DEMO-1','--scenario-id','TC-REST','--scenario-id','TC-KAFKA']
        result=subprocess.run(preflight,cwd=self.temp.name,text=True,capture_output=True)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        result=subprocess.run(preflight[:-2],cwd=self.temp.name,text=True,capture_output=True)
        self.assertEqual(2,result.returncode,result.stdout+result.stderr)

    def test_unknown_scope_and_reused_kafka_correlation_are_rejected(self):
        self.assignment['project_id']='unknown'
        self.assertTrue(any('must be resolved' in e for e in self.errors()))
        self.assignment['project_id']='fixture-project'
        plans=self.assignment['cases'][1]['probes']
        plans['negative']['correlation_id']=plans['positive']['correlation_id']
        self.assertTrue(any('correlations must differ' in e for e in self.errors()))

    def test_future_or_corrupted_restore_snapshot_is_not_accepted(self):
        stub=self.profile['stubs'][0]
        self.assignment['cases'][0].update(allowed_operations=['updated'],target_stub_id=stub['stub_id'])
        stub['operation']='updated'
        before=json.loads((self.base/self.proof['checks'][0]['readback']['artifact']).read_text())
        before['definition']={'response':{'status':503}}
        path=self.base/'observations/rest-before.json'; path.write_text(json.dumps(before))
        stub['cleanup']={'action':'restore','reason':'Снимок до изменения','previous_definition':before['definition'],
                         'before':{'artifact':'observations/rest-before.json','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
        self.assertTrue(any('must precede mutation' in e for e in self.errors()))
        path.write_text('{}')
        self.assertTrue(any('artifact hash differs' in e for e in self.errors()))

    def test_profile_hash_must_describe_the_same_bytes_that_were_parsed(self):
        self.save()
        path=self.base/'profile.json'
        original=path.read_bytes()
        changed=json.loads(original)
        changed['stubs'][0]['definition']['response']['status']=503
        altered=json.dumps(changed).encode()
        self.proof['profile_sha256']=hashlib.sha256(altered).hexdigest()
        (self.base/'verification.json').write_text(json.dumps(self.proof))
        raw_read=Path.read_bytes
        reads=[]
        def racing_read(current):
            if current == path:
                reads.append(current)
                return original if len(reads)==1 else altered
            return raw_read(current)
        with patch.object(Path,'read_bytes',racing_read):
            errors=validate_stub_bundle(self.message,self.base,self.assignment)
        self.assertTrue(any('profile hash differs' in e for e in errors))
        self.assertEqual(1,len(reads))


if __name__ == '__main__':
    unittest.main()
