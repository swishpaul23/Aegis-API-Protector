import json
import shutil
import subprocess
import time
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
    c = contact[0]
    assert c.ambiguous is True
    assert c.expected == "repair"
    assert c.maps == []
    assert {"phone_a", "phone_b"} <= set(c.old_fields)
    assert {"phone_c", "phone_d"} <= set(c.new_fields)
    for edge in ("phone_a->{phone_c:", "phone_b->{phone_c:", ",phone_d:"):
        assert edge in c.notes
    assert c.notes.count("phone_c:") == 2 and c.notes.count("phone_d:") == 2


def test_mine_two_removed_one_added_keeps_both_removed(tmp_path: Path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")

    def spec(props: dict) -> str:
        return json.dumps({"components": {"schemas": {"S": {"properties": props}}}})

    s = {"type": "string"}
    (r / SPEC_PATH).write_text(spec({"phone_a": s, "phone_b": s}))
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "o")
    _git(r, "tag", "o")
    (r / SPEC_PATH).write_text(spec({"phone_c": s}))
    _git(r, "commit", "-qam", "n")
    out = mine(Source("fx", "u", SPEC_PATH, [("o", "HEAD")]), r)
    assert len(out) == 1 and out[0].ambiguous and out[0].maps == []
    assert set(out[0].old_fields) == {"phone_a", "phone_b"}
    assert set(out[0].new_fields) == {"phone_c"}


def test_ids_stable_under_pair_order(repo: Path):
    a = Source("fx", "u", SPEC_PATH, [("old", "new"), ("old", "HEAD")])
    b = Source("fx", "u", SPEC_PATH, [("old", "HEAD"), ("old", "new")])
    ids_a = [c.id for c in mine(a, repo)]
    assert len(set(ids_a)) == len(ids_a)
    assert sorted(ids_a) == sorted(c.id for c in mine(b, repo))


def test_fetch_spec_rejects_option_like_ref(repo: Path):
    with pytest.raises(ValueError):
        fetch_spec(repo, "--output=/tmp/x", SPEC_PATH)


def test_flatten_fanout_is_bounded():
    n = 30
    schemas = {}
    for i in range(n):
        props = {f"s{j}": {"type": "string"} for j in range(10)}
        props.update(
            {f"r{j}": {"$ref": f"#/components/schemas/S{(i + j + 1) % n}"} for j in range(15)}
        )
        schemas[f"S{i}"] = {"type": "object", "properties": props}
    t = time.monotonic()
    flat = flatten_schemas({"components": {"schemas": schemas}})
    assert time.monotonic() - t < 2
    assert all(len(f) <= 2000 for f in flat.values())
    assert flat["S0"]["r0.s0"] == "string"
    assert not any(p.count(".") > 1 for p in flat["S0"])  # refs are one hop


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
    assert any(i.startswith("fx-Contact-") and len(i) == len("fx-Contact-") + 8 for i in ids)
    assert cases[0].source["old_ref"] == "old" and cases[0].source["schema"]


def test_sources_have_repo_and_spec_path():
    assert {s.api for s in SOURCES} >= {"github", "stripe"}
    assert all(s.repo_url.startswith("https://github.com/") and s.spec_path for s in SOURCES)


WIDGET_ADDED = {"fullname", "author.login"}


def test_every_case_carries_all_same_schema_added_fields(cases: list[Case]):
    widget = _by_schema(cases, "Widget")
    assert len(widget) == 5
    for c in widget:
        assert WIDGET_ADDED <= set(c.new_fields), (c.kind, c.new_fields)
    un = next(c for c in widget if c.kind == "unmappable")
    assert set(un.new_fields) - set(un.old_fields) == WIDGET_ADDED


def test_mined_ambiguous_case_after_fix_runs_through_runner(cases: list[Case], tmp_path: Path):
    from evals.real_api import runner
    from evals.real_api.review import _apply_fix

    (contact,) = _by_schema(cases, "Contact")
    fixed = _apply_fix(contact, "phone_a<-phone_c; phone_b<-phone_d")
    new = make_new_payload(fixed, make_old_records(fixed))
    assert all({"phone_c", "phone_d"} <= set(n) for n in new)
    outcome, _, err = runner.run_case(fixed, None, tmp_path)
    assert err is None and outcome == "safe_deferral"  # rules alone cannot pick a/b


def test_mined_unmappable_case_runs_through_runner(cases: list[Case], tmp_path: Path):
    from evals.real_api import runner

    un = next(c for c in _by_schema(cases, "Widget") if c.kind == "unmappable")
    new = make_new_payload(un, make_old_records(un))
    assert all(WIDGET_ADDED <= _flat_keys(n) and "legacy_flag" not in n for n in new)
    outcome, _, err = runner.run_case(un, None, tmp_path)
    assert err is None and outcome == "correct_refusal"


def _flat_keys(rec: dict, prefix: str = "") -> set[str]:
    out: set[str] = set()
    for k, v in rec.items():
        p = f"{prefix}{k}"
        out |= _flat_keys(v, p + ".") if isinstance(v, dict) else {p}
    return out


def _two_ref_repo(tmp_path: Path, old: dict, new: dict) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    (r / SPEC_PATH).write_text(json.dumps({"components": {"schemas": old}}))
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "o")
    _git(r, "tag", "o")
    (r / SPEC_PATH).write_text(json.dumps({"components": {"schemas": new}}))
    _git(r, "commit", "-qam", "n")
    return r


def _shared(owner_props: dict) -> dict:
    ref = {"$ref": "#/components/schemas/Owner"}
    s = {"type": "string"}
    return {
        "Owner": {"properties": {"id": {"type": "integer"}, **owner_props}},
        "Repo": {"properties": {"name": s, "owner": ref}},
        "Issue": {"properties": {"title": s, "user": ref}},
    }


def test_shared_component_rename_is_one_case(tmp_path: Path):
    s = {"type": "string"}
    r = _two_ref_repo(tmp_path, _shared({"login_name": s}), _shared({"loginName": s}))
    out = mine(Source("fx", "u", SPEC_PATH, [("o", "HEAD")]), r)
    assert len(out) == 1, [(c.source["schema"], c.maps) for c in out]
    (c,) = out
    assert c.source["schema"] == "Owner"  # the defining component: shortest paths
    assert [(m.target, m.source) for m in c.maps] == [("login_name", "loginName")]


def test_unrelated_changes_in_two_schemas_are_kept(tmp_path: Path):
    s, i = {"type": "string"}, {"type": "integer"}
    old = {"A": {"properties": {"full_name": s}}, "B": {"properties": {"user_count": i}}}
    new = {"A": {"properties": {"fullname": s}}, "B": {"properties": {"userCount": i}}}
    r = _two_ref_repo(tmp_path, old, new)
    out = mine(Source("fx", "u", SPEC_PATH, [("o", "HEAD")]), r)
    assert sorted(c.source["schema"] for c in out) == ["A", "B"]


def test_sources_have_pairs_and_safe_refs():
    assert SOURCES
    for s in SOURCES:
        assert s.pairs, f"{s.api} {s.spec_path}: no pairs"
        for ref in (r for pair in s.pairs for r in pair):
            assert ref and not ref.startswith("-"), f"{s.api}: unsafe ref {ref!r}"
        assert not s.api.startswith("-") and not s.spec_path.startswith("-")
    assert len([s for s in SOURCES if s.api == "twilio"]) > 1
