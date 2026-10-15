"""Accepted test-plan HTML navigation and publication gates on synthetic fixtures."""
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'scripts'), str(ROOT/'tests')]
import task_exchange as exchange
import build_test_plan_html as page
from qa_tasks import references
from self_review_fixtures import review_for


class Links(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.ids = set(); self.links = []; self.current = None
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if 'id' in values:
            assert values['id'] not in self.ids, 'duplicate HTML id'
            self.ids.add(values['id'])
        if tag == 'a':
            self.current = dict(values, text='')
            self.links.append(self.current)

    def handle_data(self, data):
        if self.current is not None:
            self.current['text'] += data

    def handle_endtag(self, tag):
        if tag == 'a':
            self.current = None


class TestPlanHTMLTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root/'task.json'
        for name in ('requirements.md', 'plan-v3.md', 'cases-v3.json'):
            (self.root/name).write_bytes((ROOT/'examples/test-plan'/name).read_bytes())
        self.message = json.loads((ROOT/'examples/test-plan/result-v3.json').read_text())
        self.message.update(task_id='QA-HTML', request_id='plan', self_review=review_for())
        self.result = self.root/'result.json'
        self.output = self.root/'result.plan.html'
        self.inputs = [str(self.root/'requirements.md')]

    def reported(self, criteria=None):
        exchange.init_task(self.state, 'QA-HTML', 'Учебный план', criteria)
        assignment = dict(task_id='QA-HTML', request_id='plan', agent='qa-test-plan-writer', stage='planning',
            goal='Проверить фильтр и отказ входа', acceptance_criteria=['Оба требования покрыты'],
            criterion_ids=['C1'] if criteria else [], stop_condition='Отчёт', expected_artifact='QA JSON',
            result_path=str(self.result), inputs=self.inputs, depends_on=[])
        exchange.assign(self.state, assignment, self.root, None)
        handoff = exchange.prepare_handoff(self.state, 'plan', None)['result']
        exchange.start_request(self.state, 'plan', 'fixture-author', handoff['delivery_id'], None)
        exchange.atomic_write(self.result, self.message)
        exchange.submit_result(self.state, 'plan', None)

    def accept(self, criteria=None):
        self.reported(criteria)
        return exchange.review_result(self.state, 'plan', 'Учебное содержательное принятие', True, None)

    def presentation(self):
        return exchange.read_state(self.state)['requests']['plan']['presentation']

    def require_ready(self):
        self.assertEqual('ready', self.presentation()['status'], self.presentation())
        return self.output.read_text()

    def test_final_review_automatically_creates_html_with_resolvable_requirement_links(self):
        result = self.accept()
        document = self.require_ready()
        parsed = Links(document)
        for link in parsed.links:
            if link['href'].startswith('#'):
                self.assertIn(link['href'][1:], parsed.ids)
        for rid in ('R-1', 'R-2'):
            self.assertGreaterEqual(len([item for item in parsed.links if item['text'] == rid]), 2)
        self.assertIn('ORDER-PAID', document)
        self.assertIn('Вход с неверным паролем отклоняется.', document)
        self.assertIn('Тест-дата', document)
        self.assertEqual(result['revision'], exchange.read_state(self.state)['revision'])
        self.assertEqual('ready', result['result']['presentation']['status'])

    def test_reported_plan_cannot_be_presented_as_accepted(self):
        self.reported()
        with self.assertRaisesRegex(ValueError, 'not accepted'):
            page.publish(self.state, 'plan')
        self.assertFalse(self.output.exists())

    def test_changes_requested_does_not_create_html(self):
        self.reported()
        exchange.review_result(self.state, 'plan', 'Исправить', False, None)
        self.assertFalse(self.output.exists())
        self.assertNotIn('presentation', exchange.read_state(self.state)['requests']['plan'])

    def test_partial_plan_does_not_create_final_html(self):
        self.message = json.loads((ROOT/'examples/test-plan/result-v2.json').read_text())
        self.message.update(task_id='QA-HTML', request_id='plan', self_review=review_for())
        for name in ('plan-v2.md', 'cases-v2.json'):
            (self.root/name).write_bytes((ROOT/'examples/test-plan'/name).read_bytes())
        self.accept()
        self.assertFalse(self.output.exists())
        self.assertNotIn('presentation', exchange.read_state(self.state)['requests']['plan'])

    def test_shared_case_has_all_requirement_links(self):
        self.message['payload']['requirement_coverage'][1]['scenario_ids'].append('TC-001')
        self.accept()
        document = self.require_ready()
        first = document.split('class="card case"', 2)[1]
        self.assertIn('>R-1</a>', first)
        self.assertIn('>R-2</a>', first)

    def test_new_step_data_and_additional_catalog_metadata_are_preserved(self):
        path = self.root/'cases-v3.json'
        catalog = json.loads(path.read_text())
        case = catalog['test_cases'][0]
        case['business_goal'] = 'Сохранить товар'
        case['steps'][0].update(test_data='SKU: <demo>',
            request=dict(method='POST', path='/orders', headers={'X-Demo':'synthetic'}, body=dict(kind='json', value={'sku':'<demo>'})),
            response=dict(status=201, example=dict(scope='fragment', json={'id':'actual-id'})))
        path.write_text(json.dumps(catalog, ensure_ascii=False))
        self.accept()
        document = self.require_ready()
        self.assertIn('Сохранить товар', document)
        self.assertIn('SKU: &lt;demo&gt;', document)
        self.assertIn('actual-id', document)
        self.assertIn('X-Demo', document)
        self.assertIn('Фрагмент JSON-ответа', document)

    def test_empty_requirement_source_is_not_a_ready_html_view(self):
        (self.root/'requirements.md').write_text('')
        self.message['payload']['requirement_coverage'][0]['source'] = 'requirements.md'
        self.accept()
        self.assertEqual('needs_attention', self.presentation()['status'])
        self.assertFalse(self.output.exists())

    def test_external_requirement_link_goes_directly_to_original_url_without_fetching(self):
        url = 'https://requirements.example.invalid/spec#access&role=buyer'
        self.message['payload']['requirement_coverage'][0]['source'] = url
        with patch('urllib.request.urlopen', side_effect=AssertionError('no network')):
            self.accept()
        links = Links(self.require_ready()).links
        matches = [item for item in links if item['text'] == 'R-1']
        self.assertTrue(matches)
        for item in matches:
            self.assertEqual(url, item['href'])
            self.assertEqual('noopener noreferrer', item['rel'])

    def test_source_must_have_been_part_of_accepted_revision(self):
        self.inputs = []
        self.accept()
        self.assertEqual('needs_attention', self.presentation()['status'])
        self.assertIn('not included', self.presentation()['error'])
        self.assertEqual('accepted', exchange.read_state(self.state)['requests']['plan']['status'])
        self.assertFalse(self.output.exists())

    def test_unresolvable_anchor_is_visible_failure_not_a_fake_link(self):
        self.message['payload']['requirement_coverage'][0]['source'] = 'requirements.md#unknown'
        result = self.accept()
        self.assertEqual('needs_attention', result['result']['presentation']['status'])
        self.assertIn('#unknown', self.presentation()['error'])
        self.assertFalse(self.output.exists())

    def test_unsafe_or_credentialed_requirement_url_is_rejected(self):
        for source in ('javascript:alert(1)', 'data:text/html,abc', 'https://user:password@example.invalid/spec'):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    page.remote_source(source)

    def test_source_html_and_template_markers_cannot_inject_markup(self):
        source = self.root/'requirements.md'
        source.write_text(source.read_text().replace('Фильтр', '<script>alert(1)</script> {{CASES}} Фильтр'))
        self.accept()
        document = self.require_ready()
        self.assertNotIn('<script>alert(1)</script>', document)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt; {{CASES}}', document)

    def test_modified_accepted_catalog_or_requirements_cannot_regenerate_html(self):
        self.accept(); original = self.output.read_bytes()
        source = self.root/'requirements.md'; source.write_text(source.read_text() + '\nChanged')
        outcome = page.publish(self.state, 'plan')
        self.assertEqual('needs_attention', outcome['result']['status'])
        self.assertEqual(original, self.output.read_bytes())

    def test_regeneration_is_idempotent_and_preserves_registered_sources(self):
        self.accept()
        sources = {name:(self.root/name).read_bytes() for name in ('requirements.md', 'plan-v3.md', 'cases-v3.json')}
        original = self.output.read_bytes(); revision = exchange.read_state(self.state)['revision']
        page.publish(self.state, 'plan')
        self.assertEqual(revision, exchange.read_state(self.state)['revision'])
        self.assertEqual(original, self.output.read_bytes())
        for name, data in sources.items():
            self.assertEqual(data, (self.root/name).read_bytes())
        self.assertEqual(hashlib.sha256(original).hexdigest(), self.presentation()['sha256'])

    def test_source_or_existing_unrelated_output_cannot_be_overwritten(self):
        self.accept()
        source = self.root/'requirements.md'; original = source.read_bytes()
        result = page.publish(self.state, 'plan', source)
        self.assertEqual('needs_attention', result['result']['status'])
        self.assertEqual(original, source.read_bytes())
        occupied = self.root/'other.html'; occupied.write_text('Other work')
        result = page.publish(self.state, 'plan', occupied)
        self.assertEqual('needs_attention', result['result']['status'])
        self.assertEqual('Other work', occupied.read_text())

    def test_replaced_input_fifo_is_rejected_without_blocking(self):
        self.accept()
        source = self.root/'requirements.md'; source.unlink(); os.mkfifo(source)
        try:
            result = page.publish(self.state, 'plan')
            self.assertEqual('needs_attention', result['result']['status'])
            self.assertIn('Regular file', result['result']['error'])
        finally:
            source.unlink()

    def test_discovery_reference_is_limited_to_presentation_fields(self):
        self.accept()
        row = exchange.read_state(self.state)['requests']['plan']
        value = references('plan', row)['presentation']
        self.assertEqual({'status', 'path', 'sha256', 'plan_revision'}, set(value))
        self.assertEqual(str(self.output), value['path'])

    def test_explicit_cli_output_and_bad_request_return_controlled_results(self):
        self.accept()
        output = self.root/'export.html'
        command = [sys.executable, str(ROOT/'scripts/build_test_plan_html.py'), '--state', str(self.state),
                   '--request-id', 'plan', '--output', str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(output.is_file())
        command[command.index('plan')] = 'missing'
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertNotIn('Traceback', result.stderr)

    def test_markdown_heading_and_line_anchors_select_exact_source_text(self):
        text = '# Spec\n## R-1\nFirst\n### Detail\nNested\n## R-2\nSecond\n'
        self.assertEqual('## R-1\nFirst\n### Detail\nNested', page.markdown_section(text, 'r-1'))
        self.assertEqual('First\n', page.markdown_section(text, 'L3'))
        self.assertEqual('First\n### Detail\n', page.markdown_section(text, 'L3-L4'))
        for fragment in ('unknown', 'L0', 'L2-L20'):
            with self.assertRaises(ValueError):
                page.markdown_section(text, fragment)
        self.assertEqual('## Name\nSecond', page.markdown_section('## Name\nFirst\n## Name\nSecond', 'name-1'))

    def test_required_independent_review_must_finish_before_html_is_ready(self):
        criteria = dict(schema_version=1, criteria=[dict(criterion_id='C1', description='Plan reviewed',
            source='requirements.md', required=True, required_reviews=[dict(agent='qa-result-reviewer', scope='oracle')])])
        self.reported(criteria)
        author = exchange.read_state(self.state)['requests']['plan']
        result = author['result']
        assignment = dict(task_id='QA-HTML', request_id='review', agent='qa-result-reviewer', stage='review',
            goal='Review plan', acceptance_criteria=['Both requirements covered'], criterion_ids=['C1'],
            stop_condition='Report', expected_artifact='QA JSON', result_path=str(self.root/'review.json'),
            inputs=list(result['artifact_hashes']), depends_on=[], review_criteria_ids=['C1'],
            review_target=dict(request_id='plan', result_path=result['path'],
                artifact_hashes=result['artifact_hashes'], scope='oracle'))
        exchange.assign(self.state, assignment, self.root, None)
        handoff = exchange.prepare_handoff(self.state, 'review', None)['result']
        exchange.start_request(self.state, 'review', 'fixture-reviewer', handoff['delivery_id'], None)
        message = dict(task_id='QA-HTML', request_id='review', agent='qa-result-reviewer', event='review_passed',
            summary='Synthetic independent review', artifacts=[], blockers=[], self_review=review_for(),
            payload=dict(target_request_id='plan', target_sha256=result['artifact_hashes'][result['path']],
                review_scope='oracle', reviewed_revision='v3', checks=[dict(criterion_id='C1', status='passed', note='Synthetic source checked', evidence=[str(self.root/'requirements.md')])],
                findings=[], limitations=[]))
        exchange.atomic_write(self.root/'review.json', message)
        exchange.submit_result(self.state, 'review', None)
        exchange.review_result(self.state, 'plan', 'Coordinator decision before required reviewer acceptance', True, None)
        self.assertEqual('needs_attention', self.presentation()['status'])
        self.assertFalse(self.output.exists())
        exchange.review_result(self.state, 'review', 'Required independent review accepted', True, None)
        page.publish(self.state, 'plan')
        self.require_ready()
        self.assertEqual(['review'], self.presentation()['review_request_ids'])
        (self.root/'review.json').write_text('{}')
        self.assertEqual('needs_attention', page.publish(self.state, 'plan')['result']['status'])


if __name__ == '__main__':
    unittest.main()
