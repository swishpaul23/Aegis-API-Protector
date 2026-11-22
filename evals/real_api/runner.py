"""Offline runner: feeds each case through the real Healer in rules or agent mode."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from aegis_healer import Healer
from aegis_healer.agent import Usage
from aegis_healer.store.files import FileStore
from evals.real_api.cases import Case
from evals.real_api.payloads import build_model, make_new_payload, make_old_records
from evals.real_api.scoring import Outcome, classify

# (case, outcome, usage, error): error is a short reason for crashed / agent_error rows.
Row = tuple[Case, Outcome, Usage, str | None]


def _reason(exc: BaseException) -> str:
    text = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {text}"[:300]


def run_case(case: Case, proposer, tmp_root: Path) -> tuple[Outcome, Usage, str | None]:
    """Run one case. Any exception, while building the case or healing, is `crashed`."""
    try:
        model = build_model("Model", case.old_fields)
        old = make_old_records(case)
        new = make_new_payload(case, old)
        expected = [model(**r) for r in old]
        healer = Healer(
            store=FileStore(Path(tmp_root) / case.id),
            persist=False,
            meaning=False,
            quarantine=False,
            proposer=proposer,
        )
        result = healer.validate(new, model)
    except Exception as exc:
        return "crashed", Usage(), _reason(exc)
    return classify(case, result, expected), result.usage, None


def run_all(
    cases: list[Case], mode: Literal["rules", "agent"], tmp_root: Path
) -> list[Row]:
    proposer = None
    if mode == "agent":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        from aegis_healer.agent.graph import AgentProposer

        proposer = AgentProposer()
    rows: list[Row] = []
    for case in cases:
        outcome, usage, error = run_case(case, proposer, Path(tmp_root))
        rows.append((case, outcome, usage, error))
    return rows
