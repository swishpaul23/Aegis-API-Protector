"""Apply mapping patches. Every transform is a fixed, named function: no eval, no code."""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from aegis_healer.paths import get_path, has_path, pop_path, set_path
from aegis_healer.types import FieldMapping, Transform

_TRUE = {"true", "1", "yes", "y"}
_FALSE = {"false", "0", "no", "n"}


def _to_int(v: Any) -> int:
    if isinstance(v, bool):
        raise ValueError("bool is not an int")
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    raise ValueError(f"cannot convert {v!r} to int")


def _to_float(v: Any) -> float:
    if isinstance(v, bool):
        raise ValueError("bool is not a float")
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, str):
        return float(v.strip())
    raise ValueError(f"cannot convert {v!r} to float")


def _to_str(v: Any) -> str:
    if isinstance(v, dict | list) or v is None:
        raise ValueError(f"cannot convert {type(v).__name__} to str")
    return str(v)


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"cannot convert {v!r} to bool")


def _parse_datetime(v: Any) -> datetime:
    if not isinstance(v, str):
        raise ValueError("parse_datetime needs a string")
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _epoch_to_iso(v: Any) -> str:
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ValueError("epoch_to_iso needs a number")
    if not 0 <= v < 32_503_680_000:  # year 3000; rejects millisecond timestamps
        raise ValueError(f"{v!r} is not an epoch timestamp in seconds")
    return datetime.fromtimestamp(v, tz=UTC).isoformat().replace("+00:00", "Z")


TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "none": lambda v: v,
    "to_int": _to_int,
    "to_float": _to_float,
    "to_str": _to_str,
    "to_bool": _to_bool,
    "parse_datetime": _parse_datetime,
    "epoch_to_iso": _epoch_to_iso,
}


def apply_transform(value: Any, transform: Transform | str) -> Any:
    """Apply a parameterless transform. Raises ValueError on failure or unknown name."""
    fn = TRANSFORMS.get(transform)
    if fn is None:
        raise ValueError(f"unknown transform {transform!r}")
    return fn(value)


def map_value(record: dict[str, Any], mapping: FieldMapping) -> Any:
    """Compute the target value for one mapping from a record. Raises KeyError/ValueError."""
    if mapping.transform == "concat":
        parts = [get_path(record, s) for s in mapping.sources]
        if not all(isinstance(p, str) for p in parts):
            raise ValueError("concat needs string values")
        return mapping.separator.join(parts)
    value = get_path(record, mapping.sources[0])
    if mapping.transform == "value_map":
        key = str(value).lower() if isinstance(value, bool) else str(value)
        assert mapping.value_map is not None
        if key not in mapping.value_map:
            raise ValueError(f"value {value!r} not in value_map")
        return mapping.value_map[key]
    return apply_transform(value, mapping.transform)


def apply_mappings(record: dict[str, Any], mappings: list[FieldMapping]) -> dict[str, Any]:
    """Return a new record with mappings applied. Missing sources are skipped."""
    src = copy.deepcopy(record)
    out = copy.deepcopy(record)
    for m in mappings:
        if not all(has_path(src, s) for s in m.sources):
            continue
        value = map_value(src, m)
        for s in m.sources:
            if s != m.target and has_path(out, s):
                pop_path(out, s)
        set_path(out, m.target, value)
    return out
