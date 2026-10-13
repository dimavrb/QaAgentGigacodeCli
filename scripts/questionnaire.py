#!/usr/bin/env python3
"""Build an offline question form or collect a single response on localhost."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name}: нужна непустая строка')
    return value.strip()


def normalize_packet(source):
    """Accept a questionnaire or an analyst/test-plan result without changing it."""
    if not isinstance(source, dict):
        raise ValueError('Ожидается JSON-объект')
    task_id = nonempty(source.get('task_id'), 'task_id')
    request_id = nonempty(source.get('request_id'), 'request_id')
    questions = source.get('questions')
    if questions is None:
        payload = source.get('payload', {})
        if not isinstance(payload, dict):
            raise ValueError('payload: ожидается объект')
        questions = payload.get('open_questions', payload.get('blocking_questions'))
    if not isinstance(questions, list):
        raise ValueError('questions: ожидается список вопросов')
    normalized, seen = [], set()
    for item in questions:
        if not isinstance(item, dict):
            raise ValueError('Каждый вопрос должен быть объектом')
        if item.get('status') == 'resolved':
            continue
        qid = nonempty(item.get('question_id'), 'question_id')
        if qid in seen:
            raise ValueError(f'Повторный question_id: {qid}')
        seen.add(qid)
        question = nonempty(item.get('question'), f'{qid}.question')
        options = item.get('options', [])
        if not isinstance(options, list):
            raise ValueError(f'{qid}.options: ожидается список')
        options = [nonempty(option, f'{qid}.options') for option in options]
        if len(options) != len(set(options)):
            raise ValueError(f'{qid}: варианты не должны повторяться')
        kind = item.get('type', 'single' if options else 'text')
        if kind not in ('text', 'single', 'multiple'):
            raise ValueError(f'{qid}.type: используйте text, single или multiple')
        if (kind == 'text' and options) or (kind != 'text' and not options):
            raise ValueError(f'{qid}: тип вопроса не соответствует вариантам')
        row = dict(question_id=qid, question=question, type=kind, options=options)
        for key in ('context', 'blocks'):
            value = item.get(key, '')
            if not isinstance(value, str):
                raise ValueError(f'{qid}.{key}: ожидается строка')
            row[key] = value
        for key in ('requirement_ids', 'scenario_ids'):
            value = item.get(key, [])
            if not isinstance(value, list):
                raise ValueError(f'{qid}.{key}: ожидается список ID')
            row[key] = [nonempty(i, f'{qid}.{key}') for i in value]
        normalized.append(row)
    if not normalized:
        raise ValueError('Нет открытых вопросов для формы')
    packet = dict(schema_version=1, task_id=task_id, request_id=request_id,
                  questionnaire_id=nonempty(source.get('questionnaire_id', f'{task_id}:{request_id}'), 'questionnaire_id'),
                  title=nonempty(source.get('title', 'Вопросы агента'), 'title'),
                  description=source.get('description', 'Ответьте на вопросы и отправьте один общий ответ.'),
                  questions=normalized)
    if not isinstance(packet['description'], str):
        raise ValueError('description: ожидается строка')
    canonical = json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()
    packet['revision'] = hashlib.sha256(canonical).hexdigest()
    return packet


def validate_response(packet, response):
    if not isinstance(response, dict):
        raise ValueError('Ожидается объект ответа')
    for key in ('schema_version', 'task_id', 'request_id', 'questionnaire_id', 'revision'):
        if response.get(key) != packet[key]:
            raise ValueError(f'{key}: ответ относится к другому пакету или ревизии вопросов')
    rows = response.get('answers')
    if not isinstance(rows, list) or len(rows) != len(packet['questions']):
        raise ValueError('Нужно ответить или выбрать «Пока не знаю» для каждого вопроса')
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Ожидается объект ответа на вопрос')
        qid = nonempty(row.get('question_id'), 'question_id')
        if qid in indexed:
            raise ValueError(f'Повторный ответ: {qid}')
        indexed[qid] = row
    answers = []
    for question in packet['questions']:
        qid = question['question_id']
        row = indexed.get(qid)
        if row is None:
            raise ValueError(f'Нет ответа на {qid}')
        status = row.get('status')
        if status not in ('answered', 'deferred'):
            raise ValueError(f'{qid}: неверный статус ответа')
        selected = row.get('selected_options', [])
        text = row.get('text', '')
        if not isinstance(text, str) or not isinstance(selected, list):
            raise ValueError(f'{qid}: неверный формат ответа')
        if any(not isinstance(v, str) or v not in question['options'] for v in selected):
            raise ValueError(f'{qid}: неизвестный вариант ответа')
        if len(selected) != len(set(selected)) or (question['type'] != 'multiple' and len(selected) > 1):
            raise ValueError(f'{qid}: неверное число выбранных вариантов')
        if status == 'deferred' and selected:
            raise ValueError(f'{qid}: отложенный вопрос не может содержать выбранный ответ')
        if status == 'answered' and not (selected or text.strip()):
            raise ValueError(f'{qid}: заполните ответ или выберите «Пока не знаю»')
        answers.append(dict(question_id=qid, status=status, selected_options=selected, text=text.strip()))
    comment = response.get('overall_comment', '')
    if not isinstance(comment, str):
        raise ValueError('overall_comment: ожидается строка')
    return {**{k: packet[k] for k in ('schema_version', 'task_id', 'request_id', 'questionnaire_id', 'revision')},
            'answers': answers, 'overall_comment': comment.strip()}


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def check_saved_response(input_path, answers_path, packet, task_state=None):
    """Read verified answers and recover a missing registry notification, never apply."""
    input_path, answers_path = Path(input_path).resolve(), Path(answers_path).resolve()
    if normalize_packet(read_json(input_path))['revision'] != packet['revision']:
        raise ValueError('Вопросы изменились во время ожидания; нужен новый пакет')
    if task_state is not None:
        from task_exchange import read_state
        state = read_state(task_state)
        row = state['questions'].get(packet['questionnaire_id'])
        if (state['task_id'] != packet['task_id'] or not row
                or row['input_path'] != str(input_path) or row['answers_path'] != str(answers_path)
                or row['packet_revision'] != packet['revision']):
            raise ValueError('Ожидание не соответствует зарегистрированному пакету вопросов')
        if row['answers_hash'] and not answers_path.exists():
            raise ValueError('Зарегистрированный файл ответов исчез')
    if not answers_path.exists():
        return None
    raw = answers_path.read_bytes()
    response = validate_response(packet, json.loads(raw))
    digest = hashlib.sha256(raw).hexdigest()
    if hashlib.sha256(answers_path.read_bytes()).hexdigest() != digest:
        raise ValueError('Ответы изменились во время проверки')
    if task_state is not None:
        from task_exchange import notify_answers, read_state
        notify_answers(task_state, packet, answers_path)
        if read_state(task_state)['questions'][packet['questionnaire_id']]['answers_hash'] != digest:
            raise ValueError('Ответы изменились во время регистрации')
    return dict(response=response, answers_sha256=digest,
                unresolved_question_ids=[a['question_id'] for a in response['answers'] if a['status'] == 'deferred'])


def wait_for_response(input_path, answers_path, *, timeout=1200, poll_seconds=60, task_state=None):
    """Bounded polling of one exact packet, including an immediate and final check."""
    for value, label, minimum, maximum in ((timeout, 'timeout', 0, 86400),
                                            (poll_seconds, 'poll_seconds', .01, 60)):
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f'{label}: допустимо {minimum}..{maximum} секунд')
    input_path, answers_path = Path(input_path).resolve(), Path(answers_path).resolve()
    if input_path == answers_path:
        raise ValueError('Путь ответов совпадает с исходными вопросами')
    packet = normalize_packet(read_json(input_path))
    start = time.monotonic()
    deadline, checks = start + timeout, 0
    while True:
        saved = check_saved_response(input_path, answers_path, packet, task_state)
        checks += 1
        current = time.monotonic()
        result = dict(task_id=packet['task_id'], request_id=packet['request_id'],
                      questionnaire_id=packet['questionnaire_id'], revision=packet['revision'],
                      input_path=str(input_path), answers_path=str(answers_path), checks=checks,
                      elapsed_seconds=max(0, current - start), timeout_seconds=timeout, poll_seconds=poll_seconds)
        if saved is not None:
            return dict(result, status='answers_ready', **saved)
        if current >= deadline:
            return dict(result, status='timeout')
        time.sleep(min(poll_seconds, deadline - current))


def render_html(packet, token='', saved=None, tracked=False):
    config = json.dumps(dict(packet=packet, token=token, saved=saved, tracked=tracked), ensure_ascii=False)
    # JSON lives in a script element; escape HTML delimiters even in user text.
    config = config.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    template = Path(__file__).with_name('questionnaire.html').read_text(encoding='utf-8')
    return template.replace('__QUESTIONNAIRE_CONFIG__', config)


def save_response(path, packet, response):
    result = validate_response(packet, response)
    path.parent.mkdir(parents=True, exist_ok=True)
    result['submitted_at'] = datetime.now(timezone.utc).isoformat()
    # Publish a complete file atomically, without replacing a prior submission.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            saved = read_json(path)
            if validate_response(packet, saved) != validate_response(packet, response):
                raise ValueError('Ответ уже отправлен. Для уточнений агент создаёт новый пакет вопросов.')
            return saved
    finally:
        temporary.unlink()
    return result


def create_server(packet, answers_path, port=0, once=False, on_saved=None):
    token = secrets.token_urlsafe(32)
    saved = read_json(answers_path) if answers_path.exists() else None
    if saved is not None:
        validate_response(packet, saved)
        if on_saved:
            on_saved(saved)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data, content_type='application/json; charset=utf-8'):
            body = data.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def host_valid(self):
            return self.headers.get('Host') == self.server.server_address_string

        def do_GET(self):
            if not self.host_valid():
                self.reply(403, json.dumps({'error': 'Недопустимый адрес'}))
            elif self.path == '/':
                self.reply(200, render_html(packet, token, self.server.saved, bool(on_saved)), 'text/html; charset=utf-8')
            else:
                self.reply(404, '{}')

        def do_POST(self):
            origin = self.headers.get('Origin')
            if (not self.host_valid() or self.headers.get('X-Questionnaire-Token') != token
                    or origin not in (None, self.server.url)):
                self.reply(403, json.dumps({'error': 'Отправляйте ответы с открытой страницы формы'}, ensure_ascii=False))
                return
            if self.path != '/answers':
                self.reply(404, '{}')
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size <= 0 or size > 1_000_000:
                    raise ValueError('Недопустимый размер ответа')
                if self.headers.get_content_type() != 'application/json':
                    raise ValueError('Ожидается application/json')
                response = json.loads(self.rfile.read(size).decode('utf-8'))
                result = save_response(answers_path, packet, response)
                if on_saved:
                    on_saved(result)
                self.server.saved = result
            except (ValueError, UnicodeError) as exc:
                self.reply(400, json.dumps({'error': str(exc)}, ensure_ascii=False))
                return
            except OSError:
                self.reply(500, json.dumps({'error': 'Не удалось сохранить ответы. Повторите отправку.'}, ensure_ascii=False))
                return
            self.reply(200, json.dumps({'ok': True, 'submitted_at': self.server.saved['submitted_at']}, ensure_ascii=False))
            if once:
                threading.Thread(target=self.server.shutdown, daemon=True).start()

    server = HTTPServer(('127.0.0.1', port), Handler)
    server.server_address_string = f'127.0.0.1:{server.server_port}'
    server.url = f'http://{server.server_address_string}'
    server.saved = saved
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    build = sub.add_parser('build', help='Создать самостоятельную HTML-страницу')
    build.add_argument('--input', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    serve = sub.add_parser('serve', help='Открыть форму и сохранить ответы на localhost')
    serve.add_argument('--input', type=Path, required=True)
    serve.add_argument('--answers', type=Path, required=True)
    serve.add_argument('--port', type=int, default=0)
    serve.add_argument('--open', action='store_true', help='Открыть в браузере')
    serve.add_argument('--once', action='store_true', help='Завершить процесс после отправки ответа')
    serve.add_argument('--task-state', type=Path, help='Register answers_ready in the shared task state')
    serve.add_argument('--expected-revision', type=int, help='Required when registering a new tracked question packet')
    check = sub.add_parser('validate', help='Проверить ответы относительно исходных вопросов')
    check.add_argument('--input', type=Path, required=True)
    check.add_argument('--answers', type=Path, required=True)
    wait = sub.add_parser('wait', help='Ждать ответы до 20 минут с проверкой раз в минуту')
    wait.add_argument('--input', type=Path, required=True)
    wait.add_argument('--answers', type=Path, required=True)
    wait.add_argument('--task-state', type=Path, help='Recover answers_ready in the registered task')
    wait.add_argument('--timeout', type=float, default=1200)
    wait.add_argument('--poll-seconds', type=float, default=60)
    args = parser.parse_args()
    try:
        packet = normalize_packet(read_json(args.input))
        if args.command == 'build':
            if args.output.resolve() == args.input.resolve():
                raise ValueError('HTML не должен перезаписывать исходный JSON')
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(render_html(packet), encoding='utf-8')
            print(args.output.resolve())
        elif args.command == 'validate':
            validate_response(packet, read_json(args.answers))
            print('Ответы соответствуют пакету вопросов')
        elif args.command == 'wait':
            result = wait_for_response(args.input, args.answers, timeout=args.timeout,
                                       poll_seconds=args.poll_seconds, task_state=args.task_state)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result['status'] == 'answers_ready' else 1
        else:
            if args.answers.resolve() == args.input.resolve():
                raise ValueError('Ответы не должны перезаписывать вопросы')
            if not 0 <= args.port <= 65535:
                raise ValueError('port: ожидается число от 0 до 65535')
            on_saved = None
            if args.task_state:
                from task_exchange import register_questions, notify_answers
                register_questions(args.task_state, args.input, args.answers, args.expected_revision)
                on_saved = lambda response: notify_answers(args.task_state, packet, args.answers)
            elif args.expected_revision is not None:
                raise ValueError('--expected-revision требует --task-state')
            with create_server(packet, args.answers, args.port, args.once, on_saved) as server:
                print(server.url, flush=True)
                print(f'Ответы: {args.answers.resolve()}', flush=True)
                if args.open:
                    webbrowser.open(server.url)
                if not (args.once and server.saved):
                    try:
                        server.serve_forever()
                    except KeyboardInterrupt:
                        pass
                if server.saved:
                    print('Общий ответ сохранён', flush=True)
    except (OSError, ValueError) as exc:
        print(f'Ошибка формы: {exc}')
        return 2
    except KeyboardInterrupt:
        print('Ожидание ответов остановлено; вопросы и сохранённые ответы оставлены')
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
