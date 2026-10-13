"""Attempt and cycle telemetry. Unknown usage is never inferred from text size."""
import json
from pathlib import Path


def context(system, prompt, snapshots, limit):
    raw=json.dumps(prompt,ensure_ascii=False)
    total=len(system)+len(raw)
    return dict(unit='Unicode characters, not tokens',system_characters=len(system),prompt_characters=len(raw),
                explicit_characters=total,max_context_characters=limit,budget_exceeded=total>limit,
                components={name:len(json.dumps(value,ensure_ascii=False)) for name,value in prompt.items()},
                documents={name:len(text) for name,text in snapshots.items()})


def usage(envelope):
    if not isinstance(envelope,dict) or not isinstance(envelope.get('usage'),dict): return None,None
    reported=envelope['usage']
    def get(*names):
        for name in names:
            value=reported.get(name)
            if type(value) is int and value>=0: return value
        return None
    return get('input_tokens','prompt_tokens'),get('output_tokens','completion_tokens')


def finish(directory, *, backend, exit_code, category, duration_seconds, correlation=None, started_epoch=None, finished_epoch=None):
    import task_exchange as exchange
    directory=Path(directory)
    envelope={}; stdout=directory/'stdout.json'
    if stdout.exists():
        try:
            value=json.loads(stdout.read_text())
            if isinstance(value,list):
                value=value[-1] if value and isinstance(value[-1],dict) and value[-1].get('type')=='result' else {}
            if isinstance(value,dict): envelope=value
        except (ValueError,UnicodeError): pass
    it,ot=usage(envelope)
    ctx=directory/'context-metrics.json'
    metrics=dict(schema_version=1,backend=backend,exit_code=exit_code,category=category,duration_seconds=duration_seconds,
                 context=exchange.read_json(ctx) if ctx.exists() else None,
                 input_tokens=it,output_tokens=ot,reported_usage=envelope.get('usage') if isinstance(envelope.get('usage'),dict) else None,
                 reported_stats=envelope.get('stats') if isinstance(envelope.get('stats'),dict) else None)
    if correlation is not None:
        metrics['correlation'] = correlation
        metrics['started_epoch'] = started_epoch
        metrics['finished_epoch'] = finished_epoch
    exchange.atomic_write(directory/'metrics.json',metrics)
    return metrics


def aggregate(states):
    """Include every reserved attempt, including failures/cancellation; do not impute missing runs."""
    total=dict(attempts=0,measured_context_attempts=0,known_explicit_characters=0,explicit_characters=None,complete_context=False,measured_usage_attempts=0,
               known_input_tokens=0,known_output_tokens=0,measured_duration_seconds=0.0,input_tokens=None,output_tokens=None,complete_usage=False)
    for state in states:
        for row in state['steps'].values():
            for attempt in row['attempts']:
                total['attempts']+=1
                path=Path(attempt['run_dir'])/'metrics.json'
                if not path.exists(): continue
                try: value=json.loads(path.read_text())
                except (ValueError,OSError): continue
                total['measured_duration_seconds']+=value.get('duration_seconds',0)
                if value.get('context'):
                    total['measured_context_attempts']+=1
                    total['known_explicit_characters']+=value['context']['explicit_characters']
                if value.get('input_tokens') is not None: total['known_input_tokens']+=value['input_tokens']
                if value.get('output_tokens') is not None: total['known_output_tokens']+=value['output_tokens']
                if value.get('input_tokens') is not None and value.get('output_tokens') is not None:
                    total['measured_usage_attempts']+=1
    total['complete_context']=bool(total['attempts']) and total['measured_context_attempts']==total['attempts']
    if total['complete_context']: total['explicit_characters']=total['known_explicit_characters']
    total['complete_usage']=bool(total['attempts']) and total['measured_usage_attempts']==total['attempts']
    if total['complete_usage']:
        total['input_tokens']=total['known_input_tokens'];total['output_tokens']=total['known_output_tokens']
    return total
