"""Local contract/process checks; no live model evaluation."""
import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import task_exchange as exchange
from action_gateway import ActionGateway, MCPClient
from gateway_fixtures import policy


class ActionGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=self.root/'tickets.json'

    def gateway(self,disconnect=False,source=None):
        source=source or policy(self.store,disconnect)
        client=MCPClient(source['server_command'],1)
        self.addCleanup(client.close)
        client.request('initialize',dict(protocolVersion='2024-11-05',capabilities={},clientInfo={'name':'test','version':'1'}))
        client.notify('notifications/initialized')
        return ActionGateway(self.root/'actions.json',source,'QA-1','step-1',client)

    def test_policy_blocks_resource_unknown_tool_and_extra_arguments_before_call(self):
        gateway=self.gateway()
        for name,args in [('forbidden_tool',{'project':'SYNTHETIC'}),('create_ticket',{'project':'OTHER','title':'x'}),
                          ('create_ticket',{'project':'SYNTHETIC','title':'x','admin':True})]:
            with self.assertRaises(ValueError):gateway.call(name,args)
        self.assertFalse(self.store.exists())

    def test_same_write_reuses_receipt_without_backend_duplicate(self):
        gateway=self.gateway()
        args={'project':'SYNTHETIC','title':'x'}
        first=gateway.call('create_ticket',args)
        self.assertEqual(first,gateway.call('create_ticket',args))
        self.assertEqual(1,json.loads(self.store.read_text())['writes'])

    def test_lost_response_requires_readback_and_disallows_different_write(self):
        gateway=self.gateway(True)
        with self.assertRaises(EOFError):gateway.call('create_ticket',{'project':'SYNTHETIC','title':'x'})
        with self.assertRaisesRegex(ValueError,'Uncertain'):gateway.call('create_ticket',{'project':'SYNTHETIC','title':'different'})
        aid=gateway.status()[0]['action_id']
        recovered=self.gateway(True)
        self.assertEqual('verified',recovered.reconcile(aid)['status'])
        recovered.call('create_ticket',{'project':'SYNTHETIC','title':'x'})
        self.assertEqual(1,json.loads(self.store.read_text())['writes'])

    def test_non_authoritative_absence_remains_unknown(self):
        source=policy(self.store,True)
        source['tools']['create_ticket']['readback']['authoritative_absence']=False
        gateway=self.gateway(source=source)
        with self.assertRaises(EOFError):gateway.call('create_ticket',{'project':'SYNTHETIC','title':'x'})
        aid=gateway.status()[0]['action_id']
        self.store.write_text(json.dumps({'tickets':{},'writes':1,'reads':0}))
        recovered=self.gateway(source=source)
        self.assertEqual('unknown',recovered.reconcile(aid)['status'])

    def test_tool_list_hides_unapproved_tools_and_gateway_owned_idempotency_key(self):
        gateway=self.gateway()
        tools=gateway.tools(gateway.client.request('tools/list'))['tools']
        self.assertNotIn('forbidden_tool',[tool['name'] for tool in tools])
        create=next(tool for tool in tools if tool['name']=='create_ticket')
        self.assertNotIn('client_id',create['inputSchema']['properties'])
        with self.assertRaises(ValueError):gateway.call('create_ticket',{'project':'SYNTHETIC','title':'x','client_id':'own'})

    def test_changed_policy_and_changed_cached_receipt_are_rejected(self):
        gateway=self.gateway()
        gateway.call('create_ticket',{'project':'SYNTHETIC','title':'x'})
        source=policy(self.store)
        source['max_write_actions']=3
        with self.assertRaisesRegex(ValueError,'scope/policy'):self.gateway(source=source)
        state=exchange.read_json(self.root/'actions.json')
        next(iter(state['actions'].values()))['result']={}
        (self.root/'actions.json').write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError,'receipt changed'):gateway.call('create_ticket',{'project':'SYNTHETIC','title':'x'})

    def test_immutable_receipt_is_available_as_evidence_and_checked_on_replay(self):
        gateway=self.gateway()
        result=gateway.call('create_ticket',{'project':'SYNTHETIC','title':'x'})
        receipt=Path(result['_meta']['qa_action_receipt']['path'])
        self.assertTrue(receipt.is_file())
        receipt.write_text('{}')
        with self.assertRaisesRegex(ValueError,'receipt changed'):
            gateway.call('create_ticket',{'project':'SYNTHETIC','title':'x'})
        self.assertEqual(1,json.loads(self.store.read_text())['writes'])

    def test_authoritative_absence_allows_only_bounded_action_retry(self):
        gateway=self.gateway(True)
        args={'project':'SYNTHETIC','title':'x'}
        with self.assertRaises(EOFError):gateway.call('create_ticket',args)
        aid=gateway.status()[0]['action_id']
        self.store.write_text(json.dumps({'tickets':{},'writes':1,'reads':0}))
        recovered=self.gateway(True)
        self.assertEqual('not_applied',recovered.reconcile(aid)['status'])
        with self.assertRaises(EOFError):recovered.call('create_ticket',args)
        self.store.write_text(json.dumps({'tickets':{},'writes':2,'reads':1}))
        last=self.gateway(True)
        self.assertEqual('not_applied',last.reconcile(aid)['status'])
        with self.assertRaisesRegex(ValueError,'budget'):last.call('create_ticket',args)

    def test_concurrent_gateways_share_one_action_and_cancellation_is_rechecked_after_lock(self):
        first, second=self.gateway(),self.gateway()
        barrier=threading.Barrier(2)
        results=[]
        def run(gateway):
            barrier.wait()
            try: results.append(gateway.call('create_ticket',{'project':'SYNTHETIC','title':'x'}))
            except Exception as exc:results.append(exc)
        threads=[threading.Thread(target=run,args=(gateway,)) for gateway in (first,second)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=5)
        self.assertEqual(2,len(results))
        self.assertTrue(all(isinstance(result,dict) for result in results),results)
        self.assertEqual(1,json.loads(self.store.read_text())['writes'])
        def deny():raise ValueError('cancelled')
        second.authorize=deny
        with self.assertRaisesRegex(ValueError,'cancelled'):
            second.call('create_ticket',{'project':'SYNTHETIC','title':'new'})
        self.assertEqual(1,json.loads(self.store.read_text())['writes'])

    def test_stdio_proxy_filters_methods_tools_and_cancelled_attempts(self):
        source=self.root/'policy.json'
        source.write_text(json.dumps(policy(self.store)))
        execution=self.root/'execution.json'
        # A real managed gateway now verifies the frozen policy before spawning
        # its backend; a token-only mock cannot represent that authorization.
        frozen=dict(steps=[dict(step_id='step-1',gateway_policy=policy(self.store))])
        runtime=dict(schema_version=1,task_id='QA-1',plan=frozen,
            plan_sha256=hashlib.sha256(json.dumps(frozen,sort_keys=True).encode()).hexdigest(),
            cancel_requested=False,steps={'step-1':{'attempts':[{'token':'owned','status':'running','assignment':{}}]}})
        execution.write_text(json.dumps(runtime))
        command=[sys.executable,str(ROOT/'scripts/mcp_action_gateway.py'),'--policy',str(source),
                 '--journal',str(self.root/'proxy-actions.json'),'--task-id','QA-1','--step-id','step-1',
                 '--execution-state',str(execution),'--attempt-token','owned']
        client=MCPClient(command,3)
        self.addCleanup(client.close)
        client.request('initialize',dict(protocolVersion='2024-11-05',capabilities={},clientInfo={'name':'test','version':'1'}))
        client.notify('notifications/initialized')
        names=[tool['name'] for tool in client.request('tools/list')['tools']]
        self.assertNotIn('forbidden_tool',names)
        with self.assertRaises(ValueError):client.request('resources/read',{'uri':'file:///tmp/private'})
        with self.assertRaises(ValueError):client.request('tools/call',dict(name='create_ticket',arguments={'project':'OTHER','title':'x'}))
        self.assertFalse(self.store.exists())
        client.request('tools/call',dict(name='create_ticket',arguments={'project':'SYNTHETIC','title':'x'}))
        runtime['cancel_requested']=True
        execution.write_text(json.dumps(runtime))
        with self.assertRaises(ValueError):client.request('tools/call',dict(name='create_ticket',arguments={'project':'SYNTHETIC','title':'new'}))
        self.assertEqual(1,json.loads(self.store.read_text())['writes'])
