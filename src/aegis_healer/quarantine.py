"""Quarantine: records blocked on a human decision wait here, then are released by replay."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aegis_healer.paths import model_key as key_of

if TYPE_CHECKING:
    from aegis_healer.healer import Healer

_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
_IGNORE_LINE = "quarantine/"

BatchKind = Literal["schema", "meaning"]


class QuarantineBatch(BaseModel):
    id: str
    model_key: str
    reason: str
    pending_id: int | None = None
    # schema: failed validation (Phase 2 rules); meaning: valid but blocked on a meaning fix.
    # Files written before Phase 3 have no kind and load as schema batches.
    kind: BatchKind = "schema"
    created_at: datetime
    records: list[dict[str, Any]]


class ReplayResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    released: list[Any] = Field(default_factory=list)
    still_blocked: int = 0
    # meaning batches tied to a rejected fix: skipped, not counted as blocked, safe to discard
    dead_batches: list[str] = Field(default_factory=list)
    batches_cleared: list[str] = Field(default_factory=list)
    batches_reduced: list[str] = Field(default_factory=list)
    remove_failed: list[str] = Field(default_factory=list)
    # batch id -> records that still fail, for rewriting reduced batches (holds payload)
    remaining: dict[str, list[dict[str, Any]]] = Field(
        default_factory=dict, exclude=True, repr=False
    )


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out = []
    for record in records:
        fingerprint = json.dumps(record, sort_keys=True)
        if fingerprint not in seen:
            seen.add(fingerprint)
            out.append(record)
    return out


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
        atomic_write(path, f"{existing}{sep}{_IGNORE_LINE}\n")

    def add(
        self,
        model_key: str,
        records: list[dict[str, Any]],
        reason: str,
        pending_id: int | None,
        kind: BatchKind = "schema",
    ) -> str:
        now = datetime.now(UTC)
        batch_id = f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        batch = QuarantineBatch(
            id=batch_id,
            model_key=model_key,
            reason=reason,
            pending_id=pending_id,
            kind=kind,
            created_at=now,
            records=records,
        )
        self._write(batch)
        return batch_id

    def add_or_merge(
        self,
        model_key: str,
        records: list[dict[str, Any]],
        reason: str,
        pending_id: int | None,
        kind: BatchKind = "schema",
        absorb: list[int] | None = None,
    ) -> str | None:
        """Merge into this pending fix's batch (skipping duplicates), else create a batch.

        The merge key is (model_key, pending_id, reason, kind). `absorb` lists older
        pending ids whose meaning batches move into this one (a superseded fix): their
        records are merged in one atomic write, then the old batches are removed.
        Returns None only when there was nothing to store.
        """
        batches = self.list(model_key)
        moved = [
            b
            for b in batches
            if absorb and kind == "meaning" and b.kind == "meaning" and b.pending_id in absorb
        ]
        if moved:
            records = _dedupe([r for b in moved for r in b.records] + list(records))
        existing = [
            b
            for b in batches
            if b.pending_id == pending_id and b.reason == reason and b.kind == kind
        ]
        if not existing and not records:
            return None
        if not existing:
            batch_id = self.add(model_key, records, reason, pending_id, kind)
        else:
            batch = existing[0]
            merged = _dedupe(batch.records + records)
            if len(merged) != len(batch.records):
                batch.records = merged
                self._write(batch)
            batch_id = batch.id
        # Only after the merged batch is safely written: a crash leaves duplicates, not loss.
        remove_batches(self, [b.id for b in moved if b.id != batch_id])
        return batch_id

    def _write(self, batch: QuarantineBatch) -> None:
        self._ensure_gitignore()
        atomic_write(
            self.dir / f"{_check_id(batch.id)}.json",
            json.dumps(batch.model_dump(mode="json"), indent=2, default=str) + "\n",
        )

    def rewrite(self, batch_id: str, records: list[dict[str, Any]]) -> None:
        """Replace a batch's records in place (same id), atomically."""
        batch = self.load(batch_id)
        batch.records = records
        self._write(batch)

    def load(self, batch_id: str) -> QuarantineBatch:
        path = self.dir / f"{_check_id(batch_id)}.json"
        try:
            return QuarantineBatch.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # never echo the exception: it can hold payload text
            raise ValueError(f"quarantine batch {batch_id} is unreadable") from None

    def remove(self, batch_id: str) -> None:
        (self.dir / f"{_check_id(batch_id)}.json").unlink(missing_ok=True)

    def list(self, model_key: str | None = None) -> list[QuarantineBatch]:
        if not self.dir.is_dir():
            return []
        out = []
        for path in sorted(self.dir.glob("*.json")):
            if not _ID.fullmatch(path.stem):
                continue
            try:
                batch = self.load(path.stem)
            except ValueError:
                logging.getLogger("aegis_healer").warning(
                    "skipping unreadable quarantine file %s", path.name
                )
                continue
            if model_key is None or batch.model_key == model_key:
                out.append(batch)
        return out


