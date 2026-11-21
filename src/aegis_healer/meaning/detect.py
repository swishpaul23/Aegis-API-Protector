"""Meaning-drift rules: compare a batch against the learned baseline (read-only)."""

from __future__ import annotations

import math
import statistics
from typing import Any

from pydantic import BaseModel

from ..paths import get_path, model_paths
from ..types import MeaningFinding
from .profile import (
    BUCKETS_PER_DECADE,
    FieldProfile,
    ModelProfile,
    classify,
    median_bucket,
    tz_class,
)

MIN_BASELINE = 20
MIN_BATCH = 5
SCALE_TRIGGER = 1.75
SCALE_SNAP = 0.35
SCALE_STEPS = (2, 3)
TZ_BASELINE_SHARE = 0.95
TZ_BATCH_SHARE = 0.80
CATEGORY_BASELINE_SHARE = 0.95
CATEGORY_NEW_SHARE = 0.50
MAX_LISTED_CATEGORIES = 10

_TZ_NAMES = {"utc": "UTC", "offset": "a local offset", "naive": "no timezone"}


def _scale(path: str, base: FieldProfile, numbers: list[float]) -> MeaningFinding | None:
    nonzero = [v for v in numbers if v != 0]
    base_bucket = median_bucket(base.numeric)
    if len(nonzero) < MIN_BATCH or base_bucket is None:
        return None
    diff = statistics.median(math.log10(abs(v)) for v in nonzero) - base_bucket / BUCKETS_PER_DECADE
    if abs(diff) < SCALE_TRIGGER:
        return None
    k = round(diff)
    snapped = abs(k) in SCALE_STEPS and abs(diff - k) <= SCALE_SNAP
    way = "larger" if diff > 0 else "smaller"
    if snapped:
        detail = f"typical value is ~{10 ** abs(k)}x {way} than the baseline"
    else:
        detail = f"typical value is ~10^{abs(diff):.1f}x {way} than the baseline"
    return MeaningFinding(
        path=path,
        kind="scale",
        detail=detail,
        suggested_scale=10.0**-k if snapped else None,
        baseline_n=round(base.n),
        batch_n=len(nonzero),
    )


def _timezone(path: str, base: FieldProfile, stamps: list[Any]) -> MeaningFinding | None:
    total = sum(base.tz.values())
    if len(stamps) < MIN_BATCH or total <= 0:
        return None
    dominant = max(sorted(base.tz), key=lambda c: base.tz[c])
    if base.tz[dominant] / total < TZ_BASELINE_SHARE:
        return None
    counts: dict[str, int] = {}
    for s in stamps:
        c = tz_class(s)
        counts[c] = counts.get(c, 0) + 1
    other = max(sorted(c for c in counts if c != dominant), key=lambda c: counts[c], default=None)
    if other is None or counts[other] / len(stamps) < TZ_BATCH_SHARE:
        return None
    return MeaningFinding(
        path=path,
        kind="timezone",
        detail=f"timestamps changed from {_TZ_NAMES[dominant]} to {_TZ_NAMES[other]}",
        baseline_n=round(base.n),
        batch_n=len(stamps),
    )


def _category(
    path: str, base: FieldProfile, strings: list[tuple[str, str]]
) -> MeaningFinding | None:
    covered = sum(base.categories.values())
    if base.free_text or not base.categories or covered / base.n < CATEGORY_BASELINE_SHARE:
        return None
    usable = len(strings)
    if usable < MIN_BATCH:
        return None
    new_tokens = [v for kind, v in strings if kind == "category" and v not in base.categories]
    new = len(new_tokens) + sum(1 for kind, _ in strings if kind == "text")
    if new / usable < CATEGORY_NEW_SHARE:
        return None
    return MeaningFinding(
        path=path,
        kind="category",
        detail=f"{new} of {usable} values are new categories",
        baseline_n=round(base.n),
        batch_n=usable,
        new_categories=sorted(set(new_tokens))[:MAX_LISTED_CATEGORIES],
    )


def detect(
    profile: ModelProfile, records: list[dict], model: type[BaseModel]
) -> list[MeaningFinding]:
    findings: list[MeaningFinding] = []
    for path in model_paths(model):
        base = profile.fields.get(path)
        if base is None or base.n < MIN_BASELINE:
            continue
        numbers: list[float] = []
        stamps: list[Any] = []
        strings: list[tuple[str, str]] = []
        for record in records:
            try:
                kind, value = classify(get_path(record, path))
            except KeyError:
                continue
            if kind == "number":
                numbers.append(value)
            elif kind == "datetime":
                stamps.append(value)
            elif kind in ("category", "text"):
                strings.append((kind, value))
        for found in (
            _scale(path, base, numbers),
            _timezone(path, base, stamps),
            _category(path, base, strings),
        ):
            if found is not None:
                findings.append(found)
    return sorted(findings, key=lambda f: (f.path, f.kind))
