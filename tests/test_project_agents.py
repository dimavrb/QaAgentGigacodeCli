import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import qa_project_agents as installer
import qa_agent_state as managed
from qa_project import CONFIG, doctor, initialize


class ProjectAgentsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / 'product with пробелами'
        self.project.mkdir()
        initialize(self.project)

    def snapshot(self):
        return {str(path.relative_to(self.project)): path.read_bytes()
                for path in self.project.rglob('*') if path.is_file()}

    def test_preview_is_read_only_and_lists_all_roles(self):
        before = self.snapshot()
        result = installer.install_agents(self.project)
        self.assertEqual('preview', result['status'])
        self.assertEqual(14, len(result['planned_files']))
        self.assertEqual([], result['changed_files'])
        self.assertFalse(result['live_agents_verified'])
        self.assertEqual(before, self.snapshot())

    def test_install_preserves_project_and_is_idempotent(self):
        agents = self.project / installer.AGENTS
        agents.mkdir(parents=True)
        (agents / 'product-agent.md').write_text('Product role')
        (self.project / '.gigacode/settings.json').write_text('{"custom":true}')
        before = self.snapshot()
        result = installer.install_agents(self.project, apply=True)
        self.assertEqual('installed', result['status'])
        self.assertEqual(14, len(result['changed_files']))
        for relative, data in before.items():
            self.assertEqual(data, (self.project / relative).read_bytes())
        for relative in result['changed_files']:
            self.assertEqual((ROOT / relative).read_bytes(), (self.project / relative).read_bytes())
        after = self.snapshot()
        self.assertEqual([], installer.install_agents(self.project, apply=True)['changed_files'])
        self.assertEqual(after, self.snapshot())

    def test_conflict_preserves_adaptation_and_does_not_block_other_roles(self):
        agents = self.project / installer.AGENTS
        agents.mkdir(parents=True)
        (agents / 'qa-test-report-writer.md').write_text('Local adaptation')
        before = self.snapshot()
        result = installer.install_agents(self.project)
        self.assertEqual('needs_attention', result['status'])
        self.assertEqual(['.gigacode/agents/qa-test-report-writer.md'], result['conflicts'])
        result = installer.install_agents(self.project, apply=True)
        self.assertEqual('needs_attention', result['status'])
        self.assertEqual(13, len(result['changed_files']))
        for relative, data in before.items():
            self.assertEqual(data, (self.project / relative).read_bytes())

    def test_binding_must_match_the_package(self):
        binding = json.loads((self.project / CONFIG).read_text())
        for field, value in [('package_root', str(self.root / 'other-package')), ('package_version', '0.0.1')]:
            with self.subTest(field=field):
                (self.project / CONFIG).write_text(json.dumps({**binding, field: value}))
                with self.assertRaisesRegex(ValueError, 'binding differs'):
                    installer.install_agents(self.project, apply=True)
                self.assertFalse((self.project / '.gigacode').exists())

    def test_doctor_accepts_adaptation_while_vendor_baseline_is_unchanged(self):
        installer.install_agents(self.project, apply=True)
        target = self.project / installer.AGENTS / 'qa-analyst.md'
        target.write_text(target.read_text() + '\nLocal adaptation\n')
        before = self.snapshot()
        with patch('qa_project.shutil.which', return_value=None):
            result = doctor(self.project)
        check = next(item for item in result['checks'] if item['name'] == 'project_agent_copies')
        self.assertEqual('passed', check['status'])
        self.assertIn('qa-analyst.md', check['detail'])
        self.assertFalse(result['live_extension_verified'])
        self.assertEqual(before, self.snapshot())

    def test_unbound_project_is_not_modified(self):
        other = self.root / 'unbound'
        other.mkdir()
        with self.assertRaises(FileNotFoundError):
            installer.install_agents(other, apply=True)
        self.assertEqual([], list(other.iterdir()))

    def test_symlinked_agent_directory_or_file_never_writes_outside_project(self):
        external = self.root / 'external'
        external.mkdir()
        for relative in (Path('.gigacode'), installer.AGENTS):
            with self.subTest(relative=relative):
                target = self.project / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(external, target_is_directory=True)
                with self.assertRaises(OSError):
                    installer.install_agents(self.project, apply=True)
                self.assertEqual([], list(external.iterdir()))
                target.unlink()
        agents = self.project / installer.AGENTS
        agents.mkdir(parents=True)
        target = agents / 'qa-analyst.md'
        target.symlink_to(external / 'missing-file')
        with self.assertRaises(OSError):
            installer.install_agents(self.project, apply=True)
        self.assertEqual([], list(external.iterdir()))

    def test_concurrent_creation_is_preserved(self):
        original = managed.publish_role
        def publish(project, op):
            if op['name'] == 'qa-analyst.md':
                path = project / installer.AGENTS / op['name']
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('Concurrent role')
            return original(project, op)
        with patch.object(managed, 'publish_role', side_effect=publish):
            result = installer.install_agents(self.project, apply=True)
        self.assertEqual('needs_attention', result['status'])
        self.assertEqual(13, len(result['changed_files']))
        self.assertEqual('Concurrent role', (self.project / installer.AGENTS / 'qa-analyst.md').read_text())

    def test_cli_uses_explicit_project_from_foreign_directory(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/qa_project_agents.py'),
                                 '--project-root', str(self.project), '--apply', '--state-dir', str(self.root / 'profile')],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(14, len(json.loads(result.stdout)['changed_files']))
        self.assertFalse((self.root / '.gigacode').exists())

    def test_cli_refuses_active_assignments_before_first_install(self):
        before = self.snapshot()
        with patch.object(sys, 'argv', ['qa_project_agents.py', '--project-root', str(self.project), '--apply',
                                      '--state-dir', str(self.root / 'profile')]), \
                patch('qa_auto_update.active_projects', return_value=True), \
                patch('qa_auto_update.other_cli_running') as processes:
            self.assertEqual(2, installer.main())
        processes.assert_not_called()
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
