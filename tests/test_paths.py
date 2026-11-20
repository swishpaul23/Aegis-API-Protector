import pytest
from fixtures.models import User
from fixtures.payloads import V1
from pydantic import ValidationError

from aegis_healer.paths import flatten, model_paths, pop_path, set_path
from aegis_healer.types import FieldMapping


def test_flatten_nested():
    flat = flatten(V1)
    assert flat["address.postal_code"] == "V5A 1S6"
    assert "address" not in flat


def test_flatten_list_is_leaf():
    assert flatten({"tags": [1, 2]}) == {"tags": [1, 2]}


def test_set_and_pop_path():
    d = {}
    set_path(d, "a.b", 1)
    assert d == {"a": {"b": 1}}
    assert pop_path(d, "a.b") == 1
    assert d == {}


def test_model_paths():
    assert set(model_paths(User)) == {
        "user_id",
        "user_name",
        "email",
        "signup_date",
        "address.city",
        "address.postal_code",
    }


def test_field_mapping_confidence_bounds():
    with pytest.raises(ValidationError):
        FieldMapping(source="a", target="b", confidence=1.5)


def test_concat_needs_list():
    with pytest.raises(ValidationError):
        FieldMapping(source="a", target="b", transform="concat")
