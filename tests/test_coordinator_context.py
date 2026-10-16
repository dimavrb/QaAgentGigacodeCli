import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import task_exchange as exchange
from read_qa_instructions import read_sections, role_metadata


class HandoffOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / 'task.json'
        self.output = self.root / 'handoff.json'
        criteria = dict(schema_version=1, criteria=[dict(criterion_id='C-1',
                        description='Покрыть требование', source='requirements.md', required=True)])
        exchange.init_task(self.state, 'QA-1', 'Проверка', criteria)
        (self.root / 'requirements.md').write_text('Первичный источник', encoding='utf-8')
        assignment = dict(task_id='QA-1', request_id='plan', agent='qa-test-plan-writer',
                          stage='planning', goal='Полное назначение. ' * 1000,
                          acceptance_criteria=['Карта и кейсы проверены'], criterion_ids=['C-1'],
                          stop_condition='Передать план или блокер', expected_artifact='План и каталог',
                          result_path='result.json', inputs=['requirements.md'])
        exchange.assign(self.state, assignment, self.root, 1)
        self.before = self.state.read_bytes()

    def prepare(self, output=None, revision=2):
        return exchange.prepare_handoff(self.state, 'plan', revision,
                                        output_path=self.output if output is None else output)

    def test_cli_receipt_is_small_and_file_preserves_full_handoff_and_fencing(self):
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/manage_task.py'),
                              '--state', str(self.state), 'prepare', '--request-id', 'plan',
                              '--expected-revision', '2', '--output', str(self.output)],
                             capture_output=True, text=True, cwd=self.root, timeout=10)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        receipt = json.loads(run.stdout)
        raw = self.output.read_bytes()
        full = json.loads(raw)
        handoff = full['result']
        self.assertEqual(3, full['revision'])
        self.assertEqual(3, receipt['revision'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt['result']['handoff_sha256'])
        self.assertEqual(str(self.output), receipt['result']['handoff_path'])
        self.assertEqual(exchange.read_state(self.state)['requests']['plan']['assignment'], handoff['assignment'])
        self.assertEqual('Покрыть требование', handoff['task_criteria']['C-1']['description'])
        self.assertEqual(['Карта и кейсы проверены'], handoff['assignment']['acceptance_criteria'])
        self.assertIn('trace_context', handoff)
        self.assertNotIn('delivery_id', receipt['result'])
        self.assertNotIn('Полное назначение', run.stdout)
        self.assertLess(len(run.stdout.encode()), len(raw) // 10)
        self.assertEqual(0o600, self.output.stat().st_mode & 0o777)
        with self.assertRaises(ValueError):
            exchange.start_request(self.state, 'plan', 'owner', 'wrong', 3)
        exchange.start_request(self.state, 'plan', 'owner', handoff['delivery_id'], 3)

    def test_default_api_still_returns_full_handoff(self):
        result = exchange.prepare_handoff(self.state, 'plan', 2)
        self.assertIn('assignment', result['result'])
        self.assertFalse(self.output.exists())

    def test_existing_file_and_reserved_paths_do_not_consume_delivery(self):
        self.output.write_text('Сохранить', encoding='utf-8')
        for output in (self.output, self.state, self.root / 'result.json', self.root / 'requirements.md'):
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.prepare(output)
            self.assertEqual(self.before, self.state.read_bytes())
        self.assertEqual('Сохранить', self.output.read_text(encoding='utf-8'))
        self.assertFalse((self.root / 'result.json').exists())

    def test_stale_revision_and_changed_input_do_not_publish(self):
        with self.assertRaises(ValueError):
            self.prepare(revision=1)
        (self.root / 'requirements.md').write_text('Изменённый источник', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.before, self.state.read_bytes())
        self.assertFalse(self.output.exists())

    def test_output_publication_failure_leaves_registry_queued(self):
        with patch('qa_files.atomic_bytes', side_effect=OSError('write failed')):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(self.before, self.state.read_bytes())
        self.assertFalse(self.output.exists())

    def test_uncommitted_file_is_not_authorization_to_start_or_repeat_prepare(self):
        with patch.object(exchange, 'atomic_write', side_effect=OSError('registry write failed')):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertTrue(self.output.is_file())
        self.assertEqual(self.before, self.state.read_bytes())
        handoff = json.loads(self.output.read_bytes())['result']
        with self.assertRaises(ValueError):
            exchange.start_request(self.state, 'plan', 'owner', handoff['delivery_id'], 2)
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.before, self.state.read_bytes())

    def test_output_symlink_and_symlinked_directory_are_rejected(self):
        self.output.symlink_to(self.root / 'elsewhere.json')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse((self.root / 'elsewhere.json').exists())
        self.output.unlink()
        (self.root / 'link').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.prepare(self.root / 'link/handoff.json')
        self.assertEqual(self.before, self.state.read_bytes())


class InstructionSelectionTests(unittest.TestCase):
    def test_sections_keep_preamble_and_only_requested_subject(self):
        full = (ROOT / 'docs/TASK_STORAGE.md').read_text(encoding='utf-8')
        selected = read_sections(ROOT, 'docs/TASK_STORAGE.md', ['Найти и продолжить работу'])
        self.assertIn('Храни материалы приложения', selected)
        self.assertIn('resume_ready=false', selected)
        self.assertNotIn('## Создать задачу локальным CLI', selected)
        self.assertLess(len(selected), len(full))
        self.assertIn('Найти и продолжить работу', read_sections(ROOT, 'docs/TASK_STORAGE.md'))

    def test_missing_duplicate_headings_and_external_documents_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'docs').mkdir()
            document = root / 'docs/guide.md'
            document.write_text('# Guide\n## One\nText\n## One\nOther\n')
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                read_sections(root, 'docs/guide.md', ['One'])
            document.write_text('# Guide\n## One\nText\n')
            with self.assertRaisesRegex(ValueError, 'missing'):
                read_sections(root, 'docs/guide.md', ['Missing'])
            for name in ('../outside.md', 'VERSION', str(ROOT / 'README.md')):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    read_sections(root, name, ['One'])

    def test_role_metadata_has_tools_without_body(self):
        metadata = role_metadata(ROOT, 'qa-test-plan-writer')
        self.assertIn('tools:', metadata)
        self.assertIn('read_file', metadata)
        full = (ROOT / '.gigacode/agents/qa-test-plan-writer.md').read_text(encoding='utf-8')
        self.assertTrue(full.startswith(metadata))
        self.assertLess(len(metadata), len(full) // 4)
        with self.assertRaises(ValueError):
            role_metadata(ROOT, '../qa-test-plan-writer')

    def test_cli_works_from_product_cwd_and_unknown_section_is_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            argv = [sys.executable, str(ROOT / 'scripts/read_qa_instructions.py'), 'document',
                    '--path', 'docs/TASK_ACCEPTANCE.md', '--section']
            run = subprocess.run([*argv, 'Согласованные критерии'], cwd=temporary,
                                 capture_output=True, text=True, timeout=10)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertIn('criterion_ids', run.stdout)
            run = subprocess.run([*argv, 'Нет раздела'], cwd=temporary,
                                 capture_output=True, text=True, timeout=10)
            self.assertEqual(2, run.returncode)
            self.assertEqual('', run.stdout)


if __name__ == '__main__':
    unittest.main()
