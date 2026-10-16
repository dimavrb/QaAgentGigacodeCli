"""Suite integrity, isolated context delivery and ungraded capture boundaries."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from benchmark_fixtures import create_reasoning_cli, run_fixture_benchmark
from benchmark_grading import make_pack, verify_pack
from check_benchmark_review import load_suite, make_template, validate_review
import run_agent_benchmark as benchmark
from run_agent_benchmark import ROLES, instructions, run_benchmark
from qa_contract import ROLE_EVENTS


class SuiteIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.prompt = self.root / 'task.md'
        self.prompt.write_text('Учебная задача с первичным требованием R-1.', encoding='utf-8')
        self.suite_path = self.root / 'suite.json'
        self.suite = dict(schema_version=1, cases=[dict(case_id='sample', prompt_file='task.md',
                          checks=[dict(check_id='coverage', expectation='Независимый критерий R-1')])])

    def save(self, suite=None):
        self.suite_path.write_text(json.dumps(self.suite if suite is None else suite), encoding='utf-8')
        return self.suite_path

    def test_all_shipped_suites_have_unique_tasks_and_complete_unreviewed_templates(self):
        for path in sorted((ROOT / 'examples/benchmark').glob('*suite.json')):
            with self.subTest(suite=path.name):
                suite, fingerprint = load_suite(path)
                template = make_template(suite, fingerprint, 'fixture-version')
                self.assertEqual([], validate_review(template, suite, fingerprint, self.root))
                self.assertTrue(all(check['status'] == 'not_checked'
                                    for case in template['cases'] for check in case['checks']))

    def test_prompt_change_invalidates_saved_review_even_when_suite_json_is_unchanged(self):
        self.save()
        suite, before = load_suite(self.suite_path)
        review = make_template(suite, before, 'fixture-version')
        suite_bytes = self.suite_path.read_bytes()
        self.prompt.write_text('R-1: изменённое обязательное ожидание.', encoding='utf-8')
        suite, after = load_suite(self.suite_path)
        self.assertEqual(suite_bytes, self.suite_path.read_bytes())
        self.assertNotEqual(before, after)
        self.assertIn('suite_sha256 differs from the suite and prompts',
                      validate_review(review, suite, after, self.root))

    def test_malformed_schemas_ids_and_rubrics_are_rejected(self):
        variants = [[], {}, dict(self.suite, schema_version=True), dict(self.suite, cases=[])]
        for change in ({'case_id': '../escape'}, {'case_id': ''}, {'checks': []},
                       {'checks': [dict(check_id='one', expectation='')]}, {'prompt_file': ''}):
            item = copy.deepcopy(self.suite)
            item['cases'][0].update(change)
            variants.append(item)
        duplicate = copy.deepcopy(self.suite)
        duplicate['cases'].append(copy.deepcopy(duplicate['cases'][0]))
        variants.append(duplicate)
        duplicate = copy.deepcopy(self.suite)
        duplicate['cases'][0]['checks'] *= 2
        variants.append(duplicate)
        for item in variants:
            with self.subTest(suite=item), self.assertRaises(ValueError):
                load_suite(self.save(item))

    def test_unsafe_and_symlinked_external_prompts_are_rejected(self):
        outside = self.root.parent / (self.root.name + '-external.md')
        outside.write_text('Do not read external sources')
        self.addCleanup(outside.unlink)
        (self.root / 'link.md').symlink_to(outside)
        for path in ('../' + outside.name, str(outside), 'link.md'):
            self.suite['cases'][0]['prompt_file'] = path
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'inside suite'):
                load_suite(self.save())

    def test_empty_prompt_is_not_a_valid_task(self):
        self.prompt.write_text(' \n\t')
        with self.assertRaisesRegex(ValueError, 'prompt.*empty'):
            load_suite(self.save())

    def test_instruction_symlink_cannot_admit_external_rubric(self):
        docs = self.root / 'docs'
        docs.mkdir()
        (docs / 'rubric.md').symlink_to(self.prompt)
        self.suite['cases'][0]['instruction_paths'] = ['docs/rubric.md']
        with patch('check_benchmark_review.ROOT', self.root), self.assertRaisesRegex(
                ValueError, 'inside package docs'):
            load_suite(self.save())

    def test_invalid_role_channel_and_instruction_paths_never_reach_cli(self):
        for index, change in enumerate(({'agent': 'qa-imaginary'}, {'agent': []}, {'test_type': 'smtp'},
                       {'test_type': True}, {'test_type': 'api'},
                       {'instruction_paths': 'docs/CONTRACT.md'},
                       {'instruction_paths': ['../README.md']},
                       {'instruction_paths': ['examples/benchmark/suite.json']},
                       {'instruction_paths': ['/tmp/secret.md']})):
            suite = copy.deepcopy(self.suite)
            suite['cases'][0].update(change)
            output = self.root / f'run-{index}'
            with self.subTest(change=change), patch('run_agent_benchmark.agent_runtime.preflight') as preflight:
                with self.assertRaises(ValueError):
                    run_benchmark(output, suite_path=self.save(suite), cli='unavailable-fixture')
                preflight.assert_not_called()
                self.assertFalse(output.exists())


class RegressionEvalTests(unittest.TestCase):
    def test_reasoning_systems_fit_linux_single_argument_limit_with_4k_pages(self):
        # Linux MAX_ARG_STRLEN = PAGE_SIZE * 32, including the terminating NUL:
        # https://github.com/torvalds/linux/blob/master/include/uapi/linux/binfmts.h
        for path in (ROOT / 'examples/benchmark').glob('*suite.json'):
            suite, _ = load_suite(path)
            for case in suite['cases']:
                with self.subTest(suite=path.name, case=case['case_id']):
                    system, _ = instructions(case['case_id'], True, role=case.get('agent'),
                        profile_skills=True, test_type=case.get('test_type'),
                        instruction_paths=case.get('instruction_paths', []))
                    self.assertLessEqual(len(system.encode('utf-8')) + 1, 32 * 4096)

    def test_programmatic_cli_cannot_be_labelled_as_live_measurement(self):
        with patch('benchmark_fixtures._run_benchmark') as runner:
            with self.assertRaisesRegex(ValueError, 'cannot produce a live measurement'):
                run_fixture_benchmark(Path('unused'), measurement_kind='live')
            runner.assert_not_called()

    def test_every_registered_role_has_a_reasoning_task_in_the_shipped_suites(self):
        represented = set()
        for path in (ROOT / 'examples/benchmark').glob('*suite.json'):
            suite, _ = load_suite(path)
            represented.update(case.get('agent') or ROLES.get(case['case_id']) for case in suite['cases'])
        self.assertEqual(set(ROLE_EVENTS), represented - {None})

    def test_manual_case_context_contains_selected_example_for_author_and_reviewer(self):
        for role in ('qa-test-plan-writer', 'qa-result-reviewer'):
            for channel in ('api', 'ui', 'kafka'):
                with self.subTest(role=role, channel=channel):
                    system, snapshots = instructions('sample', True, role=role, test_type=channel)
                    self.assertIn('docs/TEST_CASE_REVIEW.md', snapshots)
                    self.assertIn('docs/TEST_CASE_SELECTION.md', snapshots)
                    self.assertIn('.gigacode/skills/qa-test-cases/SKILL.md', snapshots)
                    examples = [name for name in snapshots if name.startswith('examples/test-cases/')]
                    self.assertEqual(2, len(examples))
                    self.assertTrue(all(name.startswith(f'examples/test-cases/{channel}/') for name in examples))
                    self.assertTrue(any(name.endswith('.json') for name in examples))
                    for name in examples:
                        self.assertIn(snapshots[name], system)

    def test_explicit_subject_docs_are_delivered_without_loading_other_subjects(self):
        _, snapshots = instructions('sample', True,
                                    instruction_paths=['docs/COORDINATION_SAFETY.md'])
        self.assertEqual((ROOT / 'docs/COORDINATION_SAFETY.md').read_text(),
                         snapshots['docs/COORDINATION_SAFETY.md'])
        self.assertNotIn('docs/QUESTIONS_UI.md', snapshots)
        self.assertFalse(any(name.startswith('examples/benchmark/') for name in snapshots))

    def test_invalid_selected_example_is_rejected_before_any_model_call(self):
        _, snapshots = instructions('sample', True, role='qa-test-plan-writer', test_type='api')
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            for name, text in snapshots.items():
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf-8')
            example = next(name for name in snapshots if name.endswith('.json'))
            (package / example).write_text('{"schema_version":1,"test_cases":[{}]}')
            with patch.object(benchmark, 'ROOT', package), self.assertRaisesRegex(
                    ValueError, 'Invalid test-case example'):
                instructions('sample', True, role='qa-test-plan-writer', test_type='api')

    def test_new_suite_runs_each_case_preserves_hashes_and_never_grades_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = create_reasoning_cli(root / 'fixture-cli')
            suite_path = ROOT / 'examples/benchmark/regression-suite.json'
            suite, fingerprint = load_suite(suite_path)
            output = root / 'run'
            result = run_benchmark(output, cli=str(cli), suite_path=suite_path,
                                   measurement_kind='fixture', model='fixture-model')
            self.assertEqual('captured', result['status'])
            self.assertEqual('fixture', result['measurement_kind'])
            self.assertEqual('not_reviewed', result['semantic_quality'])
            self.assertEqual(len(suite['cases']), len(result['trials']))
            saved, saved_fingerprint = load_suite(output / 'suite.json')
            self.assertEqual(fingerprint, saved_fingerprint)
            review = json.loads((output / 'trial-1/review.json').read_text())
            self.assertEqual([], validate_review(review, saved, fingerprint, output / 'trial-1'))
            for case in suite['cases']:
                folder = output / 'trial-1' / case['case_id']
                prompt = (folder / 'prompt.md').read_text()
                system = (folder / 'system.md').read_text()
                self.assertEqual((suite_path.parent / case['prompt_file']).read_text(), prompt)
                for check in case['checks']:
                    self.assertNotIn(check['expectation'], prompt + system)
                row = next(row for row in review['cases'] if row['case_id'] == case['case_id'])
                self.assertTrue(all(check['status'] == 'not_checked' for check in row['checks']))
                self.assertEqual(hashlib.sha256((folder / 'response.md').read_bytes()).hexdigest(),
                                 row['output_sha256'])
            pack = root / 'grading'
            make_pack(output, pack)
            manifest, saved = verify_pack(pack, output)
            self.assertEqual(sum(len(case['checks']) for case in suite['cases']),
                             manifest['expected_checks_per_trial'])
            self.assertEqual(suite, saved)


class BenchmarkCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.run = self.root / 'run'
        cli = create_reasoning_cli(self.root / 'fixture-cli')
        suite_path = ROOT / 'examples/benchmark/regression-suite.json'
        suite, _ = load_suite(suite_path)
        self.expected_checks = sum(len(case['checks']) for case in suite['cases'])
        run_benchmark(self.run, cli=str(cli), suite_path=suite_path, measurement_kind='fixture')

    def cli(self, script, *arguments):
        return subprocess.run([sys.executable, str(ROOT / 'scripts' / script), *map(str, arguments)],
                              cwd=self.root, capture_output=True, text=True, timeout=15)

    def test_grading_pack_cli_preserves_all_checks_and_rejects_changed_copy(self):
        pack = self.root / 'pack'
        made = self.cli('make_benchmark_grading_pack.py', '--run', self.run, '--output', pack)
        self.assertEqual(0, made.returncode, made.stdout + made.stderr)
        self.assertEqual(self.expected_checks, json.loads(made.stdout)['checks_per_trial'])
        checked = self.cli('check_benchmark_grading_pack.py', '--pack', pack, '--run', self.run)
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        copy_path = next(pack.glob('trial-1/*/response.md'))
        copy_path.write_text('Changed after capture')
        rejected = self.cli('check_benchmark_grading_pack.py', '--pack', pack, '--run', self.run)
        self.assertEqual(2, rejected.returncode)
        self.assertIn('sha256', rejected.stdout)

    def test_summary_cli_keeps_ungraded_stage_open_and_binds_session_and_numbers(self):
        path = self.root / 'summary.json'
        made = self.cli('build_benchmark_summary.py', '--run', self.run,
                        '--session-id', 'fixture-session', '--output', path)
        self.assertEqual(1, made.returncode, made.stdout + made.stderr)
        summary = json.loads(path.read_text())
        self.assertFalse(summary['baseline']['stage_closed'])
        self.assertFalse(summary['baseline']['quality_passed'])
        self.assertEqual(self.expected_checks, summary['baseline']['counts']['not_checked'])
        self.assertEqual(1, self.cli('check_benchmark_summary.py', '--input', path,
                                     '--session-id', 'fixture-session').returncode)
        self.assertEqual(2, self.cli('check_benchmark_summary.py', '--input', path,
                                     '--session-id', 'other-session').returncode)
        summary['baseline']['counts']['pass'] = self.expected_checks
        path.write_text(json.dumps(summary))
        rejected = self.cli('check_benchmark_summary.py', '--input', path,
                            '--session-id', 'fixture-session')
        self.assertEqual(2, rejected.returncode)
        self.assertIn('unsupported numbers', rejected.stdout)
