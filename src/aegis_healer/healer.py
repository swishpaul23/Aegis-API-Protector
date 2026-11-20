"""Orchestrator: detect -> rules -> agent (if needed) -> validate -> apply or queue."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aegis_healer.agent import AgentError, ProposalRequest, Proposer
from aegis_healer.agent.privacy import Privacy, prepare_samples
from aegis_healer.detect import detect_drift, soft_missing
from aegis_healer.mapping import apply_mappings
from aegis_healer.matcher import match
from aegis_healer.paths import flatten, get_path, model_key, model_paths
from aegis_healer.store import MappingStore, default_store, merge
from aegis_healer.types import (
    DriftReport,
    FieldMapping,
    MappingPatch,
    PatchCheck,
    Unmappable,
)
from aegis_healer.validate import validate_patch

Status = Literal["ok", "healed", "pending", "failed"]
MAX_SCAN = 50  # failing records inspected to find distinct drift shapes


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


def _rebase(new: list[FieldMapping], current: list[FieldMapping]) -> list[FieldMapping]:
    """Point new mappings at raw payload paths when they read a field `current` renamed."""
    renamed = {
        m.target: m.source for m in current if m.transform == "none" and isinstance(m.source, str)
    }
    out = []
    for m in new:
        if isinstance(m.source, str) and m.source in renamed:
            m = m.model_copy(update={"source": renamed[m.source]})
        elif isinstance(m.source, list):
            m = m.model_copy(update={"source": [renamed.get(s, s) for s in m.source]})
        out.append(m)
    return out


def _signature(report: DriftReport) -> tuple:
    return (
        tuple(report.missing),
        tuple(report.unexpected),
        tuple(t.path for t in report.type_mismatches),
    )


class Healer:
    def __init__(
        self,
        store: MappingStore | None = None,
        auto_apply_threshold: float = 0.9,
        proposer: Proposer | None | Literal["auto"] = "auto",
        sample_size: int = 5,
        persist: bool = True,
        privacy: Privacy = "masked",
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
        self.privacy = privacy

    # ── helpers ─────────────────────────────────────────────
    @staticmethod
    def _extract(payload: Any, records_path: str | None) -> list[dict[str, Any]]:
        if records_path:
            if not isinstance(payload, dict):
                raise TypeError("records_path needs the payload to be a JSON object")
            try:
                data = get_path(payload, records_path)
            except KeyError:
                raise KeyError(f"records_path '{records_path}' not found in payload") from None
        else:
            data = payload
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list) and all(isinstance(r, dict) for r in data):
            return data
        raise TypeError("records must be a JSON object or a list of objects")

    @staticmethod
    def _split(
        records: list[dict[str, Any]], mappings: list[FieldMapping], model: type[BaseModel]
    ) -> tuple[list[BaseModel], list[dict[str, Any]]]:
        valid, failing = [], []
        for raw in records:
            try:
                mapped = apply_mappings(raw, mappings)
                obj = model.model_validate(mapped)
            except (ValidationError, ValueError, KeyError):
                failing.append(raw)
                continue
            # Valid, but a renamed optional field would silently lose its data.
            if soft_missing(mapped, model):
                failing.append(raw)
            else:
                valid.append(obj)
        return valid, failing

    @staticmethod
    def _safe_apply(raw: dict[str, Any], mappings: list[FieldMapping]) -> dict[str, Any]:
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

        # Records can drift in different ways in one batch: heal each shape in turn.
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for raw in failing[:MAX_SCAN]:
            sig = _signature(detect_drift(self._safe_apply(raw, active), model))
            groups.setdefault(sig, []).append(raw)

        new: list[FieldMapping] = []
        agent_mappings: list[FieldMapping] = []
        unmappable: list[Unmappable] = []
        no_agent: list[str] = []
        all_samples: list[dict[str, Any]] = []
        first_report: DriftReport | None = None

        for raws in sorted(groups.values(), key=len, reverse=True):
            raw_samples = raws[: self.sample_size]
            all_samples += raw_samples
            current = merge(active, new)
            samples = [self._safe_apply(r, current) for r in raw_samples]
            report = detect_drift(samples[0], model)
            first_report = first_report or report
            if report.is_clean:
                continue  # an earlier group's fix already covers this shape
            det = match(report, samples, model)
            group_new = list(det.resolved)

            if det.unresolved and self.proposer is None:
                no_agent += det.unresolved
            elif det.unresolved:
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
                    samples=prepare_samples(samples, self.privacy),
                )

                def check(
                    p: MappingPatch,
                    current: list[FieldMapping] = current,
                    det_resolved: list[FieldMapping] = det.resolved,
                    raw_samples: list[dict[str, Any]] = raw_samples,
                ) -> PatchCheck:
                    added = _rebase(det_resolved + p.mappings, current)
                    return validate_patch(
                        merge(current, added),
                        raw_samples,
                        model,
                        ignore_targets=[u.target for u in p.unmappable],
                        require_sources=added,
                    )

                try:
                    patch = self.proposer.propose(request, check)
                except AgentError as exc:
                    return HealResult(
                        status="failed",
                        records=valid,
                        failed_records=len(failing),
                        report=report,
                        proposed=MappingPatch(mappings=new + det.resolved),
                        message=f"Agent could not produce a valid fix: {exc}",
                    )
                # Defense in depth: never trust a proposer to have checked its own scope.
                from aegis_healer.agent.graph import scope_errors

                bad = scope_errors(request, patch)
                if bad:
                    return HealResult(
                        status="failed",
                        records=valid,
                        failed_records=len(failing),
                        report=report,
                        message="Agent proposed changes outside the drifted fields: "
                        + "; ".join(bad[:3]),
                    )
                group_new += patch.mappings
                agent_mappings += patch.mappings
                unmappable += patch.unmappable

            new = merge(new, _rebase(group_new, current))

        proposed = MappingPatch(mappings=new, unmappable=unmappable)
        base = HealResult(
            status="failed",
            records=valid,
            failed_records=len(failing),
            report=first_report,
            proposed=proposed,
        )
        if no_agent:
            base.message = (
                f"Unresolved drift in {', '.join(sorted(set(no_agent)))} and agent disabled: "
                "set ANTHROPIC_API_KEY"
            )
            return base
        if unmappable:
            reasons = "; ".join(f"{u.target}: {u.reason}" for u in unmappable)
            base.message = f"Cannot heal without changing your model. {reasons}"
            return base

        combined = merge(active, new)
        check_result = validate_patch(combined, all_samples, model, require_sources=new)
        if not check_result.ok:
            base.message = "Proposed fix failed validation: " + "; ".join(check_result.errors[:5])
            return base

        healed, still_failing = self._split(records, combined, model)
        agent_ok = not agent_mappings or (
            self.threshold < 1.0 and all(m.confidence >= self.threshold for m in agent_mappings)
        )
        if agent_ok and not still_failing:
            if self.persist:
                self.store.save_active(key, combined, f"auto: {len(new)} field(s)")
            return HealResult(
                status="healed",
                records=healed,
                applied=new,
                report=first_report,
                proposed=proposed,
            )

        # Pending fixes don't store payload samples: .aegis/ is meant to be committed.
        pending_id = self.store.add_pending(key, proposed, first_report) if self.persist else None
        why = (
            f"{len(still_failing)} record(s) still fail with this fix"
            if still_failing
            else f"agent confidence below threshold {self.threshold}"
        )
        base.status = "pending"
        base.pending_id = pending_id
        base.message = f"Fix needs review ({why})."
        return base
