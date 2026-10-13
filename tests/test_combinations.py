"""Check actual interaction coverage, constraints, seeds and stale model handling."""
import copy
import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from generate_combinations import check, fingerprint, generate


class CombinationTests(unittest.TestCase):
    def setUp(self):
        self.model = dict(schema_version=1, factors={'a': ['0', '1'], 'b': ['0', '1'], 'c': ['0', '1']}, strength=2)

    def test_generated_rows_cover_every_pair_without_full_enumeration(self):
        report = generate(self.model)
        self.assertEqual([], check(self.model, report))
        self.assertLess(len(report['rows']), 8)
        for names in itertools.combinations(('a', 'b', 'c'), 2):
            observed = {tuple(row['values'][name] for name in names) for row in report['rows']}
            self.assertEqual(set(itertools.product(('0', '1'), repeat=2)), observed)
        self.assertEqual(report, generate(copy.deepcopy(self.model)))

    def test_pairwise_does_not_satisfy_requested_three_way_coverage(self):
        rows = [dict(combination_id=f'R-{i}', values=dict(zip(('a', 'b', 'c'), values)), scenario_ids=[])
                for i, values in enumerate(('000', '011', '101', '110'))]
        report = dict(schema_version=1, model_sha256=fingerprint(self.model), rows=rows)
        self.assertEqual([], check(self.model, report))
        self.model['interaction_groups'] = [['a', 'b', 'c']]
        report['model_sha256'] = fingerprint(self.model)
        self.assertTrue(any('missing 4 required interactions' in e for e in check(self.model, report)))
        self.assertEqual([], check(self.model, generate(self.model)))

    def test_constraints_remove_only_infeasible_interactions(self):
        self.model['exclude'] = [{'a': '1', 'b': '1'}]
        report = generate(self.model)
        self.assertEqual([], check(self.model, report))
        self.assertTrue(all(not (r['values']['a'] == r['values']['b'] == '1') for r in report['rows']))
        report['rows'].append(dict(combination_id='bad', values={'a': '1', 'b': '1', 'c': '0'}, scenario_ids=[]))
        self.assertTrue(any('infeasible' in e for e in check(self.model, report)))

    def test_mandatory_seed_is_preserved_even_if_interactions_are_covered_elsewhere(self):
        self.model['seeds'] = [dict(combination_id='critical', values={'a': '0', 'b': '0', 'c': '0'}, scenario_ids=['TC-001'])]
        report = generate(self.model)
        self.assertEqual([], check(self.model, report))
        report['rows'][0]['combination_id'] = 'renamed'
        self.assertTrue(any('mandatory seed missing' in e for e in check(self.model, report)))
        report = generate(self.model)
        report['rows'][0]['scenario_ids'] = []
        self.assertTrue(any('scenario links changed' in e for e in check(self.model, report)))

    def test_conflicting_seed_and_empty_feasible_space_are_not_silently_dropped(self):
        self.model['exclude'] = [{'a': '1'}]
        self.model['seeds'] = [dict(combination_id='must', values={'a': '1', 'b': '0', 'c': '0'})]
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            generate(self.model)
        self.model.pop('seeds')
        self.model['exclude'].append({'a': '0'})
        with self.assertRaisesRegex(ValueError, 'no feasible'):
            generate(self.model)

    def test_stale_report_and_false_counters_do_not_hide_missing_coverage(self):
        report = generate(self.model)
        report['model_sha256'] = 'old'
        self.assertTrue(any('model_sha256' in e for e in check(self.model, report)))
        report['model_sha256'] = fingerprint(self.model)
        report['rows'] = report['rows'][:1]
        report['required_interactions'] = 0
        self.assertTrue(any('required interactions' in e for e in check(self.model, report)))

    def test_ready_rows_need_real_catalog_links(self):
        report = generate(self.model)
        catalog = json.loads((ROOT / 'examples/test-cases.json').read_text())
        self.assertTrue(any('scenario_ids' in e for e in check(self.model, report, catalog=catalog)))
        identifier = catalog['test_cases'][0]['scenario_id']
        for row in report['rows']:
            row['scenario_ids'] = [identifier]
        self.assertTrue(any('behavior_rules' in e for e in check(self.model, report, catalog=catalog)))
        report['rows'][0]['scenario_ids'] = ['unknown']
        self.assertTrue(any('existing catalog' in e for e in check(self.model, report, catalog=catalog)))

    def test_limits_and_malformed_inputs_fail_before_enumeration(self):
        with self.assertRaisesRegex(ValueError, 'exceeds limit'):
            generate(self.model, limit=7)
        wide = dict(schema_version=1, strength=8,
                    factors={f'f{i}': ['0', '1'] if i < 13 else ['0'] for i in range(16)})
        with self.assertRaisesRegex(ValueError, 'interaction enumeration'):
            generate(wide)
        for mutation in (
            lambda m: m.update(strength=True),
            lambda m: m['factors'].update(a=['0', '0']),
            lambda m: m.update(exclude=[{'missing': '0'}]),
            lambda m: m.update(interaction_groups=[['a', {}]]),
        ):
            with self.subTest(mutation=mutation):
                model = copy.deepcopy(self.model)
                mutation(model)
                with self.assertRaises(ValueError):
                    generate(model)
        report = generate(self.model)
        report['rows'][0]['scenario_ids'] = [{}]
        self.assertTrue(check(self.model, report))

    def test_cli_example_and_non_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'rows.json'
            command = [sys.executable, str(ROOT / 'scripts/generate_combinations.py'),
                       '--model', str(ROOT / 'examples/test-design/combinations.json')]
            first = subprocess.run(command + ['--output', str(output)], capture_output=True, text=True)
            self.assertEqual(0, first.returncode, first.stdout + first.stderr)
            contents = output.read_bytes()
            self.assertEqual(2, subprocess.run(command + ['--output', str(output)], capture_output=True).returncode)
            self.assertEqual(contents, output.read_bytes())
            self.assertEqual(0, subprocess.run(command + ['--check', str(output)], capture_output=True).returncode)


if __name__ == '__main__':
    unittest.main()
