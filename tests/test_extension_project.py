import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from qa_project import CONFIG, CONTEXT, doctor, initialize, package_info


class ExtensionProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.package = self.root / 'extension with пробелами'
        self.package.mkdir()
        manifest = json.loads((ROOT / 'gigacode-extension.json').read_text())
        for name in ('gigacode-extension.json', 'VERSION', 'GIGACODE_EXTENSION.md',
                     'docs/COORDINATOR.md', 'docs/EXTENSION_RUNTIME.md',
                     'scripts/qa_project.py', 'scripts/qa_install.py', 'scripts/qa_tasks.py', 'scripts/qa_files.py',
                     'scripts/qa_agent_state.py', 'scripts/qa_project_agents.py',
                     'scripts/extension_resources.py',
                     'examples/qa-only/APP_CONTEXT.template.md'):
            target = self.package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        for key in ('agents', 'skills', 'commands'):
            shutil.copytree(ROOT / manifest[key], self.package / manifest[key])
            shutil.copytree(ROOT / key, self.package / key)
        shutil.copytree(ROOT / 'scripts', self.package / 'scripts', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
        self.project = self.root / 'application with пробелами'
        self.project.mkdir()

    def snapshot(self, path):
        return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file()}

    def test_manifest_and_resources_are_consistent(self):
        info = package_info(self.package)
        self.assertEqual(14, info['agent_count'])
        self.assertEqual(6, info['skill_count'])
        self.assertEqual({'qa', 'qa:init', 'qa:doctor', 'qa:tasks', 'qa:resume'}, set(info['commands']))
        self.assertEqual(str(self.package), info['package_root'])
        self.assertEqual((ROOT / 'VERSION').read_text().strip(), info['package_version'])

    def test_init_preserves_product_files_and_is_idempotent(self):
        (self.project / 'GIGACODE.md').write_text('Project instructions\n')
        (self.project / '.gitignore').write_text('/node_modules/')
        (self.project / '.gigacode').mkdir()
        (self.project / '.gigacode/settings.json').write_text('{"custom":true}')
        (self.project / 'manual-cases.json').write_text('Existing product cases')
        before = self.snapshot(self.package)
        first = initialize(self.project, self.package)
        self.assertEqual('configured', first['status'])
        self.assertFalse(first['external_systems_configured'])
        self.assertEqual('Project instructions\n', (self.project / 'GIGACODE.md').read_text())
        self.assertEqual('{"custom":true}', (self.project / '.gigacode/settings.json').read_text())
        self.assertEqual('Existing product cases', (self.project / 'manual-cases.json').read_text())
        self.assertTrue((self.project / '.gitignore').read_text().startswith('/node_modules/\n'))
        self.assertNotIn('\n/artifacts/\n', (self.project / '.gitignore').read_text())
        (self.project / CONTEXT).write_text('Local app context')
        project_snapshot = self.snapshot(self.project)
        second = initialize(self.project, self.package)
        self.assertEqual([], second['changed_files'])
        self.assertEqual(project_snapshot, self.snapshot(self.project))
        self.assertEqual(before, self.snapshot(self.package))

    def test_two_projects_keep_separate_state_and_context(self):
        other = self.root / 'second-app'
        other.mkdir()
        initialize(self.project, self.package)
        initialize(other, self.package)
        (self.project / CONTEXT).write_text('First application')
        self.assertNotEqual((self.project / CONTEXT).read_bytes(), (other / CONTEXT).read_bytes())
        self.assertEqual(str(other), json.loads((other / CONFIG).read_text())['project_root'])

    def test_upgrade_refreshes_binding_without_changing_context_or_evidence(self):
        initialize(self.project, self.package)
        (self.project / CONTEXT).write_text('Reviewed product context')
        evidence = self.project / '.qa-agent/artifacts/result.json'
        evidence.parent.mkdir(parents=True)
        evidence.write_text('Historical evidence')
        manifest = json.loads((self.package / 'gigacode-extension.json').read_text())
        manifest['version'] = '9.9.9'
        (self.package / 'gigacode-extension.json').write_text(json.dumps(manifest))
        (self.package / 'VERSION').write_text('9.9.9\n')
        with patch('qa_project.shutil.which', return_value=None):
            result = doctor(self.project, self.package)
        binding = next(check for check in result['checks'] if check['name'] == 'binding')
        self.assertEqual('failed', binding['status'])
        upgraded = initialize(self.project, self.package)
        self.assertEqual([CONFIG.as_posix()], upgraded['changed_files'])
        self.assertEqual('9.9.9', json.loads((self.project / CONFIG).read_text())['package_version'])
        self.assertEqual('Reviewed product context', (self.project / CONTEXT).read_text())
        self.assertEqual('Historical evidence', evidence.read_text())

    def test_moved_package_requires_explicit_rebind(self):
        initialize(self.project, self.package)
        reviewed = json.loads((self.project / CONFIG).read_text())
        moved = self.root / 'moved-package'
        self.package.rename(moved)
        before = self.snapshot(self.project)
        with self.assertRaisesRegex(ValueError, 'use --rebind'):
            initialize(self.project, moved)
        self.assertEqual(before, self.snapshot(self.project))
        initialize(self.project, moved, rebind=True, expected_binding=reviewed)
        self.assertEqual(str(moved), json.loads((self.project / CONFIG).read_text())['package_root'])

    def test_doctor_is_read_only_and_never_claims_live_compatibility(self):
        initialize(self.project, self.package)
        before = self.snapshot(self.project)
        with patch('qa_project.shutil.which', return_value='/fake/gigacode'), patch(
                'qa_project.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'fixture-cli 1\n', '')):
            result = doctor(self.project, self.package)
        self.assertEqual('ready_for_cli_check', result['status'])
        self.assertFalse(result['live_extension_verified'])
        self.assertFalse(result['external_systems_configured'])
        self.assertEqual(before, self.snapshot(self.project))

    def test_doctor_missing_cli_and_binding_remain_failed(self):
        with patch('qa_project.shutil.which', return_value=None):
            result = doctor(self.project, self.package)
        self.assertEqual('needs_attention', result['status'])
        failures = {check['name'] for check in result['checks'] if check['status'] == 'failed'}
        self.assertTrue({'binding', 'context', 'ignore', 'gigacode'} <= failures)
        self.assertEqual({}, self.snapshot(self.project))

    def test_init_cannot_write_into_installed_package(self):
        with self.assertRaisesRegex(ValueError, 'outside the installed package'):
            initialize(self.package, self.package)

    def test_symlinked_targets_are_rejected_before_any_write(self):
        external = self.root / 'external'
        external.write_text('Do not change')
        for relative in (CONFIG, CONTEXT, Path('.gitignore')):
            with self.subTest(relative=relative):
                target = self.project / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(external)
                with self.assertRaisesRegex(ValueError, 'symlinked'):
                    initialize(self.project, self.package)
                self.assertEqual('Do not change', external.read_text())
                self.assertFalse((self.project / '.gitignore').is_file() and relative != Path('.gitignore'))
                target.unlink()

    def test_corrupt_binding_is_not_overwritten(self):
        initialize(self.project, self.package)
        text = (self.project / CONFIG).read_text()
        (self.project / CONFIG).write_text(text.replace('"schema_version": 1', '"schema_version": 0, "schema_version": 1'))
        before = self.snapshot(self.project)
        with self.assertRaisesRegex(ValueError, 'duplicate JSON key'):
            initialize(self.project, self.package)
        self.assertEqual(before, self.snapshot(self.project))

    def test_invalid_config_directory_fails_before_modifying_gitignore(self):
        (self.project / '.qa-agent').write_text('Unrelated application file')
        before = self.snapshot(self.project)
        with self.assertRaisesRegex(ValueError, 'must be a directory'):
            initialize(self.project, self.package)
        self.assertEqual(before, self.snapshot(self.project))

    def test_manifest_version_mismatch_is_rejected_without_project_changes(self):
        (self.package / 'VERSION').write_text('9.9.9\n')
        with self.assertRaisesRegex(ValueError, 'same QA package version'):
            initialize(self.project, self.package)
        self.assertEqual({}, self.snapshot(self.project))

    def test_cli_works_from_foreign_working_directory(self):
        command = [sys.executable, str(self.package / 'scripts/qa_project.py'), 'init',
                   '--project-root', str(self.project)]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(self.package), json.loads(result.stdout)['package_root'])
        self.assertFalse((self.root / CONFIG).exists())



if __name__ == '__main__':
    unittest.main()
