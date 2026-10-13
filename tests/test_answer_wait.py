import copy
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import execution_supervisor as supervisor
import task_exchange as exchange
from questionnaire import normalize_packet, save_response, wait_for_response


class Clock:
    def __init__(self, on_sleep=None):
        self.current = 0
        self.on_sleep = on_sleep

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current = round(self.current + seconds, 10)
        if self.on_sleep:
            self.on_sleep(self.current)


class AnswerWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.answers, self.state = (self.root / name for name in ('questions.json', 'answers.json', 'task.json'))
        self.packet = normalize_packet(dict(task_id='QA-WAIT', request_id='questions-1', questionnaire_id='FORM-1',
            questions=[dict(question_id='Q-1', question='Expected role?')]))
        self.source.write_text(json.dumps(self.packet))
        self.response = {key: self.packet[key] for key in ('schema_version', 'task_id', 'request_id', 'questionnaire_id', 'revision')}
        self.response.update(answers=[dict(question_id='Q-1', status='answered', text='Operator', selected_options=[])], overall_comment='')

    def register(self):
        exchange.init_task(self.state, 'QA-WAIT', 'Synthetic answers')
        exchange.register_questions(self.state, self.source, self.answers, exchange.read_state(self.state)['revision'])

    def virtual_wait(self, clock, **kwargs):
        with patch('questionnaire.time.monotonic', clock.now), patch('questionnaire.time.sleep', clock.sleep):
            return wait_for_response(self.source, self.answers, **kwargs)

    def publish_after(self, clock, seconds, response=None):
        def publish(current):
            if current >= seconds and not self.answers.exists():
                save_response(self.answers, self.packet, response or self.response)
        clock.on_sleep = publish

    def test_answer_after_ten_minutes_is_detected_on_next_minute(self):
        clock = Clock()
        self.publish_after(clock, 601)
        result = self.virtual_wait(clock)
        self.assertEqual('answers_ready', result['status'])
        self.assertEqual(660, result['elapsed_seconds'])
        self.assertEqual('Operator', result['response']['answers'][0]['text'])
        self.assertEqual(1200, result['timeout_seconds'])
        self.assertEqual(60, result['poll_seconds'])

    def test_answer_at_twenty_minute_boundary_is_still_read(self):
        clock = Clock()
        self.publish_after(clock, 1200)
        result = self.virtual_wait(clock)
        self.assertEqual('answers_ready', result['status'])
        self.assertEqual(1200, result['elapsed_seconds'])

    def test_timeout_preserves_open_registry_and_does_not_accept_late_answer(self):
        self.register()
        before = self.state.read_bytes()
        clock = Clock()
        self.publish_after(clock, 1201)
        result = self.virtual_wait(clock, task_state=self.state)
        self.assertEqual('timeout', result['status'])
        self.assertEqual(1200, result['elapsed_seconds'])
        self.assertEqual(21, result['checks'])
        self.assertEqual(before, self.state.read_bytes())
        self.assertFalse(self.answers.exists())
        save_response(self.answers, self.packet, self.response)
        result = wait_for_response(self.source, self.answers, task_state=self.state)
        self.assertEqual('answers_ready', result['status'])
        self.assertEqual(1, result['checks'])

    def test_already_saved_answer_is_immediate_and_notification_is_recovered(self):
        self.register()
        save_response(self.answers, self.packet, self.response)
        clock = Clock(lambda _: self.fail('Existing answer must not sleep'))
        result = self.virtual_wait(clock, task_state=self.state)
        self.assertEqual('answers_ready', result['status'])
        row = exchange.read_state(self.state)['questions']['FORM-1']
        self.assertEqual('answers_ready', row['status'])
        self.assertEqual(result['answers_sha256'], row['answers_hash'])
        self.assertIsNone(row['decision'])
        before = self.state.read_bytes()
        self.virtual_wait(clock, task_state=self.state)
        self.assertEqual(before, self.state.read_bytes())

    def test_deferred_answer_is_delivered_but_remains_unresolved(self):
        self.register()
        response = copy.deepcopy(self.response)
        response['answers'][0].update(status='deferred', text='')
        save_response(self.answers, self.packet, response)
        result = wait_for_response(self.source, self.answers, task_state=self.state)
        self.assertEqual('answers_ready', result['status'])
        self.assertEqual(['Q-1'], result['unresolved_question_ids'])
        self.assertEqual(['Q-1'], exchange.read_state(self.state)['questions']['FORM-1']['unresolved_question_ids'])

    def test_wrong_revision_or_incomplete_json_is_never_received(self):
        for raw in ('{', json.dumps(dict(self.response, revision='wrong'))):
            self.answers.write_text(raw)
            with self.assertRaises(ValueError):wait_for_response(self.source, self.answers, timeout=0)

    def test_question_change_during_wait_is_rejected(self):
        def change(_):
            changed = copy.deepcopy(self.packet)
            changed['questions'][0]['question'] = 'Changed expectation?'
            self.source.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'Вопросы изменились'):
            self.virtual_wait(Clock(change))

    def test_registered_answer_mutation_or_disappearance_is_rejected(self):
        self.register()
        save_response(self.answers, self.packet, self.response)
        wait_for_response(self.source, self.answers, task_state=self.state)
        changed = copy.deepcopy(self.response)
        changed['answers'][0]['text'] = 'Administrator'
        self.answers.write_text(json.dumps(changed))
        with self.assertRaisesRegex(ValueError, 'изменились'):
            wait_for_response(self.source, self.answers, task_state=self.state)
        self.answers.unlink()
        with self.assertRaisesRegex(ValueError, 'исчез'):
            wait_for_response(self.source, self.answers, task_state=self.state)

    def test_invalid_durations_and_unregistered_packet_fail_without_wait(self):
        for kwargs in (dict(timeout=-1), dict(timeout=float('nan')), dict(timeout=float('inf')),
                       dict(poll_seconds=0), dict(poll_seconds=61), dict(timeout=True)):
            with self.assertRaises(ValueError):wait_for_response(self.source, self.answers, **kwargs)
        exchange.init_task(self.state, 'QA-WAIT', 'Synthetic')
        with self.assertRaisesRegex(ValueError, 'зарегистрированному'):
            wait_for_response(self.source, self.answers, task_state=self.state)

    def test_cli_delayed_file_and_timeout_exit_codes(self):
        def publish():
            time.sleep(.1)
            save_response(self.answers, self.packet, self.response)
        thread = threading.Thread(target=publish)
        thread.start()
        command = [sys.executable, str(ROOT / 'scripts/questionnaire.py'), 'wait', '--input', str(self.source),
            '--answers', str(self.answers), '--timeout', '2', '--poll-seconds', '.05']
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=5)
        finally:
            thread.join()
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        self.assertEqual('answers_ready', json.loads(run.stdout)['status'])
        self.answers.unlink()
        command[command.index('--timeout') + 1] = '0'
        run = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(1, run.returncode, run.stdout + run.stderr)
        self.assertEqual('timeout', json.loads(run.stdout)['status'])


