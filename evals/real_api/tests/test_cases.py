import pytest
from pydantic import ValidationError

from evals.real_api.cases import Case, ExpectedMap, load_cases, save_case, split_of


def _case(**kw) -> Case:
    base = dict(
        id="demo-1",
        api="demo",
        source={"repo": "r", "old_ref": "a", "new_ref": "b", "spec_path": "s", "schema": "S"},
        kind="rename",
        old_fields={"a": "string"},
        new_fields={"b": "string"},
        expected="repair",
        maps=[ExpectedMap(target="a", source="b")],
    )
    base.update(kw)
    return Case(**base)


def test_split_is_stable_and_roughly_30_70():
    assert split_of("x-1") == split_of("x-1")
    splits = [split_of(f"case-{i}") for i in range(1000)]
    assert set(splits) == {"dev", "test"}
    assert 0.25 <= splits.count("dev") / 1000 <= 0.35


def test_case_roundtrip(tmp_path):
    case = _case()
    path = save_case(case, tmp_path)
    assert path.name == "demo-1.json"
    assert load_cases(tmp_path) == [case]


def test_refuse_case_must_have_no_maps():
    with pytest.raises(ValidationError):
        _case(expected="refuse", kind="unmappable")
    assert _case(expected="refuse", kind="unmappable", maps=[]).maps == []


def test_unambiguous_repair_requires_maps():
    with pytest.raises(ValidationError, match="non-empty maps"):
        _case(maps=[])
    assert _case(maps=[], ambiguous=True).maps == []  # drafted ambiguous cases may be empty


def test_unmappable_requires_refuse():
    with pytest.raises(ValidationError, match="unmappable"):
        _case(kind="unmappable")
