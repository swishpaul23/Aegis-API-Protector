import json

import pytest
from fakes import FakeProposer, m
from fixtures.models import InternalUserProfile
from fixtures.payloads import PHASE1_V2
from pydantic import BaseModel, field_validator

from aegis_healer import Healer
from aegis_healer.agent.privacy import mask_value, prepare_samples
from aegis_healer.store.files import FileStore
from aegis_healer.types import MappingPatch, Unmappable

PATCH = MappingPatch(
    mappings=[m(["first_name", "last_name"], "full_name", "concat")],
    unmappable=[Unmappable(target="user_id", reason="uuid")],
)


def test_masks_free_text_and_emails():
    out = mask_value(
        {
            "emailAddress": "alice@example.com",
            "first_name": "Alice",
            "street": "742 Evergreen Terrace",
        }
    )
    assert out == {
        "emailAddress": "<email>",
        "first_name": "<string, 5 chars>",
        "street": "<string, 21 chars>",
    }


def test_keeps_categories_bools_epochs():
    data = {"status": "active", "flag": True, "ts": 1686787200}
    assert mask_value(data) == data


def test_masks_ids_and_numbers():
    assert mask_value({"userId": "a3f1-b8c2-1001", "n": 42}) == {
        "userId": "<string, 14 chars>",
        "n": "<int>",
    }


def test_masks_nested_and_lists():
    data = {"a": {"b": "Bob", "c": [{"d": 1.5, "e": "x@y.io"}, None]}, "tags": ["ACTIVE", "Mixed"]}
    assert mask_value(data) == {
        "a": {"b": "<string, 3 chars>", "c": [{"d": "<float>", "e": "<email>"}, None]},
        "tags": ["ACTIVE", "<string, 5 chars>"],
    }


def test_scalars_dates_and_float_epochs():
    assert mask_value("alice@example.com") == "<email>"
    assert mask_value("2024-01-02T03:04:05Z") == "<datetime>"
    assert mask_value("2024-01-02") == "<datetime>"
    assert mask_value(1686787200.5) == 1686787200.5
    assert mask_value(5) == "<int>"


def test_schema_mode():
    assert prepare_samples([{"a": "x", "b": 1}], "schema") == [{"a": "str", "b": "int"}]


def test_full_mode_is_deep_copy():
    src = [{"a": {"b": "x"}}]
    out = prepare_samples(src, "full")
    assert out == src and out[0]["a"] is not src[0]["a"]


@pytest.fixture
def tmp_store(tmp_path):
    return FileStore(tmp_path / ".aegis")


def test_healer_sends_masked_samples_by_default(tmp_store):
    fake = FakeProposer(PATCH)
    Healer(store=tmp_store, proposer=fake).validate(PHASE1_V2, InternalUserProfile)
    samples = fake.requests[0].samples
    assert samples[0]["emailAddress"] == "<email>"
    assert "Alice" not in json.dumps(samples)


def test_healer_full_mode_opt_in(tmp_store):
    fake = FakeProposer(PATCH)
    Healer(store=tmp_store, proposer=fake, privacy="full").validate(PHASE1_V2, InternalUserProfile)
    assert "Alice" in json.dumps(fake.requests[0].samples)


class _Probe:
    """Proposer that calls check() with a patch and records the result."""

    def __init__(self, patch):
        self.patch, self.checks = patch, []

    def propose(self, request, check, budget=None):
        self.checks.append(check(self.patch))
        return self.patch


def test_check_errors_do_not_leak_values_by_default(tmp_store):
    bad = MappingPatch(mappings=[m("first_name", "full_name", "to_int")])
    probe = _Probe(bad)
    Healer(store=tmp_store, proposer=probe).validate(PHASE1_V2, InternalUserProfile)
    errors = probe.checks[0].errors
    assert errors and "Alice" not in json.dumps(errors)


def test_check_errors_keep_values_in_full_mode(tmp_store):
    bad = MappingPatch(mappings=[m("first_name", "full_name", "to_int")])
    probe = _Probe(bad)
    Healer(store=tmp_store, proposer=probe, privacy="full").validate(PHASE1_V2, InternalUserProfile)
    assert "Alice" in json.dumps(probe.checks[0].errors)


def test_masks_data_like_keys_only():
    out = mask_value({"alice@example.com": 1, "user_id": 2, "2024-01-02 x": 3, "a1b2c3d4e5f6": 4})
    assert out == {"<key 0>": "<int>", "user_id": "<int>", "<key 2>": "<int>", "<key 3>": "<int>"}
    assert prepare_samples([{"bob smith": "x"}], "schema") == [{"<key 0>": "str"}]


def test_unknown_types_fail_closed():
    from decimal import Decimal

    assert mask_value((1, "Alice")) == ["<int>", "<string, 5 chars>"]
    assert mask_value({"d": Decimal("12.5")}) == {"d": "<Decimal>"}


def test_trailing_newline_not_a_category():
    assert mask_value("alice\n") == "<string, 6 chars>"


