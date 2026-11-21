"""Meaning-drift checks wired into the Healer (fakes only, no LLM)."""

import pytest
from pydantic import BaseModel

from aegis_healer import Healer, MeaningFinding
from aegis_healer.meaning.config import MeaningConfig
from aegis_healer.meaning.profile import load_profile
from aegis_healer.paths import model_key
from aegis_healer.quarantine import Quarantine, replay
from aegis_healer.runlog import read_runs, run_message
from aegis_healer.store.files import FileStore
from aegis_healer.types import FieldMapping


class Payment(BaseModel):
    id: int
    amount: float
    currency: str
    paid_at: str


K = model_key(Payment)
AMOUNTS = [10.0, 25.0, 40.0, 60.0, 90.0, 120.0, 180.0, 250.0, 350.0, 500.0]


def batch(run=0, scale=1, amount_key="amount", paid_key="paid_at", tz="Z"):
    return [
        {
            "id": run * 10 + i + 1,
            amount_key: a * scale,
            "currency": "usd",
            paid_key: f"2026-01-0{i % 9 + 1}T10:00:00{tz}",
        }
        for i, a in enumerate(AMOUNTS)
    ]


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path / ".aegis")


@pytest.fixture
def notices():
    return []


def healer(store, notices=None, **kw):
    return Healer(
        store=store,
        proposer=None,
        on_pending=(notices.append if notices is not None else None),
        **kw,
    )


def baseline(h, **kw):
    for run in range(4):
        result = h.validate(batch(run, **kw), Payment)
        assert result.status == "ok", result.message
        assert result.findings == []


def profile_bytes(store):
    path = store.root / "profiles"
    files = list(path.glob("*.json")) if path.exists() else []
    return {p.name: p.read_bytes() for p in files}


def test_baseline_learns_without_findings(store):
    h = healer(store)
    baseline(h)
    profile = load_profile(store.root, K)
    assert profile.fields["amount"].n == 40
    assert store.list_pending(K) == []


def test_cents_batch_blocked_and_quarantined(store, notices):
    h = healer(store, notices)
    baseline(h)
    result = h.validate(batch(4, scale=100), Payment)
    assert result.status == "pending"
    assert result.records == []
    assert [f.path for f in result.findings] == ["amount"]
    assert result.findings[0].kind == "scale"
    assert result.findings[0].suggested_scale == pytest.approx(0.01)
    assert result.message == "Meaning drift needs review: amount"
    item = store.get_pending(result.pending_id)
    assert item.kind == "meaning"
    assert item.findings == result.findings
    assert len(item.patch.mappings) == 1
    fix = item.patch.mappings[0]
    assert (fix.source, fix.target, fix.scale) == ("amount", "amount", pytest.approx(0.01))
    assert fix.origin == "deterministic" and fix.confidence == 1.0
    batches = Quarantine(store.root).list(K)
    assert len(batches) == 1
    assert batches[0].pending_id == result.pending_id
    assert len(batches[0].records) == 10
    assert batches[0].records[0]["amount"] == 1000.0  # raw dicts
    assert len(notices) == 1
    assert notices[0].fields == ["amount"]
    assert notices[0].message == f"1 field fix(es) for {K} need review"
    assert notices[0].quarantined == 10
    assert read_runs(store.root, 1)[0].message == "meaning_drift"


def test_identical_drift_reuses_pending(store, notices):
    h = healer(store, notices)
    baseline(h)
    first = h.validate(batch(4, scale=100), Payment)
    second = h.validate(batch(4, scale=100), Payment)
    assert second.status == "pending"
    assert second.pending_id == first.pending_id
    assert len(store.list_pending(K)) == 1
    assert len(notices) == 1
    assert len(Quarantine(store.root).list(K)) == 1


def test_flag_policy_loads_records_but_stays_pending(store, notices):
    h = healer(store, notices, meaning=MeaningConfig(fields={"amount": "flag"}))
    baseline(h)
    result = h.validate(batch(4, scale=100), Payment)
    assert result.status == "pending"
    assert len(result.records) == 10
    assert [f.path for f in result.findings] == ["amount"]
    assert result.pending_id is not None
    assert Quarantine(store.root).list(K) == []
    assert len(notices) == 1


