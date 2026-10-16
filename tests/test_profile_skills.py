"""Synthetic runner/grades test measurement integrity, never model quality."""
import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from compare_profile_skills import compare
from profile_skills import PROFILE_SKILLS
from run_agent_benchmark import NEEDED_FLAGS, instructions, run_benchmark, sha
from benchmark_grading import make_pack, accept_review


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


class ProfileSkillMeasurementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory()
        cls.seed = Path(cls.shared.name)
        cls.cli = cls.seed / 'fixture-cli'
        cls.cli.write_text('''#!/usr/bin/env python3
import json, sys
if '--help' in sys.argv: print(%r)
elif '--version' in sys.argv: print('fixture-version')
else: print(json.dumps({'response': 'Synthetic captured output, not a model answer.',
                       'usage': {'input_tokens': 11, 'output_tokens': 5}}))
''' % ' '.join(NEEDED_FLAGS))
        cls.cli.chmod(0o755)
        suite = json.loads((ROOT / 'examples/benchmark/profile-suite.json').read_text())
        suite['cases'] = [c for c in suite['cases'] if c['case_id'] in ('data-composition', 'stub-matcher', 'empty')]
        (cls.seed / 'prompts').mkdir()
        for case in suite['cases']:
            shutil.copy(ROOT / 'examples/benchmark' / case['prompt_file'], cls.seed / case['prompt_file'])
        save(cls.seed / 'suite.json', suite)
        for arm, enabled in (('baseline', False), ('candidate', True)):
            result = run_benchmark(cls.seed / arm, cli=str(cls.cli), suite_path=cls.seed / 'suite.json',
                                   repeats=2, profile_skills=enabled, measurement_kind='fixture', model='fixture-model')
            assert result['status'] == 'captured'
            for trial in (1, 2):
                path = cls.seed / arm / f'trial-{trial}/review.json'
                review = json.loads(path.read_text())
                review['reviewer'] = 'synthetic-test-grader'
                for row in review['cases']:
                    for idx, check in enumerate(row['checks']):
                        check['status'] = 'fail' if arm == 'baseline' and idx == 0 and row['case_id'] != 'empty' else 'pass'
                        check['evidence'] = 'Synthetic captured output, not a model answer.'
                save(path, review)
            cls.bind_reviews(cls.seed, arm)

    @staticmethod
    def bind_reviews(root, arm):
        directory = root / arm
        pack = root / (arm + '-grading-' + str(len(list(root.glob(arm + '-grading-*')))))
        make_pack(directory, pack)
        for trial in range(1, json.loads((directory / 'run.json').read_text())['repeats'] + 1):
            path = directory / f'trial-{trial}/review.json'
            grades = json.loads(path.read_text())
            template = json.loads((pack / f'trial-{trial}/review-template.json').read_text())
            template.update(reviewer=grades['reviewer'], grading_manifest_sha256=sha((pack / 'manifest.json').read_bytes()))
            for row in template['cases']:
                row['checks'] = next(c['checks'] for c in grades['cases'] if c['case_id'] == row['case_id'])
            review_path = pack / f'trial-{trial}/review-fixture.json'
            save(review_path, template)
            (directory / f'trial-{trial}/review-acceptance.json').unlink(missing_ok=True)
            accept_review(directory, pack, review_path, 'fixture-evidence-session')

    @classmethod
    def tearDownClass(cls):
        cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for arm in ('baseline', 'candidate'):
            shutil.copytree(self.seed / arm, self.root / arm)

    def report(self):
        return compare(self.root / 'baseline', self.root / 'candidate')

    def edit(self, relative, change):
        path = self.root / relative
        value = json.loads(path.read_text())
        change(value)
        save(path, value)

    def grade(self, arm, cid, check_id, status, trial=1):
        def change(review):
            row = next(r for r in review['cases'] if r['case_id'] == cid)
            next(c for c in row['checks'] if c['check_id'] == check_id)['status'] = status
        self.edit(f'{arm}/trial-{trial}/review.json', change)
        self.bind_reviews(self.root, arm)

    def test_fixture_gains_never_promote(self):
        report = self.report()
        self.assertEqual('fixture_only', report['status'])
        for role in PROFILE_SKILLS:
            result = report['roles'][role]
            self.assertEqual('fixture_only', result['decision'])
            self.assertEqual(2, result['weak_points'][0]['failed_trials'])
            self.assertEqual(2, len(result['improvements']))
        self.assertEqual('holdout', report['roles']['qa-autotest-reviewer']['decision'])

    def test_candidate_routed_only_to_its_role_without_rubric(self):
        for role, skill in PROFILE_SKILLS.items():
            system, snapshots = instructions('custom', True, role=role, profile_skills=True)
            self.assertIn(skill, snapshots)
            self.assertEqual(1, len(set(PROFILE_SKILLS.values()) & set(snapshots)))
            self.assertNotIn('expectation', system)
            _, off = instructions('custom', True, role=role)
            self.assertFalse(set(PROFILE_SKILLS.values()) & set(off))
        _, other = instructions('empty', True, role='qa-autotest-reviewer', profile_skills=True)
        self.assertFalse(set(PROFILE_SKILLS.values()) & set(other))

    def test_unchecked_keeps_all_criteria_and_pending(self):
        self.grade('candidate', 'data-composition', 'distinguish', 'not_checked')
        result = self.report()
        self.assertEqual('pending_measurement', result['status'])
        self.assertEqual('pending_measurement', result['roles']['qa-test-data-builder']['decision'])
        self.assertEqual(8, sum(result['roles']['qa-test-data-builder']['counts']['candidate'].values()))

    def test_profile_regression_requires_revision(self):
        self.grade('candidate', 'data-composition', 'oracle', 'fail')
        result = self.report()['roles']['qa-test-data-builder']
        self.assertEqual('revise_candidate', result['decision'])
        self.assertEqual(1, len(result['regressions']))
        self.assertEqual('revise_candidate', self.report()['roles']['qa-stub-builder']['decision'])

    def test_holdout_regression_blocks_both_candidates(self):
        self.grade('candidate', 'empty', 'honesty', 'fail')
        result = self.report()
        self.assertTrue(result['holdout_regression'])
        self.assertTrue(all(result['roles'][r]['decision'] == 'revise_candidate' for r in PROFILE_SKILLS))

    def test_cost_only_paired_wholly_passed_cases(self):
        report = self.report()
        self.assertEqual(0, report['roles']['qa-test-data-builder']['paired_cost']['paired_passed_cases'])
        cost = report['roles']['qa-autotest-reviewer']['paired_cost']
        self.assertEqual(2, cost['paired_passed_cases'])
        self.assertEqual(11, cost['input_tokens']['candidate_mean'])

    def test_unknown_usage_remains_unknown(self):
        for arm in ('baseline', 'candidate'):
            for trial in (1, 2):
                directory = self.root / arm / f'trial-{trial}/empty'
                raw = json.loads((directory / 'raw.json').read_text())
                raw.pop('usage')
                save(directory / 'raw.json', raw)
                self.edit(f'{arm}/trial-{trial}/empty/execution.json', lambda v: v.update(raw_sha256=sha((directory / 'raw.json').read_bytes())))
                execution = json.loads((directory / 'execution.json').read_text())
                def manifest(v):
                    idx = next(i for i,r in enumerate(v['trials']) if r['trial'] == trial and r['case_id'] == 'empty')
                    v['trials'][idx] = execution
                self.edit(f'{arm}/run.json', manifest)
                def review(v):
                    row = next(r for r in v['cases'] if r['case_id'] == 'empty')
                    row.update(input_tokens=None, output_tokens=None)
                self.edit(f'{arm}/trial-{trial}/review.json', review)
            self.bind_reviews(self.root, arm)
        cost = self.report()['roles']['qa-autotest-reviewer']['paired_cost']
        self.assertIsNone(cost['input_tokens']['candidate_mean'])
        self.assertEqual(0, cost['input_tokens']['known_pairs'])

    def test_changed_output_rejected(self):
        (self.root / 'candidate/trial-1/empty/response.md').write_text('Changed')
        with self.assertRaises(ValueError):
            self.report()

    def test_rehashed_different_output_still_rejected(self):
        path = self.root / 'candidate/trial-1/empty/response.md'
        path.write_text('Changed')
        def change(v):
            next(r for r in v['cases'] if r['case_id']=='empty')['output_sha256'] = sha(path.read_bytes())
        self.edit('candidate/trial-1/review.json', change)
        with self.assertRaises(ValueError):
            self.report()

    def test_different_model_or_provider_configuration_rejected(self):
        for field in ('model', 'configuration'):
            with self.subTest(field=field):
                old = json.loads((self.root / 'candidate/trial-1/review.json').read_text())
                self.edit('candidate/trial-1/review.json', lambda v: v.update({field: 'different'}))
                with self.assertRaises(ValueError):
                    self.report()
                save(self.root / 'candidate/trial-1/review.json', old)

    def test_different_cli_or_other_flags_rejected(self):
        for field in ('cli_version', 'cli_executable_sha256', 'self_review', 'check_quality', 'max_wall_time'):
            with self.subTest(field=field):
                old = json.loads((self.root / 'candidate/run.json').read_text())
                self.edit('candidate/run.json', lambda v: v.update({field:'different'}))
                with self.assertRaises(ValueError):
                    self.report()
                save(self.root / 'candidate/run.json', old)

    def test_missing_repeat_and_extra_repeat_rejected(self):
        shutil.rmtree(self.root / 'candidate/trial-2')
        with self.assertRaisesRegex(ValueError, 'Trial directories'):
            self.report()
        shutil.copytree(self.seed / 'candidate/trial-2', self.root / 'candidate/trial-2')
        (self.root / 'candidate/trial-3').mkdir()
        with self.assertRaisesRegex(ValueError, 'Trial directories'):
            self.report()

    def test_changed_common_instruction_rejected_even_with_consistent_hashes(self):
        directory = self.root / 'candidate/trial-1/empty'
        snapshots = json.loads((directory / 'instructions.json').read_text())
        snapshots['docs/SELF_REVIEW.md'] += '\nChanged common guidance'
        save(directory / 'instructions.json', snapshots)
        original = (directory / 'system.md').read_text()
        marker = '\n\nУсловия этого учебного reasoning benchmark'
        override = marker + original.split(marker, 1)[1]
        system = '\n\n'.join(f'Источник инструкций: {name}\n{text}'
                               for name, text in snapshots.items()) + override
        (directory / 'system.md').write_text(system)
        self.edit('candidate/trial-1/empty/execution.json', lambda v: v.update(instructions_sha256=sha(system.encode())))
        execution = json.loads((directory / 'execution.json').read_text())
        def manifest(v):
            idx = next(i for i,r in enumerate(v['trials']) if r['trial']==1 and r['case_id']=='empty')
            v['trials'][idx] = execution
        self.edit('candidate/run.json', manifest)
        # Rebind only this synthetic capture through a fresh grading copy, so the
        # failure must concern A/B comparability rather than a stale receipt/hash.
        self.bind_reviews(self.root, 'candidate')
        with self.assertRaisesRegex(ValueError, 'only the routed candidate skill'):
            self.report()

    def test_invalid_grades_evidence_or_usage_rejected(self):
        for field, value in [('output_tokens', 999), ('seconds', -1)]:
            old = json.loads((self.root / 'candidate/trial-1/review.json').read_text())
            def edit(v):
                next(r for r in v['cases'] if r['case_id']=='empty')[field] = value
            self.edit('candidate/trial-1/review.json', edit)
            with self.assertRaises(ValueError):
                self.report()
            save(self.root / 'candidate/trial-1/review.json', old)
        self.edit('candidate/trial-1/review.json', lambda v: v['cases'][0]['checks'][0].update(evidence=''))
        with self.assertRaisesRegex(ValueError, 'evidence'):
            self.report()

    def test_unavailable_preflight_does_not_invent_measurement(self):
        for arm, enabled in [('unavailable-off', False), ('unavailable-on', True)]:
            run_benchmark(self.root / arm, cli=str(self.root / 'missing'), repeats=2,
                          profile_skills=enabled, suite_path=self.seed / 'suite.json')
        report = compare(self.root / 'unavailable-off', self.root / 'unavailable-on')
        self.assertEqual('no_measurement', report['status'])
        self.assertEqual({}, report['roles'])

    def test_live_complete_gain_only_suggests_independent_promotion_review(self):
        # Only a schema branch test: relabelled fixtures are not saved as live evidence.
        with patch('benchmark_grading.validate_grading_execution'):
            for arm in ('baseline', 'candidate'):
                self.edit(f'{arm}/run.json', lambda v: v.update(measurement_kind='live'))
                self.bind_reviews(self.root, arm)
            report = self.report()
        self.assertEqual('pending_dispersion', report['status'])
        self.assertTrue(all(report['roles'][r]['decision']=='pending_dispersion' for r in PROFILE_SKILLS))

    def test_different_measurement_kind_is_not_comparable(self):
        self.edit('candidate/run.json', lambda v: v.update(measurement_kind='live'))
        with self.assertRaises(ValueError):
            self.report()

    def test_one_repeat_cannot_support_promotion(self):
        for arm in ('baseline', 'candidate'):
            self.edit(f'{arm}/run.json', lambda v: v.update(repeats=1, trials=[r for r in v['trials'] if r['trial']==1]))
            shutil.rmtree(self.root / arm / 'trial-2')
            self.bind_reviews(self.root, arm)
        result = self.report()
        self.assertTrue(all(result['roles'][r]['decision']=='pending_measurement' for r in PROFILE_SKILLS))


if __name__ == '__main__':
    unittest.main()
