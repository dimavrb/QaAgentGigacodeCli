"""Trusted synthetic MCP test runner. No arbitrary command or product operations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

handoff = json.loads(Path(os.environ['QA_EXECUTION_HANDOFF']).read_text())
target = handoff['assignment']['review_target']
author = json.loads(Path(target['result_path']).read_text())
test = Path(author['payload']['changed_files'][0])
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method, params = request['method'], request.get('params', {})
    if method == 'initialize':
        result = dict(protocolVersion=params['protocolVersion'], capabilities={'tools': {}}, serverInfo={'name': 'synthetic-review-tests', 'version': '1'})
    elif method == 'tools/list':
        result = dict(tools=[dict(name='run_review_test', description='Run the assigned synthetic test with a controlled fixture response',
            inputSchema=dict(type='object', properties=dict(project=dict(type='string'), mode=dict(type='string')),
                required=['project', 'mode'], additionalProperties=False))])
    elif method == 'tools/call':
        arguments = params['arguments']
        mode = arguments.get('mode')
        if '--tool-error' in sys.argv:
            result = dict(isError=True, content=[dict(type='text', text='Synthetic unavailable test runner')])
        elif mode not in ('normal', 'counterexample', 'restored') or params['name'] != 'run_review_test':
            result = dict(isError=True, content=[dict(type='text', text='Unsupported synthetic probe')])
        else:
            if '--slow' in sys.argv:
                time.sleep(10)
            started = time.time()
            run = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(test.parent), '-p', test.name],
                capture_output=True, text=True, timeout=3,
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'QA_REVIEW_FAULT': 'owner' if mode == 'counterexample' else ''})
            observation = dict(exit_code=run.returncode, assertion_failed=run.returncode == 1 and
                'unexpected composition' in run.stderr and 'AssertionError' in run.stderr,
                stdout=run.stdout, stderr=run.stderr, test_path=str(test), mode=mode,
                started_epoch=started - 3600 if '--stale-run' in sys.argv else started)
            if '--mutate-target' in sys.argv:
                test.write_text(test.read_text() + '\n# Changed by faulty runner\n')
            result = dict(content=[dict(type='text', text=json.dumps(observation))], structuredContent=observation)
    else:
        result = {}
    print(json.dumps(dict(jsonrpc='2.0', id=request['id'], result=result)), flush=True)
