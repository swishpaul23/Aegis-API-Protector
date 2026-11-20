"""Pipeline: fetch -> heal -> load valid -> quarantine blocked -> release after approval."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from aegis_healer.healer import Healer, HealResult, Status
from aegis_healer.paths import model_key
from aegis_healer.quarantine import Quarantine, remove_batches, replay


class PipelineRun(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: Status
    loaded: int  # records sunk this run, including released ones
    quarantined: int  # records waiting in quarantine for this model after the run
    pending_id: int | None = None
    result: HealResult


class Pipeline:
    def __init__(
        self,
        fetch: Callable[[], Any],
        model: type[BaseModel],
        sink: Callable[[list[BaseModel]], None],
        healer: Healer | None = None,
        records_path: str | None = None,
    ) -> None:
        self.fetch = fetch
        self.model = model
        self.sink = sink
        self.healer = healer or Healer()
        self.records_path = records_path

    def run(self) -> PipelineRun:
        payload = self.fetch()  # errors propagate: the caller's scheduler retries
        result = self.healer.validate(payload, self.model, self.records_path)
        loaded = 0
        if result.records:
            self.sink(result.records)
            loaded += len(result.records)
        sunk = {r.model_dump_json() for r in result.records}

        root = getattr(self.healer.store, "root", None)
        remaining = 0
        if root is not None:
            quarantine = Quarantine(Path(root))
            released = replay(quarantine, self.healer, self.model, remove=False)
            if released.released:
                # Records the fetch re-served were already delivered this run.
                fresh = [r for r in released.released if r.model_dump_json() not in sunk]
                if fresh:
                    self.sink(fresh)  # remove only after the sink accepted them
                    loaded += len(fresh)
                remove_batches(quarantine, released.batches_cleared)  # logs a warning per failure
            key = model_key(self.model)
            remaining = sum(len(b.records) for b in quarantine.list(key))
        return PipelineRun(
            status=result.status,
            loaded=loaded,
            quarantined=remaining,
            pending_id=result.pending_id,
            result=result,
        )
