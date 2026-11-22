import hashlib
import json

import pytest

from evals.real_api import __main__ as cli
from evals.real_api import runner
from evals.real_api.cases import load_cases, save_case, split_of
from evals.real_api.miner.sources import Source
from evals.real_api.tests.seed_cases import SEEDS


@pytest.fixture(autouse=True)
def _no_live_preflight(monkeypatch):
    """CLI tests never reach the network; tests that care override this."""
    monkeypatch.setattr(runner, "preflight_agent", lambda: None)


def _seed_dir(tmp_path, confirmed=False):
    d = tmp_path / "cases"
    for c in SEEDS:
        save_case(c.model_copy(update={"label_status": "confirmed"}) if confirmed else c, d)
    return d


def test_run_defaults_to_test_split(tmp_path, monkeypatch):
    cases = _seed_dir(tmp_path, confirmed=True)
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


def _run(cases, out, split, mode="rules", *extra):
    return cli.main(
        ["run", "--split", split, "--mode", mode, "--cases", str(cases), "--out", str(out), *extra]
    )


def _split_seed_dir(tmp_path):
    """Seed cases plus filler so both splits are non-empty; test-split cases are confirmed,
    dev-split ones drafted."""
    d = tmp_path / "cases"
    have = {split_of(c.id) for c in SEEDS}
    extra = []
    for want in {"dev", "test"} - have:
        i = 0
        while split_of(f"fill-{i}") != want:
            i += 1
        extra.append(SEEDS[0].model_copy(update={"id": f"fill-{i}"}))
    for c in [*SEEDS, *extra]:
        status = "confirmed" if split_of(c.id) == "test" else "drafted"
        save_case(c.model_copy(update={"label_status": status}), d)
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
    assert all("label_status" in r and "usage" in r and "error" in r for r in raw["rows"])
    assert list(run_dir.glob("*.md")) and list(run_dir.glob("*.json"))


def test_same_day_dev_and_test_runs_do_not_overwrite(tmp_path):
    cases = _split_seed_dir(tmp_path)
    out = tmp_path / "out"
    assert _run(cases, out, "dev") == 0
    assert _run(cases, out, "test") == 0
    assert _run(cases, out, "test", "rules", "--rerun") == 0
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


def _tick_clock(monkeypatch):
    ticks = iter(range(10_000))

    def now():
        n = next(ticks)
        return f"2026-01-01T{n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}+00:00"

    monkeypatch.setattr(cli, "_now", now)


def test_default_report_picks_most_recent_run_not_name_order(tmp_path, monkeypatch, capsys):
    cases = _split_seed_dir(tmp_path)
    out = tmp_path / "out"
    _tick_clock(monkeypatch)
    for split in ("dev", "test", "dev"):
        _run(cases, out, split)
    capsys.readouterr()
    assert cli.main(["report", "--cases", str(cases), "--out", str(out)]) == 0
    assert "-dev-2" in capsys.readouterr().out


def test_default_report_picks_run_11_over_run_2(tmp_path, monkeypatch, capsys):
    cases = _split_seed_dir(tmp_path)
    out = tmp_path / "out"
    _tick_clock(monkeypatch)
    for _ in range(11):
        _run(cases, out, "dev")
    capsys.readouterr()
    assert cli.main(["report", "--cases", str(cases), "--out", str(out)]) == 0
    assert "-dev-11" in capsys.readouterr().out


def test_zero_case_run_exits_1_and_creates_nothing(tmp_path, capsys):
    out = tmp_path / "out"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run(empty, out, "test") == 1
    assert not out.exists()
    assert "no confirmed cases" in capsys.readouterr().err


def test_no_args_is_usage_error():
    with pytest.raises(SystemExit):
        cli.main([])


def test_run_preflight_failure_exits_2_before_creating_anything(tmp_path, monkeypatch, capsys):
    cases = _split_seed_dir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "bad")

    def boom():
        raise RuntimeError("agent preflight failed (m): AuthenticationError: 401")

    called = []
    monkeypatch.setattr(runner, "preflight_agent", boom)
    monkeypatch.setattr(cli, "run_all", lambda *a, **k: called.append(1) or [])
    out = tmp_path / "out"
    assert _run(cases, out, "dev", mode="both") == 2
    assert called == [] and not out.exists()
    err = capsys.readouterr().err
    assert "preflight" in err and "Traceback" not in err


