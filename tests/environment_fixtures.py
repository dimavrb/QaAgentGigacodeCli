"""Synthetic guide snapshots for protocol tests, never live readiness evidence."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def bind_environment(assignment, base, environment_id='fixture', product_version='v1'):
    base = Path(base)
    rid = assignment['request_id']
    ids = assignment.get('scenario_ids', [])
    points = [dict(point_id='P-' + str(index + 1), quote='Synthetic point ' + str(index + 1),
                   expected_result='Synthetic observation recorded', scenario_ids=[sid] if sid else [])
              for index, sid in enumerate(ids or [None])]
    guide = base / (rid + '.guide.txt')
    guide.write_text('\n'.join(p['quote'] for p in points))
    path = base / (rid + '.environment.json')
    path.write_text(json.dumps(dict(schema_version=1, task_id=assignment['task_id'],
        environment_id=environment_id, product_version=product_version,
        created_at=datetime.now(timezone.utc).isoformat(), scenario_ids=ids,
        admin_guide=dict(path=str(guide), sha256=hashlib.sha256(guide.read_bytes()).hexdigest()), points=points)))
    assignment['environment_assignment'] = str(path)
    return assignment


def attach_environment_checks(message, assignment, evidence):
    if not assignment.get('environment_assignment'):
        return message
    path = Path(assignment['environment_assignment'])
    scope = json.loads(path.read_text())
    payload = message['payload']
    payload.setdefault('open_questions', [])
    blocked = set(payload.get('blocked_scenarios', []))
    checks = []
    for point in scope['points']:
        status = 'blocked' if message['event'] == 'blocked' or blocked & set(point.get('scenario_ids', scope.get('scenario_ids', []))) else 'passed'
        checks.append(dict(point_id=point['point_id'], status=status,
            actions=['Synthetic fixture action'] if status == 'passed' else [],
            actual_result='Synthetic fixture observation' if status == 'passed' else 'Synthetic missing condition',
            observed_at=datetime.now(timezone.utc).isoformat(), evidence=list(evidence) if status == 'passed' else []))
    payload.update(environment_assignment_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), guide_checks=checks)
    return message
