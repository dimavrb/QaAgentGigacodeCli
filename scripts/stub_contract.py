"""Validate sbermock intent and observations; no MCP calls or product tests."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from analysis_contract import id_list

STUB_READY_EVENTS = {'stubs_ready', 'stubs_partial'}
STUB_EVENTS = STUB_READY_EVENTS | {'stubs_configured'}
STUB_PLAN_EVENT = 'stubs_plan_ready'
CONTEXT = ('task_id', 'request_id', 'environment_id', 'provider', 'mcp_server', 'project_id', 'namespace')
SCHEMA_VERSION = 2


def text(value):
    return isinstance(value, str) and bool(value.strip())


def instant(value):
    try:
        if text(value):
            result = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if result.tzinfo is not None:
                return result
    except ValueError:
        pass
    return None


def exact(expected, actual):
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return expected.keys() == actual.keys() and all(exact(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(exact(x, y) for x, y in zip(expected, actual))
    return expected == actual


def subset(expected, actual):
    """Empty objects mean empty objects; arrays keep exact length and order."""
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        if not expected:
            return not actual
        return all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(subset(x, y) for x, y in zip(expected, actual))
    return expected == actual


def definition_matches(expected, actual):
    # Provider metadata may add root fields. Specified behavioral branches are exact.
    return (isinstance(expected, dict) and isinstance(actual, dict)
            and all(k in actual and exact(v, actual[k]) for k, v in expected.items()))


def parse_stub_json(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError('non-finite JSON number: ' + value)

    def floating(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('non-finite JSON number: ' + value)
        return parsed

    return json.loads(content, object_pairs_hook=pairs, parse_constant=nonfinite, parse_float=floating)


def read_stub_json(path: Path):
    return parse_stub_json(path.read_bytes())


def assignment_digest(assignment):
    return hashlib.sha256(json.dumps(assignment, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def validate_assignment(assignment):
    """Validate a candidate scope before any provider mutation is assigned."""
    if not isinstance(assignment, dict):
        return ['stub assignment must be an object']
    errors = []
    if type(assignment.get('schema_version')) is not int or assignment['schema_version'] != SCHEMA_VERSION:
        errors.append('assignment.schema_version must equal 2; v1 needs a new intent')
    for k in CONTEXT:
        if not text(assignment.get(k)):
            errors.append(f'assignment.{k} must be a non-empty string')
        elif assignment[k].strip().casefold() in ('unknown', 'не настроено', 'неизвестно'):
            errors.append(f'assignment.{k} must be resolved before preparing mutations')
    if assignment.get('provider') != 'sbermock':
        errors.append('assignment.provider must equal sbermock')
    if assignment.get('mode') not in ('configure_only','verify') or instant(assignment.get('issued_at')) is None:
        errors.append('assignment needs a valid mode and timezone-aware issued_at')
    elif instant(assignment['issued_at']) > datetime.now(timezone.utc):
        errors.append('assignment.issued_at cannot be in the future')
    assigned = id_list(assignment.get('scenario_ids'), 'assignment.scenario_ids', errors)
    cases = assignment.get('cases')
    if not assigned or not isinstance(cases, list) or not cases:
        return errors + ['assignment needs scenarios and cases']
    seen, covered = set(), set()
    for case in cases:
        if not isinstance(case, dict) or not text(case.get('case_id')):
            errors.append('assignment case needs case_id')
            continue
        cid = case['case_id']
        if cid in seen:
            errors.append('assignment has duplicate case_id')
        seen.add(cid)
        ids = id_list(case.get('scenario_ids'), cid + '.scenario_ids', errors)
        if not ids or not ids <= assigned:
            errors.append(f'{cid}: cases must refer to assigned scenarios')
        else:
            covered |= ids
        if case.get('kind') not in ('REST','KAFKA') or any(not text(case.get(k)) for k in ('requirement_source','purpose')):
            errors.append(f'{cid}: kind/source/purpose required')
        if not isinstance(case.get('definition'), dict) or not case['definition']:
            errors.append(f'{cid}: non-empty provider definition required')
        operations = case.get('allowed_operations')
        if not isinstance(operations, list) or not operations or any(not isinstance(x,str) or x not in ('created','reused','updated') for x in operations):
            errors.append(f'{cid}: permitted operations required')
        target = case.get('target_stub_id')
        if 'target_stub_id' not in case or target is not None and not text(target) or isinstance(operations,list) and 'updated' in operations and not text(target):
            errors.append(f'{cid}: updates need an explicitly assigned target; otherwise explicit null is allowed')
        plans = case.get('probes')
        if not isinstance(plans, dict):
            errors.append(f'{cid}: probes must be an object')
            continue
        if any(k not in ('positive','negative') for k in plans):
            errors.append(f'{cid}: represent additional probe variants as explicit cases, not ignored plan keys')
        if assignment.get('mode') == 'verify':
            for key in ('positive','negative'):
                plan = plans.get(key)
                if not isinstance(plan, dict) or any(not isinstance(plan.get(k),dict) or not plan[k] for k in ('input','expected')):
                    errors.append(f'{cid}.{key}: planned input/expected required')
                elif case.get('kind') == 'KAFKA' and (not text(plan.get('correlation_id')) or type(plan.get('window_ms')) is not int or plan['window_ms'] <= 0):
                    errors.append(f'{cid}.{key}: Kafka correlation/window required')
            if case.get('kind') == 'KAFKA':
                p, n = plans.get('positive'), plans.get('negative')
                if isinstance(p,dict) and isinstance(n,dict) and p.get('correlation_id') == n.get('correlation_id'):
                    errors.append(f'{cid}: Kafka probe correlations must differ')
            p, n = plans.get('positive'), plans.get('negative')
            if isinstance(p,dict) and isinstance(n,dict) and exact(p.get('input'),n.get('input')) and exact(p.get('preconditions',{}),n.get('preconditions',{})):
                errors.append(f'{cid}: negative control must change input or preconditions')
    if covered != assigned:
        errors.append('assignment cases must cover all assigned scenarios')
    return errors


def normalize_assignment(record):
    """Freeze independent intent before the worker receives permission to act."""
    if record['agent'] != 'qa-stub-builder':
        raise ValueError('stub_assignment requires qa-stub-builder')
    path = Path(record['stub_assignment']).resolve()
    scope = read_stub_json(path)
    errors = validate_assignment(scope)
    if not errors:
        if any(scope[key] != record[key] for key in ('task_id', 'request_id')):
            errors.append('stub intent task/request differs from assignment')
        if set(scope['scenario_ids']) != set(record['scenario_ids']):
            errors.append('stub intent scenarios differ from assignment')
    if errors:
        raise ValueError('Stub preflight: ' + '; '.join(errors))
    if str(path) not in record['inputs']:
        record['inputs'].append(str(path))


def validate_stub_plan(message, base: Path):
    """A draft is reviewable intent, not permission to mutate or readiness."""
    payload = message['payload']
    ref = payload.get('assignment_artifact')
    if not text(ref) or ref not in message.get('artifacts', []):
        return ['plan artifacts must include assignment_artifact']
    path = Path(ref)
    assignment = read_stub_json(path if path.is_absolute() else base / path)
    errors = validate_assignment(assignment)
    if errors:
        return errors
    for k in CONTEXT:
        actual = message.get(k) if k in ('task_id','request_id') else payload.get(k)
        if actual != assignment.get(k):
            errors.append(f'plan.{k} differs from draft assignment')
    if set(payload['scenario_ids']) != set(assignment['scenario_ids']):
        errors.append('plan scenarios differ from draft assignment')
    return errors


def validate_stub_message(payload, event, assigned=None):
    if not isinstance(payload, dict):
        return ['stub payload must be an object']
    errors = []
    configured = event == 'stubs_configured'
    key = 'configured_scenarios' if configured else 'scenario_ids'
    ready = id_list(payload.get(key), 'payload.' + key, errors)
    blocked = id_list(payload.get('blocked_scenarios'), 'payload.blocked_scenarios', errors)
    if ready is not None and blocked is not None:
        if ready & blocked:
            errors.append('stub ready and blocked scenario IDs overlap')
        if assigned is not None and ready | blocked != set(assigned):
            errors.append('stub scenario IDs differ from assignment')
        if not ready or (event == 'stubs_ready' and blocked) or (event == 'stubs_partial' and not blocked):
            errors.append('stub event requires a ready part and the appropriate blocked part')
    if configured and payload.get('scenario_ids', []) != []:
        errors.append('configured stubs cannot declare execution-ready scenario_ids')
    return errors


def validate_stub_bundle(message, base: Path, assignment=None, *, artifact_paths=None):
    """Validate against coordinator-owned intent, not expectations chosen by a probe."""
    if not isinstance(assignment, dict):
        return ['stub assignment is required; pass coordinator-owned --stub-assignment']
    assignment_errors = validate_assignment(assignment)
    if assignment_errors:
        return assignment_errors
    if not isinstance(message, dict) or not isinstance(message.get('payload'), dict):
        return ['stub result must have an object payload']
    payload = message['payload']
    errors = validate_stub_message(payload, message.get('event'))
    if errors:
        return errors
    expected = {k: assignment.get(k) for k in CONTEXT}
    issued = instant(assignment.get('issued_at'))
    mode = assignment.get('mode')
    upper = datetime.now(timezone.utc)

    def check_context(record, label):
        if type(record.get('schema_version')) is not int or record['schema_version'] != SCHEMA_VERSION:
            errors.append(f'{label}.schema_version must equal {SCHEMA_VERSION}; migrate v1 intent before acceptance')
        for k, value in expected.items():
            if record.get(k) != value or not text(value):
                errors.append(f'{label}.{k} differs from coordinator assignment')

    for k in CONTEXT:
        actual = message.get(k) if k in ('task_id', 'request_id') else payload.get(k)
        if actual != expected[k]:
            errors.append(f'result.{k} differs from coordinator assignment')
    assigned = set(assignment['scenario_ids'])
    errors.extend(validate_stub_message(payload, message.get('event'), assigned))

    def local_path(ref):
        path = Path(ref)
        return path if path.is_absolute() else base / path

    loaded_hashes = {}

    def load(ref, label, digest=None):
        if not text(ref):
            errors.append(f'{label}: local artifact path required')
            return {}
        try:
            path = local_path(ref)
            content = path.read_bytes()
            if artifact_paths is not None:
                artifact_paths.add(str(path.resolve()))
            actual_digest = hashlib.sha256(content).hexdigest()
            loaded_hashes[label] = actual_digest
            if digest is not None and actual_digest != digest:
                errors.append(f'{label}: artifact hash differs')
            value = parse_stub_json(content)
            if not isinstance(value, dict):
                raise ValueError('artifact must be an object')
            return value
        except (OSError, ValueError, UnicodeError) as exc:
            errors.append(f'{label}: cannot read artifact: {exc}')
            return {}

    for k in ('profile_artifact', 'verification_artifact'):
        if payload.get(k) not in message.get('artifacts', []):
            errors.append(f'artifacts must include {k}')
    profile = load(payload.get('profile_artifact'), 'profile')
    proof = load(payload.get('verification_artifact'), 'verification')
    for record, label in ((profile, 'profile'), (proof, 'verification')):
        check_context(record, label)
        if record.get('assignment_sha256') != assignment_digest(assignment):
            errors.append(f'{label}: assignment hash differs or is missing')
    if profile:
        if loaded_hashes.get('profile') != proof.get('profile_sha256'):
            errors.append('verification profile hash differs or is missing')
    prepared = instant(profile.get('prepared_at'))
    if prepared is None or (issued is not None and prepared < issued):
        errors.append('profile.prepared_at must not predate assignment')
    elif prepared > upper:
        errors.append('profile.prepared_at cannot be in the future')
    verification_started = instant(profile.get('verification_started_at'))
    if verification_started is None or prepared is not None and verification_started < prepared:
        errors.append('profile.verification_started_at must follow preparation and all mutations')
    elif verification_started > upper:
        errors.append('profile.verification_started_at cannot be in the future')
    declared = id_list(profile.get('scenario_ids'), 'profile.scenario_ids', errors)
    if declared != assigned:
        errors.append('profile must cover the full assigned scenario set')
    if proof.get('limitations') != payload.get('limitations'):
        errors.append('verification limitations differ from result')

    def index(records, label):
        result = {}
        if not isinstance(records, list) or not records:
            errors.append(f'{label} must be a non-empty list')
            return result
        for r in records:
            if not isinstance(r, dict) or not text(r.get('case_id')):
                errors.append(f'{label}: record needs case_id')
                continue
            if r['case_id'] in result:
                errors.append(f'{label}: duplicate case_id')
            result[r['case_id']] = r
        return result

    intents = index(assignment.get('cases'), 'assignment.cases')
    stubs = index(profile.get('stubs'), 'profile.stubs')
    checks = index(proof.get('checks'), 'verification.checks')
    if intents.keys() != stubs.keys() or intents.keys() != checks.keys():
        errors.append('assignment, profile and verification cases differ')
    covered, failed, server_ids = set(), set(), set()
    configured_event = message.get('event') == 'stubs_configured'

    def observation(ref, case, key, stub, minimum=None):
        if not isinstance(ref, dict) or not text(ref.get('sha256')):
            errors.append(f'{case}.{key}: needs artifact and sha256')
            return {}, None
        record = load(ref.get('artifact'), f'{case}.{key}', ref['sha256'])
        check_context(record, f'{case}.{key}')
        if record.get('stub_id') != stub.get('stub_id') or record.get('kind') != stub.get('kind'):
            errors.append(f'{case}.{key}: observation belongs to another stub/kind')
        at = instant(record.get('observed_at'))
        if at is None:
            errors.append(f'{case}.{key}: observed_at needs ISO time with timezone')
        elif minimum is not None and at < minimum:
            errors.append(f'{case}.{key}: observation predates the current preparation/readback')
        elif at > upper:
            errors.append(f'{case}.{key}: observation cannot be in the future')
        return record, at

    for case, intent in intents.items():
        stub, check = stubs.get(case, {}), checks.get(case, {})
        scenarios = id_list(intent.get('scenario_ids'), case + '.scenario_ids', errors)
        if not scenarios or not assigned or not scenarios <= assigned:
            errors.append(f'{case}: scenarios must be a non-empty subset of assignment')
            scenarios = set()
        covered |= scenarios
        if intent.get('kind') not in ('REST', 'KAFKA') or any(not text(intent.get(k)) for k in ('requirement_source', 'purpose')):
            errors.append(f'{case}: kind, requirement_source and purpose required')
        if not isinstance(intent.get('definition'), dict) or not intent['definition']:
            errors.append(f'{case}: definition must use a non-empty provider object')
        for k in ('kind', 'requirement_source', 'purpose', 'definition', 'probes'):
            if not exact(stub.get(k), intent.get(k)):
                errors.append(f'{case}.{k} differs from coordinator intent')
        observed_scenarios = id_list(stub.get('scenario_ids'), case + '.profile.scenario_ids', errors)
        if observed_scenarios != scenarios:
            errors.append(f'{case}: profile dependencies differ from intent')
        allowed = intent.get('allowed_operations')
        if not isinstance(allowed, list) or not allowed or any(not isinstance(x, str) or x not in ('created','reused','updated') for x in allowed):
            errors.append(f'{case}: allowed_operations must specify permitted mutations/reuse')
            allowed = []
        target = intent.get('target_stub_id')
        if 'target_stub_id' not in intent or (target is not None and not text(target)):
            errors.append(f'{case}: target_stub_id must be explicit null or a string')
        operation = stub.get('operation')
        if not isinstance(operation, str) or operation not in ('created','reused','updated','pending'):
            errors.append(f'{case}: unknown operation')
            operation = 'invalid'
        if operation not in allowed and operation != 'pending':
            errors.append(f'{case}: operation is outside the assigned scope')
        stub_id = stub.get('stub_id')
        if 'stub_id' not in stub or (stub_id is not None and not text(stub_id)) or (operation != 'pending' and not text(stub_id)):
            errors.append(f'{case}: applied stub needs a provider stub_id; pending may use explicit null')
        if target is not None and stub_id != target or operation == 'updated' and target is None:
            errors.append(f'{case}: existing stub must match the explicitly assigned target')
        if text(stub_id):
            identity = (intent.get('kind') if text(intent.get('kind')) else '', stub_id)
            if identity in server_ids:
                errors.append('duplicate provider stub_id')
            server_ids.add(identity)
        resolved = instant(stub.get('resolved_at'))
        if operation != 'pending' and (resolved is None or prepared is not None and resolved < prepared):
            errors.append(f'{case}: resolved_at must follow current profile preparation')
        if resolved is not None and verification_started is not None and verification_started < resolved:
            errors.append(f'{case}: verification must start after all resolved mutations')
        if resolved is not None and resolved > upper:
            errors.append(f'{case}: resolved_at cannot be in the future')
        if operation == 'pending' and stub.get('resolved_at') is not None:
            errors.append(f'{case}: pending operation needs null resolved_at')
        cleanup = stub.get('cleanup')
        actions = {'created':'delete','reused':'none','updated':'restore','pending':'inspect'}
        if not isinstance(cleanup, dict) or cleanup.get('action') != actions.get(operation) or not text(cleanup.get('reason')):
            errors.append(f'{case}: cleanup must match the operation and include a reason')
        elif operation == 'updated':
            before, at = observation(cleanup.get('before'), case, 'before', stub, issued)
            if not isinstance(before.get('definition'), dict) or not before['definition'] or not exact(before.get('definition'), cleanup.get('previous_definition')):
                errors.append(f'{case}: update needs a hashed previous definition for restoration')
            if at is not None and resolved is not None and at > resolved:
                errors.append(f'{case}: restoration snapshot must precede mutation resolution')
        plans = intent.get('probes')
        if not isinstance(plans, dict):
            errors.append(f'{case}: probes must be an object')
            plans = {}
        if mode == 'verify':
            for key in ('positive','negative'):
                p = plans.get(key)
                if not isinstance(p, dict) or any(not isinstance(p.get(k), dict) or not p[k] for k in ('input','expected')):
                    errors.append(f'{case}.{key}: coordinator probe input and expected required')
            pos, neg = plans.get('positive', {}), plans.get('negative', {})
            if isinstance(pos, dict) and isinstance(neg, dict) and exact(pos.get('input'), neg.get('input')) and exact(pos.get('preconditions', {}), neg.get('preconditions', {})):
                errors.append(f'{case}: negative control must change input or preconditions')
        status = check.get('status')
        if status not in ('verified','configured','blocked'):
            errors.append(f'{case}: check must be verified, configured or blocked')
        if mode == 'configure_only' and (status == 'verified' or 'positive' in check or 'negative' in check):
            errors.append(f'{case}: configure_only must not run or claim probes')
        if status == 'blocked':
            failed |= scenarios
            if not text(check.get('reason')):
                errors.append(f'{case}: blocked check needs reason')
            continue
        if operation == 'pending' or not text(stub_id):
            errors.append(f'{case}: pending write cannot be verified/configured')
        minimum = max(t for t in (resolved, verification_started, prepared, issued) if t is not None)
        readback, read_at = observation(check.get('readback'), case, 'readback', stub, minimum)
        if not definition_matches(intent.get('definition'), readback.get('definition')):
            errors.append(f'{case}: readback differs from desired definition')
        if status != 'verified':
            if not text(check.get('reason')):
                errors.append(f'{case}: configured check must explain why runtime was not verified')
            if not configured_event:
                failed |= scenarios
            continue
        for key in ('positive','negative'):
            record, at = observation(check.get(key), case, key, stub, read_at or resolved or issued)
            plan = plans.get(key, {})
            if not isinstance(plan, dict):
                plan = {}
            if (not exact(record.get('input'), plan.get('input')) or not exact(record.get('expected'), plan.get('expected'))
                    or not exact(record.get('preconditions', {}), plan.get('preconditions', {}))):
                errors.append(f'{case}.{key}: probe differs from assigned input/expected')
            if (record.get('control') != key or record.get('passed') is not True
                    or not isinstance(record.get('actual'), dict) or not record['actual']
                    or not subset(plan.get('expected'), record.get('actual'))):
                errors.append(f'{case}.{key}: probe has not passed or expected/actual differ')
            if intent.get('kind') == 'KAFKA':
                capture = record.get('capture')
                capture = capture if isinstance(capture, dict) else {}
                started, ended = instant(capture.get('started_at')), instant(capture.get('ended_at'))
                correlation = plan.get('correlation_id')
                window = plan.get('window_ms')
                if not text(correlation) or capture.get('correlation_id') != correlation:
                    errors.append(f'{case}.{key}: Kafka correlation differs or is missing')
                if type(window) is not int or window <= 0 or not isinstance(capture.get('position'), dict) or not capture['position']:
                    errors.append(f'{case}.{key}: Kafka needs planned window and captured position')
                if (started is None or ended is None or ended < started or at is None or ended > at
                        or read_at is not None and started < read_at or ended > upper):
                    errors.append(f'{case}.{key}: invalid or stale Kafka observation interval')
                elif key == 'negative' and type(window) is int and (ended - started).total_seconds() * 1000 < window:
                    errors.append(f'{case}.{key}: Kafka absence needs the full planned observation window')
    if covered != assigned:
        errors.append('every assigned scenario needs at least one stub in intent')
    if failed != set(payload['blocked_scenarios']):
        errors.append('blocked scenarios must exactly reflect all unverified stub dependencies')
    if not configured_event and mode != 'verify':
        errors.append('execution readiness requires assignment.mode=verify')
    return errors
