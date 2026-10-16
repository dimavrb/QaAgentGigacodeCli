"""Local contract/process checks; no live model evaluation."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from run_agent_benchmark import terminal_response, NEEDED_FLAGS
from check_benchmark_review import load_suite, validate_review, SUITE
from benchmark_fixtures import create_reasoning_cli, run_fixture_benchmark as run_benchmark


class BenchmarkExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gigacode = self.root / 'cli'
        create_reasoning_cli(self.gigacode)

    def test_executes_every_case_and_repeat_without_self_grading(self):
        output = self.root / 'run'
        result = run_benchmark(output, cli=str(self.gigacode), repeats=2, model='fixture-model')
        self.assertEqual('captured', result['status'])
        self.assertEqual(10, len(result['trials']))
        suite, fingerprint = load_suite(SUITE)
        _, saved_fingerprint = load_suite(output / 'suite.json')
        self.assertEqual(fingerprint, saved_fingerprint)
        for repeat in (1, 2):
            directory = output / f'trial-{repeat}'
            review = json.loads((directory / 'review.json').read_text())
            self.assertEqual([], validate_review(review, suite, fingerprint, directory))
            self.assertTrue(all(c['status'] == 'not_checked' for row in review['cases'] for c in row['checks']))
            self.assertTrue(all(row['input_tokens'] == 12 for row in review['cases']))
            check = subprocess.run([sys.executable, str(ROOT / 'scripts/check_benchmark_review.py'),
                                    '--suite', str(output / 'suite.json'), '--input', str(directory / 'review.json')], capture_output=True, text=True)
            self.assertEqual(1, check.returncode)
        records = [json.loads(line) for line in (output / 'trace.jsonl').read_text().splitlines()]
        self.assertEqual(10, sum(row['kind'] == 'runner_finished' for row in records))
        self.assertEqual(1, len({row['trace_id'] for row in records}))

    def test_missing_cli_is_recorded_without_fake_outputs(self):
        result = run_benchmark(self.root / 'run', cli=str(self.root / 'missing'))
        self.assertEqual('unavailable', result['status'])
        self.assertEqual([], result['trials'])
        self.assertFalse(list((self.root / 'run').glob('trial-*')))

    def test_custom_quality_suite_isolated_prompts_and_off_on_snapshots(self):
        suite_path=ROOT/'examples/benchmark/quality-suite.json'
        for enabled in (False,True):
            output=self.root/('on' if enabled else 'off')
            result=run_benchmark(output,cli=str(self.gigacode),suite_path=suite_path,check_quality=enabled)
            self.assertEqual('captured',result['status'])
            self.assertEqual(9,len(result['trials']))
            suite,fingerprint=load_suite(output/'suite.json')
            self.assertEqual(36,sum(len(c['checks']) for c in suite['cases']))
            review=json.loads((output/'trial-1/review.json').read_text())
            self.assertEqual([],validate_review(review,suite,fingerprint,output/'trial-1'))
            self.assertTrue(all(c['status']=='not_checked' for row in review['cases'] for c in row['checks']))
            snapshots=json.loads((output/'trial-1/observability/instructions.json').read_text())
            self.assertEqual(enabled,'.gigacode/skills/qa-check-quality/SKILL.md' in snapshots)
            for case in suite['cases']:
                prompt=(output/'trial-1'/case['case_id']/'prompt.md').read_text()
                self.assertNotIn('expectation',prompt)
                for check in case['checks']:self.assertNotIn(check['expectation'],prompt)

    def test_unsupported_cli_does_not_fall_back_to_less_restricted_mode(self):
        self.gigacode.write_text('#!/usr/bin/env python3\nprint("old-cli")\n')
        result = run_benchmark(self.root / 'run', cli=str(self.gigacode))
        self.assertEqual('unavailable', result['status'])
        self.assertIn('--bare', result['error'])

    def test_failed_case_is_preserved_and_other_cases_continue(self):
        self.gigacode.write_text(self.gigacode.read_text().replace("print(json.dumps([{'type': 'result'", "sys.exit(9)\n    print(json.dumps([{'type': 'result'"))
        result = run_benchmark(self.root / 'run', cli=str(self.gigacode))
        self.assertEqual('failed', result['status'])
        self.assertEqual(5, len(result['trials']))
        self.assertTrue(all(row['exit_code'] == 9 for row in result['trials']))

    def test_timeout_captures_failure_and_runs_remaining_cases(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'SLEEP': '.1'}):
            result = run_benchmark(self.root / 'run', cli=str(self.gigacode), max_wall_time='0.01s')
        self.assertEqual('failed', result['status'])
        self.assertEqual(5, len(result['trials']))
        self.assertTrue(all(row['status'] == 'timed_out' for row in result['trials']))

    def test_output_directory_never_overwrites_old_baseline(self):
        output = self.root / 'run'
        output.mkdir()
        (output / 'kept').write_text('Не менять')
        with self.assertRaises(FileExistsError):
            run_benchmark(output, cli=str(self.gigacode))
        self.assertEqual('Не менять', (output / 'kept').read_text())

    def test_error_empty_output_and_tool_use_are_not_successful_capture(self):
        for value in ({'is_error': True, 'response': 'error'}, {'response': ''},
                      {'response': 'text', 'stats': {'tools': {'totalCalls': 1}}}, []):
            with self.assertRaises(ValueError):
                terminal_response(json.dumps(value))

    def test_unknown_usage_is_not_estimated_from_text_length(self):
        self.gigacode.write_text(self.gigacode.read_text().replace("'usage': {'input_tokens': 12, 'output_tokens': 7}", "'stats': {}"))
        result = run_benchmark(self.root / 'run', cli=str(self.gigacode), self_review=False)
        self.assertEqual('captured', result['status'])
        review = json.loads((self.root / 'run/trial-1/review.json').read_text())
        self.assertTrue(all(row['input_tokens'] is None for row in review['cases']))
