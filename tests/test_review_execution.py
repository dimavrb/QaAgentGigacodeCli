"""Actual scoped MCP launches, exact-target receipts and runtime acceptance gates."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tests')]
import task_exchange as exchange
import execution_supervisor as supervisor
import execution_worker
import review_cycle
from action_gateway import ActionGateway, MCPClient
from content_review import validate_review
from review_execution import check_receipt, context_for, covers
from task_acceptance import criteria_packet


def execution_specs():
    return [dict(check_id=mode, tool='run_review_test', run_started_pointer='/structuredContent/started_epoch',
        expected_arguments={'/project': 'SYNTHETIC', '/mode': mode},
        expected_result={'/structuredContent/exit_code': 1 if mode == 'counterexample' else 0,
                         '/structuredContent/assertion_failed': mode == 'counterexample'})
            for mode in ('normal', 'counterexample', 'restored')]


class RuntimeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.registry, self.cycle = self.root/'task.json', self.root/'cycle.json'
        self.definition = dict(criterion_id='C', description='Assertion detects foreign rows and normal behavior is restored',
            source='Synthetic own paid order requirement', required=True,
            required_reviews=[dict(scope='runtime', agent='qa-autotest-reviewer', execution=execution_specs())])
        exchange.init_task(self.registry, 'T', 'Synthetic independent runtime review', dict(schema_version=1, criteria=[self.definition]))
        self.addCleanup(self.stop)

    def stop(self):
        if self.cycle.exists():
            try:
                review_cycle.cancel(self.cycle, 'Synthetic cleanup')
            except Exception:
                pass

    def policy(self, server_extra=()):
        return dict(schema_version=1, server_command=[sys.executable, str(ROOT/'tests/fixtures/review_probe_server.py'), *server_extra],
            call_timeout_seconds=2, max_review_calls=3, tools={'run_review_test': dict(effect='read',
                constraints={'/project': 'SYNTHETIC'}, allowed_arguments=['project', 'mode'])})

    def plan(self, author='strong', reviewer_extra=(), server_extra=()):
        assignment = dict(task_id='T', request_id='author', agent='qa-test-automation', stage='automation',
            goal='Synthetic exact composition assertion', acceptance_criteria=['Detect the controlled foreign row'],
            criterion_ids=['C'], scenario_ids=['TC-1'], stop_condition='Return QA JSON', expected_artifact='QA JSON',
            result_path=str(self.root/'author.json'), inputs=[], depends_on=[])
        return dict(schema_version=1, author_plan=dict(schema_version=1, task_state=str(self.registry), workspace=str(self.root),
            scope_kind='synthetic', adapter=dict(kind='command', argv=[sys.executable, str(ROOT/'tests/fixtures/review_agent.py'), '--mode', author]),
            steps=[dict(step_id='author', assignment=assignment, max_wall_seconds=5)]),
            reviewers=[dict(scope='runtime', agent='qa-autotest-reviewer', max_wall_seconds=8,
                adapter=dict(kind='command', argv=[sys.executable, str(ROOT/'tests/fixtures/runtime_reviewer.py'), *reviewer_extra]),
                gateway_policy=self.policy(server_extra))], max_active_seconds=30)

    def run_cycle(self, **kwargs):
        review_cycle.initialize(self.cycle, self.plan(**kwargs), self.root)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            result = review_cycle.tick(self.cycle)
            if result['status'] in ('accepted', 'blocked'):
                return result
            time.sleep(.05)
        self.fail('Runtime review cycle timeout: ' + json.dumps(review_cycle.read(self.cycle)))

    def assess(self, result):
        exchange.assess_criteria(self.registry, dict(assessments=[dict(criterion_id='C', status='verified',
            decision='Independent fresh observations match the agreed normal/fault/restored expectations',
            request_ids=[result['result_request_id'], *result['review_request_ids']],
            evidence=[str(Path(exchange.read_state(self.registry)['requests'][result['result_request_id']]['result']['path']))])]), None)

    def accepted(self):
        result = self.run_cycle()
        self.assertEqual('accepted', result['status'], result.get('reason'))
        return result

    def reviewer(self, result):
        return exchange.read_state(self.registry)['requests'][result['review_request_ids'][0]]

    def test_conclusion_reviewer_executes_with_its_own_policy_and_receipts(self):
        facts=self.root/'primary-facts.txt'
        facts.write_text('Synthetic local review fixture records describe controlled tests only; no live product/model evidence.\n')
        definition=copy.deepcopy(self.definition)
        definition['required_reviews'].append(dict(scope='conclusions',agent='qa-conclusion-reviewer',
            conclusions=['/summary'],execution=execution_specs()))
        exchange.set_criteria(self.registry,dict(schema_version=1,criteria=[definition]),None)
        plan=self.plan()
        plan['author_plan']['steps'][0]['assignment']['inputs']=[str(facts)]
        slot=copy.deepcopy(plan['reviewers'][0])
        slot.update(scope='conclusions',agent='qa-conclusion-reviewer',review_sources=[str(facts)])
        plan['reviewers'].append(slot)
        with patch.object(self,'plan',return_value=plan):
            result=self.run_cycle()
        self.assertEqual('accepted',result['status'],result.get('reason'))
        registry=exchange.read_state(self.registry)
        reviewer=next(registry['requests'][rid] for rid in result['review_request_ids']
            if registry['requests'][rid]['assignment']['agent']=='qa-conclusion-reviewer')
        message=exchange.read_json(reviewer['result']['path'])
        self.assertEqual(1,len(message['payload']['conclusion_checks']))
        self.assertEqual(3,len(message['payload']['execution_checks']))
        for check in message['payload']['execution_checks']:
            receipt=check_receipt(check['receipt'],reviewer['assignment'])
            self.assertEqual(reviewer['assignment']['request_id'],receipt['context']['request_id'])
            self.assertIn('Ran 1 test',receipt['result']['structuredContent']['stderr'])
        self.assess(result)
        exchange.finish_task(self.registry,'passed','Independent conclusion and fresh runtime observations checked',None)

    def test_three_actual_launches_verify_runtime_criterion_and_finish(self):
        result = self.accepted()
        row = self.reviewer(result)
        message = exchange.read_json(row['result']['path'])
        self.assertEqual(3, len(message['payload']['execution_checks']))
        observations = []
        for check in message['payload']['execution_checks']:
            receipt = check_receipt(check['receipt'], row['assignment'])
            self.assertIn(check['receipt'], row['result']['artifact_hashes'])
            self.assertEqual('managed-worker:' + receipt['context']['attempt_token'], row['owner_task_id'])
            self.assertIn('Ran 1 test', receipt['result']['structuredContent']['stderr'])
            observations.append(receipt['result']['structuredContent']['exit_code'])
        self.assertEqual([0, 1, 0], observations)
        self.assess(result)
        exchange.finish_task(self.registry, 'passed', 'Synthetic runtime reviewed', None)

    def test_weak_assert_is_corrected_and_new_revision_gets_new_observations(self):
        result = self.run_cycle(author='correct-once')
        self.assertEqual('accepted', result['status'], result.get('reason'))
        self.assertEqual(1, result['round'])
        state = exchange.read_state(self.registry)
        old = exchange.read_json(state['requests']['author.review-runtime']['result']['path'])
        new = exchange.read_json(self.reviewer(result)['result']['path'])
        self.assertEqual('changes_requested', old['event'])
        old_receipt = old['payload']['execution_checks'][0]['receipt']
        with self.assertRaises(ValueError):
            check_receipt(old_receipt, self.reviewer(result)['assignment'])
        self.assertNotEqual(old['payload']['target_sha256'], new['payload']['target_sha256'])
        self.assess(result)

    def test_generic_pass_without_launches_blocks_author_acceptance(self):
        result = self.run_cycle(reviewer_extra=['--skip-execution'])
        self.assertEqual('blocked', result['status'])
        self.assertIn('missing independent', result['reason'])
        self.assertEqual('reported', exchange.read_state(self.registry)['requests']['author']['status'])

    def test_fresh_tool_call_with_old_backend_run_cannot_verify_a_new_launch(self):
        result = self.run_cycle(server_extra=['--stale-run'])
        self.assertEqual('blocked', result['status'])
        self.assertIn('missing independent', result['reason'])
        self.assertEqual('reported', exchange.read_state(self.registry)['requests']['author']['status'])

    def test_failed_tool_call_stays_unknown_and_blocks_cycle(self):
        result = self.run_cycle(server_extra=['--tool-error'])
        self.assertEqual('blocked', result['status'])
        self.assertIn('unknown expectation', result['reason'])
        state = exchange.read_state(self.registry)
        message = exchange.read_json(state['requests']['author.review-runtime']['result']['path'])
        self.assertEqual('blocked', message['event'])
        self.assertTrue(all(c['status'] == 'unknown' for c in message['payload']['execution_checks']))

    def test_target_mutation_during_probe_cannot_be_verified(self):
        result = self.run_cycle(server_extra=['--mutate-target'])
        self.assertEqual('blocked', result['status'])
        self.assertEqual('reported', exchange.read_state(self.registry)['requests']['author']['status'])

    def test_changed_receipt_after_assessment_blocks_finish(self):
        result = self.accepted()
        self.assess(result)
        check = exchange.read_json(self.reviewer(result)['result']['path'])['payload']['execution_checks'][0]
        Path(check['receipt']).write_text('Changed observed runtime result')
        with self.assertRaises(ValueError):
            exchange.finish_task(self.registry, 'passed', 'Stale proof', None)

    def test_expected_result_arguments_and_tool_are_checked_against_actual_receipt(self):
        result = self.accepted()
        row = self.reviewer(result)
        payload = exchange.read_json(row['result']['path'])['payload']
        self.assertTrue(covers(payload, row['assignment'], 'C', execution_specs()))
        for mutate in (
            lambda specs: specs[0]['expected_result'].update({'/structuredContent/exit_code': 1}),
            lambda specs: specs[0]['expected_arguments'].update({'/project': 'ANOTHER'}),
            lambda specs: specs[0].update(tool='unapproved'),
            lambda specs: specs[0]['expected_result'].update({'/structuredContent/exit_code': False}),
        ):
            specs = execution_specs()
            mutate(specs)
            self.assertFalse(covers(payload, row['assignment'], 'C', specs))
        payload['execution_checks'].pop()
        self.assertFalse(covers(payload, row['assignment'], 'C', execution_specs()))

    def test_required_runtime_policy_is_checked_before_author_launch(self):
        plan = self.plan()
        plan['reviewers'][0].pop('gateway_policy')
        with self.assertRaisesRegex(ValueError, 'own policy'):
            review_cycle.initialize(self.cycle, plan, self.root)
        self.assertFalse(self.cycle.exists())

    def test_runtime_gateway_does_not_inherit_author_policy(self):
        plan = self.plan()
        plan['author_plan']['steps'][0]['gateway_policy'] = self.policy()
        plan['reviewers'][0].pop('gateway_policy')
        with self.assertRaisesRegex(ValueError, 'own policy'):
            review_cycle.initialize(self.cycle, plan, self.root)

    def test_runtime_policy_limits_and_structural_checks_are_not_optional(self):
        for mutation in (
            lambda d: d['required_reviews'][0]['execution'][0].pop('expected_result'),
            lambda d: d['required_reviews'][0]['execution'][0].update(expected_arguments={}),
            lambda d: d['required_reviews'][0]['execution'].append(d['required_reviews'][0]['execution'][0]),
        ):
            definition = copy.deepcopy(self.definition)
            mutation(definition)
            with self.assertRaises(ValueError):
                criteria_packet(dict(schema_version=1, criteria=[definition]))
        payload = dict(target_request_id='a', target_sha256='a'*64, review_scope='runtime',
            checks=[dict(criterion_id='C', status='passed', note='n', evidence=['e'])], findings=[], limitations=[],
            execution_checks=[dict(criterion_id='C', check_id='normal', status='unknown', note='Not launched')])
        self.assertTrue(validate_review(payload, 'review_passed'))
        self.assertEqual([], validate_review(payload, 'blocked'))

    def test_completed_attempt_cannot_launch_another_probe(self):
        result = self.accepted()
        runtime = self.reviewer(result)['assignment']['review_execution']
        with self.assertRaisesRegex(ValueError, 'active'):
            context_for(runtime['execution_state'], runtime['step_id'], runtime['attempt_token'])
        state = supervisor.read(runtime['execution_state'])
        attempt = state['steps'][runtime['step_id']]['attempts'][-1]
        run = subprocess.run([sys.executable, str(ROOT/'scripts/mcp_action_gateway.py'),
            '--policy', str(Path(attempt['run_dir'])/'policy.json'),
            '--journal', str(supervisor.action_path(runtime['execution_state'], runtime['step_id'])),
            '--task-id', 'T', '--step-id', runtime['step_id'], '--execution-state', runtime['execution_state'],
            '--attempt-token', runtime['attempt_token']], input='', text=True, capture_output=True)
        self.assertNotEqual(0, run.returncode)
        self.assertIn('fenced before backend start', run.stderr)

    def test_cli_reviewer_has_only_its_own_gateway_and_runtime_instructions(self):
        result = self.accepted()
        assignment = self.reviewer(result)['assignment']
        runtime = assignment['review_execution']
        state = supervisor.read(runtime['execution_state'])
        step = state['plan']['steps'][0]
        attempt = state['steps'][runtime['step_id']]['attempts'][-1]
        state['plan']['adapter'] = dict(kind='gigacode', executable='synthetic-cli')
        flags = '--bare --exclude-tools --append-system-prompt --output-format --max-wall-time --max-session-turns --mcp-config --allowed-tools'
        with patch.object(execution_worker.shutil, 'which', return_value='/synthetic/cli'), patch.object(
                execution_worker.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=flags, stderr='')):
            argv = execution_worker.command(state, step, attempt, runtime['execution_state'])
        self.assertEqual('mcp__qa_scoped_tools', argv[argv.index('--allowed-tools') + 1])
        self.assertIn('run_shell_command', argv)
        system = (Path(attempt['run_dir'])/'system.md').read_text()
        self.assertIn('qa_review_execution_receipt', system)
        self.assertIn('execution_checks', system)

    def test_generic_result_reviewer_can_execute_with_its_own_policy(self):
        definition = copy.deepcopy(self.definition)
        definition['required_reviews'][0]['agent'] = 'qa-result-reviewer'
        exchange.set_criteria(self.registry, dict(schema_version=1, criteria=[definition]), None)
        plan = self.plan()
        plan['reviewers'][0]['agent'] = 'qa-result-reviewer'
        plan['reviewers'].append(dict(scope='oracle', agent='qa-autotest-reviewer', max_wall_seconds=5,
            adapter=dict(kind='command', argv=[sys.executable, str(ROOT/'tests/fixtures/review_agent.py'), '--mode', 'strong'])))
        with patch.object(self, 'plan', return_value=plan):
            result = self.accepted()
        self.assess(result)
        runtime = self.reviewer(result)
        self.assertEqual('qa-result-reviewer', runtime['assignment']['agent'])

    def test_timeout_does_not_become_a_passed_observation(self):
        result = self.run_cycle(server_extra=['--slow'])
        self.assertEqual('blocked', result['status'], result)
        registry = exchange.read_state(self.registry)
        row = registry['requests']['author.review-runtime']
        if row['result']:
            message = exchange.read_json(row['result']['path'])
            self.assertNotEqual('review_passed', message['event'])
        runtime = row['assignment']['review_execution']
        journal = exchange.read_json(supervisor.action_path(runtime['execution_state'], runtime['step_id']))
        self.assertTrue(journal['observations'])
        self.assertTrue(all(o['status'] != 'verified' for o in journal['observations'].values()))

    def test_cancel_fences_running_review_and_preserves_stop(self):
        review_cycle.initialize(self.cycle, self.plan(server_extra=['--slow']), self.root)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = review_cycle.tick(self.cycle)
            reviewers = [c for c in state['children'] if c['kind'] == 'review']
            if reviewers:
                progress = supervisor.read(reviewers[0]['path'])
                journal = supervisor.action_path(reviewers[0]['path'], 'review-runtime')
                if journal.exists() and exchange.read_json(journal).get('observations'):
                    break
            time.sleep(.05)
        else:
            self.fail('No active reviewer probe')
        review_cycle.cancel(self.cycle, 'Explicit test stop while probe runs')
        self.assertTrue(review_cycle.tick(self.cycle)['cancel_requested'])
        progress = supervisor.read(reviewers[0]['path'])
        self.assertTrue(progress['cancel_requested'])
        self.assertEqual('cancelled', progress['status'])
        runtime = progress['steps']['review-runtime']['attempts'][-1]['assignment']['review_execution']
        with self.assertRaises(ValueError):
            context_for(runtime['execution_state'], runtime['step_id'], runtime['attempt_token'])

    def test_tampered_policy_is_rejected_before_starting_its_backend(self):
        result = self.accepted()
        runtime = self.reviewer(result)['assignment']['review_execution']
        state = supervisor.read(runtime['execution_state'])
        attempt = state['steps'][runtime['step_id']]['attempts'][-1]
        policy_path = Path(attempt['run_dir'])/'policy.json'
        policy = exchange.read_json(policy_path)
        marker = self.root/'unapproved-start.txt'
        policy['server_command'] = [sys.executable, '-c', 'from pathlib import Path; Path(' + repr(str(marker)) + ').write_text("started")']
        exchange.atomic_write(policy_path, policy)
        run = subprocess.run([sys.executable, str(ROOT/'scripts/mcp_action_gateway.py'), '--policy', str(policy_path),
            '--journal', str(supervisor.action_path(runtime['execution_state'], runtime['step_id'])),
            '--task-id', 'T', '--step-id', runtime['step_id'], '--execution-state', runtime['execution_state'],
            '--attempt-token', runtime['attempt_token']], input='', text=True, capture_output=True)
        self.assertNotEqual(0, run.returncode)
        self.assertIn('frozen execution plan', run.stderr)
        self.assertFalse(marker.exists())


class ObservationGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'actions.json'
        self.policy = dict(schema_version=1, server_command=['synthetic'], max_review_calls=3,
            tools={'probe': dict(effect='read', allowed_arguments=['project'], constraints={'/project': 'SYNTHETIC'}),
                   'write': dict(effect='write', allowed_arguments=['project'], constraints={'/project': 'SYNTHETIC'})})
        self.arguments = dict(project='SYNTHETIC')

    def gateway(self, client):
        return ActionGateway(self.path, self.policy, 'T', 'runtime', client, review_context=lambda: dict(synthetic=True))

    def test_identical_reads_are_new_calls_and_budget_is_enforced(self):
        class Client:
            calls = 0
            def request(self, *args):
                self.calls += 1
                return dict(structuredContent=dict(call=self.calls))
        client = Client()
        gateway = self.gateway(client)
        receipts = [gateway.call('probe', self.arguments)['_meta']['qa_review_execution_receipt'] for _ in range(3)]
        self.assertEqual(3, client.calls)
        self.assertEqual(3, len({r['path'] for r in receipts}))
        self.assertTrue(all(r['fresh'] for r in receipts))
        with self.assertRaisesRegex(ValueError, 'budget'):
            gateway.call('probe', self.arguments)
        self.assertEqual(3, client.calls)

    def test_concurrent_cached_write_is_never_a_second_fresh_launch(self):
        entered, release = threading.Event(), threading.Event()
        class Client:
            calls = 0
            def request(self, *args):
                self.calls += 1
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('Test release timeout')
                return dict(structuredContent=dict(id='Synthetic write'))
        client = Client()
        first, second = self.gateway(client), self.gateway(client)
        results, errors = [], []
        def call(gateway):
            try:
                results.append(gateway.call('write', self.arguments))
            except Exception as exc:
                errors.append(str(exc))
        one, two = threading.Thread(target=call, args=(first,)), threading.Thread(target=call, args=(second,))
        one.start()
        self.assertTrue(entered.wait(2))
        two.start()
        time.sleep(.05)
        release.set()
        one.join(3)
        two.join(3)
        self.assertFalse(errors, errors)
        self.assertEqual(1, client.calls)
        self.assertEqual([False, True], sorted(r['_meta']['qa_review_execution_receipt']['fresh'] for r in results))

    def test_failed_read_and_changed_context_never_create_verified_receipts(self):
        class Client:
            def request(self, *args):
                return dict(isError=True, content=[])
        gateway = self.gateway(Client())
        with self.assertRaisesRegex(ValueError, 'failed'):
            gateway.call('probe', self.arguments)
        row = next(iter(exchange.read_json(self.path)['observations'].values()))
        self.assertEqual('unknown', row['status'])
        self.assertNotIn('receipt_path', row)


if __name__ == '__main__':
    unittest.main()
