import json
import re

import pytest
from fakes import FakeProposer, v2_agent_patch
from fixtures.models import User
from fixtures.payloads import V1, V2
from typer.testing import CliRunner

from aegis_healer import Healer
from aegis_healer.cli import app
from aegis_healer.quarantine import Quarantine, replay
from aegis_healer.store.files import FileStore

runner = CliRunner(env={"COLUMNS": "200"})
KEY = "fixtures.models.User"
SPEC = "fixtures.models:User"


def pending_setup(tmp_path, **kw):
    store = FileStore(tmp_path / ".aegis")
    healer = Healer(store=store, proposer=FakeProposer(v2_agent_patch(0.8)), **kw)
    result = healer.validate([V1, V2], User)
    return store, healer, result


def test_pending_quarantines_failing_records(tmp_path):
    store, _, result = pending_setup(tmp_path)
    assert result.status == "pending"
    assert len(result.records) == 1
    batches = Quarantine(store.root).list()
    assert len(batches) == 1
    assert len(batches[0].records) == 1
    assert batches[0].pending_id == result.pending_id
    assert batches[0].reason == "pending"
    assert batches[0].id == result.quarantine_id
    assert batches[0].model_key == KEY


def test_failed_quarantines_with_status_reason(tmp_path):
    store = FileStore(tmp_path / ".aegis")
    result = Healer(store=store, proposer=None).validate([V2], User)
    assert result.status == "failed"
    (batch,) = Quarantine(store.root).list()
    assert batch.reason == "failed"
    assert batch.pending_id is None


def test_gitignore_written_once(tmp_path):
    store, healer, _ = pending_setup(tmp_path)
    healer.validate([V2], User)
    lines = (store.root / ".gitignore").read_text().splitlines()
    assert lines.count("quarantine/") == 1


def test_gitignore_appends_to_existing(tmp_path):
    root = tmp_path / ".aegis"
    root.mkdir()
    (root / ".gitignore").write_text("foo")
    Quarantine(root).add(KEY, [{"a": 1}], "pending", None)
    assert (root / ".gitignore").read_text().splitlines() == ["foo", "quarantine/"]


def test_batch_id_format_and_traversal_rejected(tmp_path):
    q = Quarantine(tmp_path)
    bid = q.add(KEY, [{"a": 1}], "pending", None)
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", bid)
    for bad in ("../x", "x", bid + "/..", "..\\" + bid):
        with pytest.raises(ValueError):
            q.load(bad)
        with pytest.raises(ValueError):
            q.remove(bad)


def test_replay_after_approval_releases(tmp_path):
    store, _, result = pending_setup(tmp_path)
    store.resolve_pending(result.pending_id, True)
    q = Quarantine(store.root)
    out = replay(q, Healer(store=store, proposer=None), User)
    assert len(out.released) == 1
    assert out.still_blocked == 0
    assert len(out.batches_cleared) == 1
    assert q.list() == []


def test_replay_without_approval_keeps_batch(tmp_path):
    store, _, _ = pending_setup(tmp_path)
    q = Quarantine(store.root)
    out = replay(q, Healer(store=store, proposer=None), User)
    assert out.released == []
    assert out.still_blocked == 1
    assert len(q.list()) == 1
    assert len(store.list_pending()) == 1  # replay never creates new pending fixes


def test_replay_partial_batch_untouched(tmp_path):
    store = FileStore(tmp_path / ".aegis")
    q = Quarantine(store.root)
    q.add(KEY, [V1, V2], "pending", None)
    out = replay(q, Healer(store=store, proposer=None), User)
    assert out.released == []
    assert out.still_blocked == 1
    assert len(q.list()) == 1


def test_replay_ignores_other_models_and_filters_batch(tmp_path):
    store, _, result = pending_setup(tmp_path)
    store.resolve_pending(result.pending_id, True)
    q = Quarantine(store.root)
    q.add("other.Model", [{"x": 1}], "failed", None)
    out = replay(q, Healer(store=store, proposer=None), User)
    assert len(out.released) == 1
    assert [b.model_key for b in q.list()] == ["other.Model"]


