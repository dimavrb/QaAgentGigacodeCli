"""Verify runtime selection and refusal boundaries without a live model."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import agent_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / 'runtime.json'
        self.save(dict(schema_version=1, backend='fixture', executable='fixture-cli',
                       protocol='headless-json-v1'))

    def save(self, value):
        self.config.write_text(json.dumps(value))

    def test_shipped_default_and_explicit_override(self):
        self.assertEqual('gigacode', runtime.load_runtime()['executable'])
        selected = runtime.load_runtime(self.config, executable='/custom/cli')
        self.assertEqual('fixture', selected['backend'])
        self.assertEqual('/custom/cli', selected['executable'])

    def test_invalid_configuration_cannot_silently_select_a_runtime(self):
        for value in [[], {}, dict(schema_version=1, protocol='other', executable='cli', backend='x'),
                      dict(schema_version=1, protocol='headless-json-v1', executable='', backend='x')]:
            self.save(value)
            with self.assertRaises(ValueError): runtime.load_runtime(self.config)

    def test_missing_executable_never_invokes_cli(self):
        with patch.object(runtime.shutil, 'which', return_value=None), patch.object(runtime.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                runtime.preflight(runtime.load_runtime(self.config), runtime.ISOLATION_FLAGS)
            run.assert_not_called()

    def test_similar_flag_does_not_satisfy_isolation_requirement(self):
        help_text = ' '.join(runtime.ISOLATION_FLAGS).replace('--bare', '--bare-mode')
        with patch.object(runtime.shutil, 'which', return_value='/fixture/cli'), patch.object(
                runtime.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, help_text, '')) as run:
            with self.assertRaisesRegex(ValueError, '--bare'):
                runtime.preflight(runtime.load_runtime(self.config), runtime.ISOLATION_FLAGS)
            self.assertEqual(1, run.call_count)

    def test_missing_gateway_flag_is_rejected_before_version_or_prompt(self):
        with patch.object(runtime.shutil, 'which', return_value='/fixture/cli'), patch.object(
                runtime.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, ' '.join(runtime.ISOLATION_FLAGS), '')) as run:
            with self.assertRaisesRegex(ValueError, '--mcp-config'):
                runtime.preflight(runtime.load_runtime(self.config), runtime.ISOLATION_FLAGS + ['--mcp-config'])
            self.assertEqual(1, run.call_count)

    def test_version_pin_is_checked_without_model_request(self):
        responses = [subprocess.CompletedProcess([], 0, ' '.join(runtime.ISOLATION_FLAGS), ''),
                     subprocess.CompletedProcess([], 0, 'v2', '')]
        with patch.object(runtime.shutil, 'which', return_value='/fixture/cli'), patch.object(
                runtime.subprocess, 'run', side_effect=responses) as run:
            with self.assertRaisesRegex(ValueError, 'version differs'):
                runtime.preflight(runtime.load_runtime(self.config), runtime.ISOLATION_FLAGS, expected_version='v1')
            self.assertEqual([['/fixture/cli', '--help'], ['/fixture/cli', '--version']],
                             [call.args[0] for call in run.call_args_list])

    def test_isolated_command_keeps_scope_and_model_without_auto_edit(self):
        argv = runtime.headless_command('/fixture/cli', 'task', 'rules', max_wall_time='5s',
                                        isolated=True, denied_tools=['read_file', 'run_shell_command'],
                                        model='fixture-model', max_session_turns=2)
        self.assertIn('--bare', argv)
        self.assertIn('--exclude-tools', argv)
        self.assertIn('run_shell_command', argv)
        self.assertNotIn('--approval-mode', argv)
        self.assertEqual('2', argv[argv.index('--max-session-turns') + 1])
        self.assertEqual('fixture-model', argv[argv.index('--model') + 1])
        with self.assertRaises(ValueError):
            runtime.headless_command('cli', 'task', 'rules', max_wall_time='5s', isolated=True)

    def test_measurements_need_explicit_provider_and_executable_identity(self):
        new = dict(backend='gigacode', cli_version='v1', cli_executable_sha256='same-binary')
        self.assertEqual(('gigacode', 'v1', 'same-binary'), runtime.runtime_identity(new))
        runtime.assert_same_runtime(new, dict(new))
        with self.assertRaises(ValueError): runtime.assert_same_runtime(new, dict(new, backend='different'))
        for incomplete in ({'cli_version': 'v1'}, {}, dict(new, backend=''),
                           dict(new, cli_executable_sha256=None)):
            with self.subTest(manifest=incomplete):
                with self.assertRaises(ValueError): runtime.assert_same_runtime(new, incomplete)


if __name__ == '__main__': unittest.main()
