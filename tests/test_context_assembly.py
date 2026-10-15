import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import context_builder as cb
import execution_metrics as metrics
import execution_supervisor as supervisor
import execution_worker as worker
import task_exchange as exchange
from qa_contract import ROLE_EVENTS, EVENT_FIELDS
from environment_fixtures import bind_environment


class ProjectionTests(unittest.TestCase):
    def test_all_roles_keep_event_fields_and_four_self_review_checks(self):
        for role in ROLE_EVENTS:
            spec=cb.profile(dict(agent=role),{})
            body='\n'.join(cb.render((ROOT/name).read_text(),item,role) for name,item in spec['documents'].items())
            for event in ROLE_EVENTS[role]:
                self.assertIn(event,body,role)
                for field in EVENT_FIELDS.get(event,{}): self.assertIn(field,body,(role,event,field))
            for check in ('scope','evidence','consistency','role','assignment:/acceptance_criteria','remaining_risks'):
                self.assertIn(check,body,role)

    def test_common_projection_removes_other_roles_not_own_risk(self):
        body=cb.contract((ROOT/'docs/CONTRACT.md').read_text(),'qa-test-data-builder')
        self.assertIn('data_partial',body);self.assertNotIn('| `jira_defect_created`',body)
        body=cb.self_review((ROOT/'docs/SELF_REVIEW.md').read_text(),'qa-test-data-builder')
        self.assertIn('| `qa-test-data-builder`',body);self.assertNotIn('| `qa-analyst`',body)
        self.assertIn('Самопроверка автора не заменяет',body)

    def test_data_core_is_available_with_bare_reads_disabled(self):
        for role in ('qa-test-data-builder','qa-test-automation','qa-autotest-reviewer'):
            spec=cb.profile(dict(agent=role),{})['documents']
            self.assertIn('docs/TEST_DATA_CHECKLIST.md',spec)
            text=cb.render((ROOT/'docs/TEST_DATA.md').read_text(),spec['docs/TEST_DATA.md'],role)
            self.assertNotIn('## Готовность и очистка',text)
            extended=cb.profile(dict(agent=role),dict(test_data_sections=['cleanup']))['documents']
            self.assertIn('Готовность и очистка',extended['docs/TEST_DATA.md']['sections'])

    def test_subject_standards_are_present_for_operational_roles(self):
        expected={'qa-stub-builder':['docs/STUBS.md'], 'qa-defect-investigator':['docs/INVESTIGATION.md','docs/DEFECT_VERIFICATION.md'],
                  'qa-feature-tester':['docs/EXECUTION.md'],'qa-test-report-writer':['docs/TEST_REPORTING.md'],
                  'qa-test-automation':['docs/AUTOTEST_NAMING.md']}
        for role,names in expected.items():
            spec=cb.profile(dict(agent=role),{})['documents']
            for name in names:self.assertIn(name,spec)
        self.assertIn('docs/BROWSER_PROFILES.md',cb.profile(dict(agent='qa-feature-tester',test_type='ui'),{})['documents'])

    def test_runtime_and_discussion_only_added_when_applicable(self):
        role=dict(agent='qa-conclusion-reviewer')
        self.assertNotIn('docs/REVIEW_EXECUTION.md',cb.profile(role,{})['documents'])
        self.assertIn('docs/REVIEW_EXECUTION.md',cb.profile(role,dict(gateway_policy={'tools':{}}))['documents'])
        self.assertIn('docs/REVIEW_DISCUSSION.md',cb.profile(dict(role,review_discussion={'round':1}),{})['documents'])

    def test_manual_case_workflow_is_delivered_to_bare_planner_and_reviewer(self):
        required={'docs/TEST_CASE_SELECTION.md','docs/TEST_CASE_STYLE.md',
                  '.gigacode/skills/qa-test-cases/SKILL.md',
                  '.gigacode/skills/qa-check-quality/SKILL.md'}
        for mode in ('compact','full'):
            profile=cb.profile(dict(agent='qa-test-plan-writer'),dict(context_mode=mode))
            self.assertTrue(required <= set(profile['documents']))
            hashes={name:exchange.fingerprint(ROOT/name) for name in profile['documents']}
            step=dict(context_profile=profile,instruction_hashes=hashes,
                      context_builder_sha256=exchange.fingerprint(Path(cb.__file__)))
            snapshots=cb.snapshots(step)
            for name in required:
                self.assertEqual((ROOT/name).read_text(),snapshots[name])
            step['instruction_hashes']['docs/TEST_CASE_SELECTION.md']='0'*64
            with self.assertRaisesRegex(ValueError,'Managed instructions changed'):
                cb.snapshots(step)
        unrelated=cb.profile(dict(agent='qa-test-data-builder'),{})['documents']
        self.assertFalse(required & set(unrelated))
        manual_paths=sorted(required-{'.gigacode/skills/qa-check-quality/SKILL.md'})
        for mode in ('compact','full'):
            profile=cb.profile(dict(agent='qa-result-reviewer'),
                               dict(context_mode=mode,instruction_paths=manual_paths))
            self.assertTrue(required <= set(profile['documents']))
            step=dict(context_profile=profile,
                      instruction_hashes={name:exchange.fingerprint(ROOT/name) for name in profile['documents']},
                      context_builder_sha256=exchange.fingerprint(Path(cb.__file__)))
            for name in required:
                self.assertEqual((ROOT/name).read_text(),cb.snapshots(step)[name])

    def test_missing_or_duplicate_section_is_error_not_silent_reduction(self):
        with self.assertRaisesRegex(ValueError,'missing'):cb.select('## Other\ntext',['Required'])
        with self.assertRaisesRegex(ValueError,'Duplicate'):cb.select('## One\na\n## One\nb',['One'])

    def test_invalid_selection_or_escape_is_rejected(self):
        for extra in [dict(context_mode='small'),dict(test_data_sections=['unknown']),dict(instruction_paths=['../outside.md']),dict(instruction_paths='path')]:
            with self.assertRaises((ValueError,FileNotFoundError)):cb.profile(dict(agent='qa-test-data-builder'),extra)

    def test_full_mode_preserves_sources_for_comparison(self):
        spec=cb.profile(dict(agent='qa-test-data-builder'),dict(context_mode='full'))
        for name,item in spec['documents'].items():self.assertEqual((ROOT/name).read_text(),cb.render((ROOT/name).read_text(),item,spec['role']))

    def test_reviewer_common_instructions_are_substantially_smaller(self):
        spec=cb.profile(dict(agent='qa-conclusion-reviewer'),{})
        size=sum(len(cb.render((ROOT/name).read_text(),item,spec['role'])) for name,item in spec['documents'].items())
        self.assertLess(size,33000)


class InputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.left=self.root/'left.json';self.right=self.root/'right.json'
        text=json.dumps(dict(unique='single-copy-marker',fact='not_run'))
        self.left.write_text(text);self.right.write_text(text)
        self.handoff=dict(assignment=dict(inputs=[str(self.left),str(self.right)]),sources=[dict(request_id='source',result_path=str(self.left))],answer_sources=[dict(questionnaire_id='form',answers_path=str(self.right))])

    def test_inputs_dependencies_and_answers_share_exact_content_once(self):
        result=cb.assemble(self.handoff,{})
        self.assertEqual(1,json.dumps(result).count('single-copy-marker'))
        self.assertEqual(str(self.left),result['input_contents'][str(self.right)]['input_ref'])
        self.assertEqual(str(self.left),result['source_reports']['source']['input_ref'])
        self.assertEqual(str(self.right),result['applied_answers']['form']['input_ref'])
        self.assertEqual(exchange.fingerprint(self.right),result['input_contents'][str(self.right)]['sha256'])

    def test_blind_feedback_cannot_reappear_through_sources(self):
        self.handoff['assignment'].update(inputs=[str(self.left)],blind_review_exclusions=[str(self.left)])
        self.handoff['answer_sources']=[]
        result=cb.assemble(self.handoff,{})
        self.assertNotIn('single-copy-marker',json.dumps(result))
        self.assertIn('excluded',result['input_contents'][str(self.left)]['unavailable'])

    def test_changed_input_is_not_blessed_by_new_hash(self):
        old=exchange.fingerprint(self.left);self.left.write_text('changed')
        with self.assertRaisesRegex(ValueError,'input changed'):cb.assemble(self.handoff,{}, {str(self.left):old})

    def test_binary_evidence_is_preserved_as_unavailable_not_fake_text(self):
        self.left.write_bytes(b'\xff\x00')
        result=cb.assemble(self.handoff,{})
        self.assertIn('binary',result['input_contents'][str(self.left)]['unavailable'])

    def test_correction_contains_initial_blocks_and_unknown_checks_without_passing_bulk(self):
        finding=dict(criterion_id='C',blocking=True,problem='original issue',evidence='primary',resolution_condition='fix')
        original=dict(findings=[finding],checks=[dict(criterion_id='C',status='failed',note='failure')],limitations=['scope unknown'])
        latest=dict(findings=[],checks=[dict(criterion_id='C',status='passed',note='all passed')],conclusion_checks=[dict(criterion_id='D',status='unknown',note='missing fact')])
        packet=dict(target_request_id='author',target_sha256='hash',instruction='Correct',reviews=[dict(payload=latest)],original_reviews=[dict(payload=original),dict(payload=original)])
        result=cb.correction_context(packet)
        self.assertEqual(1,len(result['findings']));self.assertEqual({'failed','unknown'},{c['status'] for c in result['unresolved_checks']})
        self.assertEqual(['scope unknown'],result['limitations']);self.assertNotIn('all passed',json.dumps(result))
        self.left.write_text(json.dumps(packet));self.handoff['assignment'].update(inputs=[str(self.left)],correction_feedback=str(self.left));self.handoff['sources']=[];self.handoff['answer_sources']=[]
        projected=cb.assemble(self.handoff,{})['input_contents'][str(self.left)]
        self.assertEqual('correction_feedback',projected['projection']);self.assertEqual(exchange.fingerprint(self.left),projected['source_sha256'])
        self.assertIn('original issue',json.dumps(projected['content']))


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()

    def test_usage_from_terminal_cli_envelope_and_zero_preserved(self):
        (self.root/'stdout.json').write_text(json.dumps([dict(type='result',usage=dict(prompt_tokens=0,completion_tokens=12),stats={'tools':{}})]))
        result=metrics.finish(self.root,backend='gigacode',exit_code=0,category=None,duration_seconds=1.2)
        self.assertEqual((0,12),(result['input_tokens'],result['output_tokens']))
        self.assertEqual(1.2,result['duration_seconds'])

    def test_missing_invalid_partial_and_failed_usage_never_imputed(self):
        for value in ['bad json',json.dumps(dict(usage=dict(input_tokens=True,output_tokens=-1))),json.dumps([])]:
            (self.root/'stdout.json').write_text(value)
            result=metrics.finish(self.root,backend='gigacode',exit_code=75,category='transient',duration_seconds=.2)
            self.assertIsNone(result['input_tokens']);self.assertIsNone(result['output_tokens'])
        self.assertEqual((4,None),metrics.usage(dict(usage=dict(input_tokens=4))))

    def test_aggregate_counts_all_attempts_and_marks_incomplete_total_unknown(self):
        d1=self.root/'one';d1.mkdir();d2=self.root/'two';d2.mkdir()
        (d1/'stdout.json').write_text(json.dumps(dict(usage=dict(input_tokens=3,output_tokens=4))))
        exchange.atomic_write(d1/'context-metrics.json',dict(explicit_characters=100))
        metrics.finish(d1,backend='gigacode',exit_code=75,category='transient',duration_seconds=.2)
        state=dict(steps={'s':dict(attempts=[dict(run_dir=str(d1)),dict(run_dir=str(d2))])})
        total=metrics.aggregate([state]);self.assertEqual(2,total['attempts']);self.assertEqual(3,total['known_input_tokens']);self.assertIsNone(total['input_tokens']);self.assertIsNone(total['explicit_characters']);self.assertEqual(100,total['known_explicit_characters'])
        (d2/'stdout.json').write_text(json.dumps(dict(usage=dict(input_tokens=5,output_tokens=6))))
        metrics.finish(d2,backend='gigacode',exit_code=0,category=None,duration_seconds=.1)
        total=metrics.aggregate([state]);self.assertTrue(total['complete_usage']);self.assertEqual((8,10),(total['input_tokens'],total['output_tokens']))


class ManagedContextTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.registry=self.root/'task.json';self.execution=self.root/'execution.json'
        exchange.init_task(self.registry,'QA-CONTEXT','Synthetic context')
        self.assignment=dict(task_id='QA-CONTEXT',request_id='env',agent='qa-test-env-configurator',stage='env',goal='Synthetic',acceptance_criteria=['Evidence'],stop_condition='Report',expected_artifact='QA JSON',result_path=str(self.root/'result.json'),inputs=[],depends_on=[])
        bind_environment(self.assignment, self.root)
        self.plan=dict(schema_version=1,workspace=str(self.root),task_state=str(self.registry),scope_kind='synthetic',steps=[dict(step_id='env',assignment=self.assignment,max_wall_seconds=5)],adapter=dict(kind='gigacode',executable='synthetic-cli'))
        self.addCleanup(self.stop)

    def stop(self):
        if self.execution.exists():
            try:supervisor.cancel(self.execution,'Synthetic cleanup')
            except Exception:pass

    def assemble(self,step):
        exchange.atomic_write(self.root/'handoff.json',dict(assignment=step['assignment'],sources=[],answer_sources=[]))
        flags='--bare --exclude-tools --append-system-prompt --output-format --max-wall-time --max-session-turns'
        with patch.object(worker.shutil,'which',return_value='/synthetic/cli'),patch.object(worker.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout=flags,stderr='')):
            return worker.command(dict(plan=dict(adapter=self.plan['adapter'])),step,dict(run_dir=str(self.root)),self.execution)

    def test_budget_overflow_records_sizes_and_preserves_input_before_refusing_launch(self):
        source=self.root/'source.txt';source.write_text('essential-evidence'*200)
        self.plan['steps'][0].update(max_context_characters=1000);self.assignment['inputs']=[str(source)]
        step=supervisor.normalize_plan(self.plan,self.root)['steps'][0]
        with self.assertRaisesRegex(ValueError,'no input was truncated'):self.assemble(step)
        self.assertTrue(exchange.read_json(self.root/'context-metrics.json')['budget_exceeded'])
        self.assertEqual(source.read_text(),exchange.read_json(self.root/'prompt.json')['input_contents'][str(source)])

    def test_context_budget_type_and_invalid_feedback_binding_are_rejected(self):
        for value in [True,0,10000001,'1000']:
            self.plan['steps'][0]['max_context_characters']=value
            with self.assertRaises(ValueError):supervisor.normalize_plan(self.plan,self.root)
        self.assignment['correction_feedback']=str(self.root/'unassigned')
        with self.assertRaisesRegex(ValueError,'author input'):exchange.assignment_record(self.assignment,self.root)

    def test_frozen_projection_and_legacy_full_snapshots(self):
        step=supervisor.normalize_plan(self.plan,self.root)['steps'][0]
        original=exchange.fingerprint
        with patch.object(exchange,'fingerprint',side_effect=lambda p:'changed' if Path(p).name=='context_builder.py' else original(p)):
            with self.assertRaisesRegex(ValueError,'builder changed'):cb.snapshots(step)
        legacy=copy.deepcopy(step);legacy.pop('context_profile');legacy.pop('context_builder_sha256')
        self.assertEqual((ROOT/'docs/CONTRACT.md').read_text(),cb.snapshots(legacy)['docs/CONTRACT.md'])

    def test_cycle_cannot_recompile_new_children_after_builder_change(self):
        import review_cycle
        state=dict(plan=dict(author_plan=dict(steps=[dict(context_builder_sha256='changed')])),children=[])
        with self.assertRaisesRegex(ValueError,'new review cycle plan'):
            review_cycle.ensure_child(self.execution,state,'author')

    def test_real_owned_fixture_process_captures_context_usage_and_terminal_metrics(self):
        fake=self.root/'fixture-cli'
        fake.write_text('#!'+sys.executable+'\n'+'''import json,os,sys
from pathlib import Path
if '--help' in sys.argv:
 print('--bare --exclude-tools --append-system-prompt --output-format --max-wall-time --max-session-turns');sys.exit(0)
if '--version' in sys.argv: print('fixture-only');sys.exit(0)
sys.path[:0]=[str(Path('''+repr(str(ROOT))+''')/'tests')]
from self_review_fixtures import review_for
from environment_fixtures import attach_environment_checks
assignment=json.loads(Path(os.environ['QA_EXECUTION_HANDOFF']).read_text())['assignment']
proof=Path(os.environ['QA_EXECUTION_HANDOFF']).parent/'proof.txt';proof.write_text('Synthetic only')
message=dict(task_id=assignment['task_id'],request_id=assignment['request_id'],agent=assignment['agent'],event='environment_ready',summary='Fixture',artifacts=[str(proof)],blockers=[],self_review=review_for(),payload=dict(environment_id='fixture',product_version='v1',verified_capabilities=['Synthetic'],limitations=[]))
attach_environment_checks(message,assignment,[str(proof)])
print(json.dumps(dict(response=json.dumps(message),usage=dict(input_tokens=21,output_tokens=8))))
''')
        fake.chmod(0o700);self.plan['adapter']['executable']=str(fake)
        supervisor.initialize(self.execution,self.plan,self.root)
        end=time.monotonic()+10
        while time.monotonic()<end:
            state=supervisor.tick(self.execution)
            if state['status']=='awaiting_review':break
            if state['status']=='blocked':self.fail(state)
            time.sleep(.05)
        self.assertEqual('awaiting_review',state['status'])
        total=metrics.aggregate([state]);self.assertEqual((21,8),(total['input_tokens'],total['output_tokens']));self.assertEqual(1,total['measured_context_attempts'])
        directory=Path(state['steps']['env']['attempts'][0]['run_dir'])
        m=exchange.read_json(directory/'metrics.json');self.assertFalse(m['context']['budget_exceeded']);self.assertIn('metrics_path',state['steps']['env']['attempts'][0])
        exchange.review_result(self.registry,'env','Synthetic evidence read',True,None)
        for _ in range(5):
            state=supervisor.tick(self.execution)
            if state['status']=='completed':break
        self.assertEqual('completed',state['status'])
        self.assertEqual(total['input_tokens'],metrics.aggregate([supervisor.tick(self.execution)])['input_tokens'])


if __name__=='__main__':unittest.main()
