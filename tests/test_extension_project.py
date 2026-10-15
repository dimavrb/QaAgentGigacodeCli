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
from qa_extension_mcp import ToolDispatcher, handle


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
                     'scripts/qa_project.py', 'scripts/qa_extension_mcp.py', 'scripts/qa_tasks.py', 'scripts/qa_files.py',
                     'scripts/qa_agent_state.py', 'scripts/qa_project_agents.py',
                     'scripts/extension_resources.py',
                     'examples/qa-only/APP_CONTEXT.template.md'):
            target = self.package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        for key in ('agents', 'skills', 'commands'):
            shutil.copytree(ROOT / manifest[key], self.package / manifest[key])
            shutil.copytree(ROOT / key, self.package / key)
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


class ExtensionMCPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        self.dispatcher = ToolDispatcher()

    def call(self, name, arguments):
        return handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                       'params': {'name': name, 'arguments': arguments}}, self.dispatcher)

    def snapshot(self):
        return {str(p.relative_to(self.project)): p.read_bytes()
                for p in self.project.rglob('*') if p.is_file()}

    def moved_binding(self):
        initialize(self.project)
        binding = json.loads((self.project / CONFIG).read_text())
        binding['package_root'] = str(self.project / 'old-package')
        (self.project / CONFIG).write_text(json.dumps(binding))
        return binding

    def run_server(self, messages, cwd):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/qa_extension_mcp.py')],
                                input='\n'.join(messages) + '\n', cwd=cwd,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_stdio_initialize_and_metadata_use_package_not_client_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            messages = [json.dumps(message) for message in (
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'qa_package_info', 'arguments': {}}},
            )]
            responses = self.run_server(messages, directory)
            self.assertEqual(3, len(responses))
            self.assertEqual('2025-03-26', responses[0]['result']['protocolVersion'])
            tools = {tool['name']: tool for tool in responses[1]['result']['tools']}
            self.assertEqual({'qa_package_info', 'qa_project_init', 'qa_project_doctor', 'qa_tasks_list', 'qa_task_get'}, set(tools))
            for name in ('qa_tasks_list', 'qa_task_get'):
                self.assertTrue(tools[name]['annotations']['readOnlyHint'])
            self.assertTrue(tools['qa_package_info']['annotations']['readOnlyHint'])
            self.assertTrue(tools['qa_project_doctor']['annotations']['readOnlyHint'])
            self.assertFalse(tools['qa_project_init']['annotations']['readOnlyHint'])
            self.assertTrue(tools['qa_project_init']['annotations']['idempotentHint'])
            self.assertEqual(False, tools['qa_project_init']['inputSchema']['properties']['rebind']['default'])
            for name in ('qa_project_init', 'qa_project_doctor'):
                self.assertEqual(['project_root'], tools[name]['inputSchema']['required'])
                self.assertFalse(tools[name]['inputSchema']['additionalProperties'])
            info = json.loads(responses[2]['result']['content'][0]['text'])
            self.assertEqual(str(ROOT), info['package_root'])
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_unknown_tools_arguments_and_malformed_messages_are_rejected(self):
        requests = ['bad json', '[]', *[json.dumps({'jsonrpc': '2.0', 'id': index,
            'method': 'tools/call', 'params': params}) for index, params in enumerate((
                {'name': 'write_file'}, {'name': 'qa_package_info', 'arguments': {'path': '/tmp'}},
                {'name': 'qa_project_init', 'arguments': {}},
                {'name': 'qa_project_doctor', 'arguments': {'project_root': '/tmp', 'rebind': True}},
                {'name': 'qa_project_init', 'arguments': {'project_root': '/tmp', 'rebind': 1}},
                {'name': 'qa_project_init', 'arguments': []},
                {'name': ['qa_project_init']}), start=1)]]
        with tempfile.TemporaryDirectory() as directory:
            results = self.run_server(requests, directory)
            self.assertTrue(all('error' in response for response in results))
            self.assertEqual([], list(Path(directory).iterdir()))

    def test_init_calls_python_directly_and_writes_only_binding_files_idempotently(self):
        (self.project / 'GIGACODE.md').write_text('Product rules')
        with patch('qa_extension_mcp.initialize', wraps=initialize) as init, patch(
                'qa_project.subprocess.run', side_effect=AssertionError('No child process allowed')):
            result = self.call('qa_project_init', {'project_root': str(self.project)})['result']
            self.assertNotIn('isError', result)
            data = json.loads(result['content'][0]['text'])
            self.assertEqual('configured', data['status'])
            self.assertEqual({'.gitignore', CONTEXT.as_posix(), CONFIG.as_posix()}, set(data['changed_files']))
            self.assertEqual({'GIGACODE.md', '.gitignore', CONTEXT.as_posix(), CONFIG.as_posix()}, set(self.snapshot()))
            (self.project / CONTEXT).write_text('Reviewed context')
            before = self.snapshot()
            second = self.call('qa_project_init', {'project_root': str(self.project), 'rebind': False})['result']
            self.assertEqual([], json.loads(second['content'][0]['text'])['changed_files'])
            self.assertEqual(before, self.snapshot())
            self.assertEqual(2, init.call_count)

    def test_relative_paths_rejected_before_initializer_or_doctor(self):
        with patch('qa_extension_mcp.initialize') as init, patch('qa_extension_mcp.doctor') as diagnostic:
            for name in ('qa_project_init', 'qa_project_doctor'):
                for path in ('application', '.', '', None, 42):
                    with self.subTest(name=name, path=path):
                        response = self.call(name, {'project_root': path})
                        self.assertEqual(-32602, response['error']['code'])
                        self.assertIn('absolute path', response['error']['message'])
            init.assert_not_called()
            diagnostic.assert_not_called()
        self.assertEqual({}, self.snapshot())

    def test_initialize_errors_have_cli_text_in_tool_result(self):
        for exc in (ValueError('invalid binding'), OSError('permission denied'), TypeError('bad configuration')):
            with self.subTest(exc=exc), patch('qa_extension_mcp.initialize', side_effect=exc):
                response = self.call('qa_project_init', {'project_root': str(self.project)})
                self.assertNotIn('error', response)
                self.assertTrue(response['result']['isError'])
                self.assertEqual(f'QA project error: {exc}', response['result']['content'][0]['text'])

    def test_stdio_execution_errors_return_content_and_server_continues(self):
        requests = [json.dumps({'jsonrpc': '2.0', 'id': index, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments}}) for index, (name, arguments) in enumerate((
                ('qa_project_init', {'project_root': str(self.project / 'missing')}),
                ('qa_package_info', {})), start=1)]
        responses = self.run_server(requests, self.project)
        self.assertTrue(responses[0]['result']['isError'])
        self.assertEqual('QA project error: project directory must already exist',
                         responses[0]['result']['content'][0]['text'])
        self.assertEqual(str(ROOT), json.loads(responses[1]['result']['content'][0]['text'])['package_root'])
        self.assertEqual({}, self.snapshot())

    def test_doctor_needs_attention_is_read_only_normal_result(self):
        with patch('qa_extension_mcp.doctor', wraps=doctor) as diagnostic, patch(
                'qa_project.shutil.which', return_value=None), patch('qa_project.subprocess.run') as child:
            response = self.call('qa_project_doctor', {'project_root': str(self.project)})
            diagnostic.assert_called_once_with(self.project)
            child.assert_not_called()
        self.assertNotIn('isError', response['result'])
        self.assertEqual('needs_attention', json.loads(response['result']['content'][0]['text'])['status'])
        self.assertEqual({}, self.snapshot())

    def test_doctor_only_runs_existing_version_probe(self):
        initialize(self.project)
        before = self.snapshot()
        with patch('qa_project.shutil.which', return_value='/fake/gigacode'), patch(
                'qa_project.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'fixture 1\n', '')) as child:
            result = self.call('qa_project_doctor', {'project_root': str(self.project)})['result']
            child.assert_called_once_with(['/fake/gigacode', '--version'], capture_output=True, text=True, timeout=10)
        data = json.loads(result['content'][0]['text'])
        self.assertEqual('ready_for_cli_check', data['status'])
        self.assertFalse(data['live_extension_verified'])
        self.assertEqual(before, self.snapshot())

    def test_rebind_requires_prior_preview_and_then_preserves_context(self):
        old = self.moved_binding()['package_root']
        (self.project / CONTEXT).write_text('Reviewed context')
        before = self.snapshot()
        arguments = {'project_root': str(self.project), 'rebind': True}
        with patch('qa_extension_mcp.initialize', wraps=initialize) as init:
            refused = self.call('qa_project_init', arguments)['result']
            self.assertTrue(refused['isError'])
            init.assert_not_called()
            self.assertEqual(before, self.snapshot())
            preview = self.call('qa_project_init', {'project_root': str(self.project)})['result']
            self.assertTrue(preview['isError'])
            text = preview['content'][0]['text']
            self.assertIn('QA project error: package path changed;', text)
            self.assertIn(f'Old package path: {old}', text)
            self.assertIn(f'New package path: {ROOT}', text)
            self.assertEqual(before, self.snapshot())
            result = self.call('qa_project_init', arguments)['result']
            self.assertNotIn('isError', result)
            self.assertEqual([CONFIG.as_posix()], json.loads(result['content'][0]['text'])['changed_files'])
            self.assertEqual('Reviewed context', (self.project / CONTEXT).read_text())

    def test_rebind_preview_is_scoped_to_session_project_and_current_paths(self):
        self.moved_binding()
        arguments = {'project_root': str(self.project)}
        self.call('qa_project_init', arguments)
        before = self.snapshot()
        other_session = ToolDispatcher()
        response = handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                           'params': {'name': 'qa_project_init', 'arguments': {**arguments, 'rebind': True}}}, other_session)
        self.assertTrue(response['result']['isError'])
        self.assertEqual(before, self.snapshot())
        with tempfile.TemporaryDirectory() as other:
            self.assertTrue(self.call('qa_project_init', {'project_root': other, 'rebind': True})['result']['isError'])
            self.assertEqual([], list(Path(other).iterdir()))
        binding = json.loads((self.project / CONFIG).read_text())
        binding['package_root'] = str(self.project / 'another-old-package')
        (self.project / CONFIG).write_text(json.dumps(binding))
        before = self.snapshot()
        self.assertTrue(self.call('qa_project_init', {**arguments, 'rebind': True})['result']['isError'])
        self.assertEqual(before, self.snapshot())
        # A fresh preview is required after changed paths or a consumed review.
        self.call('qa_project_init', arguments)
        self.assertNotIn('isError', self.call('qa_project_init', {**arguments, 'rebind': True})['result'])

    def test_stdio_project_tools_initialize_and_diagnose(self):
        messages = [json.dumps({'jsonrpc': '2.0', 'id': index, 'method': 'tools/call',
                   'params': {'name': name, 'arguments': {'project_root': str(self.project)}}})
                    for index, name in enumerate(('qa_project_init', 'qa_project_doctor'), start=1)]
        responses = self.run_server(messages, self.project)
        self.assertEqual('configured', json.loads(responses[0]['result']['content'][0]['text'])['status'])
        self.assertNotIn('isError', responses[1]['result'])
        self.assertEqual({'.gitignore', CONTEXT.as_posix(), CONFIG.as_posix()}, set(self.snapshot()))


if __name__ == '__main__':
    unittest.main()
