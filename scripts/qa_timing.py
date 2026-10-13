"""End-to-end timing against an independent coordinator-owned phase plan."""
from decimal import Decimal
import copy
from pathlib import Path

from frozen_reports import Sources, boolean, number, rows, string, strings

KINDS = {'active', 'queue', 'human_wait'}


def union_seconds(intervals):
    total, end = Decimal(0), None
    for start, stop in sorted(intervals):
        start, stop = Decimal(str(start)), Decimal(str(stop))
        total += max(Decimal(0), stop - max(start, end if end is not None else start))
        end = max(stop, end if end is not None else stop)
    return float(total)


def summarize_timing(plan_path, trace_path):
    sources = Sources()
    plan = sources.load(plan_path, 'timing_plan')
    trace = sources.load(trace_path, 'timing_trace')
    for key in ('run_id', 'build_revision', 'environment_id', 'test_data_id'):
        string(plan.get(key), 'plan.' + key)
        if trace.get(key) != plan[key]:
            raise ValueError('timing identity mismatch: ' + key)
    expected = set(strings(plan.get('required_phase_ids'), 'required_phase_ids', nonempty=True))
    start = number(trace.get('started_at'), 'started_at')
    accepted = trace.get('accepted_at')
    if accepted is not None:
        number(accepted, 'accepted_at')
        if accepted < start:
            raise ValueError('acceptance precedes start')
    spans = rows(trace.get('spans'), 'spans', 'span_id')
    intervals, by_kind, phases = [], {k: [] for k in KINDS}, set()
    effort, in_tokens, out_tokens, unknown_usage, open_spans = Decimal(0), 0, 0, [], []
    attempts, failed, retries = set(), set(), set()
    for sid, span in spans.items():
        phase = string(span.get('phase_id'), 'phase_id')
        if phase not in expected:
            raise ValueError('span phase is outside the independent plan')
        kind = span.get('kind')
        if kind not in KINDS:
            raise ValueError('unknown timing kind')
        left = number(span.get('started_at'), 'span.started_at')
        right = span.get('finished_at')
        if left < start or accepted is not None and left > accepted:
            raise ValueError('span outside task interval')
        if right is None:
            open_spans.append(sid)
            if accepted is not None:
                raise ValueError('accepted trace contains an open span')
            continue
        number(right, 'span.finished_at')
        if right < left or accepted is not None and right > accepted:
            raise ValueError('span outside task interval')
        status = span.get('status')
        if status not in ('succeeded', 'failed', 'cancelled'):
            raise ValueError('unknown span status')
        phases.add(phase)
        intervals.append((left, right)); by_kind[kind].append((left, right))
        if kind == 'active':
            attempt = string(span.get('attempt_id'), 'active.attempt_id')
            if attempt in attempts:
                raise ValueError('duplicate active attempt; split activities need distinct attempt IDs')
            attempts.add(attempt)
            if span.get('metrics') is not None:
                path = sources.reference(Path(trace_path).resolve().parent, span['metrics'], 'attempt metrics')
                metrics = sources.load(path, 'attempt metrics')
                correlation = metrics.get('correlation', {})
                for key in ('task_id', 'trace_id'):
                    if not plan.get(key) or correlation.get(key) != plan[key]:
                        raise ValueError('attempt correlation mismatch: ' + key)
                bindings = plan.get('step_phases', {})
                if bindings.get(correlation.get('step_id')) != phase:
                    raise ValueError('attempt phase differs from coordinator plan')
                derived = dict(attempt_id=correlation.get('attempt_id'), retry=correlation.get('retry'),
                    started_at=metrics.get('started_epoch'), finished_at=metrics.get('finished_epoch'),
                    input_tokens=metrics.get('input_tokens'), output_tokens=metrics.get('output_tokens'),
                    status='succeeded' if metrics.get('exit_code') == 0 and metrics.get('category') is None else 'failed')
                if any(span.get(k) != v for k, v in derived.items()):
                    raise ValueError('span differs from primary worker metrics')
            if status != 'succeeded': failed.add(attempt)
            if boolean(span.get('retry'), 'active.retry'): retries.add(attempt)
            effort += Decimal(str(right)) - Decimal(str(left))
            for field in ('input_tokens', 'output_tokens'):
                value = span.get(field)
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError('invalid usage')
            if span.get('input_tokens') is None or span.get('output_tokens') is None:
                unknown_usage.append(sid)
            in_tokens += span.get('input_tokens') or 0
            out_tokens += span.get('output_tokens') or 0
    missing = sorted(expected - phases)
    wall = float(Decimal(str(accepted)) - Decimal(str(start))) if accepted is not None else None
    covered = union_seconds(intervals)
    complete = accepted is not None and not missing and not open_spans and covered == wall
    return sources.finish(dict(schema_version=1, run_id=plan['run_id'], build_revision=plan['build_revision'],
        environment_id=plan['environment_id'], test_data_id=plan['test_data_id'],
        accepted=accepted is not None, complete_timing=complete, wall_seconds=wall,
        accounted_wall_seconds=covered, unaccounted_wall_seconds=None if wall is None else max(0, wall-covered),
        active_effort_seconds=float(effort), phase_wall_seconds={k: union_seconds(v) for k, v in sorted(by_kind.items())},
        missing_phase_ids=missing, open_span_ids=open_spans, attempts=len(attempts),
        failed_attempts=len(failed), retry_attempts=len(retries), usage_complete=bool(attempts) and not unknown_usage,
        known_input_tokens=in_tokens, known_output_tokens=out_tokens,
        input_tokens=in_tokens if attempts and not unknown_usage else None,
        output_tokens=out_tokens if attempts and not unknown_usage else None, unknown_usage_span_ids=unknown_usage))


