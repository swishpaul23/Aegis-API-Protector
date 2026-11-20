"""Rule-based matcher. Only fixes drift with exactly one sensible answer; never guesses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from aegis_healer.mapping import apply_mappings
from aegis_healer.paths import has_path
from aegis_healer.types import SIMPLE_TRANSFORMS, DriftReport, FieldMapping, MatchResult


def norm(s: str) -> str:
    return s.lower().replace("_", "").replace("-", "")


def _leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def target_ok(record: dict[str, Any], model: type[BaseModel], target: str) -> bool:
    """True if validating `record` raises no error at or under `target`."""
    try:
        model.model_validate(record)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            if loc == target or loc.startswith(f"{target}.") or target.startswith(f"{loc}."):
                return False
    return True


def _working_transform(
    source: str, target: str, samples: list[dict[str, Any]], model: type[BaseModel]
) -> FieldMapping | None:
    """First simple transform that makes `target` valid on every sample."""
    for transform in SIMPLE_TRANSFORMS:
        m = FieldMapping(source=source, target=target, transform=transform)
        try:
            ok = all(
                has_path(s, source) and target_ok(apply_mappings(s, [m]), model, target)
                for s in samples
            )
        except (ValueError, KeyError):
            continue
        if ok:
            return m
    return None


def match(
    report: DriftReport, samples: list[dict[str, Any]], model: type[BaseModel]
) -> MatchResult:
    resolved: list[FieldMapping] = []
    unresolved: list[str] = []
    used: set[str] = set()

    # Same path, wrong type: try a safe conversion.
    for mm in report.type_mismatches:
        m = _working_transform(mm.path, mm.path, samples, model)
        if m is None:
            unresolved.append(mm.path)
        else:
            resolved.append(m.model_copy(update={"confidence": 1.0, "reason": "type change"}))

    for target in report.missing:
        pool = [s for s in report.unexpected if s not in used]
        rule_a = [s for s in pool if norm(s) == norm(target)]
        rule_b = [s for s in pool if norm(_leaf(s)) == norm(_leaf(target))]
        if len(rule_a) == 1:
            source, confidence, why = rule_a[0], 1.0, "renamed (same name, different style)"
        elif not rule_a and len(rule_b) == 1:
            source, confidence, why = rule_b[0], 0.95, "moved (same name, new location)"
        else:
            unresolved.append(target)
            continue
        m = _working_transform(source, target, samples, model)
        if m is None:
            unresolved.append(target)
            continue
        used.add(source)
        resolved.append(m.model_copy(update={"confidence": confidence, "reason": why}))

    return MatchResult(resolved=resolved, unresolved=unresolved)
