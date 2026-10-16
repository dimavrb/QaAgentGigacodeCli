import copy
import http.client
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from questionnaire import create_server, normalize_packet, render_html, save_response, validate_response


class QuestionnaireTests(unittest.TestCase):
    def setUp(self):
        self.source = json.loads((ROOT / 'examples/questions/questions.json').read_text())
        self.packet = normalize_packet(self.source)
        self.response = {k: self.packet[k] for k in ('schema_version', 'task_id', 'request_id', 'questionnaire_id', 'revision')}
        self.response.update(answers=[
            dict(question_id='Q-1', status='answered', selected_options=['Только администратор'], text=''),
            dict(question_id='Q-2', status='answered', selected_options=['Новый', 'Оплачен'], text='В обеих ролях'),
            dict(question_id='Q-3', status='deferred', selected_options=[], text='Уточню сборку завтра')],
            overall_comment='Начните с независимых проверок')

    def test_existing_reports_keep_question_ids_and_filter_resolved(self):
        for filename in ('analysis/result-partial.json', 'test-plan/result-v2.json'):
            with self.subTest(filename=filename):
                source = json.loads((ROOT / 'examples' / filename).read_text())
                before = copy.deepcopy(source)
                packet = normalize_packet(source)
                questions = source['payload'].get('open_questions', source['payload'].get('blocking_questions'))
                self.assertEqual([q['question_id'] for q in questions if q.get('status') != 'resolved'],
                                 [q['question_id'] for q in packet['questions']])
                self.assertEqual(before, source)

    def test_revision_changes_when_question_or_context_changes(self):
        for key in ('question', 'context', 'options'):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.source)
                changed['questions'][0][key] = ['Другой вариант'] if key == 'options' else 'Новый текст'
                self.assertNotEqual(self.packet['revision'], normalize_packet(changed)['revision'])

    def test_malformed_packets_and_duplicate_ids_rejected(self):
        bad = [None, {}, {**self.source, 'questions': []}]
        for field, value in [('question_id', ''), ('type', 'unknown'), ('options', ['Да', 'Да']),
                             ('options', [1]), ('scenario_ids', 'TC-1'), ('context', None)]:
            source = copy.deepcopy(self.source)
            source['questions'][0][field] = value
            bad.append(source)
        duplicate = copy.deepcopy(self.source)
        duplicate['questions'].append(duplicate['questions'][0])
        bad.append(duplicate)
        for source in bad:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    normalize_packet(source)

    def test_complete_response_accepts_free_text_and_deferred(self):
        self.assertEqual(self.response, validate_response(self.packet, self.response))
        self.response['answers'][0].update(selected_options=[], text='Сотрудник службы безопасности')
        self.assertEqual('Сотрудник службы безопасности', validate_response(self.packet, self.response)['answers'][0]['text'])

    def test_missing_duplicate_unknown_and_empty_answers_rejected(self):
        variants = []
        for key in ('revision', 'request_id', 'questionnaire_id', 'task_id'):
            variants.append({**self.response, key: 'wrong'})
        variants.append({**self.response, 'answers': self.response['answers'][:-1]})
        for changes in [dict(question_id='Q-3'), dict(question_id='unknown'), dict(selected_options=['Несуществующий']),
                        dict(selected_options=['Только администратор', 'Никто']), dict(status='deferred'),
                        dict(selected_options=[], text=' '), dict(selected_options=[{}]), dict(status='resolved')]:
            response = copy.deepcopy(self.response)
            response['answers'][0].update(changes)
            variants.append(response)
        for response in variants:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    validate_response(self.packet, response)

    def test_safe_html_embedding_for_untrusted_question_text(self):
        self.source['questions'][0]['question'] = '</script><script>alert("injected")</script>&'
        packet = normalize_packet(self.source)
        page = render_html(packet)
        self.assertNotIn(self.source['questions'][0]['question'], page)
        config = re.search(r'<script id="config" type="application/json">(.*?)</script>', page, re.S).group(1)
        self.assertEqual(packet, json.loads(config)['packet'])

    def test_save_is_atomic_idempotent_and_cannot_replace_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'answers.json'
            saved = save_response(path, self.packet, self.response)
            self.assertIn('submitted_at', saved)
            original = path.read_bytes()
            self.assertEqual(saved, save_response(path, self.packet, self.response))
            changed = copy.deepcopy(self.response)
            changed['overall_comment'] = 'Новое решение'
            with self.assertRaisesRegex(ValueError, 'уже отправлен'):
                save_response(path, self.packet, changed)
            self.assertEqual(original, path.read_bytes())
            self.assertEqual([path], list(path.parent.iterdir()))

    def test_build_and_validate_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            args = [sys.executable, str(ROOT / 'scripts/questionnaire.py')]
            source = str(ROOT / 'examples/questions/questions.json')
            run = subprocess.run(args + ['build', '--input', source, '--output', str(path / 'form.html')], capture_output=True, text=True)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
            self.assertTrue((path / 'form.html').read_text().startswith('<!doctype html>'))
            self.assertEqual(['form.html'], sorted(item.name for item in path.iterdir()))
            (path / 'answers.json').write_text(json.dumps(self.response))
            run = subprocess.run(args + ['validate', '--input', source, '--answers', str(path / 'answers.json')], capture_output=True, text=True)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)

    def test_build_rejects_answers_destination_without_touching_existing_answers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            answers = path / 'answers.json'
            answers.write_text('Existing response must survive')
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/questionnaire.py'),
                                  'build', '--input', str(ROOT / 'examples/questions/questions.json'),
                                  '--output', str(path / 'form.html'), '--answers', str(answers)],
                                 capture_output=True, text=True, timeout=5)
            self.assertEqual(2, run.returncode)
            self.assertIn('unrecognized arguments: --answers', run.stderr)
            self.assertEqual('Existing response must survive', answers.read_text())
            self.assertFalse((path / 'form.html').exists())

    def test_all_deferred_answers_preserve_overall_comment_when_saved_and_reloaded(self):
        source = copy.deepcopy(self.source)
        source['questions'] = [dict(source['questions'][0], question_id=f'Q-{i}') for i in range(1, 11)]
        packet = normalize_packet(source)
        response = {key: packet[key] for key in
                    ('schema_version', 'task_id', 'request_id', 'questionnaire_id', 'revision')}
        response.update(answers=[dict(question_id=f'Q-{i}', status='deferred',
                                     selected_options=[], text='') for i in range(1, 11)],
                        overall_comment='Продолжайте независимые кейсы; доступ будет завтра')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'answers.json'
            save_response(path, packet, response)
            saved = validate_response(packet, json.loads(path.read_text()))
            self.assertEqual(response, saved)
            self.assertEqual(10, len(saved['answers']))
            self.assertTrue(all(answer['status'] == 'deferred' for answer in saved['answers']))

    def test_http_submission_origin_token_validation_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'answers.json'
            with create_server(self.packet, path) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                self.addCleanup(thread.join, 2)
                try:
                    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    self.addCleanup(conn.close)
                    conn.request('GET', '/')
                    result = conn.getresponse()
                    self.assertEqual(200, result.status)
                    page = result.read().decode()
                    config = json.loads(re.search(r'<script id="config" type="application/json">(.*?)</script>', page, re.S).group(1))
                    headers = {'Content-Type': 'application/json', 'X-Questionnaire-Token': config['token'], 'Origin': server.url}
                    body = json.dumps(self.response)
                    for replacement, expected in [({'X-Questionnaire-Token': 'wrong'}, 403),
                                                  ({'Origin': 'https://foreign.example'}, 403),
                                                  ({'Host': 'foreign.example'}, 403), ({}, 200), ({}, 200)]:
                        conn.request('POST', '/answers', body, {**headers, **replacement})
                        result = conn.getresponse()
                        self.assertEqual(expected, result.status, result.read())
                    original = path.read_bytes()
                    changed = {**self.response, 'revision': 'stale'}
                    conn.request('POST', '/answers', json.dumps(changed), headers)
                    result = conn.getresponse()
                    self.assertEqual(400, result.status, result.read())
                    self.assertEqual(original, path.read_bytes())
                    conn.request('GET', '/../../README.md')
                    result = conn.getresponse()
                    self.assertEqual(404, result.status, result.read())
                    conn.request('GET', '/')
                    result = conn.getresponse()
                    restored = result.read().decode()
                    self.assertIn('submitted_at', restored)
                finally:
                    server.shutdown()
                    thread.join(2)
            with create_server(self.packet, path) as restarted:
                self.assertEqual(self.response, validate_response(self.packet, restarted.saved))

    def test_http_once_exits_after_saved_response_and_stale_restart_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'answers.json'
            with create_server(self.packet, path, once=True) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                    self.addCleanup(conn.close)
                    conn.request('GET', '/')
                    page = conn.getresponse().read().decode()
                    config = json.loads(re.search(r'<script id="config" type="application/json">(.*?)</script>', page, re.S).group(1))
                    conn.request('POST', '/answers', json.dumps(self.response),
                                 {'Content-Type': 'application/json', 'X-Questionnaire-Token': config['token']})
                    result = conn.getresponse()
                    self.assertEqual(200, result.status, result.read())
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(self.response, validate_response(self.packet, json.loads(path.read_text())))
                finally:
                    server.shutdown()
                    thread.join(2)
            changed = copy.deepcopy(self.source)
            changed['questions'][0]['question'] = 'Изменённый вопрос'
            with self.assertRaisesRegex(ValueError, 'revision'):
                create_server(normalize_packet(changed), path)


if __name__ == '__main__':
    unittest.main()
