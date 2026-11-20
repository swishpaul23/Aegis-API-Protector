import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeProposer, m, v2_agent_patch
from fixtures.models import User
from fixtures.payloads import V1, V2
from pydantic import BaseModel, field_validator
from typer.testing import CliRunner

from aegis_healer import Healer
from aegis_healer.agent import Usage
from aegis_healer.cli import app
from aegis_healer.runlog import RunRecord, append_run, read_runs
from aegis_healer.store.files import FileStore
from aegis_healer.types import MappingPatch

runner = CliRunner(env={"COLUMNS": "200"})


def rec(i: int, **kw) -> RunRecord:
    base = dict(
        at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
        model_key="m.User",
        status="ok",
        records=1,
        healed_or_valid=1,
        failed_records=0,
        fields_fixed=[],
        pending_id=None,
        usage=Usage(),
        message="",
    )
    return RunRecord(**{**base, **kw})


def test_append_and_read_newest_first(tmp_path):
    root = tmp_path / "new" / ".aegis"
    for i in range(3):
        append_run(root, rec(i, records=i + 1))
    runs = read_runs(root)
    assert [r.records for r in runs] == [3, 2, 1]
    assert [r.records for r in read_runs(root, limit=2)] == [3, 2]
    assert read_runs(tmp_path / "none") == []


def test_malformed_line_skipped(tmp_path):
    append_run(tmp_path, rec(0))
    with open(tmp_path / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write("not json\n{}\n")
    append_run(tmp_path, rec(1, records=7))
    runs = read_runs(tmp_path)
    assert [r.records for r in runs] == [7, 1]


def test_healer_logs_each_run(tmp_path):
    store = FileStore(tmp_path / ".aegis")
    h = Healer(store=store, proposer=FakeProposer(v2_agent_patch(0.95)))
    h.validate(V1, User)
    h.validate(V2, User)
    lines = (store.root / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    runs = read_runs(store.root)
    assert [r.status for r in runs] == ["healed", "ok"]
    assert runs[0].fields_fixed and runs[0].records == 1 and runs[0].healed_or_valid == 1


def test_persist_false_does_not_log(tmp_path):
    store = FileStore(tmp_path / ".aegis")
    Healer(store=store, proposer=None, persist=False).validate(V1, User)
    assert not (store.root / "runs.jsonl").exists()


def test_no_payload_values_in_log(tmp_path):
    store = FileStore(tmp_path / ".aegis")
    Healer(store=store, proposer=FakeProposer(v2_agent_patch(0.95))).validate(V2, User)
    text = (store.root / "runs.jsonl").read_text(encoding="utf-8")
    assert '"status":"healed"' in text
    assert "a@x.io" not in text


def test_log_failure_does_not_break_validate(tmp_path, caplog):
    root = tmp_path / ".aegis"
    store = FileStore(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "runs.jsonl").mkdir()  # appending to a directory raises OSError
    with caplog.at_level(logging.WARNING, logger="aegis_healer"):
        assert Healer(store=store, proposer=None).validate(V1, User).status == "ok"
    assert "could not write run log" in caplog.text


def test_cli_runs_table(tmp_path):
    store = FileStore(tmp_path / ".aegis")
    Healer(store=store, proposer=FakeProposer(v2_agent_patch(0.95))).validate(V2, User)
    r = runner.invoke(app, ["runs", "--db", str(store.root)])
    assert r.exit_code == 0, r.output
    assert "healed" in r.output and "fixtures.models.User" in r.output
    assert re.search(r"~\$\d+\.\d{4}", r.output)


def test_cli_runs_empty(tmp_path):
    r = runner.invoke(app, ["runs", "--db", str(tmp_path / ".aegis")])
    assert r.exit_code == 0 and "No runs" in r.output


@pytest.mark.parametrize("limit", [1])
def test_cli_runs_limit(tmp_path, limit):
    for i in range(3):
        append_run(tmp_path, rec(i, model_key=f"k{i}"))
    r = runner.invoke(app, ["runs", "--db", str(tmp_path), "--limit", str(limit)])
    assert "k2" in r.output and "k0" not in r.output


class Strict(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _check(cls, v: str) -> str:
        raise ValueError(f"bad email {v}")


def _run_strict(tmp_path, **kw):
    store = FileStore(tmp_path / ".aegis")
    r = Healer(
        store=store, proposer=FakeProposer(MappingPatch(mappings=[m("who", "email")])), **kw
    ).validate({"who": "secret@x.io"}, Strict)
    return r, (store.root / "runs.jsonl").read_text(encoding="utf-8"), read_runs(store.root)[0]


def test_failed_message_is_categorical_by_default(tmp_path):
    r, text, run = _run_strict(tmp_path)
    assert r.status == "failed"
    assert "secret@x.io" not in text
    assert run.message == "failed_validation"


def test_full_privacy_keeps_message_text(tmp_path):
    r, text, run = _run_strict(tmp_path, privacy="full")
    assert run.message == r.message[:300] and "secret@x.io" in run.message


@pytest.mark.parametrize(
    ("status", "message", "code"),
    [
        ("ok", "", ""),
        ("pending", "Fix needs review (x).", "pending_review"),
        ("failed", "Unresolved drift in a and agent disabled", "agent_disabled"),
        (
            "failed",
            "Agent could not produce a valid fix: LLM call budget exhausted",
            "budget_exhausted",
        ),
        ("failed", "Agent could not produce a valid fix: nope", "agent_error"),
        ("failed", "Agent proposed changes outside the drifted fields: x", "out_of_scope"),
        ("failed", "Cannot heal without changing your model. x", "unmappable"),
        ("failed", "something new", "failed"),
    ],
)
def test_run_message_codes(status, message, code):
    from aegis_healer.runlog import run_message

    assert run_message(status, message, "masked") == code


def test_run_message_drops_values_unless_full():
    from aegis_healer.runlog import run_message

    msg = "Proposed fix failed validation: email: bad email secret@x.io"
    assert run_message("failed", msg, "masked") == "failed_validation"
    assert run_message("failed", msg, "schema") == "failed_validation"
    assert run_message("failed", msg, "full") == msg
