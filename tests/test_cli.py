import pytest
from fakes import FakeProposer, v2_agent_patch
from fixtures.payloads import V1, V2
from typer.testing import CliRunner

import aegis_healer.cli.check_cmd as check_mod
from aegis_healer.cli import app
from aegis_healer.cli.common import fetch_json
from aegis_healer.healer import Healer
from aegis_healer.store.files import FileStore

runner = CliRunner(env={"COLUMNS": "200"})
MODEL = "fixtures.models:User"


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = FileStore(tmp_path / ".aegis")
    state = {"payload": V1, "patch": None}
    monkeypatch.setattr(check_mod, "fetch_json", lambda url, headers: state["payload"])

    def build(threshold, db, persist=True):
        p = FakeProposer(state["patch"]) if state["patch"] else None
        return Healer(store=store, proposer=p, auto_apply_threshold=threshold, persist=persist)

    monkeypatch.setattr(check_mod, "build_healer", build)
    return store, state


def run(*args, input=None):
    return runner.invoke(app, list(args), input=input)


def test_check_ok_exit0(env):
    r = run("check", "http://x", "--model", MODEL)
    assert r.exit_code == 0, r.output
    assert "No drift" in r.output


def test_check_healed_exit0(env):
    env[1].update(payload=V2, patch=v2_agent_patch(0.95))
    r = run("check", "http://x", "--model", MODEL)
    assert r.exit_code == 0, r.output
    assert "Healed" in r.output


def test_check_pending_exit1(env):
    env[1].update(payload=V2, patch=v2_agent_patch(0.8))
    r = run("check", "http://x", "--model", MODEL)
    assert r.exit_code == 1, r.output
    assert "aegis review" in r.output


def test_check_failed_exit2(env):
    env[1].update(payload=V2)
    r = run("check", "http://x", "--model", MODEL)
    assert r.exit_code == 2, r.output


def test_dry_run_saves_nothing(env):
    store, state = env
    state.update(payload=V2, patch=v2_agent_patch(0.95))
    assert run("check", "http://x", "--model", MODEL, "--dry-run").exit_code == 0
    assert store.active_version("fixtures.models.User") == 0


def test_rejects_file_scheme():
    with pytest.raises(ValueError):
        fetch_json("file:///etc/passwd", {})


def test_header_redacted(env):
    r = run("check", "http://x", "--model", MODEL, "-H", "Authorization: Bearer secret")
    assert "secret" not in r.output


def test_bad_model_spec(env):
    r = run("check", "http://x", "--model", "os:path")
    assert r.exit_code != 0


def _pending(env):
    env[1].update(payload=V2, patch=v2_agent_patch(0.8))
    run("check", "http://x", "--model", MODEL)
    return env[0]


def _db(store):
    return ["--db", str(store.root)]


def test_review_approve_flag(env):
    store = _pending(env)
    r = run("review", "--approve", "1", *_db(store))
    assert r.exit_code == 0, r.output
    assert store.active_version("fixtures.models.User") == 1


def test_review_reject_flag(env):
    store = _pending(env)
    assert run("review", "--reject", "1", *_db(store)).exit_code == 0
    assert store.get_pending(1).status == "rejected"


def test_review_interactive_skip_then_approve(env):
    store = _pending(env)
    run("review", *_db(store), input="s\n")
    assert store.get_pending(1).status == "pending"
    r = run("review", *_db(store), input="d\na\n")
    assert "Drift report" in r.output
    assert store.get_pending(1).status == "approved"


def test_review_stale_message(env):
    store = _pending(env)
    store.save_active("fixtures.models.User", [], "someone else")
    r = run("review", "--approve", "1", *_db(store))
    assert r.exit_code == 1 and "stale; re-run check" in r.output


def test_history_and_rollback(env):
    store = _pending(env)
    run("review", "--approve", "1", *_db(store))
    assert "approved fix #1" in run("history", "fixtures.models.User", *_db(store)).output
    r = run("rollback", "fixtures.models.User", "1", *_db(store))
    assert r.exit_code == 0 and store.active_version("fixtures.models.User") == 2
