import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from self_review import validate_self_review
from qa_contract import validate_message


class SelfReviewTests(unittest.TestCase):
    def setUp(self):
        self.message = json.loads((ROOT / 'examples/task-exchange/result.json').read_text())
        self.assignment = json.loads((ROOT / 'examples/task-exchange/assignment.json').read_text())

    def validate(self):
        return validate_self_review(self.message, self.assignment, required=True)

    def test_valid_final_example_and_legacy_compatibility(self):
        self.assertEqual([], self.validate())
        del self.message['self_review']
        self.assertEqual([], validate_message(self.message, self.message['task_id'], self.message['request_id'], self.message['agent']))
        self.assertEqual(['missing self_review'], self.validate())

    def test_every_check_required_and_unique(self):
        for mutate in ('missing', 'duplicate', 'unknown'):
            with self.subTest(mutate=mutate):
                self.setUp()
                checks = self.message['self_review']['checks']
                if mutate == 'missing':
                    checks.pop()
                elif mutate == 'duplicate':
                    checks.append(copy.deepcopy(checks[0]))
                else:
                    checks[0]['id'] = 'confidence'
                self.assertTrue(self.validate())

    def test_limited_needs_explicit_remaining_risk(self):
        self.message['self_review']['checks'][1]['status'] = 'limited'
        self.message['self_review']['remaining_risks'] = []
        self.assertIn('limited self-review needs remaining_risks', self.validate())
        self.message['self_review']['remaining_risks'] = ['Нет доступа для обязательного запуска']
        self.assertEqual([], self.validate())

    def test_checks_require_concrete_notes_and_references(self):
        for field, value in (('note', ' '), ('evidence', []), ('evidence', ['checked everything']), ('status', 'approved')):
            with self.subTest(field=field, value=value):
                self.setUp()
                self.message['self_review']['checks'][1][field] = value
                self.assertTrue(self.validate())

    def test_unresolvable_or_circular_evidence_rejected(self):
        for reference in ('report:/payload/missing', 'assignment:/inputs/999', 'report:/self_review/checks/0',
                          'report:/artifacts/-1', 'report:/artifacts/00', 'report:/payload/bad~2key', 'file:/evidence.txt'):
            with self.subTest(reference=reference):
                self.message['self_review']['checks'][1]['evidence'] = [reference]
                self.assertTrue(self.validate())

    def test_assignment_identity_and_scope_cannot_be_invented(self):
        self.assignment['request_id'] = 'another'
        self.assertTrue(self.validate())
        self.assignment['request_id'] = self.message['request_id']
        self.message['self_review']['checks'][0]['evidence'] = ['report:/summary']
        self.assertTrue(self.validate())

    def test_pointer_escaping_and_structural_claim_limits(self):
        self.message['payload']['a/b~c'] = 'Доказательство'
        self.message['self_review']['checks'][1]['evidence'] = ['report:/payload/a~1b~0c']
        self.assertEqual([], self.validate())
        self.message['self_review']['checks'][1]['note'] = 'Автор заявляет, что всё проверено'
        # Format validation cannot decide whether an author actually inspected evidence.
        self.assertEqual([], self.validate())

    def test_malformed_values_return_errors_without_crashing(self):
        baseline = copy.deepcopy(self.message)
        for value in (None, [], {'schema_version': True, 'checks': []}):
            self.message = copy.deepcopy(baseline)
            self.message['self_review'] = value
            self.assertTrue(self.validate())
        for field in ('id', 'status', 'note', 'evidence'):
            self.message = copy.deepcopy(baseline)
            self.message['self_review']['checks'][0][field] = {}
            self.assertTrue(self.validate())

    def test_cli_validates_independent_assignment(self):
        command = [sys.executable, str(ROOT / 'scripts/validate_qa_result.py'),
                   '--input', str(ROOT / 'examples/task-exchange/result.json'),
                   '--task-id', self.message['task_id'], '--request-id', self.message['request_id'],
                   '--agent', self.message['agent'], '--require-self-review',
                   '--self-review-assignment', str(ROOT / 'examples/task-exchange/assignment.json')]
        run = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)


if __name__ == '__main__':
    unittest.main()
