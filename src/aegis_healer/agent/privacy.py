"""Privacy modes: control which payload values the agent is allowed to see."""

from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any, Literal

Privacy = Literal["full", "masked", "schema"]

_EPOCH_MIN, _EPOCH_MAX = 946684800, 4102444800  # 2000-01-01 .. 2100-01-01, seconds
_CATEGORY = re.compile(r"^[A-Za-z][A-Za-z_-]*$")


def _is_datetime(s: str) -> bool:
    if len(s) < 5 or s[4] != "-":
        return False
    try:
        datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return False
    return True


def _mask_str(s: str) -> str:
    if _is_datetime(s):
        return "<datetime>"
    if "@" in s and "." in s.split("@", 1)[1]:
        return "<email>"
    if len(s) <= 20 and _CATEGORY.fullmatch(s) and (s == s.lower() or s == s.upper()):
        return s
    return f"<string, {len(s)} chars>"


def _data_like_key(k: Any) -> bool:
    """Keys that look like data rather than field names (emails, ids, free text)."""
    if not isinstance(k, str):
        return True
    return (
        "@" in k
        or any(c.isspace() for c in k)
        or (len(k) > 8 and any(c.isdigit() for c in k))
        or _is_datetime(k)
    )


def _mask_keys(d: dict) -> list[tuple[Any, Any]]:
    return [(f"<key {i}>" if _data_like_key(k) else k, v) for i, (k, v) in enumerate(d.items())]


def mask_value(value: Any) -> Any:
    """Replace sensitive leaf values with placeholders; keep structure and enum-like values."""
    if isinstance(value, dict):
        return {k: mask_value(v) for k, v in _mask_keys(value)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [mask_value(v) for v in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if _EPOCH_MIN <= value <= _EPOCH_MAX:
            return value
        return f"<{type(value).__name__}>"
    if isinstance(value, str):
        return _mask_str(value)
    return f"<{type(value).__name__}>"


def _schema_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _schema_value(v) for k, v in _mask_keys(value)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_schema_value(v) for v in value]
    return type(value).__name__


def prepare_samples(samples: list[dict], mode: Privacy) -> list[dict]:
    """Return the samples as the agent may see them under `mode`."""
    if mode == "full":
        return copy.deepcopy(samples)
    if mode == "schema":
        return [_schema_value(s) for s in samples]
    return [mask_value(s) for s in samples]
