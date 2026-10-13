"""Mechanical reproductions of A0 failures, without live model quality claims."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from benchmark_grading import make_pack, verify_pack, check_pack_review, accept_review, read, sha
from benchmark_summary import command_result, check_summary, calibration
from check_benchmark_review import verify_quote
from evidence_language import text_errors
from check_russian_artifact import check_text
from run_agent_benchmark import run_benchmark, NEEDED_FLAGS
from run_benchmark_grader import grading_prompt, run_grader
from compare_profile_skills import load_run
import task_exchange as exchange
import context_builder


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def grade(pack, trial, name, failures=()):
    result = read(pack / f'trial-{trial}/review-template.json')
    result.update(reviewer='synthetic-reviewer-' + name, grading_manifest_sha256=sha(pack / 'manifest.json'))
    index = (trial - 1) * 20
    for case in result['cases']:
        response = (pack / f'trial-{trial}' / case['case_id'] / 'response.md').read_text()
        for check in case['checks']:
            check.update(status='fail' if index in failures else 'pass', evidence=response,
                         note='Учебная оценка для проверки механики')
            if text_errors(response): check['literal_defect_quote'] = True
            index += 1
    path = pack / f'trial-{trial}/review-{name}.json'
    save(path, result)
    return path


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(); cls.seed = Path(cls.shared.name)
        cls.fixture_cli = cls.seed / 'fixture-cli'
        cls.fixture_cli.write_text('''#!/usr/bin/env python3
import hashlib, json, sys
if '--help' in sys.argv: print(%r)
elif '--version' in sys.argv: print('fixture-version')
else:
 prompt = sys.argv[sys.argv.index('-p')+1]
 model = sys.argv[sys.argv.index('--model')+1] if '--model' in sys.argv else 'a'
 response = 'Учебный ответ ' + hashlib.sha256(prompt.encode()).hexdigest() + ' без проверки живой модели'
 if model == 'b': response += ' смесFoo 語'
 print(json.dumps({'response': response, 'usage': {'input_tokens': 11, 'output_tokens': 5}}))
''' % ' '.join(NEEDED_FLAGS))
        cls.fixture_cli.chmod(0o755)
        for arm, model in [('a', 'a'), ('b', 'b')]:
            run_benchmark(cls.seed / arm, cli=str(cls.fixture_cli), repeats=3, model=model, measurement_kind='fixture')

    @classmethod
    def tearDownClass(cls): cls.shared.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_dir = self.root / 'run'
        shutil.copytree(self.seed / 'a', self.run_dir)
        self.pack = self.root / 'pack'
        make_pack(self.run_dir, self.pack)

    def accepted(self, run=None, pack=None, failures=()):
        run, pack = run or self.run_dir, pack or self.pack
        paths = []
        for trial in (1, 2, 3):
            path = grade(pack, trial, 'accepted', failures)
            accept_review(run, pack, path, 'current-session')
            paths.append(path)
        return paths

    def cli(self, script, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts' / script), *map(str, args)], capture_output=True, text=True)

    def test_quote_gate_literal_whitespace_ellipsis_and_short_only(self):
        self.assertEqual([], verify_quote('Длинная цитата с   пробелами…другое подтверждённое наблюдение',
                                         'Длинная цитата с\nпробелами, затем другое подтверждённое наблюдение'))
        self.assertTrue(verify_quote('Подставная достаточно длинная цитата', 'Настоящее первичное наблюдение'))
        self.assertTrue(verify_quote('коротко…ещё', 'коротко ещё'))

    def test_one_false_or_foreign_case_quote_causes_rc2_extra_note_is_allowed(self):
        path = grade(self.pack, 1, 'quotes')
        valid = self.cli('check_benchmark_review.py', '--suite', self.pack / 'suite.json', '--input', path, '--grading-pack', self.pack)
        self.assertEqual(0, valid.returncode, valid.stdout)
        failed = read(path); failed['cases'][0]['checks'][0]['status'] = 'fail'; save(path, failed)
        self.assertEqual(1, self.cli('check_benchmark_review.py', '--suite', self.pack / 'suite.json', '--input', path, '--grading-pack', self.pack).returncode)
        row = read(path)
        row['cases'][0]['checks'][0]['evidence'] = row['cases'][1]['checks'][0]['evidence']
        save(path, row)
        result = self.cli('check_benchmark_review.py', '--suite', self.pack / 'suite.json', '--input', path, '--grading-pack', self.pack)
        self.assertEqual(2, result.returncode, result.stdout)

    def test_legacy_quote_audit_checks_corresponding_trial_and_is_not_acceptance(self):
        path = grade(self.pack, 1, 'legacy')
        result = self.cli('check_benchmark_review.py', '--suite', self.pack / 'suite.json', '--input', path, '--verify-quotes', self.run_dir)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn('Legacy quote audit only', result.stdout)
        save(self.run_dir / "trial-1/review.json", read(path))
        with self.assertRaises(FileNotFoundError): load_run(self.run_dir)

    def test_package_tamper_original_tamper_template_tamper_and_outside_review_are_rejected(self):
        path = grade(self.pack, 1, 'valid')
        outside = self.root / 'foreign-review.json'; shutil.copy(path, outside)
        with self.assertRaisesRegex(ValueError, 'inside'): check_pack_review(self.pack, outside)
        original = self.run_dir / 'trial-1/selection/response.md'
        saved = original.read_bytes(); original.write_text('Changed')
        with self.assertRaisesRegex(ValueError, 'original changed'): verify_pack(self.pack, self.run_dir)
        original.write_bytes(saved)
        template = self.pack / 'trial-1/review-template.json'
        value = read(template); value['cases'][0]['seconds'] = 183
        save(template, value)
        with self.assertRaisesRegex(ValueError, 'sha256'): check_pack_review(self.pack, path)

    def test_unrecognized_file_or_missing_one_of_twenty_records_cannot_be_accepted(self):
        path = self.pack / 'trial-1/review-bad.json'; save(path, {'unexpected': 'format'})
        result = self.cli('accept_benchmark_review.py', '--run', self.run_dir, '--pack', self.pack, '--input', path.resolve(), '--session-id', 'current-session')
        self.assertEqual(2, result.returncode)
        self.assertFalse((self.run_dir / 'trial-1/review-acceptance.json').exists())
        path = grade(self.pack, 1, 'short')
        table = read(path); table['cases'][0]['checks'].pop(); save(path, table)
        with self.assertRaises(ValueError): accept_review(self.run_dir, self.pack, path, 'current-session')

    def test_instruction_absence_claim_is_rejected_when_normative_was_supplied(self):
        path = grade(self.pack, 1, 'normative')
        row = read(path)
        check = next(c for c in row['cases'] if c['case_id'] == 'assertion')['checks'][0]
        check.update(status='fail', note='В инструкции нет норматива', missing_sources=['AUTOTEST_NAMING.md'])
        save(path, row)
        with self.assertRaisesRegex(ValueError, 'present'): check_pack_review(self.pack, path)

    def test_russian_note_foreign_script_and_mixed_word_are_rejected_quote_exception_is_explicit(self):
        path = grade(self.pack, 1, 'language')
        row = read(path); row['cases'][0]['checks'][0]['note'] = 'Обнаружена ошибкa 語'
        save(path, row)
        with self.assertRaisesRegex(ValueError, 'writing|mixed'): check_pack_review(self.pack, path)
        other = self.root / 'run-b'; shutil.copytree(self.seed / 'b', other)
        pack = self.root / 'pack-b'; make_pack(other, pack)
        path = grade(pack, 1, 'literal')
        check_pack_review(pack, path)
        row = read(path); row['cases'][0]['checks'][0].pop('literal_defect_quote'); save(path, row)
        with self.assertRaises(ValueError): check_pack_review(pack, path)
        row['cases'][0]['checks'][0]['literal_defect_quote'] = True
        row['cases'][0]['checks'][0]['evidence'] += '…不存在'
        save(path, row)
        with self.assertRaisesRegex(ValueError, 'fragment absent'): check_pack_review(pack, path)
        row = read(grade(self.pack, 1, 'top-note')); row['note'] = 'Посторонний текст 語'
        top_path = self.pack / 'trial-1/review-top-note.json'; save(top_path, row)
        with self.assertRaises(ValueError): check_pack_review(self.pack, top_path)

    def test_text_language_exception_requires_exact_bounded_primary_quote(self):
        source = self.root / 'defect.txt'; source.write_text('Зафиксирована ошибкa 語 в ответе')
        artifact = self.root / 'artifact.md'
        text = 'Дословный дефект: ошибкa 語. Остальной текст корректен.'
        artifact.write_text(text)
        quote = 'ошибкa 語'; start = text.index(quote)
        exception = dict(start=start, end=start + len(quote), quote=quote, source_path=str(source), source_sha256=sha(source))
        self.assertTrue(check_text(text, [], None, artifact))
        self.assertEqual([], check_text(text, [exception], self.root, artifact))
        exception['quote'] = 'неверная цитата'
        with self.assertRaises(ValueError): check_text(text, [exception], self.root, artifact)
        # Escaped JSON must be decoded before inspecting writing systems.
        artifact = self.root / 'escaped.json'
        artifact.write_text(json.dumps(dict(note='Текст 語'), ensure_ascii=True))
        self.assertEqual(2, self.cli('check_russian_artifact.py', '--input', artifact).returncode)

    def test_grader_payload_has_only_copies_and_execution_has_no_filesystem_or_run_directory(self):
        # Relabelled local fixture exercises the live receipt gate, not live quality.
        shutil.rmtree(self.pack)
        run = read(self.run_dir / 'run.json'); run['measurement_kind'] = 'live'; save(self.run_dir / 'run.json', run)
        make_pack(self.run_dir, self.pack)
        prompt = grading_prompt(self.pack, 1)
        self.assertNotIn(str(self.run_dir), prompt)
        self.assertNotIn('raw.json', prompt)
        output = self.pack / 'trial-1/review-grader.json'
        template = grade(self.pack, 1, 'provider')
        with self.assertRaisesRegex(ValueError, 'execution receipt'):
            accept_review(self.run_dir, self.pack, template, 'current-session')
        provider = {'cases': [dict(case_id=c['case_id'], checks=c['checks']) for c in read(template)['cases']]}
        calls = []
        def fake(argv, **kwargs):
            if '--help' in argv: return subprocess.CompletedProcess(argv, 0, ' '.join(NEEDED_FLAGS), '')
            if '--version' in argv: return subprocess.CompletedProcess(argv, 0, 'fixture-gigacode', '')
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, json.dumps({'response': json.dumps(provider), 'usage': {'input_tokens': 7, 'output_tokens': 5}}), '')
        with patch('run_benchmark_grader.shutil.which', return_value=str(self.seed / 'fixture-cli')), patch('run_benchmark_grader.subprocess.run', side_effect=fake):
            run_grader(self.pack, 1, output)
        argv, options = calls[0]
        self.assertIn('--bare', argv); self.assertIn('--exclude-tools', argv); self.assertIn('read_file', argv)
        self.assertNotEqual(self.run_dir, Path(options['cwd']))
        audit = read(output.with_suffix('.json.execution.json'))
        self.assertEqual('validated', audit['status'])
        accept_review(self.run_dir, self.pack, output, 'current-session')

    def test_summary_numbers_are_command_derived_and_modified_number_or_session_rejected(self):
        self.accepted()
        result = command_result(self.run_dir, 'current-session')
        self.assertEqual(60, result['baseline']['counts']['pass'])
        self.assertEqual(sum(len((self.run_dir / f'trial-{t}' / c / 'response.md').read_text()) for t in (1,2,3)
                             for c in ('selection','unknown','assertion','message','blocker')), result['baseline']['response_characters'])
        path = self.root / 'summary.json'; save(path, result)
        check_summary(path, 'current-session')
        with self.assertRaises(ValueError): check_summary(path, 'other-session')
        result['baseline']['response_characters'] = 766; save(path, result)
        with self.assertRaisesRegex(ValueError, 'unsupported'): check_summary(path, 'current-session')

    def test_nearby_score_difference_without_exceeding_procedure_noise_has_required_message(self):
        self.accepted(failures={0,1})
        other = self.root / 'run-b'; shutil.copytree(self.seed / 'b', other)
        pack = self.root / 'pack-b'; make_pack(other, pack); self.accepted(other, pack)
        config = dict(schema_version=1, pack_path=str(self.pack), evaluations=[])
        for name, failed in [('E1',{0,1}), ('E2',set()), ('E3',{0,1,2})]:
            paths = [str(grade(self.pack, trial, name, failed)) for trial in (1,2,3)]
            config['evaluations'].append(dict(evaluation_id=name, review_paths=paths))
        path = self.root / 'dispersion.json'; save(path, config)
        measured, _ = calibration(config)
        self.assertGreater(measured['threshold_passes'], 2)
        result = command_result(self.run_dir, 'current-session', other, path, 'model_requested')
        self.assertEqual('различий не обнаружено', result['comparison']['message'])
        self.assertEqual(2, result['comparison']['difference_price_passes'])
        self.assertTrue(result['candidate']['writing_system_issues'])
        with self.assertRaisesRegex(ValueError, 'dispersion'):
            command_result(self.run_dir, 'current-session', other, None, 'model_requested')

    def test_ungraded_or_unrecognized_input_does_not_close_stage(self):
        self.assertFalse(command_result(self.run_dir, 'current-session')['baseline']['stage_closed'])
        save(self.run_dir / 'trial-1/review.json', {'not': 'recognized'})
        with self.assertRaises(ValueError): command_result(self.run_dir, 'current-session')

    def test_empty_final_answer_is_execution_failure_and_usage_is_preserved(self):
        cli = self.root / 'empty-cli'
        cli.write_text('''#!/usr/bin/env python3
import json, sys
if '--help' in sys.argv: print(%r)
elif '--version' in sys.argv: print('fixture-version')
else: print(json.dumps({'response':'', 'usage':{'input_tokens':123, 'output_tokens':9}}))
''' % ' '.join(NEEDED_FLAGS)); cli.chmod(0o755)
        result = run_benchmark(self.root / 'empty-run', cli=str(cli), model='fixture', measurement_kind='fixture')
        self.assertEqual('failed', result['status'])
        self.assertTrue(all(row['reported_usage'] == {'input_tokens':123, 'output_tokens':9} for row in result['trials']))
        summary = command_result(self.root / 'empty-run', 'current-session')
        self.assertFalse(summary['baseline']['stage_closed'])
        self.assertEqual(615, summary['baseline']['input_tokens']['total'])

    def test_registry_rejects_unsupported_number_and_tracks_all_gate_sources(self):
        self.accepted()
        summary = command_result(self.run_dir, 'current-session'); path = self.root / 'summary.json'; save(path, summary)
        payload = read(ROOT / 'examples/analysis/result.json')['payload']
        payload['benchmark_summary_artifact'] = str(path)
        message = dict(task_id='QA-EVIDENCE', request_id='evidence-1', agent='qa-analyst', event='analysis_ready',
                       summary=summary['summary_text'], artifacts=[str(path)], blockers=[], payload=payload)
        result_path = self.root / 'result.json'; save(result_path, message)
        command = ['--input', result_path, '--task-id','QA-EVIDENCE','--request-id','evidence-1',
                   '--agent','qa-analyst','--evidence-session-id','current-session']
        self.assertEqual(0, self.cli('validate_qa_result.py', *command).returncode)
        message['self_review'] = dict(schema_version=1, checks=[dict(id=cid, status='passed', note='Учебный гейт проверен',
            evidence=['assignment:/acceptance_criteria', 'report:/payload/benchmark_summary_artifact'])
            for cid in ('scope','evidence','consistency','role')], fixes=[], remaining_risks=[])
        save(result_path, message)
        state = self.root / 'task.json'
        exchange.init_task(state, 'QA-EVIDENCE', 'Учебная командная сводка')
        assignment = dict(task_id='QA-EVIDENCE',request_id='evidence-1',agent='qa-analyst',stage='summary',
            goal='Проверить командные числа', acceptance_criteria=['Сводка проходит хост-гейт'], stop_condition='Вернуть проверенный файл',
            expected_artifact='analysis_ready со сводкой',result_path=str(result_path),artifact_base=str(self.root),
            inputs=[str(path)],evidence_session_id='current-session')
        exchange.assign(state, assignment, self.root, None)
        handoff = exchange.prepare_handoff(state, 'evidence-1', None)['result']
        exchange.start_request(state,'evidence-1','fixture-owner',handoff['delivery_id'],None)
        exchange.submit_result(state,'evidence-1')
        protected = exchange.read_state(state)['requests']['evidence-1']['result']['artifact_hashes']
        self.assertIn(str((self.run_dir / 'trial-1/selection/raw.json').resolve()), protected)
        message['summary'] += ' Время составило 183 с.'; save(result_path, message)
        self.assertEqual(2, self.cli('validate_qa_result.py', *command).returncode)
        with self.assertRaises(ValueError): exchange.review_result(state,'evidence-1','Changed summary',True,None)
        profile = context_builder.profile(dict(agent='qa-analyst', evidence_session_id='current-session'), {})
        self.assertIn('docs/BENCHMARK_EVIDENCE_GATES.md', profile['documents'])


if __name__ == '__main__': unittest.main()
