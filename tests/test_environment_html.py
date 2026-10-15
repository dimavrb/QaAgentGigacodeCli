"""Environment report publication and questions on synthetic observations."""
import copy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import build_environment_html as page
import environment_readiness as environment
import task_exchange as exchange
from environment_fixtures import bind_environment, attach_environment_checks
from qa_tasks import references
from self_review_fixtures import review_for, publish_content_review


class Links(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.links = []
        self.scripts = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'a': self.links.append(values['href'])
        if tag == 'script': self.scripts.append(values)


class EnvironmentHTMLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'task.json'
        exchange.init_task(self.state, 'QA-HTML-ENV', 'Synthetic environment report')
        self.proof = self.root / 'probe.txt'
        self.proof.write_text('Synthetic fixture observation')
        self.assignment = dict(task_id='QA-HTML-ENV', request_id='env', agent='qa-test-env-configurator',
            stage='environment', goal='Verify the complete synthetic guide',
            acceptance_criteria=['Every point has evidence'], stop_condition='Return result',
            expected_artifact='QA JSON', result_path=str(self.root / 'result.json'),
            inputs=[], depends_on=[], scenario_ids=['TC-1', 'TC-2'])
        bind_environment(self.assignment, self.root)
        self.output = self.root / 'result.environment.html'

    def message(self, event='environment_ready'):
        message = dict(task_id='QA-HTML-ENV', request_id='env', agent='qa-test-env-configurator', event=event,
            summary='Synthetic environment setup report', artifacts=[],
            blockers=['Synthetic dependency is unavailable'] if event == 'blocked' else [],
            self_review=review_for(limited=event == 'blocked'), payload=dict(environment_id='fixture',
                product_version='v1', verified_capabilities=['Synthetic only'], limitations=[], open_questions=[]))
        if event == 'environment_partial':
            message['payload'].update(ready_scenarios=['TC-1'], blocked_scenarios=['TC-2'])
        return attach_environment_checks(message, self.assignment, [str(self.proof)])

    def submit(self, message=None):
        exchange.assign(self.state, self.assignment, self.root, None)
        exchange.prepare_handoff(self.state, 'env', None)
        exchange.atomic_write(self.assignment['result_path'], message or self.message())
        exchange.submit_result(self.state, 'env', None)

    def accept(self, message=None):
        self.submit(message)
        return exchange.review_result(self.state, 'env', 'Synthetic guide and evidence read', True, None)

    def presentation(self):
        return exchange.read_state(self.state)['requests']['env']['presentation']

    def ready_document(self):
        self.assertEqual('ready', self.presentation()['status'], self.presentation())
        self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), self.presentation()['sha256'])
        return self.output.read_text()

    def question(self, *, blocking=False, status='open'):
        return dict(question_id='Q-1', question='Where is the missing integration endpoint?', status=status,
                    blocking=blocking, point_ids=['P-2'], scenario_ids=['TC-2'], context='Synthetic remaining question')

    def test_acceptance_returns_offline_report_with_every_point_and_source_link(self):
        result = self.accept()
        document = self.ready_document()
        self.assertEqual('ready', result['result']['presentation']['status'])
        for value in ('Стенд готов', 'P-1', 'P-2', 'Synthetic point 1', 'Synthetic fixture observation',
                      'Открытых вопросов нет.', 'TC-1', 'TC-2', 'fixture', 'v1'):
            self.assertIn(value, document)
        parsed = Links(document)
        self.assertFalse(parsed.scripts)
        for href in parsed.links:
            if not href.startswith('#'):
                self.assertTrue((self.output.parent / unquote(href)).is_file(), href)

    def test_partial_report_shows_blocking_question_and_unfinished_point(self):
        message = self.message('environment_partial')
        message['payload']['open_questions'] = [self.question(blocking=True)]
        self.accept(message)
        document = self.ready_document()
        self.assertIn('Стенд подготовлен частично', document)
        self.assertIn('Where is the missing integration endpoint?', document)
        self.assertIn('Блокирует настройку', document)
        self.assertIn('Synthetic missing condition', document)
        self.assertEqual('environment_partial', exchange.read_state(self.state)['requests']['env']['result']['event'])

    def test_blocked_without_guide_still_returns_report_and_question(self):
        del self.assignment['environment_assignment']
        message = self.message('blocked')
        message['payload'] = dict(open_questions=[dict(self.question(blocking=True), point_ids=[])])
        self.accept(message)
        document = self.ready_document()
        self.assertIn('Настройка заблокирована', document)
        self.assertIn('Админгайд не предоставлен', document)
        self.assertIn('Where is the missing integration endpoint?', document)
        with self.assertRaises(ValueError):
            exchange.finish_task(self.state, 'passed', 'Stand remains blocked', None)

    def test_only_open_questions_are_shown_and_missing_question_data_is_explicit(self):
        message = self.message()
        resolved = dict(self.question(status='resolved'), question='Resolved old question',
                        resolution='Answered', decision_source='Synthetic decision')
        message['payload']['open_questions'] = [resolved]
        self.accept(message)
        document = self.ready_document()
        self.assertNotIn('Resolved old question', document)
        self.assertIn('Открытых вопросов нет.', document)

    def test_missing_question_field_is_not_claimed_to_be_no_questions(self):
        message = self.message()
        del message['payload']['open_questions']
        self.accept(message)
        document = self.ready_document()
        self.assertIn('Сведения об оставшихся вопросах не указаны', document)
        self.assertNotIn('Открытых вопросов нет.', document)

    def test_ready_cannot_have_open_blocking_question(self):
        message = self.message()
        message['payload']['open_questions'] = [self.question(blocking=True)]
        with self.assertRaisesRegex(ValueError, 'blocking question excludes'):
            self.submit(message)
        self.assertFalse(self.output.exists())

    def test_question_ids_status_and_point_bindings_are_validated(self):
        for mutation in ('duplicate', 'foreign-point', 'foreign-scenario', 'status', 'resolved', 'passed-point'):
            message = self.message('environment_partial')
            question = self.question(blocking=True)
            message['payload']['open_questions'] = [question]
            if mutation == 'duplicate': message['payload']['open_questions'].append(copy.deepcopy(question))
            elif mutation == 'foreign-point': question['point_ids'] = ['unknown']
            elif mutation == 'foreign-scenario': question['scenario_ids'] = ['unknown']
            elif mutation == 'status': question['status'] = 'ignored'
            elif mutation == 'resolved': question['status'] = 'resolved'
            else: question['point_ids'] = ['P-1']
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                environment.check_result(message, self.root, self.assignment['environment_assignment'])

    def test_reported_or_rejected_result_does_not_publish_a_final_page(self):
        self.submit()
        with self.assertRaisesRegex(ValueError, 'not accepted'):
            page.publish(self.state, 'env')
        exchange.review_result(self.state, 'env', 'Author must correct observations', False, None)
        self.assertNotIn('presentation', exchange.read_state(self.state)['requests']['env'])
        self.assertFalse(self.output.exists())

    def test_changed_evidence_marks_view_needs_attention_and_preserves_original_page(self):
        self.accept()
        original = self.output.read_bytes()
        self.proof.write_text('Changed after acceptance')
        result = page.publish(self.state, 'env')
        self.assertEqual('needs_attention', result['result']['status'])
        self.assertEqual(original, self.output.read_bytes())
        self.assertEqual('accepted', exchange.read_state(self.state)['requests']['env']['status'])

    def test_repeat_publication_is_idempotent(self):
        self.accept()
        before = self.state.read_bytes()
        original = self.output.read_bytes()
        page.publish(self.state, 'env')
        self.assertEqual(original, self.output.read_bytes())
        self.assertEqual(before, self.state.read_bytes())

    def test_existing_output_is_not_overwritten_and_acceptance_survives(self):
        self.output.write_text('Unrelated user document')
        result = self.accept()
        self.assertEqual('needs_attention', result['result']['presentation']['status'])
        self.assertEqual('Unrelated user document', self.output.read_text())
        self.assertEqual('accepted', exchange.read_state(self.state)['requests']['env']['status'])

    def test_custom_output_cannot_overwrite_source_or_another_request_input(self):
        self.accept()
        other_input = self.root / 'another-owner.txt'
        other_input.write_text('Another assignment input')
        exchange.assign(self.state, dict(self.assignment, request_id='another',
            result_path=str(self.root / 'another.json'), inputs=[str(other_input)]), self.root, None)
        for target in (self.state, self.proof, Path(self.assignment['result_path']),
                       Path(self.assignment['environment_assignment']), other_input):
            before = target.read_bytes()
            result = page.publish(self.state, 'env', target)
            self.assertEqual('needs_attention', result['result']['status'])
            if target != self.state:
                self.assertEqual(before, target.read_bytes())
            else:
                self.assertEqual('QA-HTML-ENV', exchange.read_state(self.state)['task_id'])

    def test_symlink_output_is_rejected(self):
        target = self.root / 'user.html'
        target.write_text('User document')
        self.output.symlink_to(target)
        self.accept()
        self.assertEqual('needs_attention', self.presentation()['status'])
        self.assertEqual('User document', target.read_text())

    def test_user_text_and_template_tokens_are_escaped_without_second_expansion(self):
        message = self.message()
        message['summary'] = '<script>alert(1)</script> {{POINTS}}'
        message['payload']['open_questions'] = [dict(self.question(), question='<img src=x onerror=alert(2)>')]
        self.accept(message)
        document = self.ready_document()
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt; {{POINTS}}', document)
        self.assertIn('&lt;img src=x onerror=alert(2)&gt;', document)
        self.assertEqual(2, document.count('<article class="point '))
        self.assertFalse(Links(document).scripts)

    def test_task_discovery_exposes_only_report_reference_fields(self):
        self.accept()
        row = exchange.read_state(self.state)['requests']['env']
        ref = references('env', row)['presentation']
        self.assertEqual({'status', 'kind', 'path', 'sha256', 'source_sha256'}, set(ref))
        self.assertEqual('environment_report', ref['kind'])
        self.assertEqual(ref, exchange.state_summary(exchange.read_state(self.state))['requests'][0]['presentation'])

    def test_late_blocking_review_prevents_republication(self):
        self.accept()
        original = self.output.read_bytes()
        publish_content_review(self.state, 'env')
        result = page.publish(self.state, 'env')
        self.assertEqual('needs_attention', result['result']['status'])
        self.assertIn('Unresolved blocking', result['result']['error'])
        self.assertEqual(original, self.output.read_bytes())

    def test_cli_can_publish_accepted_report_to_a_separate_file(self):
        self.accept()
        output = self.root / 'copy.environment.html'
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/build_environment_html.py'),
            '--state', str(self.state), '--request-id', 'env', '--output', str(output)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('ready', json.loads(result.stdout)['result']['status'])
        self.assertTrue(output.is_file())


if __name__ == '__main__':
    unittest.main()