def import_metrics(plan_path, trace_path, index_path):
    """Merge recorded worker attempts; coordinator/tool/queue spans stay explicit."""
    sources = Sources(); plan = sources.load(plan_path, 'timing plan')
    trace = sources.load(trace_path, 'timing trace')
    index = sources.load(index_path, 'metrics index')
    result = copy.deepcopy(trace)
    for key in ('run_id', 'build_revision', 'environment_id', 'test_data_id'):
        if trace.get(key) != plan.get(key): raise ValueError('trace identity differs from plan')
    for key in ('task_id', 'trace_id'): string(plan.get(key), 'plan.' + key)
    if not isinstance(plan.get('step_phases'), dict): raise ValueError('plan needs step_phases for imported workers')
    references = index.get('metrics')
    if not isinstance(references, list) or not references: raise ValueError('empty metrics index')
    existing = set(rows(result.get('spans'), 'spans', 'span_id'))
    for ref in references:
        path = sources.reference(Path(index_path).resolve().parent, ref, 'worker metrics')
        metrics = sources.load(path, 'worker metrics'); ctx = metrics.get('correlation', {})
        if any(ctx.get(key) != plan[key] for key in ('task_id', 'trace_id')):
            raise ValueError('worker belongs to another task trace')
        aid = string(ctx.get('attempt_id'), 'attempt_id')
        if aid in existing: raise ValueError('duplicate imported attempt')
        existing.add(aid)
        phase = plan['step_phases'].get(ctx.get('step_id'))
        if phase not in plan['required_phase_ids']: raise ValueError('unmapped worker phase')
        result['spans'].append(dict(span_id=aid, phase_id=phase, kind='active', attempt_id=aid,
            retry=ctx.get('retry'), started_at=metrics.get('started_epoch'), finished_at=metrics.get('finished_epoch'),
            status='succeeded' if metrics.get('exit_code') == 0 and metrics.get('category') is None else 'failed',
            input_tokens=metrics.get('input_tokens'), output_tokens=metrics.get('output_tokens'),
            metrics=dict(path=str(path), sha256=sources.hashes[str(path)])))
    sources.finish({})
    return result
