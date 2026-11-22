import pytest
from fixtures.models import InternalUserProfile, User
from fixtures.payloads import PHASE1_V2, V1, V2
from pydantic import BaseModel, ConfigDict

from aegis_healer.detect import detect_drift
from aegis_healer.matcher import _candidates, match


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
    assert "email_address" in targets
    assert "user_id" in r.unresolved
    # address -> contactInfo.mailingAddress swaps the parent object, not a pure
    # wrap, so the rule matcher leaves it for the agent and the review threshold.
    assert "address.city" in r.unresolved


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


# --- rule_b: a same-name move is only a rule fix when the field was wrapped or unwrapped ---

@pytest.mark.parametrize(
    ("target", "source"),
    [
        ("email", "contact.email"),  # flat -> nested
        ("error.message", "message"),  # nested -> flat
        ("default_settings.collection_method", "collection_method"),
        ("x", "a.b.x"),  # deeper wrap
        ("a.b.x", "x"),  # deeper unwrap
        ("a.b.x", "b.x"),  # partial unwrap keeps the shared tail
        ("email", "contactInfo.email"),  # parent style doesn't matter
        ("contact_info.email", "contactInfo.email"),  # same shape, renamed parent -> rule_a
        ("contact_info.email", "email"),
    ],
)
def test_wrap_or_unwrap_still_matches(target, source):
    sources, _, _ = _candidates(target, [source])
    assert sources == [source]


@pytest.mark.parametrize(
    ("target", "source"),
    [
        # real-API eval, case github-organization-secret-scanning-alert-6898c369:
        # a license's id must not be loaded as a user's id.
        ("repository.license.node_id", "push_protection_bypassed_by.node_id"),
        # real-API eval, case github-rate-limit-overview-ce4e6170: different rate limit.
        ("resources.code_scanning_upload.remaining", "resources.copilot_usage_records.remaining"),
        # sibling swap: parent replaced, not wrapped/unwrapped -> goes to the agent.
        (
            "spending_controls.spending_limits_currency",
            "authorization_controls.spending_limits_currency",
        ),
        ("a.x", "b.x"),
        ("a.b.x", "c.b.x"),  # a different outer parent is still a swap
    ],
)
def test_parent_swap_is_not_a_rule_move(target, source):
    assert _candidates(target, [source])[0] == []


def test_eval_license_node_id_left_for_agent():
    class License(BaseModel):
        node_id: str

    class Repo(BaseModel):
        license: License

    class Alert(BaseModel):
        repository: Repo

    rec = {"repository": {}, "push_protection_bypassed_by": {"node_id": "U_kgDO"}}
    r = _run(rec, Alert)
    assert r.resolved == []
    assert "repository.license.node_id" in r.unresolved


def test_wrap_still_resolves_when_a_swap_candidate_is_also_present():
    # `msg.text` is a parent swap and no longer a candidate, so `text` is the only answer.
    class Err(BaseModel):
        text: str

    class Resp(BaseModel):
        error: Err

    rec = {"text": "boom", "msg": {"text": "other"}}
    r = _run(rec, Resp)
    assert [(m.source, m.target) for m in r.resolved] == [("text", "error.text")]


def test_two_wrap_candidates_still_not_guessed():
    class Resp(BaseModel):
        text: str

    rec = {"a": {"text": "x"}, "b": {"text": "y"}}
    r = _run(rec, Resp)
    assert r.resolved == []
    assert r.unresolved == ["text"]
