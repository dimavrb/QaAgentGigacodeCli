import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import qa_auto_update as updater
from qa_project import CONFIG
from update_fixtures import UpdateFixture, copy_package


class AutoUpdateTests(UpdateFixture, unittest.TestCase):
    def setUp(self):
        self.set_up_update_fixture(native_ci=(
            self._testMethodName == 'test_launcher_reexecutes_new_code_refreshes_binding_and_forwards_cli_args'))

    def test_fast_forward_activation_and_binding_preserve_project(self):
        target = self.publish()
        result = self.apply()
        self.assertEqual('updated', result['status'])
        self.assertEqual(target, self.git(self.source, 'rev-parse', 'HEAD'))
        self.assertTrue(updater.payload_matches(self.source, self.installed))
        refreshed = updater.refresh_binding(self.project, self.installed)
        self.assertEqual(self.next_version, refreshed['package_version'])
        self.assertIsNone(updater.load_state(self.state)['pending'])
        self.assert_project_preserved()

    def test_up_to_date_never_calls_extension_update(self):
        self.assertEqual('up_to_date', self.apply()['status'])
        self.assertEqual([], self.calls)

    def test_offline_uses_unchanged_cached_installation(self):
        original_git = updater.git
        def offline(source, *arguments):
            if arguments[0] == 'fetch':
                raise updater.UpdateError('fixture network unavailable')
            return original_git(source, *arguments)
        with patch('qa_auto_update.git', side_effect=offline):
            result = self.apply()
        self.assertEqual('offline_cached', result['status'])
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))
        self.assertEqual([], self.calls)
        self.assert_project_preserved()

    def test_dirty_source_is_not_reset(self):
        self.publish()
        (self.source / 'README.md').write_text('Uncommitted local adaptation')
        with self.assertRaisesRegex(updater.UpdateError, 'local source has changes'):
            self.apply()
        self.assertEqual('Uncommitted local adaptation', (self.source / 'README.md').read_text())
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))

    def test_wrong_origin_is_rejected_without_leaking_credentials(self):
        self.git(self.source, 'remote', 'set-url', 'origin', 'https://credential@example.invalid/private')
        with self.assertRaisesRegex(updater.UpdateError, 'authorized QA repository') as error:
            self.apply()
        self.assertNotIn('credential', str(error.exception))

    def test_wrong_branch_is_not_switched(self):
        self.git(self.source, 'switch', '-c', 'feature')
        with self.assertRaisesRegex(updater.UpdateError, 'main branch'):
            self.apply()
        self.assertEqual('feature', self.git(self.source, 'symbolic-ref', '--short', 'HEAD'))

    def test_diverged_local_branch_is_not_rewritten(self):
        self.publish()
        (self.source / 'README.md').write_text('Local committed adaptation')
        self.git(self.source, 'config', 'user.name', 'QA fixture')
        self.git(self.source, 'config', 'user.email', 'fixture@example.invalid')
        self.git(self.source, 'add', '.')
        self.git(self.source, 'commit', '-m', 'local fork')
        local = self.git(self.source, 'rev-parse', 'HEAD')
        copy_package(self.source, self.installed)
        with self.assertRaisesRegex(updater.UpdateError, 'ahead or diverged'):
            self.apply()
        self.assertEqual(local, self.git(self.source, 'rev-parse', 'HEAD'))

    def test_commit_without_version_bump_is_not_applied(self):
        self.publish(bump=False)
        with self.assertRaisesRegex(updater.UpdateError, 'increase VERSION'):
            self.apply()
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))
        self.assertEqual([], self.calls)

    def test_local_installed_adaptations_are_preserved(self):
        self.publish()
        (self.installed / 'README.md').write_text('Local installed adaptation')
        self.assertEqual('deferred_installed_changes', self.apply()['status'])
        self.assertEqual('Local installed adaptation', (self.installed / 'README.md').read_text())
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))

    def test_active_managed_task_defers_update_before_fetch(self):
        self.publish()
        directory = self.project / '.qa-agent-state'
        directory.mkdir()
        (directory / 'task.json').write_text(json.dumps({'execution_status': 'waiting', 'active_request_ids': []}))
        self.assertEqual('deferred_active_work', self.apply()['status'])
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'origin/main'))
        self.assertEqual([], self.calls)

    def test_unmanaged_cli_process_defers_update(self):
        self.publish()
        self.assertEqual('deferred_active_work', self.apply(process_check=lambda: True)['status'])
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))

    def test_shared_session_lock_prevents_updates(self):
        self.publish()
        with updater.SessionLock(self.state) as first:
            first.share()
            with updater.SessionLock(self.state) as second:
                self.assertFalse(second.exclusive)
                result = updater.update(self.source, self.project, self.installed, 'fake', second)
                self.assertEqual('deferred_session', result['status'])
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))

    def test_exclusive_update_lock_does_not_wait_forever(self):
        with updater.SessionLock(self.state):
            with self.assertRaisesRegex(updater.UpdateError, 'another updater'):
                updater.SessionLock(self.state).acquire()

    def test_unknown_managed_state_blocks_instead_of_ignoring_it(self):
        directory = self.project / '.qa-agent-state'
        directory.mkdir()
        (directory / 'task.json').write_text('broken json')
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))

    def test_failed_activation_keeps_intent_and_next_launch_repairs_it(self):
        target = self.publish()
        original_native = self.native_cli
        def failed(arguments, **kwargs):
            if arguments[:1] == ['fake-gigacode'] and arguments[-1:] == ['qa-agent']:
                self.calls.append(arguments[1:])
                return subprocess.CompletedProcess(arguments, 1, '', 'fixture failure')
            return original_native(arguments, **kwargs)
        with patch.object(self, 'native_cli', side_effect=failed):
            with self.assertRaisesRegex(updater.UpdateError, 'activation failed') as error:
                self.apply()
        self.assertIn('fixture failure', str(error.exception))
        self.assertEqual(target, updater.load_state(self.state)['pending']['commit'])
        self.assertEqual(self.base_version, (self.installed / 'VERSION').read_text().strip())
        self.assertEqual('updated', self.apply()['status'])
        self.assertIsNone(updater.load_state(self.state)['pending'])
        self.assert_project_preserved()

    def test_uninstall_request_preserves_installation_and_reconciles_manual_reinstall(self):
        target = self.publish()
        original_native = self.native_cli
        for stdout, stderr in [('', 'Please uninstall qa-agent first'),
                               ('Run UNINSTALL before installing the new version', ''),
                               ('', 'unistall qa-agent first')]:
            with self.subTest(stdout=stdout, stderr=stderr):
                def failed(arguments, **kwargs):
                    if arguments == ['fake-gigacode', 'extensions', 'update', 'qa-agent']:
                        self.calls.append(arguments[1:])
                        return subprocess.CompletedProcess(arguments, 1, stdout, stderr)
                    return original_native(arguments, **kwargs)
                with patch.object(self, 'native_cli', side_effect=failed):
                    with self.assertRaisesRegex(updater.UpdateError, 'CLI requested uninstall') as error:
                        self.apply()
                self.assertIn(stdout or stderr, str(error.exception))
                self.assertIn('qa-start --update-only', str(error.exception))
                self.assertEqual(target, updater.load_state(self.state)['pending']['commit'])
                self.assertTrue((self.state / 'recovery.json').is_file())
                recovery = json.loads((self.state / 'recovery.json').read_text())
                self.assertTrue((self.state / recovery['snapshot']).is_file())
                self.assertEqual(self.base_version, (self.installed / 'VERSION').read_text().strip())
                self.assert_project_preserved()
        self.assertTrue(all(call[:2] == ['extensions', 'update'] for call in self.calls))
        shutil.rmtree(self.installed)  # Simulate manual uninstall/install at the same path.
        copy_package(self.source, self.installed)
        calls_before_resume = list(self.calls)
        self.assertEqual('updated', self.apply()['status'])
        self.assertEqual(calls_before_resume, self.calls)
        self.assertIsNone(updater.load_state(self.state)['pending'])
        self.assertTrue(updater.payload_matches(self.source, self.installed))
        self.assert_project_preserved()

    def test_crash_before_source_merge_is_recoverable(self):
        self.publish()
        original = updater.git
        def crash(source, *arguments):
            if arguments[0] == 'merge':
                raise updater.UpdateError('fixture crash before merge')
            return original(source, *arguments)
        with patch('qa_auto_update.git', side_effect=crash):
            with self.assertRaisesRegex(updater.UpdateError, 'crash before merge'):
                self.apply()
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))
        self.assertIsNotNone(updater.load_state(self.state)['pending'])
        self.assertEqual('updated', self.apply()['status'])

    def test_readback_after_success_does_not_activate_twice(self):
        self.publish()
        original = updater.save_state
        def crash(directory, state):
            if state['pending'] is None and self.calls:
                raise updater.UpdateError('fixture crash after activation')
            return original(directory, state)
        with patch('qa_auto_update.save_state', side_effect=crash):
            with self.assertRaisesRegex(updater.UpdateError, 'after activation'):
                self.apply()
        activation_count = self.calls.count(['extensions', 'update', 'qa-agent'])
        self.assertEqual('updated', self.apply()['status'])
        self.assertEqual(activation_count, self.calls.count(['extensions', 'update', 'qa-agent']))

    def test_configure_is_local_ignored_and_rejects_source_changes(self):
        updater.configure(self.project, self.source, self.installed)
        path = self.project / updater.UPDATE_CONFIG
        before = path.read_bytes()
        updater.configure(self.project, self.source, self.installed)
        self.assertEqual(before, path.read_bytes())
        self.assertIn('/.qa-agent/update.local.json', (self.project / '.gitignore').read_text())
        with self.assertRaisesRegex(updater.UpdateError, 'source or installation changed'):
            updater.configure(self.project, self.author, self.installed)
        self.assert_project_preserved()

    def test_active_work_in_another_registered_project_defers_update(self):
        other = self.root / 'other-app'
        other.mkdir()
        self.state.mkdir()
        updater.register_project(self.state, other)
        directory = other / '.qa-agent-state'
        directory.mkdir()
        (directory / 'execution.json').write_text(json.dumps({'plan': {}, 'steps': {}, 'status': 'running'}))
        self.publish()
        self.assertEqual('deferred_active_work', self.apply()['status'])
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))

    def test_missing_cli_update_command_does_not_advance_source(self):
        self.publish()
        original = self.native_cli
        def unsupported(arguments, **kwargs):
            if arguments[:1] == ['fake-gigacode']:
                return subprocess.CompletedProcess(arguments, 2, '', 'unsupported')
            return original(arguments, **kwargs)
        with patch.object(self, 'native_cli', side_effect=unsupported):
            with self.assertRaisesRegex(updater.UpdateError, 'does not support extensions update'):
                self.apply()
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))
        self.assertIsNone(updater.load_state(self.state)['pending'])

    def test_success_exit_with_incomplete_installation_remains_pending(self):
        self.publish()
        original = self.native_cli
        def incomplete(arguments, **kwargs):
            if arguments[:1] == ['fake-gigacode'] and arguments[-1:] == ['qa-agent']:
                (self.installed / 'VERSION').write_text(self.next_version+'\n')
                return subprocess.CompletedProcess(arguments, 0, '', '')
            return original(arguments, **kwargs)
        with patch.object(self, 'native_cli', side_effect=incomplete):
            with self.assertRaisesRegex(updater.UpdateError, 'activation failed'):
                self.apply()
        self.assertIsNotNone(updater.load_state(self.state)['pending'])
        self.assertEqual(self.base_version, json.loads((self.project / CONFIG).read_text())['package_version'])
        self.assert_project_preserved()

    def test_extra_installed_agent_is_not_silently_left_after_update(self):
        self.publish()
        for relative in ('.gigacode/agents/local-agent.md', 'agents/local-agent.md',
                         'skills/local-skill/SKILL.md', 'commands/local-command.md'):
            with self.subTest(relative=relative):
                target = self.installed / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('Local resource')
                self.assertEqual('deferred_installed_changes', self.apply()['status'])
                self.assertEqual('Local resource', target.read_text())
                self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))
                target.unlink()

    def test_remote_symlink_is_rejected_before_changing_source(self):
        (self.author / 'scripts/outside.py').symlink_to('/tmp/external.py')
        self.publish()
        with self.assertRaisesRegex(updater.UpdateError, 'remote package contains a symlink'):
            self.apply()
        self.assertEqual(self.base, self.git(self.source, 'rev-parse', 'HEAD'))

    def test_timeout_kills_installer_child_before_it_can_write_later(self):
        destination = self.root / 'late-write.txt'
        child = 'import time; from pathlib import Path; time.sleep(1); Path('+repr(str(destination))+').write_text("late")'
        parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",'+repr(child)+']); time.sleep(5)'
        with self.assertRaises(subprocess.TimeoutExpired):
            updater.run([sys.executable, '-c', parent], timeout=0.2)
        time.sleep(1.05)
        self.assertFalse(destination.exists())

    def test_launcher_reexecutes_new_code_refreshes_binding_and_forwards_cli_args(self):
        from qa_project_agents import install_agents
        install_agents(self.project, self.installed, apply=True)
        role = self.author / '.gigacode/agents/qa-analyst.md'
        role.write_text(role.read_text() + '\nUpdated role fixture\n')
        self.publish()
        # A local Git shim redirects only fetch; production origin remains the approved URL.
        self.git(self.source, 'remote', 'set-url', 'origin', updater.REMOTE)
        binaries = self.root / 'bin'
        binaries.mkdir()
        real_git = shutil.which('git')
        git_script = binaries / 'git'
        git_script.write_text('#!' + sys.executable + '\nimport os, sys\na=sys.argv[1:]\n'
            'if "fetch" in a: a[a.index("origin")]=os.environ["QA_FIXTURE_REMOTE"]\n'
            'os.execv(os.environ["QA_FIXTURE_GIT"], [os.environ["QA_FIXTURE_GIT"], *a])\n')
        git_script.chmod(0o755)
        cli = binaries / 'gigacode'
        cli.write_text('#!' + sys.executable + '\nimport os,sys,json,shutil\nfrom pathlib import Path\n'
            'a=sys.argv[1:]\n'
            'if a==["extensions","update","--help"]: print("fixture update")\n'
            'elif a==["extensions","update","qa-agent"]:\n'
            ' shutil.copytree(os.environ["QA_FIXTURE_SOURCE"],os.environ["QA_FIXTURE_INSTALLED"],dirs_exist_ok=True,ignore=shutil.ignore_patterns(".git","__pycache__"))\n'
            'else: Path("cli-call.json").write_text(json.dumps(a))\n')
        cli.chmod(0o755)
        environment = dict(os.environ, PATH=str(binaries)+os.pathsep+os.environ['PATH'],
                           QA_FIXTURE_GIT=real_git, QA_FIXTURE_REMOTE=str(self.remote),
                           QA_FIXTURE_SOURCE=str(self.source), QA_FIXTURE_INSTALLED=str(self.installed))
        result = subprocess.run([sys.executable, str(self.source / 'scripts/qa_start.py'),
            '--project-root', str(self.project), '--state-dir', str(self.state), '--', '--model', 'fixture'],
            env=environment, text=True, capture_output=True, timeout=25)
        self.assertEqual(0, result.returncode, result.stdout+result.stderr)
        self.assertIn('"status": "updated"', result.stderr)
        self.assertIn('"status": "reloaded"', result.stderr)
        self.assertEqual(self.next_version, json.loads((self.project / CONFIG).read_text())['package_version'])
        self.assertEqual(['--model', 'fixture'], json.loads((self.project / 'cli-call.json').read_text()))
        self.assertEqual((self.installed / '.gigacode/agents/qa-analyst.md').read_bytes(),
                         (self.project / '.gigacode/agents/qa-analyst.md').read_bytes())
        self.assert_project_preserved()
