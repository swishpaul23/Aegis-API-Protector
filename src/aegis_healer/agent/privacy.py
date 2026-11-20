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
    if len(s) <= 20 and _CATEGORY.match(s) and (s == s.lower() or s == s.upper()):
        return s
    return f"<string, {len(s)} chars>"


def mask_value(value: Any) -> Any:
    """Replace sensitive leaf values with placeholders; keep structure and enum-like values."""
    if isinstance(value, dict):
        return {k: mask_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_value(v) for v in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if _EPOCH_MIN <= value <= _EPOCH_MAX:
            return value
        return f"<{type(value).__name__}>"
    if isinstance(value, str):
        return _mask_str(value)
    return value


def _schema_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _schema_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_schema_value(v) for v in value]
    return type(value).__name__


def prepare_samples(samples: list[dict], mode: Privacy) -> list[dict]:
    """Return the samples as the agent may see them under `mode`."""
    if mode == "full":
        return copy.deepcopy(samples)
    if mode == "schema":
        return [_schema_value(s) for s in samples]
    return [mask_value(s) for s in samples]
