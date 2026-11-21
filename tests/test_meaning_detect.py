import random

import pytest
from pydantic import BaseModel

from aegis_healer.meaning.detect import MIN_BASELINE, MIN_BATCH, detect
from aegis_healer.meaning.profile import ModelProfile, observe


class M(BaseModel):
    amount: float | None = None
    status: str | None = None
    note: str | None = None
    ts: str | None = None


def rng_amounts(n, lo=10, hi=500, seed=1):
    r = random.Random(seed)
    return [round(r.uniform(lo, hi), 2) for _ in range(n)]


def base(field, values, model=M) -> ModelProfile:
    p = ModelProfile(model_key="app.M")
    observe(p, [{field: v} for v in values], model)
    return p


def run(p, field, values):
    return detect(p, [{field: v} for v in values], M)


def amounts_profile(n=60):
    return base("amount", rng_amounts(n))


@pytest.mark.parametrize(
    "factor,expected",
    [(100, 0.01), (1000, 0.001), (0.01, 100.0), (0.001, 1000.0)],
)
def test_scale_snapped(factor, expected):
    p = amounts_profile()
    batch = [v * factor for v in rng_amounts(10, seed=2)]
    (f,) = run(p, "amount", batch)
    assert f.kind == "scale" and f.path == "amount"
    assert f.suggested_scale == expected
    assert f.baseline_n == 60 and f.batch_n == 10


def test_scale_cents_to_dollars_reverse():
    p = base("amount", rng_amounts(60, lo=5000, hi=100000))
    (f,) = run(p, "amount", rng_amounts(10, seed=2))
    assert f.suggested_scale == 100.0
    assert f.detail == "typical value is ~100x smaller than the baseline"


def test_scale_detail_value_free():
    p = amounts_profile()
    batch = [v * 100 for v in rng_amounts(10, seed=2)]
    (f,) = run(p, "amount", batch)
    assert f.detail == "typical value is ~100x larger than the baseline"
    for v in batch:
        assert str(v) not in f.detail
    (g,) = run(p, "amount", [v / 1000 for v in rng_amounts(10, seed=3)])
    assert g.detail == "typical value is ~1000x smaller than the baseline"


def test_scale_epoch_seconds_to_ms():
    r = random.Random(5)
    secs = [r.randint(1_600_000_000, 1_700_000_000) for _ in range(40)]
    p = base("amount", secs)
    (f,) = run(p, "amount", [s * 1000 for s in secs[:10]])
    assert f.suggested_scale == 0.001


def test_scale_percent_fraction_to_percent():
    r = random.Random(6)
    p = base("amount", [round(r.uniform(0.01, 0.2), 3) for _ in range(40)])
    (f,) = run(p, "amount", [round(r.uniform(1, 20), 2) for _ in range(10)])
    assert f.suggested_scale == 0.01


def test_scale_not_snapped_has_no_fix():
    p = amounts_profile()
    (f,) = run(p, "amount", [v * 10**2.24 for v in rng_amounts(10, seed=2)])
    assert f.kind == "scale"
    assert f.suggested_scale is None
    assert f.detail.startswith("typical value is ~10^") and "larger than the baseline" in f.detail


def test_times_ten_no_finding():
    assert run(amounts_profile(), "amount", [v * 10 for v in rng_amounts(10, seed=2)]) == []


def test_normal_variation_within_decade_no_finding():
    batch = rng_amounts(30, lo=5, hi=900, seed=9)
    assert run(amounts_profile(), "amount", batch) == []


def test_baseline_under_min_baseline():
    p = base("amount", rng_amounts(MIN_BASELINE - 1))
    assert run(p, "amount", [v * 100 for v in rng_amounts(10, seed=2)]) == []


def test_batch_under_min_batch():
    batch = [v * 100 for v in rng_amounts(MIN_BATCH - 1, seed=2)]
    assert run(amounts_profile(), "amount", batch) == []


