from datetime import UTC, datetime

import pytest

from evals.real_api.cases import Case, ExpectedMap
from evals.real_api.payloads import build_model, make_new_payload, make_old_records


def _case(old, new, maps, expected="repair", kind="rename") -> Case:
    return Case(
        id="t",
        api="x",
        source={},
        kind=kind,
        old_fields=old,
        new_fields=new,
        expected=expected,
        maps=maps,
    )


def test_build_model_nests_dotted_paths():
    model = build_model("M", {"address.city": "string"})
    assert model.model_validate({"address": {"city": "x"}}).address.city == "x"
    with pytest.raises(ValueError):
        model.model_validate({"address": {}})


def test_build_model_datetime():
    model = build_model("M", {"at": "datetime"})
    assert model.model_validate({"at": "2024-01-02T03:04:05+00:00"}).at.year == 2024


def test_rename_reshapes():
    case = _case(
        {"user_name": "string", "id": "integer"},
        {"userName": "string", "id": "integer"},
        [ExpectedMap(target="user_name", source="userName")],
    )
    old = make_old_records(case)
    new = make_new_payload(case, old)
    assert len(old) == len(new) == 5
    for o, n in zip(old, new, strict=True):
        assert n["userName"] == o["user_name"]
        assert "user_name" not in n
        assert n["id"] == o["id"]


def test_move_across_nesting():
    case = _case(
        {"email": "string"},
        {"contact.email": "string"},
        [ExpectedMap(target="email", source="contact.email")],
        kind="move",
    )
    old = make_old_records(case)
    new = make_new_payload(case, old)
    for o, n in zip(old, new, strict=True):
        assert n == {"contact": {"email": o["email"]}}


def test_convert_epoch_iso_roundtrip():
    case = _case(
        {"created": "datetime"},
        {"created_at": "integer"},
        [ExpectedMap(target="created", source="created_at", convert="epoch_iso")],
        kind="retype",
    )
    old = make_old_records(case)
    new = make_new_payload(case, old)
    build_model("Old", case.old_fields).model_validate(old[0])
    for o, n in zip(old, new, strict=True):
        assert isinstance(n["created_at"], int)
        back = datetime.fromtimestamp(n["created_at"], UTC)
        assert back == datetime.fromisoformat(o["created"])


def test_convert_int_str_and_int_float():
    case = _case(
        {"a": "integer", "b": "integer"},
        {"a": "string", "b": "number"},
        [
            ExpectedMap(target="a", source="a", convert="int_str"),
            ExpectedMap(target="b", source="b", convert="int_float"),
        ],
        kind="retype",
    )
    old = make_old_records(case)
    new = make_new_payload(case, old)
    for o, n in zip(old, new, strict=True):
        assert n["a"] == str(o["a"])
        assert n["b"] == float(o["b"]) and isinstance(n["b"], float)


def test_values_distinct_and_seeded():
    case = _case(
        {"a": "string", "b": "string", "n.c": "integer", "d": "integer"},
        {},
        [],
        expected="refuse",
        kind="unmappable",
    )
    recs = make_old_records(case, seed=3)
    for r in recs:
        vals = [r["a"], r["b"], r["n"]["c"], r["d"]]
        assert len(set(vals)) == 4
    assert recs == make_old_records(case, seed=3)
    assert recs != make_old_records(case, seed=4)
    assert len({r["a"] for r in recs}) == len(recs)


def test_refuse_case_drops_target():
    case = _case(
        {"id": "integer", "legacy": "string"},
        {"id": "integer"},
        [],
        expected="refuse",
        kind="unmappable",
    )
    old = make_old_records(case)
    new = make_new_payload(case, old)
    for o, n in zip(old, new, strict=True):
        assert n == {"id": o["id"]}


def test_unknown_convert_combo_raises():
    case = _case(
        {"a": "string"},
        {"a": "integer"},
        [ExpectedMap(target="a", source="a", convert="int_str")],
        kind="retype",
    )
    with pytest.raises(ValueError):
        make_new_payload(case, make_old_records(case))
