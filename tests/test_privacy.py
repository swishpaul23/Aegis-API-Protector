import json

import pytest
from fakes import FakeProposer, m
from fixtures.models import InternalUserProfile
from fixtures.payloads import PHASE1_V2

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
