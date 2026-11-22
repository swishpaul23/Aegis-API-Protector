import json

import pytest

from evals.real_api import __main__ as cli
from evals.real_api.cases import load_cases, save_case, split_of
from evals.real_api.miner.sources import Source
from evals.real_api.tests.seed_cases import SEEDS


def _seed_dir(tmp_path):
    d = tmp_path / "cases"
    for c in SEEDS:
        save_case(c, d)
    return d


def test_run_defaults_to_test_split(tmp_path, monkeypatch):
    cases = _seed_dir(tmp_path)
    seen = {}

    def fake_run_all(cs, mode, tmp_root):
        seen[mode] = [c.id for c in cs]
        return []

    monkeypatch.setattr(cli, "run_all", fake_run_all)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    rc = cli.main(["run", "--cases", str(cases), "--out", str(tmp_path / "out")])
    assert rc == 0
    expect = sorted(c.id for c in SEEDS if split_of(c.id) == "test")
    assert sorted(seen["rules"]) == expect
    assert sorted(seen["agent"]) == expect


def test_run_agent_without_key_exits_nonzero_with_message(tmp_path, monkeypatch, capsys):
    cases = _seed_dir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(cli, "run_all", lambda *a, **k: called.append(1) or [])
    rc = cli.main(["run", "--mode", "both", "--cases", str(cases), "--out", str(tmp_path / "o")])
    assert rc == 2
    assert called == []  # key checked before the rules pass
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert "Traceback" not in err


def test_review_render_then_apply_via_cli(tmp_path, capsys):
    cases = _seed_dir(tmp_path)
    review = tmp_path / "REVIEW.md"
    assert cli.main(["review", "render", "--cases", str(cases), "--review", str(review)]) == 0
    text = review.read_text(encoding="utf-8").replace("[ ] confirm", "[x] confirm")
    review.write_text(text, encoding="utf-8")
    rc = cli.main(["review", "apply", "--cases", str(cases), "--review", str(review)])
    assert rc == 0
    assert all(c.label_status == "confirmed" for c in load_cases(cases))
    assert "confirmed" in capsys.readouterr().out


def test_review_apply_nonzero_on_malformed_row(tmp_path, capsys):
    cases = _seed_dir(tmp_path)
    review = tmp_path / "REVIEW.md"
    cli.main(["review", "render", "--cases", str(cases), "--review", str(review)])
    review.write_text(review.read_text(encoding="utf-8") + "| `seed-rename` | broken |\n")
    rc = cli.main(["review", "apply", "--cases", str(cases), "--review", str(review)])
    assert rc != 0
    assert "malformed" in capsys.readouterr().out


def test_mine_does_not_overwrite_existing_case(tmp_path, monkeypatch, capsys):
    cases = tmp_path / "cases"
    confirmed = SEEDS[0].model_copy(update={"label_status": "confirmed", "notes": "keep me"})
    save_case(confirmed, cases)
    redrafted = SEEDS[0].model_copy(update={"label_status": "drafted", "notes": "new"})
    fresh = SEEDS[1]
    srcs = [Source("one", "u", "p", [("a", "b")]), Source("two", "u", "p")]
    monkeypatch.setattr(cli, "SOURCES", srcs)
    monkeypatch.setattr(cli, "clone_repo", lambda url, d: None)
    monkeypatch.setattr(cli, "mine", lambda s, d: [redrafted, fresh])
    assert cli.main(["mine", "--cases", str(cases)]) == 0
    by_id = {c.id: c for c in load_cases(cases)}
    assert by_id[confirmed.id].label_status == "confirmed"
    assert by_id[confirmed.id].notes == "keep me"
    assert fresh.id in by_id
    out = capsys.readouterr().out
    assert "one: written 1, skipped-existing 1" in out
    assert "two" in out and "no pairs" in out


def _run(cases, out, split, mode="rules"):
    return cli.main(
        ["run", "--split", split, "--mode", mode, "--cases", str(cases), "--out", str(out)]
    )


