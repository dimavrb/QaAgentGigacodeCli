"""Reject formal TC links, unused parameters, wrong rules and unreviewed rows."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
from combination_coverage import materialize
from content_review import validate_review
from generate_combinations import check, fingerprint, generate, prepare
from self_review_fixtures import review_for
from task_acceptance import criteria_packet
import task_exchange as exchange
import review_cycle


class BindingTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'examples/test-design'
        self.model = json.loads((base / 'combinations.json').read_text())
        self.report = json.loads((base / 'ready-combinations.json').read_text())
        self.catalog = json.loads((base / 'ready-cases.json').read_text())
        self.case = self.catalog['test_cases'][0]
        self.row = self.report['rows'][0]
        self.binding = self.row['scenario_bindings'][0]

    def errors(self):
        return check(self.model, self.report, catalog=self.catalog)

    def test_real_parameterized_case_can_cover_multiple_rows(self):
        self.assertEqual(9, len(self.report['rows']))
        self.assertEqual([], self.errors())

    def test_same_static_id_without_bindings_does_not_cover_rows(self):
        for row in self.report['rows']:
            del row['scenario_bindings']
        self.assertEqual([], check(self.model, self.report))  # mathematical design remains valid
        self.assertTrue(any('TC-ID alone' in e for e in self.errors()))

    def test_parameter_set_must_match_all_four_factors(self):
        self.binding['parameter_set_id'] = self.case['parameter_sets'][1]['parameter_set_id']
        self.assertTrue(any('complete row assignment' in e for e in self.errors()))

    def test_static_case_cannot_be_reused_for_incompatible_assignments(self):
        self.case.pop('parameter_sets')
        self.case['combination_values'] = self.row['values'].copy()
        self.case['preconditions'] = ['role user, owner other, channel api, browser none']
        self.case['steps'] = [dict(name='Открыть чужую запись через API', expected_result='Чужая запись не раскрывается')]
        for row in self.report['rows']:
            binding = row['scenario_bindings'][0]
            binding.pop('parameter_set_id')
            binding['factor_refs'] = {k: '/preconditions/0' for k in self.model['factors']}
        self.assertTrue(any('complete row assignment' in e for e in self.errors()))

    def test_distinct_static_cases_with_concrete_conditions_are_valid(self):
        cases = []
        for row, params in zip(self.report['rows'], self.case['parameter_sets']):
            case = copy.deepcopy(self.case)
            case.pop('parameter_sets')
            case['scenario_id'] += '-' + row['combination_id']
            case['title'] = 'Запись — комбинация ' + row['combination_id'] + ' — доступ соответствует правилу'
            case['combination_values'] = params['values'].copy()
            substitutions = dict(params['parameters'], **params['values'])
            case['preconditions'] = [materialize(s, substitutions) for s in case['preconditions']]
            case['steps'] = [{k: materialize(v, substitutions) for k, v in step.items()} for step in case['steps']]
            row['scenario_ids'] = [case['scenario_id']]
            row['scenario_bindings'][0]['scenario_id'] = case['scenario_id']
            row['scenario_bindings'][0].pop('parameter_set_id')
            cases.append(case)
        self.catalog['test_cases'] = cases
        self.assertEqual([], self.errors())

    def test_unused_parameter_and_metadata_reference_do_not_prove_setup(self):
        self.case['preconditions'][1] = 'Аккаунт имеет роль user'
        self.assertTrue(any('parameter is not used' in e for e in self.errors()))
        self.binding['factor_refs']['role'] = '/parameter_sets/0/values/role'
        self.assertTrue(any('reference must point' in e for e in self.errors()))

    def test_missing_or_unresolved_factor_cannot_hide_behind_valid_tc_id(self):
        self.binding['factor_refs'].pop('browser')
        self.assertTrue(any('every factor' in e for e in self.errors()))
        self.binding['factor_refs']['browser'] = '/preconditions/3'
        self.case['steps'][0]['name'] += ' с {{invented_resource}}'
        self.assertTrue(any('unresolved parameter' in e for e in self.errors()))

    def test_wrong_rule_unknown_expectation_and_uncovered_rule_are_rejected(self):
        self.binding['rule_id'] = 'RULE-3'
        self.assertTrue(any('does not apply' in e for e in self.errors()))
        self.binding['rule_id'] = 'RULE-2'
        self.model['behavior_rules'][1]['status'] = 'unknown'
        self.report['model_sha256'] = fingerprint(self.model)
        self.assertTrue(any('inferred/unknown' in e for e in self.errors()))
        self.model['behavior_rules'][1]['status'] = 'confirmed'
        self.model['behavior_rules'].append(dict(rule_id='SECOND', requirement_id='R-1', source='requirements.md#access',
            conditions={'owner': 'other'}, status='confirmed', expected_result='Additional confirmed observation'))
        self.report['model_sha256'] = fingerprint(self.model)
        self.assertTrue(any('every applicable' in e for e in self.errors()))

    def test_expected_reference_cannot_point_to_title_or_preparation(self):
        for ref in ('/title', '/preconditions/1', '/steps/0/name', '/steps/99/expected_result'):
            with self.subTest(ref=ref):
                self.binding['expected_refs'] = [ref]
                self.assertTrue(self.errors())

    def test_malformed_or_nested_templates_do_not_create_a_concrete_case(self):
        for value in ('Открыть {{}}', 'Открыть {{unclosed', 'Открыть {{{role}}}'):
            with self.subTest(value=value):
                self.case['steps'][0]['name'] = value
                self.assertTrue(any('parameter' in e for e in self.errors()))

    def test_pairwise_success_cannot_hide_a_business_rule_without_a_scenario(self):
        _, candidates, _ = prepare(self.model)
        missing = next(v for v in candidates if v not in [r['values'] for r in self.report['rows']])
        self.model['behavior_rules'].append(dict(rule_id='CRITICAL', requirement_id='R-1', source='requirements.md',
            status='confirmed', conditions=missing, expected_result='Critical distinguishing observation'))
        self.report['model_sha256'] = fingerprint(self.model)
        self.assertEqual([], check(self.model, self.report))
        self.assertTrue(any('no covered row' in e for e in self.errors()))

    def test_duplicate_bindings_links_and_parameter_overrides_fail(self):
        self.report['rows'][1]['scenario_bindings'][0]['binding_id'] = self.binding['binding_id']
        self.assertTrue(any('duplicate binding_id' in e for e in self.errors()))
        self.case['parameter_sets'][0]['parameters']['role'] = 'admin'
        self.assertTrue(any('cannot override' in e for e in self.errors()))

    def test_cli_rejects_formal_coverage_and_accepts_ready_example(self):
        command = [sys.executable, str(ROOT / 'scripts/generate_combinations.py'), '--model',
            str(ROOT / 'examples/test-design/combinations.json'), '--catalog', str(ROOT / 'examples/test-design/ready-cases.json')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rows.json'
            path.write_text(json.dumps(generate(self.model)))
            bad = subprocess.run(command + ['--check', str(path)], capture_output=True, text=True)
            self.assertEqual(2, bad.returncode, bad.stdout + bad.stderr)
        good = subprocess.run(command + ['--check', str(ROOT / 'examples/test-design/ready-combinations.json')], capture_output=True, text=True)
        self.assertEqual(0, good.returncode, good.stdout + good.stderr)


class CombinationAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for name in ('requirements.md', 'combinations.json', 'ready-combinations.json', 'ready-cases.json'):
            shutil.copy(ROOT / 'examples/test-design' / name, self.root / name)
        self.model = json.loads((self.root / 'combinations.json').read_text())
        self.report = json.loads((self.root / 'ready-combinations.json').read_text())
        self.state = self.root / 'task.json'
        self.definition = dict(criterion_id='C', description='Confirmed access combination coverage', source=str(self.root/'requirements.md'),
            required=True, kind='combination_coverage', combination_bundle_id='access', model_sha256=fingerprint(self.model),
            required_reviews=[dict(scope='coverage', agent='qa-result-reviewer')])
        exchange.init_task(self.state, 'T', 'Synthetic design coverage', dict(schema_version=1, criteria=[self.definition]))
        self.author_payload = dict(plan_artifact='requirements.md', plan_revision='v1', test_cases_artifact='ready-cases.json',
            requirement_coverage=[dict(requirement_id=rid, source='requirements.md', scenario_ids=['TC-ACCESS'], priority='high',
                verification='UI/API from parameter set', evidence='Concrete data observation from parameter set', status='ready') for rid in ('R-1', 'R-2', 'R-3')],
            open_questions=[], combination_coverage=[dict(bundle_id='access', model_artifact='combinations.json',
                combinations_artifact='ready-combinations.json', catalog_artifact='ready-cases.json')])

    def assignment(self, rid, agent):
        return dict(task_id='T', request_id=rid, agent=agent, stage='design', goal='Synthetic coverage fixture',
            acceptance_criteria=['Conditions, rules and distinguishing observations are mapped'], criterion_ids=['C'], scenario_ids=[],
            stop_condition='Return result', expected_artifact='QA JSON', result_path=str(self.root/(rid+'.json')), inputs=[], depends_on=[])

    def submit(self, assignment, event, payload, owner):
        rid = assignment['request_id']
        exchange.assign(self.state, assignment, self.root, None)
        handoff = exchange.prepare_handoff(self.state, rid, None)['result']
        exchange.start_request(self.state, rid, owner, handoff['delivery_id'], None)
        message = dict(task_id='T', request_id=rid, agent=assignment['agent'], event=event,
            summary='Synthetic design review fixture; no product execution', artifacts=[], blockers=[], payload=payload, self_review=review_for())
        Path(assignment['result_path']).write_text(json.dumps(message))
        exchange.submit_result(self.state, rid, None)
        exchange.review_result(self.state, rid, 'Synthetic content decision', True, None)

    def author(self):
        self.submit(self.assignment('author', 'qa-test-plan-writer'), 'plan_ready', self.author_payload, 'author-session')

    def reviewer(self, rid='review', owner='independent-session', omit=0, extras=None):
        row = exchange.read_state(self.state)['requests']['author']
        target = dict(request_id='author', result_path=row['result']['path'], artifact_hashes=row['result']['artifact_hashes'], scope='coverage')
        assignment = self.assignment(rid, 'qa-result-reviewer')
        assignment.update(review_target=target, review_criteria_ids=['C'], inputs=list(target['artifact_hashes']))
        proof = self.root / (rid + '-notes.md')
        proof.write_text('Synthetic independent review notes about every condition, rule and observation')
        checks = [dict(bundle_id='access', binding_id=b['binding_id'], status='passed',
            note='Synthetic conditions, rule source and concrete expected observation reviewed', evidence=[str(proof)])
            for r in self.report['rows'] for b in r['scenario_bindings']]
        if omit:
            checks = checks[:-omit]
        payload = dict(target_request_id='author', target_sha256=target['artifact_hashes'][target['result_path']], review_scope='coverage',
            reviewed_revision='v1',
            checks=[dict(criterion_id='C', status='passed', note='Synthetic coverage review', evidence=['requirements.md'])],
            combination_checks=checks, findings=[], limitations=['Synthetic design only'])
        payload.update(extras or {})
        self.submit(assignment, 'review_passed', payload, owner)

    def assess(self, reviews=('review',)):
        exchange.assess_criteria(self.state, dict(assessments=[dict(criterion_id='C', status='verified', decision='Synthetic coverage reviewed',
            request_ids=['author', *reviews], evidence=[str(self.root/'author.json')])]), None)

    def test_independent_review_of_every_binding_allows_completion(self):
        self.author()
        self.reviewer()
        self.assess()
        exchange.finish_task(self.state, 'passed', 'Synthetic design complete', None)
        self.assertEqual('completed', exchange.read_state(self.state)['execution_status'])

    def test_generic_review_or_one_missing_binding_cannot_verify(self):
        self.author()
        self.reviewer('generic', extras={'combination_checks': []})
        self.reviewer('partial', omit=1)
        for rid in ('generic', 'partial'):
            with self.subTest(rid=rid), self.assertRaisesRegex(ValueError, 'missing independent'):
                self.assess([rid])

    def test_author_session_cannot_review_its_own_combinations(self):
        self.author()
        with self.assertRaisesRegex(ValueError, 'different owner/session'):
            self.reviewer(owner='author-session')

    def test_foreign_binding_cannot_be_submitted_as_review_of_target(self):
        self.author()
        with self.assertRaisesRegex(ValueError, 'outside the assigned target'):
            self.reviewer(extras={'combination_checks': [dict(bundle_id='other', binding_id='fake', status='passed',
                note='Wrong target', evidence=['requirements.md'])]})

    def test_nested_model_catalog_rows_and_sources_are_bound_even_without_top_artifacts(self):
        self.author()
        hashes = exchange.read_state(self.state)['requests']['author']['result']['artifact_hashes']
        for name in ('combinations.json', 'ready-cases.json', 'ready-combinations.json', 'requirements.md'):
            self.assertIn(str(self.root / name), hashes)
        self.reviewer()
        self.assess()
        (self.root/'requirements.md').write_text('Changed primary requirement after acceptance')
        with self.assertRaises(ValueError):
            exchange.finish_task(self.state, 'passed', 'Changed source cannot finish', None)

    def test_changed_parameters_after_review_cannot_verify(self):
        self.author()
        self.reviewer()
        catalog = self.root/'ready-cases.json'
        data = json.loads(catalog.read_text())
        data['test_cases'][0]['parameter_sets'][0]['parameters']['observation'] = 'Changed expectation'
        catalog.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.assess()

    def test_nested_combination_review_evidence_is_hashed_and_rechecked(self):
        self.author()
        self.reviewer()
        self.assess()
        proof = self.root/'review-notes.md'
        self.assertIn(str(proof), exchange.read_state(self.state)['requests']['review']['result']['artifact_hashes'])
        proof.write_text('Changed reviewer notes')
        with self.assertRaises(ValueError):
            exchange.finish_task(self.state, 'passed', 'Changed review cannot finish', None)

    def test_bundle_cannot_cover_another_catalog(self):
        other = self.root/'other.json'
        shutil.copy(self.root/'ready-cases.json', other)
        self.author_payload['combination_coverage'][0]['catalog_artifact'] = 'other.json'
        with self.assertRaisesRegex(ValueError, 'plan test_cases_artifact'):
            self.author()

    def test_changed_source_link_during_snapshot_collection_cannot_omit_new_source_hash(self):
        self.author()
        assignment = exchange.read_state(self.state)['requests']['author']['assignment']
        source = self.root/'new-source.md'
        source.write_text('Another synthetic requirement snapshot')
        original = exchange.fingerprint
        changed = False
        def race(path):
            nonlocal changed
            if not changed:
                changed = True
                self.model['scope_sources'].append(source.name)
                (self.root/'combinations.json').write_text(json.dumps(self.model))
                self.report['model_sha256'] = fingerprint(self.model)
                (self.root/'ready-combinations.json').write_text(json.dumps(self.report))
            return original(path)
        with patch.object(exchange, 'fingerprint', side_effect=race):
            with self.assertRaisesRegex(ValueError, 'links changed during validation'):
                exchange.inspect_result(assignment)

    def test_mutable_remote_source_needs_a_local_snapshot(self):
        self.model['behavior_rules'][0]['source'] = 'https://example.invalid/mutable'
        (self.root/'combinations.json').write_text(json.dumps(self.model))
        self.report['model_sha256'] = fingerprint(self.model)
        (self.root/'ready-combinations.json').write_text(json.dumps(self.report))
        with self.assertRaisesRegex(ValueError, 'local snapshot'):
            self.author()

    def test_missing_primary_source_blocks_submission(self):
        (self.root/'requirements.md').unlink()
        with self.assertRaisesRegex(ValueError, 'missing or empty'):
            self.author()

    def test_weaker_model_cannot_replace_agreed_scope(self):
        self.model['interaction_groups'] = []
        (self.root/'combinations.json').write_text(json.dumps(self.model))
        self.report['model_sha256'] = fingerprint(self.model)
        (self.root/'ready-combinations.json').write_text(json.dumps(self.report))
        self.author()
        self.reviewer()
        with self.assertRaisesRegex(ValueError, 'agreed criterion scope'):
            self.assess()

    def test_typed_scope_requires_frozen_model_and_independent_review(self):
        for field in ('model_sha256', 'combination_bundle_id', 'required_reviews'):
            definition = copy.deepcopy(self.definition)
            definition.pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                criteria_packet(dict(schema_version=1, criteria=[definition]))

    def test_failed_unknown_or_duplicate_binding_checks_cannot_be_passed(self):
        payload = dict(target_request_id='a', target_sha256='a'*64, review_scope='coverage', findings=[], limitations=[],
            checks=[dict(criterion_id='C', status='passed', note='n', evidence=['e'])])
        check = dict(bundle_id='access', binding_id='b', status='unknown', note='No observation', evidence=['e'])
        for status in ('unknown', 'failed'):
            check['status'] = status
            payload['combination_checks'] = [check]
            self.assertTrue(validate_review(payload, 'review_passed'))
        check['status'] = 'passed'
        payload['combination_checks'] = [check, copy.deepcopy(check)]
        self.assertTrue(validate_review(payload, 'review_passed'))

    def run_cycle(self, generic=False):
        adapter = dict(kind='command', argv=[sys.executable, str(ROOT/'tests/fixtures/combination_agent.py')])
        cycle = self.root/'cycle.json'
        reviewer = copy.deepcopy(adapter)
        if generic:
            reviewer['argv'].append('--generic-review')
        plan = dict(schema_version=1, author_plan=dict(schema_version=1, task_state=str(self.state), workspace=str(self.root),
            scope_kind='synthetic', adapter=adapter, steps=[dict(step_id='author', assignment=self.assignment('author', 'qa-test-plan-writer'), max_wall_seconds=5)]),
            reviewers=[dict(scope='coverage', agent='qa-result-reviewer', adapter=reviewer, max_wall_seconds=5)])
        review_cycle.initialize(cycle, plan, self.root)
        self.addCleanup(lambda: review_cycle.cancel(cycle, 'Test cleanup'))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = review_cycle.tick(cycle)
            if result['status'] in ('accepted', 'blocked'):
                return result
            time.sleep(.05)
        self.fail('Synthetic combination cycle timed out')

    def test_real_process_cycle_requires_per_binding_review_before_author_acceptance(self):
        result = self.run_cycle(generic=True)
        self.assertEqual('blocked', result['status'], result)
        self.assertIn('missing independent', result['reason'])
        self.assertEqual('reported', exchange.read_state(self.state)['requests']['author']['status'])

    def test_real_process_cycle_accepts_complete_binding_review(self):
        result = self.run_cycle()
        self.assertEqual('accepted', result['status'], result.get('reason'))
        self.assess(result['review_request_ids'])
        exchange.finish_task(self.state, 'passed', 'Synthetic process coverage complete', None)


if __name__ == '__main__':
    unittest.main()
