import copy

import pytest
from fixtures.payloads import V2

from aegis_healer.mapping import apply_mappings, apply_transform, map_value
from aegis_healer.types import FieldMapping as FM


def test_rename():
    out = apply_mappings(V2, [FM(source="userName", target="user_name")])
    assert out["user_name"] == "ana" and "userName" not in out


def test_move_nested():
    out = apply_mappings(V2, [FM(source="contact.email", target="email")])
    assert out["email"] == "a@x.io" and "contact" not in out


def test_to_int():
    assert apply_transform("1", "to_int") == 1
    with pytest.raises(ValueError):
        apply_transform("1.5", "to_int")


def test_unknown_transform_rejected():
    with pytest.raises(ValueError):
        apply_transform("x", "eval")


def test_input_not_mutated():
    before = copy.deepcopy(V2)
    apply_mappings(V2, [FM(source="userName", target="user_name")])
    assert V2 == before


def test_concat_and_value_map_and_epoch():
    rec = {"f": "Alice", "l": "Johnson", "status": "active", "ts": 1686787200}
    assert map_value(rec, FM(source=["f", "l"], target="n", transform="concat")) == "Alice Johnson"
    vm = FM(
        source="status",
        target="a",
        transform="value_map",
        value_map={"active": True, "inactive": False},
    )
    assert map_value(rec, vm) is True
    with pytest.raises(ValueError):
        map_value({"status": "banned"}, vm)
    assert apply_transform(1686787200, "epoch_to_iso") == "2023-06-15T00:00:00Z"
    with pytest.raises(ValueError):
        apply_transform(1686787200000, "epoch_to_iso")  # milliseconds, not seconds
