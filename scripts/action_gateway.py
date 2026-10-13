"""Scoped MCP calls, durable action receipts, readback before retry."""
from contextlib import contextmanager, nullcontext
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import secrets
import subprocess
import threading
import time

from task_exchange import atomic_write, locked, now, read_json, required


def pointer(value, path):
    if path == '':
        return value
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('Expected JSON pointer')
    for key in path[1:].split('/'):
        key = key.replace('~1', '/').replace('~0', '~')
        try:
            value = value[int(key)] if isinstance(value, list) else value[key]
        except (KeyError, IndexError, ValueError, TypeError):
            raise ValueError(f'Missing policy field: {path}') from None
    return value


def validate_policy(source):
    value = copy.deepcopy(source)
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('Gateway policy needs schema_version=1')
    command = value.get('server_command')
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
        raise ValueError('server_command must be an explicit argv list')
    if not isinstance(value.get('tools'), dict) or not value['tools']:
        raise ValueError('Gateway policy needs tools')
    for name, rule in value['tools'].items():
        required(name, 'tool name')
        if not isinstance(rule, dict) or rule.get('effect') not in ('read', 'write'):
            raise ValueError('Tool effect must be read/write')
        if not isinstance(rule.get('constraints'), dict) or not rule['constraints']:
            raise ValueError('Each tool needs explicit resource constraints')
        for path in rule['constraints']:
            if not isinstance(path, str) or not path.startswith('/'):
                raise ValueError('Constraints use JSON pointers')
        if (not isinstance(rule.get('allowed_arguments'), list)
                or any(not isinstance(x, str) or not x for x in rule['allowed_arguments'])):
            raise ValueError('allowed_arguments must be explicit')
        if rule.get('idempotency_argument') is not None:
            required(rule['idempotency_argument'], 'idempotency_argument')
        readback = rule.get('readback')
        if readback is not None:
            if (not isinstance(readback, dict) or readback.get('tool') not in value['tools']
                    or value['tools'][readback['tool']].get('effect') != 'read'
                    or not isinstance(readback.get('arguments'), dict)
                    or not isinstance(readback.get('presence_pointer'), str)
                    or not readback['presence_pointer'].startswith('/')):
                raise ValueError('Readback needs an allowed read tool and a boolean presence pointer')
            for binding in readback['arguments'].values():
                if (not isinstance(binding, dict) or len(binding) != 1
                        or next(iter(binding)) not in ('literal', 'argument', 'action_key')):
                    raise ValueError('Invalid readback binding')
            if type(readback.get('authoritative_absence', False)) is not bool:
                raise ValueError('authoritative_absence must be bool')
    for name, default, limit in (('max_write_actions', 1, 100), ('max_action_attempts', 2, 5)):
        value.setdefault(name, default)
        if type(value[name]) is not int or not 1 <= value[name] <= limit:
            raise ValueError(f'Invalid {name}')
    value.setdefault('call_timeout_seconds', 30)
    if (type(value['call_timeout_seconds']) not in (int, float) or not math.isfinite(value['call_timeout_seconds'])
            or not 0 < value['call_timeout_seconds'] <= 300):
        raise ValueError('Invalid call timeout')
    if 'max_review_calls' in value and (type(value['max_review_calls']) is not int or not 1 <= value['max_review_calls'] <= 100):
        raise ValueError('Invalid max_review_calls')
    return value


