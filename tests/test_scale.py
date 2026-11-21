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
