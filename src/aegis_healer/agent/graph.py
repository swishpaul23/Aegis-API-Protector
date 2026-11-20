"""LangGraph repair loop: propose -> check -> (retry with errors | done)."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Protocol, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from aegis_healer.agent import ProposalRequest
from aegis_healer.agent.prompts import SYSTEM_PROMPT, render_request
from aegis_healer.types import MappingPatch, PatchCheck

DEFAULT_MODEL = "claude-haiku-5-5"


class StructuredLLM(Protocol):
    def invoke(self, messages: list[Any]) -> Any: ...


class _State(TypedDict):
    attempts: int
    patch: MappingPatch | None
    errors: list[str]
    ok: bool


def scope_errors(req: ProposalRequest, patch: MappingPatch) -> list[str]:
    """Reject anything outside what the agent was asked to touch."""
    errors = []
    for m in patch.mappings:
        if m.target not in req.unresolved:
            errors.append(f"target '{m.target}' was not an unresolved field")
        for s in m.sources:
            if s not in req.candidates:
                errors.append(f"source '{s}' was not a candidate source")
    return errors


class AgentProposer:
    def __init__(
        self,
        structured_llm: StructuredLLM | None = None,
        model: str | None = None,
        max_attempts: int = 3,
    ) -> None:
        if structured_llm is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY not set")
            from langchain_anthropic import ChatAnthropic

            llm = ChatAnthropic(
                model=model or os.environ.get("AEGIS_MODEL", DEFAULT_MODEL),
                temperature=0,
                max_tokens=2048,
                timeout=60,
            )
            structured_llm = llm.with_structured_output(MappingPatch)
        self.llm = structured_llm
        self.max_attempts = max_attempts

    def propose(
        self, request: ProposalRequest, check: Callable[[MappingPatch], PatchCheck]
    ) -> MappingPatch:
        def propose_node(state: _State) -> dict[str, Any]:
            messages = [
                SystemMessage(SYSTEM_PROMPT),
                HumanMessage(render_request(request, state["errors"] or None)),
            ]
            raw = self.llm.invoke(messages)
            patch = raw if isinstance(raw, MappingPatch) else MappingPatch.model_validate(raw)
            patch = MappingPatch(
                mappings=[m.model_copy(update={"origin": "agent"}) for m in patch.mappings],
                unmappable=patch.unmappable,
            )
            return {"patch": patch, "attempts": state["attempts"] + 1}

        def check_node(state: _State) -> dict[str, Any]:
            assert state["patch"] is not None
            errors = scope_errors(request, state["patch"])
            if not errors:
                errors = check(state["patch"]).errors
            return {"errors": errors, "ok": not errors}

        def route(state: _State) -> str:
            if state["ok"] or state["attempts"] >= self.max_attempts:
                return END
            return "propose"

        graph = StateGraph(_State)
        graph.add_node("propose", propose_node)
        graph.add_node("check", check_node)
        graph.set_entry_point("propose")
        graph.add_edge("propose", "check")
        graph.add_conditional_edges("check", route)
        final = graph.compile().invoke({"attempts": 0, "patch": None, "errors": [], "ok": False})
        assert final["patch"] is not None
        return final["patch"]
