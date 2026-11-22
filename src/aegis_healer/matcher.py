"""Rule-based matcher. Only fixes drift with exactly one sensible answer; never guesses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from aegis_healer.mapping import apply_mappings
from aegis_healer.paths import has_path, norm
from aegis_healer.types import SIMPLE_TRANSFORMS, DriftReport, FieldMapping, MatchResult


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
    """First simple transform that makes `target` valid on every sample that has `source`."""
    relevant = [s for s in samples if has_path(s, source)]
    if not relevant:
        return None
    for transform in SIMPLE_TRANSFORMS:
        m = FieldMapping(source=source, target=target, transform=transform)
        try:
            ok = all(target_ok(apply_mappings(s, [m]), model, target) for s in relevant)
        except (ValueError, KeyError):
            continue
        if ok:
            return m
    return None


def _wrapped_or_unwrapped(a: str, b: str) -> bool:
    """True if one path is the other with parents added on top (`email` <-> `contact.email`).

    Swapping a parent for a different one (`a.x` -> `b.x`) is not: same leaf name under a
    different object is often a different field, so that case is left to the agent.
    """
    pa = [norm(p) for p in a.split(".")]
    pb = [norm(p) for p in b.split(".")]
    short, long_ = sorted((pa, pb), key=len)
    return long_[len(long_) - len(short) :] == short


def _candidates(target: str, pool: list[str]) -> tuple[list[str], float, str]:
    rule_a = [s for s in pool if norm(s) == norm(target)]
    if rule_a:
        return rule_a, 1.0, "renamed (same name, different style)"
    rule_b = [s for s in pool if _wrapped_or_unwrapped(s, target)]
    return rule_b, 0.95, "moved (same name, new location)"


def match(
    report: DriftReport, samples: list[dict[str, Any]], model: type[BaseModel]
) -> MatchResult:
    resolved: list[FieldMapping] = []
    unresolved: list[str] = []

    # Same path, wrong type: try a safe conversion.
    for mm in report.type_mismatches:
        m = _working_transform(mm.path, mm.path, samples, model)
        if m is None:
            unresolved.append(mm.path)
        else:
            resolved.append(m.model_copy(update={"confidence": 1.0, "reason": "type change"}))

    # Find candidates for every missing target first, so a source wanted by two
    # targets is treated as ambiguous for both instead of going to whichever is first.
    found = {t: _candidates(t, report.unexpected) for t in report.missing}
    claims: dict[str, int] = {}
    for sources, _, _ in found.values():
        if len(sources) == 1:
            claims[sources[0]] = claims.get(sources[0], 0) + 1

    for target, (sources, confidence, why) in found.items():
        if len(sources) != 1 or claims[sources[0]] > 1:
            unresolved.append(target)
            continue
        m = _working_transform(sources[0], target, samples, model)
        if m is None:
            unresolved.append(target)
            continue
        resolved.append(m.model_copy(update={"confidence": confidence, "reason": why}))

    return MatchResult(resolved=resolved, unresolved=unresolved)
