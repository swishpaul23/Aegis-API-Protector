import os

import pytest

from evals.real_api import runner
from evals.real_api.tests.seed_cases import SEEDS

RENAME, MOVE, UNMAPPABLE = SEEDS


def test_rename_seed_repaired_by_rules(tmp_path):
    outcome, usage = runner.run_case(RENAME, None, tmp_path)
    assert outcome == "repaired"
    assert usage.calls == 0


def test_unmappable_seed_refused_rules_only(tmp_path):
    outcome, _ = runner.run_case(UNMAPPABLE, None, tmp_path)
    assert outcome == "correct_refusal"


def test_move_retype_seed_repaired_by_rules(tmp_path):
    outcome, _ = runner.run_case(MOVE, None, tmp_path)
    assert outcome == "repaired"


def test_agent_mode_requires_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    called = []
    monkeypatch.setattr(runner, "run_case", lambda *a, **k: called.append(1))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY not set"):
        runner.run_all(SEEDS, "agent", tmp_path)
    assert called == []


def test_run_leaves_no_store_in_cwd(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    runner.run_all(SEEDS, "rules", tmp_path / "work")
    assert not (cwd / ".aegis").exists()
    assert os.listdir(cwd) == []


def test_runner_is_deterministic(tmp_path):
    a = runner.run_all(SEEDS, "rules", tmp_path / "a")
    b = runner.run_all(SEEDS, "rules", tmp_path / "b")
    assert [o for _, o, _ in a] == [o for _, o, _ in b]


def test_coerced_retype_is_not_breaking(tmp_path):
    from evals.real_api.cases import Case, ExpectedMap

    case = Case(
        id="retype-coerced",
        api="seed",
        source={},
        kind="retype",
        old_fields={"id": "integer", "name": "string"},
        new_fields={"id": "string", "name": "string"},
        expected="repair",
        maps=[ExpectedMap(target="id", source="id", convert="int_str")],
    )
    outcome, _ = runner.run_case(case, None, tmp_path)
    assert outcome == "not_breaking"
