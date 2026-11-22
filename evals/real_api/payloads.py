"""Pydantic model builder and deterministic payload generator for real-API eval cases."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, create_model

from evals.real_api.cases import Case, FieldType

_PY_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "datetime": datetime,
}

_BASE_EPOCH = int(datetime(2020, 1, 1, tzinfo=UTC).timestamp())


def _build(name: str, tree: dict[str, Any]) -> type[BaseModel]:
    defs: dict[str, Any] = {}
    for key, val in tree.items():
        if isinstance(val, dict):
            defs[key] = (_build(f"{name}_{key}", val), ...)
        else:
            defs[key] = (_PY_TYPES[val], ...)
    return create_model(name, **defs)


def _tree(fields: dict[str, FieldType]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for path, ftype in fields.items():
        parts = path.split(".")
        node = root
        for p in parts[:-1]:
            node = node.setdefault(p, {})
            if not isinstance(node, dict):
                raise ValueError(f"path conflict at {path!r}")
        if isinstance(node.get(parts[-1]), dict):
            raise ValueError(f"path conflict at {path!r}")
        node[parts[-1]] = ftype
    return root


def build_model(name: str, fields: dict[str, FieldType]) -> type[BaseModel]:
    """Build a model; dotted paths become nested models."""
    return _build(name, _tree(fields))


def _get(rec: dict, path: str) -> Any:
    for p in path.split("."):
        rec = rec[p]
    return rec


def _has(rec: dict, path: str) -> bool:
    for p in path.split("."):
        if not isinstance(rec, dict) or p not in rec:
            return False
        rec = rec[p]
    return True


def _set(rec: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        rec = rec.setdefault(p, {})
    rec[parts[-1]] = value


def _value(ftype: str, rng: random.Random, path: str, attempt: int) -> Any:
    if ftype == "string":
        return f"{path.replace('.', '_')}-{rng.getrandbits(32):08x}"
    if ftype == "integer":
        return rng.randint(1_000, 10_000_000)
    if ftype == "number":
        return round(rng.uniform(1, 10_000), 2) + attempt * 0.01
    if ftype == "boolean":
        return rng.random() < 0.5
    if ftype == "datetime":
        ts = _BASE_EPOCH + rng.randint(0, 5 * 365 * 86400)
        return datetime.fromtimestamp(ts, UTC).isoformat()
    raise ValueError(f"unknown field type {ftype!r}")


def _fresh(ftype: str, salt: str, path: str, used: set) -> Any:
    """A seeded value for `path`, distinct from `used` (booleans cannot be distinct)."""
    rng = random.Random(salt)
    attempt = 0
    val = _value(ftype, rng, path, attempt)
    while ftype != "boolean" and val in used:
        attempt += 1
        val = _value(ftype, rng, path, attempt)
    if ftype != "boolean":
        used.add(val)
    return val


def _leaves(rec: Any) -> list[Any]:
    if isinstance(rec, dict):
        return [v for x in rec.values() for v in _leaves(x)]
    return [rec]


def make_old_records(case: Case, n: int = 5, seed: int = 0) -> list[dict]:
    """Nested dicts valid for the old model, deterministic and distinct per field."""
    records = []
    for i in range(n):
        rec: dict = {}
        used: set = set()
        for path, ftype in case.old_fields.items():
            _set(rec, path, _fresh(ftype, f"{seed}:{i}:{path}", path, used))
        records.append(rec)
    return records


def _convert(value: Any, kind: str, old_type: str) -> Any:
    if kind == "none":
        return value
    if kind in ("int_str", "int_float") and old_type == "integer":
        return str(value) if kind == "int_str" else float(value)
    if kind == "epoch_iso" and old_type == "datetime":
        return int(datetime.fromisoformat(value).timestamp())
    raise ValueError(f"convert {kind!r} not valid for old type {old_type!r}")


def make_new_payload(case: Case, old_records: list[dict], seed: int = 0) -> list[dict]:
    """Reshape each old record per case.maps; unmapped shared fields carry over.

    Every other `new_fields` path (a field added by the change that no map uses) gets a fresh
    seeded value distinct from every real value in the record, so a fix that maps an old field
    to such a distractor loads a visibly wrong value.
    """
    targets = {m.target for m in case.maps}
    sources = [m.source for m in case.maps]
    if len(set(sources)) != len(sources):
        raise ValueError("duplicate map source")
    for src in sources:
        if src not in case.new_fields:
            raise ValueError(f"map source {src!r} not in new_fields")
    _tree(case.new_fields)  # raises on prefix conflicts (a path that is also a parent)
    out = []
    for i, old in enumerate(old_records):
        new: dict = {}
        for m in case.maps:
            if m.target not in case.old_fields:
                raise ValueError(f"map target {m.target!r} not in old_fields")
            val = _convert(_get(old, m.target), m.convert, case.old_fields[m.target])
            _set(new, m.source, val)
        for path in case.new_fields:
            if path not in targets and _has(old, path) and not _has(new, path):
                _set(new, path, _get(old, path))
        used = {v for v in _leaves(old) + _leaves(new) if not isinstance(v, bool)}
        for path, ftype in case.new_fields.items():
            if not _has(new, path):
                _set(new, path, _fresh(ftype, f"{seed}:{i}:new:{path}", path, used))
        out.append(new)
    return out