def test_replay_does_not_write_run_log(tmp_path):
    store, _, result = pending_setup(tmp_path)
    store.resolve_pending(result.pending_id, True)
    before = (store.root / "runs").exists() and sorted((store.root / "runs").rglob("*"))
    replay(Quarantine(store.root), Healer(store=store, proposer=None), User)
    after = (store.root / "runs").exists() and sorted((store.root / "runs").rglob("*"))
    assert before == after


def test_quarantine_false_disables(tmp_path):
    store, _, result = pending_setup(tmp_path, quarantine=False)
    assert result.status == "pending"
    assert result.quarantine_id is None
    assert Quarantine(store.root).list() == []


def test_persist_false_does_not_quarantine(tmp_path):
    store = FileStore(tmp_path / ".aegis")
    r = Healer(store=store, proposer=None, persist=False).validate([V2], User)
    assert r.quarantine_id is None
    assert not (store.root / "quarantine").exists()


def test_reason_does_not_leak_payload(tmp_path):
    store, _, _ = pending_setup(tmp_path)
    (path,) = (store.root / "quarantine").glob("*.json")
    assert json.loads(path.read_text())["reason"] == "pending"


def test_cli_replay_exit_codes(tmp_path):
    store, _, result = pending_setup(tmp_path)
    db = str(store.root)
    r = runner.invoke(app, ["replay", "--model", SPEC, "--db", db])
    assert r.exit_code == 1, r.output
    assert "Still blocked: 1" in r.output
    r = runner.invoke(app, ["review", "--approve", str(result.pending_id), "--db", db])
    assert r.exit_code == 0, r.output
    assert f"Run 'aegis replay --model {SPEC}' to release quarantined records." in r.output
    r = runner.invoke(app, ["replay", "--model", SPEC, "--db", db])
    assert r.exit_code == 0, r.output
    assert "Would release 1" in r.output
    assert "Nothing removed" in r.output
    assert "V5A" not in r.output
    assert len(Quarantine(store.root).list()) == 1


def test_cli_review_no_hint_without_quarantine(tmp_path):
    store, _, result = pending_setup(tmp_path, quarantine=False)
    r = runner.invoke(app, ["review", "--approve", str(result.pending_id), "--db", str(store.root)])
    assert r.exit_code == 0, r.output
    assert "aegis replay" not in r.output


def test_cli_interactive_approve_prints_hint(tmp_path):
    store, _, _ = pending_setup(tmp_path)
    r = runner.invoke(app, ["review", "--db", str(store.root)], input="a\n")
    assert r.exit_code == 0, r.output
    assert "aegis replay --model" in r.output


def approved(tmp_path):
    store, _, result = pending_setup(tmp_path)
    store.resolve_pending(result.pending_id, True)
    return store


def test_cli_replay_without_out_is_non_destructive(tmp_path):
    store = approved(tmp_path)
    r = runner.invoke(app, ["replay", "--model", SPEC, "--db", str(store.root)])
    assert r.exit_code == 0, r.output
    assert len(Quarantine(store.root).list()) == 1


def test_cli_replay_out_saves_then_removes(tmp_path):
    store = approved(tmp_path)
    out = tmp_path / "released.jsonl"
    r = runner.invoke(app, ["replay", "--model", SPEC, "--out", str(out), "--db", str(store.root)])
    assert r.exit_code == 0, r.output
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["user_id"] == 1
    assert Quarantine(store.root).list() == []


def test_corrupt_batch_skipped(tmp_path, caplog):
    store = approved(tmp_path)
    q = Quarantine(store.root)
    bad = q.dir / "20260101T000000Z-abcdef.json"
    bad.write_text('{"id": "SECRETSNIPPET", "records": 5', encoding="utf-8")
    with caplog.at_level("WARNING", logger="aegis_healer"):
        assert len(q.list()) == 1
    assert "20260101T000000Z-abcdef.json" in caplog.text
    assert "SECRETSNIPPET" not in caplog.text
    with pytest.raises(ValueError, match="unreadable") as ei:
        q.load("20260101T000000Z-abcdef")
    assert "SECRETSNIPPET" not in str(ei.value)
    r = runner.invoke(app, ["replay", "--model", SPEC, "--db", str(store.root)])
    assert r.exit_code == 0, r.output
    assert "SECRETSNIPPET" not in r.output
    assert len(replay(q, Healer(store=store, proposer=None), User).released) == 1


