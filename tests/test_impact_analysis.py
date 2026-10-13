"""Regression-selection safety and real local Git/registry integration."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import impact_analysis as impact
import capture_impact_changes as capture
import task_exchange as exchange
import context_builder
from qa_contract import validate_message

spec = importlib.util.spec_from_file_location('impact_demo', ROOT / 'examples/impact-analysis/build_demo.py')
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = demo.create_demo(Path(self.temp.name) / 'demo')
        self.input = self.root / 'impact-input.json'
        self.message = impact.read(self.root / 'result.json')

    def mutate(self, change):
        value = impact.read(self.input)
        change(value)
        self.input.write_text(json.dumps(value))

    def selection(self):
        return impact.build_selection(self.input)

    def assert_full(self):
        result = self.selection()
        self.assertEqual('full', result['selection_mode'])
        self.assertEqual([], result['excluded_scenario_ids'])
        self.assertEqual(7, len(result['run_scenario_ids']))
        return result

    def test_transitive_impact_and_mandatory_new_failed_unknown_are_preserved(self):
        result = self.selection()
        self.assertEqual(['export', 'orders'], result['affected_component_ids'])
        self.assertEqual(['TC-PAY'], result['excluded_scenario_ids'])
        self.assertEqual(['TC-UNKNOWN'], result['review_scenario_ids'])
        reasons = {r['scenario_id']: r['reasons'] for r in result['decisions']}
        for sid, reason in [('TC-EXPORT', 'impacted'), ('TC-LOGIN', 'mandatory'), ('TC-NEW', 'new'),
                            ('TC-FAIL', 'previously_failed'), ('TC-UNKNOWN', 'unknown_mapping')]:
            self.assertIn(reason, reasons[sid])

    def test_dependency_direction_does_not_select_provider_when_only_consumer_changes(self):
        self.mutate(lambda v: v['change_components'][0].update(component_ids=['export']))
        result = self.selection()
        self.assertEqual(['export'], result['affected_component_ids'])
        self.assertIn('TC-ORDER', result['excluded_scenario_ids'])
        self.assertIn('TC-EXPORT', result['run_scenario_ids'])

    def test_cycles_terminate_and_keep_impacted_tests(self):
        self.mutate(lambda v: v['components'][0].update(depends_on=['export']))
        self.assertEqual(['export', 'orders'], self.selection()['affected_component_ids'])

    def test_each_incomplete_input_and_force_full_prevent_exclusions(self):
        original = self.input.read_text()
        for key in ('dependencies', 'test_mapping', 'history'):
            with self.subTest(key=key):
                self.input.write_text(original)
                self.mutate(lambda v: v['completeness'].update({key: False}))
                self.assert_full()
        self.input.write_text(original)
        self.mutate(lambda v: v['policy'].update(force_full=True))
        self.assert_full()

    def test_unmapped_change_uses_full_fallback_instead_of_silent_skip(self):
        self.mutate(lambda v: v.update(change_components=[]))
        result = self.assert_full()
        self.assertIn('unmapped_change:CH-001', result['fallback_reasons'])
        self.assertTrue(result['coverage_gaps'])

    def test_uncovered_component_preserves_gap_even_after_full_selection(self):
        def change(v):
            v['components'].append(dict(component_id='uncovered', depends_on=[], evidence=['requirements.md']))
            v['change_components'][0]['component_ids'] = ['uncovered']
        self.mutate(change)
        self.assertEqual([dict(change_id='CH-001', reason='no_confirmed_test_link')], self.assert_full()['coverage_gaps'])

    def test_unknown_mapping_can_never_be_excluded(self):
        def change(v):
            next(t for t in v['tests'] if t['scenario_id'] == 'TC-PAY')['mapping_status'] = 'unknown'
        self.mutate(change)
        result = self.selection()
        self.assertIn('TC-PAY', result['run_scenario_ids'])
        self.assertIn('TC-PAY', result['review_scenario_ids'])

    def test_partial_duration_is_not_reported_as_total(self):
        duration = self.selection()['estimated_serial_duration']['selected']
        self.assertIsNone(duration['total_seconds'])
        self.assertEqual(50, duration['known_seconds'])
        self.assertEqual(['TC-UNKNOWN'], duration['missing_scenario_ids'])

    def test_additional_risk_can_only_expand_the_minimum(self):
        result = impact.build_selection(self.input, [dict(scenario_id='TC-PAY', reason='Дополнительный риск', evidence=['requirements.md#payment'])])
        self.assertEqual([], result['excluded_scenario_ids'])
        with self.assertRaisesRegex(ValueError, 'unknown scenarios'):
            impact.build_selection(self.input, [dict(scenario_id='invented', reason='risk', evidence=['requirements.md'])])

    def test_missing_stale_or_mismatched_primary_sources_are_rejected(self):
        original = (self.root / 'requirements.md').read_text()
        (self.root / 'requirements.md').write_text(original + '\nchanged')
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            self.selection()
        (self.root / 'requirements.md').write_text(original)
        self.mutate(lambda v: v.update(target_revision='different-release'))
        with self.assertRaisesRegex(ValueError, 'revisions differ'):
            self.selection()

    def test_source_change_during_selection_is_not_blessed_by_a_new_hash(self):
        real = impact.select_regression
        def change_source(*args, **kwargs):
            result = real(*args, **kwargs)
            (self.root / 'history.json').write_text('{}')
            return result
        with patch.object(impact, 'select_regression', side_effect=change_source):
            with self.assertRaisesRegex(ValueError, 'changed during selection'):
                self.selection()

    def test_incomplete_change_set_falls_back_even_with_valid_mapping(self):
        changes = impact.read(self.root / 'changes.json')
        changes['complete'] = False
        (self.root / 'changes.json').write_text(json.dumps(changes))
        self.mutate(lambda v: v['change_set'].update(sha256=impact.digest(self.root / 'changes.json')))
        self.assertIn('incomplete_changes', self.assert_full()['fallback_reasons'])

    def test_incomplete_catalog_unknown_graph_ids_and_duplicate_ids_are_rejected(self):
        original = self.input.read_text()
        mutations = [lambda v: v['tests'].pop(),
                     lambda v: v['components'][0].update(depends_on=['unknown']),
                     lambda v: v['tests'].append(copy.deepcopy(v['tests'][0])),
                     lambda v: v['tests'][0].update(component_ids=['unknown']),
                     lambda v: v['policy'].update(mandatory_scenario_ids=['unknown'])]
        for change in mutations:
            with self.subTest(change=change):
                self.input.write_text(original)
                self.mutate(change)
                with self.assertRaises(ValueError): self.selection()

    def test_unfrozen_evidence_and_negative_non_numeric_durations_are_rejected(self):
        original = self.input.read_text()
        self.mutate(lambda v: v['components'][0].update(evidence=['unfrozen.md']))
        with self.assertRaisesRegex(ValueError, 'frozen local source'): self.selection()
        for duration in (-1, True, float('inf'), '10'):
            with self.subTest(duration=duration):
                self.input.write_text(original)
                self.mutate(lambda v: v['tests'][0].update(duration_seconds=duration))
                with self.assertRaises(ValueError): self.selection()

    def test_recomputed_bundle_rejects_author_narrowing_or_fake_duration(self):
        saved = impact.read(self.root / 'selection.json')
        for key, value in [('run_scenario_ids', ['TC-ORDER']), ('estimated_serial_duration', {})]:
            bad = copy.deepcopy(saved); bad[key] = value
            (self.root / 'selection.json').write_text(json.dumps(bad))
            with self.assertRaisesRegex(ValueError, 'recomputed'):
                impact.check_impact_bundle(self.message, self.root, self.input)

    def test_author_cannot_replace_coordinator_manifest(self):
        other = self.root / 'other-input.json'
        other.write_text(self.input.read_text())
        message = copy.deepcopy(self.message)
        message['payload']['impact_input'] = str(other)
        with self.assertRaisesRegex(ValueError, 'coordinator'):
            impact.check_impact_bundle(message, self.root, self.input)

    def test_contract_requires_whole_assignment_and_keeps_review_in_run(self):
        p = self.message['payload']
        args = ('QA-IMPACT', 'QA-IMPACT-1', 'qa-impact-analyst')
        self.assertEqual([], validate_message(self.message, *args, p['run_scenario_ids'] + p['excluded_scenario_ids']))
        self.assertTrue(validate_message(self.message, *args, ['TC-ORDER']))
        p['run_scenario_ids'].remove('TC-UNKNOWN')
        self.assertTrue(validate_message(self.message, *args))

    def test_cli_requires_independent_input_and_rejects_overwrite(self):
        command = [sys.executable, str(ROOT / 'scripts/validate_qa_result.py'), '--input', str(self.root / 'result.json'),
                   '--task-id', 'QA-IMPACT', '--request-id', 'QA-IMPACT-1', '--agent', 'qa-impact-analyst']
        self.assertEqual(2, subprocess.run(command, capture_output=True).returncode)
        self.assertEqual(0, subprocess.run(command + ['--impact-input', str(self.input), '--require-self-review'], capture_output=True).returncode)
        select = [sys.executable, str(ROOT / 'scripts/select_regression.py'), '--input', str(self.input), '--output', str(self.root / 'new-selection.json')]
        self.assertEqual(0, subprocess.run(select, capture_output=True).returncode)
        self.assertEqual(2, subprocess.run(select, capture_output=True).returncode)

    def test_registry_freezes_nested_sources_and_limits_ready_handoff(self):
        state = self.root / 'task.json'
        exchange.init_task(state, 'QA-IMPACT', 'Demo', criteria=impact.read(self.root / 'criteria.json'))
        assignment = impact.read(self.root / 'assignment.json')
        exchange.assign(state, assignment, self.root, None)
        handoff = exchange.prepare_handoff(state, 'QA-IMPACT-1', None)['result']
        self.assertIn(str((self.root / 'history.json').resolve()), handoff['assignment']['inputs'])
        exchange.start_request(state, 'QA-IMPACT-1', 'owner', handoff['delivery_id'], None)
        exchange.submit_result(state, 'QA-IMPACT-1')
        exchange.review_result(state, 'QA-IMPACT-1', 'Synthetic sources checked', True, None)
        sources = exchange.check_sources(exchange.read_state(state), ['QA-IMPACT-1'], scenario_ids=['TC-EXPORT'], consumer_agent='qa-feature-tester')
        self.assertTrue(sources)
        with self.assertRaisesRegex(ValueError, 'сценариев'):
            exchange.check_sources(exchange.read_state(state), ['QA-IMPACT-1'], scenario_ids=['TC-PAY'], consumer_agent='qa-feature-tester')
        # Author omissions cannot hide a primary source from later fingerprint checks.
        (self.root / 'history.json').write_text('{}')
        with self.assertRaises(ValueError):
            exchange.check_sources(exchange.read_state(state), ['QA-IMPACT-1'], scenario_ids=['TC-EXPORT'])

    def test_managed_profile_contains_impact_protocol_and_role_risk(self):
        profile = context_builder.profile(dict(agent='qa-impact-analyst'), {})
        self.assertIn('docs/IMPACT_ANALYSIS.md', profile['documents'])
        text = context_builder.contract((ROOT / 'docs/CONTRACT.md').read_text(), 'qa-impact-analyst')
        self.assertIn('input_sha256', text)
        self.assertIn('impact_ready', text)
        reviewer = context_builder.profile(dict(agent='qa-result-reviewer', review_target=dict(scope='impact_selection')), {})
        self.assertIn('docs/IMPACT_ANALYSIS.md', reviewer['documents'])


class GitCaptureTests(unittest.TestCase):
    def test_full_release_diff_includes_rename_both_paths_and_later_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args): return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout.decode().strip()
            git('init'); git('config', 'user.name', 'Fixture'); git('config', 'user.email', 'fixture@example.invalid')
            (root / 'before.txt').write_text('stable content\n' * 10)
            git('add', '.'); git('commit', '-m', 'base'); base = git('rev-parse', 'HEAD')
            git('mv', 'before.txt', 'after.txt'); git('commit', '-m', 'rename')
            (root / 'new file.txt').write_text('new\n')
            git('add', '.'); git('commit', '-m', 'addition')
            result = capture.capture(root, base, 'HEAD')
            self.assertEqual(base, result['base_revision'])
            self.assertEqual(git('rev-parse', 'HEAD'), result['target_revision'])
            self.assertTrue(any(c['paths'] == ['before.txt', 'after.txt'] for c in result['changes']))
            self.assertTrue(any(c['paths'] == ['new file.txt'] for c in result['changes']))
            self.assertTrue(result['diff'])
            self.assertEqual('', git('status', '--porcelain'))
            self.assertEqual([], capture.capture(root, 'HEAD', 'HEAD')['changes'])
            with self.assertRaises(subprocess.CalledProcessError): capture.capture(root, '--invalid', 'HEAD')


if __name__ == '__main__':
    unittest.main()
