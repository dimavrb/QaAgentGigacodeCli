"""Managed project roles: vendor baselines, per-file decisions and replayable writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets

from qa_files import atomic_text, directory, read_bytes

AGENTS = Path('.gigacode/agents')
STATE = Path('.qa-agent/agents.local.json')
DATA = Path('.qa-agent/agent-updates')
NAME = re.compile(r'qa-[a-z0-9-]+\.md')
SHA = re.compile(r'[a-f0-9]{64}')
VERSION = re.compile(r'\d+\.\d+\.\d+')


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def optional_bytes(path):
    try:
        return read_bytes(path)
    except FileNotFoundError:
        return None


def package_roles(package):
    roles = {}
    for path in sorted((package / AGENTS).glob('qa-*.md')):
        if not NAME.fullmatch(path.name):
            raise ValueError('invalid QA role name: ' + path.name)
        data = read_bytes(path)
        data.decode('utf-8')
        roles[path.name] = data
    return roles


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate managed roles key: ' + key)
        result[key] = value
    return result


def valid_base(value):
    return (isinstance(value, dict) and set(value) == {'sha256', 'version'}
            and isinstance(value['sha256'], str) and SHA.fullmatch(value['sha256'])
            and isinstance(value['version'], str) and VERSION.fullmatch(value['version']))


def blob(project, sha):
    data = read_bytes(project / DATA / 'blobs' / (sha + '.md'))
    if digest(data) != sha:
        raise ValueError('managed role baseline/blob checksum differs: ' + sha)
    data.decode('utf-8')
    return data


def store_blob(project, data):
    sha = digest(data)
    path = project / DATA / 'blobs' / (sha + '.md')
    if not atomic_text(path, data.decode('utf-8'), replace=False) and read_bytes(path) != data:
        raise ValueError('managed role blob already exists with different contents')
    return sha


def load_state(project):
    data = optional_bytes(project / STATE)
    if data is None:
        return None
    try:
        state = json.loads(data, object_pairs_hook=unique)
    except RecursionError as exc:
        raise ValueError('managed roles state nesting exceeds the supported limit') from exc
    if (not isinstance(state, dict)
            or set(state) != {'schema_version', 'project_root', 'package_root', 'package_version',
                              'roles', 'pending', 'last_transaction'}
            or type(state['schema_version']) is not int or state['schema_version'] != 1
            or state['project_root'] != str(project)
            or not isinstance(state['package_root'], str) or not Path(state['package_root']).is_absolute()
            or (state['package_version'] is not None
                and (not isinstance(state['package_version'], str) or not VERSION.fullmatch(state['package_version'])))
            or not isinstance(state['roles'], dict)
            or (state['last_transaction'] is not None
                and (not isinstance(state['last_transaction'], str)
                     or not re.fullmatch(r'[a-f0-9]{32}', state['last_transaction'])))):
        raise ValueError('invalid managed project roles state')
    for name, base in state['roles'].items():
        if not NAME.fullmatch(name) or not valid_base(base):
            raise ValueError('invalid managed role baseline record')
        blob(project, base['sha256'])
    pending = state['pending']
    if pending is not None:
        if (not isinstance(pending, dict) or set(pending) != {'id', 'version', 'operations'}
                or not isinstance(pending['id'], str) or not re.fullmatch(r'[a-f0-9]{32}', pending['id'])
                or not isinstance(pending['version'], str) or not VERSION.fullmatch(pending['version'])
                or not isinstance(pending['operations'], list)):
            raise ValueError('invalid managed roles transaction')
        names = set()
        for op in pending['operations']:
            if (not isinstance(op, dict) or set(op) != {'name', 'before', 'after', 'base_after', 'action'}
                    or not isinstance(op['name'], str) or not NAME.fullmatch(op['name']) or op['name'] in names
                    or op['action'] not in ('add', 'update', 'adopt', 'accept_local', 'retire', 'forget', 'restore')
                    or any(sha is not None and (not isinstance(sha, str) or not SHA.fullmatch(sha))
                           for sha in (op['before'], op['after']))
                    or (op['base_after'] is not None and not valid_base(op['base_after']))):
                raise ValueError('invalid managed roles transaction operation')
            names.add(op['name'])
            for sha in (op['before'], op['after'], (op['base_after'] or {}).get('sha256')):
                if sha is not None:
                    blob(project, sha)
            if op['base_after'] is None and op['after'] is not None:
                raise ValueError('retired managed role must not publish a payload')
    return state


def save_state(project, state):
    atomic_text(project / STATE, json_text(state))


def publish_role(project, op):
    """Project directory lock coordinates writers; recheck bytes before publishing."""
    target = project / AGENTS / op['name']
    if digest(optional_bytes(target)) != op['before']:
        return False
    if op['after'] is None:
        if op['before'] is not None:
            with directory(target.parent) as parent:
                os.unlink(target.name, dir_fd=parent)
                os.fsync(parent)
    else:
        data = blob(project, op['after'])
        if not atomic_text(target, data.decode('utf-8'), replace=op['before'] is not None):
            return False
    return True


def replay(project, state):
    pending = state['pending']
    changed, conflicts = [], []
    for op in pending['operations']:
        local = digest(optional_bytes(project / AGENTS / op['name']))
        if local != op['after']:
            if local != op['before'] or not publish_role(project, op):
                conflicts.append(op['name'])
                continue
            changed.append((AGENTS / op['name']).as_posix())
        if op['base_after'] is None:
            state['roles'].pop(op['name'], None)
        else:
            state['roles'][op['name']] = op['base_after']
    state['pending'] = None
    state['last_transaction'] = pending['id']
    save_state(project, state)
    return changed, conflicts


def sync_roles(project, package, version, payloads, *, apply=False, managed_only=False,
               accept_local=None, restore=None):
    """Caller holds the project lock. Preview never creates state or directories."""
    accept_local, restore = accept_local or {}, set(restore or ())
    if (not VERSION.fullmatch(version) or not payloads
            or any(not NAME.fullmatch(name) or not isinstance(data, bytes) for name, data in payloads.items())):
        raise ValueError('invalid incoming managed QA roles')
    for data in payloads.values():
        data.decode('utf-8')
    state = load_state(project)
    local_files = {name: optional_bytes(project / AGENTS / name)
                   for name in sorted(set(payloads) | set((state or {}).get('roles', {})))}
    if state is None and managed_only:
        legacy = [name for name, data in local_files.items() if data is not None]
        return {'status': 'needs_attention' if legacy else 'unmanaged', 'needs_sync': bool(legacy),
                'changed_files': [], 'planned_files': [], 'actions': [], 'adapted_roles': [],
                'conflicts': [(AGENTS / name).as_posix() for name in legacy],
                'conflict_details': [{'name': name, 'reason': 'baseline_missing_run_explicit_apply'} for name in legacy],
                'pending': False, 'state_file': str(project / STATE)}
    if state is None:
        state = {'schema_version': 1, 'project_root': str(project), 'package_root': str(package),
                 'package_version': None, 'roles': {}, 'pending': None, 'last_transaction': None}
    recovered, recovered_conflicts = [], []
    pending_id = None
    if state['pending']:
        pending_id = state['pending']['id']
        if not apply:
            return {'status': 'needs_attention', 'needs_sync': True, 'pending': True,
                    'changed_files': [], 'planned_files': [], 'actions': [], 'adapted_roles': [],
                    'conflicts': [], 'conflict_details': [], 'state_file': str(project / STATE)}
        recovered, recovered_conflicts = replay(project, state)
        local_files = {name: optional_bytes(project / AGENTS / name)
                       for name in sorted(set(payloads) | set(state['roles']))}
    unknown = (set(accept_local) | restore) - set(local_files)
    if unknown or set(accept_local) & restore:
        raise ValueError('resolution names are unknown or overlap: ' + ', '.join(sorted(unknown or set(accept_local) & restore)))
    operations, details, adapted = [], [], []
    for name, local in local_files.items():
        incoming = payloads.get(name)
        old = state['roles'].get(name)
        before, after = digest(local), digest(incoming)
        base = old['sha256'] if old else None
        record = {'sha256': after, 'version': version} if incoming is not None else None
        action, reason = None, None
        target = after
        if name in accept_local:
            if local is None or incoming is None or tuple(accept_local[name]) != (before, after):
                raise ValueError('reviewed local/incoming hashes changed for ' + name)
            action, target = 'accept_local', before
        elif name in restore:
            if old is None or local is not None or incoming is None:
                raise ValueError('--restore requires a deleted managed role: ' + name)
            action = 'restore'
        elif incoming is None:
            if local is None:
                action = 'forget'
            elif before == base:
                action = 'retire'
            else:
                reason = 'removed_upstream_with_local_changes'
        elif before == after:
            if old != record:
                action = 'adopt'
        elif old is None:
            if local is None:
                action = 'add'
            else:
                reason = 'unknown_baseline'
        elif local is None:
            reason = 'deleted_locally'
        elif before == base:
            action = 'update'
        elif after == base:
            adapted.append(name)
        else:
            reason = 'both_changed'
        if reason:
            details.append({'name': name, 'reason': reason, 'base_sha256': base,
                            'local_sha256': before, 'incoming_sha256': after,
                            'base_file': str(project / DATA / 'blobs' / (base + '.md')) if base else None,
                            'incoming_file': str(package / AGENTS / name) if incoming is not None else None})
        elif action:
            operations.append({'name': name, 'before': before, 'after': target,
                               'base_after': record, 'action': action})
    if recovered_conflicts:
        for name in recovered_conflicts:
            if name not in {item['name'] for item in details}:
                details.append({'name': name, 'reason': 'changed_during_recovery'})
        operations = [op for op in operations if op['name'] not in recovered_conflicts]
    planned = [(AGENTS / op['name']).as_posix() for op in operations if op['before'] != op['after']]
    changed = list(recovered)
    if apply and operations:
        transaction = {'id': secrets.token_hex(16), 'version': version, 'operations': operations}
        for op in operations:
            name = op['name']
            for data in (local_files[name], payloads.get(name)):
                if data is not None:
                    store_blob(project, data)
            if local_files[name] is not None:
                backup = project / DATA / 'backups' / transaction['id'] / name
                atomic_text(backup, local_files[name].decode('utf-8'), replace=False)
        atomic_text(project / DATA / 'transactions' / (transaction['id'] + '.json'),
                    json_text({'project_root': str(project), 'package_root': str(package),
                               'roles_before': state['roles'], **transaction}), replace=False)
        state['pending'] = transaction
        save_state(project, state)
        written, late_conflicts = replay(project, state)
        changed.extend(written)
        for name in late_conflicts:
            details.append({'name': name, 'reason': 'changed_during_installation'})
    complete = not details
    needs_sync = bool(operations or pending_id or state['package_version'] != version or state['package_root'] != str(package))
    if apply:
        previous = json_text(state)
        state['package_root'] = str(package)
        if complete:
            state['package_version'] = version
        if optional_bytes(project / STATE) is None or json_text(state) != previous:
            save_state(project, state)
    return {'status': 'needs_attention' if details else 'installed' if apply else 'preview',
            'needs_sync': needs_sync if not apply else not complete, 'pending': False,
            'changed_files': changed, 'planned_files': planned, 'actions': operations,
            'conflicts': [(AGENTS / item['name']).as_posix() for item in details],
            'conflict_details': details, 'adapted_roles': adapted,
            'state_file': str(project / STATE), 'recovered_transaction': pending_id}