def _split_seed_dir(tmp_path):
    """Seed cases plus filler so both splits are non-empty."""
    d = _seed_dir(tmp_path)
    have = {split_of(c.id) for c in SEEDS}
    for want in {"dev", "test"} - have:
        i = 0
        while split_of(f"fill-{i}") != want:
            i += 1
        save_case(SEEDS[0].model_copy(update={"id": f"fill-{i}"}), d)
    return d


def test_run_rules_writes_raw_and_report(tmp_path):
    cases = _split_seed_dir(tmp_path)
    out = tmp_path / "out"
    assert _run(cases, out, "dev") == 0
    (run_dir,) = [p for p in out.iterdir() if p.is_dir()]
    assert run_dir.name.endswith("-dev-1")
    raw = json.loads((run_dir / "raw-rules.json").read_text(encoding="utf-8"))
    assert raw["split"] == "dev" and raw["mode"] == "rules"
    assert raw["meta"]["run"] == run_dir.name
    ids = {c.id for c in load_cases(cases) if split_of(c.id) == "dev"}
    assert {r["id"] for r in raw["rows"]} == ids
    assert all("label_status" in r and "usage" in r for r in raw["rows"])
    assert list(run_dir.glob("*.md")) and list(run_dir.glob("*.json"))


def test_same_day_dev_and_test_runs_do_not_overwrite(tmp_path):
    cases = _split_seed_dir(tmp_path)
    out = tmp_path / "out"
    assert _run(cases, out, "dev") == 0
    assert _run(cases, out, "test") == 0
    assert _run(cases, out, "test") == 0
    dirs = sorted(p.name for p in out.iterdir() if p.is_dir())
    assert len(dirs) == 3
    assert dirs[0].endswith("-dev-1")
    assert dirs[1].endswith("-test-1") and dirs[2].endswith("-test-2")
    for d in dirs:
        assert (out / d / "raw-rules.json").exists()
        md = next((out / d).glob("*.md")).read_text(encoding="utf-8")
        assert f"Split: {d.split('-')[3]}" in md
        assert f"Run: {d}" in md


def test_report_uses_stored_meta_not_today(tmp_path, monkeypatch):
    cases = _split_seed_dir(tmp_path)
    out = tmp_path / "out"
    _run(cases, out, "dev")
    (run_dir,) = [p for p in out.iterdir() if p.is_dir()]
    stored = json.loads((run_dir / "raw-rules.json").read_text(encoding="utf-8"))["meta"]
    for p in run_dir.glob("*.md"):
        p.unlink()
    for p in run_dir.glob("[0-9]*.json"):
        p.unlink()
    fake = {"aegis_version": "x", "model": "y", "date": "1999-01-01"}
    monkeypatch.setattr(cli, "_meta", lambda: fake)
    assert cli.main(["report", str(run_dir), "--cases", str(cases), "--out", str(out)]) == 0
    assert (run_dir / f"{stored['date']}.md").exists()
    assert not list(out.rglob("1999-01-01*"))
    md = (run_dir / f"{stored['date']}.md").read_text(encoding="utf-8")
    assert "Model: y" not in md


def test_report_defaults_to_latest_run_and_warns_on_label_change(tmp_path, capsys):
    cases = _split_seed_dir(tmp_path)
    out = tmp_path / "out"
    _run(cases, out, "dev")
    c = next(c for c in load_cases(cases) if split_of(c.id) == "dev")
    save_case(c.model_copy(update={"label_status": "confirmed"}), cases)
    capsys.readouterr()
    assert cli.main(["report", "--cases", str(cases), "--out", str(out)]) == 0
    assert f"warning: {c.id}" in capsys.readouterr().err


def test_zero_case_run_exits_1_and_creates_nothing(tmp_path, capsys):
    out = tmp_path / "out"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run(empty, out, "test") == 1
    assert not out.exists()
    assert "no cases" in capsys.readouterr().err


def test_no_args_is_usage_error():
    with pytest.raises(SystemExit):
        cli.main([])