def test_zeros_ignored_and_missing_field_ok():
    p = amounts_profile()
    assert run(p, "amount", [0] * 10) == []
    assert detect(p, [{"other": 1}] * 10, M) == []


def test_detect_does_not_mutate():
    p = amounts_profile()
    before = p.model_dump_json()
    run(p, "amount", [v * 100 for v in rng_amounts(10, seed=2)])
    assert p.model_dump_json() == before


UTC_TS = [f"2026-01-{d:02d}T10:00:00Z" for d in range(1, 29)]


def test_tz_utc_to_offset():
    p = base("ts", UTC_TS)
    (f,) = run(p, "ts", [f"2026-02-{d:02d}T10:00:00-08:00" for d in range(1, 11)])
    assert f.kind == "timezone" and f.suggested_scale is None
    assert f.detail == "timestamps changed from UTC to a local offset"


def test_tz_utc_to_naive():
    p = base("ts", UTC_TS)
    (f,) = run(p, "ts", [f"2026-02-{d:02d}T10:00:00" for d in range(1, 11)])
    assert f.detail == "timestamps changed from UTC to no timezone"


def test_tz_mixed_baseline_no_finding():
    vals = UTC_TS[:17] + [f"2026-01-{d:02d}T10:00:00-08:00" for d in range(1, 12)]
    p = base("ts", vals)
    assert run(p, "ts", [f"2026-02-{d:02d}T10:00:00" for d in range(1, 11)]) == []


def test_tz_unchanged_no_finding():
    p = base("ts", UTC_TS)
    assert run(p, "ts", [f"2026-02-{d:02d}T10:00:00Z" for d in range(1, 11)]) == []


def cat_profile():
    return base("status", ["active", "inactive"] * 15)


def test_category_new_values():
    (f,) = run(cat_profile(), "status", ["A", "I", "A", "I", "A"])
    assert f.kind == "category"
    assert f.new_categories == ["A", "I"]
    assert f.detail == "5 of 5 values are new categories"
    assert f.suggested_scale is None


def test_category_one_new_among_many_known():
    batch = ["active"] * 9 + ["pending"]
    assert run(cat_profile(), "status", batch) == []


def test_category_free_text_baseline_never_flags():
    p = base("note", ["hello world, this is text"] * 30)
    assert p.fields["note"].free_text
    assert run(p, "note", ["foo", "bar", "baz", "qux", "quux"]) == []


def test_category_free_text_batch_counts_but_not_listed():
    batch = ["active", "Some free text", "More text here", "x y z", "active"]
    (f,) = run(cat_profile(), "status", batch)
    assert f.new_categories == []
    assert f.detail == "3 of 5 values are new categories"


def test_category_new_list_capped_and_sorted():
    batch = [f"v{chr(97 + i)}" for i in range(12)]
    (f,) = run(cat_profile(), "status", batch)
    assert f.new_categories == sorted(batch)[:10]


def test_findings_sorted_by_path_and_kind():
    p = ModelProfile(model_key="app.M")
    observe(
        p,
        [
            {"amount": a, "status": s}
            for a, s in zip(rng_amounts(30), ["active", "inactive"] * 15, strict=True)
        ],
        M,
    )
    recs = [
        {"amount": a * 100, "status": s}
        for a, s in zip(rng_amounts(10, seed=2), "ABCDEABCDE", strict=True)
    ]
    out = detect(p, recs, M)
    assert [(f.path, f.kind) for f in out] == [("amount", "scale"), ("status", "category")]


@pytest.mark.parametrize("seed", [11, 12, 13, 14, 15])
@pytest.mark.parametrize("k", [2, 3, -2, -3])
def test_scale_symmetry_across_seeds(seed, k):
    p = base("amount", rng_amounts(60, seed=seed))
    batch = [v * 10.0**k for v in rng_amounts(10, seed=seed + 100)]
    (f,) = run(p, "amount", batch)
    assert f.kind == "scale"
    assert f.suggested_scale == 10.0**-k
