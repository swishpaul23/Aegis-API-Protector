import json
import logging
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel, Field

from aegis_healer.meaning import profile as prof
from aegis_healer.meaning.profile import (
    FieldProfile,
    ModelProfile,
    classify,
    load_profile,
    median_bucket,
    observe,
    reset_fields,
    save_profile,
    tz_class,
)


class Inner(BaseModel):
    when: str | None = None


class M(BaseModel):
    amount: float | None = None
    status: str | None = None
    note: str | None = None
    email: str | None = None
    ts: str | None = None
    inner: Inner = Inner()
    renamed: int = Field(default=0, alias="ren")


def mp(key="app.M") -> ModelProfile:
    return ModelProfile(model_key=key)


@pytest.mark.parametrize(
    "value,kind",
    [
        (5, "number"),
        (5.5, "number"),
        (0, "number"),
        (float("nan"), "skip"),
        (float("inf"), "skip"),
        (True, "skip"),
        (None, "skip"),
        ("2026-01-02T03:04:05Z", "datetime"),
        ("2026-01-02 03:04:05", "datetime"),
        (datetime(2026, 1, 2, 3, 4, 5), "datetime"),
        ("2026-01-02", "skip"),
        ("active", "category"),
        ("ACTIVE", "category"),
        ("Alice Smith", "text"),
        ("a3f1-9c", "text"),
        ("Active", "text"),
        ("x" * 21, "text"),
        ({"a": 1}, "skip"),
        ([1], "skip"),
    ],
)
def test_classify_kinds(value, kind):
    assert classify(value)[0] == kind


def test_tz_classes():
    assert tz_class("2026-01-02T03:04:05Z") == "utc"
    assert tz_class("2026-01-02T03:04:05+00:00") == "utc"
    assert tz_class("2026-01-02T03:04:05-07:00") == "offset"
    assert tz_class("2026-01-02T03:04:05") == "naive"
    assert tz_class(datetime(2026, 1, 2, tzinfo=UTC)) == "utc"


def test_buckets_decimals_zeros():
    p = mp()
    observe(p, [{"amount": 100}, {"amount": 100.25}, {"amount": 0}, {"amount": 1e-7}], M)
    f = p.fields["amount"]
    assert f.n == 4
    assert f.zeros == 1
    assert f.numeric[8] == 2  # round(log10(100) * 4)
    assert f.decimals == {0: 2, 2: 1, 4: 1}  # 1e-7 -> exponent repr -> "4 or more"


def test_n_counts_only_usable_values():
    p = mp()
    observe(p, [{"amount": None}, {"amount": True}, {"amount": float("nan")}, {}, {"amount": 5}], M)
    assert p.fields["amount"].n == 1


def test_tz_counts_and_nested_alias_paths():
    p = mp()
    observe(
        p,
        [
            {"inner": {"when": "2026-01-02T03:04:05Z"}, "ren": 7},
            {"inner": {"when": "2026-01-02T03:04:05"}},
        ],
        M,
    )
    assert p.fields["inner.when"].tz == {"utc": 1, "naive": 1}
    assert "ren" in p.fields


def test_categories_and_free_text():
    p = mp()
    observe(p, [{"status": "active"}, {"status": "active"}, {"status": "closed"}], M)
    assert p.fields["status"].categories == {"active": 2, "closed": 1}
    observe(p, [{"status": "Alice Smith"}], M)
    f = p.fields["status"]
    assert f.free_text and f.categories == {} and f.n == 4
    observe(p, [{"status": "later"}], M)
    assert p.fields["status"].categories == {}


