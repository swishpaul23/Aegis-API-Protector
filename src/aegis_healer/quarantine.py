"""Quarantine: records blocked on a human decision wait here, then are released by replay."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from aegis_healer.paths import model_key as key_of

if TYPE_CHECKING:
    from aegis_healer.healer import Healer

_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
_IGNORE_LINE = "quarantine/"


class QuarantineBatch(BaseModel):
    id: str
    model_key: str
    reason: str
    pending_id: int | None = None
    created_at: datetime
    records: list[dict[str, Any]]


class ReplayResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    released: list[Any] = Field(default_factory=list)
    still_blocked: int = 0
    batches_cleared: list[str] = Field(default_factory=list)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _check_id(batch_id: str) -> str:
    if not isinstance(batch_id, str) or not _ID.fullmatch(batch_id):
        raise ValueError("invalid quarantine batch id")
    return batch_id


class Quarantine:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.dir = self.root / "quarantine"

    def _ensure_gitignore(self) -> None:
        path = self.root / ".gitignore"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if _IGNORE_LINE in existing.splitlines():
            return
        sep = "" if not existing or existing.endswith("\n") else "\n"
        _atomic_write(path, f"{existing}{sep}{_IGNORE_LINE}\n")

    def add(
        self, model_key: str, records: list[dict[str, Any]], reason: str, pending_id: int | None
    ) -> str:
        now = datetime.now(UTC)
        batch_id = f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        batch = QuarantineBatch(
            id=batch_id,
            model_key=model_key,
            reason=reason,
            pending_id=pending_id,
            created_at=now,
            records=records,
        )
        self._ensure_gitignore()
        _atomic_write(
            self.dir / f"{batch_id}.json",
            json.dumps(batch.model_dump(mode="json"), indent=2, default=str) + "\n",
        )
        return batch_id

    def load(self, batch_id: str) -> QuarantineBatch:
        path = self.dir / f"{_check_id(batch_id)}.json"
        return QuarantineBatch.model_validate_json(path.read_text(encoding="utf-8"))

    def remove(self, batch_id: str) -> None:
        (self.dir / f"{_check_id(batch_id)}.json").unlink(missing_ok=True)

    def list(self, model_key: str | None = None) -> list[QuarantineBatch]:
        if not self.dir.is_dir():
            return []
        out = []
        for path in sorted(self.dir.glob("*.json")):
            if not _ID.fullmatch(path.stem):
                continue
            batch = self.load(path.stem)
            if model_key is None or batch.model_key == model_key:
                out.append(batch)
        return out


def replay(
    quarantine: Quarantine,
    healer: Healer,
    model: type[BaseModel],
    batch_id: str | None = None,
) -> ReplayResult:
    """Re-check quarantined records against the current active mappings only.

    No matcher, no agent, no new pending fix, no run log. A batch is released only if
    every record in it validates; otherwise it is left untouched.
    """
    from aegis_healer.healer import Healer as _Healer

    key = key_of(model)
    if batch_id is not None:
        batches = [b for b in [quarantine.load(batch_id)] if b.model_key == key]
    else:
        batches = quarantine.list(key)
    mappings = healer.store.get_active(key)
    result = ReplayResult()
    for batch in batches:
        valid, failing = _Healer._split(batch.records, mappings, model)
        if failing:
            result.still_blocked += len(failing)
            continue
        quarantine.remove(batch.id)
        result.released += valid
        result.batches_cleared.append(batch.id)
    return result
