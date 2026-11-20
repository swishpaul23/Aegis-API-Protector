import pytest
from fakes import FakeProposer, m, v2_agent_patch
from fixtures.models import InternalUserProfile, User
from fixtures.payloads import PHASE1_V2, V1, V2

from aegis_healer import DriftError, Healer
from aegis_healer.store.files import FileStore
from aegis_healer.types import MappingPatch, Unmappable


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path / ".aegis")


def healer(store, patch=None, **kw):
    return Healer(store=store, proposer=FakeProposer(patch) if patch else None, **kw)


def test_v1_ok(store):
    r = healer(store).validate(V1, User)
    assert r.status == "ok" and len(r.records) == 1


def test_v2_healed_above_threshold(store):
    h = healer(store, v2_agent_patch(0.95), auto_apply_threshold=0.9)
    r = h.validate(V2, User)
    assert r.status == "healed", r.message
    assert r.records[0].address.postal_code == "V5A 1S6"
    assert h.validate(V2, User).status == "ok"  # persisted


def test_v2_pending_below_threshold(store):
    r = healer(store, v2_agent_patch(0.8)).validate(V2, User)
    assert r.status == "pending" and r.pending_id == 1
    assert store.active_version("fixtures.models.User") == 0


def test_threshold_one_always_pending(store):
    r = healer(store, v2_agent_patch(1.0), auto_apply_threshold=1.0).validate(V2, User)
    assert r.status == "pending"


def test_deterministic_only_heals_without_agent(store):
    rec = {k: v for k, v in V1.items() if k != "user_name"} | {"userName": "ana"}
    r = healer(store).validate(rec, User)
    assert r.status == "healed" and r.records[0].user_name == "ana"


def test_agent_only_sees_unresolved_fields(store):
    fake = FakeProposer(v2_agent_patch())
    Healer(store=store, proposer=fake).validate(V2, User)
    assert set(fake.requests[0].unresolved) == {"user_id", "signup_date", "address.postal_code"}


def test_no_key_failed(store):
    r = healer(store).validate(V2, User)
    assert r.status == "failed" and "ANTHROPIC_API_KEY" in r.message


def test_envelope_and_empty(store):
    h = healer(store, v2_agent_patch())
    r = h.validate({"data": [V2, V2]}, User, records_path="data")
    assert r.status == "healed" and len(r.records) == 2
    empty = h.validate({"data": []}, User, records_path="data")
    assert empty.status == "ok" and empty.records == []


def test_invalid_threshold(store):
    with pytest.raises(ValueError):
        Healer(store=store, proposer=None, auto_apply_threshold=1.2)


def test_raise_for_status(store):
    with pytest.raises(DriftError):
        healer(store).validate(V2, User).raise_for_status()


def test_persist_false_heals_in_memory_only(store):
    r = healer(store, v2_agent_patch(), persist=False).validate(V2, User)
    assert r.status == "healed"
    assert store.active_version("fixtures.models.User") == 0


def test_unmappable_fails_with_reason(store):
    patch = MappingPatch(
        mappings=[m(["first_name", "last_name"], "full_name", "concat")],
        unmappable=[Unmappable(target="user_id", reason="userId is a UUID string, not an int")],
    )
    r = healer(store, patch).validate(PHASE1_V2, InternalUserProfile)
    assert r.status == "failed" and "UUID" in r.message
    assert store.active_version("fixtures.models.InternalUserProfile") == 0


def test_bad_agent_patch_never_applied(store):
    bad = MappingPatch(
        mappings=[m("id", "user_id", "to_int"), m("registered", "address.postal_code")]
    )
    r = healer(store, bad).validate(V2, User)
    assert r.status == "failed"
    assert store.active_version("fixtures.models.User") == 0
