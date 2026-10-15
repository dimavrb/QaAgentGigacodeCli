"""Scope and merge regressions on protocol fixtures, not live author quality."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'),str(ROOT/'tests')]
import context_builder as cb
import build_test_plan_html as page
import execution_supervisor as supervisor
import partition_test_plan as partition
import review_cycle
import task_exchange as exchange
from self_review_fixtures import review_for


class PartitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.state = self.root/'task.json'
        for name in ('requirements.md','plan-v3.md','cases-v3.json'):
            (self.root/name).write_bytes((ROOT/'examples/test-plan'/name).read_bytes())
        self.baseline = json.loads((ROOT/'examples/test-plan/result-v3.json').read_text())
        self.baseline.update(task_id='QA-PART',request_id='scope',self_review=review_for())
        self.spec = dict(schema_version=1,plan_revision='v4',integration_request_id='integrated',groups=[
            dict(group_id='filter',request_id='plan-filter',test_type='ui',scenario_ids=['TC-001']),
            dict(group_id='login',request_id='plan-login',test_type='ui',scenario_ids=['TC-002'])])
        self.out = self.root/'v4'; self.manifest = self.out/'partition.json'
        self.criteria = None

    def report(self, assignment, message, accept=True):
        exchange.assign(self.state,assignment,self.root,None)
        handoff = exchange.prepare_handoff(self.state,assignment['request_id'],None)['result']
        exchange.start_request(self.state,assignment['request_id'],'fixture-'+assignment['request_id'],handoff['delivery_id'],None)
        exchange.atomic_write(assignment['result_path'],message)
        exchange.submit_result(self.state,assignment['request_id'],None)
        if accept:
            exchange.review_result(self.state,assignment['request_id'],'Synthetic content decision',True,None)

    def base(self, accepted=True):
        exchange.init_task(self.state,'QA-PART','Synthetic partition',self.criteria)
        assignment = dict(task_id='QA-PART',request_id='scope',agent='qa-test-plan-writer',stage='scope',goal='Both requirements',
            acceptance_criteria=['Both requirements covered'],stop_condition='Report',expected_artifact='QA JSON',
            result_path=str(self.root/'scope.json'),inputs=[str(self.root/'requirements.md')],depends_on=[])
        self.report(assignment,self.baseline,accepted)

    def build(self):
        self.base()
        return partition.build(self.state,'scope',self.spec,self.out)

    def group_message(self, name):
        assignment = partition.file_json(self.out/name/'assignment.json')
        context = partition.file_json(self.out/name/'context.json')
        cases = partition.file_json(self.root/'cases-v3.json')['test_cases']
        selected = [copy.deepcopy(c) for c in cases if c['scenario_id'] in assignment['scenario_ids']]
        for case in selected:
            old = case['requirement_source'].partition('#')[2]
            case['requirement_source'] = str(self.root/'requirements.md')+'#'+old
        catalog = dict(schema_version=1,test_cases=selected)
        (self.out/name/'plan.md').write_text('Synthetic group '+name)
        exchange.atomic_write(self.out/name/'catalog.json',catalog)
        coverage = [dict(row,status='ready') for row in context['requirement_coverage']]
        message = dict(task_id='QA-PART',request_id=assignment['request_id'],agent='qa-test-plan-writer',event='plan_ready',
            summary='Synthetic group',artifacts=['plan.md','catalog.json'],blockers=[],self_review=review_for(),
            payload=dict(base_revision=assignment['base_revision'],plan_revision=assignment['plan_revision'],plan_artifact='plan.md',test_cases_artifact='catalog.json',
                requirement_coverage=coverage,open_questions=context['open_questions']))
        return assignment,message

    def group(self,name,accepted=True):
        assignment,message = self.group_message(name)
        self.report(assignment,message,accepted)
        return assignment,message

    def ready(self):
        self.build(); self.group('filter'); self.group('login')

    def test_context_is_a_verified_requirement_projection_without_other_group_text(self):
        self.build()
        selected = partition.file_json(self.out/'filter/context.json')
        self.assertEqual(['R-1'],[r['requirement_id'] for r in selected['requirement_coverage']])
        body = json.dumps(selected,ensure_ascii=False)
        self.assertIn('ORDER-PAID',body)
        self.assertNotIn('Вход с неверным паролем отклоняется',body)
        assignment = exchange.assignment_record(partition.file_json(self.out/'filter/assignment.json'),self.root)
        prompt = cb.assemble(dict(assignment=assignment,sources=[],answer_sources=[]),{})
        self.assertEqual('filter',prompt['partition_scope']['group_id'])
        self.assertNotIn(str(self.root/'requirements.md'),prompt['input_contents'])
        self.assertIn('docs/TEST_PLAN_PARTITION.md',cb.profile(assignment,{})['documents'])

    def test_partition_requires_accepted_common_scope(self):
        self.base(False)
        with self.assertRaisesRegex(ValueError,'accepted first'):
            partition.build(self.state,'scope',self.spec,self.out)
        self.assertFalse(self.out.exists())

    def test_duplicate_missing_and_extra_scenarios_are_rejected_before_writing(self):
        self.base()
        for groups in ([self.spec['groups'][0]],
                       [self.spec['groups'][0],dict(self.spec['groups'][1],scenario_ids=['TC-001'])],
                       [self.spec['groups'][0],dict(self.spec['groups'][1],scenario_ids=['TC-002','UNASSIGNED'])]):
            with self.subTest(groups=groups),self.assertRaises(ValueError):
                partition.build(self.state,'scope',dict(self.spec,groups=groups),self.out)
            self.assertFalse(self.out.exists())

    def test_cycle_and_unknown_group_dependency_are_rejected(self):
        self.base()
        for deps in [('login','filter'),('missing',None)]:
            spec = copy.deepcopy(self.spec); spec['groups'][0]['depends_on']=[deps[0]]
            if deps[1]: spec['groups'][1]['depends_on']=[deps[1]]
            with self.assertRaisesRegex(ValueError,'dependency'):
                partition.build(self.state,'scope',spec,self.out)

    def test_changed_primary_source_stops_dispatch_before_model_start(self):
        self.build()
        assignment = partition.file_json(self.out/'filter/assignment.json')
        exchange.assign(self.state,assignment,self.root,None)
        (self.root/'requirements.md').write_text('Changed primary requirement')
        with self.assertRaisesRegex(ValueError,'changed'):
            exchange.prepare_handoff(self.state,'plan-filter',None)
        self.assertEqual('queued',exchange.read_state(self.state)['requests']['plan-filter']['status'])

    def test_group_cannot_drop_or_replace_a_declared_dependency(self):
        self.spec['groups'][0]['depends_on'] = ['login']
        self.build()
        original = partition.file_json(self.out/'filter/assignment.json')
        for dependencies in ([], ['scope'], ['plan-login-other'], ['plan-login.revision-0']):
            with self.subTest(dependencies=dependencies):
                assignment = dict(original, depends_on=dependencies)
                with self.assertRaisesRegex(ValueError, 'required group dependency'):
                    exchange.assign(self.state,assignment,self.root,None)
                self.assertNotIn('plan-filter',exchange.read_state(self.state)['requests'])

    def test_group_requires_an_accepted_dependency_and_allows_an_explicit_revision(self):
        self.spec['groups'][0]['depends_on'] = ['login']
        self.build()
        assignment = partition.file_json(self.out/'filter/assignment.json')
        with self.assertRaisesRegex(ValueError, 'plan-login'):
            exchange.assign(self.state,assignment,self.root,None)
        dependency, message = self.group_message('login')
        dependency['request_id'] += '.revision-1'
        dependency['result_path'] = str(self.out/'login/result.revision-1.json')
        message['request_id'] = dependency['request_id']
        self.report(dependency,message)
        assignment['depends_on'] = [dependency['request_id']]
        exchange.assign(self.state,assignment,self.root,None)
        handoff = exchange.prepare_handoff(self.state,'plan-filter',None)['result']
        self.assertEqual([dependency['request_id']], [source['request_id'] for source in handoff['sources']])

    def test_changed_context_or_manifest_cannot_be_blessed_by_assignment(self):
        self.build(); assignment = partition.file_json(self.out/'filter/assignment.json')
        path = self.out/'filter/context.json'; original = path.read_bytes(); path.write_bytes(original+b' ')
        with self.assertRaisesRegex(ValueError,'context changed'):
            exchange.assignment_record(assignment,self.root)
        path.write_bytes(original)
        self.manifest.write_bytes(self.manifest.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'manifest changed'):
            exchange.assignment_record(assignment,self.root)

    def test_assignment_cannot_claim_another_scope_or_output(self):
        self.build(); source = partition.file_json(self.out/'filter/assignment.json')
        for patch in (dict(scenario_ids=['TC-002']),dict(result_path=str(self.root/'foreign.json')),dict(test_type='api')):
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                exchange.assignment_record(dict(source,**patch),self.root)

    def test_matching_task_id_does_not_allow_another_registry(self):
        self.build(); other=self.root/'other-task.json'
        exchange.init_task(other,'QA-PART','Another registry')
        assignment=partition.file_json(self.out/'filter/assignment.json')
        with self.assertRaisesRegex(ValueError,'another task registry'):
            exchange.assign(other,assignment,self.root,None)
        self.assertEqual({},exchange.read_state(other)['requests'])

    def test_group_submit_cannot_change_requirement_links_source_or_priority(self):
        self.build(); assignment,message = self.group_message('filter')
        exchange.assign(self.state,assignment,self.root,None)
        handoff = exchange.prepare_handoff(self.state,'plan-filter',None)['result']
        exchange.start_request(self.state,'plan-filter','fixture-filter',handoff['delivery_id'],None)
        for field,value in [('scenario_ids',['TC-002']),('source','made-up.md'),('priority','low')]:
            candidate = copy.deepcopy(message); candidate['payload']['requirement_coverage'][0][field]=value
            exchange.atomic_write(assignment['result_path'],candidate)
            with self.subTest(field=field),self.assertRaises(ValueError):
                exchange.submit_result(self.state,'plan-filter',None)

    def test_group_cannot_drop_shared_question(self):
        self.build(); assignment,message = self.group_message('login')
        self.assertTrue(message['payload']['open_questions'])
        message['payload']['open_questions']=[]
        with self.assertRaisesRegex(ValueError,'drops a shared question'):
            self.report(assignment,message)

    def test_group_cannot_write_another_catalog_or_publish_global_html(self):
        self.build(); assignment,message = self.group_message('filter')
        message['payload']['test_cases_artifact']=str(self.root/'cases-v3.json')
        with self.assertRaisesRegex(ValueError,'catalog path'):
            self.report(assignment,message)
        self.assertFalse((self.out/'filter/result.plan.html').exists())

    def test_direct_html_export_cannot_present_a_group_as_the_final_plan(self):
        self.build()
        assignment, message = self.group_message('filter')
        assignment['inputs'].append(str(self.root/'requirements.md'))
        self.report(assignment,message)
        outcome = page.publish(self.state,assignment['request_id'])
        self.assertEqual('needs_attention',outcome['result']['status'])
        self.assertIn('accept integration first',outcome['result']['error'])
        self.assertFalse((self.out/'filter/result.plan.html').exists())
        registry = exchange.read_state(self.state)
        self.assertEqual('accepted',registry['requests'][assignment['request_id']]['status'])
        self.assertNotIn('integrated',registry['requests'])

    def test_merge_refuses_reported_group_and_preserves_existing_artifacts(self):
        self.build(); self.group('filter'); self.group('login',False)
        before=(self.out/'filter/catalog.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'completely accepted'):
            partition.merge(self.manifest)
        self.assertEqual(before,(self.out/'filter/catalog.json').read_bytes())
        self.assertFalse((self.out/'integration/catalog.json').exists())

    def test_changed_accepted_group_is_not_merged(self):
        self.ready()
        path=self.out/'filter/catalog.json'; path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaisesRegex(ValueError,'changed'):
            partition.merge(self.manifest)

    def test_exact_merge_preserves_case_bodies_and_is_idempotent(self):
        self.ready(); result=partition.merge(self.manifest)
        self.assertTrue(result['final_content_review_required'])
        combined=partition.file_json(result['catalog_path'])
        cases=[c for name in ('filter','login') for c in partition.file_json(self.out/name/'catalog.json')['test_cases']]
        self.assertEqual(sorted(cases,key=lambda c:c['scenario_id']),combined['test_cases'])
        raw=(self.out/'integration/partition-merge.json').read_bytes()
        partition.merge(self.manifest)
        self.assertEqual(raw,(self.out/'integration/partition-merge.json').read_bytes())
        self.assertFalse((self.out/'filter/result.plan.html').exists())

    def test_cross_group_requirement_keeps_both_links(self):
        self.baseline['payload']['requirement_coverage'][0]['scenario_ids'].append('TC-002')
        self.ready(); partition.merge(self.manifest)
        payload=partition.file_json(self.out/'integration/payload.json')
        self.assertEqual(['TC-001','TC-002'],payload['requirement_coverage'][0]['scenario_ids'])
        self.assertEqual(2,len(payload['requirement_coverage']))

    def test_shared_constraints_reach_every_author(self):
        (self.root/'shared.md').write_text('Synthetic common rule: isolated users, quantity limit 10')
        self.baseline['artifacts'].append('shared.md'); self.spec['shared_context_paths']=['shared.md']
        self.build()
        for name in ('filter','login'):
            context=partition.file_json(self.out/name/'context.json')['shared_and_group_context']
            self.assertIn('quantity limit 10',context[str(self.root/'shared.md')]['content'])

    def test_duplicate_titles_across_individually_valid_groups_stop_merge(self):
        self.build(); self.group('filter')
        assignment,message=self.group_message('login')
        catalog=partition.file_json(self.out/'login/catalog.json')
        catalog['test_cases'][0]['title']=partition.file_json(self.out/'filter/catalog.json')['test_cases'][0]['title']
        exchange.atomic_write(self.out/'login/catalog.json',catalog)
        self.report(assignment,message)
        with self.assertRaisesRegex(ValueError,'duplicate title'):
            partition.merge(self.manifest)
        self.assertFalse((self.out/'integration/catalog.json').exists())

    def test_global_planning_blocker_cannot_be_hidden_by_ready_group(self):
        self.baseline=json.loads((ROOT/'examples/test-plan/result-v2.json').read_text())
        self.baseline.update(task_id='QA-PART',request_id='scope',self_review=review_for())
        for name in ('plan-v2.md','cases-v2.json'):
            (self.root/name).write_bytes((ROOT/'examples/test-plan'/name).read_bytes())
        self.spec['groups']=self.spec['groups'][:1]
        self.build(); self.group('filter')
        with self.assertRaisesRegex(ValueError,'Global planning blocker'):
            partition.merge(self.manifest)
        self.assertFalse((self.out/'integration/catalog.json').exists())

    def test_optional_common_plan_information_is_preserved(self):
        self.baseline['payload']['business_goal']='Synthetic shared business goal'
        self.ready(); partition.merge(self.manifest)
        self.assertEqual('Synthetic shared business goal',partition.file_json(self.out/'integration/payload.json')['business_goal'])

    def test_request_namespace_collision_is_rejected_before_writing(self):
        self.base()
        for request in ('scope','plan-filter.revision-1'):
            spec=copy.deepcopy(self.spec); spec['groups'][1]['request_id']=request
            with self.assertRaises(ValueError): partition.build(self.state,'scope',spec,self.out)
            self.assertFalse(self.out.exists())

    def test_unknown_configuration_does_not_silently_drop_shared_conditions(self):
        self.base()
        with self.assertRaisesRegex(ValueError,'Unknown partition specification fields'):
            partition.build(self.state,'scope',dict(self.spec,shared_context=['requirements.md']),self.out)
        self.assertFalse(self.out.exists())

    def test_fragment_cannot_be_reused_as_global_baseline(self):
        self.build(); self.group('filter')
        with self.assertRaisesRegex(ValueError,'group cannot be the global'):
            partition.build(self.state,'plan-filter',self.spec,self.root/'wrong-global')
        self.assertFalse((self.root/'wrong-global').exists())

    def test_required_group_review_is_checked_before_merge(self):
        self.criteria=dict(schema_version=1,criteria=[dict(criterion_id='C-FILTER',description='Filter reviewed',
            source='requirements.md',required=True,required_reviews=[dict(agent='qa-result-reviewer',scope='sources')])])
        self.spec['groups'][0]['criterion_ids']=['C-FILTER']
        self.ready()
        with self.assertRaisesRegex(ValueError,'missing independent content review'):
            partition.merge(self.manifest)
        row=exchange.read_state(self.state)['requests']['plan-filter']; result=row['result']
        assignment=dict(task_id='QA-PART',request_id='filter-review',agent='qa-result-reviewer',stage='filter-review',
            goal='Synthetic source review',acceptance_criteria=['Filter reviewed'],criterion_ids=['C-FILTER'],
            scenario_ids=['TC-001'],stop_condition='Report',expected_artifact='QA JSON',result_path=str(self.out/'filter/review.json'),
            inputs=list(result['artifact_hashes']),depends_on=[],review_criteria_ids=['C-FILTER'],
            plan_partition=copy.deepcopy(row['assignment']['plan_partition']),
            review_target=dict(request_id='plan-filter',result_path=result['path'],artifact_hashes=result['artifact_hashes'],scope='sources'))
        message=dict(task_id='QA-PART',request_id='filter-review',agent='qa-result-reviewer',event='review_passed',summary='Synthetic',
            artifacts=[],blockers=[],self_review=review_for(),payload=dict(target_request_id='plan-filter',
                target_sha256=result['artifact_hashes'][result['path']],review_scope='sources',reviewed_revision='v4',
                checks=[dict(criterion_id='C-FILTER',status='passed',note='Synthetic source checked',evidence=[str(self.out/'filter/context.json')])],
                findings=[],limitations=[]))
        self.report(assignment,message)
        self.assertEqual(2,partition.merge(self.manifest)['case_count'])

    def test_integration_assignment_cannot_drop_global_review_criteria(self):
        self.criteria=dict(schema_version=1,criteria=[dict(criterion_id='C-GLOBAL',description='Full plan reviewed',
            source='requirements.md',required=True,required_reviews=[dict(agent='qa-result-reviewer',scope='global')])])
        self.spec['integration_criterion_ids']=['C-GLOBAL']
        self.ready(); partition.merge(self.manifest)
        assignment=partition.file_json(self.out/'integration/assignment.json')
        assignment['criterion_ids']=[]
        with self.assertRaisesRegex(ValueError,'drop its global criteria'):
            exchange.assignment_record(assignment,self.root)

    def test_criteria_definition_changes_invalidate_partition(self):
        self.criteria=dict(schema_version=1,criteria=[dict(criterion_id='C-GLOBAL',description='Full plan reviewed',
            source='requirements.md',required=True)])
        self.spec['integration_criterion_ids']=['C-GLOBAL']
        self.build()
        state=exchange.read_state(self.state); state['criteria']['C-GLOBAL']['definition']['description']='Changed scope'
        exchange.atomic_write(self.state,state)
        with self.assertRaisesRegex(ValueError,'criteria changed'):
            partition.load(self.manifest)

    def test_full_integration_is_rechecked_and_gets_html_only_after_final_acceptance(self):
        self.ready(); partition.merge(self.manifest)
        assignment=partition.file_json(self.out/'integration/assignment.json')
        payload=partition.file_json(self.out/'integration/payload.json')
        (self.out/'integration/plan.md').write_text('Synthetic cross-group consistency review')
        message=dict(task_id='QA-PART',request_id='integrated',agent='qa-test-plan-writer',event='plan_ready',
            summary='Synthetic integrated plan',artifacts=['plan.md','catalog.json','partition-merge.json'],blockers=[],
            self_review=review_for(),payload=payload)
        self.assertFalse((self.out/'integration/result.plan.html').exists())
        self.report(assignment,message)
        presentation=exchange.read_state(self.state)['requests']['integrated']['presentation']
        self.assertEqual('ready',presentation['status'],presentation)
        self.assertIn('R-1',Path(presentation['path']).read_text())

    def test_integration_cannot_remove_a_requirement_after_merge(self):
        self.ready(); partition.merge(self.manifest)
        assignment=partition.file_json(self.out/'integration/assignment.json')
        payload=partition.file_json(self.out/'integration/payload.json')
        payload['requirement_coverage']=payload['requirement_coverage'][:1]
        payload['requirement_coverage'][0]['scenario_ids'].append('TC-002')
        payload['open_questions']=[]
        (self.out/'integration/plan.md').write_text('Synthetic')
        message=dict(task_id='QA-PART',request_id='integrated',agent='qa-test-plan-writer',event='plan_ready',summary='Synthetic',
            artifacts=['plan.md','catalog.json'],blockers=[],self_review=review_for(),payload=payload)
        with self.assertRaisesRegex(ValueError,'changes verified'):
            self.report(assignment,message)

    def test_catalog_bytes_cannot_change_between_merge_and_final_submit(self):
        self.ready(); partition.merge(self.manifest)
        path=self.out/'integration/catalog.json'; path.write_bytes(path.read_bytes()+b' ')
        assignment=partition.file_json(self.out/'integration/assignment.json')
        payload=partition.file_json(self.out/'integration/payload.json')
        (self.out/'integration/plan.md').write_text('Synthetic')
        message=dict(task_id='QA-PART',request_id='integrated',agent='qa-test-plan-writer',event='plan_ready',summary='Synthetic',
            artifacts=['plan.md','catalog.json','partition-merge.json'],blockers=[],self_review=review_for(),payload=payload)
        with self.assertRaisesRegex(ValueError,'artifact bytes changed'):
            self.report(assignment,message)

    def test_explicit_corrected_revision_is_selected_without_guessing_latest(self):
        self.build(); assignment,message=self.group('filter',False)
        exchange.review_result(self.state,'plan-filter','Synthetic correction requested',False,None)
        correction=copy.deepcopy(assignment); correction['request_id']+='.revision-1'
        correction['result_path']=str(self.out/'filter/result.revision-1.json')
        message['request_id']=correction['request_id']; self.report(correction,message)
        self.group('login')
        with self.assertRaises(ValueError): partition.merge(self.manifest)
        result=partition.merge(self.manifest,{'filter':'plan-filter.revision-1','login':'plan-login'})
        self.assertEqual(2,result['case_count'])
        deps=partition.file_json(self.out/'integration/assignment.json')['depends_on']
        self.assertIn('plan-filter.revision-1',deps); self.assertNotIn('plan-filter',deps)

    def test_review_cycle_retains_group_binding_and_bare_protocol(self):
        self.build(); assignment,_=self.group('filter')
        plan=supervisor.normalize_plan(dict(schema_version=1,workspace=str(self.root),task_state=str(self.state),
            scope_kind='synthetic',adapter=dict(kind='gigacode',executable='fixture-cli'),
            steps=[dict(step_id='author',assignment=assignment)]),self.root)
        state=dict(task_id='QA-PART',feedback=[],plan=dict(author_plan=plan,max_active_seconds=30))
        reviewer_plan=review_cycle.review_plan(state,dict(agent='qa-result-reviewer',scope='sources',
            max_wall_seconds=5,adapter=plan['adapter']),'plan-filter',self.out/'filter')
        step=supervisor.normalize_plan(reviewer_plan,self.root)['steps'][0]
        self.assertEqual('filter',step['assignment']['plan_partition']['group_id'])
        self.assertIn('docs/TEST_PLAN_PARTITION.md',step['instruction_paths'])
        self.assertNotIn(str(self.root/'requirements.md'),step['assignment']['inputs'])

    def test_cli_error_is_controlled_and_does_not_create_output(self):
        self.build()
        command=[sys.executable,str(ROOT/'scripts/partition_test_plan.py'),'merge','--partition',str(self.manifest)]
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(2,result.returncode)
        self.assertNotIn('Traceback',result.stderr)
        self.assertFalse((self.out/'integration/catalog.json').exists())


if __name__=='__main__': unittest.main()
