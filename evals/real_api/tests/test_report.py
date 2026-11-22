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
        (cases[0], "repaired", Usage()),
        (cases[1], "safe_deferral", Usage()),
        (cases[2], "wrong_applied", Usage()),
        (cases[3], "correct_refusal", Usage()),
    ]
    agent = [
        (cases[0], "repaired", U),
        (cases[1], "repaired", U),
        (cases[2], "wrong_applied", U),
        (cases[3], "correct_refusal", U),
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
        (_case(t1), "repaired", Usage()),
        (_case(d1), "repaired", Usage()),  # dev split
        (_case(t2, status="drafted"), "repaired", Usage()),  # drafted
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
    rows = [(_case(t1), "repaired", U), (_case(d1), "repaired", U)]
    jp, mp = write_report(rows, rows, tmp_path, META)
    md = mp.read_text()
    assert "Headline rows only (confirmed test split)" in md
    assert "All cases incl. dev/unconfirmed" in md
    data = json.loads(jp.read_text())
    assert data["agent"]["headline_usage"]["input_tokens"] == 1000
    assert data["agent"]["usage"]["input_tokens"] == 2000
