import os

import pytest
from fakes import FakeProposer, m

from aegis_healer.agent import AgentError, Usage
from aegis_healer.types import MappingPatch
from evals.real_api import runner
from evals.real_api.cases import Case, ExpectedMap
from evals.real_api.tests.seed_cases import SEEDS

RENAME, MOVE, UNMAPPABLE = SEEDS


def test_rename_seed_repaired_by_rules(tmp_path):
    outcome, usage, err = runner.run_case(RENAME, None, tmp_path)
    assert err is None
    assert outcome == "repaired"
    assert usage.calls == 0


def test_unmappable_seed_refused_rules_only(tmp_path):
    outcome, _, _ = runner.run_case(UNMAPPABLE, None, tmp_path)
    assert outcome == "correct_refusal"


def test_move_retype_seed_repaired_by_rules(tmp_path):
    outcome, _, _ = runner.run_case(MOVE, None, tmp_path)
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
    assert [r[1] for r in a] == [r[1] for r in b]


def test_coerced_retype_is_not_breaking(tmp_path):
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
    outcome, _, _ = runner.run_case(case, None, tmp_path)
    assert outcome == "not_breaking"


def _unbuildable() -> Case:
    # convert int_str on a string field: passes the schema, fails payload building
    return Case(
        id="bad-build",
        api="seed",
        source={},
        kind="retype",
        old_fields={"a": "string"},
        new_fields={"a": "integer"},
        expected="repair",
        maps=[ExpectedMap(target="a", source="a", convert="int_str")],
    )


def test_unbuildable_case_is_crashed_with_reason(tmp_path):
    outcome, usage, err = runner.run_case(_unbuildable(), None, tmp_path)
    assert outcome == "crashed" and usage.calls == 0
    assert "int_str" in err


def test_one_bad_case_does_not_abort_run(tmp_path):
    rows = runner.run_all([_unbuildable(), *SEEDS], "rules", tmp_path)
    assert [r[1] for r in rows] == ["crashed", "repaired", "repaired", "correct_refusal"]
    assert rows[0][3] and all(r[3] is None for r in rows[1:])


# --- agent mode -------------------------------------------------------------------------

SEMANTIC = Case(
    id="semantic-rename",
    api="seed",
    source={},
    kind="rename",
    old_fields={"id": "integer", "surname": "string"},
    new_fields={"id": "integer", "family_name": "string"},
    expected="repair",
    maps=[ExpectedMap(target="surname", source="family_name")],
)


@pytest.fixture
def agent_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    calls = []
    monkeypatch.setattr(runner, "preflight_agent", lambda: calls.append(1))
    return calls


def test_agent_mode_end_to_end_with_fake_proposer(tmp_path, agent_env):
    fake = FakeProposer(MappingPatch(mappings=[m("family_name", "surname")]))
    rules = runner.run_all([SEMANTIC], "rules", tmp_path / "r")
    rows = runner.run_all([SEMANTIC], "agent", tmp_path / "a", proposer=fake)
    assert rules[0][1] == "safe_deferral"  # rules alone cannot do a semantic rename
    assert agent_env == [1]  # preflight ran before the agent pass
    ((case, outcome, usage, err),) = rows
    assert (outcome, err) == ("repaired", None)
    assert usage.calls == 1 and usage.input_tokens == 1000
    assert fake.requests and fake.requests[0].unresolved == ["surname"]


def test_agent_mode_preflight_failure_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    def boom():
        raise RuntimeError("agent preflight failed: 401")

    called = []
    monkeypatch.setattr(runner, "preflight_agent", boom)
    monkeypatch.setattr(runner, "run_case", lambda *a, **k: called.append(1))
    with pytest.raises(RuntimeError, match="preflight"):
        runner.run_all([SEMANTIC], "agent", tmp_path, proposer=FakeProposer(MappingPatch()))
    assert called == []


class OutageProposer:
    """Mimics AgentProposer during an API outage: calls counted, no tokens, AgentError."""

    def __init__(self):
        self.last_usage = Usage()

    def propose(self, request, check, budget=None):
        self.last_usage = Usage(calls=3)
        raise AgentError(["AuthenticationError: 401 invalid x-api-key"])


def _copies(case: Case, k: int) -> list[Case]:
    return [case.model_copy(update={"id": f"{case.id}-{i}"}) for i in range(k)]


def test_zero_token_agent_failure_is_agent_error(tmp_path, agent_env):
    refuse = Case(
        id="refuse",
        api="seed",
        source={},
        kind="unmappable",
        old_fields={"id": "integer", "legacy": "string"},
        new_fields={"id": "integer", "other": "string"},
        expected="refuse",
        maps=[],
    )
    rows = runner.run_all([SEMANTIC, refuse], "agent", tmp_path, proposer=OutageProposer())
    assert [r[1] for r in rows] == ["agent_error", "agent_error"]  # not deferral / refusal
    assert all("401" in r[3] for r in rows)


def test_three_agent_errors_are_tolerated_four_abort(tmp_path, agent_env):
    ok = runner.run_all(_copies(SEMANTIC, 3), "agent", tmp_path / "a", proposer=OutageProposer())
    assert [r[1] for r in ok] == ["agent_error"] * 3
    with pytest.raises(runner.AgentOutage, match="agent_error"):
        runner.run_all(_copies(SEMANTIC, 5), "agent", tmp_path / "b", proposer=OutageProposer())


def test_rules_mode_never_preflights(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "preflight_agent", lambda: pytest.fail("preflight in rules"))
    runner.run_all([SEMANTIC], "rules", tmp_path)


def test_preflight_agent_calls_model_and_wraps_errors(monkeypatch):
    import langchain_anthropic

    seen = {}

    class Fake:
        def __init__(self, **kw):
            seen.update(kw)

        def invoke(self, msg):
            seen["msg"] = msg
            if seen.get("fail"):
                raise ValueError("529 overloaded")
            return "pong"

    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", Fake)
    monkeypatch.setenv("AEGIS_MODEL", "claude-test-model")
    runner.preflight_agent()
    assert seen["model"] == "claude-test-model" and seen["max_tokens"] <= 16
    assert seen["msg"] == "ping"
    seen["fail"] = True
    with pytest.raises(RuntimeError, match="529 overloaded"):
        runner.preflight_agent()
