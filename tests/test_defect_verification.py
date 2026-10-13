import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from investigation_state import review_digest, result_digest, validate_review, validate_state
from qa_contract import validate_message


class DefectVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'verification'
        shutil.copytree(ROOT / 'examples/investigations/verification', self.base)
        shutil.copyfile(ROOT / 'examples/investigations/requirements.md', self.base.parent / 'requirements.md')
        self.state = json.loads((self.base / 'state.json').read_text())
        self.result = json.loads((self.base / 'result.json').read_text())
        self.record = json.loads((self.base / 'verification.json').read_text())

    def save(self):
        """Simulate a fresh assessment for gate tests, not a real content review."""
        (self.base / 'verification.json').write_text(json.dumps(self.record, ensure_ascii=False))
        for item in self.state['evidence']:
            item['sha256'] = hashlib.sha256((self.base / item['path']).read_bytes()).hexdigest()
        self.state['review']['state_sha256'] = review_digest(self.state)
        self.state['review']['result_sha256'] = result_digest(self.result)
        for name, body in [('state.json', self.state), ('result.json', self.result)]:
            (self.base / name).write_text(json.dumps(body, ensure_ascii=False))

    def errors(self):
        self.save()
        return validate_review(self.result, self.state, self.base)

    def unavailable_code(self):
        self.record['code_path'] = {
            'status': 'unavailable', 'repository': None, 'revision': None,
            'deployment_match': 'unknown', 'deployment_evidence_ids': [], 'steps': [],
            'assessment': 'Код недоступен; факт нарушения подтверждается первичным ответом.'}
        self.record['cause'] = {'status': 'unknown', 'scope': None, 'statement': None, 'evidence_ids': []}
        self.record['limitations'].append('Код недоступен; причина не установлена')
        self.result['payload']['limitations'] = copy.deepcopy(self.record['limitations'])

    def test_synthetic_code_and_observation_support_fixture_verdict(self):
        self.assertEqual([], validate_message(self.result, 'QA-FIXTURE', 'QA-FIXTURE-1', 'qa-defect-investigator'))
        self.assertEqual([], validate_review(self.result, self.state, self.base))
        # Exercise the deliberately faulty synthetic branch independently of the validator.
        namespace = {}
        exec((self.base / 'application.py').read_text(), namespace)
        status, body = namespace['admin_users']('buyer')
        self.assertEqual(200, status)
        self.assertEqual(['fixture-user'], body['users'])
        self.assertEqual('list_users_without_role_check', body['branch'])
        self.assertIn(body['branch'], (self.base / 'observation.txt').read_text())

    def test_defect_can_be_confirmed_without_code_or_cause(self):
        self.unavailable_code()
        self.assertEqual([], self.errors())

    def test_static_code_alone_cannot_confirm_defect(self):
        self.record['observation']['runtime_evidence_ids'] = []
        self.record['cause'].update(status='suspected', evidence_ids=['E2'])
        self.assertTrue(any('code alone' in error for error in self.errors()))
        self.record['observation']['runtime_evidence_ids'] = ['E2']
        self.assertTrue(any('source evidence cannot also' in error for error in self.errors()))

    def test_source_mismatch_or_unknown_prevents_confirmed_application_cause(self):
        for match in ('unknown', 'mismatch'):
            with self.subTest(match=match):
                self.record['code_path']['deployment_match'] = match
                self.assertTrue(any('confirmed application cause' in e for e in self.errors()))
        self.record['cause']['status'] = 'suspected'
        self.assertEqual([], self.errors())  # The runtime violation remains confirmed.

    def test_confirmed_cause_needs_source_runtime_and_deployment_proof(self):
        for ids in (['E2'], ['E1'], ['E3']):
            with self.subTest(ids=ids):
                self.record['cause']['evidence_ids'] = ids
                self.assertTrue(self.errors())
        self.record['cause']['evidence_ids'] = ['E1', 'E2', 'E3']
        self.record['code_path']['deployment_evidence_ids'] = []
        self.assertTrue(any('matched deployment' in e for e in self.errors()))

    def test_proofs_must_belong_to_executed_current_context_attempts(self):
        self.state['attempts'][0]['context']['product_version'] = 'old-build'
        self.assertTrue(any('current context' in e for e in self.errors()))
        self.state['attempts'][0]['context'] = copy.deepcopy(self.state['context'])
        self.state['attempts'][0]['status'] = 'blocked'
        self.assertTrue(any('executed' in e for e in self.errors()))

    def test_required_verification_cannot_be_omitted_or_unregistered(self):
        del self.result['payload']['verification_artifact']
        self.assertTrue(any('verification_artifact is required' in e for e in self.errors()))
        self.result['payload']['verification_artifact'] = 'unregistered.json'
        self.assertTrue(any('registered' in e for e in self.errors()))
        self.state['verification_required'] = 'true'
        self.assertTrue(any('must be a boolean' in e for e in validate_state(self.state, 'QA-FIXTURE', self.base)))

    def test_changed_verification_invalidates_approval_even_with_updated_file_hash(self):
        self.record['cause']['statement'] = 'Изменённый вывод'
        (self.base / 'verification.json').write_text(json.dumps(self.record))
        self.assertTrue(any('hash differs' in e for e in validate_review(self.result, self.state, self.base)))
        for item in self.state['evidence']:
            item['sha256'] = hashlib.sha256((self.base / item['path']).read_bytes()).hexdigest()
        self.assertTrue(any('state changed' in e for e in validate_review(self.result, self.state, self.base)))
        (self.base / 'verification.json').unlink()
        self.assertTrue(any('cannot be read' in e for e in validate_review(self.result, self.state, self.base)))

    def test_targeted_cause_check_does_not_block_established_defect(self):
        self.unavailable_code()
        self.record['next_check'] = {'action': 'targeted_check', 'goal': 'cause_analysis',
                                     'gap': 'Какая эффективная настройка прав на обработчике?',
                                     'reason': 'Ответ достаточен для нарушения; настройка нужна только для причины', 'repeat_of': None}
        self.assertEqual([], self.errors())
        self.record['next_check']['goal'] = 'defect_confirmation'
        self.assertTrue(any('confirmation pending' in e for e in self.errors()))
        self.record['next_check'].update(action='retest', goal='cause_analysis', gap='')
        errors = self.errors()
        self.assertTrue(any('missing fact' in e for e in errors))
        self.assertTrue(any('previous attempt' in e for e in errors))
        self.record['next_check'].update(gap='Сработает ли отказ при отключённом учебном тоггле?', repeat_of='A1')
        self.assertEqual([], self.errors())

    def test_artifact_cannot_be_its_own_proof_and_fields_are_consistent(self):
        self.record['observation']['runtime_evidence_ids'] = ['E4']
        self.assertTrue(any('itself as proof' in e for e in self.errors()))
        self.record['observation']['runtime_evidence_ids'] = ['unknown']
        self.assertTrue(any('named attempts' in e for e in self.errors()))
        self.record['observation']['runtime_evidence_ids'] = ['E1']
        self.record['business_rule']['relation'] = 'matches'
        self.assertTrue(any('inconsistent' in e for e in self.errors()))
        self.record['business_rule']['relation'] = 'violated'
        self.record['observation']['actual_result'] = 'HTTP 403'
        self.assertTrue(any('actual_result' in e for e in self.errors()))

    def test_invalid_or_missing_artifact_content_is_rejected(self):
        self.record['code_path']['steps'][0]['line'] = True
        self.assertTrue(any('positive line' in e for e in self.errors()))
        self.record['code_path']['steps'][0].update(line=3, file='../application.py')
        self.assertTrue(any('repository-relative' in e for e in self.errors()))
        self.record['code_path']['steps'][0]['file'] = 'application.py'
        del self.record['next_check']['gap']
        self.assertTrue(any('gap is required' in e for e in self.errors()))
        (self.base / 'verification.json').write_text('null')
        self.state['evidence'][-1]['sha256'] = hashlib.sha256((self.base / 'verification.json').read_bytes()).hexdigest()
        self.assertTrue(any('must be an object' in e for e in validate_review(self.result, self.state, self.base)))

    def test_cli_requires_state_and_supports_absolute_paths_from_another_directory(self):
        args = [sys.executable, str(ROOT / 'scripts/validate_qa_result.py'),
                '--input', str(self.base / 'result.json'), '--task-id', 'QA-FIXTURE',
                '--request-id', 'QA-FIXTURE-1', '--agent', 'qa-defect-investigator']
        result = subprocess.run(args, cwd=self.temp.name, text=True, capture_output=True)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn('requires --investigation-state', result.stdout)
        result = subprocess.run(args + ['--investigation-state', str(self.base / 'state.json')],
                                cwd=self.temp.name, text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
