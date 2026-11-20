from fixtures.models import InternalUserProfile, User
from fixtures.payloads import PHASE1_V2, V1, V2
from pydantic import BaseModel, ConfigDict

from aegis_healer.detect import detect_drift
from aegis_healer.matcher import match


def _run(rec, model=User):
    return match(detect_drift(rec, model), [rec], model)


def test_v2_deterministic():
    r = _run(V2)
    got = {(m.source, m.target, m.confidence) for m in r.resolved}
    assert got == {("userName", "user_name", 1.0), ("contact.email", "email", 0.95)}
    assert set(r.unresolved) == {"user_id", "signup_date", "address.postal_code"}


class StrictId(BaseModel):
    model_config = ConfigDict(strict=True)
    user_id: int


def test_type_mismatch_fixed():
    r = _run({"user_id": "7"}, StrictId)
    assert len(r.resolved) == 1
    m = r.resolved[0]
    assert (m.source, m.target, m.transform) == ("user_id", "user_id", "to_int")


def test_ambiguous_not_guessed():
    rec = {k: v for k, v in V1.items() if k != "user_name"}
    rec |= {"userName": "ana", "user-name": "ana"}
    r = _run(rec)
    assert "user_name" in r.unresolved
    assert r.resolved == []


def test_uncoercible_unresolved():
    assert "user_id" in _run({**V1, "user_id": "abc"}).unresolved


def test_phase1_uuid_not_forced_into_int():
    r = _run(PHASE1_V2, InternalUserProfile)
    targets = {m.target for m in r.resolved}
    assert {"email_address", "address.city"} <= targets
    assert "user_id" in r.unresolved


class City(BaseModel):
    city: str


class TwoHomes(BaseModel):
    home: City
    work: City


def test_two_targets_competing_for_one_source_not_guessed():
    rec = {"loc": {"city": "Van"}}
    r = _run(rec, TwoHomes)
    assert r.resolved == []
    assert set(r.unresolved) == {"home.city", "work.city"}
