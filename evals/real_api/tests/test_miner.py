import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evals.real_api.cases import Case
from evals.real_api.miner.candidates import mine
from evals.real_api.miner.diff import diff_schema
from evals.real_api.miner.sources import SOURCES, Source
from evals.real_api.miner.specs import MAX_DEPTH, fetch_spec, flatten_schemas
from evals.real_api.payloads import make_new_payload, make_old_records

FIX = Path(__file__).parent / "fixtures"
SPEC_PATH = "spec.json"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args], check=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    shutil.copy(FIX / "spec_old.json", r / SPEC_PATH)
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "old")
    _git(r, "tag", "old")
    shutil.copy(FIX / "spec_new.json", r / SPEC_PATH)
    _git(r, "commit", "-qam", "new")
    _git(r, "tag", "new")
    return r


@pytest.fixture
def cases(repo: Path) -> list[Case]:
    src = Source("fx", "https://example.invalid/fx", SPEC_PATH, [("old", "new")])
    return mine(src, repo)


def _by_schema(cases: list[Case], schema: str) -> list[Case]:
    return [c for c in cases if c.source["schema"] == schema]


def test_fetch_spec_reads_ref(repo: Path):
    old = fetch_spec(repo, "old", SPEC_PATH)
    new = fetch_spec(repo, "HEAD", SPEC_PATH)
    assert "full_name" in old["components"]["schemas"]["Widget"]["properties"]
    assert "fullname" in new["components"]["schemas"]["Widget"]["properties"]
    assert old == json.loads((FIX / "spec_old.json").read_text())


def test_flatten_resolves_refs_and_skips_cycles():
    flat = flatten_schemas(json.loads((FIX / "spec_old.json").read_text()))
    w = flat["Widget"]
    assert w["owner.login"] == "string"
    assert w["updated_at"] == "datetime"
    assert w["id"] == "integer" and w["legacy_flag"] == "boolean"
    assert not any(p.startswith(("tags", "extra")) for p in w)  # arrays of objects, oneOf
    node = flat["Node"]  # self-reference terminates
    assert node["name"] == "string"
    assert max(p.count(".") for p in node) <= MAX_DEPTH
    assert "parent.name" not in node


def test_diff_finds_removed_added_retyped():
    flat_o = flatten_schemas(json.loads((FIX / "spec_old.json").read_text()))
    flat_n = flatten_schemas(json.loads((FIX / "spec_new.json").read_text()))
    d = diff_schema(flat_o["Widget"], flat_n["Widget"])
    assert set(d.removed) == {"full_name", "legacy_flag", "owner.login"}
    assert set(d.added) == {"fullname", "author.login"}
    assert d.retyped == {"id": ("integer", "string"), "updated_at": ("datetime", "integer")}


def test_mine_pairs_rename_and_move(cases: list[Case]):
    w = {
        (c.kind, tuple((m.target, m.source, m.convert) for m in c.maps)): c
        for c in _by_schema(cases, "Widget")
    }
    assert ("rename", (("full_name", "fullname", "none"),)) in w
    assert ("move", (("owner.login", "author.login", "none"),)) in w
    assert ("retype", (("id", "id", "int_str"),)) in w
    assert ("retype", (("updated_at", "updated_at", "epoch_iso"),)) in w
    for c in w.values():
        if c.expected == "repair":
            assert not c.ambiguous
            make_new_payload(c, make_old_records(c))  # consumable


def test_mine_flags_ambiguous_pairing(cases: list[Case]):
    contact = _by_schema(cases, "Contact")
    assert len(contact) == 1
    assert contact[0].ambiguous is True
    assert contact[0].expected == "repair"


def test_mine_emits_unmappable_for_orphan_removed_field(cases: list[Case]):
    un = [c for c in _by_schema(cases, "Widget") if c.kind == "unmappable"]
    assert len(un) == 1
    assert un[0].expected == "refuse" and un[0].maps == []
    assert "legacy_flag" in un[0].old_fields


def test_all_mined_cases_are_drafted(cases: list[Case]):
    assert cases
    assert {c.label_status for c in cases} == {"drafted"}
    assert not _by_schema(cases, "Stable") and not _by_schema(cases, "Node")
    ids = [c.id for c in cases]
    assert len(set(ids)) == len(ids)
    assert all(i.startswith("fx-") for i in ids)
    assert "fx-Contact-1" in ids
    assert cases[0].source["old_ref"] == "old" and cases[0].source["schema"]


def test_sources_have_repo_and_spec_path():
    assert {s.api for s in SOURCES} >= {"github", "stripe"}
    assert all(s.repo_url.startswith("https://github.com/") and s.spec_path for s in SOURCES)
