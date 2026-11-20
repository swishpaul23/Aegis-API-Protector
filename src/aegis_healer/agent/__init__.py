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


class BudgetExceeded(AgentError):
    """The per-run LLM call budget ran out."""


INPUT_USD_PER_MTOK = 0.10  # Haiku 5.5 list prices
OUTPUT_USD_PER_MTOK = 0.50


class Usage(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens

    @property
    def est_cost_usd(self) -> float:
        return (
            self.input_tokens / 1e6 * INPUT_USD_PER_MTOK
            + self.output_tokens / 1e6 * OUTPUT_USD_PER_MTOK
        )


class Proposer(Protocol):
    """Returns a patch that passed `check`, or raises AgentError.

    `budget` caps LLM calls for this invocation (None = unlimited); proposers expose
    `last_usage` after each call, even when it raised.
    """

    last_usage: Usage

    def propose(
        self,
        request: ProposalRequest,
        check: Callable[[MappingPatch], PatchCheck],
        budget: int | None = None,
    ) -> MappingPatch: ...


__all__ = [
    "AgentError",
    "BudgetExceeded",
    "ProposalRequest",
    "Proposer",
    "Usage",
    "INPUT_USD_PER_MTOK",
    "OUTPUT_USD_PER_MTOK",
]
