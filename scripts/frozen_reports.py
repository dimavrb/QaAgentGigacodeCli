"""Small shared primitives for reproducible, source-bound QA reports."""
import hashlib
import json
import math
from pathlib import Path


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    def constant(value):
        raise ValueError('non-finite JSON number: ' + value)
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=pairs, parse_constant=constant)


def obj(value, name):
    if not isinstance(value, dict):
        raise ValueError(name + ' must be an object')
    return value


def schema(value, name):
    obj(value, name)
    if type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError(name + ': unknown schema')
    return value


def string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + ' must be a nonempty string')
    return value


def boolean(value, name):
    if type(value) is not bool:
        raise ValueError(name + ' must be boolean')
    return value


def number(value, name, positive=False):
    try:
        finite = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < 0 or positive and value == 0:
        raise ValueError(name + ' must be a finite nonnegative number')
    return value


def same_json(left, right):
    """Prevent bool/int and int/float coercions in source-derived saved facts."""
    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return canonical(left) == canonical(right)


def strings(value, name, nonempty=False):
    if not isinstance(value, list):
        raise ValueError(name + ' must be a list')
    for item in value:
        string(item, name)
    if len(value) != len(set(value)) or nonempty and not value:
        raise ValueError(name + ' must contain unique IDs')
    return value


def rows(value, name, key):
    if not isinstance(value, list):
        raise ValueError(name + ' must be a list')
    result = {}
    for row in value:
        obj(row, name)
        identifier = string(row.get(key), name + '.' + key)
        if identifier in result:
            raise ValueError('duplicate ' + key)
        result[identifier] = row
    return result


class Sources:
    def __init__(self):
        self.hashes = {}

    def load(self, path, name):
        path = Path(path).resolve()
        fingerprint = sha(path)
        if str(path) in self.hashes and self.hashes[str(path)] != fingerprint:
            raise ValueError('source changed: ' + str(path))
        self.hashes[str(path)] = fingerprint
        return schema(read(path), name)

    def absorb(self, hashes):
        for path, fingerprint in hashes.items():
            if path in self.hashes and self.hashes[path] != fingerprint:
                raise ValueError('source changed: ' + path)
            self.hashes[path] = fingerprint

    def reference(self, base, ref, name):
        obj(ref, name)
        path = (Path(base) / string(ref.get('path'), name + '.path')).resolve()
        expected = string(ref.get('sha256'), name + '.sha256')
        if sha(path) != expected:
            raise ValueError('source hash mismatch: ' + name)
        if str(path) in self.hashes and self.hashes[str(path)] != expected:
            raise ValueError('source changed: ' + str(path))
        self.hashes[str(path)] = expected
        return path

    def finish(self, result):
        for path, expected in self.hashes.items():
            if sha(path) != expected:
                raise ValueError('source changed during report: ' + path)
        return dict(result, source_hashes=dict(sorted(self.hashes.items())))


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
