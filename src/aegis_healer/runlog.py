"""Append-only audit trail of every validate() run: counts and field paths, never payload values."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from aegis_healer.agent import Usage
from aegis_healer.agent.privacy import Privacy

RUNS_FILE = "runs.jsonl"
MAX_MESSAGE = 300


class RunRecord(BaseModel):
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    model_key: str
    status: Literal["ok", "healed", "pending", "failed"]
    records: int
    healed_or_valid: int
    failed_records: int
    fields_fixed: list[str] = Field(default_factory=list)
    pending_id: int | None = None
    usage: Usage = Field(default_factory=Usage)
    message: str = ""


def append_run(root: Path, record: RunRecord) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / RUNS_FILE, "a", encoding="utf-8", newline="\n") as f:
        f.write(record.model_dump_json() + "\n")


def read_runs(root: Path, limit: int = 20) -> list[RunRecord]:
    path = Path(root) / RUNS_FILE
    if not path.exists() or limit <= 0:
        return []
    runs: list[RunRecord] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                runs.append(RunRecord.model_validate_json(line))
            except (ValueError, ValidationError):
                continue
    return runs[-limit:][::-1]


_FAILED_CODES = (
    ("Unresolved drift in", "agent_disabled"),
    ("Agent proposed changes outside", "out_of_scope"),
    ("Cannot heal without changing your model", "unmappable"),
    ("Proposed fix failed validation", "failed_validation"),
)


def run_message(status: str, message: str, privacy: Privacy) -> str:
    """What the committed run log may say about a run.

    Result messages can embed payload values (validator text, LLM-written reasons), so
    unless privacy is "full" only a fixed category code is stored.
    """
    if privacy == "full":
        return message[:MAX_MESSAGE]
    if status == "pending":
        if message.startswith("Meaning drift needs review"):
            return "meaning_drift"
        return "pending_review"
    if status != "failed":
        return ""
    if message.startswith("Agent could not produce a valid fix"):
        return "budget_exhausted" if "budget" in message.lower() else "agent_error"
    for prefix, code in _FAILED_CODES:
        if message.startswith(prefix):
            return code
    return "failed"
