"""Git-friendly JSON file store.

.aegis/
  mappings/<model>.json            current version
  history/<model>/0001.json        every version, never edited
  pending/0001.json                fixes waiting for review
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aegis_healer.store import (
    MappingVersion,
    PendingPatch,
    StalePatchError,
    merge,
)
from aegis_healer.types import DriftReport, FieldMapping, MappingPatch

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_name(model_key: str) -> str:
    """Model keys become file names: strip anything that could escape the folder."""
    name = _SAFE.sub("_", model_key).strip(".")
    if not name:
        raise ValueError(f"invalid model key {model_key!r}")
    return name


def _now() -> datetime:
    return datetime.now(UTC)


class FileStore:
    def __init__(self, root: str | Path = ".aegis") -> None:
        self.root = Path(root)

    # ── io ──────────────────────────────────────────────────
    def _write(self, path: Path, data: Any) -> None:
        """Atomic write: temp file in the same folder, then rename over the target."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True, default=str)
                f.write("\n")
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _read(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def _current(self, model_key: str) -> Path:
        return self.root / "mappings" / f"{_safe_name(model_key)}.json"

    def _history_dir(self, model_key: str) -> Path:
        return self.root / "history" / _safe_name(model_key)

    def _pending_path(self, pending_id: int) -> Path:
        return self.root / "pending" / f"{int(pending_id):04d}.json"

    # ── mappings ────────────────────────────────────────────
    def get_active(self, model_key: str) -> list[FieldMapping]:
        path = self._current(model_key)
        if not path.exists():
            return []
        return [FieldMapping.model_validate(m) for m in self._read(path)["mappings"]]

    def active_version(self, model_key: str) -> int:
        path = self._current(model_key)
        return int(self._read(path)["version"]) if path.exists() else 0

    def save_active(self, model_key: str, mappings: list[FieldMapping], reason: str) -> int:
        version = self.active_version(model_key) + 1
        record = MappingVersion(
            version=version, mappings=mappings, reason=reason, created_at=_now()
        )
        data = record.model_dump(mode="json")
        self._write(self._history_dir(model_key) / f"{version:04d}.json", data)
        self._write(
            self._current(model_key),
            {"model": model_key, "version": version, "mappings": data["mappings"]},
        )
        return version

    def history(self, model_key: str) -> list[MappingVersion]:
        folder = self._history_dir(model_key)
        if not folder.exists():
            return []
        return [
            MappingVersion.model_validate(self._read(p)) for p in sorted(folder.glob("[0-9]*.json"))
        ]

    def rollback(self, model_key: str, version: int) -> int:
        path = self._history_dir(model_key) / f"{int(version):04d}.json"
        if not path.exists():
            raise KeyError(f"{model_key} has no version {version}")
        old = MappingVersion.model_validate(self._read(path))
        return self.save_active(model_key, old.mappings, f"rollback to v{version}")

    # ── pending ─────────────────────────────────────────────
    def add_pending(
        self,
        model_key: str,
        patch: MappingPatch,
        report: DriftReport,
        samples: list[dict] | None = None,
    ) -> int:
        folder = self.root / "pending"
        existing = [int(p.stem) for p in folder.glob("[0-9]*.json")] if folder.exists() else []
        pending_id = max(existing, default=0) + 1
        item = PendingPatch(
            id=pending_id,
            model_key=model_key,
            patch=patch,
            report=report,
            samples=(samples or [])[:3],
            base_version=self.active_version(model_key),
            created_at=_now(),
        )
        self._write(self._pending_path(pending_id), item.model_dump(mode="json"))
        return pending_id

    def get_pending(self, pending_id: int) -> PendingPatch:
        path = self._pending_path(pending_id)
        if not path.exists():
            raise KeyError(f"no pending fix #{pending_id}")
        return PendingPatch.model_validate(self._read(path))

    def list_pending(self, model_key: str | None = None) -> list[PendingPatch]:
        folder = self.root / "pending"
        if not folder.exists():
            return []
        items = [
            PendingPatch.model_validate(self._read(p)) for p in sorted(folder.glob("[0-9]*.json"))
        ]
        return [
            i
            for i in items
            if i.status == "pending" and (model_key is None or i.model_key == model_key)
        ]

    def resolve_pending(self, pending_id: int, approve: bool) -> int | None:
        item = self.get_pending(pending_id)
        if item.status != "pending":
            raise ValueError(f"fix #{pending_id} is already {item.status}")
        version: int | None = None
        if approve:
            if item.base_version != self.active_version(item.model_key):
                raise StalePatchError("stale; re-run check")
            merged = merge(self.get_active(item.model_key), item.patch.mappings)
            version = self.save_active(item.model_key, merged, f"approved fix #{pending_id}")
        item.status = "approved" if approve else "rejected"
        self._write(self._pending_path(pending_id), item.model_dump(mode="json"))
        return version
