import json

import pytest
from fakes import m
from fixtures.models import User
from fixtures.payloads import V1

from aegis_healer import Healer
from aegis_healer.meaning.profile import FieldProfile, ModelProfile, load_profile, save_profile
from aegis_healer.meaning.resolve import keep_data
from aegis_healer.quarantine import Quarantine, replay
from aegis_healer.store.files import FileStore
from aegis_healer.types import DriftReport, MappingPatch, MeaningFinding

K = "fixtures.models.User"


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path / ".aegis")


def finding(path="amount"):
    return MeaningFinding(path=path, kind="scale", detail="x100", baseline_n=30, batch_n=6)


def add_meaning(store, findings=None):
    return store.add_pending(
        K,
        MappingPatch(mappings=[]),
        DriftReport(),
        kind="meaning",
        findings=findings if findings is not None else [finding()],
    )


def test_old_pending_json_loads(store):
    pid = store.add_pending(K, MappingPatch(mappings=[m("a", "b")]), DriftReport())
    path = store.root / "pending" / f"{pid:04d}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("kind")
    data.pop("findings")
    path.write_text(json.dumps(data), encoding="utf-8")
    item = store.get_pending(pid)
    assert item.kind == "schema" and item.findings == []


def test_meaning_pending_roundtrip(store):
    pid = add_meaning(store)
    item = store.get_pending(pid)
    assert item.kind == "meaning" and item.findings[0].path == "amount"
    assert item.status == "pending"


def test_keep_pending_only_meaning_and_pending(store):
    sid = store.add_pending(K, MappingPatch(mappings=[m("a", "b")]), DriftReport())
    with pytest.raises(ValueError):
        store.keep_pending(sid)
    mid = add_meaning(store)
    store.keep_pending(mid)
    assert store.get_pending(mid).status == "kept"
    assert store.list_pending() == [store.get_pending(sid)]
    with pytest.raises(ValueError):
        store.keep_pending(mid)  # already kept
    rid = add_meaning(store)
    store.resolve_pending(rid, False)
    with pytest.raises(ValueError):
        store.keep_pending(rid)


def test_keep_pending_does_not_touch_mappings(store):
    store.save_active(K, [m("a", "b")], "one")
    mid = add_meaning(store)
    store.keep_pending(mid)
    assert store.active_version(K) == 1


def test_keep_data_resets_profile_fields(store):
    save_profile(
        store.root,
        ModelProfile(
            model_key=K,
            fields={"amount": FieldProfile(n=30), "other": FieldProfile(n=5)},
        ),
    )
    pid = add_meaning(store, [finding("amount"), finding("amount")])
    assert keep_data(store, pid) == ["amount"]
    assert set(load_profile(store.root, K).fields) == {"other"}
    assert store.get_pending(pid).status == "kept"


def test_keep_data_needs_root(store):
    store.root = None
    with pytest.raises(ValueError):
        keep_data(store, 1)


def _quarantined(store, status):
    if status == "approved":  # a meaning fix needs a suggested change to be approved
        pid = store.add_pending(K, MappingPatch(mappings=[]), DriftReport())
        store.resolve_pending(pid, True)
    else:
        pid = add_meaning(store)
    if status == "rejected":
        store.resolve_pending(pid, False)
    elif status == "kept":
        store.keep_pending(pid)
    q = Quarantine(store.root)
    q.add(K, [V1] * 2, "meaning", pid)
    return q


@pytest.mark.parametrize("status", ["pending"])
def test_replay_gate_blocks_unresolved(store, status):
    q = _quarantined(store, status)
    out = replay(q, Healer(store=store, proposer=None), User)
    assert out.released == [] and out.still_blocked == 2
    assert out.batches_cleared == [] and out.batches_reduced == []
    assert len(q.list()) == 1


@pytest.mark.parametrize("status", ["approved", "kept"])
def test_replay_gate_releases_resolved(store, status):
    q = _quarantined(store, status)
    out = replay(q, Healer(store=store, proposer=None), User)
    assert len(out.released) == 2 and out.still_blocked == 0
    assert len(out.batches_cleared) == 1


def test_replay_gate_missing_pending_is_unresolved(store):
    q = Quarantine(store.root)
    q.add(K, [V1], "meaning", 99)
    out = replay(q, Healer(store=store, proposer=None), User)
    assert out.released == [] and out.still_blocked == 1


def test_keep_data_save_failure_leaves_pending(store, monkeypatch):
    import aegis_healer.meaning.resolve as res

    pid = add_meaning(store)

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(res, "save_profile", boom)
    with pytest.raises(OSError):
        keep_data(store, pid)
    assert store.get_pending(pid).status == "pending"


def test_keep_data_rejects_schema_fix(store):
    sid = store.add_pending(K, MappingPatch(mappings=[m("a", "b")]), DriftReport())
    with pytest.raises(ValueError):
        keep_data(store, sid)


def test_approve_meaning_fix_without_mappings_refused(store):
    pid = add_meaning(store)
    with pytest.raises(ValueError, match="use keep instead"):
        store.resolve_pending(pid, True)
    assert store.get_pending(pid).status == "pending"
    store.resolve_pending(pid, False)  # reject is still fine


def test_replay_rejected_batch_is_dead_not_blocked(store):
    q = _quarantined(store, "rejected")
    out = replay(q, Healer(store=store, proposer=None), User)
    assert out.still_blocked == 0 and len(out.dead_batches) == 1
    assert out.released == []


def test_discard_batches(store):
    from aegis_healer.quarantine import discard_batches

    q = _quarantined(store, "rejected")
    ids = [b.id for b in q.list()]
    assert discard_batches(q, ids) == []
    assert q.list() == []
