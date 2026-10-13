"""Guard against incomplete or stale manual evaluations being counted as success."""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from check_benchmark_review import SUITE, load_suite, make_template, validate_review


class BenchmarkReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.suite, self.fingerprint = load_suite(SUITE)
        self.review = make_template(self.suite, self.fingerprint, '1.3')
        self.review.update(model='test-model', configuration='test-config', reviewer='human-test')
        output = self.base / 'output.md'
        output.write_text('Saved test fixture, not a live model result.\n')
        for case in self.review['cases']:
            folder = self.base / case['case_id']; folder.mkdir()
            (folder / 'response.md').write_bytes(output.read_bytes())
            case['output_artifact'] = 'output.md'
            case['output_sha256'] = hashlib.sha256(output.read_bytes()).hexdigest()
            for check in case['checks']:
                check.update(status='pass', evidence='Saved test fixture, not a live model result.')

    def errors(self, review=None):
        return validate_review(self.review if review is None else review,
                               self.suite, self.fingerprint, self.base)

    def run_cli(self, review):
        path = self.base / 'review.json'
        path.write_text(json.dumps(review))
        return subprocess.run([sys.executable, str(ROOT / 'scripts/check_benchmark_review.py'),
                               '--input', str(path), '--verify-quotes', str(self.base)], capture_output=True, text=True)

    def test_complete_manual_review_and_unknown_usage_are_valid(self):
        self.assertEqual([], self.errors())
        result = self.run_cli(self.review)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn('pass=20, fail=0, not_checked=0', result.stdout)
        self.assertTrue(all(case['input_tokens'] is None for case in self.review['cases']))

    def test_unreviewed_and_failed_criteria_never_pass(self):
        template = make_template(self.suite, self.fingerprint, '1.3')
        self.assertEqual([], self.errors(template))
        self.assertEqual(1, self.run_cli(template).returncode)
        self.review['cases'][0]['checks'][0]['status'] = 'fail'
        self.assertEqual(1, self.run_cli(self.review).returncode)

    def test_omitted_and_duplicate_cases_or_checks_are_rejected(self):
        for mutate in (
            lambda r: r['cases'].pop(),
            lambda r: r['cases'].append(copy.deepcopy(r['cases'][0])),
            lambda r: r['cases'][0]['checks'].pop(),
            lambda r: r['cases'][0]['checks'].append(copy.deepcopy(r['cases'][0]['checks'][0])),
        ):
            with self.subTest(mutation=mutate):
                review = copy.deepcopy(self.review)
                mutate(review)
                self.assertTrue(self.errors(review))

    def test_stale_output_or_changed_suite_invalidates_review(self):
        (self.base / 'output.md').write_text('Changed after review')
        self.assertTrue(any('output_sha256' in error for error in self.errors()))
        self.review['suite_sha256'] = 'outdated'
        self.assertTrue(any('suite_sha256' in error for error in self.errors()))

    def test_review_requires_evidence_reviewer_and_accessible_output(self):
        self.review['cases'][0]['checks'][0]['evidence'] = ''
        self.review['reviewer'] = None
        self.review['cases'][0]['output_artifact'] = 'missing.md'
        errors = self.errors()
        self.assertTrue(any('needs evidence' in error for error in errors))
        self.assertTrue(any('need reviewer' in error for error in errors))
        self.assertTrue(any('cannot read output' in error for error in errors))

    def test_invalid_usage_values_cannot_be_used_for_savings(self):
        for metric, value in [('seconds', -1), ('seconds', float('nan')),
                              ('input_tokens', True), ('output_tokens', 1.5)]:
            with self.subTest(metric=metric, value=value):
                review = copy.deepcopy(self.review)
                review['cases'][0][metric] = value
                self.assertTrue(self.errors(review))

    def test_init_is_incomplete_and_never_overwrites_prior_review(self):
        path = self.base / 'new.json'
        command = [sys.executable, str(ROOT / 'scripts/check_benchmark_review.py'),
                   '--init', str(path)]
        self.assertEqual(0, subprocess.run(command, capture_output=True).returncode)
        contents = path.read_bytes()
        self.assertEqual(2, subprocess.run(command, capture_output=True).returncode)
        self.assertEqual(contents, path.read_bytes())
        self.assertEqual(1, self.run_cli(json.loads(contents)).returncode)


if __name__ == '__main__':
    unittest.main()
