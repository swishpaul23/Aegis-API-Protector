from fakes import m, v2_agent_patch
from fixtures.models import User
from fixtures.payloads import V2

from aegis_healer.types import MappingPatch
from aegis_healer.validate import validate_patch


def full_v2():
    p = v2_agent_patch()
    p.mappings += [m("userName", "user_name"), m("contact.email", "email")]
    return p


def test_full_v2_patch_ok():
    check = validate_patch(full_v2(), [V2], User)
    assert check.ok, check.errors


def test_unknown_source_rejected():
    p = full_v2()
    p.mappings.append(m("nope", "user_name"))
    check = validate_patch(p, [V2], User)
    assert not check.ok and any("nope" in e for e in check.errors)


def test_unknown_target_rejected():
    assert not validate_patch(MappingPatch(mappings=[m("id", "password")]), [V2], User).ok


def test_duplicate_target_rejected():
    p = full_v2()
    p.mappings.append(m("userName", "email"))
    assert not validate_patch(p, [V2], User).ok


def test_any_sample_failure_rejects():
    assert not validate_patch(full_v2(), [V2, {**V2, "id": "x"}], User).ok
