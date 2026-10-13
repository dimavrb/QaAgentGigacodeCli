"""Meaningful counterexamples for coverage freshness, missed defects and cost."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT / 'scripts'))
from frozen_reports import read, sha
from regression_observations import update_map, shadow_report
from regression_schedule import make_schedule
from qa_timing import summarize_timing, import_metrics
from impact_analysis import build_selection
from grader_controls import prepare_controls, run_controls, check_controls
from qa_improvements import report
from execute_regression_plan import execute
from run_benchmark_grader import GRADING_SYSTEM
from run_agent_benchmark import NEEDED_FLAGS
from improvement_acceptance import check_bundle
import task_exchange as exchange
import context_builder

spec = importlib.util.spec_from_file_location('improvements_demo', ROOT / 'examples/qa-improvements/build_demo.py')
demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


class ImprovementsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = demo.create_demo(Path(self.temp.name) / 'demo')
        self.input = self.root / 'impact-input.json'
        self.policy = self.root / 'execution-policy.json'

    def mutate(self, name, edit):
        path = self.root / name; value = read(path); edit(value); save(path, value); return value

    def mapped(self):
        return update_map(self.root / 'sources/impact-input.json', self.root / 'coverage.json', environment_id='fixture-env', test_data_id='fixture-data')

    def shadow(self):
        return shadow_report(self.input, self.root / 'full-results.json', self.root / 'selected-results.json')

    def bind_timing(self, arm):
        self.mutate(arm + '-results.json', lambda r: r['timing']['trace'].update(sha256=sha(self.root / (arm + '-trace.json'))))

    def test_coverage_refresh_produces_valid_frozen_input_and_known_mapping(self):
        result = build_selection(self.input)
        self.assertEqual(['TC-PAY'], result['excluded_scenario_ids'])
        self.assertEqual([], result['review_scenario_ids'])
        self.assertEqual(60, result['estimated_serial_duration']['selected']['total_seconds'])

    def test_stale_coverage_missing_test_and_incomplete_hit_never_allow_exclusion(self):
        for edit in [lambda v: v.update(revision='old-build'), lambda v: v['tests'].pop(),
                     lambda v: v['tests'][0].update(coverage_complete=False)]:
            original = (self.root / 'coverage.json').read_text()
            self.mutate('coverage.json', edit); save(self.root / 'refreshed.json', self.mapped())
            self.assertEqual('full', build_selection(self.root / 'refreshed.json')['selection_mode'])
            (self.root / 'coverage.json').write_text(original)

    def test_partial_file_ownership_and_unknown_external_changes_force_full(self):
        self.mutate('coverage.json', lambda v: v['components'][0].update(files=[]))
        save(self.root / 'refreshed.json', self.mapped())
        self.assertEqual('full', build_selection(self.root / 'refreshed.json')['selection_mode'])

    def test_retry_pass_does_not_erase_previously_failed_test(self):
        self.mutate('coverage.json', lambda v: v['tests'][2]['attempts'].extend([
            dict(attempt_id='retry-failure', status='fail', duration_seconds=4),
            dict(attempt_id='retry-pass', status='pass', duration_seconds=8)]))
        refreshed = self.mapped()
        self.assertIn(read(self.root / 'coverage.json')['tests'][2]['scenario_id'], refreshed['policy']['previously_failed_scenario_ids'])

    def test_invalid_graph_unknown_tests_duplicate_and_nonfinite_numbers_are_rejected(self):
        original = (self.root / 'coverage.json').read_text()
        for edit in [lambda v: v['components'][0].update(depends_on=['missing']),
                     lambda v: v['tests'][0].update(scenario_id='invented'),
                     lambda v: v['tests'].append(copy.deepcopy(v['tests'][0])),
                     lambda v: v['tests'][0]['attempts'][0].update(duration_seconds=float('nan')),
                     lambda v: v.update(revision='old-build', dependencies_complete='yes')]:
            self.mutate('coverage.json', edit)
            with self.assertRaises(ValueError): self.mapped()
            (self.root / 'coverage.json').write_text(original)

    def test_shadow_measures_total_observed_cost_not_serial_test_sum(self):
        result = self.shadow()
        self.assertEqual('no_miss_in_observed_run', result['outcome'])
        self.assertEqual(5, result['observed_saved_wall_seconds'])
        self.assertEqual(75, result['timing']['full']['wall_seconds'])
        self.assertFalse(result['promotion_eligible'])
        self.assertEqual('fixture', result['measurement_kind'])

    def test_confirmed_defect_only_in_excluded_test_is_a_miss(self):
        proof = self.root / 'defect.md'; proof.write_text('Учебное подтверждение дефекта платежа.')
        def edit(v):
            row = next(r for r in v['results'] if r['scenario_id'] == 'TC-PAY')
            row['attempts'][0].update(status='fail', classification='confirmed_defect', defect_id='BUG-PAY',
                confirmation=dict(path=str(proof), sha256=sha(proof)))
        self.mutate('full-results.json', edit)
        result = self.shadow()
        self.assertEqual('missed_defects', result['outcome']); self.assertEqual(['BUG-PAY'], result['missed_defect_ids'])
        self.assertIsNone(result['observed_saved_wall_seconds'])
        proof.write_text('Изменено')
        with self.assertRaisesRegex(ValueError, 'hash'): self.shadow()

    def test_initial_failure_followed_by_pass_is_preserved_and_unresolved(self):
        self.mutate('selected-results.json', lambda v: v['results'][0]['attempts'].insert(0,
            dict(attempt_id='first-fail', status='fail', classification='unclassified', duration_seconds=3)))
        result = self.shadow()
        self.assertEqual('inconclusive', result['outcome']); self.assertEqual(7, result['execution_attempts']['selected'])
        self.assertTrue(result['unstable_scenario_ids']['selected'])

    def test_different_environment_missing_results_same_run_and_wrong_catalog_are_rejected(self):
        original = (self.root / 'selected-results.json').read_text()
        for edit in [lambda v: v.update(environment_id='other'), lambda v: v['results'].pop(),
                     lambda v: v.update(run_id='fixture-full'), lambda v: v.update(catalog_sha256='fake')]:
            self.mutate('selected-results.json', edit)
            with self.assertRaises(ValueError): self.shadow()
            (self.root / 'selected-results.json').write_text(original)

    def test_no_acceptance_or_unmeasured_gap_prevents_positive_cost_conclusion(self):
        self.mutate('selected-trace.json', lambda v: v.update(accepted_at=None)); self.bind_timing('selected')
        self.assertIsNone(self.shadow()['observed_saved_wall_seconds'])
        self.mutate('selected-trace.json', lambda v: v.update(accepted_at=80)); self.bind_timing('selected')
        self.assertEqual(10, self.shadow()['timing']['selected']['unaccounted_wall_seconds'])

    def test_parallel_timing_uses_union_and_separates_effort(self):
        self.mutate('full-trace.json', lambda v: v['spans'].append(dict(span_id='other', phase_id='execution', kind='active',
            attempt_id='other', retry=True, status='failed', started_at=3, finished_at=73, input_tokens=11, output_tokens=None)))
        result = summarize_timing(self.root / 'full-plan.json', self.root / 'full-trace.json')
        self.assertEqual(75, result['wall_seconds']); self.assertEqual(145, result['active_effort_seconds'])
        self.assertEqual(1, result['failed_attempts']); self.assertIsNone(result['input_tokens'])
        self.assertEqual(11, result['known_input_tokens'])

    def test_unknown_format_open_accepted_span_and_repeated_attempt_are_rejected(self):
        original = (self.root / 'full-trace.json').read_text()
        for edit in [lambda v: v.update(schema_version=2), lambda v: v['spans'][0].update(finished_at=None),
                     lambda v: v['spans'][1].update(attempt_id=v['spans'][0]['attempt_id'])]:
            self.mutate('full-trace.json', edit)
            with self.assertRaises(ValueError): summarize_timing(self.root / 'full-plan.json', self.root / 'full-trace.json')
            (self.root / 'full-trace.json').write_text(original)

    def test_schedule_conserves_selection_and_serializes_shared_resource(self):
        schedule = make_schedule(self.input, self.policy)
        self.assertEqual(6, len(schedule['assignments']))
        locked = [r for r in schedule['assignments'] if r['resource_slots']]
        self.assertEqual(2, len(locked))
        first, second = sorted(locked, key=lambda r: r['starts_after_seconds'])
        self.assertLessEqual(first['ends_after_seconds'], second['starts_after_seconds'])
        self.assertLess(schedule['estimated_wall_seconds'], 60)

    def test_scheduler_rejects_unlocked_shared_data_and_unknown_worker_capability(self):
        original = self.policy.read_text()
        for edit in [lambda v: v['tests'][0].update(isolated_test_data=False, resource_ids=[]),
                     lambda v: v['tests'][0].update(required_capabilities=['missing']),
                     lambda v: v['resources'][0].update(capacity=True)]:
            self.mutate('execution-policy.json', edit)
            with self.assertRaises(ValueError): make_schedule(self.input, self.policy)
            self.policy.write_text(original)

    def test_unknown_duration_is_pending_without_fabricated_estimate(self):
        self.mutate('impact-input.json', lambda v: v['tests'][0].update(duration_seconds=None))
        self.mutate('execution-policy.json', lambda v: v.update(impact_input_sha256=sha(self.input)))
        schedule = make_schedule(self.input, self.policy)
        self.assertEqual('needs_durations', schedule['status']); self.assertIsNone(schedule['estimated_wall_seconds'])

    def test_report_checker_rejects_fabricated_savings(self):
        result = subprocess.run([sys.executable, str(ROOT/'scripts/qa_improvements.py'), 'verify', '--report', str(self.root/'shadow.json')], capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout)
        self.mutate('shadow.json', lambda v: v.update(observed_saved_wall_seconds=999))
        result = subprocess.run([sys.executable, str(ROOT/'scripts/qa_improvements.py'), 'verify', '--report', str(self.root/'shadow.json')], capture_output=True)
        self.assertEqual(2, result.returncode)

    def control_capture(self, wrong=False, empty=False):
        public = read(self.root/'controls-public.json')
        suite = read(ROOT/'examples/grader-controls/suite.json')
        gold = {c['case_id']:c['checks'][0]['gold_status'] for c in suite['cases']}
        grades = dict(cases=[dict(case_id=c['case_id'], checks=[dict(check_id='meaning',
            status='pass' if wrong else gold[c['case_id']], evidence=c['response'], note='Оценка по условиям задачи.')]) for c in public['cases']])
        envelope = json.dumps(dict(response='' if empty else json.dumps(grades), usage=dict(input_tokens=20, output_tokens=5)))
        with patch('grader_controls.invoke_isolated', return_value=(subprocess.CompletedProcess([],0,envelope,''),1.5)):
            run_controls(self.root/'controls-public.json', self.root/'control-review.json', measurement_kind='fixture')
        return check_controls(ROOT/'examples/grader-controls/suite.json', self.root/'controls-public.json', self.root/'control-review.json')

    def test_semantic_controls_detect_real_quote_with_wrong_verdict(self):
        result = self.control_capture(wrong=True)
        self.assertEqual(8, result['metrics']['false_approvals'])
        self.assertEqual(0, result['metrics']['error_detection_recall'])
        self.assertEqual(4, result['by_split']['heldout']['false_approvals'])
        self.assertTrue(result['provisional_gold']); self.assertFalse(result['promotion_eligible'])

    def test_correct_controls_have_no_false_blocks_or_approvals(self):
        result = self.control_capture()
        self.assertEqual(16, result['metrics']['correct'])

    def test_control_payload_never_includes_gold_labels_or_heldout_split(self):
        public = (self.root/'controls-public.json').read_text()
        for field in ('gold_status','gold_reason','error_family','heldout','calibration','reviewed_by'):
            self.assertNotIn(field, public)

    def test_empty_control_response_preserves_usage_as_execution_failure(self):
        result = self.control_capture(empty=True)
        self.assertEqual('execution_failed', result['status']); self.assertEqual(20, result['input_tokens'])

    def test_tampered_control_grades_cannot_replace_primary_capture(self):
        self.control_capture()
        self.mutate('control-review.json', lambda v: v['grades']['cases'][0]['checks'][0].update(status='fail'))
        with self.assertRaisesRegex(ValueError, 'primary raw'):
            check_controls(ROOT/'examples/grader-controls/suite.json', self.root/'controls-public.json', self.root/'control-review.json')

    def test_local_executor_keeps_initial_failure_and_respects_resource_locks(self):
        commands = dict(schema_version=1, impact_input_sha256=sha(self.input), run_id='fixture-execution',
            build_revision='demo-target', environment_id='fixture-env', test_data_id='fixture-data', measurement_kind='fixture', tests=[])
        script = self.root/'fixture-test.py'
        script.write_text('''import pathlib, sys, time
root=pathlib.Path(sys.argv[1]); name=sys.argv[2]; shared=sys.argv[3]=='shared'
lock=root/'busy'; counter=root/('count-'+name)
if shared:
 try: lock.mkdir()
 except FileExistsError: sys.exit(7)
try:
 time.sleep(.03)
 prior=int(counter.read_text()) if counter.exists() else 0; counter.write_text(str(prior+1))
finally:
 if shared: lock.rmdir()
sys.exit(1 if name=='TC-ORDER' and prior==0 else 0)
''')
        for sid in build_selection(self.input)['run_scenario_ids']:
            commands['tests'].append(dict(scenario_id=sid, argv=[sys.executable,str(script),str(self.root),sid,
                'shared' if sid in ('TC-ORDER','TC-EXPORT') else 'isolated'], cwd=str(self.root),
                timeout_seconds=2, max_attempts=2, retry_safe=True, retry_exit_codes=[1]))
        save(self.root/'commands.json', commands)
        result = execute(self.input,self.policy,self.root/'commands.json',self.root/'execution')
        order = next(r for r in result['results'] if r['scenario_id']=='TC-ORDER')
        self.assertEqual(['fail','pass'], [a['status'] for a in order['attempts']])
        self.assertEqual('unclassified', order['attempts'][0]['classification'])
        self.assertTrue(all(r['attempts'][-1]['status']=='pass' for r in result['results']))
        self.assertFalse(result['qa_accepted']); self.assertIsNone(result['timing'])

    def test_worker_import_rechecks_origin_usage_and_correlation(self):
        self.mutate('full-plan.json', lambda v:v.update(task_id='task',trace_id='trace',step_phases={'step':'execution'}))
        metrics = dict(schema_version=1, correlation=dict(task_id='task',trace_id='trace',step_id='step',attempt_id='worker',retry=False),
            started_epoch=3,finished_epoch=73, input_tokens=12,output_tokens=4,exit_code=0,category=None)
        save(self.root/'metrics.json',metrics)
        save(self.root/'index.json',dict(schema_version=1,metrics=[dict(path='metrics.json',sha256=sha(self.root/'metrics.json'))]))
        merged = import_metrics(self.root/'full-plan.json',self.root/'full-trace.json',self.root/'index.json')
        save(self.root/'merged-trace.json',merged)
        result = summarize_timing(self.root/'full-plan.json', self.root/'merged-trace.json')
        self.assertEqual(12,result['known_input_tokens'])
        merged['spans'][-1]['input_tokens']=999;save(self.root/'merged-trace.json',merged)
        with self.assertRaisesRegex(ValueError,'primary worker'): summarize_timing(self.root/'full-plan.json',self.root/'merged-trace.json')

    def test_coverage_context_mismatch_and_later_context_change_force_conservative_behavior(self):
        with self.assertRaisesRegex(ValueError, 'environment'):
            update_map(self.root/'sources/impact-input.json', self.root/'coverage.json',environment_id='other',test_data_id='fixture-data')
        self.mutate('impact-input.json', lambda v:v['execution_context'].update(environment_id='other'))
        selection = build_selection(self.input)
        self.assertEqual('full',selection['selection_mode'])
        self.assertIn('incompatible_coverage_context',selection['fallback_reasons'])

    def test_independent_acceptance_plan_rejects_fixture_when_live_required(self):
        plan = dict(schema_version=1,session_id='current-session',reports=[dict(report_id='shadow',command='shadow',
            command_inputs=dict(input=str(self.input),full=str(self.root/'full-results.json'),selected=str(self.root/'selected-results.json')),
            requirements=dict(require_live=True,require_no_miss=True))])
        save(self.root/'improvement-plan.json',plan)
        message=dict(event='analysis_ready',summary=read(self.root/'shadow.json')['summary_text'],payload=dict(improvement_reports={'shadow':'shadow.json'}))
        with self.assertRaisesRegex(ValueError,'live measurement'): check_bundle(message,self.root,self.root/'improvement-plan.json')
        message['event']='blocked'
        self.assertTrue(check_bundle(message,self.root,self.root/'improvement-plan.json')['gate_failures'])

    def test_registry_binds_independent_plan_and_tracks_primary_files(self):
        plan = dict(schema_version=1,session_id='current-session',reports=[dict(report_id='shadow',command='shadow',
            command_inputs=dict(input=str(self.input),full=str(self.root/'full-results.json'),selected=str(self.root/'selected-results.json')),
            requirements=dict(require_no_miss=True))])
        plan_path=self.root/'improvement-plan.json';save(plan_path,plan)
        payload=read(ROOT/'examples/analysis/result.json')['payload'];payload['improvement_reports']={'shadow':'shadow.json'}
        message=dict(task_id='QA-IMPROVEMENTS',request_id='summary',agent='qa-analyst',event='analysis_ready',
            summary=read(self.root/'shadow.json')['summary_text'],artifacts=['shadow.json'],blockers=[],payload=payload,
            self_review=dict(schema_version=1,checks=[dict(id=cid,status='passed',note='Командная сводка проверена.',
                evidence=['assignment:/acceptance_criteria','report:/payload/improvement_reports']) for cid in ('scope','evidence','consistency','role')],
                fixes=[],remaining_risks=['Учебные данные.']))
        result_path=self.root/'result.json';save(result_path,message)
        state=self.root/'task.json';exchange.init_task(state,'QA-IMPROVEMENTS','Учебный пилот')
        assignment=dict(task_id='QA-IMPROVEMENTS',request_id='summary',agent='qa-analyst',stage='summary',
            goal='Проверить пилот',acceptance_criteria=['Командная сводка соответствует первичным данным.'],stop_condition='Сохранённый отчёт',
            expected_artifact='analysis_ready',result_path=str(result_path),artifact_base=str(self.root),improvement_plan=str(plan_path))
        exchange.assign(state,assignment,self.root,None)
        assigned=exchange.read_state(state)['requests']['summary']['assignment']
        self.assertIn(str(self.root/'coverage.json'),assigned['inputs'])
        handoff=exchange.prepare_handoff(state,'summary',None)['result']
        exchange.start_request(state,'summary','fixture-owner',handoff['delivery_id'],None)
        exchange.submit_result(state,'summary')
        protected=exchange.read_state(state)['requests']['summary']['result']['artifact_hashes']
        self.assertIn(str(self.root/'full-trace.json'),protected)
        message['summary']+=' Экономия 100 с.';save(result_path,message)
        with self.assertRaises(ValueError): exchange.inspect_result(assigned)
        profile=context_builder.profile(dict(agent='qa-analyst',improvement_plan=str(plan_path)),{})
        self.assertIn('docs/QA_IMPROVEMENTS.md',profile['documents'])

    def test_provisional_gold_and_false_approval_fail_independent_control_gate(self):
        self.control_capture(wrong=True)
        inputs=dict(suite=ROOT/'examples/grader-controls/suite.json',pack=self.root/'controls-public.json',review=self.root/'control-review.json')
        scored=report('controls-check',inputs);save(self.root/'score.json',scored)
        plan=dict(schema_version=1,session_id='current-session',reports=[dict(report_id='controls',command='controls-check',
            command_inputs={k:str(v) for k,v in inputs.items()},requirements=dict(require_reviewed_gold=True,max_false_approvals=0))])
        save(self.root/'plan.json',plan)
        message=dict(event='analysis_ready',summary=scored['summary_text'],payload=dict(improvement_reports={'controls':'score.json'}))
        with self.assertRaisesRegex(ValueError,'provisional gold'):check_bundle(message,self.root,self.root/'plan.json')

    def test_executor_timeout_and_unsafe_retry_are_explicit_failures(self):
        ids=build_selection(self.input)['run_scenario_ids']
        commands=dict(schema_version=1,impact_input_sha256=sha(self.input),run_id='timeout',build_revision='demo-target',
            environment_id='fixture-env',test_data_id='fixture-data',measurement_kind='fixture',tests=[
                dict(scenario_id=sid,argv=[sys.executable,'-c','import time;time.sleep(1)'],cwd=str(self.root),
                    timeout_seconds=.02,max_attempts=1,retry_exit_codes=[]) for sid in ids])
        save(self.root/'commands.json',commands)
        result=execute(self.input,self.policy,self.root/'commands.json',self.root/'timeout-execution')
        self.assertTrue(all(r['attempts'][0]['status']=='error' for r in result['results']))
        self.assertTrue(all(r['attempts'][0]['classification']=='infrastructure' for r in result['results']))
        commands['tests'][0]['max_attempts']=2;save(self.root/'unsafe-commands.json',commands)
        with self.assertRaisesRegex(ValueError,'safe'):
            execute(self.input,self.policy,self.root/'unsafe-commands.json',self.root/'unsafe-execution')

    def test_real_fixture_cli_receives_only_blind_payload_in_empty_cwd(self):
        cli=self.root/'fixture-grader'
        cli.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
if '--help' in sys.argv: print(%r)
else:
 assert not list(pathlib.Path.cwd().iterdir())
 assert '--bare' in sys.argv and '--exclude-tools' in sys.argv
 prompt=sys.argv[sys.argv.index('-p')+1]
 assert 'gold_status' not in prompt and 'heldout' not in prompt
 cases=json.loads(prompt)['cases']
 result={'cases':[{'case_id':c['case_id'],'checks':[{'check_id':x['check_id'],'status':'pass','evidence':c['response'],'note':'Учебная оценка без живой модели.'} for x in c['checks']]} for c in cases]}
 print(json.dumps({'response':json.dumps(result),'usage':{'input_tokens':30,'output_tokens':10}}))
''' % ' '.join(NEEDED_FLAGS));cli.chmod(0o755)
        run_controls(self.root/'controls-public.json',self.root/'actual-fixture.json',cli=str(cli),measurement_kind='fixture')
        result=check_controls(ROOT/'examples/grader-controls/suite.json',self.root/'controls-public.json',self.root/'actual-fixture.json')
        self.assertEqual(8,result['metrics']['false_approvals']);self.assertEqual(30,result['input_tokens'])

    def test_boolean_counter_cannot_masquerade_as_number_in_saved_report(self):
        self.mutate('shadow.json',lambda v:v.update(missed_defect_ids=[],promotion_eligible=0))
        result=subprocess.run([sys.executable,str(ROOT/'scripts/qa_improvements.py'),'verify','--report',str(self.root/'shadow.json')],capture_output=True)
        self.assertEqual(2,result.returncode)


if __name__ == '__main__': unittest.main()
