#!/usr/bin/env python3
"""Generate/check finite constrained interaction coverage; no behavioral oracle."""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path


def fingerprint(model: dict) -> str:
    return hashlib.sha256(json.dumps(model, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def prepare(model: object, limit: int = 10000) -> tuple[list[str], list[dict], list[tuple[str, ...]]]:
    if not isinstance(model, dict) or type(model.get('schema_version')) is not int or model['schema_version'] != 1:
        raise ValueError('model schema_version must be 1')
    factors = model.get('factors')
    if not isinstance(factors, dict) or not 1 <= len(factors) <= 16:
        raise ValueError('factors must contain 1..16 named domains')
    for name, domain in factors.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError('factor names must be non-empty strings')
        if (not isinstance(domain, list) or not domain or
                any(not isinstance(v, str) or not v.strip() for v in domain) or
                len(domain) != len(set(domain))):
            raise ValueError(f'factor {name} needs unique non-empty string values')
    names = sorted(factors)
    strength = model.get('strength', 2)
    if type(strength) is not int or not 1 <= strength <= len(names):
        raise ValueError('strength must be an integer from 1 to the factor count')
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError('limit must be an integer from 1 to 10000')
    if math.prod(len(domain) for domain in factors.values()) > limit:
        raise ValueError(f'full assignment space exceeds limit {limit}; split the model explicitly')

    def assignment(values: object, full: bool) -> None:
        if not isinstance(values, dict) or not values or (full and set(values) != set(names)):
            raise ValueError('assignment must specify valid factors (seeds/rows: all factors)')
        for name, value in values.items():
            if name not in factors or not isinstance(value, str) or value not in factors[name]:
                raise ValueError(f'unknown factor/value in assignment: {name}')

    exclusions = model.get('exclude', [])
    if not isinstance(exclusions, list):
        raise ValueError('exclude must be a list of partial assignments')
    for values in exclusions:
        assignment(values, False)
    candidates = [dict(zip(names, values))
                  for values in itertools.product(*(factors[name] for name in names))]
    candidates = [values for values in candidates
                  if not any(all(values[k] == v for k, v in excluded.items()) for excluded in exclusions)]
    if not candidates:
        raise ValueError('constraints leave no feasible assignments')
    groups = model.get('interaction_groups', [])
    if not isinstance(groups, list):
        raise ValueError('interaction_groups must be a list')
    scopes = set(itertools.combinations(names, strength))
    for group in groups:
        if (not isinstance(group, list) or len(group) < 2 or
                any(not isinstance(name, str) or name not in factors for name in group) or
                len(group) != len(set(group))):
            raise ValueError('interaction group needs distinct existing factor names')
        scopes.add(tuple(sorted(group)))
    if len(candidates) * len(scopes) > 250000:
        raise ValueError('interaction enumeration exceeds 250000 visits; split the model explicitly')
    seeds = model.get('seeds', [])
    if not isinstance(seeds, list):
        raise ValueError('seeds must be a list')
    seen = set()
    for seed in seeds:
        if not isinstance(seed, dict) or not isinstance(seed.get('combination_id'), str) or not seed['combination_id'].strip():
            raise ValueError('seed needs combination_id')
        if seed['combination_id'] in seen:
            raise ValueError('duplicate seed combination_id')
        seen.add(seed['combination_id'])
        assignment(seed.get('values'), True)
        if seed['values'] not in candidates:
            raise ValueError(f'mandatory seed {seed["combination_id"]} conflicts with constraints')
        scenario_ids(seed.get('scenario_ids', []))
    return names, candidates, sorted(scopes)


def scenario_ids(value: object) -> None:
    if (not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value)
            or len(value) != len(set(value))):
        raise ValueError('scenario_ids must contain unique non-empty strings')


def interactions(values: dict, scopes: list[tuple[str, ...]]) -> set[tuple]:
    return {tuple((name, values[name]) for name in scope) for scope in scopes}


def generate(model: dict, limit: int = 10000) -> dict:
    _, candidates, scopes = prepare(model, limit)
    candidate_sets = [interactions(values, scopes) for values in candidates]
    required = set().union(*candidate_sets)
    uncovered = required.copy()
    rows = [dict(combination_id=seed['combination_id'], values=dict(seed['values']),
                 scenario_ids=list(seed.get('scenario_ids', []))) for seed in model.get('seeds', [])]
    used = {row['combination_id'] for row in rows}
    for row in rows:
        uncovered.difference_update(interactions(row['values'], scopes))
    next_id = 1
    while uncovered:
        index = max(range(len(candidates)), key=lambda i: len(candidate_sets[i] & uncovered))
        while f'COMB-{next_id:03}' in used:
            next_id += 1
        identifier = f'COMB-{next_id:03}'
        used.add(identifier)
        rows.append(dict(combination_id=identifier, values=candidates[index].copy(), scenario_ids=[]))
        next_id += 1
        uncovered.difference_update(candidate_sets[index])
    return dict(schema_version=1, model_sha256=fingerprint(model), algorithm='deterministic_greedy',
                feasible_assignments=len(candidates), required_interactions=len(required), rows=rows)


def check(model: dict, report: object, limit: int = 10000, catalog: dict | None = None) -> list[str]:
    names, candidates, scopes = prepare(model, limit)
    if not isinstance(report, dict) or type(report.get('schema_version')) is not int or report['schema_version'] != 1:
        return ['report schema_version must be 1']
    errors = []
    if report.get('model_sha256') != fingerprint(model):
        errors.append('model_sha256 differs from current model')
    rows = report.get('rows')
    if not isinstance(rows, list) or not rows:
        return errors + ['rows must be a non-empty list']
    catalog_ids = None
    if catalog is not None:
        from validate_test_cases import validate_catalog
        catalog_errors = validate_catalog(catalog)
        if catalog_errors:
            return errors + [f'catalog: {error}' for error in catalog_errors]
        catalog_ids = {case['scenario_id'] for case in catalog['test_cases']}
    required = set().union(*(interactions(values, scopes) for values in candidates))
    covered = set()
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('combination_id'), str) or not row['combination_id'].strip():
            errors.append('row needs combination_id')
            continue
        identifier = row['combination_id']
        if identifier in indexed:
            errors.append(f'duplicate combination_id: {identifier}')
        indexed[identifier] = row
        values = row.get('values')
        if not isinstance(values, dict) or set(values) != set(names) or values not in candidates:
            errors.append(f'{identifier}: infeasible or incomplete assignment')
        else:
            covered.update(interactions(values, scopes))
        ids = row.get('scenario_ids')
        try:
            scenario_ids(ids)
            if catalog_ids is not None and (not ids or set(ids) - catalog_ids):
                errors.append(f'{identifier}: scenario_ids must reference existing catalog cases')
        except ValueError as exc:
            errors.append(f'{identifier}: {exc}')
    for seed in model.get('seeds', []):
        row = indexed.get(seed['combination_id'])
        if row is None or row.get('values') != seed['values']:
            errors.append(f'mandatory seed missing or changed: {seed["combination_id"]}')
        elif (isinstance(row.get('scenario_ids'), list) and all(isinstance(v, str) for v in row['scenario_ids'])
              and not set(seed.get('scenario_ids', [])) <= set(row['scenario_ids'])):
            errors.append(f'mandatory seed scenario links changed: {seed["combination_id"]}')
    missing = required - covered
    if missing:
        errors.append(f'missing {len(missing)} required interactions; first: {min(missing)}')
    if catalog is not None and not errors:
        from combination_coverage import check_bindings
        errors.extend(check_bindings(model, report, catalog))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--output', type=Path)
    mode.add_argument('--check', type=Path)
    parser.add_argument('--catalog', type=Path, help='Check ready rows, factor bindings and applicable behavior rules')
    parser.add_argument('--limit', type=int, default=10000)
    args = parser.parse_args()
    if args.catalog and not args.check:
        parser.error('--catalog requires --check')
    try:
        model = json.loads(args.model.read_text(encoding='utf-8'))
        if args.output:
            report = generate(model, args.limit)
            with args.output.open('x', encoding='utf-8') as output:
                json.dump(report, output, ensure_ascii=False, indent=2)
                output.write('\n')
            print(f'Generated {len(report["rows"])} design rows; behavioral expectations not verified')
        else:
            report = json.loads(args.check.read_text(encoding='utf-8'))
            catalog = json.loads(args.catalog.read_text(encoding='utf-8')) if args.catalog else None
            errors = check(model, report, args.limit, catalog)
            if errors:
                for error in errors:
                    print(error)
                return 2
            print('Valid interaction coverage' + (' and concrete case/rule bindings' if catalog is not None else '') +
                  '; behavior and scenario semantics need independent review')
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'Combination error: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
