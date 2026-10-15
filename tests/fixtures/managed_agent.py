#!/usr/bin/env python3
"""Synthetic trusted process adapter; never a live-model quality measurement."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
from self_review_fixtures import review_for
from environment_fixtures import attach_environment_checks
from action_gateway import ActionGateway, MCPClient

parser = argparse.ArgumentParser()
parser.add_argument('--counter', type=Path, required=True)
parser.add_argument('--mode', default='success')
args = parser.parse_args()
count = int(args.counter.read_text()) + 1 if args.counter.exists() else 1
args.counter.write_text(str(count))
handoff = json.loads(Path(os.environ['QA_EXECUTION_HANDOFF']).read_text())
assignment = handoff['assignment']
if args.mode == 'transient' or args.mode == 'transient-once' and count == 1:
    raise SystemExit(75)
if args.mode == 'permanent':
    raise SystemExit(2)
if args.mode == 'hold' or args.mode == 'hold-once' and count == 1:
    time.sleep(30)
if args.mode == 'invalid':
    print('not a QA report')
    raise SystemExit(0)
if args.mode in ('external', 'external-unknown-success'):
    policy = json.loads((Path(os.environ['QA_EXECUTION_HANDOFF']).parent / 'policy.json').read_text())
    client = MCPClient(policy['server_command'], policy['call_timeout_seconds'])
    client.request('initialize', dict(protocolVersion='2024-11-05', capabilities={}, clientInfo={'name':'fixture','version':'1'}))
    client.notify('notifications/initialized')
    gateway = ActionGateway(os.environ['QA_EXECUTION_ACTIONS'], policy, assignment['task_id'], os.environ['QA_EXECUTION_STEP'], client)
    try:
        gateway.call('create_ticket', {'project': 'SYNTHETIC', 'title': 'fixture'})
    except Exception:
        if args.mode != 'external-unknown-success':
            raise SystemExit(75)
    finally:
        client.close()
proof = Path(os.environ['QA_EXECUTION_HANDOFF']).parent / 'proof.txt'
proof.write_text('Synthetic evidence; no real environment check')
message = dict(task_id=assignment['task_id'], request_id=assignment['request_id'], agent=assignment['agent'],
    event='environment_ready', summary='Synthetic fixture result', artifacts=[str(proof)], blockers=[],
    self_review=review_for(), payload=dict(environment_id='fixture', product_version='fixture-1',
                                         verified_capabilities=['Synthetic protocol only'], limitations=[]))
attach_environment_checks(message, assignment, [str(proof)])
print(json.dumps(message))
