import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import qa_files
import qa_project as project_api


class ProjectHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()

    def snapshot(self):
        return {str(p.relative_to(self.project)): p.read_bytes() for p in self.project.rglob('*') if p.is_file()}

    def test_editor_creating_context_at_publication_is_never_overwritten(self):
        original = project_api.atomic_text
        context = self.project / project_api.CONTEXT
        def edit(path, text, **kwargs):
            if path == context:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('Reviewed application facts')
            return original(path, text, **kwargs)
        with patch('qa_project.atomic_text', side_effect=edit):
            result = project_api.initialize(self.project)
        self.assertEqual('Reviewed application facts', context.read_text())
        self.assertNotIn(project_api.CONTEXT.as_posix(), result['changed_files'])
        self.assertEqual({'.gitignore', project_api.CONFIG.as_posix(), project_api.CONTEXT.as_posix()}, set(self.snapshot()))

    def test_project_lock_serializes_another_process_without_a_lock_file(self):
        code = ('from pathlib import Path; import sys; sys.path.insert(0,sys.argv[1]); '
                'from qa_project import initialize; print("ready",flush=True); '
                'initialize(Path(sys.argv[2])); print("done",flush=True)')
        with qa_files.project_lock(self.project):
            child = subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'scripts'), str(self.project)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual('ready', child.stdout.readline().strip())
                time.sleep(.1)
                self.assertIsNone(child.poll())
                self.assertEqual({}, self.snapshot())
            except BaseException:
                child.kill(); child.communicate()
                raise
        output, errors = child.communicate(timeout=10)
        self.assertEqual(0, child.returncode, errors)
        self.assertIn('done', output)
        result = project_api.initialize(self.project)
        self.assertEqual([], result['changed_files'])
        self.assertEqual(1, (self.project / '.gitignore').read_text().count('/.qa-agent/project.local.json'))
        self.assertEqual(3, len(self.snapshot()))

    def test_rebind_checks_entire_reviewed_binding_not_only_old_path(self):
        project_api.initialize(self.project)
        path = self.project / project_api.CONFIG
        old = json.loads(path.read_text()); old['package_root'] = str(self.project / 'old')
        path.write_text(json.dumps(old))
        reviewed = dict(old); old['package_version'] = '9.9.9'; path.write_text(json.dumps(old))
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, 'binding changed since review'):
            project_api.initialize(self.project, rebind=True, expected_binding=reviewed)
        self.assertEqual(before, self.snapshot())

    def test_cli_rebind_requires_the_digest_from_the_first_step(self):
        project_api.initialize(self.project)
        path = self.project / project_api.CONFIG
        binding = json.loads(path.read_text()); binding['package_root'] = str(self.project / 'old')
        path.write_text(json.dumps(binding))
        command = [sys.executable, str(ROOT / 'scripts/qa_project.py'), 'init', '--project-root', str(self.project)]
        first = subprocess.run(command, capture_output=True, text=True)
        expected = project_api.binding_digest(binding)
        self.assertEqual(2, first.returncode)
        self.assertIn(expected, first.stderr)
        before = self.snapshot()
        refused = subprocess.run([*command, '--rebind'], capture_output=True, text=True)
        self.assertEqual(2, refused.returncode)
        self.assertEqual(before, self.snapshot())
        allowed = subprocess.run([*command, '--rebind', '--expected-binding-sha256', expected], capture_output=True, text=True)
        self.assertEqual(0, allowed.returncode, allowed.stderr)

    def test_fifo_binding_fails_quickly_in_launcher_before_cli_or_profile_setup(self):
        path = self.project / project_api.CONFIG; path.parent.mkdir()
        os.mkfifo(path)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/qa_start.py'), '--project-root',
                                 str(self.project), '--no-update'], capture_output=True, text=True, timeout=3)
        self.assertEqual(2, result.returncode)
        self.assertIn('regular file', result.stderr)
        self.assertEqual([path], list(path.parent.iterdir()))

    def test_read_binding_rejects_symlinked_parent_and_fifo_without_following(self):
        external = self.project / 'external'; external.mkdir()
        target = external / 'project.local.json'; target.write_text('Do not read')
        (self.project / '.qa-agent').symlink_to(external, target_is_directory=True)
        with self.assertRaises(OSError):
            project_api.read_binding(self.project / project_api.CONFIG, self.project)
        (self.project / '.qa-agent').unlink(); (self.project / '.qa-agent').mkdir()
        os.mkfifo(self.project / project_api.CONFIG)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            project_api.read_binding(self.project / project_api.CONFIG, self.project)

    def test_durable_file_publication_orders_fsync_before_and_after_rename(self):
        calls = []
        fsync, replace = os.fsync, os.replace
        def sync(fd):
            calls.append('file' if stat.S_ISREG(os.fstat(fd).st_mode) else 'directory')
            return fsync(fd)
        def publish(*args, **kwargs):
            self.assertIn('file', calls)
            calls.append('rename')
            return replace(*args, **kwargs)
        with patch('qa_files.os.fsync', side_effect=sync), patch('qa_files.os.replace', side_effect=publish):
            project_api.atomic_write(self.project / 'marker.json', '{}')
        self.assertLess(calls.index('file'), calls.index('rename'))
        self.assertIn('directory', calls[calls.index('rename') + 1:])

    def test_failed_file_fsync_does_not_publish_a_completion_marker(self):
        original = os.fsync
        def fail(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError('injected disk sync failure')
            return original(fd)
        with patch('qa_files.os.fsync', side_effect=fail), self.assertRaises(OSError):
            project_api.atomic_write(self.project / 'marker.json', '{}')
        self.assertEqual({}, self.snapshot())


if __name__ == '__main__':
    unittest.main()
