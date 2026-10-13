#!/usr/bin/env python3
"""Policy-enforcing stdio MCP proxy used by managed CLI attempts."""
import argparse
import json
import sys
from pathlib import Path

from action_gateway import ActionGateway, MCPClient
from task_exchange import read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--step-id', required=True)
    parser.add_argument('--execution-state', type=Path)
    parser.add_argument('--attempt-token')
    parser.add_argument('--reconcile', help='Perform only the approved readback for an action ID')
    args = parser.parse_args()
    policy = read_json(args.policy)
    if args.execution_state:
        import execution_supervisor as supervisor
        from review_execution import policy_hash
        state = supervisor.read(args.execution_state)
        step_policy = next(s for s in state['plan']['steps'] if s['step_id'] == args.step_id)['gateway_policy']
        if state['task_id'] != args.task_id or policy_hash(policy) != policy_hash(step_policy):
            raise ValueError('Gateway policy differs from the frozen execution plan')
        if not args.reconcile:
            attempts = state['steps'][args.step_id]['attempts']
            if (state['cancel_requested'] or not attempts or attempts[-1]['token'] != args.attempt_token
                    or attempts[-1]['status'] != 'running'):
                raise ValueError('Execution cancelled or attempt fenced before backend start')
    client = MCPClient(policy['server_command'], policy.get('call_timeout_seconds', 30))
    def fence():
        if args.execution_state:
            state = read_json(args.execution_state)
            step = state['steps'][args.step_id]
            if (state.get('cancel_requested') or not step['attempts'] or step['attempts'][-1]['token'] != args.attempt_token
                    or step['attempts'][-1]['status'] != 'running'):
                raise ValueError('Execution cancelled or attempt fenced')
    review_context = None
    if args.execution_state and not args.reconcile:
        state = read_json(args.execution_state)
        assignment = state['steps'][args.step_id]['attempts'][-1]['assignment']
        if assignment.get('review_execution'):
            from review_execution import context_for
            review_context = lambda: context_for(args.execution_state, args.step_id, args.attempt_token)
    gateway = ActionGateway(args.journal, policy, args.task_id, args.step_id, client,
                            authorize=None if args.reconcile else fence, review_context=review_context)
    try:
        if args.reconcile:
            client.request('initialize', dict(protocolVersion='2024-11-05', capabilities={},
                                              clientInfo=dict(name='qa-readback', version='1')))
            client.notify('notifications/initialized')
            print(json.dumps(gateway.reconcile(args.reconcile), ensure_ascii=False))
            return 0
        for line in sys.stdin:
            rid = None
            try:
                request = json.loads(line)
                rid, method, params = request.get('id'), request['method'], request.get('params', {})
                if rid is None:
                    if method == 'notifications/initialized':
                        client.notify(method)
                    continue
                if method == 'initialize':
                    result = client.request(method, params)
                    result['capabilities'] = {'tools': {}}
                elif method == 'ping':
                    result = {}
                elif method == 'tools/list':
                    if params.get('cursor'):
                        raise ValueError('Pagination cursor is not exposed by the filtered gateway')
                    # Collect pagination internally so every published tool is checked.
                    tools, seen, page = [], set(), client.request(method)
                    while True:
                        tools.extend(page.get('tools', []))
                        if not page.get('nextCursor'):
                            break
                        if page['nextCursor'] in seen or len(seen) >= 100:
                            raise ValueError('MCP tool pagination did not converge')
                        seen.add(page['nextCursor'])
                        page = client.request(method, {'cursor': page['nextCursor']})
                    result = gateway.tools({'tools': tools})
                elif method == 'tools/call':
                    name, arguments = params['name'], params.get('arguments', {})
                    fence()
                    if name == 'qa_action_status':
                        result = {'content': [{'type': 'text', 'text': json.dumps(gateway.status())}]}
                    elif name == 'qa_reconcile_action':
                        result = {'content': [{'type': 'text', 'text': json.dumps(gateway.reconcile(arguments['action_id']))}]}
                    else:
                        result = gateway.call(name, arguments)
                else:
                    raise ValueError('MCP method is outside the gateway protocol')
                response = dict(jsonrpc='2.0', id=rid, result=result)
            except (OSError, ValueError, KeyError, TypeError, EOFError, TimeoutError) as exc:
                response = dict(jsonrpc='2.0', id=rid, error=dict(code=-32000, message=str(exc)))
            print(json.dumps(response, ensure_ascii=False), flush=True)
        return 0
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
