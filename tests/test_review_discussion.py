"""Bounded discussion and evidence gates on trusted fixtures, not model-quality scores."""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
import task_exchange as exchange
import review_cycle
import execution_supervisor as supervisor
import execution_worker
from review_discussion import check_binding, check_cohort, configuration, validate_checks
from self_review_fixtures import review_for

spec=importlib.util.spec_from_file_location('discussion_demo',ROOT/'examples/review-discussion/build_demo.py')
demo=importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class DiscussionBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.source=self.root/'primary-source.json'
        exchange.atomic_write(self.source,dict(product_run='not_run'))
        self.author=self.root/'author.json'
        exchange.atomic_write(self.author,dict(summary='Synthetic author'))
        self.reviews=[]
        for scope,status in [('facts','passed'),('inference','failed')]:
            path=self.root/(scope+'.json')
            message=dict(task_id='T',request_id=scope,agent='qa-conclusion-reviewer',event='review_passed' if status=='passed' else 'changes_requested',
                summary='Synthetic '+scope,artifacts=[],blockers=[],self_review=review_for(),payload=dict(
                    checks=[dict(criterion_id='C',status=status)],conclusion_checks=[dict(criterion_id='C',pointer='/summary',status=status)]))
            exchange.atomic_write(path,message)
            self.reviews.append(dict(scope=scope,request_id=scope,result_path=str(path),artifact_hashes={str(path):exchange.fingerprint(path)}))
        self.packet=self.root/'packet.json'
        self.content=dict(schema_version=1,author_request='author',target_sha256=exchange.fingerprint(self.author),round=1,reviews=self.reviews,
                          issues=[dict(issue_id='I-1',criterion_id='C',pointer='/summary')])
        exchange.atomic_write(self.packet,self.content)
        self.assignment=dict(agent='qa-conclusion-reviewer',inputs=[str(self.packet),str(self.source),*[r['result_path'] for r in self.reviews]],
            review_sources=[str(self.source)],review_target=dict(request_id='author',result_path=str(self.author),
                artifact_hashes={str(self.author):exchange.fingerprint(self.author)},scope='facts'),
            review_discussion=dict(packet_path=str(self.packet),packet_sha256=exchange.fingerprint(self.packet),round=1,prior_request_id='facts'))
        self.payload=dict(checks=[dict(criterion_id='C',status='failed')],conclusion_checks=[dict(criterion_id='C',pointer='/summary',status='failed')],
            discussion_checks=[dict(issue_id='I-1',position='revise',status='resolved',note='Primary not_run contradicts product success',
                                    source_refs=[dict(path=str(self.source),quote='"product_run": "not_run"')])])

    def test_evidence_bound_change_is_valid(self):
        self.assertEqual([],validate_checks(self.payload,'changes_requested'))
        check_binding(self.payload,self.assignment)

    def test_consensus_without_evidence_is_invalid(self):
        self.payload['discussion_checks'][0]['source_refs']=[]
        self.assertTrue(validate_checks(self.payload,'changes_requested'))

    def test_peer_verdict_cannot_be_evidence(self):
        self.payload['discussion_checks'][0]['source_refs']=[dict(path=self.reviews[1]['result_path'],quote='Synthetic inference')]
        with self.assertRaisesRegex(ValueError,'peer verdict'):
            check_binding(self.payload,self.assignment)

    def test_invented_quote_cannot_change_position(self):
        self.payload['discussion_checks'][0]['source_refs'][0]['quote']='Invented observation'
        with self.assertRaisesRegex(ValueError,'quote is absent'):
            check_binding(self.payload,self.assignment)

    def test_changed_prior_review_is_rejected(self):
        Path(self.reviews[0]['result_path']).write_text('Changed prior vote')
        with self.assertRaisesRegex(ValueError,'prior review changed'):
            check_binding(self.payload,self.assignment)

    def test_packet_mutation_or_wrong_round_target_scope_is_rejected(self):
        for field,value in [('round',2),('prior_request_id','inference'),('packet_sha256','0'*64)]:
            with self.subTest(field=field):
                assignment=copy.deepcopy(self.assignment)
                assignment['review_discussion'][field]=value
                with self.assertRaises(ValueError):check_binding(self.payload,assignment)

    def test_change_requires_revise_and_unchanged_requires_maintain(self):
        self.payload['discussion_checks'][0]['position']='maintain'
        with self.assertRaisesRegex(ValueError,'prior verdict'):check_binding(self.payload,self.assignment)
        self.payload['discussion_checks'][0]['position']='revise'
        self.payload['checks'][0]['status']='passed'
        self.payload['conclusion_checks'][0]['status']='passed'
        with self.assertRaisesRegex(ValueError,'prior verdict'):check_binding(self.payload,self.assignment)

    def test_all_disputes_and_only_assigned_disputes_required(self):
        for checks in ([],[dict(self.payload['discussion_checks'][0],issue_id='unknown')]):
            payload=copy.deepcopy(self.payload)
            payload['discussion_checks']=checks
            with self.assertRaisesRegex(ValueError,'every assigned'):check_binding(payload,self.assignment)

    def test_undisputed_check_cannot_change(self):
        prior=exchange.read_json(self.reviews[0]['result_path'])
        prior['payload']['conclusion_checks'].append(dict(criterion_id='C',pointer='/payload/other',status='passed'))
        exchange.atomic_write(self.reviews[0]['result_path'],prior)
        self.content['reviews'][0]['artifact_hashes'][self.reviews[0]['result_path']]=exchange.fingerprint(self.reviews[0]['result_path'])
        exchange.atomic_write(self.packet,self.content)
        self.assignment['review_discussion']['packet_sha256']=exchange.fingerprint(self.packet)
        self.payload['conclusion_checks'].append(dict(criterion_id='C',pointer='/payload/other',status='failed'))
        with self.assertRaisesRegex(ValueError,'undisputed'):check_binding(self.payload,self.assignment)

    def test_passing_unresolved_and_duplicate_issue_are_rejected(self):
        self.payload['discussion_checks'][0]['status']='unresolved'
        self.assertTrue(validate_checks(self.payload,'review_passed'))
        self.payload['discussion_checks'].append(copy.deepcopy(self.payload['discussion_checks'][0]))
        self.assertTrue(any('unique' in e for e in validate_checks(self.payload,'blocked')))

    def test_fabricated_runtime_receipt_is_rejected(self):
        self.payload['discussion_checks'][0].update(source_refs=[],receipt=str(self.source))
        with self.assertRaisesRegex(ValueError,'managed reviewer policy'):check_binding(self.payload,self.assignment)

    def test_initial_votes_and_later_rounds_cannot_be_mixed(self):
        def row(scope,discussion=True):
            a=dict(review_target=dict(request_id='author',scope=scope))
            if discussion:a['review_discussion']=dict(packet_path=str(self.packet))
            return dict(assignment=a)
        state=dict(requests=dict(facts=row('facts'),inference=row('inference')))
        check_cohort(state,dict(facts={},inference={}), 'author')
        state['requests']['facts']=row('facts',False)
        with self.assertRaisesRegex(ValueError,'mix'):check_cohort(state,dict(facts={},inference={}), 'author')
        with self.assertRaisesRegex(ValueError,'both directions'):check_cohort(state,dict(inference={}), 'author')

    def test_round_limit_and_role_count_are_strict(self):
        reviewers=[dict(agent='qa-conclusion-reviewer')]*2
        self.assertIsNone(configuration(None,reviewers))
        self.assertEqual(dict(max_rounds=2),configuration({},reviewers))
        for value in (dict(max_rounds=True),dict(max_rounds=0),dict(max_rounds=3),dict(max_rounds=1,extra=True)):
            with self.assertRaises(ValueError):configuration(value,reviewers)
        for roles in (reviewers[:1],[dict(agent='qa-result-reviewer'),reviewers[0]]):
            with self.assertRaises(ValueError):configuration({},roles)


class DiscussionCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'demo'
        self.cycle=self.root/'cycle.json'
        self.addCleanup(self.stop)

    def stop(self):
        if self.cycle.exists():
            try:review_cycle.cancel(self.cycle,'Synthetic cleanup')
            except Exception:pass

    def start(self,mode='resolve',**extra):
        plan=demo.build(self.root,mode)
        plan.update(extra)
        review_cycle.initialize(self.cycle,plan,self.root)
        return plan

    def run_cycle(self,deadline=15):
        end=time.monotonic()+deadline
        while time.monotonic()<end:
            state=review_cycle.tick(self.cycle)
            if state['status'] in ('accepted','blocked'):return state
            time.sleep(.05)
        self.fail('Discussion cycle timeout')

    def assess(self,state):
        registry=self.root/'task.json'
        exchange.assess_criteria(registry,dict(assessments=[dict(criterion_id='C-CONCLUSION',status='verified',decision='Synthetic evidence checked',
            request_ids=[state['result_request_id'],*state['review_request_ids']],evidence=[str(self.root/'primary-facts.json')])]),None)
        exchange.finish_task(registry,'passed','Synthetic complete; no model evaluation',None)

    def test_disagreement_is_discussed_then_author_corrected_and_both_scopes_pass(self):
        self.start()
        state=self.run_cycle()
        self.assertEqual('accepted',state['status'],state.get('reason'))
        self.assertEqual(1,state['round'])
        log=state['discussions']['0']
        self.assertEqual('resolved',log['status'])
        self.assertEqual(1,len(log['rounds']))
        registry=exchange.read_state(self.root/'task.json')
        initial=registry['requests']['author.review-facts']
        final=registry['requests']['author.review-facts.discussion-1']
        self.assertEqual('review_passed',initial['result']['event'])
        self.assertEqual('changes_requested',final['result']['event'])
        self.assertNotEqual(initial['owner_task_id'],final['owner_task_id'])
        self.assertEqual('changes_requested',registry['requests']['author']['status'])
        self.assess(state)

    def test_agreement_does_not_start_discussion(self):
        self.start('agree')
        state=self.run_cycle()
        self.assertEqual('accepted',state['status'])
        self.assertNotIn('discussions',state)
        self.assertFalse(any(c['kind']=='discussion' for c in state['children']))

    def test_two_rounds_can_resolve_without_creating_third(self):
        self.start('round-two')
        state=self.run_cycle()
        self.assertEqual('accepted',state['status'],state.get('reason'))
        self.assertEqual(2,len(state['discussions']['0']['rounds']))
        self.assertEqual({1,2},{c['discussion_round'] for c in state['children'] if c['kind']=='discussion'})

    def test_unresolved_stops_and_resume_does_not_reset_rounds(self):
        self.start('unresolved')
        state=self.run_cycle()
        self.assertEqual('blocked',state['status'])
        self.assertIn('unresolved disagreement',state['reason'])
        self.assertEqual(2,len(state['discussions']['0']['rounds']))
        count=len(state['children'])
        with self.assertRaisesRegex(ValueError,'blocked requires new evidence'):
            review_cycle.resume(self.cycle,'Explicit synthetic resume without new evidence')
        state=review_cycle.tick(self.cycle)
        self.assertEqual('blocked',state['status'])
        self.assertEqual(count,len(state['children']))

    def test_new_runtime_receipts_can_support_discussion_with_own_policy(self):
        plan=demo.build(self.root,'receipt')
        store=self.root/'probe-store.json'
        policy=dict(schema_version=1,server_command=[sys.executable,str(ROOT/'tests/fixtures/action_server.py'),'--store',str(store)],
            call_timeout_seconds=2,max_review_calls=1,tools={'find_ticket':dict(effect='read',
            constraints={'/project':'SYNTHETIC','/client_id':'discussion'},allowed_arguments=['project','client_id'])})
        for slot in plan['reviewers']:slot['gateway_policy']=copy.deepcopy(policy)
        review_cycle.initialize(self.cycle,plan,self.root)
        state=self.run_cycle()
        self.assertEqual('accepted',state['status'],state.get('reason'))
        registry=exchange.read_state(self.root/'task.json')
        from review_execution import check_receipt
        for rid in state['discussions']['0']['rounds'][0]['review_ids']:
            row=registry['requests'][rid]
            message=exchange.read_json(row['result']['path'])
            check=message['payload']['discussion_checks'][0]
            receipt=check_receipt(check['receipt'],row['assignment'])
            self.assertEqual(rid,receipt['context']['request_id'])
            self.assertTrue(receipt['fresh'])
            self.assertIn(check['receipt'],row['result']['artifact_hashes'])
        self.assertEqual(2,exchange.read_json(store)['reads'])
        self.assess(state)

    def test_one_round_limit_is_honored(self):
        self.start('round-two',discussion=dict(max_rounds=1))
        state=self.run_cycle()
        self.assertEqual('blocked',state['status'])
        self.assertEqual(1,len(state['discussions']['0']['rounds']))

    def test_peer_consensus_cannot_erase_original_blocking_review(self):
        self.start('false-alarm',max_corrections=0)
        state=self.run_cycle()
        self.assertEqual('blocked',state['status'])
        registry=exchange.read_state(self.root/'task.json')
        self.assertEqual('changes_requested',registry['requests']['author.review-inference']['result']['event'])
        self.assertEqual('review_passed',registry['requests']['author.review-inference.discussion-1']['result']['event'])
        self.assertEqual('changes_requested',registry['requests']['author']['status'])

    def test_unknown_can_be_clarified_by_primary_evidence(self):
        self.start('unknown')
        state=self.run_cycle()
        self.assertEqual('accepted',state['status'],state.get('reason'))
        self.assertEqual(0,state['round'])
        self.assertTrue(all('.discussion-1' in rid for rid in state['review_request_ids']))
        self.assess(state)

    def test_consensus_without_primary_evidence_never_accepts(self):
        for mode in ('no-evidence','peer-evidence'):
            with self.subTest(mode=mode):
                if self.root.exists():
                    self.stop()
                    import shutil
                    shutil.rmtree(self.root)
                self.start(mode)
                state=self.run_cycle()
                self.assertEqual('blocked',state['status'])
                self.assertIsNone(state.get('result_request_id'))

    def test_cancel_during_discussion_is_durable(self):
        self.start('hold')
        end=time.monotonic()+10
        while time.monotonic()<end:
            state=review_cycle.tick(self.cycle)
            if any(c['kind']=='discussion' for c in state['children']):break
            time.sleep(.05)
        review_cycle.cancel(self.cycle,'Explicit synthetic cancellation')
        for _ in range(2):self.assertTrue(review_cycle.tick(self.cycle)['cancel_requested'])
        self.assertEqual('cancelled',review_cycle.read(self.cycle)['status'])

    def test_replaying_tick_does_not_replace_packet_or_create_new_children(self):
        self.start()
        state=self.run_cycle()
        hashes={r['packet_path']:exchange.fingerprint(r['packet_path']) for log in state['discussions'].values() for r in log['rounds']}
        children=len(state['children'])
        for _ in range(3):review_cycle.tick(self.cycle)
        self.assertEqual(children,len(review_cycle.read(self.cycle)['children']))
        self.assertTrue(all(exchange.fingerprint(p)==h for p,h in hashes.items()))

    def test_crash_after_discussion_child_intent_reuses_same_packet_and_child(self):
        self.start()
        original=supervisor.initialize
        interrupted=False
        def initialize(path,plan,base):
            nonlocal interrupted
            if plan['steps'][0]['step_id'].startswith('discussion-') and not interrupted:
                interrupted=True
                raise RuntimeError('Synthetic crash after saved discussion intent')
            return original(path,plan,base)
        deadline=time.monotonic()+10
        with patch.object(supervisor,'initialize',side_effect=initialize):
            while time.monotonic()<deadline:
                try:review_cycle.tick(self.cycle)
                except RuntimeError:break
                time.sleep(.05)
        self.assertTrue(interrupted)
        intent=next(c for c in review_cycle.read(self.cycle)['children'] if c['kind']=='discussion')
        self.assertFalse(Path(intent['path']).exists())
        packet=intent['plan']['steps'][0]['assignment']['review_discussion']['packet_path']
        digest=exchange.fingerprint(packet)
        state=self.run_cycle()
        self.assertEqual('accepted',state['status'],state.get('reason'))
        self.assertEqual(digest,exchange.fingerprint(packet))
        self.assertEqual(1,sum(c['path']==intent['path'] for c in state['children']))

    def test_cli_blind_phase_hides_feedback_but_keeps_target_hash(self):
        self.start()
        state=self.run_cycle()
        child=next(c for c in state['children'] if c['round']==1 and c['kind']=='review')
        execution=supervisor.read(child['path'])
        step=execution['plan']['steps'][0]
        execution['plan']['adapter']=dict(kind='gigacode',executable='synthetic-cli')
        directory=self.root/'mock-cli'
        directory.mkdir()
        exchange.atomic_write(directory/'handoff.json',dict(assignment=step['assignment'],sources=[],answer_sources=[]))
        flags='--bare --exclude-tools --append-system-prompt --output-format --max-wall-time --max-session-turns'
        with patch.object(execution_worker.shutil,'which',return_value='/synthetic/cli'),patch.object(execution_worker.subprocess,'run',
            return_value=subprocess.CompletedProcess([],0,stdout=flags,stderr='')):
            execution_worker.command(execution,step,dict(run_dir=str(directory)),child['path'])
        prompt=exchange.read_json(directory/'prompt.json')
        feedback=step['assignment']['blind_review_exclusions'][0]
        self.assertIn(feedback,step['assignment']['review_target']['artifact_hashes'])
        self.assertIn('excluded',prompt['input_contents'][feedback]['unavailable'])
        self.assertNotIn('original_reviews',json.dumps(prompt))


if __name__=='__main__':unittest.main()
