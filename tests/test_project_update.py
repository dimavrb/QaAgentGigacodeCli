"""Project update, offline fallback, gates and a real launcher re-exec."""
from argparse import Namespace
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import qa_install
import qa_project_update as updater


class ProjectUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.info, cls.files = qa_install.source_files(ROOT)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / 'product with пробелами'; self.project.mkdir()
        with patch('qa_install.ensure_idle'):
            qa_install.install(self.project, ROOT, apply=True)
        self.package = self.project / qa_install.PACKAGE
        self.current = qa_install.binding(self.project)
        self.next_version = '.'.join(map(str, [*map(int, self.info['package_version'].split('.')[:2]),
                                               int(self.info['package_version'].split('.')[2]) + 1]))
        stack = ExitStack(); self.addCleanup(stack.close)
        stack.enter_context(patch('qa_project_update.active_projects', return_value=False))
        stack.enter_context(patch('qa_project_update.other_cli_running', return_value=False))
        self.ci = stack.enter_context(patch('qa_project_update.ci_passed', return_value=True))

    def origin(self, *, version=None, extra=None, reload_marker=False):
        source = self.base / 'origin'; source.mkdir()
        payload = dict(self.files)
        release = version or self.next_version
        payload['VERSION'] = (release + '\n').encode()
        manifest = json.loads(payload['gigacode-extension.json']); manifest['version'] = release
        payload['gigacode-extension.json'] = qa_install.encoded(manifest)
        if reload_marker:
            payload['scripts/qa_start.py'] = payload['scripts/qa_start.py'].replace(
                b'import sys\n', b'import sys\nos.environ["QA_FIXTURE_RELOADED"]="1"\n')
        payload.update(extra or {})
        for name, data in payload.items():
            target = source / name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
        for args in (['init', '-b', 'main'], ['add', '.'],
                     ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                      '-c', 'commit.gpgsign=false', 'commit', '-m', 'Synthetic release']):
            result = subprocess.run(['git', '-C', str(source), *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
        return source

    def update(self, source=None):
        with qa_install.session_lock(self.project, exclusive=True) as descriptor:
            with patch('qa_project_update.REMOTE', (source or self.base / 'missing').as_uri()):
                return updater.update_project(self.project, self.current, descriptor)

    def arguments(self, **kwargs):
        return Namespace(cli_args=[], project_session_lock_fd=None, project_resume_version=None,
                         resume_after_update=None, session_lock_fd=None, update_only=False,
                         no_update=kwargs.get('no_update', False))

    def test_new_release_updates_all_files_and_keeps_product_context(self):
        source = self.origin(extra={'docs/new-guide.md': b'New guide'})
        context = self.project / qa_install.CONTEXT; context.write_text('Application context')
        result = self.update(source)
        self.assertEqual('updated', result['status'])
        self.assertEqual(self.next_version, qa_install.metadata(self.project)['package_version'])
        self.assertEqual(b'New guide', (self.package / 'docs/new-guide.md').read_bytes())
        self.assertEqual('Application context', context.read_text())
        self.assertEqual([], list((self.project / qa_install.BOOTSTRAP).glob('update.*')))

    def test_same_or_older_release_is_not_installed(self):
        source = self.origin(version=self.info['package_version'])
        self.assertEqual('current', self.update(source)['status'])
        self.ci.assert_not_called()

    def test_offline_launches_verified_installed_copy(self):
        result = self.update()
        self.assertEqual('offline', result['status'])
        self.assertFalse(result['changed'])
        with patch('qa_project_update.update_project', return_value=result), patch(
                'qa_project_update.shutil.which', return_value='/fake/gigacode'), patch(
                'qa_project_update.subprocess.call', return_value=0) as child:
            self.assertEqual(0, updater.start_project(self.project, self.package, self.arguments()))
        child.assert_called_once_with(['/fake/gigacode'], cwd=self.project)

    def test_unverified_ci_never_imports_or_installs_candidate(self):
        source = self.origin(); self.ci.return_value = False
        with patch('qa_project_update.validate') as validate, patch('qa_project_update.probe') as probe:
            self.assertEqual('unverified_release', self.update(source)['status'])
        validate.assert_not_called(); probe.assert_not_called()
        self.assertEqual(self.info['package_version'], qa_install.metadata(self.project)['package_version'])

    def test_major_upgrade_requires_manual_installation(self):
        source = self.origin(version='99.0.0')
        self.assertEqual('manual_major_update', self.update(source)['status'])
        self.ci.assert_not_called()
        self.assertEqual(self.info['package_version'], qa_install.metadata(self.project)['package_version'])

    def test_unfinished_work_defers_update_without_fetching(self):
        with patch('qa_project_update.active_projects', return_value=True), patch('qa_project_update.run') as run:
            self.assertEqual('deferred_active_work', self.update()['status'])
        run.assert_not_called()

    def test_active_session_defers_update_and_second_session_can_start(self):
        with qa_install.session_lock(self.project), patch('qa_project_update.update_project') as update, patch(
                'qa_project_update.shutil.which', return_value='/fake/gigacode'), patch(
                'qa_project_update.subprocess.call', return_value=0):
            self.assertEqual(0, updater.start_project(self.project, self.package, self.arguments()))
        update.assert_not_called()

    def test_conflict_keeps_old_release_and_independent_installer(self):
        (self.package / 'docs/new-guide.md').write_text('Local file')
        source = self.origin(extra={'docs/new-guide.md': b'New vendor guide'})
        with self.assertRaisesRegex(ValueError, 'QA update conflicts'):
            self.update(source)
        self.assertEqual(self.info['package_version'], qa_install.metadata(self.project)['package_version'])
        self.assertEqual('Local file', (self.package / 'docs/new-guide.md').read_text())
        stages = list((self.project / qa_install.BOOTSTRAP).glob('update.*'))
        self.assertEqual(1, len(stages))
        self.assertTrue((stages[0] / 'source/scripts/qa_install.py').is_file())

    def test_no_update_does_not_use_network(self):
        with patch('qa_project_update.update_project') as update, patch(
                'qa_project_update.shutil.which', return_value='/fake/gigacode'), patch(
                'qa_project_update.subprocess.call', return_value=0):
            self.assertEqual(0, updater.start_project(self.project, self.package, self.arguments(no_update=True)))
        update.assert_not_called()

    def test_inherited_gate_cannot_belong_to_another_project(self):
        other = self.base / 'other'; other.mkdir()
        with qa_install.session_lock(other, exclusive=True) as descriptor:
            duplicate = os.dup(descriptor)
            try:
                with self.assertRaisesRegex(ValueError, 'another project'):
                    with qa_install.session_lock(self.project, exclusive=True, descriptor=duplicate):
                        pass
            finally:
                os.close(duplicate)

    def test_actual_launcher_reexecs_new_code_before_starting_cli(self):
        source = self.origin(reload_marker=True)
        binaries = self.base / 'bin'; binaries.mkdir()
        cli = binaries / 'gigacode'
        cli.write_text('#!' + sys.executable + '''
import json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'.qa-agent/package/scripts'))
from qa_install import session_lock
blocked=False
try:
 with session_lock(Path.cwd(),exclusive=True): pass
except ValueError:
 blocked=True
print(json.dumps(dict(reloaded=os.environ.get('QA_FIXTURE_RELOADED')=='1',locked=blocked,args=sys.argv[1:])))
''')
        cli.chmod(0o700)
        program = '''import sys
sys.path.insert(0,sys.argv[1])
import qa_start,qa_project_update
qa_project_update.REMOTE=sys.argv[3]
qa_project_update.ci_passed=lambda commit: True
sys.argv=['qa_start.py','--project-root',sys.argv[2],'--','--fixture-option']
raise SystemExit(qa_start.main())
'''
        result = subprocess.run([sys.executable, '-B', '-c', program, str(self.package / 'scripts'),
                                 str(self.project), source.as_uri()], cwd=self.project,
                                env={**os.environ, 'PATH': str(binaries) + os.pathsep + os.environ['PATH']},
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(dict(reloaded=True, locked=True, args=['--fixture-option']), json.loads(result.stdout))
        self.assertIn('"status": "updated"', result.stderr)
        self.assertIn('"status": "reloaded"', result.stderr)
        self.assertEqual(self.next_version, qa_install.metadata(self.project)['package_version'])


if __name__ == '__main__':
    unittest.main()
