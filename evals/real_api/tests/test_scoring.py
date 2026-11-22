import pytest
from pydantic import BaseModel

from aegis_healer import HealResult
from evals.real_api.cases import Case, ExpectedMap
from evals.real_api.scoring import classify, summarize


class Rec(BaseModel):
    name: str


def _case(expected="repair", api="a", kind="rename", cid="c") -> Case:
    maps = [ExpectedMap(target="name", source="n")] if expected == "repair" else []
    return Case(
        id=cid,
        api=api,
        source={},
        kind=kind,
        old_fields={"name": "string"},
        new_fields={"n": "string"},
        expected=expected,
        maps=maps,
    )


EXPECTED = [Rec(name="x")]


def test_healed_equal_is_repaired():
    r = HealResult(status="healed", records=[Rec(name="x")])
    assert classify(_case(), r, EXPECTED) == "repaired"


def test_healed_different_is_wrong_applied():
    r = HealResult(status="healed", records=[Rec(name="y")])
    assert classify(_case(), r, EXPECTED) == "wrong_applied"


def test_refuse_healed_is_wrong_applied():
    r = HealResult(status="healed", records=[Rec(name="x")])
    assert classify(_case("refuse"), r, []) == "wrong_applied"


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_refuse_pending_or_failed_is_correct_refusal(status):
    assert classify(_case("refuse"), HealResult(status=status), []) == "correct_refusal"


@pytest.mark.parametrize("status", ["pending", "failed"])
def test_repair_pending_or_failed_is_safe_deferral(status):
    assert classify(_case(), HealResult(status=status), EXPECTED) == "safe_deferral"


def test_none_is_crashed():
    assert classify(_case(), None, EXPECTED) == "crashed"


def test_summarize_rates():
    rows = [
        (_case(api="a", kind="rename", cid="1"), "repaired"),
        (_case(api="a", kind="move", cid="2"), "wrong_applied"),
        (_case("refuse", api="b", kind="unmappable", cid="3"), "correct_refusal"),
        (_case(api="b", kind="rename", cid="4"), "repaired"),
    ]
    s = summarize(rows)
    assert s["n"] == 4
    assert s["counts"]["repaired"] == 2
    assert s["repair_rate"] == 0.5
    assert s["wrong_applied_rate"] == 0.25
    assert set(s["by_api"]) == {"a", "b"}
    assert s["by_api"]["a"]["n"] == 2
    assert s["by_api"]["a"]["repair_rate"] == 0.5
    assert set(s["by_kind"]) == {"rename", "move", "unmappable"}
    assert s["by_kind"]["unmappable"]["counts"]["correct_refusal"] == 1


def test_summarize_empty():
    assert summarize([])["repair_rate"] == 0.0


def test_ok_on_repair_case_is_not_breaking():
    # Lax models coerce e.g. "1234" -> int, so Healer sees no drift: not a repair.
    r = HealResult(status="ok", records=[Rec(name="x")])
    assert classify(_case(), r, EXPECTED) == "not_breaking"


def test_ok_on_repair_case_with_wrong_values_is_wrong_applied():
    r = HealResult(status="ok", records=[Rec(name="y")])
    assert classify(_case(), r, EXPECTED) == "wrong_applied"


def test_ok_on_refuse_case_is_wrong_applied():
    r = HealResult(status="ok", records=[Rec(name="x")])
    assert classify(_case("refuse"), r, []) == "wrong_applied"


def test_not_breaking_is_excluded_from_n_but_counted():
    rows = [
        (_case(cid="1"), "repaired"),
        (_case(cid="2"), "not_breaking"),
        (_case(cid="3"), "safe_deferral"),
    ]
    s = summarize(rows)
    assert s["n"] == 2
    assert s["counts"]["not_breaking"] == 1
    assert s["repair_rate"] == 0.5
    assert s["by_api"]["a"]["n"] == 2 and s["by_api"]["a"]["counts"]["not_breaking"] == 1
