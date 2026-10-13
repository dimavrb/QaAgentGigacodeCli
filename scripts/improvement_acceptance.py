"""Bind reproducible improvement reports to an independent assignment plan."""
from pathlib import Path

from frozen_reports import Sources, rows, boolean, string, same_json
from qa_improvements import FIELDS, report


def plan_inputs(plan_path):
    sources = Sources(); plan = sources.load(plan_path, 'improvement plan')
    string(plan.get('session_id'), 'session_id')
    specifications = rows(plan.get('reports'), 'reports', 'report_id')
    if not specifications: raise ValueError('improvement plan needs reports')
    for spec in specifications.values():
        command = spec.get('command')
        if command not in FIELDS: raise ValueError('unknown improvement report kind')
        inputs = spec.get('command_inputs')
        if not isinstance(inputs, dict) or set(inputs) != set(FIELDS[command]):
            raise ValueError('improvement plan command inputs differ')
        for key in inputs:
            inputs[key] = str((Path(plan_path).resolve().parent / string(inputs[key], key)).resolve())
            value = sources.load(inputs[key], key)
        requirements = spec.get('requirements')
        if not isinstance(requirements, dict): raise ValueError('report requirements must be explicit')
        allowed = {'require_live'} if command in ('controls-check', 'shadow') else set()
        allowed.update({'require_reviewed_gold', 'max_false_approvals', 'max_false_blocks', 'max_abstentions'} if command == 'controls-check'
            else {'require_no_miss'} if command == 'shadow' else {'require_planned'} if command == 'schedule' else {'require_complete'})
        if set(requirements) - allowed: raise ValueError('unknown report acceptance requirement')
        for key, value in requirements.items():
            if key.startswith('require_'): boolean(value, key)
            elif type(value) is not int or value < 0: raise ValueError('acceptance threshold must be a nonnegative integer')
        sources.absorb(report(command, inputs)['source_hashes'])
    sources.finish({})
    return plan, sources.hashes


def check_bundle(message, base, plan_path):
    plan, primary = plan_inputs(plan_path)
    sources = Sources(); sources.absorb(primary)
    refs = message['payload'].get('improvement_reports')
    expected = {r['report_id']:r for r in plan['reports']}
    if not isinstance(refs, dict) or refs.keys() != expected.keys():
        raise ValueError('result needs every coordinator-assigned improvement report')
    summaries, failed = [], []
    for rid, spec in expected.items():
        path = (Path(base) / string(refs[rid], rid)).resolve()
        saved = sources.load(path, rid)
        recomputed = report(spec['command'], spec['command_inputs'])
        if not same_json(saved, recomputed): raise ValueError('improvement report differs from independent plan/recomputation: ' + rid)
        sources.absorb(recomputed['source_hashes']); summaries.append(recomputed['summary_text'])
        policy = spec['requirements']
        if policy.get('require_live') and recomputed.get('measurement_kind') != 'live': failed.append(rid + ': live measurement required')
        if spec['command'] == 'controls-check':
            if recomputed['status'] != 'scored': failed.append(rid + ': evaluator execution failed')
            else:
                if policy.get('require_reviewed_gold') and recomputed['provisional_gold']: failed.append(rid + ': provisional gold')
                for key in ('false_approvals', 'false_blocks', 'abstentions'):
                    if 'max_' + key in policy and recomputed['metrics'][key] > policy['max_' + key]: failed.append(rid + ': ' + key)
        elif policy.get('require_no_miss') and recomputed['outcome'] != 'no_miss_in_observed_run': failed.append(rid + ': inconclusive/missed defects')
        elif policy.get('require_planned') and recomputed['status'] != 'planned': failed.append(rid + ': missing durations')
        elif policy.get('require_complete') and not recomputed['complete_timing']: failed.append(rid + ': incomplete timing')
    summary = ' '.join(summaries)
    if message['summary'] != summary: raise ValueError('result summary differs from command-derived improvement summaries')
    if failed and message['event'] != 'blocked': raise ValueError('improvement acceptance gate failed: ' + '; '.join(failed))
    return sources.finish(dict(schema_version=1, session_id=plan['session_id'], summary_text=summary, gate_failures=failed))
