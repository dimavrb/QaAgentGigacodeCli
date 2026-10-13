import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from investigation_state import continuation, validate_review, validate_state
from qa_contract import validate_message


class InvestigationTests(unittest.TestCase):
    def fixture(self, name='confirmed'):
        base = ROOT / 'examples/investigations' / name
        return json.loads((base / 'state.json').read_text()), json.loads((base / 'result.json').read_text()), base

    def test_control_scenes_and_unsubstantiated_claim(self):
        for name in ('confirmed', 'not-reproduced', 'expected-behavior', 'blocked', 'unsupported'):
            with self.subTest(scene=name):
                state, report, base = self.fixture(name)
                self.assertEqual([], validate_state(state, 'QA-FIXTURE', base))
                self.assertEqual([], validate_message(report, 'QA-FIXTURE', 'QA-FIXTURE-1', 'qa-defect-investigator'))
                if name != 'blocked':
                    errors = validate_review(report, state, base)
                    self.assertEqual(name == 'unsupported', bool(errors), errors)
                    if name == 'unsupported':
                        self.assertTrue(any('evidence_support' in e for e in errors))

    def test_resume_and_context_changes(self):
        state, _, base = self.fixture('not-reproduced')
        current = json.loads((base / 'current-context.json').read_text())
        result = continuation(state, current, 'QA-FIXTURE-2', base)
        self.assertEqual('resume_ready', result['status'])
        self.assertEqual(['A1'], result['do_not_repeat_without_reason'])
        self.assertEqual(['H1'], result['open_hypotheses'])
        for field in ('environment_id', 'product_version', 'requirements_revision', 'data_revision'):
            changed = copy.deepcopy(current)
            changed['context'][field] += '-new'
            result = continuation(state, changed, 'QA-FIXTURE-2', base)
            self.assertEqual('revalidate_conditions', result['status'])
            self.assertEqual([field], result['changed_conditions'])
            self.assertEqual(['A1'], result['previous_attempts'])

    def test_old_context_observation_and_used_request_are_rejected(self):
        state, _, base = self.fixture()
        current = json.loads((base / 'current-context.json').read_text())
        self.assertEqual('invalid', continuation(state, current, 'QA-FIXTURE-1', base)['status'])
        current['verified_at'] = '2026-10-01T10:00:00+03:00'
        self.assertEqual('invalid', continuation(state, current, 'QA-FIXTURE-2', base)['status'])

    def test_assignment_without_attempt_is_not_reused_and_unknown_conditions_need_check(self):
        state, _, base = self.fixture()
        current = json.loads((base / 'current-context.json').read_text())
        state['used_request_ids'].append('QA-FIXTURE-aborted')
        self.assertEqual('invalid', continuation(state, current, 'QA-FIXTURE-aborted', base)['status'])
        state['context']['data_revision'] = 'unknown'
        current['context']['data_revision'] = 'unknown'
        self.assertEqual('revalidate_conditions', continuation(state, current, 'QA-FIXTURE-2', base)['status'])

    def test_repeated_actions_need_reference_and_reason(self):
        state, _, base = self.fixture()
        repeated = copy.deepcopy(state['attempts'][0])
        repeated.update(attempt_id='A2', request_id='QA-FIXTURE-2', evidence_ids=[])
        state['attempts'].append(repeated)
        state['used_request_ids'].append('QA-FIXTURE-2')
        self.assertTrue(any('repeated actions' in e for e in validate_state(state, 'QA-FIXTURE', base)))
        repeated.update(repeat_of='A1', repeat_reason='Нужна независимая повторная попытка после проверки исходных условий')
        self.assertEqual([], validate_state(state, 'QA-FIXTURE', base))
        repeated['repeat_reason'] = ''
        self.assertTrue(validate_state(state, 'QA-FIXTURE', base))

    def test_changed_or_missing_artifact_rejects_review(self):
        state, report, base = self.fixture()
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp)
            (target / 'observation.txt').write_bytes((base / 'observation.txt').read_bytes())
            self.assertEqual([], validate_review(report, state, target))
            (target / 'observation.txt').write_text('Different observation')
            self.assertTrue(any('hash differs' in e for e in validate_review(report, state, target)))
            (target / 'observation.txt').unlink()
            self.assertTrue(any('cannot be read' in e for e in validate_review(report, state, target)))

    def test_new_state_or_report_invalidates_old_approval(self):
        state, report, base = self.fixture()
        state['context']['product_version'] = 'new-build'
        self.assertTrue(any('state changed' in e for e in validate_review(report, state, base)))
        state, report, base = self.fixture()
        report['payload']['actual_result'] = 'Different fact'
        self.assertTrue(any('assessed report' in e for e in validate_review(report, state, base)))

    def test_review_and_attempt_links_are_required(self):
        state, report, base = self.fixture()
        del state['review']
        self.assertTrue(any('review is required' in e for e in validate_review(report, state, base)))
        state, report, base = self.fixture()
        report['payload']['attempt_ids'] = ['missing']
        self.assertTrue(any('unknown attempt' in e for e in validate_review(report, state, base)))
        state, report, base = self.fixture()
        state['attempts'][0]['hypothesis_id'] = []
        self.assertTrue(validate_state(state, 'QA-FIXTURE', base))

    def test_cli_gate_and_continuation(self):
        for name, expected_code in [('confirmed', 0), ('not-reproduced', 0), ('expected-behavior', 0), ('blocked', 0), ('unsupported', 2)]:
            _, _, base = self.fixture(name)
            result = subprocess.run([sys.executable, str(ROOT/'scripts/validate_qa_result.py'), '--input', str(base/'result.json'), '--task-id', 'QA-FIXTURE', '--request-id', 'QA-FIXTURE-1', '--agent', 'qa-defect-investigator', '--investigation-state', str(base/'state.json')], capture_output=True, text=True)
            self.assertEqual(expected_code, result.returncode, result.stdout + result.stderr)
        _, _, base = self.fixture('not-reproduced')
        command = [sys.executable, str(ROOT/'scripts/validate_investigation_state.py'), '--input', str(base/'state.json'), '--task-id', 'QA-FIXTURE', '--current-context', str(base/'current-context.json'), '--request-id', 'QA-FIXTURE-2']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual('resume_ready', json.loads(result.stdout)['status'])
        with tempfile.TemporaryDirectory() as temp:
            current = json.loads((base/'current-context.json').read_text())
            current['context']['data_revision'] = 'D2'
            path = Path(temp)/'current.json'
            path.write_text(json.dumps(current))
            command[command.index('--current-context')+1] = str(path)
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(3, result.returncode, result.stdout + result.stderr)
            self.assertEqual('revalidate_conditions', json.loads(result.stdout)['status'])


if __name__ == '__main__':
    unittest.main()
