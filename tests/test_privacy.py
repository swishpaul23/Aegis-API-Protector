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
