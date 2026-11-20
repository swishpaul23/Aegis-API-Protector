"""Orchestrator: detect -> rules -> agent (if needed) -> validate -> apply or queue."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aegis_healer.agent import AgentError, BudgetExceeded, ProposalRequest, Proposer, Usage
from aegis_healer.agent.privacy import Privacy, has_data_like_segment, mask_path, prepare_samples
from aegis_healer.detect import detect_drift, soft_missing
from aegis_healer.mapping import apply_mappings
from aegis_healer.matcher import match
from aegis_healer.notify import PendingNotice
from aegis_healer.paths import flatten, get_path, model_key, model_paths
from aegis_healer.quarantine import Quarantine
from aegis_healer.runlog import RunRecord, append_run, run_message
from aegis_healer.store import MappingStore, default_store, merge
from aegis_healer.types import (
    DriftReport,
    FieldMapping,
    MappingPatch,
    PatchCheck,
    TypeMismatch,
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
    usage: Usage = Field(default_factory=Usage)
    quarantine_id: str | None = None
    pending_is_new: bool = False  # internal: False when an existing pending fix was reused

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


def _mapping_sig(mappings: list[FieldMapping]) -> frozenset[tuple]:
    return frozenset(
        (
            m.target,
            tuple(m.sources),
            m.transform,
            json.dumps(m.value_map, sort_keys=True),
            m.separator,
        )
        for m in mappings
    )


def mask_report(report: DriftReport) -> DriftReport:
    """A DriftReport safe to write to .aegis/ (committed) or to show without payload keys.

    `missing` holds model paths (schema, not payload), so it is never masked.
    """
    mismatches, seen = [], set()
    for t in report.type_mismatches:
        path = mask_path(t.path)
        if path not in seen:
            seen.add(path)
            mismatches.append(TypeMismatch(path=path, expected=t.expected, received=t.received))
    return DriftReport(
        missing=list(report.missing),
        unexpected=sorted({mask_path(p) for p in report.unexpected}),
        type_mismatches=mismatches,
    )


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
        max_llm_calls: int | None = 10,
        quarantine: bool = True,
        on_pending: Callable[[PendingNotice], None] | None = None,
    ) -> None:
        if max_llm_calls is not None and max_llm_calls < 0:
            raise ValueError("max_llm_calls must be >= 0 or None")
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
        self.max_llm_calls = max_llm_calls
        self.quarantine = quarantine
        self.on_pending = on_pending
        if quarantine and persist and getattr(self.store, "root", None) is None:
            logging.getLogger("aegis_healer").warning(
                "quarantine is enabled but the store has no root folder: "
                "blocked records will not be quarantined"
            )

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

    def _pending_for(
        self, key: str, proposed: MappingPatch, report: DriftReport | None
    ) -> tuple[int, bool]:
        """Reuse an identical open pending fix (same base version and mappings) if any."""
        base_version = self.store.active_version(key)
        wanted = _mapping_sig(proposed.mappings)
        for item in self.store.list_pending(key):
            if item.base_version == base_version and _mapping_sig(item.patch.mappings) == wanted:
                return item.id, False
        # Pending files are committed to git: never store data-like keys, whatever the mode.
        safe_report = mask_report(report) if report is not None else report
        return self.store.add_pending(key, proposed, safe_report), True

    def _reusable_pending(
        self,
        key: str,
        current: list[FieldMapping],
        resolved: list[FieldMapping],
        unresolved: list[str],
        raw_samples: list[dict[str, Any]],
        model: type[BaseModel],
    ) -> list[FieldMapping] | None:
        """Mappings of an open, up-to-date pending fix that already heal this group."""
        try:
            base_version = self.store.active_version(key)
            items = self.store.list_pending(key)
        except (OSError, ValueError):
            return None
        wanted = set(unresolved)
        targets = wanted | {m.target for m in resolved}
        for item in items:
            if item.base_version != base_version:
                continue
            maps = [m for m in item.patch.mappings if m.target in targets]
            if not wanted <= {m.target for m in maps}:
                continue
            group = merge(_rebase(resolved, current), maps)
            check = validate_patch(
                merge(current, group),
                raw_samples,
                model,
                ignore_targets=[u.target for u in item.patch.unmappable],
                require_sources=group,
                redact=True,
            )
            if check.ok:
                return group
        return None

    def _propose(
        self,
        request: ProposalRequest,
        check: Callable[[MappingPatch], PatchCheck],
        budget: int | None,
        usage: Usage,
    ) -> MappingPatch:
        """Call the proposer, falling back once for old `propose(request, check)` ones."""
        assert self.proposer is not None
        legacy = False
        if hasattr(self.proposer, "last_usage"):
            try:  # reset so a proposer that never updates it can't re-report an old value
                self.proposer.last_usage = Usage()
            except AttributeError:
                pass
        try:
            try:
                return self.proposer.propose(request, check, budget=budget)
            except TypeError as exc:
                if "budget" not in str(exc):
                    raise
            legacy = True
            if budget is not None and budget < 1:
                raise BudgetExceeded(["LLM call budget exhausted"])
            return self.proposer.propose(request, check)  # type: ignore[call-arg]
        finally:
            last = getattr(self.proposer, "last_usage", None)
            if isinstance(last, Usage):
                usage.add(last)
            if legacy and not (isinstance(last, Usage) and last.calls):
                usage.calls += 1  # no usage reported: count the call against the budget

    # ── main entry point ────────────────────────────────────
    def validate(
        self,
        payload: dict[str, Any] | list[dict[str, Any]],
        model: type[BaseModel],
        records_path: str | None = None,
    ) -> HealResult:
        result, failing, count = self._validate(payload, model, records_path)
        if self.privacy != "full" and result.report is not None:
            result.report = mask_report(result.report)
        if self.persist:
            self._quarantine(model, result, failing)
            self._log_run(model, result, count)
        if result.status == "pending" and result.pending_is_new:
            self._notify(model, result, len(failing) if result.quarantine_id else 0)
        return result

    def _notify(self, model: type[BaseModel], result: HealResult, quarantined: int) -> None:
        if self.on_pending is None or not self.persist:
            return  # a dry run has nothing to review
        mappings = result.proposed.mappings if result.proposed else []
        agent_conf = [m.confidence for m in mappings if m.origin == "agent"]
        key = model_key(model)
        fields = sorted({m.target for m in mappings})
        notice = PendingNotice(
            model_key=key,
            pending_id=result.pending_id,
            fields=fields,
            min_confidence=min(agent_conf) if agent_conf else None,
            quarantined=quarantined,
            # Fixed and value-free: result.message can contain payload text.
            message=f"{len(fields)} field fix(es) for {key} need review",
        )
        try:
            self.on_pending(notice)
        except Exception as exc:  # a notifier must never change the heal result
            logging.getLogger("aegis_healer").warning(
                "pending notification failed: %s", type(exc).__name__
            )

    def _quarantine(
        self, model: type[BaseModel], result: HealResult, failing: list[dict[str, Any]]
    ) -> None:
        root = getattr(self.store, "root", None)
        if root is None or not self.quarantine or result.status not in ("pending", "failed"):
            return
        if not failing:
            return
        try:
            # The reason is the status only: payload values must never leak into it.
            result.quarantine_id = Quarantine(root).add_or_merge(
                model_key(model), failing, result.status, result.pending_id
            )
        except (
            OSError,
            ValueError,
        ) as exc:  # keep the pipeline flowing; the records are in the caller's hands
            logging.getLogger("aegis_healer").warning(
                "could not quarantine records: %s", type(exc).__name__
            )

    def _log_run(self, model: type[BaseModel], result: HealResult, count: int) -> None:
        root = getattr(self.store, "root", None)
        if root is None:
            return
        record = RunRecord(
            model_key=model_key(model),
            status=result.status,
            records=count,
            healed_or_valid=len(result.records),
            failed_records=result.failed_records,
            fields_fixed=sorted({m.target for m in result.applied}),
            pending_id=result.pending_id,
            usage=result.usage,
            message=run_message(result.status, result.message, self.privacy),
        )
        try:
            append_run(root, record)
        except OSError as exc:  # an audit-log failure must never break validation
            logging.getLogger("aegis_healer").warning("could not write run log: %s", exc)

    def _validate(
        self,
        payload: dict[str, Any] | list[dict[str, Any]],
        model: type[BaseModel],
        records_path: str | None,
    ) -> tuple[HealResult, list[dict[str, Any]], int]:
        records = self._extract(payload, records_path)
        failing: list[dict[str, Any]] = []
        result = self._run(records, model, failing)
        return result, failing, len(records)

    def _run(
        self,
        records: list[dict[str, Any]],
        model: type[BaseModel],
        failing_out: list[dict[str, Any]] | None = None,
    ) -> HealResult:
        if not records:
            return HealResult(status="ok")

        key = model_key(model)
        active = self.store.get_active(key)
        valid, failing = self._split(records, active, model)
        if not failing:
            return HealResult(status="ok", records=valid)
        if failing_out is not None:
            failing_out.extend(failing)

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
        usage = Usage()

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

            reused = (
                self._reusable_pending(
                    key, current, det.resolved, det.unresolved, raw_samples, model
                )
                if det.unresolved and self.proposer is not None
                else None
            )
            if reused is not None:
                # An open pending fix already heals this shape: no agent call, same pending id.
                new = merge(new, reused)
                agent_mappings += [m for m in reused if m.origin == "agent"]
                continue
            if det.unresolved and self.proposer is None:
                no_agent += det.unresolved
            elif det.unresolved:
                used = {s for m in det.resolved for s in m.sources}
                first = flatten(samples[0])
                request = ProposalRequest(
                    model_name=model.__name__,
                    model_schema=model_paths(model),
                    unresolved=det.unresolved,
                    # Map-style data keys (emails, ids) can't be field renames: never send them.
                    candidates={
                        p: type(first.get(p)).__name__
                        for p in report.unexpected
                        if p not in used
                        and (self.privacy == "full" or not has_data_like_segment(p))
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
                        redact=self.privacy != "full",
                    )

                remaining = (
                    None if self.max_llm_calls is None else max(self.max_llm_calls - usage.calls, 0)
                )
                try:
                    patch = self._propose(request, check, remaining, usage)
                except AgentError as exc:
                    return HealResult(
                        usage=usage,
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
                        usage=usage,
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
            usage=usage,
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
        check_result = validate_patch(
            combined, all_samples, model, require_sources=new, redact=self.privacy != "full"
        )
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
                usage=usage,
            )

        # Pending fixes don't store payload samples: .aegis/ is meant to be committed.
        pending_id, is_new = (
            self._pending_for(key, proposed, first_report) if self.persist else (None, False)
        )
        why = (
            f"{len(still_failing)} record(s) still fail with this fix"
            if still_failing
            else f"agent confidence below threshold {self.threshold}"
        )
        base.status = "pending"
        base.pending_id = pending_id
        base.pending_is_new = is_new
        base.message = f"Fix needs review ({why})."
        return base
