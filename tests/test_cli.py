import httpx
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

    def build(threshold, db, persist=True, **kw):
        p = FakeProposer(state["patch"]) if state["patch"] else None
        return Healer(
            store=store, proposer=p, auto_apply_threshold=threshold, persist=persist, **kw
        )

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


@pytest.mark.parametrize(
    "payload,extra",
    [
        ("just a string", []),
        (None, []),
        ([1, 2], []),
        ({"data": {"x": 1}}, ["--records-path", "nope"]),
    ],
)
def test_bad_payloads_exit2_without_traceback(env, payload, extra):
    env[1].update(payload=payload)
    r = run("check", "http://x", "--model", MODEL, *extra)
    assert r.exit_code == 2, r.output
    assert "Traceback" not in r.output and "Could not read records" in r.output


def test_untrusted_text_is_escaped(env):
    from rich.console import Console

    from aegis_healer.cli.common import mapping_table, safe

    assert safe("[green]ok[/green]\x1b[8m") == "\\[green]ok\\[/green]?[8m"
    out = Console(record=True, width=200)
    out.print(mapping_table([m_("a", "b", reason="[link=https://evil]click[/link] \x1b]8;;x")]))
    text = out.export_text()
    assert "[link=https://evil]" in text and "\x1b" not in text


def m_(src, tgt, **kw):
    from aegis_healer.types import FieldMapping

    return FieldMapping(source=src, target=tgt, **kw)


def test_redirect_drops_headers_cross_origin_and_refuses_downgrade():
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("x-api-key")))
        if request.url.host == "api.test":
            return httpx.Response(302, headers={"location": "https://other.test/data"})
        return httpx.Response(200, json={"ok": True})

    out = fetch_json(
        "https://api.test/x", {"X-Api-Key": "s3cret"}, transport=httpx.MockTransport(handler)
    )
    assert out == {"ok": True}
    assert seen == [("api.test", "s3cret"), ("other.test", None)]

    def downgrade(request):
        return httpx.Response(302, headers={"location": "http://api.test/plain"})

    with pytest.raises(ValueError, match="https to http"):
        fetch_json("https://api.test/x", {}, transport=httpx.MockTransport(downgrade))


def test_interactive_review_continues_after_stale_fix(env):
    store, state = env
    state.update(payload=V2, patch=v2_agent_patch(0.8))
    run("check", "http://x", "--model", MODEL)
    first = store.get_pending(1)
    # An identical re-run is de-duplicated, so add a different fix on the same base version.
    other = first.patch.model_copy(update={"mappings": first.patch.mappings[:2]})
    store.add_pending(first.model_key, other, first.report)
    r = run("review", *_db(store), input="a\na\n")
    assert "stale" in r.output
    assert store.get_pending(1).status == "approved"
    assert store.get_pending(2).status == "pending"


def test_unknown_pending_id_message(env):
    store = env[0]
    r = run("review", "--approve", "99", *_db(store))
    assert r.exit_code == 1 and "no pending fix #99" in r.output and "'no pending" not in r.output


def test_query_string_secrets_not_printed(env, monkeypatch):
    def boom(url, headers):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(check_mod, "fetch_json", boom)
    r = run("check", "http://x/api?api_key=SECRET", "--model", MODEL)
    assert r.exit_code == 2 and "SECRET" not in r.output


def test_init_writes_into_new_folder_with_correct_hint(tmp_path, monkeypatch):
    import aegis_healer.cli.init_cmd as init_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(init_mod, "fetch_json", lambda url, headers: [V1])
    r = run("init", "http://x", "--name", "user-profile", "--out", "sub/m.py")
    assert r.exit_code == 0, r.output
    assert (tmp_path / "sub" / "m.py").exists()
    assert "--model sub.m:UserProfile" in r.output


def test_check_rejects_bad_privacy(env):
    r = run("check", "http://x", "--model", MODEL, "--privacy", "bogus")
    assert r.exit_code == 2
    assert "--privacy" in r.output


def test_check_privacy_passed_through(env, monkeypatch):
    seen = {}

    def build(threshold, db, persist=True, privacy="masked", **kw):
        seen["privacy"] = privacy
        return Healer(store=env[0], proposer=None)

    monkeypatch.setattr(check_mod, "build_healer", build)
    assert run("check", "http://x", "--model", MODEL, "--privacy", "schema").exit_code == 0
    assert seen["privacy"] == "schema"


def _status_error(url, headers):
    request = httpx.Request("GET", "http://x/api?api_key=SECRET123")
    response = httpx.Response(403, request=request)
    raise httpx.HTTPStatusError(
        "Client error '403 Forbidden' for url 'http://x/api?api_key=SECRET123'",
        request=request,
        response=response,
    )


def test_check_fetch_status_error_hides_query(env, monkeypatch):
    monkeypatch.setattr(check_mod, "fetch_json", _status_error)
    r = run("check", "http://x/api?api_key=SECRET123", "--model", MODEL)
    assert r.exit_code == 2
    assert "SECRET123" not in r.output and "HTTP 403" in r.output


def test_init_fetch_status_error_hides_query(tmp_path, monkeypatch):
    import aegis_healer.cli.init_cmd as init_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(init_mod, "fetch_json", _status_error)
    r = run("init", "http://x/api?api_key=SECRET123")
    assert r.exit_code == 2
    assert "SECRET123" not in r.output and "HTTP 403" in r.output


def test_fetch_other_error_shows_type_only(env, monkeypatch):
    def boom(url, headers):
        raise ValueError("bad thing at http://x/api?api_key=SECRET123")

    monkeypatch.setattr(check_mod, "fetch_json", boom)
    r = run("check", "http://x/api?api_key=SECRET123", "--model", MODEL)
    assert r.exit_code == 2 and "SECRET123" not in r.output and "ValueError" in r.output


def test_unmappable_reason_printed_once(env):
    from aegis_healer.types import MappingPatch, Unmappable

    env[1].update(
        payload=V2,
        patch=MappingPatch(unmappable=[Unmappable(target="user_id", reason="REASON-XYZ")]),
    )
    r = run("check", "http://x", "--model", MODEL)
    assert r.exit_code == 2, r.output
    assert r.output.count("REASON-XYZ") == 1
    assert "Cannot heal without changing your model" in r.output
