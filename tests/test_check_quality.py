import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import task_exchange as exchange
import review_cycle
import execution_supervisor as supervisor
from content_review import validate_review, check_required_reviews
from self_review_fixtures import review_for
from task_acceptance import criteria_packet


class QualityAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'task.json'
        self.proof = self.root / 'proof.txt'
        self.proof.write_text('Synthetic observed product result')
        self.spec = self.root / 'requirements.md'
        self.spec.write_text('Synthetic requirement')
        exchange.init_task(self.state, 'QA-1', 'Execution', dict(schema_version=1, criteria=[dict(
            criterion_id='C-1', description='Both scenarios executed', source=str(self.spec), required=True,
            kind='scenario_execution', scenario_ids=['TC-1', 'TC-2'], execution_context=dict(product_version='v1'))]))

    def feature(self, statuses=('passed', 'passed'), rid='feature', version='v1', scenario_ids=None):
        ids = scenario_ids or ['TC-1', 'TC-2']
        assignment = dict(task_id='QA-1', request_id=rid, agent='qa-feature-tester', stage='execution', goal='Synthetic execution',
            acceptance_criteria=['Both executed'], criterion_ids=['C-1'], scenario_ids=ids, stop_condition='Report',
            expected_artifact='QA JSON', result_path=str(self.root / (rid + '.json')), inputs=[], depends_on=[])
        exchange.assign(self.state, assignment, self.root, None)
        handoff = exchange.prepare_handoff(self.state, rid, None)['result']
        exchange.start_request(self.state, rid, 'author-' + rid, handoff['delivery_id'], None)
        rows = [dict(scenario_id=sid, environment_id='fixture', product_version=version, status=status,
            steps=['Synthetic action'] if status in ('passed', 'failed') else [], expected_source=str(self.spec),
            expected_result='Synthetic expectation', actual_result='Synthetic observation', evidence=[str(self.proof)])
                for sid, status in zip(ids, statuses)]
        message = dict(task_id='QA-1', request_id=rid, agent='qa-feature-tester', event='feature_test_complete',
            summary='Synthetic result', artifacts=[], blockers=[], self_review=review_for(),
            payload=dict(scenario_results=rows, uncovered_scenarios=[r['scenario_id'] for r in rows if r['status'] in ('blocked','not_run')], defects=[]))
        Path(assignment['result_path']).write_text(json.dumps(message))
        exchange.submit_result(self.state, rid, None)
        exchange.review_result(self.state, rid, 'Synthetic content accepted', True, None)
        return assignment

    def assess(self, rids, **extra):
        return exchange.assess_criteria(self.state, dict(assessments=[dict(criterion_id='C-1', status='verified',
            decision='Synthetic evidence checked', request_ids=rids, evidence=[str(self.proof)], **extra)]), None)

    def test_nested_evidence_and_local_expectation_are_hashed(self):
        self.feature()
        hashes = exchange.read_state(self.state)['requests']['feature']['result']['artifact_hashes']
        self.assertIn(str(self.proof), hashes)
        self.assertIn(str(self.spec), hashes)
        self.proof.write_text('Changed observation')
        with self.assertRaisesRegex(ValueError, 'изменились'):
            self.assess(['feature'])

    def test_missing_nested_file_cannot_be_submitted(self):
        self.proof.unlink()
        with self.assertRaises((OSError, ValueError)):
            self.feature()
        self.assertEqual('running',exchange.read_state(self.state)['requests']['feature']['status'])

    def test_product_failure_is_execution_but_blocked_and_not_run_are_not(self):
        for status in ('passed', 'failed', 'blocked', 'not_run'):
            rid = 'feature-' + status
            self.feature(('passed', status), rid=rid)
            if status in ('passed', 'failed'):
                self.assess([rid])
            else:
                with self.assertRaisesRegex(ValueError, 'not executed'):
                    self.assess([rid])

    def test_continuations_cover_exact_ids_and_context(self):
        self.feature(('passed',), rid='first', scenario_ids=['TC-1'])
        self.feature(('failed',), rid='second', scenario_ids=['TC-2'])
        self.assess(['first', 'second'])
        self.feature(('passed',), rid='other-build', version='v2', scenario_ids=['TC-2'])
        with self.assertRaisesRegex(ValueError, 'context'):
            self.assess(['first', 'other-build'])

    def test_repeated_scenario_needs_explicit_source_selection(self):
        self.feature(rid='first')
        self.feature(rid='second')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.assess(['first', 'second'])
        self.assess(['first', 'second'], scenario_sources={'TC-1':'second', 'TC-2':'second'})
        exchange.finish_task(self.state, 'passed', 'Synthetic completed', None)

    def test_partial_then_continuation_can_finish_but_not_reuse_not_run(self):
        self.feature(('passed','not_run'), rid='first')
        self.feature(('passed',), rid='second', scenario_ids=['TC-2'])
        with self.assertRaisesRegex(ValueError,'not executed'):
            self.assess(['first','second'],scenario_sources={'TC-1':'first','TC-2':'first'})
        self.assess(['first','second'],scenario_sources={'TC-1':'first','TC-2':'second'})
        exchange.finish_task(self.state,'passed','Synthetic continuation complete',None)

    def test_finish_rechecks_evidence_after_assessment(self):
        self.feature()
        self.assess(['feature'])
        self.proof.write_text('Changed after assessment')
        with self.assertRaises(ValueError):exchange.finish_task(self.state,'passed','Synthetic',None)

    def test_typed_definition_rejects_missing_ids_and_unknown_context(self):
        for extra in ({'scenario_ids':[]}, {'scenario_ids':['TC-1','TC-1']},
                      {'scenario_ids':['TC-1'], 'execution_context':{'invented':'v1'}}):
            with self.assertRaises(ValueError):
                criteria_packet(dict(schema_version=1, criteria=[dict(criterion_id='C', description='d', source='s',
                    required=True, kind='scenario_execution', **extra)]))


class ReviewCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.registry, self.cycle = self.root / 'task.json', self.root / 'cycle.json'
        exchange.init_task(self.registry, 'QA-1', 'Synthetic review', dict(schema_version=1, criteria=[dict(
            criterion_id='C-1', description='Assertion detects foreign rows', source='Synthetic fixture requirement', required=True,
            required_reviews=[dict(scope='oracle', agent='qa-result-reviewer'), dict(scope='execution',agent='qa-autotest-reviewer')])]))
        self.addCleanup(self.stop)

    def stop(self):
        if self.cycle.exists():
            try:
                review_cycle.cancel(self.cycle, 'Test cleanup')
            except Exception:
                pass

    def adapter(self, mode):
        return dict(kind='command', argv=[sys.executable, str(ROOT/'tests/fixtures/review_agent.py'), '--mode', mode])

    def plan(self, mode='correct-once', review_mode='strong', **extra):
        assignment = dict(task_id='QA-1',request_id='auto',agent='qa-test-automation',stage='automation',
            goal='Check own paid filter',acceptance_criteria=['Return order 11, never foreign rows'],criterion_ids=['C-1'],
            scenario_ids=['TC-1'],stop_condition='Return result',expected_artifact='QA JSON',
            result_path=str(self.root/'author.json'),inputs=[],depends_on=[])
        return dict(schema_version=1,author_plan=dict(schema_version=1,task_state=str(self.registry),workspace=str(self.root),
            scope_kind='synthetic',adapter=self.adapter(mode),steps=[dict(step_id='author',assignment=assignment,max_wall_seconds=5)]),
            reviewers=[dict(scope='oracle',agent='qa-result-reviewer',adapter=self.adapter(review_mode),max_wall_seconds=5),
                       dict(scope='execution',agent='qa-autotest-reviewer',adapter=self.adapter(review_mode),max_wall_seconds=5)], **extra)

    def wait(self, predicate, timeout=20):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            state=review_cycle.tick(self.cycle)
            if predicate(state):return state
            time.sleep(.05)
        self.fail('Timed out: '+json.dumps(review_cycle.read(self.cycle)))

    def accepted(self, mode='correct-once'):
        review_cycle.initialize(self.cycle,self.plan(mode),self.root)
        state=self.wait(lambda s:s['status'] in ('accepted','blocked'))
        self.assertEqual('accepted',state['status'],state.get('reason'))
        return state

    def assess(self, state, rids=None):
        rid=state['result_request_id']
        target=exchange.read_state(self.registry)['requests'][rid]
        exchange.assess_criteria(self.registry,dict(assessments=[dict(criterion_id='C-1',status='verified',
            decision='Independent source and execution reviews passed',request_ids=rids or [rid,*state['review_request_ids']],
            evidence=[target['result']['path']])]),None)

    def test_weak_assert_is_corrected_and_two_scopes_accept_exact_revision(self):
        state=self.accepted()
        self.assertEqual(1,state['round'])
        self.assertEqual('changes_requested',exchange.read_state(self.registry)['requests']['auto']['status'])
        self.assertEqual(2,len(state['review_request_ids']))
        self.assess(state)
        exchange.finish_task(self.registry,'passed','Synthetic artifact accepted',None)
        self.assertEqual('completed',exchange.read_state(self.registry)['execution_status'])

    def test_strong_assert_does_not_trigger_unnecessary_correction(self):
        state=self.accepted('strong')
        self.assertEqual(0,state['round'])
        self.assertEqual(3,len(state['children']))

    def test_missing_second_review_cannot_verify_criterion(self):
        state=self.accepted('strong')
        with self.assertRaisesRegex(ValueError,'missing independent'):
            self.assess(state,[state['result_request_id'],state['review_request_ids'][0]])

    def test_same_owner_cannot_fill_two_required_directions(self):
        state=self.accepted('strong')
        registry=exchange.read_state(self.registry)
        first,second=state['review_request_ids']
        registry['requests'][second]['owner_task_id']=registry['requests'][first]['owner_task_id']
        exchange.atomic_write(self.registry,registry)
        with self.assertRaisesRegex(ValueError,'missing independent'):
            self.assess(state)

    def test_identical_findings_stop_without_unbounded_retries(self):
        review_cycle.initialize(self.cycle,self.plan('always-weak'),self.root)
        state=self.wait(lambda s:s['status']=='blocked')
        self.assertEqual(1,state['round'])
        self.assertIn('identical findings',state['reason'])
        before=len(state['children'])
        for _ in range(3):review_cycle.tick(self.cycle)
        self.assertEqual(before,len(review_cycle.read(self.cycle)['children']))

    def test_zero_correction_budget_preserves_rejected_result(self):
        review_cycle.initialize(self.cycle,self.plan(max_corrections=0),self.root)
        state=self.wait(lambda s:s['status']=='blocked')
        self.assertEqual(0,state['round'])
        self.assertEqual('changes_requested',exchange.read_state(self.registry)['requests']['auto']['status'])

    def test_unknown_review_is_blocked_not_autofixed_or_passed(self):
        review_cycle.initialize(self.cycle,self.plan('strong',review_mode='unknown'),self.root)
        state=self.wait(lambda s:s['status']=='blocked')
        self.assertIn('unknown expectation',state['reason'])
        self.assertEqual(0,state['round'])
        self.assertEqual('reported',exchange.read_state(self.registry)['requests']['auto']['status'])

    def test_cancel_is_durable_and_explicit_resume_preserves_limits(self):
        review_cycle.initialize(self.cycle,self.plan('strong'),self.root)
        review_cycle.cancel(self.cycle,'User cancelled before launch')
        for _ in range(2):self.assertTrue(review_cycle.tick(self.cycle)['cancel_requested'])
        self.assertEqual([],review_cycle.read(self.cycle)['children'])
        review_cycle.resume(self.cycle,'User explicitly resumes')
        state=self.wait(lambda s:s['status'] in ('accepted','blocked'))
        self.assertEqual('accepted',state['status'],state.get('reason'))
        self.assertEqual(2,state['plan']['max_corrections'])

    def test_target_change_after_review_blocks_assessment(self):
        state=self.accepted('strong')
        registry=exchange.read_state(self.registry)
        rid=state['result_request_id']
        message=json.loads(Path(registry['requests'][rid]['result']['path']).read_text())
        Path(message['payload']['changed_files'][0]).write_text('Changed after independent review')
        with self.assertRaises(ValueError):self.assess(state)

    def test_nested_reviewer_proof_is_hashed_and_rechecked(self):
        state=self.accepted('strong')
        registry=exchange.read_state(self.registry)
        row=registry['requests'][state['review_request_ids'][0]]
        message=json.loads(Path(row['result']['path']).read_text())
        proof=message['payload']['checks'][0]['evidence'][0]
        self.assertIn(proof,row['result']['artifact_hashes'])
        Path(proof).write_text('Changed reviewer observation')
        with self.assertRaises(ValueError):self.assess(state)

    def test_crash_after_rejection_replays_saved_correction(self):
        review_cycle.initialize(self.cycle,self.plan(),self.root)
        real=exchange.review_result
        def crash(path,rid,decision,accept,revision):
            result=real(path,rid,decision,accept,revision)
            if rid=='auto' and not accept:raise RuntimeError('Simulated controller crash')
            return result
        with patch.object(exchange,'review_result',side_effect=crash):
            with self.assertRaisesRegex(RuntimeError,'controller crash'):
                self.wait(lambda s:s['status']=='accepted')
        self.assertIn('correction_intent',review_cycle.read(self.cycle))
        state=self.wait(lambda s:s['status'] in ('accepted','blocked'))
        self.assertEqual('accepted',state['status'],state.get('reason'))
        self.assertEqual(1,state['round'])

    def test_cancel_and_resume_replay_after_partial_child_updates(self):
        self.accepted('strong')
        cancel=supervisor.cancel
        calls=[]
        def crash_cancel(path,reason):
            result=cancel(path,reason); calls.append(path)
            if len(calls)==1:raise RuntimeError('Stop interrupted')
            return result
        with patch.object(supervisor,'cancel',side_effect=crash_cancel):
            with self.assertRaises(RuntimeError):review_cycle.cancel(self.cycle,'Explicit test stop')
        review_cycle.tick(self.cycle)
        self.assertTrue(all(supervisor.read(c['path'])['cancel_requested'] for c in review_cycle.read(self.cycle)['children']))
        resume=supervisor.resume
        calls=[]
        def crash_resume(path,decision):
            result=resume(path,decision); calls.append(path)
            if len(calls)==1:raise RuntimeError('Resume interrupted')
            return result
        with patch.object(supervisor,'resume',side_effect=crash_resume):
            with self.assertRaises(RuntimeError):review_cycle.resume(self.cycle,'Explicit resume')
        review_cycle.tick(self.cycle)
        self.assertFalse(review_cycle.read(self.cycle)['cancel_requested'])
        self.assertFalse(any(supervisor.read(c['path'])['cancel_requested'] for c in review_cycle.read(self.cycle)['children']))

    def test_worker_obeys_parent_budget_without_controller_ticks(self):
        review_cycle.initialize(self.cycle,self.plan('hold',max_active_seconds=1),self.root)
        state=review_cycle.tick(self.cycle)
        child=state['children'][0]
        self.assertLessEqual(supervisor.read(child['path'])['plan']['steps'][0]['max_wall_seconds'],1)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            progress=supervisor.read(child['path'])
            attempts=next(iter(progress['steps'].values()))['attempts']
            if attempts and attempts[-1]['status'] in supervisor.TERMINAL:break
            time.sleep(.05)
        else:self.fail('Worker remained active without controller beyond budget')
        self.assertEqual('timeout',attempts[-1]['category'])

    def test_new_cancel_fences_interrupted_resume(self):
        self.accepted('strong')
        review_cycle.cancel(self.cycle,'First stop')
        resume=supervisor.resume
        def crash(path,decision):
            resume(path,decision)
            raise RuntimeError('Interrupted resume')
        with patch.object(supervisor,'resume',side_effect=crash):
            with self.assertRaises(RuntimeError):review_cycle.resume(self.cycle,'Resume first stop')
        review_cycle.cancel(self.cycle,'New explicit stop')
        state=review_cycle.tick(self.cycle)
        self.assertTrue(state['cancel_requested'])
        self.assertNotIn('resume_intent',state)
        self.assertTrue(all(supervisor.read(c['path'])['cancel_requested'] for c in state['children']))

    def test_cancel_resume_preserves_interrupted_correction(self):
        review_cycle.initialize(self.cycle,self.plan(),self.root)
        real=exchange.review_result
        def crash(path,rid,decision,accept,revision):
            result=real(path,rid,decision,accept,revision)
            if rid=='auto' and not accept:raise RuntimeError('Interrupted correction')
            return result
        with patch.object(exchange,'review_result',side_effect=crash):
            with self.assertRaises(RuntimeError):self.wait(lambda s:s['status']=='accepted')
        review_cycle.cancel(self.cycle,'Stop before correction')
        review_cycle.resume(self.cycle,'Explicitly continue correction')
        state=self.wait(lambda s:s['status'] in ('accepted','blocked'))
        self.assertEqual('accepted',state['status'],state.get('reason'))
        self.assertEqual(1,state['round'])

    def test_frozen_cycle_policy_cannot_be_relaxed_on_resume(self):
        review_cycle.initialize(self.cycle,self.plan(),self.root)
        state=review_cycle.read(self.cycle)
        state['plan']['max_corrections']=5
        exchange.atomic_write(self.cycle,state)
        with self.assertRaisesRegex(ValueError,'changed review'):
            review_cycle.tick(self.cycle)

    def test_missing_required_scope_is_rejected_before_execution(self):
        plan=self.plan()
        plan['reviewers'].pop()
        with self.assertRaises(ValueError):review_cycle.initialize(self.cycle,plan,self.root)
        self.assertFalse(self.cycle.exists())

    def test_review_passed_cannot_hide_unknown_or_blocking_findings(self):
        payload=dict(target_request_id='a',target_sha256='a'*64,review_scope='source',
            checks=[dict(criterion_id='C',status='unknown',note='Missing source',evidence=['source'])],
            findings=[],limitations=['Unavailable'])
        self.assertTrue(validate_review(payload,'review_passed'))
        payload['checks'][0]['status']='passed'
        payload['findings']=[dict(finding_id='F',criterion_id='C',blocking=True,problem='p',evidence='e',resolution_condition='r')]
        self.assertTrue(validate_review(payload,'review_passed'))


class ReviewPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.definition=dict(criterion_id='C',required_reviews=[dict(scope='oracle',agent='qa-result-reviewer'),
            dict(scope='execution',agent='qa-autotest-reviewer')])
        self.state=dict(requests={})
        self.results={}
        for rid in ('A','B'):
            self.state['requests'][rid]=dict(status='accepted',assignment=dict(agent='qa-feature-tester'),owner_task_id='author-'+rid)
            self.results[rid]={}

    def review(self,rid,author,scope,owner,event='review_passed'):
        p=self.root/(rid+'.json')
        p.write_text(json.dumps(dict(task_id='T',request_id=rid,agent='qa-result-reviewer',summary='Fixture',artifacts=[],blockers=[],
            event=event,payload=dict(checks=[dict(criterion_id='C',status='passed')]))))
        assignment=dict(agent='qa-result-reviewer' if scope=='oracle' else 'qa-autotest-reviewer',
            review_target=dict(request_id=author,scope=scope))
        self.state['requests'][rid]=dict(status='accepted',assignment=assignment,owner_task_id=owner,result=dict(event=event))
        self.results[rid]=dict(path=str(p))

    def test_each_selected_author_needs_all_directions(self):
        self.review('ao','A','oracle','r1');self.review('ae','A','execution','r2')
        with self.assertRaisesRegex(ValueError,'B/oracle'):
            check_required_reviews(self.definition,self.state,self.results,{'A','B'})
        self.review('bo','B','oracle','r1');self.review('be','B','execution','r2')
        check_required_reviews(self.definition,self.state,self.results,{'A','B'})

    def test_directions_cannot_be_split_between_targets(self):
        self.review('ao','A','oracle','r1');self.review('be','B','execution','r2')
        with self.assertRaisesRegex(ValueError,'missing independent'):
            check_required_reviews(self.definition,self.state,self.results)

    def test_matching_search_finds_valid_nonfirst_pair(self):
        self.review('ao1','A','oracle','r1');self.review('ao2','A','oracle','r2')
        self.review('ae','A','execution','r1')
        check_required_reviews(self.definition,self.state,self.results,{'A'})

    def test_blocking_review_outside_selected_results_cannot_be_outvoted(self):
        self.review('ao','A','oracle','r1');self.review('ae','A','execution','r2')
        self.review('rejected','A','oracle','r3','changes_requested')
        self.results.pop('rejected')
        with self.assertRaisesRegex(ValueError,'Unresolved blocking'):
            check_required_reviews(self.definition,self.state,self.results,{'A'})


if __name__=='__main__':unittest.main()