class MCPClient:
    """One sequential stdio client; server-initiated capabilities are refused."""
    def __init__(self, command, timeout=30, cwd=None, stderr=None):
        self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=stderr or subprocess.DEVNULL, text=True, bufsize=1)
        self.timeout, self.counter = timeout, 0
        self.messages = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except (ValueError, OSError) as exc:
            self.messages.put(exc)
        self.messages.put(EOFError('MCP server stopped'))

    def notify(self, method, params=None):
        value = dict(jsonrpc='2.0', method=method)
        if params is not None:
            value['params'] = params
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def request(self, method, params=None):
        self.counter += 1
        rid = f'gateway:{self.counter}'
        self.process.stdin.write(json.dumps(dict(jsonrpc='2.0', id=rid, method=method, params=params or {})) + '\n')
        self.process.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                response = self.messages.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError('MCP reply timed out') from None
            if isinstance(response, Exception):
                raise response
            if not isinstance(response, dict):
                raise ValueError('Invalid MCP response')
            if 'method' in response and 'id' in response:
                self.process.stdin.write(json.dumps(dict(jsonrpc='2.0', id=response['id'],
                    error=dict(code=-32601, message='Server-initiated requests are disabled'))) + '\n')
                self.process.stdin.flush()
            elif response.get('id') == rid:
                if 'error' in response:
                    raise ValueError('MCP server rejected request')
                return response['result']
            if time.monotonic() >= deadline:
                raise TimeoutError('MCP reply timed out')

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()


