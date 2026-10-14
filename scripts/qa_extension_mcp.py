#!/usr/bin/env python3
"""Expose package metadata, project init/doctor and read-only task discovery over stdio MCP.

Init writes only the three QA binding files; doctor only probes gigacode --version.
"""
import json
import sys
from pathlib import Path

from qa_project import RebindRequired, doctor, initialize, package_info, project_path
from qa_tasks import task_get, tasks_list

PROTOCOLS = {'2024-11-05', '2025-03-26', '2025-06-18'}
TOOLS = [{'name': 'qa_package_info', 'description': 'Read installed QA package version and absolute resource paths',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                        'idempotentHint': True, 'openWorldHint': False}},
         {'name': 'qa_project_init',
          'description': 'Initialize a project QA binding, preserving existing context. Writes only .gitignore, '
                         '.qa-agent/APP_CONTEXT.md and .qa-agent/project.local.json. On a changed package path, '
                         'call without rebind first, show old/new paths to the user, then use rebind=true '
                         'only for an authorized move in this MCP session.',
          'inputSchema': {'type': 'object', 'properties': {
              'project_root': {'type': 'string', 'description': 'Absolute existing product project directory'},
              'rebind': {'type': 'boolean', 'default': False}},
              'required': ['project_root'], 'additionalProperties': False},
          'annotations': {'readOnlyHint': False, 'destructiveHint': False,
                          'idempotentHint': True, 'openWorldHint': False}},
         {'name': 'qa_project_doctor',
          'description': 'Read project QA checks; needs_attention is a normal result. '
                         'The only child process is the gigacode --version probe; no external services.',
          'inputSchema': {'type': 'object', 'properties': {
              'project_root': {'type': 'string', 'description': 'Absolute existing product project directory'}},
              'required': ['project_root'], 'additionalProperties': False},
          'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                          'idempotentHint': True, 'openWorldHint': False}},
         {'name': 'qa_tasks_list',
          'description': 'List saved local project tasks, unfinished by default. Read-only snapshots; '
                         'warnings mean incomplete discovery. Does not select, start or resume work.',
          'inputSchema': {'type': 'object', 'properties': {
              'project_root': {'type': 'string', 'description': 'Absolute product project directory'},
              'include_completed': {'type': 'boolean', 'default': False},
              'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20},
              'offset': {'type': 'integer', 'minimum': 0, 'default': 0}},
              'required': ['project_root'], 'additionalProperties': False},
          'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                          'idempotentHint': True, 'openWorldHint': False}},
         {'name': 'qa_task_get',
          'description': 'Read one saved task by ID, including owners, criteria, artifact references and '
                         'resume checks. Snapshot only: not a handoff or permission to repeat external actions.',
          'inputSchema': {'type': 'object', 'properties': {
              'project_root': {'type': 'string', 'description': 'Absolute product project directory'},
              'task_id': {'type': 'string', 'minLength': 1}},
              'required': ['project_root', 'task_id'], 'additionalProperties': False},
          'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                          'idempotentHint': True, 'openWorldHint': False}}]


def text_result(value, *, is_error=False):
    result = {'content': [{'type': 'text', 'text': value if isinstance(value, str)
                          else json.dumps(value, ensure_ascii=False)}]}
    if is_error:
        result['isError'] = True
    return result


class ToolDispatcher:
    def __init__(self):
        # Session-local previews: never write review state into the product.
        self.rebind_reviews = {}
        self.handlers = {'qa_package_info': lambda arguments: package_info(),
                         'qa_project_init': self.project_init,
                         'qa_project_doctor': lambda arguments: doctor(Path(arguments['project_root'])),
                         'qa_tasks_list': lambda arguments: tasks_list(**arguments),
                         'qa_task_get': lambda arguments: task_get(**arguments)}

    def validate(self, name, arguments):
        if not isinstance(name, str) or name not in self.handlers:
            raise ValueError('Unknown tool')
        if not isinstance(arguments, dict):
            raise ValueError('Expected object arguments')
        allowed = set() if name == 'qa_package_info' else {'project_root'}
        if name == 'qa_project_init':
            allowed.add('rebind')
        if name == 'qa_tasks_list':
            allowed.update(('include_completed', 'limit', 'offset'))
        if name == 'qa_task_get':
            allowed.add('task_id')
        if set(arguments) - allowed:
            raise ValueError('Unexpected tool arguments')
        if name != 'qa_package_info':
            project = arguments.get('project_root')
            if not isinstance(project, str) or not Path(project).is_absolute():
                raise ValueError('project_root must be an absolute path')
        if 'rebind' in arguments and type(arguments['rebind']) is not bool:
            raise ValueError('rebind must be a boolean')
        if name == 'qa_task_get' and (not isinstance(arguments.get('task_id'), str) or not arguments['task_id'].strip()):
            raise ValueError('task_id must be a nonempty string')
        if name == 'qa_tasks_list':
            if type(arguments.get('include_completed', False)) is not bool:
                raise ValueError('include_completed must be a boolean')
            if type(arguments.get('limit', 20)) is not int or not 1 <= arguments.get('limit', 20) <= 100:
                raise ValueError('limit must be an integer in 1..100')
            if type(arguments.get('offset', 0)) is not int or arguments.get('offset', 0) < 0:
                raise ValueError('offset must be a nonnegative integer')

    def project_init(self, arguments):
        info = package_info()
        project = project_path(Path(arguments['project_root']), Path(info['package_root']))
        if arguments.get('rebind', False):
            reviewed = self.rebind_reviews.pop(project, None)
            if reviewed is None or reviewed['new_root'] != info['package_root']:
                raise ValueError('rebind requires a preceding qa_project_init without rebind in this MCP session; '
                                 'show the current old/new paths before retrying')
            return initialize(project, rebind=True, expected_binding=reviewed['binding'])
        else:
            self.rebind_reviews.pop(project, None)
            try:
                return initialize(project)
            except RebindRequired as exc:
                self.rebind_reviews[project] = {'binding': exc.binding, 'new_root': exc.new_root}
                raise ValueError(f'{exc}\nShow both paths to the user; use rebind=true only for an authorized move.') from exc

    def call(self, name, arguments):
        # Argument errors remain JSON-RPC -32602; execution errors are MCP tool results.
        self.validate(name, arguments)
        try:
            return text_result(self.handlers[name](arguments))
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            prefix = 'QA task error' if name in ('qa_tasks_list', 'qa_task_get') else 'QA project error'
            return text_result(f'{prefix}: {exc}', is_error=True)


DISPATCHER = ToolDispatcher()


def handle(request, dispatcher=DISPATCHER):
    if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
    if 'id' not in request:
        return None
    rid, method, params = request['id'], request['method'], request.get('params', {})
    response = {'jsonrpc': '2.0', 'id': rid}
    if not isinstance(params, dict):
        response['error'] = {'code': -32602, 'message': 'Expected object params'}
    elif method == 'initialize':
        protocol = params.get('protocolVersion')
        response['result'] = {'protocolVersion': protocol if protocol in PROTOCOLS else '2024-11-05',
                              'capabilities': {'tools': {}},
                              'serverInfo': {'name': 'qa-agent-package', 'version': package_info()['package_version']}}
    elif method == 'ping':
        response['result'] = {}
    elif method == 'tools/list':
        response['result'] = {'tools': TOOLS}
    elif method == 'tools/call':
        try:
            response['result'] = dispatcher.call(params.get('name'), params.get('arguments', {}))
        except ValueError as exc:
            response['error'] = {'code': -32602, 'message': str(exc)}
    else:
        response['error'] = {'code': -32601, 'message': 'Method not found'}
    return response


def main():
    maximum = 65536
    while True:
        line = sys.stdin.readline(maximum + 1)
        if not line:
            break
        request = None
        try:
            if len(line) > maximum:
                while line and not line.endswith('\n'):
                    line = sys.stdin.readline(maximum + 1)
                raise ValueError('MCP message exceeds 65536 characters')
            request = json.loads(line)
            response = handle(request)
        except (json.JSONDecodeError, RecursionError):
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        except (OSError, ValueError, TypeError) as exc:
            rid = request.get('id') if isinstance(request, dict) else None
            response = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32603, 'message': str(exc)}}
        if response is not None:
            # Escape even malformed Unicode IDs so one request cannot break stdout encoding.
            print(json.dumps(response, ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