def test_off_policy_no_finding(store, notices):
    h = healer(store, notices, meaning=MeaningConfig(fields={"amount": "off"}))
    baseline(h)
    result = h.validate(batch(4, scale=100), Payment)
    assert result.status == "ok"
    assert result.findings == []
    assert len(result.records) == 10
    assert notices == []


def test_config_file_loaded_by_default(store):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "config.toml").write_text(
        '[meaning]\nfields = { amount = "flag" }\n', encoding="utf-8"
    )
    h = healer(store)
    baseline(h)
    result = h.validate(batch(4, scale=100), Payment)
    assert result.status == "pending" and len(result.records) == 10


def test_broken_config_fails_at_construction(store):
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "config.toml").write_text(
        '[meaning]\nfields = { amount = "loud" }\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="amount"):
        healer(store)


def test_meaning_false_writes_no_profile(store):
    h = healer(store, meaning=False)
    baseline(h)
    assert profile_bytes(store) == {}
    result = h.validate(batch(4, scale=100), Payment)
    assert result.status == "ok" and len(result.records) == 10


def test_profile_not_updated_on_drift_run(store):
    h = healer(store)
    baseline(h)
    before = profile_bytes(store)
    assert before
    h.validate(batch(4, scale=100), Payment)
    assert profile_bytes(store) == before


def test_dry_run_computes_but_writes_nothing(store):
    baseline(healer(store))
    before = profile_bytes(store)
    dry = healer(store, persist=False)
    result = dry.validate(batch(4, scale=100), Payment)
    assert result.status == "pending"
    assert result.records == []
    assert result.pending_id is None
    assert [f.path for f in result.findings] == ["amount"]
    assert result.proposed is not None and result.proposed.mappings[0].scale == pytest.approx(0.01)
    assert store.list_pending(K) == []
    assert Quarantine(store.root).list(K) == []
    assert profile_bytes(store) == before
    # A dry run on clean data does not teach the baseline either.
    dry.validate(batch(5), Payment)
    assert profile_bytes(store) == before


def test_timezone_drift_blocks_without_suggested_fix(store):
    h = healer(store)
    baseline(h)
    result = h.validate(batch(4, tz="+05:30"), Payment)
    assert result.status == "pending"
    assert [(f.path, f.kind) for f in result.findings] == [("paid_at", "timezone")]
    assert result.records == []
    assert store.get_pending(result.pending_id).patch.mappings == []


def test_mixed_batch_schema_healed_and_meaning_blocked(store, notices):
    """Review Focus 5."""
    h = healer(store, notices)
    baseline(h)
    result = h.validate(batch(4, scale=100, paid_key="paidAt"), Payment)
    assert result.status == "pending"
    assert result.records == []
    assert [m.target for m in result.applied] == ["paid_at"]
    assert store.active_version(K) == 1  # schema fix saved as before
    assert [m.target for m in store.get_active(K)] == ["paid_at"]
    item = store.get_pending(result.pending_id)
    assert item.kind == "meaning" and item.base_version == 1
    batches = Quarantine(store.root).list(K)
    assert len(batches) == 1 and len(batches[0].records) == 10
    assert batches[0].pending_id == result.pending_id
    assert len(notices) == 1


def test_mixed_batch_blocks_only_affected_records(store):
    """Records without a usable value on the drifted field are not blocked."""

    class Opt(BaseModel):
        id: int
        amount: float | None = None
        currency: str
        paid_at: str

    key = model_key(Opt)
    h = healer(store)
    for run in range(4):
        assert h.validate(batch(run), Opt).status == "ok"
    rows = batch(4, scale=100) + [
        {"id": 99, "amount": None, "currency": "usd", "paid_at": "2026-01-01T10:00:00Z"}
    ]
    result = h.validate(rows, Opt)
    assert result.status == "pending"
    assert [r.id for r in result.records] == [99]
    assert len(Quarantine(store.root).list(key)[0].records) == 10


def test_scale_fix_composes_with_rename_and_replays_dollars(store):
    """Review Focus 2."""
    store.save_active(K, [FieldMapping(source="amt", target="amount")], "rename")
    h = healer(store)
    baseline(h, amount_key="amt")
    result = h.validate(batch(4, scale=100, amount_key="amt"), Payment)
    assert result.status == "pending"
    fix = store.get_pending(result.pending_id).patch.mappings
    assert len(fix) == 1
    assert (fix[0].source, fix[0].target, fix[0].scale) == ("amt", "amount", pytest.approx(0.01))
    store.resolve_pending(result.pending_id, True)
    out = replay(Quarantine(store.root), h, Payment)
    assert sorted(r.amount for r in out.released) == AMOUNTS
    assert Quarantine(store.root).list(K) == []


def test_existing_mapping_transform_kept_when_scaled(store):
    store.save_active(K, [FieldMapping(source="amt", target="amount", transform="to_float")], "x")
    h = healer(store)
    rows = lambda run, scale=1: [  # noqa: E731
        {**r, "amt": str(r.pop("amt"))} for r in batch(run, scale=scale, amount_key="amt")
    ]
    for run in range(4):
        assert h.validate(rows(run), Payment).status == "ok"
    result = h.validate(rows(4, 100), Payment)
    fix = store.get_pending(result.pending_id).patch.mappings[0]
    assert (fix.source, fix.transform, fix.scale) == ("amt", "to_float", pytest.approx(0.01))


def test_no_baseline_phase2_unchanged(store, notices):
    h = healer(store, notices)
    result = h.validate(batch(0, scale=100), Payment)
    assert result.status == "ok" and result.findings == []


def test_run_message_meaning_code():
    msg = "Meaning drift needs review: amount"
    assert run_message("pending", msg, "masked") == "meaning_drift"
    assert run_message("pending", msg, "full") == msg
    assert run_message("pending", "Fix needs review (x).", "masked") == "pending_review"


def test_meaning_finding_exported():
    assert MeaningFinding.__name__ == "MeaningFinding"


def _cents_source_baseline(store, h, mult=100):
    """Source sends cents; the active mapping scales them to dollars."""
    store.save_active(K, [FieldMapping(source="amount", target="amount", scale=0.01)], "cents")
    for run in range(4):
        assert h.validate(batch(run, scale=mult), Payment).status == "ok"


def test_scale_fix_removes_existing_scale_when_source_reverts(store):
    h = healer(store)
    _cents_source_baseline(store, h)
    result = h.validate(batch(4, scale=1), Payment)  # source now sends dollars
    assert result.status == "pending"
    assert result.failed_records == 10
    fix = store.get_pending(result.pending_id).patch.mappings
    assert len(fix) == 1
    assert (fix[0].source, fix[0].target, fix[0].scale) == ("amount", "amount", None)
    store.resolve_pending(result.pending_id, True)
    out = replay(Quarantine(store.root), h, Payment)
    assert sorted(r.amount for r in out.released) == AMOUNTS


def test_scale_fix_composes_with_existing_scale(store):
    h = healer(store)
    _cents_source_baseline(store, h)
    result = h.validate(batch(4, scale=10000), Payment)  # hundredths of cents
    fix = store.get_pending(result.pending_id).patch.mappings
    assert fix[0].scale == 0.0001
    store.resolve_pending(result.pending_id, True)
    out = replay(Quarantine(store.root), h, Payment)
    assert sorted(r.amount for r in out.released) == pytest.approx(AMOUNTS)


def test_meaning_pending_never_reused_for_schema_drift(store):
    from aegis_healer.types import DriftReport, MappingPatch

    heal = FieldMapping(source="amt", target="amount")
    meaning_id = store.add_pending(
        K, MappingPatch(mappings=[heal]), DriftReport(), kind="meaning", findings=[]
    )
    h = healer(store)
    raws = batch(0, amount_key="amt")[:5]
    assert h._reusable_pending(K, [], [], ["amount"], raws, Payment) is None
    pid, is_new = h._pending_for(K, MappingPatch(mappings=[heal]), DriftReport())
    assert is_new and pid != meaning_id
    assert store.get_pending(pid).kind == "schema"
