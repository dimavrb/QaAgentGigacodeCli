"""Synthetic protocol checks; no CLI, product, browser or Jira execution."""
import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from qa_contract import validate_message
from investigation_state import validate_review


def load(path):
    return json.loads(path.read_text())


def validate(message, ids=None):
    return validate_message(message, message['task_id'], message['request_id'], message['agent'], ids)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.message = load(ROOT / 'examples/analysis/result.json')

    def test_confirmed_and_partially_unknown_examples(self):
        self.assertEqual([], validate(self.message))
        self.assertEqual([], validate(load(ROOT / 'examples/analysis/result-partial.json')))

    def test_empty_objects_and_legacy_strings_are_rejected(self):
        for value in ([{}], ['R1']):
            message = copy.deepcopy(self.message)
            message['payload'].update(requirements=value, risk_scenarios=value)
            self.assertTrue(validate(message))

    def test_missing_source_duplicate_and_unknown_links_are_rejected(self):
        for collection, field, value in (
            ('requirements', 'source', ''),
            ('risk_scenarios', 'expected_source', None),
            ('risk_scenarios', 'requirement_id', 'missing'),
            ('risk_scenarios', 'priority', 'critical'),
        ):
            with self.subTest(collection=collection, field=field):
                message = copy.deepcopy(self.message)
                message['payload'][collection][0][field] = value
                self.assertTrue(validate(message))
        for collection in ('requirements', 'risk_scenarios'):
            message = copy.deepcopy(self.message)
            message['payload'][collection].append(copy.deepcopy(message['payload'][collection][0]))
            self.assertTrue(validate(message))

    def test_unknown_and_inferred_expectations_need_planning_questions(self):
        message = load(ROOT / 'examples/analysis/result-partial.json')
        for mutation in ('missing', 'execution', 'bad-link', 'invented-expectation'):
            modified = copy.deepcopy(message)
            payload = modified['payload']
            if mutation == 'missing':
                payload['blocking_questions'] = []
            elif mutation == 'execution':
                payload['blocking_questions'][0]['blocks'] = 'execution'
            elif mutation == 'bad-link':
                payload['blocking_questions'][0]['scenario_ids'] = ['missing']
            else:
                payload['risk_scenarios'][1]['expected_result'] = 'Invented expectation'
            self.assertTrue(validate(modified), mutation)
        inferred = copy.deepcopy(self.message)
        inferred['payload']['risk_scenarios'][0]['expectation_status'] = 'inferred'
        self.assertTrue(validate(inferred))
        inferred['payload']['blocking_questions'] = [dict(question_id='Q1', question='Confirm expectation?', requirement_ids=['R-1'], scenario_ids=['TC-001'], blocks='planning')]
        self.assertEqual([], validate(inferred))

    def test_assumptions_gaps_and_plan_recommendation_are_structured(self):
        for collection, id_key in (('assumptions', 'assumption_id'), ('coverage_gaps', 'gap_id')):
            message = copy.deepcopy(self.message)
            entry = {id_key: 'A1', 'description': 'Synthetic limitation', 'requirement_ids': ['R-1'], 'scenario_ids': ['TC-001']}
            message['payload'][collection] = [entry]
            self.assertEqual([], validate(message))
            entry['requirement_ids'] = ['missing']
            self.assertTrue(validate(message))
        for field, value in (('test_plan_required', 'yes'), ('test_plan_reason', '')):
            message = copy.deepcopy(self.message)
            message['payload'][field] = value
            self.assertTrue(validate(message))

    def test_malformed_nested_values_return_errors(self):
        for collection, fields in (
            ('requirements', ('requirement_id', 'status', 'source')),
            ('risk_scenarios', ('scenario_id', 'requirement_id', 'expectation_status', 'priority')),
        ):
            for field in fields:
                message = copy.deepcopy(self.message)
                message['payload'][collection][0][field] = [{}]
                self.assertTrue(validate(message), (collection, field))


class ReadinessAndFeatureTests(unittest.TestCase):
    def test_readiness_matches_assignment_and_is_disjoint(self):
        for name, ready_key in (('environment.json', 'ready_scenarios'), ('data.json', 'scenario_ids')):
            message = load(ROOT / 'examples/handoffs' / name)
            self.assertEqual([], validate(message, ['TC-001', 'TC-002']))
            for values in (['TC-002'], ['TC-001', 'TC-001'], [''], [{}], ['unexpected']):
                modified = copy.deepcopy(message)
                modified['payload'][ready_key] = values
                self.assertTrue(validate(modified, ['TC-001', 'TC-002']), values)
            self.assertTrue(validate(message, ['TC-001', 'TC-002', 'missing']))
            modified = copy.deepcopy(message)
            modified['payload']['blocked_scenarios'] = ['TC-002', 'TC-002']
            self.assertTrue(validate(modified))

    def test_data_ready_requires_entire_assignment(self):
        message = load(ROOT / 'examples/handoffs/data.json')
        message['event'] = 'data_ready'
        del message['payload']['blocked_scenarios']
        self.assertEqual([], validate(message, ['TC-001']))
        self.assertTrue(validate(message, ['TC-001', 'TC-002']))

    def test_mixed_fully_blocked_and_not_run_results(self):
        message = load(ROOT / 'examples/handoffs/feature.json')
        ids = ['TC-001', 'TC-002']
        self.assertEqual([], validate(message, ids))
        message['payload']['scenario_results'][0].update(status='not_run', steps=[], evidence=[], actual_result='Time limit')
        message['payload']['uncovered_scenarios'] = ids
        self.assertEqual([], validate(message, ids))
        blocked = load(ROOT / 'examples/handoffs/feature-blocked.json')
        self.assertEqual([], validate(blocked, ids))
        for mutation in ('empty', 'omitted', 'duplicate', 'executed', 'not_run', 'wrong-event'):
            modified = copy.deepcopy(blocked)
            if mutation == 'empty':
                modified['payload'] = {}
            elif mutation == 'omitted':
                modified['payload']['scenario_results'].pop()
            elif mutation == 'duplicate':
                modified['payload']['scenario_results'][1]['scenario_id'] = 'TC-001'
            elif mutation == 'wrong-event':
                modified['event'] = 'feature_test_complete'
            else:
                modified['payload']['scenario_results'][0]['status'] = 'passed' if mutation == 'executed' else 'not_run'
            self.assertTrue(validate(modified, ids), mutation)


