import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import qa_tasks
import task_exchange as exchange


class TaskDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve() / 'product with пробелами'
        self.project.mkdir()

    def registry(self, tid='QA-123', directory=None):
        path = self.project / qa_tasks.TASKS / (directory or tid) / 'task.json'
        exchange.init_task(path, tid, 'Проверка заказов', {'schema_version': 1, 'criteria': [{
            'criterion_id': 'C-1', 'description': 'Проверить требования', 'source': 'requirements.md', 'required': True}]})
        return path

    def change(self, path, **updates):
        data = json.loads(path.read_text())
        data.update(updates)
        path.write_text(json.dumps(data, ensure_ascii=False))
        return data

    def snapshot(self):
        return {str(path.relative_to(self.project)): path.read_bytes() if path.is_file() else None
                for path in self.project.rglob('*')}

    def test_empty_discovery_creates_nothing_and_runs_no_processes(self):
        with patch('subprocess.Popen', side_effect=AssertionError('No processes')):
            result = qa_tasks.tasks_list(self.project)
        self.assertEqual('ready', result['status'])
        self.assertEqual([], result['tasks'])
        self.assertEqual(0, result['total'])
        self.assertEqual({}, self.snapshot())

    def test_list_and_get_read_real_registry_without_mutation(self):
        path = self.registry()
        before = self.snapshot()
        listing = qa_tasks.tasks_list(self.project)
        task = listing['tasks'][0]
        self.assertEqual('QA-123', task['task_id'])
        self.assertEqual(str(path), task['state_path'])
        self.assertEqual('queued', task['execution_status'])
        self.assertEqual('Проверка заказов', task['title'])
        self.assertNotIn('criteria', task)
        detail = qa_tasks.task_get(self.project, 'QA-123')
        self.assertEqual('C-1', detail['task']['criteria'][0]['criterion_id'])
        self.assertTrue(detail['snapshot_only'])
        self.assertFalse(detail['resume_ready'])
        self.assertEqual(task['state_sha256'], detail['task']['state_sha256'])
        self.assertEqual(before, self.snapshot())

    def test_actual_owner_and_artifact_references_are_preserved_but_tokens_are_hidden(self):
        path = self.registry()
        assignment = {'task_id': 'QA-123', 'request_id': 'plan-1', 'agent': 'qa-test-plan-writer',
                      'stage': 'plan', 'goal': 'План заказов', 'acceptance_criteria': ['Карта требований'],
                      'stop_condition': 'Нет источника ожиданий', 'expected_artifact': 'JSON отчёт',
                      'result_path': str(self.project / '.qa-agent/artifacts/QA-123/plan/result.json'),
                      'criterion_ids': ['C-1'], 'scenario_ids': ['TC-1'], 'inputs': []}
        exchange.assign(path, assignment, self.project, 1)
        packet = exchange.prepare_handoff(path, 'plan-1', 2)
        delivery = packet['result']['delivery_id']
        exchange.start_request(path, 'plan-1', 'existing-owner', delivery, 3)
        data = json.loads(path.read_text())
        data['secret_extra'] = 'SECRET-EXTRA'
        data['events'].append({'secret': 'SECRET-EVENT'})
        external = str(self.project.parent / 'unread-artifact.txt')
        data['requests']['plan-1']['result'] = {'artifact_hashes': {external: 'a' * 64}, 'secret': 'SECRET-RESULT'}
        path.write_text(json.dumps(data))
        before = self.snapshot()
        detail = qa_tasks.task_get(self.project, 'QA-123')
        self.assertEqual('existing-owner', detail['task']['requests'][0]['owner_task_id'])
        self.assertEqual(['plan-1'], detail['task']['active_request_ids'])
        self.assertEqual(['TC-1'], detail['sources'][0]['scenario_ids'])
        self.assertEqual({external: 'a' * 64}, detail['sources'][0]['artifact_hashes'])
        serialized = json.dumps(detail)
        for secret in (delivery, 'SECRET-EXTRA', 'SECRET-EVENT', 'SECRET-RESULT', 'delivery_id', 'attempt_token'):
            self.assertNotIn(secret, serialized)
        self.assertEqual(before, self.snapshot())

    def test_completed_filter_pagination_and_chronological_sort(self):
        first = self.registry('QA-1')
        second = self.registry('QA-2')
        third = self.registry('QA-3')
        self.change(first, updated_at='2026-10-14T10:00:00+03:00')
        self.change(second, updated_at='2026-10-14T08:00:00+00:00')
        self.change(third, updated_at='2026-10-15T08:00:00+00:00', execution_status='completed')
        page = qa_tasks.tasks_list(self.project, limit=1)
        self.assertEqual(2, page['total'])
        self.assertEqual('QA-2', page['tasks'][0]['task_id'])
        self.assertEqual(1, page['next_offset'])
        last = qa_tasks.tasks_list(self.project, limit=1, offset=page['next_offset'])
        self.assertEqual('QA-1', last['tasks'][0]['task_id'])
        self.assertIsNone(last['next_offset'])
        self.assertEqual('QA-3', qa_tasks.tasks_list(self.project, include_completed=True)['tasks'][0]['task_id'])
        self.assertEqual('completed', qa_tasks.task_get(self.project, 'QA-3')['task']['execution_status'])

    def test_directory_name_is_not_task_id_and_projects_are_isolated(self):
        self.registry(directory='old-folder-name')
        self.assertEqual('QA-123', qa_tasks.task_get(self.project, 'QA-123')['task']['task_id'])
        other = self.project.parent / 'other'; other.mkdir()
        self.assertEqual([], qa_tasks.tasks_list(other)['tasks'])

    def test_bad_registry_warns_without_hiding_valid_tasks_or_echoing_content(self):
        self.registry('QA-VALID')
        bad = self.project / qa_tasks.TASKS / 'bad/task.json'; bad.parent.mkdir()
        bad.write_text('SECRET-INVALID-DATA')
        result = qa_tasks.tasks_list(self.project)
        self.assertEqual('partial', result['status'])
        self.assertEqual(['QA-VALID'], [t['task_id'] for t in result['tasks']])
        self.assertEqual(str(bad), result['warnings'][0]['state_path'])
        self.assertNotIn('SECRET-INVALID-DATA', json.dumps(result))
        self.assertEqual('QA-VALID', qa_tasks.task_get(self.project, 'QA-VALID')['task']['task_id'])

    def test_duplicate_id_is_reported_and_never_arbitrarily_selected(self):
        self.registry(directory='first')
        self.registry(directory='second')
        result = qa_tasks.tasks_list(self.project)
        self.assertEqual('partial', result['status'])
        self.assertTrue(any(w.get('task_id') == 'QA-123' for w in result['warnings']))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            qa_tasks.task_get(self.project, 'QA-123')

    def test_missing_task_and_invalid_ids_do_not_create_state(self):
        for tid in ('QA-MISSING', '../outside', '', '..', '/tmp', 'bad\nID'):
            with self.subTest(tid=tid), self.assertRaises(ValueError):
                qa_tasks.task_get(self.project, tid)
        self.assertEqual({}, self.snapshot())

    def test_nonstandard_registry_is_not_implicitly_scanned(self):
        outside = self.project / 'artifacts/task.json'
        exchange.init_task(outside, 'QA-OLD', 'Legacy explicit path')
        before = self.snapshot()
        self.assertEqual([], qa_tasks.tasks_list(self.project)['tasks'])
        with self.assertRaisesRegex(ValueError, 'not found'):
            qa_tasks.task_get(self.project, 'QA-OLD')
        self.assertEqual(before, self.snapshot())

    def test_symlinked_task_and_file_are_not_followed(self):
        path = self.registry()
        actual = path.with_name('actual.json'); path.rename(actual); path.symlink_to(actual)
        result = qa_tasks.tasks_list(self.project)
        self.assertEqual([], result['tasks'])
        self.assertEqual('partial', result['status'])
        path.unlink(); actual.rename(path)
        linked = path.parent.with_name('linked'); linked.symlink_to(path.parent, target_is_directory=True)
        result = qa_tasks.tasks_list(self.project)
        self.assertEqual(1, len(result['tasks']))
        self.assertTrue(any(w['reason'] == 'symlinked entry' for w in result['warnings']))

    def test_symlinked_root_and_regular_file_parent_are_errors(self):
        outside = self.project.parent / 'external'; outside.mkdir()
        parent = self.project / '.qa-agent-state'
        parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            qa_tasks.tasks_list(self.project)
        parent.unlink(); parent.write_text('Not a directory')
        with self.assertRaises(OSError):
            qa_tasks.tasks_list(self.project)

    def test_fifo_registry_is_rejected_without_blocking(self):
        path = self.registry(); path.unlink(); os.mkfifo(path)
        result = qa_tasks.tasks_list(self.project)
        self.assertEqual('partial', result['status'])
        self.assertEqual([], result['tasks'])

    def test_parent_swap_after_open_does_not_read_external_registry(self):
        path = self.registry()
        external = self.project.parent / 'external'; external.mkdir()
        (external / 'task.json').write_text('SECRET-EXTERNAL')
        original = os.open
        moved = path.parent.with_name('moved')
        def swap(name, flags, *args, **kwargs):
            if name == 'task.json':
                path.parent.rename(moved)
                path.parent.symlink_to(external, target_is_directory=True)
            return original(name, flags, *args, **kwargs)
        with patch('qa_tasks.os.open', side_effect=swap):
            result = qa_tasks.tasks_list(self.project)
        self.assertEqual('QA-123', result['tasks'][0]['task_id'])
        self.assertNotIn('SECRET-EXTERNAL', json.dumps(result))

    def test_invalid_shapes_duplicates_and_deep_json_are_warnings(self):
        path = self.registry()
        original = path.read_text()
        samples = ('[]', '{"schema_version":1,"schema_version":1}', '[' * 1500 + '0' + ']' * 1500,
                   json.dumps({**json.loads(original), 'requests': {'bad': {}}}),
                   json.dumps({**json.loads(original), 'revision': True}),
                   json.dumps({**json.loads(original), 'active_request_ids': ['missing']}),
                   json.dumps({**json.loads(original), 'updated_at': '2026-10-14'}),
                   json.dumps({**json.loads(original), 'updated_at': '0001-01-01T00:00:00+01:00'}),
                   json.dumps({**json.loads(original), 'title': '\ud800'}))
        for sample in samples:
            with self.subTest(sample=sample[:80]):
                path.write_text(sample)
                result = qa_tasks.tasks_list(self.project)
                self.assertEqual('partial', result['status'])
                self.assertEqual([], result['tasks'])

    def test_file_and_scan_limits_fail_without_writes(self):
        path = self.registry(); before = self.snapshot()
        with patch('qa_tasks.MAX_STATE_BYTES', 16):
            self.assertEqual('partial', qa_tasks.tasks_list(self.project)['status'])
        with patch('qa_tasks.MAX_ENTRIES', 0), self.assertRaisesRegex(ValueError, 'entries'):
            qa_tasks.tasks_list(self.project)
        with patch('qa_tasks.time.monotonic', side_effect=[0, 11]), self.assertRaisesRegex(ValueError, '10 seconds'):
            qa_tasks.tasks_list(self.project)
        self.assertEqual(before, self.snapshot())

    def test_project_and_pagination_validation(self):
        for root in (Path('.'), self.project / 'missing', ROOT):
            with self.subTest(root=root), self.assertRaises(ValueError):
                qa_tasks.tasks_list(root)
        for kwargs in ({'limit': True}, {'limit': 0}, {'limit': 101}, {'offset': -1},
                       {'offset': True}, {'include_completed': 'yes'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                qa_tasks.tasks_list(self.project, **kwargs)

    def test_cli_reads_from_foreign_cwd_without_mutation(self):
        self.registry()
        before = self.snapshot()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/qa_tasks.py'),
                                 '--project-root', str(self.project), 'list'], cwd=self.project.parent,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, json.loads(result.stdout)['total'])
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
