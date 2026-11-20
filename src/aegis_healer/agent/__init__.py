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


class AgentError(Exception):
    """The agent could not produce a valid, in-scope fix."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:5]) or "agent failed")
        self.errors = errors


class Proposer(Protocol):
    """Returns a patch that passed `check`, or raises AgentError."""

    def propose(
        self, request: ProposalRequest, check: Callable[[MappingPatch], PatchCheck]
    ) -> MappingPatch: ...


__all__ = ["AgentError", "ProposalRequest", "Proposer"]