# ── data-like keys (final review item 2) ──────────────────────
class Scored(BaseModel):
    user_id: int
    scores: dict[str, int]


SCORED = {"ref": "7", "scores": {"alice@example.com": 3}}


def test_is_data_like_key_and_mask_path():
    from aegis_healer.agent.privacy import is_data_like_key, mask_path

    assert is_data_like_key("alice@example.com") and not is_data_like_key("user_id")
    assert mask_path("scores.alice@example.com") == "scores.<key>"
    assert mask_path("address.zip") == "address.zip"
    assert mask_path("notes.Alice Smith") == "notes.<key>"


def test_data_like_keys_never_reach_llm_or_pending(tmp_store):
    fake = FakeProposer(MappingPatch(mappings=[m("ref", "user_id", "to_int", 0.5)]))
    r = Healer(store=tmp_store, proposer=fake).validate(SCORED, Scored)
    assert r.status == "pending", r.message
    assert "alice" not in json.dumps(fake.requests[0].model_dump())
    assert list(fake.requests[0].candidates) == ["ref"]
    files = list((tmp_store.root / "pending").rglob("*.json"))
    assert files
    for f in files:
        assert "alice" not in f.read_text(encoding="utf-8")
    assert "alice" not in r.report.model_dump_json()


def test_pending_report_masked_even_in_full_mode(tmp_store):
    fake = FakeProposer(MappingPatch(mappings=[m("ref", "user_id", "to_int", 0.5)]))
    r = Healer(store=tmp_store, proposer=fake, privacy="full").validate(SCORED, Scored)
    assert "scores.alice@example.com" in r.report.unexpected  # in-memory report kept in full mode
    for f in (tmp_store.root / "pending").rglob("*.json"):
        assert "alice" not in f.read_text(encoding="utf-8")


class _Checked(BaseModel):
    code: str

    @field_validator("code")
    @classmethod
    def _no_secret(cls, v):
        if v != "ok":
            raise ValueError(f"bad code {v}")
        return v


def test_redact_hides_value_error_text_and_data_keys():
    from aegis_healer.validate import validate_patch

    out = validate_patch([], [{"code": "SECRET-42"}], _Checked, redact=True)
    assert not out.ok and "SECRET-42" not in json.dumps(out.errors)
    assert "value_error" in out.errors[0]
    out = validate_patch([], [{"user_id": 1, "scores": {"alice@x.io": "z"}}], Scored, redact=True)
    assert not out.ok and "alice" not in json.dumps(out.errors)
    kept = validate_patch([], [{"code": "SECRET-42"}], _Checked)
    assert "SECRET-42" in json.dumps(kept.errors)  # unredacted callers keep the detail


def test_final_validation_is_redacted(tmp_store, monkeypatch):
    import aegis_healer.healer as hmod

    calls = []
    real = hmod.validate_patch

    def spy(*a, **k):
        calls.append(k.get("redact", False))
        return real(*a, **k)

    monkeypatch.setattr(hmod, "validate_patch", spy)
    fake = FakeProposer(MappingPatch(mappings=[m("ref", "user_id", "to_int", 0.5)]))
    Healer(store=tmp_store, proposer=fake).validate(SCORED, Scored)
    assert calls and all(calls)


# ── identifier-like segments are not data (re-review) ─────────
class Lined(BaseModel):
    user_id: int
    address_line_1: str


def test_identifier_segments_with_digits_kept_as_candidates(tmp_store):
    fake = FakeProposer(MappingPatch(mappings=[m("street_line1", "address_line_1", "none", 0.5)]))
    r = Healer(store=tmp_store, proposer=fake).validate(
        {"user_id": 1, "street_line1": "x y"}, Lined
    )
    assert "street_line1" in fake.requests[0].candidates
    assert r.status in ("pending", "healed"), r.message
    assert r.report.missing == ["address_line_1"]
    assert r.report.unexpected == ["street_line1"]


def test_segment_predicate_and_missing_never_masked():
    from aegis_healer.agent.privacy import is_data_like_segment, mask_path
    from aegis_healer.healer import mask_report
    from aegis_healer.types import DriftReport

    for ok in ("street_line1", "addressLine2", "oauth2_token", "user_id", "com"):
        assert not is_data_like_segment(ok)
    for bad in ("alice@example", "Alice Smith", "2026-01-02", "a3f1.b8c2", "123", "x+y"):
        assert is_data_like_segment(bad)
    assert mask_path("contact.addressLine2") == "contact.addressLine2"
    rep = mask_report(DriftReport(missing=["oauth2_token"], unexpected=["m.bob@x.io"]))
    assert rep.missing == ["oauth2_token"] and rep.unexpected == ["m.<key>"]


def test_field_names_with_a_digit_stay_visible():
    out = mask_value({"streetLine1": "742 Evergreen", "oauth2_token": "t", "usr_8812abc": "x"})
    assert set(out) == {"streetLine1", "oauth2_token", "<key 2>"}
