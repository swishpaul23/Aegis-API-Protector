"""Append-only audit trail of every validate() run: counts and field paths, never payload values."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from aegis_healer.agent import Usage

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
