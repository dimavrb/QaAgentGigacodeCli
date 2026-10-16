"""Local contract/process checks; no live model evaluation."""
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import execution_supervisor as supervisor
import process_control
import task_exchange as exchange
from action_gateway import ActionGateway, MCPClient
import failure_journal
import execution_worker
from self_review_fixtures import publish_content_review
from environment_fixtures import bind_environment
from questionnaire import normalize_packet, save_response
from gateway_fixtures import policy

AGENT = ROOT / "tests/fixtures/managed_agent.py"


class ManagedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = self.root / 'task.json'
        self.execution = self.root / 'execution.json'
        self.counter = self.root / 'counter.txt'
        exchange.init_task(self.registry, 'QA-1', 'Synthetic execution')
        self.addCleanup(self.stop)

    def stop(self):
        if self.execution.exists():
            try:
                supervisor.cancel(self.execution, 'Synthetic test cleanup')
                for child in supervisor.CHILDREN.values():
                    child.poll()
            except Exception:
                pass

    def plan(self, mode='success', steps=1, **extra):
        rows = []
        for index in range(steps):
            assignment = dict(task_id='QA-1', request_id=f'env-{index+1}', agent='qa-test-env-configurator', stage=f'env-{index+1}',
                goal='Synthetic task', acceptance_criteria=['Synthetic evidence available'], stop_condition='Return report',
                expected_artifact='QA JSON', result_path=str(self.root/f'result-{index+1}.json'), inputs=[],
                depends_on=[f'env-{index}'] if index else [])
            bind_environment(assignment, self.root, 'fixture', 'fixture-1')
            rows.append(dict(step_id=f'env-{index+1}', assignment=assignment, max_attempts=3, retry_safe=True,
                retry_on=['transient','timeout','process_lost'], retry_backoff_seconds=0, max_wall_seconds=5))
        value = dict(schema_version=1, task_state=str(self.registry), workspace=str(self.root), scope_kind='synthetic',
                     adapter=dict(kind='command', argv=[sys.executable,str(AGENT),'--counter',str(self.counter),'--mode',mode]), steps=rows)
        value.update(extra)
        return value

    def init(self, plan):
        supervisor.initialize(self.execution, plan, self.root)

    def wait(self, predicate, timeout=10):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            value = supervisor.tick(self.execution)
            if predicate(value):
                return value
            time.sleep(.08)
        self.fail('Timed out: '+json.dumps(supervisor.read(self.execution)))

    def accept(self, rid):
        registry = exchange.read_state(self.registry)
        exchange.review_result(self.registry,rid,'Synthetic report/evidence read',True,registry['revision'])

    def test_capture_requires_content_review_then_reuses_checkpoint(self):
        self.init(self.plan())
        value = self.wait(lambda state:state['status']=='awaiting_review')
        self.assertIsNone(value['steps']['env-1']['checkpoint'])
        self.assertEqual('reported',exchange.read_state(self.registry)['requests']['env-1']['status'])
        self.accept('env-1')
        self.wait(lambda state:state['status']=='completed')
        for _ in range(3):supervisor.tick(self.execution)
        self.assertEqual('1',self.counter.read_text())

    def test_late_blocking_review_prevents_checkpoint_and_next_worker(self):
        self.init(self.plan(steps=2))
        self.wait(lambda state: state['status'] == 'awaiting_review')
        self.accept('env-1')
        publish_content_review(self.registry, 'env-1')
        with self.assertRaisesRegex(ValueError, 'Unresolved blocking'):
            supervisor.tick(self.execution)
        value = supervisor.read(self.execution)
        self.assertIsNone(value['steps']['env-1']['checkpoint'])
        self.assertEqual([], value['steps']['env-2']['attempts'])
        self.assertEqual('1', self.counter.read_text())

    def test_late_blocking_review_invalidates_saved_checkpoint(self):
        plan = self.plan(steps=2)
        plan['steps'][1]['assignment']['depends_on'] = []
        self.init(plan)
        self.wait(lambda state: state['status'] == 'awaiting_review')
        self.accept('env-1')
        value = self.wait(lambda state: state['steps']['env-1']['checkpoint'] is not None)
        publish_content_review(self.registry, 'env-1')
        with self.assertRaisesRegex(ValueError, 'Unresolved blocking'):
            supervisor.tick(self.execution)
        self.assertEqual(value['steps']['env-1']['checkpoint'],
                         supervisor.read(self.execution)['steps']['env-1']['checkpoint'])
        self.assertEqual([], supervisor.read(self.execution)['steps']['env-2']['attempts'])
        self.assertEqual('1', self.counter.read_text())

    def test_concurrent_registry_notification_does_not_invalidate_report_and_trace_ids_match(self):
        self.init(self.plan())
        submit=exchange.submit_result
        once={'done':False}
        def concurrent(path,rid,revision):
            if not once['done']:
                once['done']=True
                exchange.mutate(path,None,lambda state:exchange.event(state,'synthetic_progress'))
            return submit(path,rid,revision)
        with patch.object(exchange,'submit_result',side_effect=concurrent):
            state=self.wait(lambda state:state['status']=='awaiting_review')
        attempt=state['steps']['env-1']['attempts'][-1]
        registry=exchange.read_state(self.registry)
        self.assertEqual(registry['requests']['env-1']['attempt_id'],attempt['attempt_id'])
        trace=[json.loads(line) for line in (Path(attempt['run_dir'])/'trace.jsonl').read_text().splitlines()]
        self.assertTrue(all(event['attempt_id']==attempt['attempt_id'] for event in trace))
        self.assertEqual('1',self.counter.read_text())

    def test_sequential_steps_wait_for_review_and_bind_retried_source(self):
        self.init(self.plan('transient-once',steps=2))
        self.wait(lambda state:state['status']=='awaiting_review')
        registry=exchange.read_state(self.registry)
        self.assertEqual('failed',registry['requests']['env-1']['status'])
        self.accept('env-1.retry-2')
        self.wait(lambda state:state['steps']['env-2']['status']=='awaiting_review')
        self.assertEqual(['env-1.retry-2'],exchange.read_state(self.registry)['requests']['env-2']['assignment']['depends_on'])

    def test_second_supervisor_cannot_claim_controller_or_launch_duplicate(self):
        self.init(self.plan('hold'))
        with supervisor.controller(self.execution):
            with self.assertRaisesRegex(ValueError,'Another supervisor'):
                supervisor.tick(self.execution)
        self.wait(lambda state:self.counter.exists() and state['steps']['env-1']['attempts'] and state['steps']['env-1']['attempts'][-1].get('child_identity'))
        for _ in range(3):supervisor.tick(self.execution)
        self.assertEqual('1',self.counter.read_text())
        self.assertEqual(1,len(supervisor.read(self.execution)['steps']['env-1']['attempts']))

    def test_cli_restart_observes_same_worker(self):
        self.init(self.plan('hold'))
        result=subprocess.run([sys.executable,str(ROOT/'scripts/manage_execution.py'),'--state',str(self.execution),'tick'],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        self.wait(lambda state:self.counter.exists() and bool(state['steps']['env-1']['attempts'][-1].get('child_identity')))
        self.assertEqual('1',self.counter.read_text())
        self.assertEqual(1,len(supervisor.read(self.execution)['steps']['env-1']['attempts']))

    def test_cancel_stops_process_and_prevents_auto_resume(self):
        self.init(self.plan('hold'))
        value=self.wait(lambda state:self.counter.exists() and state['steps']['env-1']['attempts'] and state['steps']['env-1']['attempts'][-1].get('child_identity'))
        child=value['steps']['env-1']['attempts'][-1]['child_identity']
        result=supervisor.cancel(self.execution,'User explicitly stopped')
        self.assertEqual('cancelled',result['status'])
        self.assertFalse(process_control.matches(child))
        for _ in range(3):supervisor.tick(self.execution)
        self.assertEqual('1',self.counter.read_text())
        supervisor.resume(self.execution,'User explicitly resumes')
        self.wait(lambda state:self.counter.exists() and self.counter.read_text()=='2' and len(state['steps']['env-1']['attempts'])==2 and state['steps']['env-1']['attempts'][-1].get('child_identity'))
        self.assertEqual('2',self.counter.read_text())

    def test_cancelling_while_awaiting_review_preserves_result_on_resume(self):
        self.init(self.plan())
        self.wait(lambda state:state['status']=='awaiting_review')
        supervisor.cancel(self.execution,'Stop waiting')
        supervisor.resume(self.execution,'Continue review')
        self.accept('env-1')
        self.wait(lambda state:state['status']=='completed')
        self.assertEqual('1',self.counter.read_text())

    def test_permanent_failure_is_not_retried(self):
        self.init(self.plan('permanent'))
        self.wait(lambda state:state['status']=='blocked')
        self.assertEqual('1',self.counter.read_text())

    def test_repeated_transient_error_stops_without_new_information(self):
        self.init(self.plan('transient'))
        value=self.wait(lambda state:state['status']=='blocked')
        self.assertEqual(2,len(value['steps']['env-1']['attempts']))
        self.assertEqual('2',self.counter.read_text())

    def test_invalid_report_has_no_automatic_retry_without_repair_policy(self):
        self.init(self.plan('invalid'))
        value=self.wait(lambda state:state['status']=='blocked')
        self.assertEqual('invalid_result',value['steps']['env-1']['attempts'][0]['category'])
        self.assertEqual('1',self.counter.read_text())

    def test_timeout_kills_backend_before_retry(self):
        plan=self.plan('hold-once')
        plan['steps'][0]['max_wall_seconds']=.5
        self.init(plan)
        value=self.wait(lambda state:state['status']=='awaiting_review')
        self.assertEqual(2,len(value['steps']['env-1']['attempts']))
        old=value['steps']['env-1']['attempts'][0]
        self.assertEqual('timeout',old['category'])
        self.assertTrue(supervisor.safe_group_gone(old))

    def test_dead_worker_child_is_stopped_and_safe_retry_is_new_attempt(self):
        self.init(self.plan('hold-once'))
        value=self.wait(lambda state:self.counter.exists() and state['steps']['env-1']['attempts'] and state['steps']['env-1']['attempts'][-1].get('child_identity'))
        attempt=value['steps']['env-1']['attempts'][-1]
        os.kill(attempt['worker_identity']['pid'],signal.SIGKILL)
        value=self.wait(lambda state:state['status']=='awaiting_review')
        self.assertEqual(2,len(value['steps']['env-1']['attempts']))
        self.assertFalse(process_control.matches(attempt['child_identity']))

    def test_changed_checkpoint_proof_blocks_next_execution(self):
        self.init(self.plan(steps=2))
        self.wait(lambda state:state['status']=='awaiting_review')
        self.accept('env-1')
        supervisor.tick(self.execution)
        checkpoint=supervisor.read(self.execution)['steps']['env-1']['checkpoint']
        evidence=next(name for name in checkpoint['artifact_hashes'] if name.endswith('proof.txt'))
        Path(evidence).write_text('changed')
        with self.assertRaisesRegex(ValueError,'Checkpoint evidence changed'):
            supervisor.tick(self.execution)
        self.assertEqual('1',self.counter.read_text())

    def test_applied_html_answers_resume_only_bound_step(self):
        packet=normalize_packet(dict(task_id='QA-1',request_id='question-1',questionnaire_id='FORM-1',
                                     questions=[dict(question_id='Q-1',question='Expected scope?')]))
        source=self.root/'questions.json'
        source.write_text(json.dumps(packet))
        answers=self.root/'answers.json'
        exchange.register_questions(self.registry,source,answers,exchange.read_state(self.registry)['revision'])
        plan=self.plan(auto_apply_answers=True)
        plan['steps'][0]['wait_for_questionnaire_ids']=['FORM-1']
        self.init(plan)
        self.assertEqual('waiting_answers',supervisor.tick(self.execution)['status'])
        response={key:packet[key] for key in ('schema_version','task_id','request_id','questionnaire_id','revision')}
        response.update(answers=[dict(question_id='Q-1',status='answered',text='Only synthetic scope',selected_options=[])],overall_comment='')
        save_response(answers,packet,response)
        exchange.notify_answers(self.registry,packet,answers)
        self.wait(lambda state:state['status']=='awaiting_review')
        registry=exchange.read_state(self.registry)
        self.assertEqual('applied',registry['questions']['FORM-1']['status'])
        self.assertIn(str(answers.resolve()),registry['requests']['env-1']['assignment']['inputs'])

    def test_saved_html_answers_resume_after_missed_notification(self):
        packet=normalize_packet(dict(task_id='QA-1',request_id='question-1',questionnaire_id='FORM-1',
            questions=[dict(question_id='Q-1',question='Expected scope?')]))
        source,answers=self.root/'questions.json',self.root/'answers.json'
        source.write_text(json.dumps(packet))
        exchange.register_questions(self.registry,source,answers,exchange.read_state(self.registry)['revision'])
        plan=self.plan(auto_apply_answers=True)
        plan['steps'][0]['wait_for_questionnaire_ids']=['FORM-1']
        self.init(plan)
        self.assertEqual('waiting_answers',supervisor.tick(self.execution)['status'])
        response={key:packet[key] for key in ('schema_version','task_id','request_id','questionnaire_id','revision')}
        response.update(answers=[dict(question_id='Q-1',status='answered',text='Only synthetic scope',selected_options=[])],overall_comment='')
        save_response(answers,packet,response)  # Offline download, no notify_answers call.
        supervisor.serve(self.execution,max_seconds=.3,answers_poll_seconds=.05)
        self.wait(lambda state:state['status']=='awaiting_review')
        registry=exchange.read_state(self.registry)
        self.assertEqual('applied',registry['questions']['FORM-1']['status'])
        self.assertEqual(1,sum(e['kind']=='answers_ready' for e in registry['events']))
        self.assertIn(str(answers.resolve()),registry['requests']['env-1']['assignment']['inputs'])

    def test_uncertain_external_write_blocks_retry_until_readback(self):
        store=self.root/'tickets.json'
        plan=self.plan('external')
        plan['steps'][0]['gateway_policy']=policy(store,True)
        self.init(plan)
        value=self.wait(lambda state:state['status']=='needs_reconciliation')
        self.assertEqual('1',self.counter.read_text())
        self.assertEqual(1,json.loads(store.read_text())['writes'])
        with self.assertRaisesRegex(ValueError,'uncertain'):
            supervisor.retry_after_reconciliation(self.execution,'env-1','Retry requested')
        journal=supervisor.action_path(self.execution,'env-1')
        aid=next(iter(exchange.read_json(journal)['actions']))
        command=[sys.executable,str(ROOT/'scripts/manage_execution.py'),'--state',str(self.execution),
                 'reconcile-action','--step-id','env-1','--action-id',aid]
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        supervisor.retry_after_reconciliation(self.execution,'env-1','Existing synthetic ticket confirmed')
        self.wait(lambda state:state['status']=='awaiting_review')
        self.assertEqual(1,json.loads(store.read_text())['writes'])

    def test_successful_report_cannot_hide_uncertain_external_effect(self):
        plan = self.plan('external-unknown-success')
        plan['steps'][0]['gateway_policy'] = policy(self.root/'tickets.json', True)
        self.init(plan)
        self.wait(lambda state: state['status'] == 'needs_reconciliation')
        self.assertIsNone(supervisor.read(self.execution)['steps']['env-1']['checkpoint'])
        self.assertEqual('1', self.counter.read_text())
        source=plan['steps'][0]['gateway_policy']
        client=MCPClient(source['server_command'],1)
        self.addCleanup(client.close)
        client.request('initialize',dict(protocolVersion='2024-11-05',capabilities={},clientInfo={'name':'test','version':'1'}))
        gateway=ActionGateway(supervisor.action_path(self.execution,'env-1'),source,'QA-1','env-1',client)
        gateway.reconcile(gateway.status()[0]['action_id'])
        supervisor.retry_after_reconciliation(self.execution,'env-1','Effect verified; inspect existing output')
        self.wait(lambda state:state['status']=='awaiting_review')
        self.assertEqual('1',self.counter.read_text())

    def test_retry_requires_explicit_safe_flag_and_attempt_budget(self):
        plan = self.plan('transient')
        plan['steps'][0]['retry_safe'] = False
        self.init(plan)
        self.wait(lambda state: state['status'] == 'blocked')
        self.assertEqual('1', self.counter.read_text())

    def test_waiting_for_answers_and_review_does_not_consume_active_budget(self):
        self.init(self.plan(max_active_seconds=1))
        self.wait(lambda state: state['status'] == 'awaiting_review')
        supervisor.update(self.execution, lambda state: state.update(first_run_at=time.time()-86400))
        supervisor.tick(self.execution)
        self.assertFalse(supervisor.read(self.execution)['cancel_requested'])
        self.accept('env-1')
        self.wait(lambda state:state['status']=='completed')

    def test_failures_are_recorded_once_without_claiming_recovery_or_cause(self):
        self.init(self.plan('permanent'))
        self.wait(lambda state: state['status'] == 'blocked')
        supervisor.tick(self.execution)
        records, _ = failure_journal.read_journal(supervisor.read(self.execution)['plan']['failure_journal'])
        self.assertEqual(1, len(records))
        self.assertEqual('unknown', records[0]['entry']['cause']['status'])
        self.assertEqual('not_attempted', records[0]['entry']['recovery']['outcome'])
        self.assertEqual('synthetic', records[0]['entry']['scope_kind'])

    def test_frozen_plan_and_inputs_cannot_change_before_start(self):
        source = self.root/'requirement.txt'
        source.write_text('original')
        plan = self.plan()
        plan['steps'][0]['assignment']['inputs'] = [str(source)]
        self.init(plan)
        source.write_text('changed')
        state = supervisor.tick(self.execution)
        self.assertEqual('waiting_dependencies',state['status'])
        self.assertFalse(self.counter.exists())
        state['plan']['steps'][0]['max_attempts'] = 5
        self.execution.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'Frozen'):
            supervisor.read(self.execution)

    def test_late_unclaimed_worker_is_fenced_before_model_start(self):
        self.init(self.plan())
        with patch.object(supervisor.subprocess,'Popen',side_effect=OSError('synthetic spawn failure')):
            supervisor.tick(self.execution)
        state=supervisor.read(self.execution)
        attempt=state['steps']['env-1']['attempts'][-1]
        result=subprocess.run([sys.executable,str(ROOT/'scripts/execution_worker.py'),'--state',str(self.execution),
                              '--step-id','env-1','--attempt-token',attempt['token']],start_new_session=True,capture_output=True,text=True)
        self.assertEqual(2,result.returncode)
        self.assertFalse(self.counter.exists())

    def test_cli_preflight_never_launches_when_required_flags_are_missing(self):
        self.init(self.plan())
        state=supervisor.read(self.execution)
        state['plan']['adapter']={'kind':'cli','executable':'synthetic-cli','backend':'fixture'}
        with patch.object(execution_worker.shutil,'which',return_value='/synthetic/cli'), patch.object(
                execution_worker.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout='--bare',stderr='')):
            with self.assertRaisesRegex(ValueError,'flags'):
                execution_worker.command(state,state['plan']['steps'][0],{'run_dir':str(self.root)},self.execution)

    def test_cli_command_snapshots_contract_and_has_only_explicit_gateway_config(self):
        plan=self.plan()
        plan['adapter']={'kind':'cli','executable':'synthetic-cli','backend':'fixture'}
        plan['steps'][0]['gateway_policy']=policy(self.root/'tickets.json')
        self.init(plan)
        state=supervisor.read(self.execution)
        step=state['plan']['steps'][0]
        exchange.atomic_write(self.root/'handoff.json',{'assignment':step['assignment'],'sources':[],'answer_sources':[]})
        flags='--bare --exclude-tools --append-system-prompt --output-format --max-wall-time --max-session-turns --mcp-config --allowed-tools'
        with patch.object(execution_worker.shutil,'which',return_value='/synthetic/cli'),patch.object(
                execution_worker.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=flags,stderr='')):
            argv=execution_worker.command(state,step,{'run_dir':str(self.root),'assignment':step['assignment'],'token':'owned'},self.execution)
        self.assertIn('--bare',argv)
        self.assertIn('--max-session-turns',argv)
        self.assertEqual('mcp__qa_scoped_tools',argv[argv.index('--allowed-tools')+1])
        self.assertEqual({'qa_scoped_tools'},set(exchange.read_json(self.root/'mcp.json')['mcpServers']))
        self.assertIn('docs/CONTRACT.md',exchange.read_json(self.root/'instructions.json'))
        self.assertIn('run_shell_command',argv)
        for bridge in ('exec','create_sub_session','cron_create','thread_post','tool_call','read_mcp_resource'):
            self.assertIn(bridge,argv)
        state['plan']['adapter']['expected_version']='authorized-version'
        with patch.object(execution_worker.shutil,'which',return_value='/synthetic/cli'),patch.object(
                execution_worker.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=flags,stderr='')):
            with self.assertRaisesRegex(ValueError,'version differs'):
                execution_worker.command(state,step,{'run_dir':str(self.root)},self.execution)

    def test_answers_remain_pending_until_coordinator_applies_them_by_default(self):
        packet=normalize_packet(dict(task_id='QA-1',request_id='question-1',questionnaire_id='FORM-1',
                                     questions=[dict(question_id='Q-1',question='Scope?')]))
        source=self.root/'questions.json'
        source.write_text(json.dumps(packet))
        answers=self.root/'answers.json'
        exchange.register_questions(self.registry,source,answers,exchange.read_state(self.registry)['revision'])
        plan=self.plan()
        plan['steps'][0]['wait_for_questionnaire_ids']=['FORM-1']
        self.init(plan)
        response={key:packet[key] for key in ('schema_version','task_id','request_id','questionnaire_id','revision')}
        response.update(answers=[dict(question_id='Q-1',status='answered',text='Synthetic',selected_options=[])],overall_comment='')
        save_response(answers,packet,response)
        exchange.notify_answers(self.registry,packet,answers)
        self.assertEqual('waiting_answers',supervisor.tick(self.execution)['status'])
        self.assertFalse(self.counter.exists())
        exchange.apply_answers(self.registry,'FORM-1','Meaning reviewed',exchange.read_state(self.registry)['revision'])
        self.wait(lambda state:state['status']=='awaiting_review')

    def test_active_budget_stops_execution_and_cannot_be_reset_by_resume(self):
        self.init(self.plan('hold',max_active_seconds=1))
        self.wait(lambda state:state['cancel_requested'])
        self.assertEqual('cancelled',supervisor.read(self.execution)['status'])
        with self.assertRaisesRegex(ValueError,'budget'):
            supervisor.resume(self.execution,'Resume requested')
