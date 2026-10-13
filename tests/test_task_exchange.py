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
import task_exchange as exchange
from questionnaire import create_server, normalize_packet, save_response
from self_review_fixtures import review_for


class TaskExchangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'task.json'
        exchange.init_task(self.state, 'QA-1', 'Учебная проверка')
        (self.root / 'evidence.txt').write_text('Учебное доказательство')
        (self.root / 'requirements.md').write_text('Учебное требование')

    @property
    def revision(self):
        return exchange.read_state(self.state)['revision']

    def assignment(self, rid='env-1', dependencies=None):
        return dict(task_id='QA-1', request_id=rid, agent='qa-test-env-configurator',
                    goal='Подтвердить готовность стенда', stage='environment',
                    stop_condition='Вернуть подтверждённые возможности или блокер',
                    expected_artifact='JSON-отчёт и доказательства', acceptance_criteria=['Возможности проверены'],
                    result_path=f'{rid}.json', inputs=['requirements.md'], depends_on=dependencies or [])

    def report(self, rid='env-1'):
        return dict(task_id='QA-1', request_id=rid, agent='qa-test-env-configurator',
                    event='environment_ready', summary='Учебный стенд готов', artifacts=['evidence.txt'], blockers=[],
                    self_review=review_for(),
                    payload=dict(environment_id='synthetic', product_version='fixture-1',
                                 verified_capabilities=['Учебный API доступен'], limitations=[]))

    def register(self, rid='env-1', dependencies=None):
        return exchange.assign(self.state, self.assignment(rid, dependencies), self.root, self.revision)

    def dispatch(self, rid='env-1'):
        return exchange.prepare_handoff(self.state, rid, self.revision)['result']

    def publish(self, rid='env-1', accept=False):
        self.register(rid)
        self.dispatch(rid)
        (self.root / f'{rid}.json').write_text(json.dumps(self.report(rid)))
        result = exchange.submit_result(self.state, rid, self.revision)
        if accept:
            exchange.review_result(self.state, rid, 'Источники и содержимое проверены', True, self.revision)
        return result

    def question_packet(self, request_id='questions-1', qid='form-1'):
        source = dict(task_id='QA-1', request_id=request_id, questionnaire_id=qid,
                      questions=[dict(question_id='Q-1', question='Какие права нужны?')])
        path = self.root / f'{qid}.json'
        path.write_text(json.dumps(source))
        return path, normalize_packet(source), self.root / f'{qid}-answers.json'

    def answers(self, packet, status='answered'):
        response = {key: packet[key] for key in ('schema_version', 'task_id', 'request_id', 'questionnaire_id', 'revision')}
        response.update(answers=[dict(question_id='Q-1', status=status, text='Администратор' if status == 'answered' else '', selected_options=[])], overall_comment='')
        return response

    def test_registry_reopens_without_loss_and_rejects_different_task(self):
        self.register()
        self.assertEqual('QA-1', exchange.read_state(self.state)['task_id'])
        self.assertEqual(2, exchange.init_task(self.state, 'QA-1', 'Учебная проверка')['revision'])
        with self.assertRaises(ValueError):
            exchange.init_task(self.state, 'QA-OTHER', 'Другая задача')

    def test_dispatch_reserves_single_delivery_and_binds_owner(self):
        self.register()
        handoff = self.dispatch()
        self.assertEqual('dispatching', exchange.read_state(self.state)['requests']['env-1']['status'])
        with self.assertRaises(ValueError):
            self.dispatch()
        with self.assertRaises(ValueError):
            exchange.start_request(self.state, 'env-1', 'cli-task-7', 'wrong', self.revision)
        exchange.start_request(self.state, 'env-1', 'cli-task-7', handoff['delivery_id'], self.revision)
        self.assertEqual('cli-task-7', exchange.read_state(self.state)['requests']['env-1']['owner_task_id'])

    def test_unprepared_missing_duplicate_and_stale_assignments_rejected(self):
        self.register()
        before = self.state.read_bytes()
        for operation in (lambda: exchange.submit_result(self.state, 'env-1', self.revision),
                          lambda: self.register(),
                          lambda: exchange.assign(self.state, self.assignment('env-2'), self.root, 1),
                          lambda: exchange.prepare_handoff(self.state, 'missing', self.revision)):
            with self.assertRaises(ValueError):
                operation()
            self.assertEqual(before, self.state.read_bytes())

    def test_structural_errors_never_publish_a_ready_event(self):
        self.register()
        self.dispatch()
        before = self.state.read_bytes()
        for changes in [dict(request_id='wrong'), dict(agent='qa-analyst'), dict(payload={})]:
            report = {**self.report(), **changes}
            (self.root / 'env-1.json').write_text(json.dumps(report))
            with self.assertRaises(ValueError):
                exchange.submit_result(self.state, 'env-1', self.revision)
            self.assertEqual(before, self.state.read_bytes())

    def test_self_review_required_and_evidence_bound_to_assignment(self):
        self.register()
        handoff = self.dispatch()
        self.assertIs(True, handoff['assignment']['self_review_required'])
        self.assertTrue(Path(handoff['self_review_guide']).is_file())
        before = self.state.read_bytes()
        for mutation in ('missing', 'unknown-criterion', 'self-proof'):
            report = self.report()
            if mutation == 'missing':
                del report['self_review']
            elif mutation == 'unknown-criterion':
                report['self_review']['checks'][0]['evidence'].append('assignment:/acceptance_criteria/5')
            else:
                report['self_review']['checks'][0]['evidence'] = ['report:/self_review']
            (self.root / 'env-1.json').write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, 'self_review|evidence reference'):
                exchange.submit_result(self.state, 'env-1', self.revision)
            self.assertEqual(before, self.state.read_bytes())

    def test_limited_review_reported_without_content_acceptance(self):
        self.register()
        self.dispatch()
        report = self.report()
        report.update(event='blocked', blockers=['Учебный доступ недоступен'], self_review=review_for(limited=True))
        (self.root / 'env-1.json').write_text(json.dumps(report))
        exchange.submit_result(self.state, 'env-1', self.revision)
        row = exchange.read_state(self.state)['requests']['env-1']
        self.assertEqual('reported', row['status'])
        self.assertIsNone(row['review'])
        report['self_review']['remaining_risks'] = ['Новый вывод без нового назначения']
        (self.root / 'env-1.json').write_text(json.dumps(report))
        with self.assertRaises(ValueError):
            exchange.review_result(self.state, 'env-1', 'Принимаю', True, self.revision)

    def test_new_assignment_cannot_disable_review_but_archived_assignment_works(self):
        assignment = self.assignment()
        assignment['self_review_required'] = False
        with self.assertRaisesRegex(ValueError, 'self_review_required'):
            exchange.assign(self.state, assignment, self.root, self.revision)
        self.register()
        self.dispatch()
        # Simulate an existing registry created before the new policy.
        state = exchange.read_state(self.state)
        del state['requests']['env-1']['assignment']['self_review_required']
        exchange.atomic_write(self.state, state)
        report = self.report()
        del report['self_review']
        (self.root / 'env-1.json').write_text(json.dumps(report))
        exchange.submit_result(self.state, 'env-1', self.revision)
        self.assertEqual('reported', exchange.read_state(self.state)['requests']['env-1']['status'])

    def test_full_validator_checks_catalog_not_only_message_shape(self):
        source = json.loads((ROOT / 'examples/test-plan/result-v3.json').read_text())
        source.update(task_id='QA-1', request_id='plan-1')
        source['artifacts'] = []
        source['payload']['plan_artifact'] = str(ROOT / 'examples/test-plan/plan-v3.md')
        source['payload']['test_cases_artifact'] = str(self.root / 'bad-cases.json')
        (self.root / 'bad-cases.json').write_text(json.dumps({'schema_version': 1, 'test_cases': []}))
        assignment = self.assignment('plan-1')
        assignment['agent'] = 'qa-test-plan-writer'
        exchange.assign(self.state, assignment, self.root, self.revision)
        self.dispatch('plan-1')
        (self.root / 'plan-1.json').write_text(json.dumps(source))
        with self.assertRaisesRegex(ValueError, 'Результат не прошёл'):
            exchange.submit_result(self.state, 'plan-1', self.revision)

    def test_handoff_requires_review_and_checks_changed_evidence(self):
        self.publish()
        with self.assertRaisesRegex(ValueError, 'не принят'):
            self.register('env-2', ['env-1'])
        exchange.review_result(self.state, 'env-1', 'Содержание проверено', True, self.revision)
        self.register('env-2', ['env-1'])
        (self.root / 'evidence.txt').write_text('Изменённое доказательство')
        with self.assertRaisesRegex(ValueError, 'изменились'):
            self.dispatch('env-2')
        self.assertEqual('queued', exchange.read_state(self.state)['requests']['env-2']['status'])

    def test_input_mutation_is_blocked_before_start_and_submission(self):
        self.register()
        (self.root / 'requirements.md').write_text('Новая версия')
        with self.assertRaisesRegex(ValueError, 'Исходные материалы'):
            self.dispatch()

    def test_result_cannot_be_changed_after_registration_or_review(self):
        self.publish()
        (self.root / 'evidence.txt').write_text('Другое доказательство')
        for operation in (lambda: exchange.submit_result(self.state, 'env-1', self.revision),
                          lambda: exchange.review_result(self.state, 'env-1', 'Принимаю', True, self.revision)):
            with self.assertRaises(ValueError):
                operation()

    def test_submission_is_idempotent_and_event_survives_until_acknowledged(self):
        result = self.publish()
        before = self.state.read_bytes()
        self.assertEqual(result, exchange.submit_result(self.state, 'env-1', self.revision))
        self.assertEqual(before, self.state.read_bytes())
        event_id = result['result']['event_id']
        for _ in range(2):
            events = exchange.wait_events(self.state, event_id - 1, 0)['events']
            self.assertEqual([event_id], [e['event_id'] for e in events])
        exchange.acknowledge(self.state, event_id, self.revision)
        self.assertEqual([], exchange.wait_events(self.state, event_id - 1, 0)['events'])

    def test_wait_returns_new_event_without_chat_input(self):
        result = {}
        thread = threading.Thread(target=lambda: result.update(exchange.wait_events(self.state, 0, 3)))
        thread.start()
        self.register()
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual('assignment_ready', result['events'][0]['kind'])

    def test_paths_cannot_overwrite_registry_or_another_assignment(self):
        for path in (str(self.state), str(self.state) + '.lock', 'requirements.md'):
            assignment = self.assignment()
            assignment['result_path'] = path
            with self.assertRaises(ValueError):
                exchange.assign(self.state, assignment, self.root, self.revision)
        self.register()
        assignment = self.assignment('env-2')
        assignment['result_path'] = 'env-1.json'
        with self.assertRaises(ValueError):
            exchange.assign(self.state, assignment, self.root, self.revision)

    def test_competing_processes_cannot_overwrite_same_revision(self):
        processes = []
        for rid in ('env-1', 'env-2'):
            path = self.root / f'{rid}-assignment.json'
            path.write_text(json.dumps(self.assignment(rid)))
            processes.append(subprocess.Popen([sys.executable, str(ROOT / 'scripts/manage_task.py'), '--state', str(self.state),
                                             'assign', '--input', str(path), '--expected-revision', '1'], stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for process in processes:
            process.communicate(timeout=10)
        self.assertEqual([0, 2], sorted(p.returncode for p in processes))
        state = exchange.read_state(self.state)
        self.assertEqual(2, state['revision'])
        self.assertEqual(1, len(state['requests']))

    def test_questions_signal_is_idempotent_and_deferred_stays_open(self):
        self.publish(accept=True)
        path, packet, answers_path = self.question_packet()
        exchange.register_questions(self.state, path, answers_path, self.revision)
        save_response(answers_path, packet, self.answers(packet, 'deferred'))
        first = exchange.notify_answers(self.state, packet, answers_path)
        self.assertEqual(first, exchange.notify_answers(self.state, packet, answers_path))
        self.assertEqual(['Q-1'], first['result']['unresolved_question_ids'])
        exchange.apply_answers(self.state, 'form-1', 'Независимые проверки продолжены', self.revision)
        self.assertEqual(['Q-1'], exchange.read_state(self.state)['open_question_ids'])
        with self.assertRaisesRegex(ValueError, 'Пока не знаю'):
            exchange.finish_task(self.state, 'passed', 'Готово', self.revision)
        path, packet, answers_path = self.question_packet('questions-2', 'form-2')
        exchange.register_questions(self.state, path, answers_path, self.revision)
        save_response(answers_path, packet, self.answers(packet))
        exchange.notify_answers(self.state, packet, answers_path)
        exchange.apply_answers(self.state, 'form-2', 'Получено решение по Q-1', self.revision)
        self.assertEqual([], exchange.read_state(self.state)['open_question_ids'])
        exchange.finish_task(self.state, 'passed', 'Все обязательные условия проверены', self.revision)
        self.assertEqual('completed', exchange.read_state(self.state)['execution_status'])
        with self.assertRaises(ValueError):
            self.register('env-2')

    def test_changed_question_or_answers_never_creates_a_signal(self):
        path, packet, answers_path = self.question_packet()
        exchange.register_questions(self.state, path, answers_path, self.revision)
        save_response(answers_path, packet, self.answers(packet))
        before = self.state.read_bytes()
        source = json.loads(path.read_text())
        source['questions'][0]['question'] = 'Изменённый вопрос'
        path.write_text(json.dumps(source))
        with self.assertRaisesRegex(ValueError, 'Исходные вопросы'):
            exchange.notify_answers(self.state, packet, answers_path)
        self.assertEqual(before, self.state.read_bytes())

    def test_handoff_uses_only_applied_answers_and_detects_later_mutation(self):
        path, packet, answers_path = self.question_packet()
        exchange.register_questions(self.state, path, answers_path, self.revision)
        save_response(answers_path, packet, self.answers(packet))
        exchange.notify_answers(self.state, packet, answers_path)
        assignment = self.assignment()
        assignment['questionnaire_ids'] = ['form-1']
        with self.assertRaisesRegex(ValueError, 'не применены'):
            exchange.assign(self.state, assignment, self.root, self.revision)
        exchange.apply_answers(self.state, 'form-1', 'Решение проверено по вопросу Q-1', self.revision)
        exchange.assign(self.state, assignment, self.root, self.revision)
        handoff = self.dispatch()
        self.assertEqual(str(answers_path.resolve()), handoff['answer_sources'][0]['answers_path'])
        self.assertEqual([], handoff['answer_sources'][0]['unresolved_question_ids'])
        response = json.loads(answers_path.read_text())
        response['answers'][0]['text'] = 'Другое решение'
        answers_path.write_text(json.dumps(response))
        with self.assertRaisesRegex(ValueError, 'Исходные материалы'):
            exchange.start_request(self.state, 'env-1', 'cli-task-2', handoff['delivery_id'], self.revision)

    def test_readiness_is_checked_against_the_assignment_scenario_set(self):
        assignment = self.assignment()
        assignment['scenario_ids'] = ['TC-001']
        exchange.assign(self.state, assignment, self.root, self.revision)
        self.dispatch()
        report = json.loads((ROOT / 'examples/handoffs/environment.json').read_text())
        report.update(task_id='QA-1', request_id='env-1')
        (self.root / 'env-1.json').write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'Результат не прошёл'):
            exchange.submit_result(self.state, 'env-1', self.revision)

    def test_transitive_sources_are_rechecked_before_handoff(self):
        self.publish('env-1', accept=True)
        self.register('env-2', ['env-1'])
        self.dispatch('env-2')
        (self.root / 'env-2.json').write_text(json.dumps(self.report('env-2')))
        exchange.submit_result(self.state, 'env-2', self.revision)
        exchange.review_result(self.state, 'env-2', 'Проверено', True, self.revision)
        self.register('env-3', ['env-2'])
        report = json.loads((self.root / 'env-1.json').read_text())
        report['summary'] = 'Изменён только самый ранний источник'
        (self.root / 'env-1.json').write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'изменились'):
            self.dispatch('env-3')

    def test_tracked_form_retries_after_notification_failure_and_recovers_on_restart(self):
        path, packet, answers_path = self.question_packet()
        exchange.register_questions(self.state, path, answers_path, self.revision)
        callbacks = []
        def on_saved(response):
            callbacks.append(response)
            if len(callbacks) == 1:
                raise OSError('Учебный сбой реестра')
            exchange.notify_answers(self.state, packet, answers_path)
        with create_server(packet, answers_path, on_saved=on_saved) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                self.addCleanup(conn.close)
                conn.request('GET', '/')
                page = conn.getresponse().read().decode()
                config = json.loads(re.search(r'<script id="config" type="application/json">(.*?)</script>', page, re.S).group(1))
                self.assertTrue(config['tracked'])
                headers = {'Content-Type': 'application/json', 'X-Questionnaire-Token': config['token']}
                for status in (500, 200):
                    conn.request('POST', '/answers', json.dumps(self.answers(packet)), headers)
                    result = conn.getresponse()
                    self.assertEqual(status, result.status, result.read())
                    if status == 500:
                        self.assertIsNone(server.saved)
                        self.assertTrue(answers_path.exists())
            finally:
                server.shutdown()
                thread.join(2)
        before = self.state.read_bytes()
        with create_server(packet, answers_path, on_saved=on_saved) as server:
            self.assertIsNotNone(server.saved)
        self.assertEqual(before, self.state.read_bytes())
        self.assertEqual(1, len([e for e in exchange.read_state(self.state)['events'] if e['kind'] == 'answers_ready']))

    def test_failed_delivery_is_not_retried_under_same_request_id(self):
        self.register()
        self.dispatch()
        exchange.fail_request(self.state, 'env-1', 'Проверено: исполнитель не запустился', self.revision)
        with self.assertRaises(ValueError):
            self.dispatch()
        self.register('env-2')
        self.dispatch('env-2')

    def test_single_running_owner_and_resolved_review_history(self):
        self.register('env-1')
        first = self.dispatch('env-1')
        self.register('env-2')
        second = self.dispatch('env-2')
        exchange.start_request(self.state, 'env-1', 'cli-owner', first['delivery_id'], self.revision)
        with self.assertRaisesRegex(ValueError, 'другое активное'):
            exchange.start_request(self.state, 'env-2', 'cli-owner', second['delivery_id'], self.revision)
        (self.root / 'env-1.json').write_text(json.dumps(self.report('env-1')))
        exchange.submit_result(self.state, 'env-1', self.revision)
        exchange.review_result(self.state, 'env-1', 'Нужно дополнительное подтверждение', False, self.revision)
        exchange.start_request(self.state, 'env-2', 'cli-owner', second['delivery_id'], self.revision)
        (self.root / 'env-2.json').write_text(json.dumps(self.report('env-2')))
        exchange.submit_result(self.state, 'env-2', self.revision)
        exchange.review_result(self.state, 'env-2', 'Уточнённый результат принят', True, self.revision)
        self.assertEqual('passed', exchange.read_state(self.state)['review_status'])


if __name__ == '__main__':
    unittest.main()
