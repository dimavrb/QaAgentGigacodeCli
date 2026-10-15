#!/usr/bin/env python3
"""Read project QA task snapshots without writes, subprocesses or artifact execution."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from qa_project import ROOT

TASKS = Path('.qa-agent-state/tasks')
MAX_STATE_BYTES = 4 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_SCAN_SECONDS = 10
REQUEST_STATUSES = {'queued', 'dispatching', 'running', 'reported', 'accepted', 'changes_requested', 'failed'}


def project_path(value):
    project = Path(value)
    if not project.is_absolute():
        raise ValueError('project_root must be an absolute path')
    project = project.resolve()
    if not project.is_dir() or project == ROOT or ROOT in project.parents:
        raise ValueError('project_root must be an existing directory outside the installed package')
    return project


@contextmanager
def directory(project, relative):
    """Walk via directory descriptors so a swapped parent cannot escape the project."""
    descriptors = []
    try:
        descriptors.append(os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for component in Path(relative).parts:
            descriptors.append(os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=descriptors[-1]))
        yield descriptors[-1]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def read_snapshot(project, relative):
    with directory(project, relative.parent) as parent:
        descriptor = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
                raise ValueError('task state must be a regular file of at most 4 MiB')
            data = stream.read(MAX_STATE_BYTES + 1)
            if len(data) > MAX_STATE_BYTES:
                raise ValueError('task state exceeds 4 MiB')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key in task state')
            result[key] = value
        return result
    state = json.loads(data.decode('utf-8'), object_pairs_hook=unique)
    return state, hashlib.sha256(data).hexdigest()


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('invalid task text field')
    value.encode('utf-8')
    return value


def identifier(value):
    value = text(value)
    if (len(value) > 128 or value in ('.', '..') or any(c in '/\\' or ord(c) < 32 for c in value)):
        raise ValueError('invalid task ID')
    return value


def strings(value):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError('invalid task string list')
    return value


def references(rid, row):
    assignment = row['assignment']
    result = row.get('result')
    if result is not None and not isinstance(result, dict):
        raise ValueError('invalid saved result reference')
    hashes = {}
    for field, value in (('input_hashes', row.get('input_hashes', {})),
                         ('artifact_hashes', result.get('artifact_hashes', {}) if result else {})):
        if (not isinstance(value, dict) or any(not isinstance(path, str) or not path
                or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
                for path, digest in value.items())):
            raise ValueError('invalid recorded artifact hashes')
        hashes[field] = dict(value)
    return {'request_id': rid, 'inputs': strings(assignment.get('inputs', [])),
            'depends_on': strings(assignment.get('depends_on', [])),
            'scenario_ids': strings(assignment.get('scenario_ids', [])), **hashes,
            **({'presentation': presentation_reference(row['presentation'])} if 'presentation' in row else {}),
            **{key: text(assignment[key]) for key in ('investigation_state', 'stub_assignment')
               if key in assignment}}


def presentation_reference(value):
    if not isinstance(value, dict) or value.get('status') not in ('ready', 'needs_attention'):
        raise ValueError('invalid HTML presentation reference')
    if value['status'] == 'needs_attention':
        return {'status': 'needs_attention', 'error': text(value['error'])}
    if not re.fullmatch(r'[a-f0-9]{64}', value.get('sha256', '')):
        raise ValueError('invalid HTML presentation hash')
    return {'status': 'ready', 'path': text(value['path']), 'sha256': value['sha256'],
            'plan_revision': text(value['plan_revision'])}


def snapshot_summary(state, project, relative, digest):
    """Whitelist response fields; never expose delivery/attempt tokens or raw events."""
    try:
        if (not isinstance(state, dict) or type(state.get('schema_version')) is not int
                or state['schema_version'] != 1 or type(state.get('revision')) is not int
                or state['revision'] < 1 or not isinstance(state.get('requests'), dict)
                or not isinstance(state.get('questions'), dict) or not isinstance(state.get('events'), list)):
            raise ValueError('unsupported task state')
        task_id = identifier(state['task_id'])
        execution = state['execution_status']
        if execution not in ('queued', 'running', 'waiting', 'completed'):
            raise ValueError('invalid task execution status')
        review = state['review_status']
        if review not in ('not_required', 'pending', 'passed', 'changes_requested'):
            raise ValueError('invalid task review status')
        updated = datetime.fromisoformat(text(state['updated_at']))
        if updated.tzinfo is None:
            raise ValueError('updated_at must include a timezone')
        requests = []
        for rid, row in state['requests'].items():
            assignment = row['assignment']
            if (not isinstance(assignment, dict) or assignment['task_id'] != task_id
                    or assignment['request_id'] != rid or row['status'] not in REQUEST_STATUSES):
                raise ValueError('invalid task assignment')
            owner = row['owner_task_id']
            if owner is not None:
                text(owner)
            references(rid, row)
            requests.append({'request_id': text(rid), 'agent': text(assignment['agent']),
                             'stage': text(assignment['stage']), 'status': row['status'],
                             'owner_task_id': owner, 'result_path': text(assignment['result_path'])})
        active = strings(state['active_request_ids'])
        if set(active) != {r['request_id'] for r in requests if r['status'] in ('queued', 'dispatching', 'running')}:
            raise ValueError('inconsistent active assignments')
        criteria = []
        for cid, row in state.get('criteria', {}).items():
            definition, assessment = row['definition'], row['assessment']
            status = assessment['status'] if assessment else 'unknown'
            if type(definition['required']) is not bool or status not in ('verified', 'failed', 'unknown', 'not_applicable'):
                raise ValueError('invalid task criterion')
            criteria.append({'criterion_id': text(cid), 'description': text(definition['description']),
                             'required': definition['required'], 'status': status,
                             **({'source': text(definition['source'])} if 'source' in definition else {})})
        questions = []
        for qid, row in state['questions'].items():
            questions.append({'questionnaire_id': text(qid), 'status': text(row['status']),
                              'answers_path': text(row['answers_path']),
                              'unresolved_question_ids': strings(row.get('unresolved_question_ids', []))})
        summary = {'task_id': task_id, 'title': text(state['title']), 'revision': state['revision'],
                   'execution_status': execution, 'review_status': review,
                   'qa_outcome': text(state['qa_outcome']) if state.get('qa_outcome') is not None else None,
                   'updated_at': updated.astimezone(timezone.utc).isoformat(),
                   'state_path': str(project / relative), 'state_sha256': digest,
                   'active_request_ids': active, 'open_question_ids': strings(state.get('open_question_ids', [])),
                   'request_count': len(requests), 'criteria': criteria,
                   'requests': requests, 'questions': questions}
        return summary
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError('incomplete or malformed task state') from exc


def scan(project):
    started = time.monotonic()
    names, warnings = [], []
    try:
        with directory(project, TASKS) as parent:
            with os.scandir(parent) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_ENTRIES:
                        raise ValueError('task directory exceeds 1000 entries; archive old tasks explicitly')
                    if entry.is_symlink():
                        warnings.append({'state_path': str(project / TASKS / entry.name), 'reason': 'symlinked entry'})
                    elif entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
    except FileNotFoundError:
        return [], []
    snapshots = []
    for name in sorted(names):
        if time.monotonic() - started > MAX_SCAN_SECONDS:
            raise ValueError('task scan exceeded 10 seconds; reduce the local task archive')
        relative = TASKS / name / 'task.json'
        try:
            state, digest = read_snapshot(project, relative)
            summary = snapshot_summary(state, project, relative, digest)
            snapshots.append((summary, state))
        except (OSError, ValueError, TypeError, RecursionError):
            # Never echo registry contents or exception text that might contain secrets.
            warnings.append({'state_path': str(project / relative), 'reason': 'unreadable or invalid task state'})
    ids = [summary['task_id'] for summary, _ in snapshots]
    duplicates = sorted({tid for tid in ids if ids.count(tid) > 1})
    for tid in duplicates:
        warnings.append({'task_id': tid, 'reason': 'duplicate task ID; select by resolving the registries first'})
    return sorted(snapshots, key=lambda row: (row[0]['updated_at'], row[0]['task_id'], row[0]['state_path']),
                  reverse=True), warnings


def tasks_list(project_root, *, include_completed=False, limit=20, offset=0):
    if type(include_completed) is not bool or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('include_completed must be boolean and limit must be 1..100')
    if type(offset) is not int or offset < 0:
        raise ValueError('offset must be a nonnegative integer')
    project = project_path(project_root)
    snapshots, warnings = scan(project)
    tasks = [summary for summary, _ in snapshots if include_completed or summary['execution_status'] != 'completed']
    page = [{**{k: v for k, v in summary.items() if k not in
                ('requests', 'questions', 'criteria', 'active_request_ids', 'open_question_ids', 'title')},
             'title': summary['title'][:400], 'title_truncated': len(summary['title']) > 400,
             'active_request_count': len(summary['active_request_ids']),
             'open_question_count': len(summary['open_question_ids'])}
            for summary in tasks[offset:offset + limit]]
    more = offset + len(page) < len(tasks)
    return {'status': 'partial' if warnings else 'ready', 'project_root': str(project),
            'tasks': page, 'total': len(tasks), 'warnings': warnings,
            'next_offset': offset + len(page) if more else None, 'snapshot_only': True}


def task_get(project_root, task_id):
    project, task_id = project_path(project_root), identifier(task_id)
    snapshots, warnings = scan(project)
    matches = [(summary, state) for summary, state in snapshots if summary['task_id'] == task_id]
    if len(matches) != 1:
        raise ValueError('task ID is ambiguous' if matches else 'task not found in the standard local registry')
    summary, state = matches[0]
    # Paths are references only: do not read/execute arbitrary artifacts from a registry.
    sources = [references(rid, row) for rid, row in state['requests'].items()]
    return {'project_root': str(project), 'task': summary, 'sources': sources, 'warnings': warnings,
            'snapshot_only': True, 'resume_ready': False,
            'resume_checks': ['Confirm the requested task and scope',
                              'Check existing owner before dispatching or repeating work',
                              'Read referenced artifacts and verify revision, hashes and current conditions',
                              'Use manage_task with expected_revision; never reset cancellation or acceptance']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--include-completed', action='store_true')
    listing.add_argument('--limit', type=int, default=20)
    listing.add_argument('--offset', type=int, default=0)
    get = commands.add_parser('get')
    get.add_argument('--task-id', required=True)
    args = parser.parse_args()
    try:
        result = (tasks_list(args.project_root, include_completed=args.include_completed, limit=args.limit, offset=args.offset)
                  if args.command == 'list' else task_get(args.project_root, args.task_id))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(f'QA task error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
