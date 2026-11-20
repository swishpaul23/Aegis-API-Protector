"""The repair agent interface. Any object with `propose` can stand in (tests use fakes)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, Field

from aegis_healer.types import MappingPatch, PatchCheck


class ProposalRequest(BaseModel):
    model_name: str
    model_schema: dict[str, str]  # target path -> type name
    unresolved: list[str]  # target paths still missing
    candidates: dict[str, str]  # unexpected source path -> observed type
    already_mapped: list[str] = Field(default_factory=list)
    samples: list[dict[str, Any]]


class Proposer(Protocol):
    def propose(
        self, request: ProposalRequest, check: Callable[[MappingPatch], PatchCheck]
    ) -> MappingPatch: ...


__all__ = ["ProposalRequest", "Proposer"]
