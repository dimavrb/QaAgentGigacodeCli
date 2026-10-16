"""Create or find a project's standard QA task registry, preserving existing work."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import stat
import time
from pathlib import Path
import sys

from qa_files import atomic_text, directory, project_lock
from qa_project import CONFIG, package_info, read_binding
from qa_tasks import MAX_ENTRIES, MAX_STATE_BYTES, TASKS, identifier, project_path, read_snapshot, scan, snapshot_summary
from task_exchange import new_task_state


@contextmanager
def registry_lock(project, relative):
    # Use the CLI's lock name so creation also coordinates with manage_task.py.
    with directory(project / relative.parent, create=True) as parent:
        descriptor = os.open(relative.name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError('task lock must be a regular file')
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ValueError('task registry is busy; retry after the other writer finishes')
                    time.sleep(0.05)
            os.fsync(parent)
            yield
        finally:
            os.close(descriptor)


def task_create(project_root, task_id, title, criteria):
    project, task_id = project_path(project_root), identifier(task_id)
    if task_id != task_id.strip():
        raise ValueError('task_id must not contain surrounding whitespace')
    if not isinstance(title, str) or not title.strip() or len(title) > 1000:
        raise ValueError('title must contain 1..1000 characters')
    if not isinstance(criteria, dict):
        raise ValueError('criteria must be an agreed schema_version=1 packet')
    # Validate all criteria before creating even a directory or lock file.
    proposed = new_task_state(task_id, title, criteria)
    payload = json.dumps(proposed, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if len(payload.encode('utf-8')) > MAX_STATE_BYTES:
        raise ValueError('initial task state exceeds the supported 4 MiB limit')
    relative = TASKS / task_id / 'task.json'
    info = package_info()
    with project_lock(project):
        binding = read_binding(project / CONFIG, project)
        if (binding['package_root'] != info['package_root']
                or binding['package_version'] != info['package_version']):
            raise ValueError('project binding differs from the installed package; run /qa:init first')
        try:
            read_snapshot(project, relative)
            missing = False
        except FileNotFoundError:
            missing = True
        snapshots, warnings = scan(project)
        # An interrupted first creation can leave its directory/lock without task.json.
        warnings = [warning for warning in warnings if not (
            missing and warning.get('state_path') == str(project / relative)
            and warning.get('reason') == 'unreadable or invalid task state')]
        if warnings:
            raise ValueError('task discovery is incomplete; resolve registry warnings before creating a task')
        matches = [summary for summary, _ in snapshots if summary['task_id'] == task_id]
        if matches and (len(matches) != 1 or matches[0]['state_path'] != str(project / relative)):
            raise ValueError('task ID already exists outside its standard directory; use qa_tasks.py get')
        if not matches and len(snapshots) >= MAX_ENTRIES:
            raise ValueError('task archive is full; archive old tasks explicitly before creating another')
        with registry_lock(project, relative):
            # No overwrite: even a writer that ignores locks cannot lose its registry.
            try:
                state, sha = read_snapshot(project, relative)
                created = False
            except FileNotFoundError:
                created = atomic_text(project / relative, payload, replace=False)
                state, sha = read_snapshot(project, relative)
            summary = snapshot_summary(state, project, relative, sha)
            if (state['task_id'] != task_id or state['title'] != proposed['title']
                    or state.get('acceptance_mode') != 'criteria'
                    or {key: row['definition'] for key, row in state.get('criteria', {}).items()}
                    != {key: row['definition'] for key, row in proposed['criteria'].items()}):
                raise ValueError('existing task identity or criteria differ; inspect qa_tasks.py get before changing scope')
    return {'status': 'created' if created else 'existing', 'project_root': str(project), 'task': summary,
            'execution_started': False, 'external_systems_configured': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path.cwd())
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--criteria', type=Path, required=True)
    args = parser.parse_args()
    try:
        from qa_project import read_json
        result = task_create(str(args.project_root.resolve()), args.task_id, args.title, read_json(args.criteria))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f'QA task creation error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
