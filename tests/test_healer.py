import pytest
from fakes import FakeProposer, m, v2_agent_patch
from fixtures.models import InternalUserProfile, User
from fixtures.payloads import PHASE1_V2, V1, V2
from pydantic import BaseModel

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


def test_scope_bypass_never_applies(store):
    """A proposer returning an out-of-scope fix must not get it applied (security review #1)."""

    class U(BaseModel):
        user_id: int
        role: str

    evil = MappingPatch(
        mappings=[m("uid", "user_id", "to_int", 1.0), m("injected_role", "role", "none", 1.0)]
    )
    r = healer(store, evil).validate({"uid": "1", "role": "viewer", "injected_role": "admin"}, U)
    assert r.status == "failed" and "outside the drifted fields" in r.message
    assert not list(store.root.rglob("mappings/*.json"))


def test_saved_mapping_with_vanished_source_does_not_block(store):
    rec1 = {k: v for k, v in V1.items() if k != "user_name"} | {"userName": "ana"}
    assert healer(store).validate(rec1, User).status == "healed"
    rec2 = {k: v for k, v in V1.items() if k != "email"} | {"Email": "a@x.io"}
    r = healer(store).validate(rec2, User)
    assert r.status == "healed", r.message


class P(BaseModel):
    id: int
    nick: str | None = None


def test_renamed_optional_field_not_lost(store):
    r = healer(store).validate({"id": 1, "Nick": "x"}, P)
    assert r.status == "healed" and r.records[0].nick == "x"


def test_threshold_one_still_applies_rule_fixes(store):
    rec = {k: v for k, v in V1.items() if k != "user_name"} | {"userName": "ana"}
    assert healer(store, auto_apply_threshold=1.0).validate(rec, User).status == "healed"


def test_mixed_drift_shapes_in_one_batch(store):
    a = {k: v for k, v in V1.items() if k != "user_name"} | {"userName": "ana"}
    b = {k: v for k, v in V1.items() if k != "email"} | {"Email": "a@x.io"}
    r = healer(store).validate([V1, a, b], User)
    assert r.status == "healed", r.message
    assert len(r.records) == 3


def test_agent_failure_is_clean(store):
    from aegis_healer.agent import AgentError

    class Broken:
        def propose(self, request, check, budget=None):
            raise AgentError(["model returned garbage"])

    r = Healer(store=store, proposer=Broken()).validate(V2, User)
    assert r.status == "failed" and "garbage" in r.message


# ── pending reuse (final review item 3) ───────────────────────
def test_open_pending_fix_reused_without_agent_calls(store):
    seen = []
    fake = FakeProposer(v2_agent_patch(0.8))
    h = Healer(store=store, proposer=fake, on_pending=seen.append)
    results = [h.validate(V2, User) for _ in range(3)]
    assert [r.status for r in results] == ["pending"] * 3
    assert len(fake.requests) == 1
    assert len({r.pending_id for r in results}) == 1
    assert len(store.list_pending()) == 1
    assert len(seen) == 1
    assert results[1].usage.calls == 0


def test_pending_not_reused_when_it_does_not_fit(store):
    first = healer(store, v2_agent_patch(0.8)).validate(V2, User)
    other = {**V2, "joined": V2["registered"]}
    del other["registered"]
    patch = MappingPatch(
        mappings=[
            m("id", "user_id", "to_int", 0.8),
            m("joined", "signup_date", "parse_datetime", 0.8),
            m("address.zip", "address.postal_code", "none", 0.8),
        ]
    )
    fake = FakeProposer(patch)
    second = Healer(store=store, proposer=fake).validate(other, User)
    assert len(fake.requests) == 1
    assert second.status == "pending" and second.pending_id != first.pending_id


def test_stale_pending_not_reused(store):
    healer(store, v2_agent_patch(0.8)).validate(V2, User)
    store.save_active("fixtures.models.User", [], "bump version")
    fake = FakeProposer(v2_agent_patch(0.8))
    Healer(store=store, proposer=fake).validate(V2, User)
    assert len(fake.requests) == 1


# ── legacy two-argument proposers (M5) ────────────────────────
class _LegacyProposer:
    def __init__(self, patch):
        self.patch, self.calls = patch, 0

    def propose(self, request, check):
        self.calls += 1
        check(self.patch)
        return self.patch


def test_two_argument_proposer_still_works(store):
    legacy = _LegacyProposer(v2_agent_patch(0.95))
    r = Healer(store=store, proposer=legacy).validate(V2, User)
    assert r.status == "healed", r.message
    assert legacy.calls == 1


def test_two_argument_proposer_respects_zero_budget(store):
    legacy = _LegacyProposer(v2_agent_patch(0.95))
    r = Healer(store=store, proposer=legacy, max_llm_calls=0).validate(V2, User)
    assert r.status == "failed" and legacy.calls == 0


# ── quarantine without a store root (M8) ──────────────────────
class _RootlessStore:
    root = None

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_warns_once_when_quarantine_has_no_root(store, caplog):
    rootless = _RootlessStore(store)
    with caplog.at_level("WARNING", logger="aegis_healer"):
        h = Healer(store=rootless, proposer=None)
        h.validate(V2, User)
        h.validate(V2, User)
    warnings = [r for r in caplog.records if "quarantine" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].name == "aegis_healer"
    caplog.clear()
    with caplog.at_level("WARNING", logger="aegis_healer"):
        Healer(store=rootless, proposer=None, quarantine=False)
    assert not caplog.records


class _StaleUsageProposer:
    """Reports usage but never resets it: a stale value must not be counted twice."""

    def __init__(self, patch):
        from aegis_healer.agent import Usage

        self.patch = patch
        self.last_usage = Usage(calls=5)

    def propose(self, request, check, budget=None):
        check(self.patch)
        return self.patch


def test_stale_last_usage_not_counted(store):
    r = Healer(store=store, proposer=_StaleUsageProposer(v2_agent_patch(0.95))).validate(V2, User)
    assert r.usage.calls == 0
