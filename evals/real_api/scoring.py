"""Value-level outcome scoring for the real-API eval (see the design spec, "Scoring")."""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel

from aegis_healer import HealResult
from evals.real_api.cases import Case

Outcome = Literal["repaired", "wrong_applied", "correct_refusal", "safe_deferral", "crashed"]

OUTCOMES: tuple[Outcome, ...] = (
    "repaired",
    "wrong_applied",
    "correct_refusal",
    "safe_deferral",
    "crashed",
)


def classify(case: Case, result: HealResult | None, expected_records: list[BaseModel]) -> Outcome:
    """Score one run. `result is None` means the run raised.

    `ok` is treated like `healed` (records loaded without review) and scored by value.
    """
    if result is None:
        return "crashed"
    if result.status in ("ok", "healed"):
        if case.expected == "repair" and result.records == expected_records:
            return "repaired"
        return "wrong_applied"
    # pending / failed: nothing applied
    return "correct_refusal" if case.expected == "refuse" else "safe_deferral"


def _block(outcomes: list[Outcome]) -> dict[str, Any]:
    n = len(outcomes)
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
