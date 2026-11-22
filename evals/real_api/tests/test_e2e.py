"""End to end through the CLI: spec history -> mine -> review -> run (rules + agent) -> report.

The agent is an oracle fake (no network). It knows one semantic rename the rules cannot do,
and it deliberately maps the removed `legacy` field to the added distractor `is_active`, so
the report must show both agent lift and a wrongly applied fix.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fakes import FakeProposer, m

from aegis_healer.types import MappingPatch, Unmappable
from evals.real_api import __main__ as cli
from evals.real_api import runner
from evals.real_api.cases import load_cases, split_of
from evals.real_api.miner.sources import Source

S, INT, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
OLD = {
    "id": INT,
    "user_name": S,
    "full_name": S,
    "created_at": {"type": "string", "format": "date-time"},
    "count": INT,
    "first_name": S,
    "last_name": S,
    "legacy": B,
    "email": S,
}
NEW = {
    "id": INT,
    "userName": S,
    "name": S,
    "created_at": INT,
    "count": S,
    "firstName": S,
    "lastName": S,
    "is_active": B,
    "contact": {"type": "object", "properties": {"email": S}},
}

# What the oracle agent maps: one right semantic rename, one wrong distractor pick.
ORACLE = {"full_name": "name", "legacy": "is_active"}


class OracleProposer(FakeProposer):
    def __init__(self):
        super().__init__(MappingPatch())

    def propose(self, request, check, budget=None):
        maps = [m(ORACLE[t], t) for t in request.unresolved if ORACLE.get(t) in request.candidates]
        done = {x.target for x in maps}
        self.patch = MappingPatch(
            mappings=maps,
            unmappable=[
                Unmappable(target=t, reason="no candidate")
                for t in request.unresolved
                if t not in done
            ],
        )
        return super().propose(request, check, budget)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _spec(props: dict) -> str:
    return json.dumps(
        {"openapi": "3.0.0", "components": {"schemas": {"User": {"properties": props}}}}
    )


@pytest.fixture
def spec_repo(tmp_path: Path) -> Path:
    r = tmp_path / "upstream"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "spec.json").write_text(_spec(OLD))
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "old")
    _git(r, "tag", "old")
    (r / "spec.json").write_text(_spec(NEW))
    _git(r, "commit", "-qam", "new")
    _git(r, "tag", "new")
    return r


def _edit_review(review: Path, labels: dict[str, str]) -> None:
    """labels: case id -> "confirm" or a fix string."""
    out = []
    for line in review.read_text(encoding="utf-8").splitlines():
        for cid, label in labels.items():
            if line.startswith(f"| `{cid}`"):
                if label == "confirm":
                    line = line.replace("[ ] confirm", "[x] confirm")
                else:
                    line = line.rstrip()[:-1].rstrip() + f" {label} |"
        out.append(line)
    review.write_text("\n".join(out) + "\n", encoding="utf-8")


def _outcomes(run_dir: Path, mode: str) -> dict[str, str]:
    raw = json.loads((run_dir / f"raw-{mode}.json").read_text(encoding="utf-8"))
    return {r["id"]: r["outcome"] for r in raw["rows"]}


def test_mine_review_run_report_end_to_end(tmp_path, spec_repo, monkeypatch, capsys):
    cases_dir, review, out = tmp_path / "cases", tmp_path / "REVIEW.md", tmp_path / "results"
    src = Source("e2e", "https://example.invalid/e2e", "spec.json", [("old", "new")])
    monkeypatch.setattr(cli, "SOURCES", [src])
    monkeypatch.setattr(cli, "clone_repo", lambda url, dest: shutil.copytree(spec_repo, dest))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(runner, "preflight_agent", lambda: None)
    monkeypatch.setattr(runner, "make_agent_proposer", OracleProposer)

    # mine
    assert cli.main(["mine", "--cases", str(cases_dir)]) == 0
    mined = {c.id: c for c in load_cases(cases_dir)}
    by_target = {}
    for c in mined.values():
        key = c.maps[0].target if c.maps else ("ambiguous" if c.ambiguous else c.kind)
        by_target[key] = c.id
    assert set(by_target) == {
        "count",
        "created_at",
        "email",
        "first_name",
        "ambiguous",
        "unmappable",
    }
    unmappable = mined[by_target["unmappable"]]
    assert {"is_active", "name", "userName"} <= set(unmappable.new_fields)  # candidates

    # review: confirm the drafted pairings, fix the ambiguous group, keep the refusal
    assert cli.main(["review", "render", "--cases", str(cases_dir), "--review", str(review)]) == 0
    text = review.read_text(encoding="utf-8")
    assert "schema: User" in text and "added: " in text and "is_active:boolean" in text
    labels = {by_target[k]: "confirm" for k in ("count", "created_at", "email", "first_name")}
    labels[by_target["unmappable"]] = "confirm"
    labels[by_target["ambiguous"]] = "full_name<-name; last_name<-lastName; user_name<-userName"
    _edit_review(review, labels)
    assert cli.main(["review", "apply", "--cases", str(cases_dir), "--review", str(review)]) == 0
    assert {c.label_status for c in load_cases(cases_dir)} == {"confirmed"}

    # run both splits, both modes
    for split in ("dev", "test"):
        assert (
            cli.main(["run", "--split", split, "--cases", str(cases_dir), "--out", str(out)]) == 0
        )
    assert cli.main(["run", "--cases", str(cases_dir), "--out", str(out)]) == 2  # test ran once

    expect = {  # target -> (rules outcome, agent outcome)
        "count": ("not_breaking", "not_breaking"),  # "123" coerces to int: Aegis did nothing
        "created_at": ("not_breaking", "not_breaking"),  # int epoch coerces to datetime
        "email": ("repaired", "repaired"),  # same-leaf move: rules
        "first_name": ("repaired", "repaired"),  # style rename: rules
        "ambiguous": ("safe_deferral", "repaired"),  # full_name<-name needs the agent
        "unmappable": ("correct_refusal", "wrong_applied"),  # agent picked the distractor
    }
    test_ids = {k: v for k, v in by_target.items() if split_of(v) == "test"}
    assert test_ids, "fixture must put some cases in the test split"
    for split in ("dev", "test"):
        (run_dir,) = out.glob(f"*-{split}-1")
        rules, agent = _outcomes(run_dir, "rules"), _outcomes(run_dir, "agent")
        for key, cid in by_target.items():
            if split_of(cid) == split:
                assert (rules[cid], agent[cid]) == expect[key], key

    # headline: confirmed test split, not_breaking excluded from N
    (test_dir,) = out.glob("*-test-1")
    in_n = [k for k in test_ids if expect[k][0] != "not_breaking"]
    nb = len(test_ids) - len(in_n)
    n = len(in_n)
    assert (n, nb) == (3, 2)  # count + created_at coerce; 3 real breaking changes
    r_rep = sum(expect[k][0] == "repaired" for k in in_n)
    a_rep = sum(expect[k][1] == "repaired" for k in in_n)
    a_wrong = sum(expect[k][1] == "wrong_applied" for k in in_n)
    data = json.loads(next(test_dir.glob("[0-9]*.json")).read_text())
    assert data["rules"]["headline"]["n"] == data["agent"]["headline"]["n"] == n
    assert data["agent"]["headline"]["counts"]["not_breaking"] == nb
    assert data["agent"]["wrong_applied_ids"] == sorted(
        by_target[k] for k in in_n if expect[k][1] == "wrong_applied"
    )
    md = next(test_dir.glob("*.md")).read_text(encoding="utf-8")

    def pct(k: int) -> str:
        return f"{k / n * 100:.1f}% ({k}/{n})"

    assert (
        f"**Rules only: repaired {pct(r_rep)} of {n} confirmed breaking test cases; "
        f"wrongly auto-applied {pct(0)}; {nb} not breaking (excluded from N).**"
    ) in md
    assert (
        f"**Rules + agent: repaired {pct(a_rep)} of {n} confirmed breaking test cases; "
        f"wrongly auto-applied {pct(a_wrong)}; {nb} not breaking (excluded from N).**"
    ) in md
    assert "Prior test runs: 0" in md
