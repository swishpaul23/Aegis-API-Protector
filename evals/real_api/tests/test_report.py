import json

from aegis_healer.agent import Usage
from evals.real_api.cases import Case, ExpectedMap, split_of
from evals.real_api.report import HONESTY_NOTE, write_report

META = {"aegis_version": "0.9.9", "model": "test-model-x", "date": "2026-11-21"}


def _case(cid: str, status="confirmed", expected="repair", api="a", kind="rename") -> Case:
    maps = [ExpectedMap(target="name", source="n")] if expected == "repair" else []
    return Case(
        id=cid,
        api=api,
        source={},
        kind=kind,
        old_fields={"name": "string"},
        new_fields={"n": "string"},
        expected=expected,
        maps=maps,
        label_status=status,
    )


def _ids(want: str, k: int) -> list[str]:
    out, i = [], 0
    while len(out) < k:
        if split_of(f"c{i}") == want:
            out.append(f"c{i}")
        i += 1
    return out


U = Usage(calls=2, input_tokens=1000, output_tokens=500)


def test_report_contains_headline_and_wrong_applied_ids(tmp_path):
    t1, t2, t3, t4 = _ids("test", 4)
    cases = [_case(t1), _case(t2), _case(t3), _case(t4, expected="refuse")]
    rules = [
        (cases[0], "repaired", Usage(), None),
        (cases[1], "safe_deferral", Usage(), None),
        (cases[2], "wrong_applied", Usage(), None),
        (cases[3], "correct_refusal", Usage(), None),
    ]
    agent = [
        (cases[0], "repaired", U, None),
        (cases[1], "repaired", U, None),
        (cases[2], "wrong_applied", U, None),
        (cases[3], "correct_refusal", U, None),
    ]
    jp, mp = write_report(rules, agent, tmp_path, META)
    assert jp.name == "2026-11-21.json" and mp.name == "2026-11-21.md"
    md = mp.read_text()
    assert "50.0%" in md and "2/4" in md  # agent headline
    assert "25.0%" in md and "1/4" in md  # rules headline
    assert t3 in md  # wrongly applied id listed
    assert "test-model-x" in md and "0.9.9" in md
    assert HONESTY_NOTE in md
    assert "est_cost_usd" in md or "$" in md
    data = json.loads(jp.read_text())
    assert data["meta"]["model"] == "test-model-x"
    assert data["agent"]["headline"]["n"] == 4
    assert data["agent"]["wrong_applied_ids"] == [t3]
    assert data["agent"]["usage"]["input_tokens"] == 4000


def test_report_headline_counts_only_confirmed_test_split(tmp_path):
    (t1,) = _ids("test", 1)
    (d1,) = _ids("dev", 1)
    (t2,) = _ids("test", 2)[1:]
    rows = [
        (_case(t1), "repaired", Usage(), None),
        (_case(d1), "repaired", Usage(), None),  # dev split
        (_case(t2, status="drafted"), "repaired", Usage(), None),  # drafted
    ]
    jp, _ = write_report(rows, rows, tmp_path, META)
    data = json.loads(jp.read_text())
    assert data["rules"]["headline"]["n"] == 1
    assert data["rules"]["dev"]["n"] == 2


def test_zero_confirmed_cases_writes_files_without_dividing(tmp_path):
    jp, mp = write_report([], [], tmp_path, META)
    assert jp.exists()
    assert "no confirmed test cases" in mp.read_text().lower()


def test_usage_sections_are_labelled_by_scope(tmp_path):
    (t1,) = _ids("test", 1)
    (d1,) = _ids("dev", 1)
    rows = [(_case(t1), "repaired", U, None), (_case(d1), "repaired", U, None)]
    jp, mp = write_report(rows, rows, tmp_path, META)
    md = mp.read_text()
    assert "Headline rows only (confirmed test split)" in md
    assert "All cases incl. dev/unconfirmed" in md
    data = json.loads(jp.read_text())
    assert data["agent"]["headline_usage"]["input_tokens"] == 1000
    assert data["agent"]["usage"]["input_tokens"] == 2000


