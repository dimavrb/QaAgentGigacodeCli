"""Local synthetic protocol and fake gateway effects; no Jira/model evaluation."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import context_builder
import jira_publication as jira
import task_exchange as exchange
import execution_supervisor as supervisor
from self_review_fixtures import review_for, publish_content_review

spec = importlib.util.spec_from_file_location('jira_demo', ROOT / 'examples/jira-publication/build_demo.py')
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class JiraPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root, self.client, self.gateway = demo.build(Path(self.temp.name) / 'demo')
        self.state = self.root / 'task.json'
        self.scope_path = self.root / 'jira-assignment.json'
        self.assignment = exchange.read_json(self.root / 'assignment.json')
        self.message = exchange.read_json(self.root / 'jira-result.json')

    def check(self, message=None):
        return jira.check_bundle(message or self.message, self.root, self.scope_path, self.state,
                                 self.assignment['result_path'])

    def submit(self, message=None, assignment=None):
        target = assignment or self.assignment
        exchange.assign(self.state, target, self.root, None)
        exchange.prepare_handoff(self.state, target['request_id'], None)
        exchange.atomic_write(target['result_path'], message or self.message)
        exchange.submit_result(self.state, target['request_id'], None)

    def accept(self):
        self.submit()
        exchange.review_result(self.state, 'jira-1', 'Synthetic publication read', True, None)

    def consumer(self, *, mode='ready'):
        return dict(task_id='QA-FIXTURE', request_id='consumer', agent='qa-analyst', stage='consumer',
            goal='Read publication', acceptance_criteria=['Source verified'], stop_condition='QA JSON',
            expected_artifact='JSON', result_path=str(self.root / 'consumer.json'), inputs=[],
            depends_on=['jira-1'], dependency_modes={'jira-1': mode})

    def mutate(self, name, callback):
        path = self.root / name
        value = exchange.read_json(path)
        callback(value)
        exchange.atomic_write(path, value)

    def test_created_receipt_readback_accepted_source_and_nested_hashes(self):
        paths = self.check()
        for name in ('publication.json', 'search.json', 'readback.json', 'gateway.json'):
            self.assertIn(str(self.root / name), paths)
        self.assertTrue(any('.receipts/' in p for p in paths))
        self.accept()
        row = exchange.read_state(self.state)['requests']['jira-1']
        self.assertIn(str(self.root / 'policy.json'), row['input_hashes'])
        self.assertIn(str(self.root / 'synthetic-skill.md'), row['input_hashes'])
        self.assertIn(str(self.root / 'readback.json'), row['result']['artifact_hashes'])
        exchange.assign(self.state, self.consumer(), self.root, None)
        self.assertEqual(1, self.client.writes)

    def test_duplicate_requires_read_same_defect_candidate_and_no_write(self):
        root, client, _ = demo.build(Path(self.temp.name) / 'duplicate', duplicate=True)
        message = exchange.read_json(root / 'jira-result.json')
        paths = jira.check_bundle(message, root, root / 'jira-assignment.json', root / 'task.json', root / 'jira-result.json')
        self.assertIn(str(root / 'candidate.json'), paths)
        self.assertEqual(0, client.writes)
        assignment = exchange.read_json(root / 'assignment.json')
        exchange.assign(root / 'task.json', assignment, root, None)
        exchange.prepare_handoff(root / 'task.json', 'jira-1', None)
        exchange.submit_result(root / 'task.json', 'jira-1', None)
        exchange.review_result(root / 'task.json', 'jira-1', 'Same synthetic defect read', True, None)

    def test_unaccepted_or_nonconfirmed_investigation_cannot_open_assignment(self):
        state = exchange.read_state(self.state)
        state['requests']['QA-FIXTURE-1']['status'] = 'reported'
        exchange.atomic_write(self.state, state)
        with self.assertRaisesRegex(ValueError, 'принят'):
            exchange.assign(self.state, self.assignment, self.root, None)
        state['requests']['QA-FIXTURE-1']['status'] = 'accepted'
        scope = exchange.read_json(self.scope_path)
        result_path = self.root / 'investigation/result.json'
        original = exchange.read_json(result_path)
        for outcome in ('expected_behavior', 'not_reproduced', 'inconclusive'):
            message = copy.deepcopy(original)
            message['payload']['outcome'] = outcome
            exchange.atomic_write(result_path, message)
            scope['investigation_sha256'] = jira.sha(result_path)
            with self.subTest(outcome=outcome), self.assertRaisesRegex(ValueError, 'only defect_confirmed'):
                jira.check_source(state, self.assignment, scope)

    def test_source_must_be_explicit_ready_dependency_and_state_backed(self):
        for mode in ('missing', 'context', 'stateless'):
            assignment = copy.deepcopy(self.assignment)
            state = exchange.read_state(self.state)
            if mode == 'missing': assignment['depends_on'] = []
            elif mode == 'context': assignment['dependency_modes'] = {'QA-FIXTURE-1': 'context'}
            else: state['requests']['QA-FIXTURE-1']['assignment'].pop('investigation_state')
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                jira.check_source(state, assignment, exchange.read_json(self.scope_path))

    def test_changed_investigation_or_late_blocking_review_prevents_publication(self):
        publish_content_review(self.state, 'QA-FIXTURE-1')
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, 'blocking'):
            exchange.assign(self.state, self.assignment, self.root, None)
        self.assertEqual(before, self.state.read_bytes())

    def test_incomplete_unknown_unread_duplicate_candidate_cannot_pass(self):
        original = (self.root / 'search.json').read_bytes()
        for mutation in ('incomplete', 'unknown', 'unread'):
            search = exchange.read_json(self.root / 'search.json')
            if mutation == 'incomplete': search['complete'] = False
            else:
                search['candidates'] = [dict(issue_key='FIXTURE-8', issue_url='https://jira.example.invalid/browse/FIXTURE-8',
                    assessment='unknown' if mutation == 'unknown' else 'different', reason='Synthetic candidate',
                    readback_artifact='missing.json')]
            exchange.atomic_write(self.root / 'search.json', search)
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, OSError)): self.check()
            (self.root / 'search.json').write_bytes(original)

    def test_matching_duplicate_forbids_new_creation(self):
        self.mutate('search.json', lambda row: row.update(candidates=[dict(issue_key='FIXTURE-1',
            issue_url=self.message['payload']['issue_url'], assessment='same_defect', reason='Same synthetic symptom',
            readback_artifact='readback.json')]))
        with self.assertRaisesRegex(ValueError, 'prohibits creation'): self.check()

    def test_candidate_read_after_creation_cannot_retroactively_complete_duplicate_check(self):
        candidate = exchange.read_json(self.root / 'readback.json')
        candidate.update(issue_key='FIXTURE-8', issue_url='https://jira.example.invalid/browse/FIXTURE-8')
        candidate['fields']['summary'] = 'Different synthetic symptom'
        exchange.atomic_write(self.root / 'late-candidate.json', candidate)
        self.mutate('search.json', lambda row: row.update(candidates=[dict(issue_key=candidate['issue_key'],
            issue_url=candidate['issue_url'], assessment='different', reason='Different synthetic symptom',
            readback_artifact='late-candidate.json')]))
        with self.assertRaisesRegex(ValueError, 'candidate reads must precede creation'):
            self.check()

    def test_duplicate_event_cannot_hide_a_creation_attempt(self):
        message = copy.deepcopy(self.message)
        message['event'] = 'jira_duplicate_found'
        with self.assertRaisesRegex(ValueError, 'must not hide'): self.check(message)

    def test_created_requires_readback_exact_fields_and_matching_identity(self):
        original = (self.root / 'readback.json').read_bytes()
        for mutation in ('summary', 'description', 'project', 'url', 'key', 'missing'):
            row = exchange.read_json(self.root / 'readback.json')
            if mutation in ('summary', 'description'): row['fields'][mutation] = 'Different'
            elif mutation == 'project': row['fields']['project_key'] = 'OTHER'
            elif mutation == 'url': row['issue_url'] = 'https://other.invalid/browse/FIXTURE-1'
            elif mutation == 'key': row['issue_key'] = 'FIXTURE-2'
            if mutation == 'missing': (self.root / 'readback.json').unlink()
            else: exchange.atomic_write(self.root / 'readback.json', row)
            with self.subTest(mutation=mutation), self.assertRaises((ValueError, OSError)): self.check()
            (self.root / 'readback.json').write_bytes(original)

    def test_report_url_project_key_and_assignment_hash_must_match(self):
        for key, value in (('issue_key', 'OTHER-1'), ('issue_url', 'https://jira.example.invalid/browse/FIXTURE-2'),
                           ('jira_assignment_sha256', '0' * 64)):
            message = copy.deepcopy(self.message)
            message['payload'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): self.check(message)

    def test_observations_are_fresh_and_search_precedes_creation(self):
        original = (self.root / 'search.json').read_bytes()
        now = datetime.now(timezone.utc)
        for stamp in ((now - timedelta(days=1)).isoformat(), (now + timedelta(days=1)).isoformat(),
                      '2026-10-15T12:00:00', 'invalid', (now + timedelta(microseconds=1)).isoformat()):
            self.mutate('search.json', lambda row: row.update(observed_at=stamp))
            with self.subTest(stamp=stamp), self.assertRaises(ValueError): self.check()
            (self.root / 'search.json').write_bytes(original)

    def test_receipt_and_saved_action_arguments_cannot_be_changed(self):
        state = exchange.read_json(self.root / 'gateway.json')
        action = next(iter(state['actions'].values()))
        receipt = Path(action['receipt_path'])
        original = receipt.read_bytes()
        receipt.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'receipt changed'): self.check()
        receipt.write_bytes(original)
        action['arguments']['project'] = 'OTHER'
        exchange.atomic_write(self.root / 'gateway.json', state)
        with self.assertRaisesRegex(ValueError, 'arguments differ'): self.check()

    def test_timeout_blocks_repeat_then_reconcile_reuses_one_creation(self):
        root, client, gateway = demo.build(Path(self.temp.name) / 'unknown', uncertain=True)
        message = exchange.read_json(root / 'jira-result.json')
        def check():
            return jira.check_bundle(message, root, root / 'jira-assignment.json', root / 'task.json', root / 'jira-result.json')
        with self.assertRaisesRegex(ValueError, 'unresolved'): check()
        scope = exchange.read_json(root / 'jira-assignment.json')
        arguments = scope['gateway']['create_arguments']
        with self.assertRaisesRegex(ValueError, 'Uncertain external action'):
            gateway.call('create_issue', arguments)
        self.assertEqual(1, client.writes)
        aid = next(iter(exchange.read_json(root / 'gateway.json')['actions']))
        self.assertEqual('verified', gateway.reconcile(aid)['status'])
        check()
        gateway.call('create_issue', arguments)
        self.assertEqual(1, client.writes)

    def test_non_authoritative_absence_never_allows_second_create(self):
        root, client, gateway = demo.build(Path(self.temp.name) / 'absent', uncertain=True)
        original_request = client.request
        def response(method, params):
            if params['name'] == 'read_issue':
                return dict(structuredContent=dict(present=False))
            return original_request(method, params)
        client.request = response
        aid = next(iter(exchange.read_json(root / 'gateway.json')['actions']))
        self.assertEqual('unknown', gateway.reconcile(aid)['status'])
        arguments = exchange.read_json(root / 'jira-assignment.json')['gateway']['create_arguments']
        with self.assertRaisesRegex(ValueError, 'Uncertain external action'):
            gateway.call('create_issue', arguments)
        self.assertEqual(1, client.writes)

    def test_new_request_cannot_reset_gateway_for_same_defect(self):
        message = copy.deepcopy(self.message)
        message.update(event='blocked', payload={}, blockers=['Synthetic unknown outcome'])
        self.submit(message)
        exchange.review_result(self.state, 'jira-1', 'Synthetic blocker read', True, None)
        scope = exchange.read_json(self.scope_path)
        scope['gateway']['state_path'] = str(self.root / 'another-gateway.json')
        exchange.atomic_write(self.root / 'another-scope.json', scope)
        assignment = dict(self.assignment, request_id='jira-2', result_path=str(self.root / 'jira-2.json'),
                          jira_assignment=str(self.root / 'another-scope.json'))
        with self.assertRaisesRegex(ValueError, 'reuse its durable gateway'):
            exchange.assign(self.state, assignment, self.root, None)
        # Changing the old scope to the new journal cannot reset the operation.
        exchange.atomic_write(self.scope_path, scope)
        with self.assertRaisesRegex(ValueError, 'изменились'):
            exchange.assign(self.state, assignment, self.root, None)

    def test_changed_nested_evidence_blocks_ready_transition_and_finish(self):
        self.accept()
        self.mutate('readback.json', lambda row: row['fields'].update(summary='Changed'))
        before = self.state.read_bytes()
        with self.assertRaises(ValueError): exchange.assign(self.state, self.consumer(), self.root, None)
        with self.assertRaises(ValueError): exchange.finish_task(self.state, 'passed', 'Changed proof', None)
        self.assertEqual(before, self.state.read_bytes())

    def test_primary_evidence_cannot_be_replaced_by_unrelated_file(self):
        message = copy.deepcopy(self.message)
        message['payload']['evidence'] = [str(self.root / 'readback.json')]
        with self.assertRaisesRegex(ValueError, 'evidence differs'): self.check(message)

    def test_new_assignment_cannot_disable_guard_and_missing_scope_allows_blocked(self):
        assignment = dict(self.assignment, jira_checks_required=False)
        assignment.pop('jira_assignment')
        exchange.assign(self.state, assignment, self.root, None)
        row = exchange.read_state(self.state)['requests']['jira-1']
        self.assertTrue(row['assignment']['jira_checks_required'])
        self.assertEqual(str(self.state), row['assignment']['jira_task_state'])
        exchange.prepare_handoff(self.state, 'jira-1', None)
        with self.assertRaisesRegex(ValueError, 'coordinator jira_assignment'):
            exchange.submit_result(self.state, 'jira-1', None)
        message = dict(self.message, event='blocked', payload={}, blockers=['Skill or Jira configuration unavailable'])
        exchange.atomic_write(self.assignment['result_path'], message)
        exchange.submit_result(self.state, 'jira-1', None)
        exchange.review_result(self.state, 'jira-1', 'Missing configuration read', True, None)
        with self.assertRaises(ValueError): exchange.finish_task(self.state, 'passed', 'Still blocked', None)

    def test_legacy_jira_is_context_only_and_cannot_finish(self):
        assignment = dict(self.assignment)
        assignment.pop('jira_assignment')
        exchange.assign(self.state, assignment, self.root, None)
        state = exchange.read_state(self.state)
        state['requests']['jira-1']['assignment'].pop('jira_checks_required')
        exchange.atomic_write(self.state, state)
        message = copy.deepcopy(self.message)
        message['payload'].pop('publication_artifact')
        message['payload'].pop('jira_assignment_sha256')
        exchange.atomic_write(self.assignment['result_path'], message)
        exchange.prepare_handoff(self.state, 'jira-1', None)
        exchange.submit_result(self.state, 'jira-1', None)
        exchange.review_result(self.state, 'jira-1', 'Historical structural audit', True, None)
        with self.assertRaisesRegex(ValueError, 'Публикация Jira'):
            exchange.assign(self.state, self.consumer(), self.root, None)
        with self.assertRaisesRegex(ValueError, 'Публикация Jira'):
            exchange.finish_task(self.state, 'passed', 'Legacy cannot close publication', None)
        state = exchange.read_state(self.state)
        state['requests']['jira-1']['assignment']['criterion_ids'] = ['C-JIRA']
        with self.assertRaisesRegex(ValueError, 'Публикация Jira'):
            exchange.check_assessment(state, dict(criterion_id='C-JIRA', description='Verified publication', required=True),
                dict(criterion_id='C-JIRA', status='verified', decision='Legacy is insufficient',
                     request_ids=['jira-1'], evidence=[self.assignment['result_path']]))
        execution = self.root / 'execution.json'
        supervisor.initialize(execution, dict(schema_version=1, task_state=str(self.state), workspace=str(self.root),
            scope_kind='synthetic', adapter=dict(kind='command', argv=[sys.executable, '-c', 'pass']),
            steps=[dict(step_id='jira', assignment=assignment)]), self.root)
        def terminal(value):
            value['steps']['jira'].update(status='awaiting_review', attempts=[dict(
                status='finished', request_id='jira-1', category=None)])
        supervisor.update(execution, terminal)
        with self.assertRaisesRegex(ValueError, 'Публикация Jira'):
            supervisor.tick(execution)
        def checkpoint(value):
            value['steps']['jira']['checkpoint'] = dict(request_id='jira-1',
                artifact_hashes=state['requests']['jira-1']['result']['artifact_hashes'])
        supervisor.update(execution, checkpoint)
        with self.assertRaisesRegex(ValueError, 'Публикация Jira'):
            supervisor.tick(execution)
        exchange.assign(self.state, self.consumer(mode='context'), self.root, None)

    def test_scope_must_bind_exact_source_and_cannot_be_used_by_another_role(self):
        scope = exchange.read_json(self.scope_path)
        scope['investigation_sha256'] = '0' * 64
        exchange.atomic_write(self.scope_path, scope)
        with self.assertRaisesRegex(ValueError, 'revision differs'):
            exchange.assign(self.state, self.assignment, self.root, None)
        with self.assertRaisesRegex(ValueError, 'requires qa-jira-defect-creator'):
            exchange.assign(self.state, dict(self.assignment, agent='qa-analyst'), self.root, None)

    def test_scope_skill_policy_and_source_hash_are_frozen(self):
        exchange.assign(self.state, self.assignment, self.root, None)
        (self.root / 'synthetic-skill.md').write_text('Changed skill')
        with self.assertRaisesRegex(ValueError, 'изменились'):
            exchange.prepare_handoff(self.state, 'jira-1', None)

    def test_scope_limits_and_urls_are_validated(self):
        original = self.scope_path.read_bytes()
        for field, value in (('schema_version', True), ('jira_base_url', 'https://user:password@jira.example.invalid'),
                             ('project_key', 'other'), ('investigation_sha256', 'bad')):
            self.mutate('jira-assignment.json', lambda row: row.update({field: value}))
            with self.subTest(field=field), self.assertRaises(ValueError): jira.load_assignment(self.scope_path)
            self.scope_path.write_bytes(original)
        self.mutate('policy.json', lambda row: row.update(max_action_attempts=2))
        with self.assertRaisesRegex(ValueError, 'one creation attempt'): jira.load_assignment(self.scope_path)

    def test_bare_jira_and_publication_reviewer_receive_contract(self):
        for assignment in (self.assignment, dict(agent='qa-result-reviewer', review_target=dict(scope='jira_publication'))):
            docs = context_builder.profile(assignment, {})['documents']
            self.assertIn('docs/JIRA_PUBLICATION.md', docs)
            self.assertIn('docs/ACTION_GATEWAY.md', docs)

    def test_cli_and_documented_commands_reach_accepted_publication(self):
        cli = [sys.executable, str(ROOT / 'scripts/validate_qa_result.py'), '--input', self.assignment['result_path'],
            '--task-id', 'QA-FIXTURE', '--request-id', 'jira-1', '--agent', 'qa-jira-defect-creator', '--require-jira-checks']
        missing = subprocess.run(cli, capture_output=True, text=True)
        self.assertEqual(2, missing.returncode)
        valid = subprocess.run(cli + ['--jira-assignment', str(self.scope_path), '--jira-task-state', str(self.state)], capture_output=True, text=True)
        self.assertEqual(0, valid.returncode, valid.stdout + valid.stderr)
        for args in (['assign', '--input', str(self.root / 'assignment.json'), '--expected-revision', '5'],
                     ['prepare', '--request-id', 'jira-1', '--expected-revision', '6'],
                     ['submit', '--request-id', 'jira-1', '--expected-revision', '7'],
                     ['review', '--request-id', 'jira-1', '--decision', 'Synthetic receipts read', '--expected-revision', '8']):
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/manage_task.py'), '--state', str(self.state), *args], capture_output=True, text=True)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        exchange.finish_task(self.state, 'passed', 'Synthetic Jira publication protocol completed', 9)
        self.assertEqual('completed', exchange.read_state(self.state)['execution_status'])


if __name__ == '__main__':
    unittest.main()
