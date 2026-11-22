"""Value-level outcome scoring for the real-API eval (see the design spec, "Scoring")."""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel

from aegis_healer import HealResult
from evals.real_api.cases import Case

Outcome = Literal[
    "repaired",
    "wrong_applied",
    "correct_refusal",
    "safe_deferral",
    "crashed",
    "agent_error",
    "not_breaking",
]

OUTCOMES: tuple[Outcome, ...] = (
    "repaired",
    "wrong_applied",
    "correct_refusal",
    "safe_deferral",
    "crashed",
    "agent_error",  # agent called but never answered (outage/bad key); in N, as a failure
    "not_breaking",
)
# Outcomes that are not part of the denominator N: the change did not break the model.
EXCLUDED_FROM_N: frozenset[str] = frozenset({"not_breaking"})


def classify(case: Case, result: HealResult | None, expected_records: list[BaseModel]) -> Outcome:
    """Score one run. `result is None` means the run raised.

    `ok` means no drift was detected (lax Pydantic coercion absorbed the change). On a repair
    case with correct values that is `not_breaking`: Aegis did nothing, so it is excluded from
    N rather than credited as a repair. `ok` with wrong values, or on a refuse case, is
    `wrong_applied`.
    """
    if result is None:
        return "crashed"
    if result.status == "ok":
        if case.expected == "repair" and result.records == expected_records:
            return "not_breaking"
        return "wrong_applied"
    if result.status == "healed":
        if case.expected == "repair" and result.records == expected_records:
            return "repaired"
        return "wrong_applied"
    # pending / failed: nothing applied
    return "correct_refusal" if case.expected == "refuse" else "safe_deferral"


def _block(outcomes: list[Outcome]) -> dict[str, Any]:
    n = sum(1 for o in outcomes if o not in EXCLUDED_FROM_N)
    c = Counter(outcomes)
    counts = {o: c.get(o, 0) for o in OUTCOMES}
    return {
        "n": n,
        "counts": counts,
        "repair_rate": counts["repaired"] / n if n else 0.0,
        "wrong_applied_rate": counts["wrong_applied"] / n if n else 0.0,
    }


def summarize(rows: list[tuple[Case, Outcome]]) -> dict[str, Any]:
    out = _block([o for _, o in rows])
    out["by_api"] = {
        k: _block([o for c, o in rows if c.api == k]) for k in sorted({c.api for c, _ in rows})
    }
    out["by_kind"] = {
        k: _block([o for c, o in rows if c.kind == k]) for k in sorted({c.kind for c, _ in rows})
    }
    return out
