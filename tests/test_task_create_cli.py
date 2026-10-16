"""Project task creation through Python API and CLI."""
from concurrent.futures import ThreadPoolExecutor
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
import qa_task_create as creator
from qa_project import CONFIG, initialize
import qa_tasks
import task_exchange as exchange


class TaskCreateCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / 'product with пробелами'
        self.project.mkdir()
        initialize(self.project)
        self.criteria = {'schema_version': 1, 'criteria': [{'criterion_id': 'C-1',
            'description': 'Проверить требования', 'source': 'qa/requirements/source.md', 'required': True}]}
        self.arguments = dict(project_root=str(self.project), task_id='QA-123', title='Проверка заказов',
                              criteria=self.criteria)
        self.path = self.project / '.qa-agent-state/tasks/QA-123/task.json'

    def snapshot(self):
        return {str(p.relative_to(self.project)): p.read_bytes()
                for p in self.project.rglob('*') if p.is_file() and not p.is_symlink()}

    def create(self, **updates):
        return creator.task_create(**{**self.arguments, **updates})

    def failure(self, **updates):
        with self.assertRaises((ValueError, OSError, TypeError)) as caught:
            self.create(**updates)
        return str(caught.exception)

    def test_create_and_discover_without_any_child_process_or_package_writes(self):
        before = self.snapshot()
        with patch('subprocess.Popen', side_effect=AssertionError('Shell is unavailable')):
            result = self.create()
            listing = qa_tasks.tasks_list(self.project)
            detail = qa_tasks.task_get(self.project, 'QA-123')
        self.assertEqual('created', result['status'])
        self.assertFalse(result['execution_started'])
        self.assertEqual('queued', result['task']['execution_status'])
        self.assertEqual([], result['task']['requests'])
        self.assertEqual(str(self.path), listing['tasks'][0]['state_path'])
        self.assertEqual(self.criteria['criteria'][0]['source'], detail['task']['criteria'][0]['source'])
        self.assertEqual({'.qa-agent-state/tasks/QA-123/task.json',
                          '.qa-agent-state/tasks/QA-123/task.json.lock'}, set(self.snapshot()) - set(before))
        for name, data in before.items():
            self.assertEqual(data, self.snapshot()[name])
        self.assertFalse((ROOT / '.qa-agent-state/tasks/QA-123/task.json').exists())

    def test_both_cli_entrypoints_create_the_same_contract(self):
        self.create()
        native = exchange.read_state(self.path)
        cli = exchange.init_task(self.root / 'cli/task.json', 'QA-123', self.arguments['title'], self.criteria)
        for name in ('created_at', 'updated_at', 'trace_id'):
            native.pop(name)
            cli.pop(name)
        self.assertEqual(cli, native)
        self.assertEqual('criteria', native['acceptance_mode'])

    def assign(self):
        return exchange.assign(self.path, dict(task_id='QA-123', request_id='plan-1', agent='qa-test-plan-writer',
            stage='plan', goal='План заказов', acceptance_criteria=['Карта требований'], criterion_ids=['C-1'],
            stop_condition='Нет источника ожиданий', expected_artifact='JSON отчёт',
            result_path=str(self.project / 'result.json')), self.project, 1)

    def test_retry_preserves_advanced_registry_and_never_restarts_work(self):
        self.create()
        self.assign()
        before = self.snapshot()
        result = self.create()
        self.assertEqual('existing', result['status'])
        self.assertEqual(2, result['task']['revision'])
        self.assertEqual(['plan-1'], result['task']['active_request_ids'])
        self.assertEqual(before, self.snapshot())

    def test_different_title_or_criteria_never_overwrite_existing_registry(self):
        self.create()
        before = self.snapshot()
        for change in ({'title': 'Different task'}, {'criteria': {'schema_version': 1, 'criteria': [{
                **self.criteria['criteria'][0], 'description': 'Different scope'}]}}):
            with self.subTest(change=change):
                self.assertIn('existing task identity or criteria differ', self.failure(**change))
                self.assertEqual(before, self.snapshot())

    def test_invalid_criteria_refuse_before_creating_state_directories(self):
        before = self.snapshot()
        for packet in ({}, {'schema_version': True, 'criteria': []}, {'schema_version': 1, 'criteria': []},
                       {'schema_version': 1, 'criteria': [{**self.criteria['criteria'][0], 'required': 'yes'}]},
                       {'schema_version': 1, 'criteria': self.criteria['criteria'] * 2}):
            with self.subTest(packet=packet):
                self.failure(criteria=packet)
                self.assertEqual(before, self.snapshot())
                self.assertFalse((self.project / '.qa-agent-state').exists())

    def test_missing_or_stale_binding_cannot_create_task(self):
        binding = json.loads((self.project / CONFIG).read_text())
        for change in ({'package_version': '0.0.1'}, {'package_root': str(self.root / 'other-package')}, None):
            with self.subTest(change=change):
                if change is None:
                    (self.project / CONFIG).unlink()
                else:
                    (self.project / CONFIG).write_text(json.dumps({**binding, **change}))
                before = self.snapshot()
                self.failure()
                self.assertEqual(before, self.snapshot())
                self.assertFalse((self.project / '.qa-agent-state').exists())

    def test_path_traversal_and_invalid_ids_never_write_outside_standard_directory(self):
        before = self.snapshot()
        for task_id in ('../escaped', '/tmp/escaped', 'nested/id', 'nested\\id', '..', '.', ' QA-1', 'QA-1 ',
                        'QA\x00bad', 'QA\nbad', 'Q' * 129):
            with self.subTest(task_id=task_id):
                self.failure(task_id=task_id)
                self.assertEqual(before, self.snapshot())

    def test_api_rejects_arbitrary_paths_scripts_and_invalid_arguments(self):
        before = self.snapshot()
        for change in ({'state': '/tmp/arbitrary.json'}, {'script': 'manage_task.py'}, {'argv': []},
                       {'legacy_no_criteria': True}, {'title': ''}, {'criteria': None}):
            with self.subTest(change=change):
                self.failure(**change)
                self.assertEqual(before, self.snapshot())

    def test_duplicate_task_id_in_legacy_directory_is_not_created_again(self):
        legacy = self.project / qa_tasks.TASKS / 'old-location/task.json'
        exchange.init_task(legacy, 'QA-123', self.arguments['title'], self.criteria)
        before = self.snapshot()
        self.failure()
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.path.parent.exists())

    def test_incomplete_discovery_is_not_treated_as_absence_of_tasks(self):
        broken = self.project / qa_tasks.TASKS / 'broken/task.json'
        broken.parent.mkdir(parents=True)
        broken.write_text('{')
        before = self.snapshot()
        self.failure()
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.path.parent.exists())

    def test_symlinks_in_state_directories_registry_or_lock_cannot_escape_project(self):
        external = self.root / 'outside'
        external.mkdir()
        for relative in ('.qa-agent-state', '.qa-agent-state/tasks', '.qa-agent-state/tasks/QA-123',
                         '.qa-agent-state/tasks/QA-123/task.json', '.qa-agent-state/tasks/QA-123/task.json.lock'):
            with self.subTest(relative=relative):
                target = self.project / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(external / 'missing' if relative.endswith(('.json', '.lock')) else external)
                self.failure()
                self.assertEqual([], list(external.iterdir()))
                target.unlink()

    def test_fifo_registry_and_lock_are_rejected_without_blocking(self):
        self.path.parent.mkdir(parents=True)
        for path in (self.path, self.path.with_name('task.json.lock')):
            with self.subTest(path=path):
                os.mkfifo(path)
                self.failure()
                self.assertTrue(path.exists())
                path.unlink()

    def test_parent_swapped_for_symlink_before_publication_cannot_receive_registry(self):
        original = creator.atomic_text
        outside = self.root / 'displaced'
        def publish(path, text, **kwargs):
            self.path.parent.rename(outside)
            self.path.parent.symlink_to(outside)
            return original(path, text, **kwargs)
        with patch('qa_task_create.atomic_text', side_effect=publish):
            self.failure()
        self.assertFalse((outside / 'task.json').exists())

    def test_competing_noncooperating_writer_is_never_overwritten(self):
        original = creator.atomic_text
        for same in (True, False):
            with self.subTest(same=same):
                if self.path.exists():
                    self.path.unlink()
                def publish(path, text, **kwargs):
                    state = exchange.new_task_state('QA-123', self.arguments['title'] if same else 'Other task', self.criteria)
                    original(path, json.dumps(state), replace=False)
                    return original(path, text, **kwargs)
                with patch('qa_task_create.atomic_text', side_effect=publish):
                    if same:
                        self.assertEqual('existing', self.create()['status'])
                    else:
                        self.failure()
                self.assertEqual(self.arguments['title'] if same else 'Other task', exchange.read_state(self.path)['title'])

    def test_two_simultaneous_calls_create_exactly_one_registry(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: creator.task_create(**self.arguments), range(2)))
        self.assertEqual(['created', 'existing'], sorted(row['status'] for row in results))
        self.assertEqual(1, exchange.read_state(self.path)['revision'])
        self.assertEqual(1, qa_tasks.tasks_list(self.project)['total'])

    def test_interrupted_creation_with_only_directory_and_lock_can_be_retried(self):
        self.path.parent.mkdir(parents=True)
        self.path.with_name('task.json.lock').touch()
        self.assertEqual('created', self.create()['status'])
        self.assertEqual(1, exchange.read_state(self.path)['revision'])

    def test_new_task_does_not_exceed_discovery_capacity(self):
        self.create()
        with patch('qa_task_create.MAX_ENTRIES', 1):
            self.assertEqual('existing', self.create()['status'])
            self.failure(task_id='QA-SECOND')
        self.assertFalse((self.project / qa_tasks.TASKS / 'QA-SECOND').exists())

    def test_oversized_initial_state_is_rejected_before_publication(self):
        before = self.snapshot()
        with patch('qa_task_create.MAX_STATE_BYTES', 100):
            self.failure()
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.project / '.qa-agent-state').exists())

    def test_cli_from_foreign_cwd_and_retry(self):
        criteria = self.root / 'criteria.json'
        criteria.write_text(json.dumps(self.criteria))
        command = [sys.executable, '-B', str(ROOT / 'scripts/qa_task_create.py'),
                   '--project-root', str(self.project), '--task-id', 'QA-123',
                   '--title', self.arguments['title'], '--criteria', str(criteria)]
        for status in ('created', 'existing'):
            result = subprocess.run(command, cwd=self.root, text=True, capture_output=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(status, json.loads(result.stdout)['status'])
        self.assertFalse((self.root / '.qa-agent-state').exists())


if __name__ == '__main__':
    unittest.main()
