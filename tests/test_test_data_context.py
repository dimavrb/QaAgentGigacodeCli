import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/read_test_data_guide.py'
spec = importlib.util.spec_from_file_location('qa_data_context', SCRIPT)
guide = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guide)


class TestDataContextTests(unittest.TestCase):
    def test_roles_receive_shared_checklist_and_only_their_initial_sections(self):
        checklist = (ROOT / 'docs/TEST_DATA_CHECKLIST.md').read_text().strip()
        for role, keys in guide.ROLE_SECTIONS.items():
            with self.subTest(role=role):
                output = guide.build_context(ROOT, role)
                self.assertEqual(1, output.count(checklist))
                for key, heading in guide.SECTIONS.items():
                    self.assertEqual(key in keys, ('## ' + heading + '\n') in output)
                self.assertNotIn('## Пример и источники', output)
                self.assertNotIn('| Роль |', output)

    def test_additional_reads_do_not_repeat_checklist_or_initial_sections(self):
        output = guide.build_context(ROOT, 'qa-test-automation', ['cleanup', 'isolation', 'cleanup'], True)
        self.assertNotIn('# Обязательный минимум', output)
        self.assertNotIn('## Рецепт и фикстура', output)
        self.assertEqual(1, output.count('## Готовность и очистка'))
        self.assertEqual(1, output.count('## Изоляция, повтор и время'))
        self.assertNotIn('## Компактный профиль', output)

    def test_cli_uses_package_paths_from_an_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(SCRIPT), '--role', 'qa-autotest-reviewer'],
                                    cwd=directory, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(guide.build_context(ROOT, 'qa-autotest-reviewer'), result.stdout)

    def test_unknown_or_empty_additional_selection_fails_without_context_output(self):
        for args in (['--role', 'qa-test-automation', '--only-sections'],
                     ['--role', 'qa-test-automation', '--section', 'nonexistent'],
                     ['--role', 'nonexistent']):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
                self.assertEqual(2, result.returncode)
                self.assertEqual('', result.stdout)

    def test_missing_empty_or_duplicate_headings_fail_instead_of_silent_omission(self):
        original = (ROOT / 'docs/TEST_DATA.md').read_text()
        for defect in ('missing', 'empty', 'duplicate'):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'docs').mkdir()
                (root / 'docs/TEST_DATA_CHECKLIST.md').write_bytes((ROOT / 'docs/TEST_DATA_CHECKLIST.md').read_bytes())
                heading = '## Рецепт и фикстура'
                if defect == 'missing':
                    damaged = original.replace(heading, '## Неверный заголовок')
                elif defect == 'empty':
                    start = original.index(heading) + len(heading)
                    end = original.index('\n## ', start)
                    damaged = original[:start] + '\n' + original[end:]
                else:
                    damaged = original + '\n' + heading + '\nПовтор\n'
                (root / 'docs/TEST_DATA.md').write_text(damaged)
                with self.assertRaises(ValueError):
                    guide.build_context(root, 'qa-test-automation')


if __name__ == '__main__':
    unittest.main()