def test_remove_failure_keeps_batch_and_records(tmp_path, monkeypatch):
    store = approved(tmp_path)
    q = Quarantine(store.root)

    def boom(self, batch_id):
        raise PermissionError("nope")

    monkeypatch.setattr(Quarantine, "remove", boom)
    out = replay(q, Healer(store=store, proposer=None), User)
    assert len(out.released) == 1
    assert len(out.remove_failed) == 1
    monkeypatch.undo()
    assert len(q.list()) == 1


def test_remove_false_keeps_batches(tmp_path):
    store = approved(tmp_path)
    q = Quarantine(store.root)
    out = replay(q, Healer(store=store, proposer=None), User, remove=False)
    assert len(out.released) == 1 and len(out.batches_cleared) == 1
    assert len(q.list()) == 1


@pytest.mark.parametrize("bad", ["../etc/passwd", "20260101T000000Z-aaaaaa"])
def test_cli_replay_unknown_batch(tmp_path, bad):
    store = approved(tmp_path)
    r = runner.invoke(app, ["replay", "--model", SPEC, "--batch", bad, "--db", str(store.root)])
    assert r.exit_code == 1
    assert f"No quarantine batch {bad} for {SPEC}." in r.output
    assert "Traceback" not in r.output


def test_cli_replay_batch_of_other_model(tmp_path):
    store = approved(tmp_path)
    bid = Quarantine(store.root).add("other.Model", [{"x": 1}], "failed", None)
    r = runner.invoke(app, ["replay", "--model", SPEC, "--batch", bid, "--db", str(store.root)])
    assert r.exit_code == 1
    assert "No quarantine batch" in r.output


def test_healer_quarantine_valueerror_swallowed(tmp_path, monkeypatch):
    def boom(self, *a, **k):
        raise ValueError("x")

    monkeypatch.setattr(Quarantine, "add", boom)
    store = FileStore(tmp_path / ".aegis")
    r = Healer(store=store, proposer=None).validate([V2], User)
    assert r.status == "failed" and r.quarantine_id is None


def test_cli_replay_exception_is_clean(tmp_path, monkeypatch):
    import aegis_healer.cli.replay_cmd as mod

    store = approved(tmp_path)

    def boom(*a, **k):
        raise ValueError("payload-ish secret")

    monkeypatch.setattr(mod, "run_replay", boom)
    r = runner.invoke(app, ["replay", "--model", SPEC, "--db", str(store.root)])
    assert r.exit_code == 1
    assert "Traceback" not in r.output and "secret" not in r.output
    assert "ValueError" in r.output


def test_cli_out_untouched_when_nothing_released(tmp_path):
    store, _, _ = pending_setup(tmp_path)  # not approved: nothing releasable
    out = tmp_path / "keep.jsonl"
    out.write_text("precious\n", encoding="utf-8")
    r = runner.invoke(app, ["replay", "--model", SPEC, "--out", str(out), "--db", str(store.root)])
    assert out.read_text(encoding="utf-8") == "precious\n"
    assert "Nothing to release." in r.output


def test_cli_no_nothing_removed_note_when_zero(tmp_path):
    store, _, _ = pending_setup(tmp_path)
    r = runner.invoke(app, ["replay", "--model", SPEC, "--db", str(store.root)])
    assert "Nothing removed" not in r.output
    assert "Nothing to release." in r.output


def test_cli_out_remove_failure_exits_1(tmp_path, monkeypatch):
    store = approved(tmp_path)
    out = tmp_path / "o.jsonl"

    def boom(self, batch_id):
        raise PermissionError("nope")

    monkeypatch.setattr(Quarantine, "remove", boom)
    r = runner.invoke(app, ["replay", "--model", SPEC, "--out", str(out), "--db", str(store.root)])
    assert r.exit_code == 1
    assert "re-running will export their records again" in r.output
    assert out.exists()