def test_not_breaking_shown_in_every_table_and_excluded_from_n(tmp_path):
    t1, t2, t3 = _ids("test", 3)
    (d1,) = _ids("dev", 1)
    rows = [
        (_case(t1), "repaired", Usage(), None),
        (_case(t2), "not_breaking", Usage(), None),
        (_case(t3), "safe_deferral", Usage(), None),
        (_case(d1), "not_breaking", Usage(), None),
    ]
    jp, mp = write_report(rows, rows, tmp_path, META)
    data = json.loads(jp.read_text())
    assert data["rules"]["headline"]["n"] == 2
    assert data["rules"]["headline"]["counts"]["not_breaking"] == 1
    md = mp.read_text()
    assert "50.0% (1/2)" in md
    tables = [b for b in md.split("### ")[1:] if "|---" in b]
    assert len(tables) >= 4
    for t in tables:
        assert "not_breaking" in t or "Not breaking" in t, t


def test_crashed_cases_listed_with_reason(tmp_path):
    (t1,) = _ids("test", 1)
    (d1,) = _ids("dev", 1)
    rows = [
        (_case(t1), "crashed", Usage(), "ValueError: boom"),
        (_case(d1), "crashed", Usage(), "KeyError: x"),
    ]
    jp, mp = write_report(rows, [], tmp_path, META)
    md = mp.read_text()
    assert f"{t1} (ValueError: boom)" in md
    assert f"{d1} (KeyError: x)" in md
    data = json.loads(jp.read_text())
    assert data["rules"]["crashed_ids"] == [t1]
    assert data["rules"]["dev_crashed_ids"] == [d1]
    assert data["rules"]["errors"] == {t1: "ValueError: boom", d1: "KeyError: x"}


def test_agent_error_counts_in_n_and_is_listed(tmp_path):
    t1, t2 = _ids("test", 2)
    agent = [
        (_case(t1), "repaired", U, None),
        (_case(t2), "agent_error", Usage(calls=3), "AuthenticationError: 401"),
    ]
    jp, mp = write_report([], agent, tmp_path, META)
    data = json.loads(jp.read_text())
    assert data["agent"]["headline"]["n"] == 2
    assert data["agent"]["headline"]["repair_rate"] == 0.5
    assert data["agent"]["agent_error_ids"] == [t2]
    assert f"{t2} (AuthenticationError: 401)" in mp.read_text()


def _headline_lines(md: str) -> list[str]:
    return [ln for ln in md.splitlines() if ln.startswith(("**Rules only", "**Rules + agent"))]


def test_rules_only_run_headline_says_agent_not_run(tmp_path):
    t1, t2 = _ids("test", 2)
    rules = [(_case(t1), "repaired", Usage(), None), (_case(t2), "safe_deferral", Usage(), None)]
    _, mp = write_report(rules, [], tmp_path, {**META, "modes_run": "rules"})
    md = mp.read_text()
    lines = _headline_lines(md)
    assert len(lines) == 2, lines
    assert "50.0% (1/2)" in lines[0] and "2 confirmed" in lines[0]
    assert lines[1] == "**Rules + agent: not run.**"
    assert "0.0% (0/0)" not in md
    assert "not run" in md.split("### Rules only vs rules + agent")[1].split("###")[0]


def test_agent_only_run_headline_says_rules_not_run(tmp_path):
    (t1,) = _ids("test", 1)
    agent = [(_case(t1), "repaired", U, None)]
    _, mp = write_report([], agent, tmp_path, {**META, "modes_run": "agent"})
    lines = _headline_lines(mp.read_text())
    assert lines[0] == "**Rules only: not run.**"
    assert "100.0% (1/1)" in lines[1]


def test_headline_lists_not_breaking_separately(tmp_path):
    t1, t2 = _ids("test", 2)
    rows = [(_case(t1), "repaired", Usage(), None), (_case(t2), "not_breaking", Usage(), None)]
    _, mp = write_report(rows, rows, tmp_path, {**META, "modes_run": "rules, agent"})
    lines = _headline_lines(mp.read_text())
    assert len(lines) == 2
    for ln in lines:
        assert "100.0% (1/1)" in ln and "1 not breaking" in ln