def test_category_cap_sets_free_text():
    p = mp()
    observe(p, [{"status": chr(97 + i // 26) + chr(97 + i % 26)} for i in range(31)], M)
    f = p.fields["status"]
    assert f.free_text and f.categories == {}


def test_decay_halves_all_counts(monkeypatch):
    monkeypatch.setattr(prof, "DECAY_AT", 10)
    p = mp()
    observe(p, [{"amount": 100.5, "status": "x"} for _ in range(10)], M)
    assert p.fields["amount"].n == 10  # not above the limit yet
    observe(p, [{"amount": 100.5} for _ in range(2)], M)
    f = p.fields["amount"]
    assert f.n == 6
    assert f.numeric == {8: 6} and f.decimals == {1: 6}
    assert p.fields["status"].n == 10  # untouched field is not decayed


def test_decay_halves_zeros_tz_categories(monkeypatch):
    monkeypatch.setattr(prof, "DECAY_AT", 4)
    p = mp()
    observe(p, [{"amount": 0, "ts": "2026-01-02T03:04:05Z", "status": "ok"}] * 6, M)
    assert p.fields["amount"].zeros == 3
    assert p.fields["ts"].tz == {"utc": 3}
    assert p.fields["status"].categories == {"ok": 3}


def test_median_bucket():
    assert median_bucket({}) is None
    assert median_bucket({8: 1, 12: 1}) == 8  # cumulative reaches half at 8
    assert median_bucket({4: 1, 8: 1, 12: 1}) == 8
    assert median_bucket({4: 1, 12: 10}) == 12


def test_save_load_round_trip(tmp_path):
    p = mp()
    observe(p, [{"amount": 100.5, "status": "active", "ts": "2026-01-02T03:04:05Z"}] * 3, M)
    save_profile(tmp_path, p)
    assert p.updated_at is not None and p.updated_at.tzinfo is not None
    files = list((tmp_path / "profiles").glob("*.json"))
    assert len(files) == 1
    loaded = load_profile(tmp_path, "app.M")
    assert loaded == p
    assert all(isinstance(k, int) for k in loaded.fields["amount"].numeric)
    save_profile(tmp_path, loaded)
    assert load_profile(tmp_path, "app.M").fields == p.fields


def test_load_missing_is_empty(tmp_path):
    p = load_profile(tmp_path, "app.M")
    assert p.model_key == "app.M" and p.fields == {}


def test_profile_file_has_no_values(tmp_path):
    p = mp()
    records = [
        {
            "amount": 123456.789,
            "email": "stuart@example.com",
            "note": "Alice Smith",
            "ts": "2031-05-06T07:08:09Z",
        },
        {
            "amount": 987654.321,
            "email": "bob.jones@corp.io",
            "note": "Bob Jones",
            "status": "active",
        },
    ]
    observe(p, records, M)
    save_profile(tmp_path, p)
    text = next((tmp_path / "profiles").glob("*.json")).read_text(encoding="utf-8")
    for secret in [
        "123456",
        "987654",
        "stuart",
        "example.com",
        "bob",
        "Alice",
        "Jones",
        "2031",
        "07:08",
    ]:
        assert secret not in text
    assert "active" in text
    assert p.fields["email"].categories == {} and p.fields["email"].free_text


def test_corrupt_file_warns_with_name_only(tmp_path, caplog):
    save_profile(tmp_path, mp())
    path = next((tmp_path / "profiles").glob("*.json"))
    path.write_text('{"secret-payload": nope', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="aegis_healer"):
        p = load_profile(tmp_path, "app.M")
    assert p.fields == {} and p.model_key == "app.M"
    assert path.name in caplog.text
    assert "secret-payload" not in caplog.text


def test_wrong_model_key_in_file_is_empty(tmp_path):
    save_profile(tmp_path, mp("app.M"))
    path = next((tmp_path / "profiles").glob("*.json"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["model_key"] = "other"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_profile(tmp_path, "app.M").fields == {}


def test_reset_fields():
    p = mp()
    p.fields = {"a": FieldProfile(n=3), "b": FieldProfile(n=1)}
    reset_fields(p, ["a", "missing"])
    assert list(p.fields) == ["b"]