def remove_batches(quarantine: Quarantine, batch_ids: list[str]) -> list[str]:
    """Remove batches one by one; returns the ids that could not be removed."""
    failed = []
    for batch_id in batch_ids:
        try:
            quarantine.remove(batch_id)
        except OSError:
            logging.getLogger("aegis_healer").warning(
                "could not remove quarantine batch %s", batch_id
            )
            failed.append(batch_id)
    return failed


def apply_replay(quarantine: Quarantine, result: ReplayResult) -> list[str]:
    """Remove cleared batches and shrink reduced ones to their still-failing records.

    Call only once the released records are safe (sunk or written). A batch that can't be
    changed is kept as it was (its released records will be released again next time);
    the returned ids are also recorded in `result.remove_failed`.
    """
    failed = remove_batches(quarantine, result.batches_cleared)
    for batch_id in result.batches_reduced:
        try:
            quarantine.rewrite(batch_id, result.remaining[batch_id])
        except (OSError, ValueError, KeyError):
            # Never echo the exception: it can hold payload text.
            logging.getLogger("aegis_healer").warning(
                "could not rewrite quarantine batch %s", batch_id
            )
            failed.append(batch_id)
    result.remove_failed = failed
    return failed


def _fix_status(healer: Healer, pending_id: int) -> str | None:
    """Status of the fix a batch waits on; None when it is missing or unreadable."""
    try:
        return healer.store.get_pending(pending_id).status
    except (KeyError, ValueError, OSError):
        return None


def discard_batches(quarantine: Quarantine, batch_ids: list[str]) -> list[str]:
    """Remove batches (e.g. dead ones); returns the ids that could not be removed."""
    return remove_batches(quarantine, batch_ids)


def replay(
    quarantine: Quarantine,
    healer: Healer,
    model: type[BaseModel],
    batch_id: str | None = None,
    remove: bool = True,
) -> ReplayResult:
    """Re-check quarantined records against the current active mappings only.

    No matcher, no agent, no new pending fix, no run log. Schema batches (records that
    failed validation) release whatever validates now, whatever happened to their fix.
    Meaning batches (records that validated but whose values changed meaning) wait on their
    fix: skipped and counted as still blocked until it is approved or kept (also when it is
    pending, superseded, missing or unreadable); a meaning batch of a rejected fix is dead:
    skipped and listed in `dead_batches`, not counted as blocked.
    Every record in the other batches that validates is released. A batch whose records all
    validate is cleared; a batch with some records still failing is reduced to those records
    (same id). With `remove=True` that happens here; with `remove=False` the caller applies
    it with `apply_replay` once the released records are safe.
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
        if batch.kind == "meaning":
            status = None if batch.pending_id is None else _fix_status(healer, batch.pending_id)
            if status == "rejected":
                result.dead_batches.append(batch.id)
                continue
            if status not in ("approved", "kept"):
                result.still_blocked += len(batch.records)  # tied to an unresolved fix
                continue
        valid, failing = _Healer._split(batch.records, mappings, model)
        result.released += valid
        result.still_blocked += len(failing)
        if not failing:
            result.batches_cleared.append(batch.id)
        elif valid:
            result.batches_reduced.append(batch.id)
            result.remaining[batch.id] = failing
    if remove:  # change files only after every result is collected
        apply_replay(quarantine, result)
    return result
