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


def run_case(case: Case, proposer, tmp_root: Path) -> tuple[Outcome, Usage]:
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
    try:
        result = healer.validate(new, model)
    except Exception:
        return "crashed", Usage()
    return classify(case, result, expected), result.usage


def run_all(
    cases: list[Case], mode: Literal["rules", "agent"], tmp_root: Path
) -> list[tuple[Case, Outcome, Usage]]:
    proposer = None
    if mode == "agent":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        from aegis_healer.agent.graph import AgentProposer

        proposer = AgentProposer()
    rows = []
    for case in cases:
        outcome, usage = run_case(case, proposer, Path(tmp_root))
        rows.append((case, outcome, usage))
    return rows
