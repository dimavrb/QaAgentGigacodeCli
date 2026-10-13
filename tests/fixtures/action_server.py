#!/usr/bin/env python3
"""Synthetic stdio MCP server; ticket state is a local JSON file."""
import argparse
import json
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--store', type=Path, required=True)
parser.add_argument('--disconnect-on-create', action='store_true')
args = parser.parse_args()
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method, params = request['method'], request.get('params', {})
    if method == 'initialize':
        result = dict(protocolVersion=params['protocolVersion'], capabilities={'tools':{}}, serverInfo={'name':'fixture','version':'1'})
    elif method == 'tools/list':
        result = {'tools':[dict(name=name, description='fixture', inputSchema={'type':'object','properties':{
            'project':{'type':'string'},'title':{'type':'string'},'client_id':{'type':'string'}},'required':['project']})
            for name in ('create_ticket','find_ticket','forbidden_tool')]}
    elif method == 'tools/call':
        value = json.loads(args.store.read_text()) if args.store.exists() else {'tickets':{},'writes':0,'reads':0}
        arguments = params['arguments']
        if params['name'] == 'create_ticket':
            value['writes'] += 1
            key = arguments.get('client_id','unkeyed')
            value['tickets'][key] = dict(id='FIXTURE-1', **arguments)
            args.store.write_text(json.dumps(value))
            if args.disconnect_on_create:
                raise SystemExit(0)
            observation = value['tickets'][key]
        elif params['name'] == 'find_ticket':
            value['reads'] += 1
            observation = dict(found=arguments['client_id'] in value['tickets'], ticket=value['tickets'].get(arguments['client_id']))
            args.store.write_text(json.dumps(value))
        else:
            observation = dict(forbidden=True)
        result = {'content':[{'type':'text','text':json.dumps(observation)}],'structuredContent':observation}
    else:
        result = {}
    print(json.dumps(dict(jsonrpc='2.0', id=request['id'], result=result)), flush=True)
