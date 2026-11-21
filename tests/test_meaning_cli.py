"""Meaning drift in the CLI: check, review, profile, and the examples/meaning demo."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fixtures.payloads import V1
from typer.testing import CliRunner

import aegis_healer.cli.check_cmd as check_mod
from aegis_healer.cli import app
from aegis_healer.healer import Healer
from aegis_healer.store.files import FileStore

runner = CliRunner(env={"COLUMNS": "200"})
DEMO = Path(__file__).resolve().parent.parent / "examples" / "meaning"
MODEL = "models:Payment"
KEY = "models.Payment"


def run(*args, input=None):
    return runner.invoke(app, list(args), input=input)


def demo_json(name):
    return json.loads((DEMO / name).read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """The real CLI against the demo folder; only the HTTP fetch is faked."""
    monkeypatch.chdir(DEMO)
    monkeypatch.setattr(sys, "path", list(sys.path))  # load_model edits it
    monkeypatch.setenv("AEGIS_DIR", str(tmp_path / ".aegis"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AEGIS_WEBHOOK_URL", raising=False)
    state = {"payload": demo_json("payments_v1.json")}
    monkeypatch.setattr(check_mod, "fetch_json", lambda url, headers: state["payload"])
    yield FileStore(tmp_path / ".aegis"), state
    sys.modules.pop("models", None)


def check(**_):
    return run("check", "http://x", "--model", MODEL)


def build_baseline(state, runs=2):
    state["payload"] = demo_json("payments_v1.json")
    for _ in range(runs):
        r = check()
        assert r.exit_code == 0, r.output


def make_scale_drift(state):
    build_baseline(state)
    state["payload"] = demo_json("payments_v2.json")
    r = check()
    assert r.exit_code == 1, r.output
    return r


def make_timezone_drift(state):
    build_baseline(state)
    state["payload"] = [
        {**rec, "paid_at": rec["paid_at"][:-1] + "+02:00"} for rec in demo_json("payments_v1.json")
    ]
    r = check()
    assert r.exit_code == 1, r.output
    return r


# --- demo ---------------------------------------------------------------------------------


def test_demo_files_validate_against_demo_models():
    name = "aegis_demo_meaning_models"
    spec = importlib.util.spec_from_file_location(name, DEMO / "models.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        v1, v2 = demo_json("payments_v1.json"), demo_json("payments_v2.json")
        assert len(v1) == len(v2) == 25
        for a, b in zip(v1, v2, strict=True):
            module.Payment.model_validate(a)
            module.Payment.model_validate(b)
            assert 12.5 <= a["amount"] <= 480.0
            assert b["amount"] == round(a["amount"] * 100)
            assert a["currency"] == "usd" and a["paid_at"].endswith("Z")
    finally:
        sys.modules.pop(name, None)


def test_scale_fix_reproduces_v1_amounts_exactly():
    from aegis_healer.mapping import map_value
    from aegis_healer.types import FieldMapping

    m = FieldMapping(source="amount", target="amount", scale=0.01)
    v1, v2 = demo_json("payments_v1.json"), demo_json("payments_v2.json")
    for a, b in zip(v1, v2, strict=True):
        assert map_value(b, m) == a["amount"]


def test_demo_end_to_end(env):
    store, state = env
    build_baseline(state)  # v1 twice: ok
    state["payload"] = demo_json("payments_v2.json")
    r = check()
    assert r.exit_code == 1, r.output
    assert "Suggested fix" in r.output and "x0.01" in r.output and "scale" in r.output
    assert r.output.index("x0.01") < r.output.index("Pending")  # table before the status line
    assert "aegis review" in r.output
    assert "matcher" not in r.output and "1.00" not in r.output  # no mapping table

    r = run("review", "--approve", "1")
    assert r.exit_code == 0, r.output
    assert "aegis replay --model models:Payment" in r.output

    out = store.root.parent / "released.jsonl"
    r = run("replay", "--model", MODEL, "--out", str(out))
    assert r.exit_code == 0, r.output
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 25
    assert sorted(x["amount"] for x in lines) == sorted(
        x["amount"] for x in demo_json("payments_v1.json")
    )


# --- review -------------------------------------------------------------------------------


def test_review_interactive_apply_meaning_fix(env):
    store, state = env
    make_scale_drift(state)
    r = run("review", input="a\n")
    assert r.exit_code == 0, r.output
    for word in ("Field", "Kind", "Detail", "Suggested fix", "x0.01"):
        assert word in r.output
    assert "[a]pply fix" in r.output and "[k]eep data" in r.output
    assert "matcher" not in r.output and "1.00" not in r.output
    assert "Approved fix #1" in r.output
    assert "aegis replay --model models:Payment" in r.output
    assert store.get_pending(1).status == "approved"
    assert [m.scale for m in store.get_active(KEY)] == [0.01]


def test_review_interactive_keep(env):
    store, state = env
    make_scale_drift(state)
    r = run("review", input="k\n")
    assert r.exit_code == 0, r.output
    assert store.get_pending(1).status == "kept"


def test_apply_not_offered_without_suggestion(env):
    store, state = env
    make_timezone_drift(state)
    r = run("review", input="a\ns\n")
    assert r.exit_code == 0, r.output
    assert "[a]pply" not in r.output
    assert "[k]eep data" in r.output and "[r]eject" in r.output
    assert "Type k, r, s or d." in r.output  # "a" is invalid input here
    assert store.get_pending(1).status == "pending"
    assert "-" in r.output and "timezone" in r.output


def test_review_reject_meaning_fix(env):
    store, state = env
    make_scale_drift(state)
    r = run("review", input="r\n")
    assert r.exit_code == 0, r.output
    assert store.get_pending(1).status == "rejected"


def test_keep_flag_marks_kept_and_resets_baseline(env):
    store, state = env
    make_scale_drift(state)
    r = run("review", "--keep", "1")
    assert r.exit_code == 0, r.output
    assert store.get_pending(1).status == "kept"
    assert "aegis replay --model models:Payment" in r.output
    # Review Focus 4: the new-scale data is the new normal; no repeat alarm.
    for _ in range(3):  # 3 x 25 records, well past MIN_BASELINE
        r = check()
        assert r.exit_code == 0, r.output
        assert "x0.01" not in r.output and "Pending" not in r.output
    assert store.list_pending(KEY) == []


def test_keep_flag_errors_are_clean(env):
    store, state = env
    make_scale_drift(state)
    assert run("review", "--keep", "1").exit_code == 0
    again = run("review", "--keep", "1")
    assert again.exit_code == 1 and "already kept" in again.output
    assert "Traceback" not in again.output
    missing = run("review", "--keep", "99")
    assert missing.exit_code == 1 and "Traceback" not in missing.output


# --- check --------------------------------------------------------------------------------


@pytest.fixture
def fake_user(tmp_path, monkeypatch):
    seen = {}
    store = FileStore(tmp_path / ".aegis")
    monkeypatch.setattr(check_mod, "fetch_json", lambda url, headers: V1)

    def build(threshold, db, persist=True, **kw):
        seen.update(kw)
        return Healer(store=store, proposer=None, auto_apply_threshold=threshold, persist=persist)

    monkeypatch.setattr(check_mod, "build_healer", build)
    return seen


def test_no_meaning_flag_passes_false(fake_user):
    r = run("check", "http://x", "--model", "fixtures.models:User", "--no-meaning")
    assert r.exit_code == 0, r.output
    assert fake_user["meaning"] is False


def test_meaning_default_is_none(fake_user):
    r = run("check", "http://x", "--model", "fixtures.models:User")
    assert r.exit_code == 0, r.output
    assert fake_user["meaning"] is None


# --- profile ------------------------------------------------------------------------------


def test_profile_shows_aggregates_only(env):
    store, state = env
    build_baseline(state)
    r = run("profile", KEY)
    assert r.exit_code == 0, r.output
    for word in ("Field", "Records", "Kind", "Typical", "Timezone", "Categories"):
        assert word in r.output
    assert "amount" in r.output and "number" in r.output and "~10^2." in r.output
    assert "datetime" in r.output and "utc" in r.output
    v1 = demo_json("payments_v1.json")
    for rec in v1:
        assert rec["paid_at"] not in r.output
        assert str(rec["amount"]) not in r.output
    assert "2026-11" not in r.output and "usd" not in r.output


def test_profile_empty(env):
    r = run("profile", "models.Nothing")
    assert r.exit_code == 0, r.output
    assert "No profile for models.Nothing." in r.output
