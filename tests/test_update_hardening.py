import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import qa_auto_update as updater
import qa_recover
import qa_release
import test_auto_update as fixtures


class UpdateHardeningTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AutoUpdateTests('test_up_to_date_never_calls_extension_update')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_missing_launcher_dependency_is_rejected_before_fast_forward(self):
        c = self.case
        (c.author / 'scripts/package_agent.py').unlink()
        c.publish()
        with self.assertRaisesRegex(updater.UpdateError, 'preflight failed'):
            c.apply()
        self.assertEqual(c.base, c.git(c.source, 'rev-parse', 'HEAD'))
        self.assertIsNone(updater.load_state(c.state)['pending'])
        self.assertEqual([], c.calls)
        self.assertTrue(updater.payload_matches(c.source, c.installed))

    def test_syntax_and_import_failure_are_rejected_without_changing_installation(self):
        c = self.case
        (c.author / 'scripts/qa_start.py').write_text('def broken(:\n')
        c.publish()
        with self.assertRaisesRegex(updater.UpdateError, 'preflight failed'):
            c.apply()
        self.assertEqual(c.base, c.git(c.source, 'rev-parse', 'HEAD'))
        self.assertTrue(updater.payload_matches(c.source, c.installed))

    def test_runtime_import_failure_is_not_confused_with_a_valid_python_file(self):
        c = self.case
        (c.author / 'scripts/qa_start.py').write_text('raise RuntimeError("fixture import failure")\n')
        c.publish()
        with self.assertRaisesRegex(updater.UpdateError, 'probe failed'):
            c.apply()
        self.assertEqual(c.base, c.git(c.source, 'rev-parse', 'HEAD'))
        self.assertIsNone(updater.load_state(c.state)['pending'])

    def helper(self, command='rollback', env=None, process_rows=''):
        c = self.case
        # Exercise the real zipapp with a deterministic host-process observation.
        # Other tests/CLI sessions on the machine must not select the wrong gate.
        program = (
            'import runpy, subprocess, sys\n'
            'original_run = subprocess.run\n'
            'def run(arguments, *args, **kwargs):\n'
            "    if arguments == ['ps', '-axo', 'pid=,args=']:\n"
            f"        return subprocess.CompletedProcess(arguments, 0, {process_rows!r}, '')\n"
            '    return original_run(arguments, *args, **kwargs)\n'
            'subprocess.run = run\n'
            'archive = sys.argv.pop(1)\n'
            "runpy.run_path(archive, run_name='__main__')\n"
        )
        return subprocess.run([sys.executable, '-I', '-c', program, str(c.state / 'qa-recover.pyz'),
                               '--state-dir', str(c.state), command], cwd=c.source, env=env,
                              capture_output=True, text=True, timeout=20)

    def test_recovery_helper_refuses_observed_active_cli_without_changing_files(self):
        c = self.case; target = c.publish(); c.apply()
        def snapshot(root):
            return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*')
                    if path.is_file() and '.git' not in path.relative_to(root).parts}
        source_before = snapshot(c.source)
        installed_before = snapshot(c.installed)
        refused = self.helper(process_rows='4242 /fixture/bin/gigacode --mode qa\n')
        self.assertEqual(2, refused.returncode)
        self.assertIn('close active GIGACODE work', refused.stderr)
        self.assertEqual(target, c.git(c.source, 'rev-parse', 'HEAD'))
        self.assertEqual(source_before, snapshot(c.source))
        self.assertEqual(installed_before, snapshot(c.installed))

    def test_independent_helper_works_when_the_mutable_launcher_cannot_import(self):
        c = self.case
        script = c.author / 'scripts/qa_start.py'
        script.write_text(script.read_text().replace('def main():',
            'if os.environ.get("QA_FIXTURE_IMPORT_FAILURE"):\n    raise RuntimeError("fixture startup failure")\n\ndef main():'))
        c.publish(); c.apply()
        environment = dict(os.environ, QA_FIXTURE_IMPORT_FAILURE='1')
        broken = subprocess.run([sys.executable, str(c.source / 'scripts/qa_start.py'), '--help'],
                                capture_output=True, text=True, env=environment, timeout=5)
        self.assertNotEqual(0, broken.returncode)
        result = self.helper(env=environment)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('rolled_back', json.loads(result.stdout)['status'])
        self.assertEqual(c.base, c.git(c.source, 'rev-parse', 'HEAD'))
        self.assertTrue(updater.payload_matches(c.source, c.installed))
        c.assert_project_preserved()
        with self.assertRaisesRegex(updater.UpdateError, 'rolled back'):
            c.apply()

    def test_partial_activation_can_be_rolled_back_without_running_package_code(self):
        c = self.case; c.publish()
        original = c.native_cli
        def partial(arguments, **kwargs):
            if arguments == ['fake-gigacode', 'extensions', 'update', 'qa-agent']:
                (c.installed / 'VERSION').write_text(c.next_version + '\n')
                return subprocess.CompletedProcess(arguments, 1, '', 'partial fixture')
            return original(arguments, **kwargs)
        with patch.object(c, 'native_cli', side_effect=partial), self.assertRaises(updater.UpdateError):
            c.apply()
        self.assertIsNotNone(updater.load_state(c.state)['pending'])
        result = self.helper()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(updater.payload_matches(c.source, c.installed))
        self.assertIsNone(updater.load_state(c.state)['pending'])
        c.assert_project_preserved()

    def test_backup_corruption_and_unrelated_installed_edits_refuse_rollback(self):
        c = self.case; target = c.publish(); c.apply()
        path = c.installed / 'README.md'; path.write_text('Unrelated user adaptation')
        refused = self.helper()
        self.assertEqual(2, refused.returncode)
        self.assertIn('unrelated local edits', refused.stderr)
        self.assertEqual('Unrelated user adaptation', path.read_text())
        self.assertEqual(target, c.git(c.source, 'rev-parse', 'HEAD'))
        path.write_bytes((c.source / 'README.md').read_bytes())
        journal = qa_recover.load(c.state)
        backup = c.state / journal['snapshot']
        with backup.open('ab') as stream:
            stream.write(b'corrupt snapshot')
        refused = self.helper()
        self.assertEqual(2, refused.returncode)
        self.assertIn('snapshot checksum', refused.stderr)
        self.assertEqual(target, c.git(c.source, 'rev-parse', 'HEAD'))

    def test_dirty_source_and_active_session_prevent_rollback(self):
        c = self.case; target = c.publish(); c.apply()
        path = c.source / 'README.md'; expected = path.read_bytes(); path.write_text('Unrelated source edit')
        refused = self.helper()
        self.assertEqual(2, refused.returncode)
        self.assertIn('unrelated changes', refused.stderr)
        self.assertEqual(target, c.git(c.source, 'rev-parse', 'HEAD'))
        path.write_bytes(expected)
        with updater.SessionLock(c.state) as lock:
            lock.share()
            refused = self.helper()
            self.assertEqual(2, refused.returncode)
        self.assertEqual(target, c.git(c.source, 'rev-parse', 'HEAD'))

    def test_crash_between_directory_renames_can_resume_independently(self):
        c = self.case; c.publish(); c.apply()
        original = os.rename
        def crash(source, destination, **kwargs):
            if str(source).startswith('.qa-restore-'):
                raise OSError('injected power-loss boundary')
            return original(source, destination, **kwargs)
        with patch('qa_recover.os.rename', side_effect=crash), self.assertRaises(OSError):
            qa_recover.rollback(c.state)
        self.assertFalse(c.installed.exists())
        self.assertEqual('restoring', qa_recover.load(c.state)['phase'])
        result = self.helper()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(updater.payload_matches(c.source, c.installed))

    def test_recovery_intent_survives_crash_before_pending_marker(self):
        c = self.case; c.publish()
        original = updater.save_state
        def crash(directory, state):
            if state['pending']:
                raise OSError('injected pending marker failure')
            return original(directory, state)
        with patch('qa_auto_update.save_state', side_effect=crash), self.assertRaises(OSError):
            c.apply()
        self.assertIsNone(updater.load_state(c.state)['pending'])
        self.assertEqual('prepared', qa_recover.load(c.state)['phase'])
        self.assertEqual(c.base, c.git(c.source, 'rev-parse', 'HEAD'))
        self.assertEqual('updated', c.apply()['status'])
        self.assertEqual('completed', qa_recover.load(c.state)['phase'])

    def test_unverified_public_commit_uses_cached_release_without_fast_forward(self):
        c = self.case; c.publish()
        c.git(c.source, 'remote', 'set-url', 'origin', updater.REMOTE)
        original_git = updater.git
        def local_fetch(source, *arguments):
            arguments = list(arguments)
            if arguments[0] == 'fetch':
                arguments[arguments.index('origin')] = str(c.remote)
            return original_git(source, *arguments)
        with patch('qa_auto_update.git', side_effect=local_fetch), patch('qa_auto_update.ci_passed', return_value=False), \
                updater.SessionLock(c.state) as lock:
            result = updater.update(c.source, c.project, c.installed, 'fake-gigacode', lock, process_check=lambda: False)
        self.assertEqual('release_not_verified', result['status'])
        self.assertEqual(c.base, c.git(c.source, 'rev-parse', 'HEAD'))
        self.assertIsNone(updater.load_state(c.state)['pending'])

    def test_managed_execution_must_be_stopped_before_rollback(self):
        c = self.case; c.publish(); c.apply()
        state = c.project / '.qa-agent-state/execution.json'; state.parent.mkdir()
        state.write_text(json.dumps({'plan': {}, 'steps': {}, 'status': 'running'}))
        refused = self.helper()
        self.assertEqual(2, refused.returncode)
        self.assertIn('stop active assignments', refused.stderr)
        state.write_text(json.dumps({'plan': {}, 'steps': {}, 'status': 'cancelled'}))
        allowed = self.helper()
        self.assertEqual(0, allowed.returncode, allowed.stderr)
        self.assertEqual('cancelled', json.loads(state.read_text())['status'])

    def test_rollback_preserves_modes_and_archives_displaced_extension_outside_install_root(self):
        c = self.case
        executable = c.installed / 'scripts/qa_start.py'; executable.chmod(0o755)
        c.publish(); c.apply()
        result = self.helper()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(0o755, executable.stat().st_mode & 0o777)
        journal = qa_recover.load(c.state)
        self.assertTrue((c.state / journal['failed_snapshot']).is_file())
        self.assertFalse(Path(journal['displaced_path']).exists())
        self.assertEqual([], list(c.installed.parent.glob('.qa-displaced-*')))
        before = (c.state / 'recovery.json').read_bytes()
        self.assertEqual(0, self.helper().returncode)
        self.assertEqual(before, (c.state / 'recovery.json').read_bytes())

    def test_crash_after_completion_marker_reconciles_journal_before_next_update(self):
        c = self.case; c.publish()
        original = updater.save_recovery
        def crash(profile, journal):
            if journal['phase'] == 'completed':
                raise OSError('injected completion journal failure')
            return original(profile, journal)
        with patch('qa_auto_update.save_recovery', side_effect=crash), self.assertRaises(OSError):
            c.apply()
        self.assertIsNone(updater.load_state(c.state)['pending'])
        self.assertEqual('activated', qa_recover.load(c.state)['phase'])
        self.assertEqual('updated', c.apply()['status'])
        self.assertEqual('completed', qa_recover.load(c.state)['phase'])

    def test_interrupt_terminates_installer_group_before_it_can_write_later(self):
        destination = self.case.root / 'late-interrupt.txt'
        child_code = 'import time;from pathlib import Path;time.sleep(1);Path(' + repr(str(destination)) + ').write_text("late")'
        parent_code = 'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",' + repr(child_code) + ']);time.sleep(5)'
        process = subprocess.Popen([sys.executable, '-c', parent_code], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        original = process.communicate
        calls = 0
        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                time.sleep(.1)
                raise KeyboardInterrupt()
            return original(*args, **kwargs)
        with patch('qa_auto_update.subprocess.Popen', return_value=process), \
                patch.object(process, 'communicate', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            updater.run(['fixture-installer'])
        time.sleep(1.05)
        self.assertFalse(destination.exists())
        self.assertIsNotNone(process.poll())


class ReleaseCIGateTests(unittest.TestCase):
    def check(self, value):
        response = io.BytesIO(json.dumps(value).encode())
        with patch('qa_release.urllib.request.urlopen', return_value=response):
            return qa_release.ci_passed('a' * 40)

    def run_record(self, **updates):
        return {'head_sha': 'a' * 40, 'head_branch': 'main', 'event': 'push', 'status': 'completed',
                'conclusion': 'success', 'run_number': 1, **updates}

    def test_exact_successful_main_push_is_required(self):
        self.assertTrue(self.check({'workflow_runs': [self.run_record()]}))
        for wrong in ({'head_sha': 'b' * 40}, {'head_branch': 'feature'}, {'event': 'pull_request'},
                      {'status': 'in_progress'}, {'conclusion': 'failure'}):
            self.assertFalse(self.check({'workflow_runs': [self.run_record(**wrong)]}))

    def test_newer_failed_or_running_check_cannot_reuse_an_old_success(self):
        for value in ({'conclusion': 'failure'}, {'status': 'in_progress'}):
            self.assertFalse(self.check({'workflow_runs': [self.run_record(), self.run_record(run_number=2, **value)]}))

    def test_unavailable_malformed_or_oversized_ci_response_fails_closed(self):
        for value in ({}, {'workflow_runs': 'invalid'}, []):
            self.assertFalse(self.check(value))
        with patch('qa_release.urllib.request.urlopen', side_effect=OSError('offline')):
            self.assertFalse(qa_release.ci_passed('a' * 40))
        with patch('qa_release.urllib.request.urlopen', return_value=io.BytesIO(b'x' * (1024 * 1024 + 1))):
            self.assertFalse(qa_release.ci_passed('a' * 40))


if __name__ == '__main__':
    unittest.main()
