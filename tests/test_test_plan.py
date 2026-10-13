import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from qa_contract import validate_message
from test_plan import validate_plan


class TestPlanTests(unittest.TestCase):
    def setUp(self):
        self.message = json.loads((ROOT / 'examples/test-plan/result-v2.json').read_text())
        self.payload = self.message['payload']

    def validate(self):
        return validate_message(self.message, self.message['task_id'], self.message['request_id'], self.message['agent'])

    def test_example_sequence_validates_with_local_artifacts(self):
        for n in (1, 2, 3):
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/validate_qa_result.py'),
                '--input', str(ROOT / f'examples/test-plan/result-v{n}.json'),
                '--task-id', 'QA-PLAN-1', '--request-id', f'QA-PLAN-1-plan-{n}',
                '--agent', 'qa-test-plan-writer'], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_partial_cannot_be_reported_as_final(self):
        self.message['event'] = 'plan_ready'
        self.assertIn('plan_ready cannot contain unfinished planning', self.validate())

    def test_open_question_cannot_block_ready_requirement_or_shared_case(self):
        for key, value in [('requirement_ids', ['R-1']), ('scenario_ids', ['TC-001'])]:
            with self.subTest(key=key):
                payload = copy.deepcopy(self.payload)
                payload['open_questions'][0][key] = value
                self.assertTrue(any('blocks ready coverage' in e for e in validate_plan(payload, 'plan_partial')))

    def test_unknown_question_links_and_duplicate_ids_rejected(self):
        self.payload['open_questions'][0]['requirement_ids'] = ['missing']
        self.payload['open_questions'][0]['scenario_ids'] = ['missing']
        self.payload['open_questions'].append(copy.deepcopy(self.payload['open_questions'][0]))
        self.payload['requirement_coverage'].append(copy.deepcopy(self.payload['requirement_coverage'][0]))
        errors = self.validate()
        for text in ['unknown requirements', 'unknown scenarios', 'duplicate question_id', 'duplicate requirement_id']:
            self.assertTrue(any(text in e for e in errors), errors)

    def test_resolved_question_needs_answer_and_source(self):
        self.payload['open_questions'][0]['status'] = 'resolved'
        errors = self.validate()
        self.assertTrue(any('resolution' in e for e in errors))
        self.assertTrue(any('decision_source' in e for e in errors))
        self.assertIn('blocked coverage needs an open planning question', errors)

    def test_execution_question_does_not_block_plan_ready(self):
        message = json.loads((ROOT / 'examples/test-plan/result-v3.json').read_text())
        question = message['payload']['open_questions'][0]
        question.update(status='open', blocks='execution', question='Где взять пользователя?')
        self.assertEqual([], validate_plan(message['payload'], 'plan_ready'))

    def test_catalog_ids_must_exactly_match_ready_coverage(self):
        catalog = json.loads((ROOT / 'examples/test-plan/cases-v3.json').read_text())
        errors = validate_plan(self.payload, 'plan_partial', catalog)
        self.assertTrue(any('outside ready coverage' in e for e in errors))
        catalog['test_cases'] = []
        self.assertTrue(any('missing catalog IDs' in e for e in validate_plan(self.payload, 'plan_partial', catalog)))

    def test_coverage_and_partial_stage_cannot_overstate_readiness(self):
        self.assertTrue(any('cannot declare' in e for e in validate_plan(self.payload, 'coverage_ready')))
        self.payload['requirement_coverage'][0]['status'] = 'planned'
        self.assertTrue(any('needs ready and blocked' in e for e in self.validate()))

    def test_malformed_values_return_errors_without_crashing(self):
        for collection, fields in [('requirement_coverage', ['status', 'scenario_ids', 'requirement_id']),
                                   ('open_questions', ['status', 'blocks', 'question_id', 'requirement_ids'])]:
            for field in fields:
                with self.subTest(field=field, collection=collection):
                    payload = copy.deepcopy(self.payload)
                    payload[collection][0][field] = [{}]
                    self.assertTrue(validate_plan(payload, 'plan_partial'))

    def test_cli_rejects_missing_plan_and_bad_catalog_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            message = json.loads((ROOT / 'examples/test-plan/result-v3.json').read_text())
            (root / 'result.json').write_text(json.dumps(message))
            command = [sys.executable, str(ROOT / 'scripts/validate_qa_result.py'), '--input', str(root / 'result.json'),
                       '--task-id', message['task_id'], '--request-id', message['request_id'], '--agent', message['agent']]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(2, result.returncode)
            self.assertIn('plan_artifact', result.stdout)
            (root / 'plan-v3.md').write_text('План')
            catalog = json.loads((ROOT / 'examples/test-plan/cases-v3.json').read_text())
            catalog['test_cases'][1]['scenario_id'] = 'unrelated'
            (root / 'cases-v3.json').write_text(json.dumps(catalog))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(2, result.returncode)
            self.assertIn('missing catalog IDs', result.stdout)


if __name__ == '__main__':
    unittest.main()
