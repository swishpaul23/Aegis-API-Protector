from types import SimpleNamespace

import pytest
from fakes import FakeProposer, FakeStructuredLLM, m, v2_agent_patch
from fixtures.models import User
from fixtures.payloads import V1, V2
from typer.testing import CliRunner

import aegis_healer.cli.check_cmd as check_mod
from aegis_healer import Healer
from aegis_healer.agent import AgentError, BudgetExceeded, ProposalRequest, Usage
from aegis_healer.agent.graph import AgentProposer
from aegis_healer.cli import app
from aegis_healer.store.files import FileStore
from aegis_healer.types import MappingPatch, PatchCheck

REQ = ProposalRequest(
    model_name="User",
    model_schema={"user_id": "int", "email": "str"},
    unresolved=["user_id"],
    candidates={"id": "str", "note": "str"},
    samples=[V2],
)
GOOD = MappingPatch(mappings=[m("id", "user_id", "to_int")])


def ok(_p):
    return PatchCheck(ok=True, errors=[])


def raw_result(parsed, error=None, usage=None):
    raw = SimpleNamespace(usage_metadata=usage or {"input_tokens": 1500, "output_tokens": 400})
    return {"raw": raw, "parsed": parsed, "parsing_error": error}


def test_usage_summed_and_costed():
    u = Usage(calls=2, input_tokens=2_000_000, output_tokens=1_000_000)
    assert u.est_cost_usd == pytest.approx(0.70)
    u.add(Usage(calls=1, input_tokens=5, output_tokens=7))
    assert (u.calls, u.input_tokens, u.output_tokens) == (3, 2_000_005, 1_000_007)


def test_agent_reads_usage_metadata():
    p = AgentProposer(structured_llm=FakeStructuredLLM([raw_result(GOOD)]))
    p.propose(REQ, ok)
    assert p.last_usage == Usage(calls=1, input_tokens=1500, output_tokens=400)


def test_missing_usage_metadata_counts_zero_tokens():
    llm = FakeStructuredLLM([{"raw": SimpleNamespace(), "parsed": GOOD, "parsing_error": None}])
    p = AgentProposer(structured_llm=llm)
    p.propose(REQ, ok)
    assert p.last_usage == Usage(calls=1)


def test_parsing_error_retries():
    llm = FakeStructuredLLM([raw_result(None, ValueError("bad json")), raw_result(GOOD)])
    p = AgentProposer(structured_llm=llm)
    out = p.propose(REQ, ok)
    assert out.mappings[0].target == "user_id"
    assert p.last_usage.calls == 2


def test_budget_checked_before_invoke():
    llm = FakeStructuredLLM([raw_result(None, ValueError("x"))])
    p = AgentProposer(structured_llm=llm, max_attempts=3)
    with pytest.raises(BudgetExceeded) as ei:
        p.propose(REQ, ok, budget=1)
    assert isinstance(ei.value, AgentError)
    assert len(llm.calls) == 1 and p.last_usage.calls == 1


def test_zero_budget_never_calls():
    llm = FakeStructuredLLM([raw_result(GOOD)])
    p = AgentProposer(structured_llm=llm)
    with pytest.raises(BudgetExceeded):
        p.propose(REQ, ok, budget=0)
    assert llm.calls == [] and p.last_usage.calls == 0


def test_failed_invoke_counts_as_call():
    class Boom:
        def invoke(self, messages):
            raise RuntimeError("api down")

    p = AgentProposer(structured_llm=Boom(), max_attempts=2)
    with pytest.raises(AgentError):
        p.propose(REQ, ok)
    assert p.last_usage.calls == 2


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path / ".aegis")


def test_budget_exceeded_mid_batch(store):
    other = {k: v for k, v in V1.items() if k != "email"} | {"zzq": "a@x.io"}
    fake = FakeProposer(
        MappingPatch(mappings=[m("id", "user_id", "to_int")]),
    )
    h = Healer(store=store, proposer=fake, max_llm_calls=1)
    r = h.validate([V2, other], User)
    assert len(fake.requests) >= 1
    assert r.status == "failed"
    assert "budget" in r.message.lower()
    assert store.active_version("fixtures.models.User") == 0
    assert r.usage.calls == 1


def test_zero_budget_fails_immediately(store):
    fake = FakeProposer(v2_agent_patch())
    r = Healer(store=store, proposer=fake, max_llm_calls=0).validate(V2, User)
    assert r.status == "failed" and "budget" in r.message.lower()
    assert fake.requests == [] and r.usage.calls == 0


def test_healresult_usage_reported(store):
    r = Healer(store=store, proposer=FakeProposer(v2_agent_patch())).validate(V2, User)
    assert r.status == "healed" and r.usage.calls >= 1


def test_usage_reported_when_pending(store):
    r = Healer(store=store, proposer=FakeProposer(v2_agent_patch(0.5))).validate(V2, User)
    assert r.status == "pending" and r.usage.calls == 1


def test_cli_prints_llm_line(tmp_path, monkeypatch):
    store = FileStore(tmp_path / ".aegis")
    monkeypatch.setattr(check_mod, "fetch_json", lambda url, headers: V2)
    seen = {}

    def build(threshold, db, persist=True, **kw):
        seen.update(kw)
        return Healer(
            store=store,
            proposer=FakeProposer(v2_agent_patch()),
            auto_apply_threshold=threshold,
            persist=persist,
            max_llm_calls=kw.get("max_llm_calls", 10),
        )

    monkeypatch.setattr(check_mod, "build_healer", build)
    r = CliRunner(env={"COLUMNS": "200"}).invoke(
        app, ["check", "http://x", "--model", "fixtures.models:User", "--max-llm-calls", "4"]
    )
    assert r.exit_code == 0, r.output
    assert "LLM: 1 call(s), 1000 in / 200 out tokens, ~$0.0002" in r.output
    assert seen["max_llm_calls"] == 4
