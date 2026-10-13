"""Conclusion inventory, independent sources, exact revisions and bounded correction."""
import copy
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import task_exchange as exchange
import review_cycle
from conclusion_review import pointer, validate_checks
from qa_contract import validate_message
from task_acceptance import criteria_packet
from self_review_fixtures import review_for

spec = importlib.util.spec_from_file_location('conclusion_demo', ROOT / 'examples/conclusion-review/build_demo.py')
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class ConclusionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'demo'
        self.plan = demo.build(self.root)
        self.state = self.root / 'task.json'
        self.source = self.root / 'primary-facts.json'
        self.cycle = self.root / 'cycle.json'
        self.addCleanup(self.stop)

    def stop(self):
        if self.cycle.exists():
            try:
                review_cycle.cancel(self.cycle, 'Synthetic test cleanup')
            except (OSError, ValueError, KeyError):
                pass

    def start(self, assignment, owner):
        exchange.assign(self.state, assignment, self.root, None)
        handoff = exchange.prepare_handoff(self.state, assignment['request_id'], None)['result']
        exchange.start_request(self.state, assignment['request_id'], owner, handoff['delivery_id'], None)

    def author(self):
        assignment = self.plan['author_plan']['steps'][0]['assignment']
        self.start(assignment, 'author-owner')
        message = dict(task_id=assignment['task_id'], request_id='author', agent=assignment['agent'], event='environment_ready',
            summary='Продуктовый запуск не выполнялся.', artifacts=[], blockers=[], self_review=review_for(),
            payload=dict(environment_id='fixture', product_version='fixture-v1', verified_capabilities=['Synthetic read'], limitations=['Synthetic only']))
        exchange.atomic_write(assignment['result_path'], message)
        exchange.submit_result(self.state, 'author', None)
        exchange.review_result(self.state, 'author', 'Synthetic content accepted', True, None)
        return assignment, message

    def reviewer(self, owner='independent-owner', event='review_passed', mutate=None):
        registry = exchange.read_state(self.state)
        result = registry['requests']['author']['result']
        assignment = dict(task_id=registry['task_id'], request_id='review', agent='qa-conclusion-reviewer', stage='conclusions',
            goal='Check provided conclusions independently', acceptance_criteria=['Match primary source'], criterion_ids=['C-CONCLUSION'],
            stop_condition='Return review', expected_artifact='QA JSON', result_path=str(self.root/'review.json'),
            inputs=list(result['artifact_hashes']), depends_on=[], review_sources=[str(self.source)],
            review_target=dict(request_id='author', result_path=result['path'], artifact_hashes=result['artifact_hashes'], scope='conclusions'),
            review_criteria_ids=['C-CONCLUSION'], conclusion_paths={'C-CONCLUSION':['/summary']})
        message = dict(task_id=registry['task_id'], request_id='review', agent=assignment['agent'], event=event,
            summary='Synthetic conclusion review', artifacts=[], blockers=[], self_review=review_for(),
            payload=dict(target_request_id='author',target_sha256=result['artifact_hashes'][result['path']],review_scope='conclusions',
                reviewed_revision='author',checks=[dict(criterion_id='C-CONCLUSION',status='passed',note='Primary fact supports summary', evidence=[str(self.source)])],
                findings=[],limitations=['Synthetic fixture only'],conclusion_checks=[dict(criterion_id='C-CONCLUSION',pointer='/summary',
                    conclusion='Продуктовый запуск не выполнялся.',status='passed',note='Matches recorded not_run',
                    source_refs=[dict(path=str(self.source),quote='"product_run": "not_run"')])]))
        if mutate:
            mutate(assignment,message)
        self.start(assignment,owner)
        exchange.atomic_write(assignment['result_path'],message)
        exchange.submit_result(self.state,'review',None)
        exchange.review_result(self.state,'review','Independent result recorded',True,None)
        return assignment,message

    def assess(self,rids=('author','review')):
        return exchange.assess_criteria(self.state,dict(assessments=[dict(criterion_id='C-CONCLUSION',status='verified',
            decision='Primary fact and independent exact-value review checked',request_ids=list(rids),evidence=[str(self.source)])]),None)

    def run_cycle(self, mode=None):
        if mode:
            self.plan['reviewers'][0]['adapter']['argv'] += ['--mode',mode]
        review_cycle.initialize(self.cycle,self.plan,self.root)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            state=review_cycle.tick(self.cycle)
            if state['status'] in ('accepted','blocked'):
                return state
            time.sleep(.05)
        self.fail('Cycle timeout')

    def test_independent_supported_conclusion_can_assess_and_finish(self):
        self.author()
        assignment,_=self.reviewer()
        row=exchange.read_state(self.state)['requests']['review']
        self.assertIn(str(self.source),row['result']['artifact_hashes'])
        self.assertNotEqual(row['owner_task_id'],exchange.read_state(self.state)['requests']['author']['owner_task_id'])
        self.assess()
        exchange.finish_task(self.state,'passed','Synthetic conclusion accepted',None)

    def test_without_independent_review_assessment_is_rejected(self):
        self.author()
        with self.assertRaisesRegex(ValueError,'independent content review'):
            self.assess(['author'])

    def test_same_owner_cannot_accept_own_conclusion(self):
        self.author()
        with self.assertRaisesRegex(ValueError,'different owner/session'):
            self.reviewer(owner='author-owner')

    def test_generic_verdict_without_inventory_is_rejected(self):
        self.author()
        with self.assertRaisesRegex(ValueError,'conclusion_checks'):
            self.reviewer(mutate=lambda a,m:m['payload'].pop('conclusion_checks'))

    def test_exact_author_value_is_required(self):
        self.author()
        with self.assertRaisesRegex(ValueError,'exact author value'):
            self.reviewer(mutate=lambda a,m:m['payload']['conclusion_checks'][0].update(conclusion='Different assertion'))

    def test_unknown_and_failed_cannot_receive_passing_verdict(self):
        for status in ('failed','unknown'):
            with self.subTest(status=status):
                errors=validate_checks(dict(conclusion_checks=[dict(criterion_id='C',pointer='/summary',conclusion='s',
                    status=status,note='Unavailable',source_refs=[])]),'review_passed',required=True)
                self.assertTrue(any('unverified' in e for e in errors))

    def test_unsupported_source_and_invented_quote_rejected(self):
        self.author()
        other=self.root/'other.txt'
        other.write_text('Unassigned source')
        with self.assertRaisesRegex(ValueError,'assigned source'):
            self.reviewer(mutate=lambda a,m:m['payload']['conclusion_checks'][0]['source_refs'][0].update(path=str(other)))

    def test_quote_must_exist_in_source(self):
        self.author()
        with self.assertRaisesRegex(ValueError,'quote is absent'):
            self.reviewer(mutate=lambda a,m:m['payload']['conclusion_checks'][0]['source_refs'][0].update(quote='Invented primary fact'))

    def test_author_cannot_be_own_primary_source(self):
        self.author()
        with self.assertRaisesRegex(ValueError,'own primary source'):
            self.reviewer(mutate=lambda a,m:a.update(review_sources=[a['review_target']['result_path']]))

    def test_source_is_mandatory(self):
        self.author()
        with self.assertRaisesRegex(ValueError,'review_sources'):
            self.reviewer(mutate=lambda a,m:a.update(review_sources=[]))

    def test_source_hash_cannot_be_replaced_between_plan_and_assignment(self):
        self.author()
        with self.assertRaisesRegex(ValueError,'frozen hash'):
            self.reviewer(mutate=lambda a,m:a.update(review_source_hashes={str(self.source):'0'*64}))

    def test_source_mutation_after_assessment_blocks_finish(self):
        self.author()
        self.reviewer()
        self.assess()
        self.source.write_text('Changed source')
        with self.assertRaises(ValueError):
            exchange.finish_task(self.state,'passed','Synthetic stale source rejected',None)

    def test_author_mutation_invalidates_old_review(self):
        assignment,message=self.author()
        self.reviewer()
        message['summary']='Changed author assertion'
        exchange.atomic_write(assignment['result_path'],message)
        with self.assertRaises(ValueError):
            self.assess()

    def test_duplicate_claim_check_is_rejected(self):
        self.author()
        with self.assertRaisesRegex(ValueError,'unique'):
            self.reviewer(mutate=lambda a,m:m['payload']['conclusion_checks'].append(copy.deepcopy(m['payload']['conclusion_checks'][0])))

    def test_assigned_field_cannot_be_skipped(self):
        definition=exchange.read_state(self.state)['criteria']['C-CONCLUSION']['definition']
        definition['required_reviews'][0]['conclusions']=['/summary','/payload/product_version']
        exchange.set_criteria(self.state,dict(schema_version=1,criteria=[definition]),None)
        self.author()
        with self.assertRaisesRegex(ValueError,'agreed review criteria'):
            self.reviewer()

    def test_missing_second_check_is_rejected(self):
        definition=exchange.read_state(self.state)['criteria']['C-CONCLUSION']['definition']
        definition['required_reviews'][0]['conclusions']=['/summary','/payload/product_version']
        exchange.set_criteria(self.state,dict(schema_version=1,criteria=[definition]),None)
        self.author()
        with self.assertRaisesRegex(ValueError,'every assigned'):
            self.reviewer(mutate=lambda a,m:a.update(conclusion_paths={'C-CONCLUSION':['/summary','/payload/product_version']}))

    def test_criteria_reject_missing_summary_duplicates_bad_escape_or_other_role(self):
        base=exchange.read_state(self.state)['criteria']['C-CONCLUSION']['definition']
        for paths in ([],['/payload/run_result'],['/summary','/summary'],['/summary','/payload/bad~2']):
            definition=copy.deepcopy(base)
            definition['required_reviews'][0]['conclusions']=paths
            with self.assertRaises(ValueError):
                criteria_packet(dict(schema_version=1,criteria=[definition]))
        base['required_reviews'][0]['agent']='qa-result-reviewer'
        with self.assertRaises(ValueError):
            criteria_packet(dict(schema_version=1,criteria=[base]))

    def test_pointer_uses_json_array_indices_and_exact_escaped_object_keys(self):
        value={'a/b':{'~key':[True,1]}}
        self.assertEqual(1,pointer(value,'/a~1b/~0key/1'))
        for index in ('-1','01','-'):
            with self.assertRaises(ValueError):
                pointer(value,'/a~1b/~0key/'+index)

    def test_bounded_cycle_corrects_contradicted_conclusion_in_new_revision(self):
        result=self.run_cycle()
        self.assertEqual('accepted',result['status'],result.get('reason'))
        self.assertEqual(1,result['round'])
        registry=exchange.read_state(self.state)
        self.assertEqual('changes_requested',registry['requests']['author']['status'])
        self.assertEqual('changes_requested',registry['requests']['author.review-conclusions']['result']['event'])
        author=registry['requests'][result['result_request_id']]
        review=registry['requests'][result['review_request_ids'][0]]
        self.assertNotEqual(author['owner_task_id'],review['owner_task_id'])
        self.assertNotEqual(registry['requests']['author']['result']['artifact_hashes'],author['result']['artifact_hashes'])
        self.assess([result['result_request_id'],*result['review_request_ids']])
        exchange.finish_task(self.state,'passed','Corrected synthetic conclusion accepted',None)

    def test_cycle_cannot_accept_generic_verdict(self):
        result=self.run_cycle('generic')
        self.assertEqual('blocked',result['status'])
        self.assertIsNone(result.get('result_request_id'))

    def test_unknown_blocks_cycle_without_inventing_evidence(self):
        result=self.run_cycle('unknown')
        self.assertEqual('blocked',result['status'])
        self.assertIn('unknown',result['reason'])

    def test_cycle_source_frozen_before_launch(self):
        review_cycle.initialize(self.cycle,self.plan,self.root)
        self.source.write_text('Changed before launch')
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            state=review_cycle.tick(self.cycle)
            if state['status']=='blocked':
                break
            time.sleep(.05)
        self.assertEqual('blocked',state['status'])

    def test_cycle_requires_primary_sources_and_preserves_result_source(self):
        self.plan['reviewers'][0]['review_sources']=[]
        with self.assertRaisesRegex(ValueError,'review_sources'):
            review_cycle.initialize(self.cycle,self.plan,self.root)
        self.plan['reviewers'][0]['review_sources']=[str(self.source)]
        self.plan['author_plan']['steps'][0]['assignment']['result_path']=str(self.source)
        with self.assertRaises(ValueError):
            review_cycle.initialize(self.cycle,self.plan,self.root)


if __name__=='__main__':
    unittest.main()