class SupervisorAnswerPollingTests(unittest.TestCase):
    def virtual_serve(self, state, clock, tick, **kwargs):
        with patch.object(supervisor, 'controller', return_value=nullcontext()), \
                patch.object(supervisor, 'read', side_effect=lambda _: dict(state)), \
                patch.object(supervisor, 'tick_locked', side_effect=tick), \
                patch.object(supervisor.time, 'monotonic', clock.now), \
                patch.object(supervisor.time, 'sleep', clock.sleep):
            return supervisor.serve('synthetic', **kwargs)

    def test_waiting_answers_polls_for_twenty_minutes_every_sixty_seconds(self):
        state = dict(cancel_requested=False, status='waiting_answers')
        clock, checks = Clock(), []
        def tick(_):
            checks.append(clock.now())
            return dict(state)
        result = self.virtual_serve(state, clock, tick)
        self.assertEqual('waiting_answers', result['status'])
        self.assertEqual(list(range(0, 1201, 60)), checks)

    def test_answer_after_ten_minutes_resumes_prompt_supervision(self):
        state = dict(cancel_requested=False, status='waiting_answers')
        clock, checks = Clock(), []
        def tick(_):
            checks.append(clock.now())
            if clock.now() >= 660:
                state['status'] = 'running' if clock.now() == 660 else 'completed'
            return dict(state)
        result = self.virtual_serve(state, clock, tick)
        self.assertEqual('completed', result['status'])
        self.assertEqual(list(range(0, 661, 60)), checks[:-1])
        self.assertAlmostEqual(660.2, checks[-1])

    def test_cancellation_is_observed_before_next_answer_poll(self):
        state = dict(cancel_requested=False, status='waiting_answers')
        clock = Clock(lambda now: state.update(cancel_requested=True) if now >= 10 else None)
        checks = []
        result = self.virtual_serve(state, clock, lambda _: (checks.append(clock.now()) or dict(state)))
        self.assertTrue(result['cancel_requested'])
        self.assertEqual([0], checks)
        self.assertEqual(10, clock.now())


if __name__ == '__main__':
    unittest.main()
