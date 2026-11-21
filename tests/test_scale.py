import pytest
from pydantic import ValidationError

from aegis_healer.healer import _mapping_sig
from aegis_healer.mapping import map_value
from aegis_healer.types import FieldMapping as FM


def test_scale_after_transform():
    m = FM(source="a", target="b", transform="to_int", scale=0.01)
    assert map_value({"a": "1234"}, m) == pytest.approx(12.34)


def test_scale_int_stays_int():
    out = map_value({"a": 1500}, FM(source="a", target="b", scale=0.01))
    assert out == 15 and type(out) is int


@pytest.mark.parametrize("bad", ["12", True])
def test_scale_rejects_non_number(bad):
    with pytest.raises(ValueError, match="scale needs a number"):
        map_value({"a": bad}, FM(source="a", target="b", scale=2))


@pytest.mark.parametrize("bad", [0, float("inf"), float("nan")])
def test_scale_must_be_finite_nonzero(bad):
    with pytest.raises(ValidationError):
        FM(source="a", target="b", scale=bad)


def test_mapping_sig_includes_scale():
    a = [FM(source="a", target="b", scale=0.01)]
    b = [FM(source="a", target="b", scale=100)]
    c = [FM(source="a", target="b")]
    assert len({_mapping_sig(a), _mapping_sig(b), _mapping_sig(c)}) == 3


def test_scale_constructs_fractional():
    assert FM(source="a", target="b", scale=0.5).scale == 0.5


def test_scale_float_input_stays_float():
    out = map_value({"a": 3.0}, FM(source="a", target="b", scale=2))
    assert out == 6.0 and type(out) is float


def test_scale_rejects_non_finite_result():
    with pytest.raises(ValueError, match="scaled value is not finite"):
        map_value({"a": 1e300}, FM(source="a", target="b", scale=1e300))


def test_describe_shows_scale():
    from aegis_healer.cli.common import describe, mapping_table

    assert describe(FM(source="a", target="b", scale=0.01)) == "x0.01"
    assert describe(FM(source="a", target="b", transform="to_int", scale=0.01)) == "to_int x0.01"
    vm = FM(source="a", target="b", transform="value_map", value_map={"x": 1}, scale=100)
    assert describe(vm) == 'value_map {"x": 1} x100'
    cc = FM(source=["a", "c"], target="b", transform="concat", scale=2)
    assert describe(cc) == "concat (sep ' ') x2"
    assert describe(FM(source="a", target="b")) == "-"
    cells = mapping_table([FM(source="a", target="b", transform="to_int", scale=0.01)]).columns[2]
    assert list(cells.cells) == ["to_int x0.01"]


def test_agent_scale_is_out_of_scope():
    from aegis_healer.agent import ProposalRequest
    from aegis_healer.agent.graph import scope_errors
    from aegis_healer.types import MappingPatch

    req = ProposalRequest(
        model_name="User",
        model_schema={"price": "int"},
        unresolved=["price"],
        candidates={"cost": "int"},
        samples=[{"cost": 1}],
    )
    ok = MappingPatch(mappings=[FM(source="cost", target="price")])
    bad = MappingPatch(mappings=[FM(source="cost", target="price", scale=100)])
    assert scope_errors(req, ok) == []
    assert "scale is not allowed in agent proposals" in scope_errors(req, bad)


def test_scale_huge_int_overflow_is_value_error():
    with pytest.raises(ValueError, match="scaled value is not finite"):
        map_value({"a": 10**400}, FM(source="a", target="b", scale=0.01))


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_scale_rejects_non_finite_input(bad):
    with pytest.raises(ValueError, match="value is not finite"):
        map_value({"a": bad}, FM(source="a", target="b", scale=2))


def test_describe_scale_not_truncated():
    from aegis_healer.cli.common import describe

    assert describe(FM(source="a", target="b", scale=0.0123456789)) == "x0.0123456789"


def test_scale_has_no_float_noise():
    assert map_value({"a": 3480}, FM(source="a", target="b", scale=0.01)) == 34.8
    assert map_value({"a": 1250}, FM(source="a", target="b", scale=0.01)) == 12.5
    assert map_value({"a": 5}, FM(source="a", target="b", scale=100)) == 500
    assert type(map_value({"a": 5}, FM(source="a", target="b", scale=100))) is int


def test_scale_huge_int_is_refused():
    with pytest.raises(ValueError):
        map_value({"a": 10**400}, FM(source="a", target="b", scale=0.01))
