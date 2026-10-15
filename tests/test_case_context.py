"""Delivery and current-byte validation of examples; no live model-quality claims."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import context_builder as cb
import execution_supervisor as supervisor
import review_cycle
import task_exchange as exchange
from self_review_fixtures import review_for


class CaseProjectionTests(unittest.TestCase):
    def test_required_roles_receive_whole_valid_examples_in_both_modes(self):
        roles = ('qa-test-plan-writer', 'qa-feature-tester', 'qa-test-automation',
                 'qa-autotest-reviewer', 'qa-result-reviewer',
                 'qa-test-data-builder', 'qa-test-env-configurator')
        for role in roles:
            for kind, filename in cb.CASE_EXAMPLES.items():
                for mode in ('compact', 'full'):
                    with self.subTest(role=role, kind=kind, mode=mode):
                        spec = cb.profile(dict(agent=role, test_type=kind), dict(context_mode=mode))
                        docs = spec['documents']
                        self.assertIn('docs/TEST_CASE_REVIEW.md', docs)
                        self.assertIn('docs/TEST_CASE_STYLE.md', docs)
                        self.assertIn(f'examples/test-cases/{kind}/INDEX.md', docs)
                        examples = [p for p in docs if p.endswith('.json')]
                        path = f'examples/test-cases/{kind}/{filename}'
                        self.assertEqual([path], examples)
                        self.assertEqual((ROOT/path).read_text(), cb.render((ROOT/path).read_text(), docs[path], role))

    def test_selected_example_replaces_baseline_and_mixed_catalog_gets_each_index(self):
        paths = ['examples/test-cases/api/cancellation.json', 'examples/test-cases/ui/filter.json']
        docs = cb.profile(dict(agent='qa-result-reviewer', test_type='api',
                               review_target=dict(scope='test_cases')),
                          dict(instruction_paths=paths))['documents']
        self.assertEqual(set(paths), {p for p in docs if p.endswith('.json')})
        for kind in ('api', 'ui'):
            self.assertIn(f'examples/test-cases/{kind}/INDEX.md', docs)

    def test_direct_catalog_reviewer_gets_standards_without_manual_extra_paths(self):
        docs = cb.profile(dict(agent='qa-result-reviewer', review_target=dict(scope='test_cases')), {})['documents']
        for path in ('docs/TEST_CASE_REVIEW.md', 'docs/TEST_CASE_STYLE.md',
                     'docs/TEST_CASE_SELECTION.md', 'docs/TEST_PLAN_HTML.md', '.gigacode/skills/qa-test-cases/SKILL.md'):
            self.assertIn(path, docs)
        self.assertFalse(any(p.startswith('examples/') for p in docs))

    def test_unrelated_roles_do_not_receive_whole_example_library(self):
        for role in ('qa-conclusion-reviewer', 'qa-test-data-builder', 'qa-test-env-configurator'):
            docs = cb.profile(dict(agent=role), {})['documents']
            self.assertNotIn('docs/TEST_CASE_REVIEW.md', docs)
            self.assertFalse(any(p.startswith('examples/') for p in docs))

    def test_unknown_type_and_escaping_example_are_rejected(self):
        for kind in ('rest', True, [], ''):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'test_type'):
                cb.profile(dict(agent='qa-test-plan-writer', test_type=kind), {})
        with self.assertRaises(ValueError):
            cb.profile(dict(agent='qa-test-plan-writer'),
                       dict(instruction_paths=['examples/test-cases/api/../../outside.json']))

    def test_invalid_example_prevents_profile_in_compact_and_full_mode(self):
        original = Path.read_text
        example = ROOT/'examples/test-cases/ui/create.json'
        for text in ('{"schema_version": true, "test_cases": []}', '{"test_cases": NaN}', '{}'):
            for mode in ('compact', 'full'):
                def read(path, *args, **kwargs):
                    return text if path == example else original(path, *args, **kwargs)
                with self.subTest(text=text, mode=mode), patch.object(Path, 'read_text', read):
                    with self.assertRaisesRegex(ValueError, 'Invalid test-case example'):
                        cb.profile(dict(agent='qa-feature-tester', test_type='ui'), dict(context_mode=mode))

    def test_selected_example_hash_is_enforced_before_handoff(self):
        spec = cb.profile(dict(agent='qa-test-plan-writer', test_type='kafka'), {})
        step = dict(context_profile=spec,
                    instruction_hashes={p:exchange.fingerprint(ROOT/p) for p in spec['documents']},
                    context_builder_sha256=exchange.fingerprint(Path(cb.__file__)))
        example = 'examples/test-cases/kafka/persistence.json'
        self.assertEqual((ROOT/example).read_text(), cb.snapshots(step)[example])
        original = Path.read_bytes
        def mutated(path):
            return original(path) + b' ' if path == ROOT/example else original(path)
        with patch.object(Path, 'read_bytes', mutated), self.assertRaisesRegex(ValueError, 'instructions changed'):
            cb.snapshots(step)


class CatalogInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'catalog.json'
        self.handoff = dict(assignment=dict(inputs=[str(self.path)]), sources=[], answer_sources=[])

    def test_current_catalog_report_and_sources_use_exact_bytes_without_commands(self):
        raw = (ROOT/'examples/test-cases/api/cancellation.json').read_bytes().replace(b'\n', b'\r\n')
        self.path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.handoff['sources'] = [dict(request_id='author', result_path=str(self.path))]
        with patch('subprocess.run', side_effect=AssertionError('no subprocess')):
            result = cb.assemble(self.handoff, {}, {str(self.path):digest})
        report = result['catalog_validations'][str(self.path)]
        self.assertTrue(report['valid'])
        self.assertEqual([], report['errors'])
        self.assertEqual(1, report['case_count'])
        self.assertEqual(digest, report['sha256'])
        self.assertEqual(raw.decode(), result['input_contents'][str(self.path)])
        self.assertEqual(digest, result['source_reports']['author']['sha256'])

    def test_invalid_catalog_reports_actual_new_data_not_prior_success(self):
        data = json.loads((ROOT/'examples/test-cases/ui/create.json').read_text())
        data['test_cases'][0]['steps'][0]['test_data'] = ''
        self.path.write_text(json.dumps(data))
        result = cb.assemble(self.handoff, {})
        report = result['catalog_validations'][str(self.path)]
        self.assertFalse(report['valid'])
        self.assertEqual(exchange.fingerprint(self.path), report['sha256'])
        self.assertTrue(any('test_data' in error for error in report['errors']))

    def test_source_reference_and_validation_remain_bound_to_one_read(self):
        raw = (ROOT/'examples/test-cases/ui/filter.json').read_bytes()
        self.path.write_bytes(raw)
        self.handoff['sources'] = [dict(request_id='author', result_path=str(self.path))]
        original = Path.read_bytes
        reads = []
        def changing_read(path):
            data = original(path)
            if path == self.path:
                reads.append(path)
                path.write_text('{"test_cases":[]}')
            return data
        with patch.object(Path, 'read_bytes', changing_read):
            result = cb.assemble(self.handoff, {})
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual([self.path], reads)
        self.assertEqual(digest, result['catalog_validations'][str(self.path)]['sha256'])
        self.assertEqual(digest, result['source_reports']['author']['sha256'])
        self.assertEqual(raw.decode(), result['input_contents'][str(self.path)])

    def test_malformed_duplicate_and_deep_json_are_controlled_diagnostics(self):
        for text in ('{"test_cases":', '{"test_cases":[],"test_cases":[]}',
                     '{"test_cases":NaN}', '['*2000 + '0' + ']'*2000):
            with self.subTest(text=text[:50]):
                self.path.write_text(text)
                report = cb.assemble(self.handoff, {})['catalog_validations'][str(self.path)]
                self.assertEqual('json', report['kind'])
                self.assertFalse(report['valid'])
                self.assertTrue(report['errors'])

    def test_non_catalog_and_hidden_feedback_are_not_catalog_checks(self):
        for raw in (b'{"requirement":"not a catalog"}', b'plain source', b'\xff'):
            self.path.write_bytes(raw)
            self.assertEqual({}, cb.assemble(self.handoff, {})['catalog_validations'])
        self.path.write_text('{"test_cases":[]}')
        self.handoff['assignment']['blind_review_exclusions'] = [str(self.path)]
        result = cb.assemble(self.handoff, {})
        self.assertEqual({}, result['catalog_validations'])
        self.assertNotIn('test_cases', json.dumps(result['input_contents']))

    def test_changed_expected_input_is_rejected_before_validation(self):
        self.path.write_bytes((ROOT/'examples/test-cases/ui/filter.json').read_bytes())
        digest = exchange.fingerprint(self.path)
        self.path.write_bytes(self.path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'input changed'):
            cb.assemble(self.handoff, {}, {str(self.path):digest})


class CaseReviewCycleTests(unittest.TestCase):
    def test_review_cycle_keeps_selected_type_example_and_current_catalog_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            registry = root/'task.json'
            exchange.init_task(registry, 'QA-CASES', 'Synthetic case review')
            catalog = root/'catalog.json'
            catalog.write_bytes((ROOT/'examples/test-cases/api/cancellation.json').read_bytes())
            (root/'plan.md').write_text('Synthetic coverage only')
            assignment = dict(task_id='QA-CASES', request_id='author', agent='qa-test-plan-writer',
                              stage='plan', goal='Synthetic', acceptance_criteria=['Check catalog'],
                              criterion_ids=[], scenario_ids=[], stop_condition='Report',
                              expected_artifact='QA JSON', result_path=str(root/'author.json'),
                              inputs=[], depends_on=[], test_type='api')
            source = supervisor.normalize_plan(dict(schema_version=1, workspace=str(root), task_state=str(registry),
                scope_kind='synthetic', steps=[dict(step_id='author', assignment=assignment,
                    instruction_paths=['examples/test-cases/api/cancellation.json'])],
                adapter=dict(kind='gigacode', executable='fixture-cli')), root)
            normalized = source['steps'][0]['assignment']
            exchange.assign(registry, normalized, root, None)
            payload = dict(plan_artifact='plan.md', test_cases_artifact='catalog.json', plan_revision='v1',
                           requirement_coverage=[dict(requirement_id='R1', source='synthetic',
                               scenario_ids=['EX-API-006'], priority='high', verification='API',
                               evidence='response', status='ready')], open_questions=[])
            exchange.atomic_write(root/'author.json', dict(task_id='QA-CASES', request_id='author',
                agent='qa-test-plan-writer', event='plan_ready', summary='Synthetic catalog',
                artifacts=['plan.md', 'catalog.json'], blockers=[], self_review=review_for(), payload=payload))
            state = dict(task_id='QA-CASES', feedback=[], plan=dict(author_plan=source, max_active_seconds=30))
            slot = dict(agent='qa-result-reviewer', scope='oracle', max_wall_seconds=5,
                        adapter=source['adapter'])
            plan = review_cycle.review_plan(state, slot, 'author', root)
            reviewer = supervisor.normalize_plan(plan, root)['steps'][0]
            self.assertEqual('api', reviewer['assignment']['test_type'])
            docs = reviewer['context_profile']['documents']
            self.assertIn('examples/test-cases/api/cancellation.json', docs)
            self.assertNotIn('examples/test-cases/api/create.json', docs)
            self.assertIn('docs/TEST_CASE_REVIEW.md', docs)
            handoff = dict(assignment=reviewer['assignment'], sources=[], answer_sources=[])
            report = cb.assemble(handoff, {}, reviewer['input_hashes'])['catalog_validations'][str(catalog)]
            self.assertTrue(report['valid'])
            self.assertEqual(exchange.fingerprint(catalog), report['sha256'])


if __name__ == '__main__':
    unittest.main()
