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


def test_run_rules_writes_raw_and_report(tmp_path):
    cases = _seed_dir(tmp_path)
    out = tmp_path / "out"
    assert cli.main(["run", "--split", "dev", "--mode", "rules", "--cases", str(cases),
                     "--out", str(out)]) == 0
    raws = list((out / "raw").glob("*-rules.json"))
    assert len(raws) == 1
    rows = json.loads(raws[0].read_text(encoding="utf-8"))
    assert {r["id"] for r in rows} == {c.id for c in SEEDS if split_of(c.id) == "dev"}
    assert list(out.glob("*.md")) and list(out.glob("*.json"))


def test_report_rerenders_from_raw(tmp_path):
    cases = _seed_dir(tmp_path)
    out = tmp_path / "out"
    cli.main(["run", "--split", "dev", "--mode", "rules", "--cases", str(cases),
              "--out", str(out)])
    for p in out.glob("*.md"):
        p.unlink()
    assert cli.main(["report", "--cases", str(cases), "--out", str(out)]) == 0
    assert list(out.glob("*.md"))


def test_no_args_is_usage_error():
    with pytest.raises(SystemExit):
        cli.main([])
