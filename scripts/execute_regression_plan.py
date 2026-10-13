"""Execute an explicit local command plan with worker limits and resource locks.

No product commands are inferred by an agent. Call only with coordinator-approved
argv/cwd and isolated data or named shared resources. Every retry stays visible.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import signal
import threading
import time

from frozen_reports import Sources, number, rows, string, strings, write_new, sha
from regression_schedule import make_schedule


def execute(input_path, policy_path, commands_path, output):
    schedule = make_schedule(input_path, policy_path)
    if schedule['status'] != 'planned': raise ValueError('schedule is not executable without durations')
    sources = Sources(); sources.absorb(schedule['source_hashes'])
    policy = sources.load(policy_path, 'execution_policy')
    commands = sources.load(commands_path, 'execution_commands')
    if commands.get('impact_input_sha256') != sources.hashes[str(Path(input_path).resolve())]:
        raise ValueError('commands belong to another frozen impact input')
    identity = {key: string(commands.get(key), key) for key in ('run_id', 'build_revision', 'environment_id', 'test_data_id')}
    manifest = sources.load(input_path, 'impact_input')
    if identity['build_revision'] != manifest['target_revision']:
        raise ValueError('commands are for another build')
    if manifest.get('execution_context') and any(identity[k] != manifest['execution_context'][k] for k in ('environment_id', 'test_data_id')):
        raise ValueError('commands differ from frozen execution context')
    if commands.get('measurement_kind') not in ('live', 'fixture'): raise ValueError('unknown measurement kind')
    tests = rows(commands.get('tests'), 'commands.tests', 'scenario_id')
    if tests.keys() != set(schedule['run_scenario_ids']): raise ValueError('commands must exactly match the selected tests')
    for test in tests.values():
        argv = test.get('argv')
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or '\x00' in a for a in argv):
            raise ValueError('argv must be a nonempty list of strings')
        cwd = Path(string(test.get('cwd'), 'cwd')).resolve()
        if not cwd.is_dir(): raise ValueError('cwd does not exist')
        test['cwd'] = str(cwd)
        number(test.get('timeout_seconds'), 'timeout_seconds', positive=True)
        if type(test.get('max_attempts')) is not int or not 1 <= test['max_attempts'] <= 3:
            raise ValueError('max_attempts must be 1..3')
        if test['max_attempts'] > 1 and test.get('retry_safe') is not True:
            raise ValueError('retries require explicitly safe, resettable test data')
        codes = test.get('retry_exit_codes')
        if not isinstance(codes, list) or any(type(c) is not int or c == 0 for c in codes) or len(codes) != len(set(codes)):
            raise ValueError('retry exit codes must be explicit unique nonzero integers')
    resources = {r['resource_id']: threading.BoundedSemaphore(r['capacity']) for r in policy['resources']}
    output = Path(output).resolve(); output.mkdir()  # Entire attempt is a new directory.
    write_new(output / 'schedule.json', schedule)
    write_new(output / 'inputs.json', sources.finish(dict(schema_version=1)))
    started = time.time()
    by_worker = {w['worker_id']: [] for w in policy['workers']}
    for assignment in schedule['assignments']: by_worker[assignment['worker_id']].append(assignment)
    result_rows, spans = [], []
    def worker(worker_id, assignments):
        local_rows, local_spans = [], []
        for assignment in assignments:
            sid = assignment['scenario_id']; test = tests[sid]
            locks = [resources[r] for r in sorted(assignment['resource_slots'])]
            queued = time.time()
            acquired = []
            try:
                for lock in locks: lock.acquire(); acquired.append(lock)
                active = time.time()
                local_spans.append(dict(span_id=sid + ':queue', phase_id='execution', kind='queue',
                    started_at=queued, finished_at=active, status='succeeded'))
                history = []
                for index in range(1, test['max_attempts'] + 1):
                    aid = sid + ':' + str(index); left = time.time(); code, stdout, stderr = None, b'', b''
                    error = None
                    try:
                        process = subprocess.Popen(test['argv'], cwd=test['cwd'], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
                        try:
                            stdout, stderr = process.communicate(timeout=test['timeout_seconds'])
                        except subprocess.TimeoutExpired:
                            try: os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError: pass
                            stdout, stderr = process.communicate()
                            error = 'timeout'
                        code = process.returncode
                    except subprocess.TimeoutExpired as exc:
                        stdout, stderr = exc.stdout or b'', exc.stderr or b''
                        error = 'timeout'
                    except OSError as exc:
                        error = type(exc).__name__
                    right = time.time()
                    # Filenames use stable numeric indexes, never product scenario IDs.
                    capture = output / f'capture-{list(tests).index(sid)}-{index}.json'
                    write_new(capture, dict(schema_version=1, scenario_id=sid, attempt_id=aid, exit_code=code,
                        error=error, stdout=stdout.decode('utf-8', errors='replace'), stderr=stderr.decode('utf-8', errors='replace')))
                    status = 'error' if error else 'pass' if code == 0 else 'fail'
                    row = dict(attempt_id=aid, status=status, duration_seconds=right-left,
                        capture=dict(path=str(capture), sha256=sha(capture)))
                    if status != 'pass': row['classification'] = 'infrastructure' if error else 'unclassified'
                    history.append(row)
                    local_spans.append(dict(span_id=aid, phase_id='execution', kind='active', attempt_id=aid,
                        retry=index > 1, started_at=left, finished_at=right, status='succeeded' if status == 'pass' else 'failed',
                        input_tokens=0, output_tokens=0))
                    if error or code == 0 or code not in test['retry_exit_codes']: break
                local_rows.append(dict(scenario_id=sid, worker_id=worker_id, attempts=history))
            finally:
                for lock in reversed(acquired): lock.release()
        return local_rows, local_spans
    with ThreadPoolExecutor(max_workers=len(by_worker)) as pool:
        futures = [pool.submit(worker, wid, assignments) for wid, assignments in by_worker.items()]
        for future in futures:
            records, times = future.result(); result_rows.extend(records); spans.extend(times)
    finished = time.time()
    # QA acceptance and analysis/review/setup are supplied by the coordinator later;
    # completing subprocesses cannot automatically close the entire QA stage.
    trace = dict(schema_version=1, **identity, started_at=started, accepted_at=None,
        spans=sorted(spans, key=lambda s: (s['started_at'], s['span_id'])), execution_finished_at=finished)
    write_new(output / 'execution-trace.json', trace)
    result = sources.finish(dict(schema_version=1, **identity, measurement_kind=commands['measurement_kind'],
        catalog_sha256=manifest['catalog']['sha256'], results=sorted(result_rows, key=lambda r: r['scenario_id']),
        timing=None, qa_accepted=False))
    write_new(output / 'results.json', result)
    return result