class HandoffTests(unittest.TestCase):
    base = ROOT / 'examples/handoffs'

    def cli(self, path, ids=(), state=None):
        message = load(path)
        command = [sys.executable, str(ROOT / 'scripts/validate_qa_result.py'), '--input', str(path),
                   '--task-id', message['task_id'], '--request-id', message['request_id'], '--agent', message['agent']]
        for identifier in ids:
            command.extend(['--scenario-id', identifier])
        if state:
            command.extend(['--investigation-state', str(state)])
        return subprocess.run(command, capture_output=True, text=True)

    def test_all_four_routes_have_valid_saved_reports_and_readable_artifacts(self):
        for route in load(self.base / 'routes.json'):
            with self.subTest(route=route['route']):
                upstream = load((self.base / route['upstream']).resolve())
                downstream = load((self.base / route['downstream']).resolve())
                self.assertEqual(upstream['task_id'], downstream['task_id'])
                self.assertNotEqual(upstream['request_id'], downstream['request_id'])
                for key in ('upstream', 'downstream', 'data'):
                    if key not in route:
                        continue
                    path = (self.base / route[key]).resolve()
                    message = load(path)
                    state = (self.base / route['state']).resolve() if key == 'upstream' and 'state' in route else None
                    result = self.cli(path, route.get('scenario_ids', []), state)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    for artifact in message['artifacts']:
                        self.assertTrue((path.parent / artifact).is_file(), artifact)

    def test_analysis_plan_preserve_ids_and_source_paths(self):
        analysis_path = ROOT / 'examples/analysis/result.json'
        plan_path = self.base / 'plan.json'
        analysis = load(analysis_path)['payload']
        plan = load(plan_path)['payload']
        for scenario in analysis['risk_scenarios']:
            coverage = next(row for row in plan['requirement_coverage'] if row['requirement_id'] == scenario['requirement_id'])
            self.assertIn(scenario['scenario_id'], coverage['scenario_ids'])
            source = lambda base, ref: (base / ref.split('#')[0]).resolve()
            self.assertEqual(source(analysis_path.parent, scenario['expected_source']), source(plan_path.parent, coverage['source']))

    def test_readiness_and_feature_preserve_environment_and_eligibility(self):
        environment, data, feature = [load(self.base / name)['payload'] for name in ('environment.json', 'data.json', 'feature.json')]
        for row in feature['scenario_results']:
            self.assertEqual(environment['environment_id'], row['environment_id'])
            self.assertEqual(data['environment_id'], row['environment_id'])
            self.assertEqual(environment['product_version'], row['product_version'])
            if row['status'] == 'passed':
                self.assertIn(row['scenario_id'], environment['ready_scenarios'])
                self.assertIn(row['scenario_id'], data['scenario_ids'])
            else:
                self.assertIn(row['scenario_id'], environment['blocked_scenarios'])

    def test_review_revision_and_files_match_automation(self):
        automation = load(self.base / 'automation.json')['payload']
        review = load(self.base / 'review.json')['payload']
        self.assertEqual(automation['current_revision'], review['reviewed_revision'])
        self.assertTrue((self.base / automation['diff_artifact']).is_file())
        for path in automation['changed_files']:
            self.assertTrue((self.base / path).is_file())

    def test_jira_fixture_uses_accepted_investigation_evidence(self):
        base = ROOT / 'examples/investigations/confirmed'
        investigation = load(base / 'result.json')
        self.assertEqual([], validate_review(investigation, load(base / 'state.json'), base))
        self.assertEqual('defect_confirmed', investigation['payload']['outcome'])
        jira = load(self.base / 'jira.json')
        accepted = {(base / ref).resolve() for ref in investigation['payload']['evidence']}
        for reference in jira['payload']['evidence']:
            self.assertIn((self.base / reference).resolve(), accepted)

    def test_cli_rejects_readiness_without_assignment_and_accepts_full_block(self):
        result = self.cli(self.base / 'environment.json')
        self.assertEqual(2, result.returncode)
        self.assertIn('--scenario-id', result.stdout)
        result = self.cli(self.base / 'feature-blocked.json', ['TC-001', 'TC-002'])
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
