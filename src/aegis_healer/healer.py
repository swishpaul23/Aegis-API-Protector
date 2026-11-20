"""Orchestrator: detect -> rules -> agent (if needed) -> validate -> apply or queue."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aegis_healer.agent import ProposalRequest, Proposer
from aegis_healer.detect import detect_drift
from aegis_healer.mapping import apply_mappings
from aegis_healer.matcher import match
from aegis_healer.paths import flatten, get_path, model_key, model_paths
from aegis_healer.store import MappingStore, default_store, merge
from aegis_healer.types import DriftReport, FieldMapping, MappingPatch, PatchCheck
from aegis_healer.validate import validate_patch

Status = Literal["ok", "healed", "pending", "failed"]


class DriftError(Exception):
    def __init__(self, result: HealResult) -> None:
        super().__init__(f"{result.status}: {result.message}")
        self.result = result


class HealResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: Status
    records: list[Any] = Field(default_factory=list)
    failed_records: int = 0
    applied: list[FieldMapping] = Field(default_factory=list)
    proposed: MappingPatch | None = None
    pending_id: int | None = None
    report: DriftReport | None = None
    message: str = ""

    def raise_for_status(self) -> HealResult:
        if self.status not in ("ok", "healed"):
            raise DriftError(self)
        return self


def _rebase(new: list[FieldMapping], active: list[FieldMapping]) -> list[FieldMapping]:
    """Point new mappings at raw payload paths when they read a field an active mapping renamed."""
    renamed = {
        m.target: m.source for m in active if m.transform == "none" and isinstance(m.source, str)
    }
    out = []
    for m in new:
        if isinstance(m.source, str) and m.source in renamed:
            m = m.model_copy(update={"source": renamed[m.source]})
        elif isinstance(m.source, list):
            m = m.model_copy(update={"source": [renamed.get(s, s) for s in m.source]})
        out.append(m)
    return out


class Healer:
    def __init__(
        self,
        store: MappingStore | None = None,
        auto_apply_threshold: float = 0.9,
        proposer: Proposer | None | Literal["auto"] = "auto",
        sample_size: int = 5,
        persist: bool = True,
    ) -> None:
        if not 0.0 <= auto_apply_threshold <= 1.0:
            raise ValueError("auto_apply_threshold must be between 0 and 1")
        if proposer == "auto":
            try:
                from aegis_healer.agent.graph import AgentProposer

                proposer = AgentProposer()
            except RuntimeError:
                proposer = None
        self.store = store or default_store()
        self.threshold = auto_apply_threshold
        self.proposer = proposer
        self.sample_size = sample_size
        self.persist = persist

    # ── helpers ─────────────────────────────────────────────
    @staticmethod
    def _extract(payload: Any, records_path: str | None) -> list[dict[str, Any]]:
        data = get_path(payload, records_path) if records_path else payload
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list) and all(isinstance(r, dict) for r in data):
            return data
        raise TypeError("payload must be a JSON object or a list of objects")

    @staticmethod
    def _split(
        records: list[dict[str, Any]], mappings: list[FieldMapping], model: type[BaseModel]
    ) -> tuple[list[BaseModel], list[dict[str, Any]]]:
        valid, failing = [], []
        for raw in records:
            try:
                valid.append(model.model_validate(apply_mappings(raw, mappings)))
            except (ValidationError, ValueError, KeyError):
                failing.append(raw)
        return valid, failing

    def _safe_apply(self, raw: dict[str, Any], mappings: list[FieldMapping]) -> dict[str, Any]:
        try:
            return apply_mappings(raw, mappings)
        except (ValueError, KeyError):
            return raw

    # ── main entry point ────────────────────────────────────
    def validate(
        self,
        payload: dict[str, Any] | list[dict[str, Any]],
        model: type[BaseModel],
        records_path: str | None = None,
    ) -> HealResult:
        records = self._extract(payload, records_path)
        if not records:
            return HealResult(status="ok")

        key = model_key(model)
        active = self.store.get_active(key)
        valid, failing = self._split(records, active, model)
        if not failing:
            return HealResult(status="ok", records=valid)

        raw_samples = failing[: self.sample_size]
        samples = [self._safe_apply(r, active) for r in raw_samples]
        report = detect_drift(samples[0], model)
        det = match(report, samples, model)

        agent_patch = MappingPatch()
        if det.unresolved:
            if self.proposer is None:
                return HealResult(
                    status="failed",
                    records=valid,
                    failed_records=len(failing),
                    report=report,
                    proposed=MappingPatch(mappings=det.resolved),
                    message=(
                        f"Unresolved drift in {', '.join(det.unresolved)} and agent disabled: "
                        "set ANTHROPIC_API_KEY"
                    ),
                )
            used = {s for m in det.resolved for s in m.sources}
            first = flatten(samples[0])
            request = ProposalRequest(
                model_name=model.__name__,
                model_schema=model_paths(model),
                unresolved=det.unresolved,
                candidates={
                    p: type(first.get(p)).__name__ for p in report.unexpected if p not in used
                },
                already_mapped=[m.target for m in det.resolved],
                samples=samples,
            )

            def check(p: MappingPatch) -> PatchCheck:
                combined = merge(active, _rebase(det.resolved + p.mappings, active))
                return validate_patch(
                    combined, raw_samples, model, [u.target for u in p.unmappable]
                )

            agent_patch = self.proposer.propose(request, check)

        new = _rebase(det.resolved + agent_patch.mappings, active)
        proposed = MappingPatch(mappings=new, unmappable=agent_patch.unmappable)
        combined = merge(active, new)
        base = HealResult(
            status="failed",
            records=valid,
            failed_records=len(failing),
            report=report,
            proposed=proposed,
        )

        if agent_patch.unmappable:
            reasons = "; ".join(f"{u.target}: {u.reason}" for u in agent_patch.unmappable)
            base.message = f"Cannot heal without changing your model. {reasons}"
            return base

        check_result = validate_patch(combined, raw_samples, model)
        if not check_result.ok:
            base.message = "Proposed fix failed validation: " + "; ".join(check_result.errors[:5])
            return base

        healed, still_failing = self._split(records, combined, model)
        agent_ok = self.threshold < 1.0 and all(
            m.confidence >= self.threshold for m in agent_patch.mappings
        )
        if agent_ok and not still_failing:
            if self.persist:
                self.store.save_active(key, combined, f"auto: {len(new)} field(s)")
            return HealResult(
                status="healed", records=healed, applied=new, report=report, proposed=proposed
            )

        pending_id = self.store.add_pending(key, proposed, report) if self.persist else None
        why = (
            f"{len(still_failing)} record(s) still fail with this fix"
            if still_failing
            else f"agent confidence below threshold {self.threshold}"
        )
        base.status = "pending"
        base.pending_id = pending_id
        base.message = f"Fix needs review ({why})."
        return base
