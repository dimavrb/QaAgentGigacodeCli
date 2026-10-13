"""Cooperative, local Playwright ownership; never calls MCP or kills processes."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timezone-aware timestamp required')
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError('timezone-aware timestamp required')
    return at


def text(value):
    return isinstance(value, str) and bool(value.strip())


def read_json(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('non-finite JSON number: ' + value)
    def floating(value):
        result = float(value)
        if not math.isfinite(result):
            return invalid(value)
        return result
    return json.loads(content, object_pairs_hook=pairs, parse_constant=invalid, parse_float=floating)


def resources(config):
    if not isinstance(config, dict) or type(config.get('schema_version')) is not int or config['schema_version'] != 1:
        raise ValueError('pool config schema_version must equal 1')
    entries = config.get('resources')
    if not isinstance(entries, list) or not entries:
        raise ValueError('pool config needs non-empty resources')
    servers, directories, result = set(), set(), []
    for entry in entries:
        if not isinstance(entry, dict) or not text(entry.get('server')) or entry.get('mode') not in ('isolated', 'persistent'):
            raise ValueError('resource needs server and isolated/persistent mode; external/shared browsers are unmanaged')
        if entry['server'] in servers:
            raise ValueError('duplicate MCP server')
        servers.add(entry['server'])
        item = {'server': entry['server'], 'mode': entry['mode']}
        if entry['mode'] == 'persistent':
            directory = entry.get('user_data_dir')
            if not text(directory) or not Path(directory).is_absolute():
                raise ValueError('persistent resource needs its actual absolute user_data_dir')
            if 'REPLACE_WITH_ACTUAL_PATH' in directory:
                raise ValueError('replace persistent profile placeholders with actual user_data_dir paths')
            directory = str(Path(directory).resolve())
            if directory in directories:
                raise ValueError('different servers cannot share a persistent user_data_dir')
            directories.add(directory)
            item['user_data_dir'] = directory
        elif entry.get('user_data_dir') is not None:
            raise ValueError('isolated resource must not declare a persistent directory')
        result.append(item)
    return result


class BrowserPool:
    def __init__(self, path, clock=now):
        self.path = Path(path).resolve()
        self.clock = clock

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Separate lock inode: replacing the JSON must not invalidate the lock.
        with self.path.with_suffix(self.path.suffix + '.lock').open('a+b') as lock:
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def save(self, state):
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + '.')
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(state, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def load(self):
        state = read_json(self.path.read_bytes())
        if not isinstance(state, dict) or type(state.get('schema_version')) is not int or state['schema_version'] != 1:
            raise ValueError('invalid pool state; preserve it for investigation')
        entries = state.get('resources')
        resources({'schema_version': 1, 'resources': entries})
        for item in entries:
            if item.get('status') not in ('free', 'leased', 'closing', 'quarantined', 'recovering'):
                raise ValueError('invalid resource status; do not reset the registry')
            lease = item.get('lease')
            if item['status'] == 'free' and lease is not None:
                raise ValueError('free resource still has an owner; preserve it for investigation')
            if item['status'] == 'free':
                receipt = item.get('last_release')
                if (not isinstance(receipt, dict) or not text(receipt.get('lease_id'))
                        or not isinstance(receipt.get('evidence'), dict)
                        or not text(receipt['evidence'].get('sha256'))):
                    raise ValueError('free resource has no cleanup receipt; preserve it for investigation')
            timestamp(item.get('changed_at'))
            if item['status'] in ('leased', 'closing', 'recovering') and not isinstance(lease, dict):
                raise ValueError('active resource is missing its owner')
            if lease is not None:
                if not isinstance(lease, dict) or any(not text(lease.get(k)) for k in ('lease_id', 'task_id', 'request_id', 'owner_task_id')):
                    raise ValueError('invalid lease identity')
                for key in ('acquired_at', 'expires_at'):
                    timestamp(lease.get(key))
                if type(lease.get('ttl_seconds')) is not int or not 30 <= lease['ttl_seconds'] <= 3600:
                    raise ValueError('invalid lease ttl')
            if item['status'] in ('closing', 'recovering'):
                if not text(item.get('close_id')):
                    raise ValueError('cleanup phase missing close_id')
                timestamp(item.get('closing_started_at'))
        return state

    def refresh(self, state):
        at = self.clock()
        for item in state['resources']:
            if item['status'] in ('leased', 'closing', 'recovering') and timestamp(item['lease']['expires_at']) <= at:
                item.update(status='quarantined', reason='lease expired; browser state unknown', changed_at=at.isoformat())

    def apply(self, action):
        with self.locked():
            state = self.load()
            self.refresh(state)
            # Expiry remains recorded even if the requested action is refused.
            self.save(state)
            result = action(state)
            self.save(state)
            return copy.deepcopy(result)

    def init(self, config):
        entries = resources(config)
        with self.locked():
            if self.path.exists():
                raise ValueError('pool already exists; do not overwrite active ownership')
            at = self.clock().isoformat()
            state = {'schema_version': 1, 'resources': [dict(e, status='quarantined', lease=None,
                     reason='initial state unknown; adopt after verified cleanup', changed_at=at) for e in entries]}
            self.save(state)
        return self.status()

    def status(self):
        return self.apply(lambda state: state)

    @staticmethod
    def item(state, server):
        for item in state['resources']:
            if item['server'] == server:
                return item
        raise ValueError('unknown MCP server')

    @staticmethod
    def owned(state, server, lease_id):
        item = BrowserPool.item(state, server)
        if not text(lease_id) or not item.get('lease') or item['lease']['lease_id'] != lease_id:
            raise ValueError('lease no longer belongs to this caller')
        return item

    def lease(self, task_id, request_id, owner_task_id, ttl):
        if any(not text(x) for x in (task_id, request_id, owner_task_id)):
            raise ValueError('task_id/request_id/actual CLI owner_task_id required')
        if type(ttl) is not int or not 30 <= ttl <= 3600:
            raise ValueError('ttl must be 30..3600 seconds')
        at = self.clock()
        return dict(lease_id=uuid.uuid4().hex, task_id=task_id, request_id=request_id, owner_task_id=owner_task_id,
                    acquired_at=at.isoformat(), expires_at=(at + timedelta(seconds=ttl)).isoformat(), ttl_seconds=ttl)

    def acquire(self, task_id, request_id, owner_task_id, server=None, ttl=300):
        candidate = self.lease(task_id, request_id, owner_task_id, ttl)
        def action(state):
            for item in state['resources']:
                previous = item.get('lease')
                if previous and previous['owner_task_id'] == owner_task_id:
                    if (all(previous[k] == candidate[k] for k in ('task_id', 'request_id'))
                            and item['status'] == 'leased' and (server is None or server == item['server'])):
                        return item  # Retry gets the same lease, no second reservation.
                    raise ValueError('owner already has a reservation needing cleanup; do not reserve another')
            choices = [self.item(state, server)] if server else state['resources']
            free = [i for i in choices if i['status'] == 'free']
            if not free:
                raise ValueError('no free profile; queue UI work and inspect owners/quarantine instead of creating a new server')
            item = min(free, key=lambda i: i.get('last_released_at', ''))
            item.update(status='leased', lease=candidate, changed_at=self.clock().isoformat())
            return item
        return self.apply(action)

    def check(self, server, lease_id, heartbeat=False):
        def action(state):
            item = self.owned(state, server, lease_id)
            if item['status'] != 'leased':
                raise ValueError('profile is not available for UI actions; close/recover it')
            if heartbeat:
                item['lease']['expires_at'] = (self.clock() + timedelta(seconds=item['lease']['ttl_seconds'])).isoformat()
            return item
        return self.apply(action)

    def begin_close(self, server, lease_id):
        def action(state):
            item = self.owned(state, server, lease_id)
            if item['status'] == 'closing':
                return item
            if item['status'] not in ('leased', 'quarantined'):
                raise ValueError('cannot begin owner cleanup from this state')
            at = self.clock()
            item.update(status='closing', close_id=uuid.uuid4().hex, closing_started_at=at.isoformat())
            item['lease']['expires_at'] = (at + timedelta(seconds=item['lease']['ttl_seconds'])).isoformat()
            return item
        return self.apply(action)

    def quarantine(self, server, lease_id, reason):
        if not text(reason):
            raise ValueError('quarantine needs a concrete reason')
        def action(state):
            item = self.owned(state, server, lease_id)
            item.update(status='quarantined', reason=reason, changed_at=self.clock().isoformat())
            return item
        return self.apply(action)

    def evidence(self, path):
        path = Path(path).resolve()
        content = path.read_bytes()
        record = read_json(content)
        if not isinstance(record, dict):
            raise ValueError('evidence must be an object')
        return record, {'artifact': str(path), 'sha256': hashlib.sha256(content).hexdigest()}

    def recent(self, record, minimum):
        at = timestamp(record.get('observed_at'))
        if at < timestamp(minimum) or at > self.clock() + timedelta(seconds=5):
            raise ValueError('evidence predates the current phase or is in the future')

    def begin_recovery(self, server, task_id, request_id, owner_task_id, evidence, ttl=300):
        record, reference = self.evidence(evidence)
        candidate = self.lease(task_id, request_id, owner_task_id, ttl)
        def action(state):
            item = self.item(state, server)
            if item['status'] == 'recovering' and all(item['lease'][k] == candidate[k] for k in ('task_id', 'request_id', 'owner_task_id')):
                if item.get('recovery_evidence') == reference:
                    return item
                raise ValueError('recovery already started with different evidence')
            if item['status'] != 'quarantined':
                raise ValueError('only quarantined profiles can be adopted/recovered; never steal an active owner')
            old = item.get('lease')
            if (record.get('server') != server or 'previous_lease_id' not in record
                    or record.get('previous_lease_id') != (old['lease_id'] if old else None)
                    or record.get('prior_owner_stopped') is not True or record.get('connections_quiesced') is not True
                    or not text(record.get('method'))):
                raise ValueError('recovery needs current evidence that the previous owner and other callers stopped')
            self.recent(record, item['changed_at'])
            if any(i.get('lease') and i['lease']['owner_task_id'] == owner_task_id and i['status'] != 'free'
                   for i in state['resources'] if i is not item):
                raise ValueError('recovery owner already holds another profile')
            item.update(status='recovering', lease=candidate, close_id=uuid.uuid4().hex,
                        closing_started_at=self.clock().isoformat(), recovery_evidence=reference)
            return item
        return self.apply(action)

    def finish_close(self, server, lease_id, evidence):
        record, reference = self.evidence(evidence)
        def action(state):
            current = self.item(state, server)
            previous = current.get('last_release', {})
            if (current['status'] == 'free' and previous.get('lease_id') == lease_id
                    and previous.get('evidence') == reference):
                return current  # A lost CLI response must not require another MCP close.
            item = self.owned(state, server, lease_id)
            if item['status'] not in ('closing', 'recovering'):
                raise ValueError('begin cleanup first; expired/failed cleanup stays quarantined')
            if (record.get('server') != server or record.get('lease_id') != lease_id
                    or record.get('close_id') != item['close_id'] or record.get('scope') not in ('context', 'server')
                    or record.get('success') is not True or record.get('profile_released') is not True
                    or not text(record.get('method')) or not isinstance(record.get('result'), dict) or not record['result']):
                raise ValueError('cleanup needs context/server release evidence for this lease and close_id; tab close is insufficient')
            if record['result'].get('isError') is True:
                raise ValueError('MCP reported an error; cleanup cannot be declared successful')
            self.recent(record, item['closing_started_at'])
            receipt = dict(lease_id=lease_id, close_id=item['close_id'], evidence=reference,
                           released_at=self.clock().isoformat())
            receipt.update({k: item['lease'][k] for k in ('task_id', 'request_id', 'owner_task_id')})
            item.update(status='free', lease=None, last_release=receipt, last_released_at=receipt['released_at'],
                        changed_at=receipt['released_at'])
            for key in ('close_id', 'closing_started_at', 'reason', 'recovery_evidence'):
                item.pop(key, None)
            return item
        return self.apply(action)
