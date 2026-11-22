"""Offline runner: feeds each case through the real Healer in rules or agent mode."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from aegis_healer import Healer
from aegis_healer.agent import Usage
from aegis_healer.agent.graph import DEFAULT_MODEL
from aegis_healer.store.files import FileStore
from evals.real_api.cases import Case
from evals.real_api.payloads import build_model, make_new_payload, make_old_records
from evals.real_api.scoring import Outcome, classify

# (case, outcome, usage, error): error is a short reason for crashed / agent_error rows.
Row = tuple[Case, Outcome, Usage, str | None]

MAX_AGENT_ERRORS = 3  # more than this many agent_error rows aborts the agent pass


class AgentOutage(RuntimeError):
    """Too many cases where the agent was called but never answered (API outage, bad key)."""


def _reason(exc: BaseException) -> str:
    text = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {text}"[:300]


def agent_model() -> str:
    return os.environ.get("AEGIS_MODEL") or DEFAULT_MODEL


def preflight_agent() -> None:
    """One minimal live call through the eval's model; raises RuntimeError on any failure.

    The proposer swallows API errors (they become a failed heal), so without this a bad key
    or an outage would score as safe deferrals and correct refusals.
    """
    try:
        from langchain_anthropic import ChatAnthropic

        ChatAnthropic(model=agent_model(), max_tokens=8, timeout=30).invoke("ping")
    except Exception as exc:
        raise RuntimeError(f"agent preflight failed ({agent_model()}): {_reason(exc)}") from exc


def make_agent_proposer():
    from aegis_healer.agent.graph import AgentProposer

    return AgentProposer()


def _agent_never_answered(usage: Usage) -> bool:
    return usage.calls > 0 and usage.input_tokens == 0 and usage.output_tokens == 0


def run_case(case: Case, proposer, tmp_root: Path) -> tuple[Outcome, Usage, str | None]:
    """Run one case. Any exception, while building the case or healing, is `crashed`.

    With a proposer, a run where the agent was called but no tokens came back is
    `agent_error` rather than a deferral or refusal Aegis did not actually decide.
    """
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
    outcome = classify(case, result, expected)
    if (
        proposer is not None
        and outcome in ("safe_deferral", "correct_refusal")
        and _agent_never_answered(result.usage)
    ):
        return "agent_error", result.usage, (result.message or "agent returned no tokens")[:300]
    return outcome, result.usage, None


def run_all(
    cases: list[Case],
    mode: Literal["rules", "agent"],
    tmp_root: Path,
    proposer=None,
) -> list[Row]:
    """Run every case. Agent mode checks the key, then a live preflight, before any case.

    `proposer` overrides the real AgentProposer in agent mode (tests).
    """
    if mode == "agent":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        preflight_agent()
        proposer = proposer if proposer is not None else make_agent_proposer()
    else:
        proposer = None
    rows: list[Row] = []
    agent_errors = 0
    for case in cases:
        outcome, usage, error = run_case(case, proposer, Path(tmp_root))
        rows.append((case, outcome, usage, error))
        if outcome == "agent_error":
            agent_errors += 1
            if agent_errors > MAX_AGENT_ERRORS:
                raise AgentOutage(
                    f"aborted: {agent_errors} cases ended in agent_error (agent called, no "
                    f"tokens returned); last: {error}"
                )
    return rows