def test_run_agent_outage_exits_2_and_writes_no_agent_rows(tmp_path, monkeypatch, capsys):
    cases = _split_seed_dir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    real = cli.run_all

    def fake(cs, mode, tmp_root):
        if mode == "agent":
            raise runner.AgentOutage("aborted: 4 cases ended in agent_error")
        return real(cs, mode, tmp_root)

    monkeypatch.setattr(cli, "run_all", fake)
    out = tmp_path / "out"
    assert _run(cases, out, "dev", mode="both") == 2
    (run_dir,) = list(out.iterdir())
    assert (run_dir / "raw-rules.json").exists()
    assert not (run_dir / "raw-agent.json").exists()
    assert not list(run_dir.glob("*.md"))
    assert "agent_error" in capsys.readouterr().err


def test_test_split_runs_only_confirmed_dev_runs_all(tmp_path, monkeypatch):
    d = tmp_path / "cases"
    by_split: dict[str, list] = {"dev": [], "test": []}
    i = 0
    while min(len(v) for v in by_split.values()) < 2:
        cid = f"c-{i}"
        if len(by_split[split_of(cid)]) < 2:
            by_split[split_of(cid)].append(cid)
        i += 1
    for ids in by_split.values():
        for cid, status in zip(ids, ("confirmed", "drafted"), strict=True):
            save_case(SEEDS[0].model_copy(update={"id": cid, "label_status": status}), d)
    seen = {}
    monkeypatch.setattr(cli, "run_all", lambda cs, mode, t: seen.setdefault(mode, cs) and [])
    out = tmp_path / "out"
    assert _run(d, out, "test") == 0
    assert [c.id for c in seen.pop("rules")] == [by_split["test"][0]]
    assert _run(d, out, "dev") == 0
    assert sorted(c.id for c in seen["rules"]) == sorted(by_split["dev"])


def test_test_split_with_no_confirmed_cases_exits_1(tmp_path, capsys):
    cases = _seed_dir(tmp_path)  # all drafted, all in the test split
    out = tmp_path / "out"
    assert _run(cases, out, "test") == 1
    assert not out.exists()
    assert "confirmed" in capsys.readouterr().err


def test_second_test_run_refused_without_rerun(tmp_path, capsys):
    cases = _split_seed_dir(tmp_path)
    out = tmp_path / "out"
    assert _run(cases, out, "test") == 0
    first = json.loads(next(out.glob("*-test-1/raw-rules.json")).read_text(encoding="utf-8"))
    assert first["meta"]["prior_test_runs"] == 0
    capsys.readouterr()
    assert _run(cases, out, "test") == 2
    err = capsys.readouterr().err
    assert "already" in err and "--rerun" in err
    assert len(list(out.glob("*-test-*"))) == 1
    assert _run(cases, out, "dev") == 0  # dev is never blocked
    assert _run(cases, out, "test", "rules", "--rerun") == 0
    second = json.loads(next(out.glob("*-test-2/raw-rules.json")).read_text(encoding="utf-8"))
    assert second["meta"]["prior_test_runs"] == 1
    md = next(out.glob("*-test-2/*.md")).read_text(encoding="utf-8")
    assert "Prior test runs: 1" in md


def test_rules_only_run_report_says_agent_not_run(tmp_path):
    cases = _split_seed_dir(tmp_path)
    out = tmp_path / "out"
    assert _run(cases, out, "test", "rules") == 0
    (run_dir,) = list(out.iterdir())
    md = next(run_dir.glob("*.md")).read_text(encoding="utf-8")
    assert "**Rules + agent: not run.**" in md and "0.0% (0/0)" not in md
    for p in run_dir.glob("*.md"):
        p.unlink()
    assert cli.main(["report", str(run_dir), "--cases", str(cases), "--out", str(out)]) == 0
    assert "**Rules + agent: not run.**" in next(run_dir.glob("*.md")).read_text(encoding="utf-8")


def test_mine_handles_several_sources_with_the_same_api(tmp_path, monkeypatch, capsys):
    cases = tmp_path / "cases"
    srcs = [
        Source("tw", "https://x/repo", "a.json", [("a", "b")]),
        Source("tw", "https://x/repo", "b.json", [("a", "b")]),
        Source("other", "https://x/repo", "c.json", [("a", "b")]),
    ]
    monkeypatch.setattr(cli, "SOURCES", srcs)
    clones = []
    monkeypatch.setattr(cli, "clone_repo", lambda url, d: clones.append(url))
    mined = {"a.json": SEEDS[0], "b.json": SEEDS[1], "c.json": SEEDS[2]}
    monkeypatch.setattr(cli, "mine", lambda s, d: [mined[s.spec_path]])
    assert cli.main(["mine", "--api", "tw", "--cases", str(cases)]) == 0
    assert {c.id for c in load_cases(cases)} == {SEEDS[0].id, SEEDS[1].id}
    assert clones == ["https://x/repo"]  # one clone for both sources
    assert capsys.readouterr().out.count("tw: written 1") == 2


