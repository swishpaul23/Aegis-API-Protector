import json
import logging

import httpx
import pytest
from fakes import FakeProposer, v2_agent_patch
from fixtures.models import User
from fixtures.payloads import V1, V2
from typer.testing import CliRunner

from aegis_healer import Healer
from aegis_healer.cli import app
from aegis_healer.cli import check_cmd as check_mod
from aegis_healer.notify import PendingNotice, webhook_notifier
from aegis_healer.store.files import FileStore

URL = "https://hooks.example.com/services/SECRET-TOKEN"
KEY = "fixtures.models.User"


def pending_healer(tmp_path, on_pending):
    store = FileStore(tmp_path / ".aegis")
    return Healer(
        store=store,
        proposer=FakeProposer(v2_agent_patch(0.8)),
        on_pending=on_pending,
    )


def notice(**kw):
    base = dict(
        model_key=KEY,
        pending_id=3,
        fields=["a", "b"],
        min_confidence=0.8,
        quarantined=1,
        message="2 field fix(es) for X need review",
    )
    return PendingNotice(**{**base, **kw})


def test_on_pending_called_once_with_fields(tmp_path):
    seen = []
    result = pending_healer(tmp_path, seen.append).validate([V1, V2], User)
    assert result.status == "pending"
    assert len(seen) == 1
    n = seen[0]
    assert n.model_key == KEY
    assert n.pending_id == result.pending_id
    assert n.fields == sorted(m.target for m in result.proposed.mappings)
    assert {"address.postal_code", "signup_date", "user_id"} <= set(n.fields)
    assert n.min_confidence == 0.8
    assert n.quarantined == 1


def test_on_pending_not_called_when_healed_or_ok(tmp_path):
    seen = []
    store = FileStore(tmp_path / ".aegis")
    healer = Healer(
        store=store, proposer=FakeProposer(v2_agent_patch(0.95)), on_pending=seen.append
    )
    assert healer.validate([V1], User).status == "ok"
    assert healer.validate([V1, V2], User).status == "healed"
    assert seen == []


def test_callback_exception_does_not_change_status(tmp_path, caplog):
    def boom(_):
        raise RuntimeError("secret payload text")

    with caplog.at_level(logging.WARNING, logger="aegis_healer"):
        result = pending_healer(tmp_path, boom).validate([V1, V2], User)
    assert result.status == "pending"
    assert result.pending_id is not None
    assert "RuntimeError" in caplog.text
    assert "secret payload text" not in caplog.text


def test_webhook_posts_text_and_payload():
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    send = webhook_notifier(URL, transport=httpx.MockTransport(handler))
    send(notice())
    assert len(captured) == 1
    assert captured[0].method == "POST"
    body = json.loads(captured[0].content)
    assert body["aegis"]["pending_id"] == 3
    assert body["text"] == (
        f"Aegis: 2 field fix(es) need review for {KEY} (pending #3, 1 record(s) quarantined). "
        "Run: aegis review"
    )


def test_webhook_text_without_pending_id():
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(204)

    webhook_notifier(URL, transport=httpx.MockTransport(handler))(notice(pending_id=None))
    assert "(pending, 1 record(s) quarantined)" in captured[0]["text"]


def test_webhook_500_and_timeout_swallowed(caplog):
    def h500(request):
        return httpx.Response(500, text="body-with-secret")

    def htimeout(request):
        raise httpx.ConnectTimeout("boom " + str(request.url))

    with caplog.at_level(logging.WARNING, logger="aegis_healer"):
        webhook_notifier(URL, transport=httpx.MockTransport(h500))(notice())
        webhook_notifier(URL, transport=httpx.MockTransport(htimeout))(notice())
    assert "500" in caplog.text
    assert "ConnectTimeout" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text
    assert "body-with-secret" not in caplog.text
    assert "hooks.example.com" not in caplog.text


