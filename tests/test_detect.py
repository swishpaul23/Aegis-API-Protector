from fixtures.models import InternalUserProfile, User
from fixtures.payloads import PHASE1_V2, V1, V2

from aegis_healer.detect import detect_drift


def test_clean_v1():
    assert detect_drift(V1, User).is_clean


def test_v2_report():
    r = detect_drift(V2, User)
    assert set(r.missing) == {"user_id", "user_name", "email", "signup_date", "address.postal_code"}
    assert set(r.unexpected) == {"id", "userName", "contact.email", "registered", "address.zip"}


def test_type_mismatch_same_path():
    r = detect_drift({**V1, "user_id": "abc"}, User)
    assert r.type_mismatches[0].path == "user_id"
    assert r.missing == []


def test_missing_nested_model_expands_to_leaves():
    r = detect_drift(PHASE1_V2, InternalUserProfile)
    assert {"address.street", "address.city", "address.state", "address.zip_code"} <= set(r.missing)