class ActionGateway:
    def __init__(self, path, policy, task_id, step_id, client, authorize=None, review_context=None):
        self.path = Path(path).resolve()
        self.policy = validate_policy(policy)
        self.client, self.task_id, self.step_id = client, task_id, step_id
        self.authorize = authorize or (lambda: None)
        self.review_context = review_context
        self.policy_hash = hashlib.sha256(json.dumps(self.policy, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with locked(self.path):
            if self.path.exists():
                self._read()
            else:
                atomic_write(self.path, dict(schema_version=1, task_id=task_id, step_id=step_id,
                    policy_sha256=self.policy_hash, actions={}, events=[]))

    def _read(self):
        value = read_json(self.path)
        if (value.get('schema_version') != 1 or value.get('task_id') != self.task_id or value.get('step_id') != self.step_id
                or value.get('policy_sha256') != self.policy_hash or not isinstance(value.get('actions'), dict)):
            raise ValueError('Action journal scope/policy differs')
        return value

    @contextmanager
    def io_lock(self):
        with self.path.with_name(self.path.name + '.io.lock').open('a+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _save(self, state, kind, action_id):
        state['events'].append(dict(event_id=len(state['events']) + 1, kind=kind, action_id=action_id, created_at=now()))
        atomic_write(self.path, state)

    def check(self, name, arguments):
        rule = self.policy['tools'].get(name)
        if not rule or not isinstance(arguments, dict):
            raise ValueError('Tool is outside approved policy')
        if rule.get('idempotency_argument') in arguments:
            raise ValueError('Idempotency key is owned by the gateway')
        if set(arguments) - set(rule['allowed_arguments']):
            raise ValueError('Arguments outside approved schema')
        for path, expected in rule['constraints'].items():
            if json.dumps(pointer(arguments, path), sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True, allow_nan=False):
                raise ValueError('Resource outside approved scope')
        json.dumps(arguments, sort_keys=True, allow_nan=False)
        return rule

    def _receipt(self, row, result):
        payload = dict(schema_version=1, task_id=self.task_id, step_id=self.step_id,
                       action_id=row['action_id'], tool=row['tool'], arguments=row['arguments'], result=result)
        if 'context' in row:
            payload.update(kind='review_execution', context=row['context'], fresh=row['fresh'],
                           started_epoch=row['started_epoch'], finished_epoch=row['finished_epoch'])
        data = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2).encode() + b'\n'
        digest = hashlib.sha256(data).hexdigest()
        directory = self.path.with_name(self.path.name + '.receipts')
        directory.mkdir(exist_ok=True)
        target = directory / (row['action_id'] + '.' + digest + '.json')
        try:
            with target.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if target.read_bytes() != data:
                raise ValueError('Action receipt changed')
        return dict(result=result, result_sha256=hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest(),
                    receipt_path=str(target), receipt_sha256=digest)

    def _cached(self, row):
        target = Path(row['receipt_path'])
        directory = self.path.with_name(self.path.name + '.receipts')
        if (target.parent != directory or not target.name.startswith(row['action_id'] + '.')
                or hashlib.sha256(target.read_bytes()).hexdigest() != row['receipt_sha256']
                or hashlib.sha256(json.dumps(row['result'], sort_keys=True).encode()).hexdigest() != row['result_sha256']):
            raise ValueError('Action receipt changed')
        payload = read_json(target)
        if any(payload.get(key) != expected for key, expected in (
                ('task_id', self.task_id), ('step_id', self.step_id), ('action_id', row['action_id']),
                ('tool', row['tool']), ('arguments', row['arguments']), ('result', row['result']))):
            raise ValueError('Action receipt changed')
        result = copy.deepcopy(row['result'])
        result.setdefault('_meta', {})['qa_action_receipt'] = dict(action_id=row['action_id'],
            path=row['receipt_path'], sha256=row['receipt_sha256'])
        return result

    def call(self, name, arguments):
        if self.review_context is not None:
            with self.io_lock():
                return self._observe(name, arguments)
        return self._call(name, arguments)

    def _observe(self, name, arguments):
        rule = self.check(name, arguments)
        self.authorize()
        context = self.review_context()
        oid = 'OBS-' + secrets.token_hex(16)
        with locked(self.path):
            state = self._read()
            observations = state.setdefault('observations', {})
            if len(observations) >= self.policy.get('max_review_calls', 20):
                raise ValueError('Review execution call budget exhausted')
            # A cached write result is useful context but never a new execution.
            canonical = json.dumps([self.task_id, self.step_id, name, arguments], sort_keys=True, allow_nan=False)
            aid = 'ACT-' + hashlib.sha256(canonical.encode()).hexdigest()
            fresh = rule['effect'] == 'read' or state['actions'].get(aid, {}).get('status') != 'verified'
            row = dict(action_id=oid, tool=name, arguments=copy.deepcopy(arguments), context=context,
                       fresh=fresh, status='started', started_epoch=time.time())
            observations[oid] = row
            self._save(state, 'review_call_started', oid)
        try:
            result = self._call(name, arguments, io_owned=True)
            if not isinstance(result, dict) or result.get('isError'):
                raise ValueError('Review call failed; no observation verified')
            self.authorize()
            if self.review_context() != context:
                raise ValueError('Review execution context changed')
        except (OSError, ValueError, TimeoutError, EOFError, KeyError, TypeError):
            with locked(self.path):
                state = self._read()
                state['observations'][oid].update(status='unknown', finished_epoch=time.time())
                self._save(state, 'review_call_unknown', oid)
            raise
        with locked(self.path):
            state = self._read()
            row = state['observations'][oid]
            row.update(status='verified', finished_epoch=time.time())
            row.update(self._receipt(row, result))
            self._save(state, 'review_call_verified', oid)
        response = copy.deepcopy(result)
        response.setdefault('_meta', {})['qa_review_execution_receipt'] = dict(path=row['receipt_path'],
            sha256=row['receipt_sha256'], observation_id=oid, fresh=row['fresh'])
        return response

    def _call(self, name, arguments, io_owned=False):
        rule = self.check(name, arguments)
        with nullcontext() if io_owned else self.io_lock():
            self.authorize()  # Recheck after waiting for another call or controller cancellation.
            if rule['effect'] == 'read':
                return self.client.request('tools/call', dict(name=name, arguments=arguments))
            canonical = json.dumps([self.task_id, self.step_id, name, arguments], sort_keys=True, allow_nan=False)
            aid = 'ACT-' + hashlib.sha256(canonical.encode()).hexdigest()
            with locked(self.path):
                state = self._read()
                existing = state['actions'].get(aid)
                if existing and existing['status'] == 'verified':
                    return self._cached(existing)
                if any(row['status'] in ('started', 'unknown') for row in state['actions'].values()):
                    raise ValueError('Uncertain external action; readback required before any write')
                if existing and existing['attempts'] >= self.policy['max_action_attempts']:
                    raise ValueError('Action retry budget exhausted')
                if not existing and len(state['actions']) >= self.policy['max_write_actions']:
                    raise ValueError('Write action budget exhausted')
                row = existing or dict(action_id=aid, tool=name, arguments=copy.deepcopy(arguments), attempts=0)
                row.update(status='started', attempts=row['attempts'] + 1, started_at=now())
                state['actions'][aid] = row
                self._save(state, 'action_started', aid)  # Persist before crossing the side-effect boundary.
            forwarded = copy.deepcopy(arguments)
            if rule.get('idempotency_argument'):
                forwarded[rule['idempotency_argument']] = aid
            try:
                result = self.client.request('tools/call', dict(name=name, arguments=forwarded))
                if not isinstance(result, dict) or result.get('isError'):
                    raise ValueError('Write outcome not confirmed')
            except (OSError, ValueError, TimeoutError, EOFError, KeyError, TypeError):
                with locked(self.path):
                    state = self._read()
                    state['actions'][aid].update(status='unknown', finished_at=now())
                    self._save(state, 'action_unknown', aid)
                raise
            with locked(self.path):
                state = self._read()
                state['actions'][aid].update(status='verified', **self._receipt(state['actions'][aid], result), finished_at=now())
                self._save(state, 'action_verified', aid)
                return self._cached(state['actions'][aid])

    def reconcile(self, aid):
        with self.io_lock():
            self.authorize()
            with locked(self.path):
                state = self._read()
                row = copy.deepcopy(state['actions'].get(aid))
                if not row:
                    raise ValueError('Unknown action ID')
                if row['status'] == 'verified':
                    return dict(status='verified', observation=self._cached(row))
            readback = self.policy['tools'][row['tool']].get('readback')
            if not readback:
                raise ValueError('No approved readback; coordinator reconciliation required')
            arguments = {}
            for key, binding in readback['arguments'].items():
                if 'literal' in binding:
                    arguments[key] = binding['literal']
                elif 'argument' in binding:
                    arguments[key] = pointer(row['arguments'], binding['argument'])
                else:
                    arguments[key] = aid
            self.check(readback['tool'], arguments)
            result = self.client.request('tools/call', dict(name=readback['tool'], arguments=arguments))
            if not isinstance(result, dict) or result.get('isError'):
                raise ValueError('Readback failed')
            observation = result.get('structuredContent')
            if observation is None and len(result.get('content', [])) == 1:
                observation = json.loads(result['content'][0]['text'])
            present = pointer(observation, readback['presence_pointer'])
            if type(present) is not bool:
                raise ValueError('Readback must explicitly confirm presence/absence')
            with locked(self.path):
                state = self._read()
                target = state['actions'][aid]
                target['readback_result'] = result
                if present:
                    target.update(status='verified', **self._receipt(target, result))
                elif readback.get('authoritative_absence', False):
                    target['status'] = 'not_applied'
                else:
                    target['status'] = 'unknown'
                self._save(state, 'action_reconciled', aid)
                return dict(status=target['status'], observation=result)

    def tools(self, upstream):
        result = []
        for item in upstream.get('tools', []):
            if item.get('name') not in self.policy['tools']:
                continue
            tool = copy.deepcopy(item)
            rule = self.policy['tools'][tool['name']]
            tool['description'] = tool.get('description', '') + ' Scope is restricted by the task policy.'
            argument = rule.get('idempotency_argument')
            if argument:
                schema = tool.get('inputSchema', {})
                schema.get('properties', {}).pop(argument, None)
                if 'required' in schema:
                    schema['required'] = [key for key in schema['required'] if key != argument]
            result.append(tool)
        result.append(dict(name='qa_reconcile_action', description='Read back an uncertain action before retrying writes.',
                           inputSchema=dict(type='object', properties={'action_id': {'type': 'string'}},
                                            required=['action_id'], additionalProperties=False)))
        result.append(dict(name='qa_action_status', description='Inspect action IDs and outcomes for this step.',
                           inputSchema=dict(type='object', properties={}, additionalProperties=False)))
        return dict(tools=result)

    def status(self):
        with locked(self.path):
            state = self._read()
            return [dict(action_id=aid, tool=row['tool'], status=row['status'], attempts=row['attempts'],
                         receipt_path=row.get('receipt_path'), receipt_sha256=row.get('receipt_sha256'))
                    for aid, row in state['actions'].items()]