# --- select -------------------------------------------------------------------------------


def _hash_key(case_id: str) -> str:
    return hashlib.sha256(case_id.encode()).hexdigest()


def _mk(case_id, api, expected="repair", status="drafted"):
    base = SEEDS[2] if expected == "refuse" else SEEDS[0]
    return base.model_copy(
        update={"id": case_id, "api": api, "label_status": status, "expected": expected}
    )


def _select_dir(tmp_path):
    """api a: 5 repair + 3 refuse drafted, 2 confirmed; api b: 2 repair drafted."""
    d = tmp_path / "cases"
    for i in range(5):
        save_case(_mk(f"a-r{i}", "a"), d)
    for i in range(3):
        save_case(_mk(f"a-x{i}", "a", "refuse"), d)
    for i in range(2):
        save_case(_mk(f"a-c{i}", "a", status="confirmed"), d)
    for i in range(2):
        save_case(_mk(f"b-r{i}", "b"), d)
    return d


def _top(d):
    return {c.id for c in load_cases(d)}


def _pooled(d):
    return {p.stem for p in (d / "pool").glob("*.json")}


def _sel(d, *extra):
    return cli.main(
        ["select", "--cases", str(d), "--repair-per-api", "2", "--refuse-per-api", "1", *extra]
    )


def test_select_respects_quotas_per_api(tmp_path, capsys):
    d = _select_dir(tmp_path)
    assert _sel(d) == 0
    kept = {c.id: c for c in load_cases(d) if c.label_status == "drafted"}
    for api, n_rep, n_ref in (("a", 2, 1), ("b", 2, 0)):
        mine = [c for c in kept.values() if c.api == api]
        assert sum(c.expected == "repair" for c in mine) == n_rep
        assert sum(c.expected == "refuse" for c in mine) == n_ref
    assert len(_pooled(d)) == 3 + 2 + 0  # a: 3 repair + 2 refuse pooled; b: none
    out = capsys.readouterr().out
    assert "a" in out and "pooled" in out


def test_select_leaves_confirmed_untouched(tmp_path):
    d = _select_dir(tmp_path)
    before = {p.name: p.read_bytes() for p in d.glob("a-c*.json")}
    _sel(d)
    assert {p.name: p.read_bytes() for p in d.glob("a-c*.json")} == before
    assert not _pooled(d) & {"a-c0", "a-c1"}


def test_select_is_idempotent(tmp_path, capsys):
    d = _select_dir(tmp_path)
    _sel(d)
    snap = (_top(d), _pooled(d))
    capsys.readouterr()
    assert _sel(d) == 0
    assert (_top(d), _pooled(d)) == snap
    assert "pooled 0" in capsys.readouterr().out


def test_select_orders_by_sha256_of_id(tmp_path):
    d = tmp_path / "cases"
    ids = [f"ord-{i}" for i in range(6)]
    for i in ids:
        save_case(_mk(i, "o"), d)
    assert cli.main(["select", "--cases", str(d), "--repair-per-api", "3"]) == 0
    expect = set(sorted(ids, key=_hash_key)[:3])
    assert _top(d) == expect
    assert _pooled(d) == set(ids) - expect
    assert expect != set(ids[:3])  # the test would be vacuous if hash order == name order


def test_select_never_reads_run_results(tmp_path):
    d = _select_dir(tmp_path)
    out = tmp_path / "results"
    out.mkdir()
    (out / "junk.json").write_text("not json")
    assert _sel(d, "--cases", str(d)) == 0  # no --out option, results dir is never consulted


def test_select_warns_when_one_api_exceeds_40_percent(tmp_path, capsys):
    d = _select_dir(tmp_path)  # kept: a 3, b 2 -> a is 60%
    assert _sel(d) == 0
    cap = capsys.readouterr()
    assert "WARNING" in cap.out + cap.err and "a" in (cap.out + cap.err)
    assert "60.0%" in cap.out


def test_pool_is_excluded_from_load_cases_and_review_render(tmp_path):
    d = _select_dir(tmp_path)
    _sel(d)
    pooled = _pooled(d)
    assert pooled and not (_top(d) & pooled)
    review = tmp_path / "REVIEW.md"
    assert cli.main(["review", "render", "--cases", str(d), "--review", str(review)]) == 0
    text = review.read_text(encoding="utf-8")
    assert all(f"`{i}`" not in text for i in pooled)
    assert all(f"`{c.id}`" in text for c in load_cases(d) if c.label_status == "drafted")
