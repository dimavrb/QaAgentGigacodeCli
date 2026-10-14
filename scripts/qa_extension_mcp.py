#!/usr/bin/env python3
"""Expose installed package metadata over stdio MCP; no project writes or external calls."""
import json
import sys

from qa_project import package_info

PROTOCOLS = {'2024-11-05', '2025-03-26', '2025-06-18'}
TOOL = {'name': 'qa_package_info', 'description': 'Read installed QA package version and absolute resource paths',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                        'idempotentHint': True, 'openWorldHint': False}}


def handle(request):
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
        response['result'] = {'tools': [TOOL]}
    elif method == 'tools/call':
        if params.get('name') != TOOL['name'] or params.get('arguments', {}) != {}:
            response['error'] = {'code': -32602, 'message': 'Only qa_package_info with empty arguments is supported'}
        else:
            response['result'] = {'content': [{'type': 'text', 'text': json.dumps(package_info(), ensure_ascii=False)}]}
    else:
        response['error'] = {'code': -32601, 'message': 'Method not found'}
    return response


def main():
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            response = handle(request)
        except json.JSONDecodeError:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        except (OSError, ValueError, TypeError) as exc:
            rid = request.get('id') if isinstance(request, dict) else None
            response = {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32603, 'message': str(exc)}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
