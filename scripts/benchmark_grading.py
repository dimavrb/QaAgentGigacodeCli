"""Frozen grading copies and file-based acceptance. Notifications are never inputs."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import secrets
import re

from check_benchmark_review import load_suite, make_template, validate_review


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def relative_file(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('package path must be relative and contained')
    root = Path(root).resolve()
    path = root / relative
    if path.is_symlink() or path.resolve() != path.absolute() or not path.is_file():
        raise ValueError('package file missing, symlinked or escaping root: ' + relative)
    return path


def make_pack(run_directory, output):
    # Import late to avoid circularity with the comparison's mandatory acceptance gate.
    from compare_profile_skills import load_run
    run_sha256 = sha(Path(run_directory).resolve() / 'run.json')
    root, run, suite, records = load_run(run_directory, require_grading=False)
    if run['status'] != 'captured':
        raise ValueError('grading package requires a complete captured run; execution failures stay failures')
    output = Path(output).resolve()
    if output == root or output.is_relative_to(root):
        raise ValueError('grading copies must live outside the run directory')
    output.mkdir(parents=True, exist_ok=False)
    files, source_hashes = {}, {}
    def snapshot(relative):
        source = relative_file(root, relative)
        before = sha(source)
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != before:
            raise ValueError('source changed during copy: ' + relative)
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        files[relative] = before
        source_hashes[relative] = before
    snapshot('suite.json')
    for case in suite['cases']:
        snapshot(case['prompt_file'])
    source_hashes['run.json'] = run_sha256
    for (trial, cid), record in sorted(records.items()):
        for name in ('prompt.md', 'system.md', 'response.md'):
            snapshot(f'trial-{trial}/{cid}/{name}')
        for name in ('raw.json', 'execution.json', 'instructions.json'):
            relative = f'trial-{trial}/{cid}/{name}'
            source_hashes[relative] = sha(relative_file(root, relative))
    manifest = dict(schema_version=1, kind='benchmark_grading_pack', pack_id=secrets.token_hex(16),
                    suite_sha256=run['suite_sha256'], repeats=run['repeats'],
                    measurement_kind=run.get('measurement_kind'),
                    expected_checks_per_trial=sum(len(c['checks']) for c in suite['cases']), files=files,
                    source_hashes=source_hashes)
    for trial in range(1, run['repeats'] + 1):
        template = make_template(suite, run['suite_sha256'], run['agent_version'])
        # Metadata and measurements come from captured artifacts, not the evaluator.
        first = records[trial, suite['cases'][0]['case_id']]['review']
        template.update(model=first['model'], configuration=first['configuration'],
                        grading_pack_id=manifest['pack_id'], grading_trial=trial)
        for row in template['cases']:
            original = records[trial, row['case_id']]['row']
            for field in ('output_artifact', 'output_sha256', 'seconds', 'input_tokens', 'output_tokens'):
                row[field] = original[field]
        write(output / f'trial-{trial}/review-template.json', template)
        files[f'trial-{trial}/review-template.json'] = sha(output / f'trial-{trial}/review-template.json')
    write(output / 'manifest.json', manifest)
    verify_pack(output, root)
    return manifest


def verify_pack(pack, run_directory=None):
    pack = Path(pack).resolve()
    manifest = read(relative_file(pack, 'manifest.json'))
    if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
            or manifest['schema_version'] != 1 or manifest.get('kind') != 'benchmark_grading_pack'
            or not isinstance(manifest.get('pack_id'), str) or not manifest['pack_id']):
        raise ValueError('unrecognized grading manifest')
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise ValueError('grading manifest needs frozen files')
    suite, fingerprint = load_suite(relative_file(pack, 'suite.json'))
    if fingerprint != manifest.get('suite_sha256'):
        raise ValueError('grading suite changed')
    repeats = manifest.get('repeats')
    if type(repeats) is not int or not 1 <= repeats <= 20:
        raise ValueError('invalid grading repeats')
    if manifest.get('measurement_kind') not in ('live', 'fixture'):
        raise ValueError('unknown grading measurement kind')
    expected = {'suite.json', *(c['prompt_file'] for c in suite['cases'])}
    expected.update(f'trial-{trial}/{case["case_id"]}/{name}' for trial in range(1, repeats + 1)
                    for case in suite['cases'] for name in ('prompt.md', 'system.md', 'response.md'))
    templates = {f'trial-{trial}/review-template.json' for trial in range(1, repeats + 1)}
    expected.update(templates)
    if set(files) != expected or manifest.get('expected_checks_per_trial') != sum(len(c['checks']) for c in suite['cases']):
        raise ValueError('grading package does not contain the complete rubric/repeats')
    for relative, checksum in files.items():
        if sha(relative_file(pack, relative)) != checksum:
            raise ValueError('grading copy sha256 differs: ' + relative)
    source_hashes = manifest.get('source_hashes')
    source_expected = (expected - templates) | {'run.json'} | {f'trial-{trial}/{case["case_id"]}/{name}'
                      for trial in range(1, repeats + 1) for case in suite['cases']
                      for name in ('raw.json', 'execution.json', 'instructions.json')}
    if not isinstance(source_hashes, dict) or set(source_hashes) != source_expected:
        raise ValueError('grading original snapshot set is incomplete')
    if any(source_hashes.get(name) != checksum for name, checksum in files.items() if name not in templates):
        raise ValueError('grading copies differ from declared originals')
    if run_directory is not None:
        run_directory = Path(run_directory).resolve()
        original_run = read(relative_file(run_directory, 'run.json'))
        for key in ('suite_sha256', 'repeats', 'measurement_kind'):
            if original_run.get(key) != manifest.get(key):
                raise ValueError('grading scope differs from original run: ' + key)
        if original_run.get('status') != 'captured':
            raise ValueError('grading original is not a complete capture')
        for name, checksum in source_hashes.items():
            if sha(relative_file(run_directory, name)) != checksum:
                raise ValueError('grading original changed: ' + name)
    return manifest, suite


def check_pack_review(pack, review_path, require_execution=True):
    pack, review_path = Path(pack).resolve(), Path(review_path).resolve()
    manifest, suite = verify_pack(pack)
    if not review_path.is_relative_to(pack) or review_path.is_symlink():
        raise ValueError('review must be created inside the grading package')
    review = read(review_path)
    if not isinstance(review, dict):
        raise ValueError('review must be a JSON object')
    trial = review.get('grading_trial')
    if (type(trial) is not int or not 1 <= trial <= manifest['repeats']
            or review.get('grading_pack_id') != manifest['pack_id']
            or review.get('grading_manifest_sha256') != sha(pack / 'manifest.json')
            or review_path.parent != pack / f'trial-{trial}'):
        raise ValueError('review is not bound to the frozen package and trial')
    base = pack / f'trial-{trial}'
    errors = validate_review(review, suite, manifest['suite_sha256'], base, base)
    if errors:
        raise ValueError('; '.join(errors))
    from evidence_language import check_review_language
    errors.extend(check_review_language(review))
    template = read(base / 'review-template.json')
    for field in ('agent_version', 'model', 'configuration'):
        if review.get(field) != template.get(field):
            errors.append('review altered captured metadata: ' + field)
    for case in review.get('cases', []):
        expected_artifact = case['case_id'] + '/response.md'
        if case.get('output_artifact') != expected_artifact:
            errors.append('review must use its own package response.md')
        # Evaluation metadata cannot replace captured measurements.
        original = next(c for c in template['cases'] if c['case_id'] == case['case_id'])
        for field in ('output_sha256', 'seconds', 'input_tokens', 'output_tokens'):
            if case.get(field) != original[field]:
                errors.append('review altered captured field: ' + field)
        system = (base / case['case_id'] / 'system.md').read_text()
        supplied = set(re.findall(r'^Источник инструкций: (.+)$', system, flags=re.MULTILINE))
        supplied.update(Path(name).name for name in tuple(supplied))
        for check in case['checks']:
            if check.get('literal_defect_quote') is True:
                response = (base / case['case_id'] / 'response.md').read_text()
                normalized = ' '.join(response.split())
                pieces = [' '.join(part.split()) for part in re.split(r'…|\.{3,}', check['evidence'])]
                if any(part and part not in normalized for part in pieces):
                    errors.append('literal defect exception includes a fragment absent from response.md')
            missing = check.get('missing_sources', [])
            note = str(check.get('note', '')).casefold()
            absence = r'(нет|отсутств|недоступ|не\s+(?:(?:был[аи]?\s+)?передан|содерж|найден|предостав)|неизвест)'
            standard = r'(норматив|стандарт|инструкц)'
            if re.search(absence + r'.*' + standard + '|' + standard + r'.*' + absence, note) and not missing:
                errors.append('instruction-absence claim requires explicit missing_sources')
            if not isinstance(missing, list) or any(not isinstance(s, str) or not s for s in missing):
                errors.append('missing_sources must list explicit instruction names')
            elif any(source in supplied for source in missing):
                errors.append('claimed missing instruction is present in the supplied system.md')
    count = sum(len(c['checks']) for c in review.get('cases', []))
    if count != manifest['expected_checks_per_trial']:
        errors.append('review record count differs from required checks per repeat')
    if errors:
        raise ValueError('; '.join(errors))
    if require_execution and manifest['measurement_kind'] == 'live':
        validate_grading_execution(pack, review_path)
    return review, manifest


def validate_grading_execution(pack, review_path):
    """Require a host-created isolated grader capture for live acceptance."""
    from run_benchmark_grader import grading_prompt
    from run_agent_benchmark import terminal_response
    audit_path = Path(review_path).with_suffix(Path(review_path).suffix + '.execution.json')
    if not audit_path.is_file():
        raise ValueError('live review requires an isolated grader execution receipt')
    audit = read(audit_path)
    raw_path = Path(review_path).with_suffix(Path(review_path).suffix + '.raw.json')
    review = read(review_path)
    expected_prompt = grading_prompt(pack, review['grading_trial'])
    if (not isinstance(audit, dict) or audit.get('schema_version') != 1 or audit.get('status') != 'validated'
            or audit.get('exit_code') != 0
            or audit.get('tools') != 'disabled' or audit.get('cwd') != 'empty_temporary_directory'
            or audit.get('raw_sha256') != sha(raw_path) or audit.get('review_sha256') != sha(review_path)
            or audit.get('package_manifest_sha256') != sha(Path(pack) / 'manifest.json')
            or audit.get('prompt_sha256') != hashlib.sha256(expected_prompt.encode()).hexdigest()):
        raise ValueError('isolated grading execution receipt differs from package/result')
    response, _ = terminal_response(raw_path.read_text())
    grades = json.loads(response)
    source = {case['case_id']: case['checks'] for case in grades['cases']}
    saved = {case['case_id']: case['checks'] for case in review['cases']}
    if source != saved:
        raise ValueError('saved grades differ from captured grader response')


def accept_review(run_directory, pack, review_path, session_id):
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError('acceptance requires coordinator session_id')
    root, pack, review_path = Path(run_directory).resolve(), Path(pack).resolve(), Path(review_path).resolve()
    review, manifest = check_pack_review(pack, review_path)
    verify_pack(pack, root)
    trial = review['grading_trial']
    original_path = root / f'trial-{trial}/review.json'
    # This is the only intentional replacement: an ungraded or previously accepted
    # table becomes the exact accepted package table. Other captures remain immutable.
    mirrored = copy.deepcopy(review)
    if original_path.exists():
        previous = read(original_path)
        if not isinstance(previous, dict):
            raise ValueError('unrecognized original review schema')
    raw = json.dumps(mirrored, ensure_ascii=False, indent=2) + '\n'
    receipt_path = root / f'trial-{trial}/review-acceptance.json'
    if receipt_path.exists():
        raise ValueError('trial already accepted; new evaluation needs an independent run/acceptance snapshot')
    original_path.write_text(raw, encoding='utf-8')
    receipt = dict(schema_version=1, kind='benchmark_review_acceptance', command='accept_benchmark_review',
                   session_id=session_id, review_path=str(review_path), review_sha256=sha(review_path),
                   pack_path=str(pack), manifest_sha256=sha(pack / 'manifest.json'),
                   mirrored_review_sha256=sha(original_path), trial=trial, source_hashes=manifest['source_hashes'])
    audit_path = review_path.with_suffix(review_path.suffix + '.execution.json')
    if audit_path.is_file():
        receipt['grading_execution_sha256'] = sha(audit_path)
    write(receipt_path, receipt)
    return receipt


def check_accepted_review(run_directory, trial):
    root = Path(run_directory).resolve()
    receipt = read(root / f'trial-{trial}/review-acceptance.json')
    if (not isinstance(receipt, dict) or receipt.get('schema_version') != 1
            or receipt.get('kind') != 'benchmark_review_acceptance' or receipt.get('trial') != trial):
        raise ValueError('unrecognized review acceptance receipt')
    pack, review_path = Path(receipt['pack_path']), Path(receipt['review_path'])
    review, manifest = check_pack_review(pack, review_path)
    verify_pack(pack, root)
    if (sha(review_path) != receipt['review_sha256'] or sha(pack / 'manifest.json') != receipt['manifest_sha256']
            or sha(root / f'trial-{trial}/review.json') != receipt['mirrored_review_sha256']
            or read(root / f'trial-{trial}/review.json') != review or manifest['source_hashes'] != receipt['source_hashes']):
        raise ValueError('accepted grading artifacts or mirrored review changed')
    if 'grading_execution_sha256' in receipt and sha(review_path.with_suffix(review_path.suffix + '.execution.json')) != receipt['grading_execution_sha256']:
        raise ValueError('grading execution changed after acceptance')
    return receipt
