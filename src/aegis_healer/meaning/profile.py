"""Per-field baseline profiles: aggregate counts only, never raw values."""

from __future__ import annotations

import logging
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..paths import get_path, model_paths
from ..quarantine import atomic_write
from ..store.files import _safe_name

log = logging.getLogger("aegis_healer")

BUCKETS_PER_DECADE = 4
CATEGORY_MAX_DISTINCT = 30
DECAY_AT = 5000

# Same rule as agent/privacy.py (_mask_str); that module has no public helper for it.
_CATEGORY = re.compile(r"^[A-Za-z][A-Za-z_-]*$")


class FieldProfile(BaseModel):
    n: float = 0
    numeric: dict[int, float] = {}
    zeros: float = 0
    decimals: dict[int, float] = {}
    tz: dict[str, float] = {}
    categories: dict[str, float] = {}
    free_text: bool = False


class ModelProfile(BaseModel):
    model_key: str
    fields: dict[str, FieldProfile] = {}
    updated_at: datetime | None = None


def _parse_datetime(s: str) -> datetime | None:
    if len(s) < 5 or s[4] != "-" or ("T" not in s and " " not in s):
        return None
    try:
        return datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
    except ValueError:
        return None


def classify(value: Any) -> tuple[str, Any]:
    if value is None or isinstance(value, (bool, dict, list, tuple, set)):
        return "skip", None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return "skip", None
        return "number", value
    if isinstance(value, datetime):
        return "datetime", value
    if isinstance(value, str):
        parsed = _parse_datetime(value)
        if parsed is not None:
            return "datetime", parsed
        if len(value) >= 5 and value[4] == "-":
            try:  # date-only (or other ISO form without a time part): not a usable datetime
                datetime.fromisoformat(value)
            except ValueError:
                pass
            else:
                return "skip", None
        if (
            len(value) <= 20
            and _CATEGORY.fullmatch(value)
            and (value == value.lower() or value == value.upper())
        ):
            return "category", value
        return "text", value
    return "skip", None


def tz_class(value: Any) -> str:
    dt = value if isinstance(value, datetime) else _parse_datetime(str(value))
    if dt is None or dt.utcoffset() is None:
        return "naive"
    return "utc" if not dt.utcoffset() else "offset"


def _decimals(v: int | float) -> int:
    if isinstance(v, int):
        return 0
    r = repr(v)
    if "e" in r or "E" in r:
        return 4
    return min(len(r.split(".", 1)[1]) if "." in r else 0, 4)


def _bump(d: dict, key: Any) -> None:
    d[key] = d.get(key, 0) + 1


def _observe_value(f: FieldProfile, kind: str, value: Any) -> None:
    f.n += 1
    if kind == "number":
        _bump(f.decimals, _decimals(value))
        if value == 0:
            f.zeros += 1
        else:
            _bump(f.numeric, round(math.log10(abs(value)) * BUCKETS_PER_DECADE))
    elif kind == "datetime":
        _bump(f.tz, tz_class(value))
    elif kind == "category":
        if f.free_text:
            return
        _bump(f.categories, value)
        if len(f.categories) > CATEGORY_MAX_DISTINCT:
            f.free_text = True
            f.categories = {}
    elif kind == "text":
        f.free_text = True
        f.categories = {}


def _decay(f: FieldProfile) -> None:
    f.n *= 0.5
    f.zeros *= 0.5
    for d in (f.numeric, f.decimals, f.tz, f.categories):
        for k in d:
            d[k] *= 0.5


def observe(profile: ModelProfile, records: list[dict], model: type[BaseModel]) -> None:
    paths = list(model_paths(model))
    for record in records:
        for path in paths:
            try:
                value = get_path(record, path)
            except KeyError:
                continue
            kind, value = classify(value)
            if kind == "skip":
                continue
            _observe_value(profile.fields.setdefault(path, FieldProfile()), kind, value)
    for f in profile.fields.values():
        if f.n > DECAY_AT:
            _decay(f)


def median_bucket(counts: dict[int, float]) -> float | None:
    total = sum(counts.values())
    if total <= 0:
        return None
    running = 0.0
    for key in sorted(counts):
        running += counts[key]
        if running >= total / 2:
            return key
    return None  # unreachable


def _file(root: Path, model_key: str) -> Path:
    return Path(root) / "profiles" / f"{_safe_name(model_key)}.json"


def load_profile(root: Path, model_key: str) -> ModelProfile:
    path = _file(root, model_key)
    if not path.exists():
        return ModelProfile(model_key=model_key)
    try:
        loaded = ModelProfile.model_validate_json(path.read_text(encoding="utf-8"))
        if loaded.model_key != model_key:
            raise ValueError("model key mismatch")
        return loaded
    except (ValidationError, ValueError, OSError):
        log.warning("ignoring unreadable profile file %s; starting an empty baseline", path.name)
        return ModelProfile(model_key=model_key)


def save_profile(root: Path, profile: ModelProfile) -> None:
    profile.updated_at = datetime.now(UTC)
    atomic_write(_file(root, profile.model_key), profile.model_dump_json(indent=2) + "\n")


def reset_fields(profile: ModelProfile, paths: list[str]) -> None:
    for p in paths:
        profile.fields.pop(p, None)
