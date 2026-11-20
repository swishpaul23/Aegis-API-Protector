"""Where mappings, their history, and pending fixes live."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from aegis_healer.types import DriftReport, FieldMapping, MappingPatch


class MappingVersion(BaseModel):
    version: int
    mappings: list[FieldMapping]
    reason: str
    created_at: datetime


class PendingPatch(BaseModel):
    id: int
    model_key: str
    patch: MappingPatch
    report: DriftReport
    samples: list[dict] = Field(default_factory=list)
    base_version: int
    status: Literal["pending", "approved", "rejected"] = "pending"
    created_at: datetime


class StalePatchError(Exception):
    """The mappings changed after this pending fix was proposed."""


class MappingStore(Protocol):
    def get_active(self, model_key: str) -> list[FieldMapping]: ...
    def active_version(self, model_key: str) -> int: ...
    def save_active(self, model_key: str, mappings: list[FieldMapping], reason: str) -> int: ...
    def history(self, model_key: str) -> list[MappingVersion]: ...
    def rollback(self, model_key: str, version: int) -> int: ...
    def add_pending(
        self,
        model_key: str,
        patch: MappingPatch,
        report: DriftReport,
        samples: list[dict] | None = None,
    ) -> int: ...
    def get_pending(self, pending_id: int) -> PendingPatch: ...
    def list_pending(self, model_key: str | None = None) -> list[PendingPatch]: ...
    def resolve_pending(self, pending_id: int, approve: bool) -> int | None: ...


def merge(active: list[FieldMapping], new: list[FieldMapping]) -> list[FieldMapping]:
    """New mappings replace active ones with the same target."""
    targets = {m.target for m in new}
    return [m for m in active if m.target not in targets] + list(new)


def default_store() -> MappingStore:
    from aegis_healer.store.files import FileStore

    return FileStore(os.environ.get("AEGIS_DIR", ".aegis"))


__all__ = [
    "MappingStore",
    "MappingVersion",
    "PendingPatch",
    "StalePatchError",
    "default_store",
    "merge",
]