def test_webhook_does_not_follow_redirects(caplog):
    hits = []

    def handler(request):
        hits.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    with caplog.at_level(logging.WARNING, logger="aegis_healer"):
        webhook_notifier(URL, transport=httpx.MockTransport(handler))(notice())
    assert len(hits) == 1
    assert "302" in caplog.text


def test_webhook_rejects_plain_http_remote():
    with pytest.raises(ValueError) as e:
        webhook_notifier("http://example.com/x")
    assert "example.com" not in str(e.value)


@pytest.mark.parametrize(
    "url", ["http://localhost:8080/x", "http://127.0.0.1/x", "https://a.example/x"]
)
def test_webhook_accepts_allowed_urls(url):
    webhook_notifier(url)


@pytest.mark.parametrize("url", ["ftp://a.example/x", "not a url", "https://", ""])
def test_webhook_rejects_bad_urls(url):
    with pytest.raises(ValueError):
        webhook_notifier(url)


def test_no_values_in_notice(tmp_path):
    seen = []
    pending_healer(tmp_path, seen.append).validate([V1, V2], User)
    dumped = json.dumps(seen[0].model_dump(mode="json"))
    for value in ("ana", "a@x.io", "V5A 1S6", "Vancouver", "2026-01-02"):
        assert value not in dumped


def test_cli_wires_webhook_from_env(monkeypatch, tmp_path):
    store = FileStore(tmp_path / ".aegis")
    monkeypatch.setattr(check_mod, "fetch_json", lambda url, headers: V1)
    seen = {}

    def build(threshold, db, persist=True, **kw):
        seen.update(kw)
        return Healer(store=store, proposer=None)

    monkeypatch.setattr(check_mod, "build_healer", build)
    runner = CliRunner(env={"COLUMNS": "200"})
    monkeypatch.setenv("AEGIS_WEBHOOK_URL", URL)
    assert runner.invoke(app, ["check", "http://x", "-m", "fixtures.models:User"]).exit_code == 0
    assert callable(seen["on_pending"])

    monkeypatch.setenv("AEGIS_WEBHOOK_URL", "http://example.com/hook")
    r = runner.invoke(app, ["check", "http://x", "-m", "fixtures.models:User"])
    assert r.exit_code == 0
    assert seen["on_pending"] is None
    assert "example.com" not in r.output
    assert "AEGIS_WEBHOOK_URL" in r.output


def test_no_notification_when_not_persisting(tmp_path):
    seen = []
    healer = Healer(
        store=FileStore(tmp_path / ".aegis"),
        proposer=FakeProposer(v2_agent_patch(0.8)),
        persist=False,
        on_pending=seen.append,
    )
    assert healer.validate([V1, V2], User).status == "pending"
    assert seen == []


def test_cli_dry_run_does_not_notify(monkeypatch, tmp_path):
    store = FileStore(tmp_path / ".aegis")
    monkeypatch.setattr(check_mod, "fetch_json", lambda url, headers: [V1, V2])
    calls = []
    monkeypatch.setattr(check_mod, "webhook_notifier", lambda url: calls.append)

    def build(threshold, db, persist=True, **kw):
        return Healer(
            store=store, proposer=FakeProposer(v2_agent_patch(0.8)), persist=persist, **kw
        )

    monkeypatch.setattr(check_mod, "build_healer", build)
    monkeypatch.setenv("AEGIS_WEBHOOK_URL", URL)
    r = CliRunner(env={"COLUMNS": "200"}).invoke(
        app, ["check", "http://x", "-m", "fixtures.models:User", "--dry-run"]
    )
    assert r.exit_code == 1, r.output
    assert calls == []


def test_webhook_client_ignores_env(monkeypatch):
    from aegis_healer import notify

    seen = {}
    real = httpx.Client

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(notify.httpx, "Client", spy)
    webhook_notifier(URL, transport=httpx.MockTransport(lambda r: httpx.Response(200)))(notice())
    assert seen["trust_env"] is False
    assert seen["follow_redirects"] is False
