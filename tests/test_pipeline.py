import importlib.util
from pathlib import Path

import pytest
from fakes import FakeProposer, v2_agent_patch
from fixtures.models import User
from fixtures.payloads import V1, V2

from aegis_healer import Healer, Pipeline, PipelineRun
from aegis_healer.quarantine import Quarantine
from aegis_healer.store.files import FileStore
from aegis_healer.types import DriftReport, MappingPatch

ROOT = Path(__file__).resolve().parent.parent


def build(tmp_path, payload, on_pending=None):
    store = FileStore(tmp_path / ".aegis")
    healer = Healer(store=store, proposer=FakeProposer(v2_agent_patch(0.8)), on_pending=on_pending)
    sunk: list = []
    pipe = Pipeline(lambda: payload, User, sunk.extend, healer=healer)
    return store, healer, pipe, sunk


def test_run_loads_valid_and_quarantines_rest(tmp_path):
    store, _, pipe, sunk = build(tmp_path, [V1, V2])
    run = pipe.run()
    assert isinstance(run, PipelineRun)
    assert run.status == "pending"
    assert run.loaded == 1 and len(sunk) == 1
    assert run.quarantined == 1
    assert run.pending_id is not None
    assert run.result.quarantine_id is not None


def test_second_run_does_not_duplicate_pending(tmp_path):
    seen = []
    store, _, pipe, sunk = build(tmp_path, [V1, V2], on_pending=seen.append)
    first = pipe.run()
    second = pipe.run()
    assert second.pending_id == first.pending_id
    assert len(store.list_pending("fixtures.models.User")) == 1
    assert len(seen) == 1  # reused pending does not notify again
    assert len(Quarantine(store.root).list()) == 1  # same batch, no duplicate records
    assert second.quarantined == 1
    assert second.result.quarantine_id == first.result.quarantine_id


def test_quarantine_merges_new_records_into_existing_batch(tmp_path):
    store, healer, _, _ = build(tmp_path, [V1, V2])
    first = healer.validate([V1, V2], User)
    other = {**V2, "id": "999"}
    second = healer.validate([V2, other], User)
    assert second.pending_id == first.pending_id
    (batch,) = Quarantine(store.root).list()
    assert batch.id == first.quarantine_id == second.quarantine_id
    assert len(batch.records) == 2


def test_run_after_approval_releases_quarantine_into_sink(tmp_path):
    store, _, pipe, sunk = build(tmp_path, [V1, V2])
    first = pipe.run()
    store.resolve_pending(first.pending_id, True)
    run = pipe.run()
    assert run.status == "ok"
    assert run.loaded == 2  # the fetch re-serves V2, so the released copy is not sunk again
    assert Quarantine(store.root).list() == []
    assert run.quarantined == 0


def test_reserved_drifted_records_reach_sink_exactly_once(tmp_path):
    store, _, pipe, sunk = build(tmp_path, [V2, {**V2, "id": "777"}])
    first = pipe.run()
    assert first.loaded == 0 and first.quarantined == 2
    store.resolve_pending(first.pending_id, True)
    run = pipe.run()
    assert len(sunk) == 2  # == number of fetched records, no duplicates from the release
    assert run.loaded == 2 and run.quarantined == 0
    assert Quarantine(store.root).list() == []


def test_release_counts_quarantined_records_from_earlier_run(tmp_path):
    store, healer, _, sunk = build(tmp_path, [V1, V2])
    healer.validate([V1, {**V2, "id": "555"}], User)
    pending = store.list_pending("fixtures.models.User")[0]
    store.resolve_pending(pending.id, True)
    sunk2: list = []
    run = Pipeline(lambda: [V1], User, sunk2.extend, healer=healer).run()
    assert run.loaded == 2  # 1 fetched + 1 released
    assert len(sunk2) == 2
    assert Quarantine(store.root).list() == []


def test_sink_failure_keeps_quarantine(tmp_path):
    store, healer, _, _ = build(tmp_path, [V1, V2])
    healer.validate([V1, V2], User)
    store.resolve_pending(store.list_pending("fixtures.models.User")[0].id, True)

    def bad_sink(records):
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        Pipeline(lambda: [], User, bad_sink, healer=healer).run()
    assert len(Quarantine(store.root).list()) == 1


def test_fetch_error_propagates(tmp_path):
    store = FileStore(tmp_path / ".aegis")

    def fetch():
        raise ConnectionError("boom")

    pipe = Pipeline(fetch, User, lambda r: None, healer=Healer(store=store, proposer=None))
    with pytest.raises(ConnectionError):
        pipe.run()


def test_example_script_imports():
    path = ROOT / "examples" / "pipeline" / "run_pipeline.py"
    spec = importlib.util.spec_from_file_location("run_pipeline_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "main")


def test_repeated_failed_runs_share_one_batch(tmp_path):
    store = FileStore(tmp_path / ".aegis")
    healer = Healer(store=store, proposer=None)
    first = healer.validate([V2], User)
    second = healer.validate([V2], User)
    assert first.status == second.status == "failed"
    (batch,) = Quarantine(store.root).list()
    assert second.quarantine_id == first.quarantine_id == batch.id
    assert len(batch.records) == 1


def test_released_list_has_no_duplicates(tmp_path):
    store = FileStore(tmp_path / ".aegis")
    quarantine = Quarantine(store.root)
    quarantine.add("fixtures.models.User", [V1], "failed", None)
    pid = store.add_pending("fixtures.models.User", MappingPatch(mappings=[]), DriftReport())
    store.resolve_pending(pid, True)
    quarantine.add("fixtures.models.User", [V1], "pending", pid)  # legacy duplicate batches
    sunk: list = []
    run = Pipeline(lambda: [], User, sunk.extend, healer=Healer(store=store, proposer=None)).run()
    assert len(sunk) == 1 and run.loaded == 1
    assert quarantine.list() == []


def test_no_replay_when_not_persisting(tmp_path):
    store = FileStore(tmp_path / ".aegis")
    Quarantine(store.root).add("fixtures.models.User", [V1], "failed", None)
    sunk: list = []
    pipe = Pipeline(
        lambda: [], User, sunk.extend, healer=Healer(store=store, proposer=None, persist=False)
    )
    run = pipe.run()
    assert sunk == [] and run.loaded == 0
    assert len(Quarantine(store.root).list()) == 1


def test_approved_still_failing_fix_releases_healable_records(tmp_path):
    """Item 1 E2E: 7 records, 1 unfixable -> pending -> approve -> 6 released, 1 kept."""
    store = FileStore(tmp_path / ".aegis")
    # Confidence above threshold: the fix is pending only because a record still fails.
    healer = Healer(store=store, proposer=FakeProposer(v2_agent_patch(0.95)))
    batch = [{**V2, "id": str(i)} for i in range(1, 7)] + [{**V2, "id": "abc"}]
    sunk: list = []
    first = Pipeline(lambda: batch, User, sunk.extend, healer=healer).run()
    assert first.status == "pending" and "still fail" in first.result.message
    assert first.quarantined == 7 and sunk == []
    store.resolve_pending(first.pending_id, True)
    run = Pipeline(lambda: [], User, sunk.extend, healer=healer).run()
    assert sorted(r.user_id for r in sunk) == [1, 2, 3, 4, 5, 6]
    assert run.loaded == 6 and run.quarantined == 1
    (left,) = Quarantine(store.root).list()
    assert left.records == [{**V2, "id": "abc"}]
    again = Pipeline(lambda: [], User, sunk.extend, healer=healer).run()
    assert again.loaded == 0 and len(sunk) == 6  # exactly once
