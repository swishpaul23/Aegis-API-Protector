from pathlib import Path

import pytest

from evals.real_api.cases import Case, ExpectedMap, load_cases, save_case
from evals.real_api.review import apply_review, render_review

_SRC = {"repo": "https://x/y.git", "old_ref": "a1", "new_ref": "b2", "spec_path": "o.yaml"}


def _case(cid: str, **kw) -> Case:
    base = dict(
        id=cid,
        api="api",
        source=_SRC,
        kind="rename",
        old_fields={"a": "string", "c": "integer"},
        new_fields={"b": "string", "d": "string"},
        expected="repair",
        maps=[
            ExpectedMap(target="a", source="b"),
            ExpectedMap(target="c", source="d", convert="int_str"),
        ],
    )
    base.update(kw)
    return Case(**base)


def _ambiguous(cid: str) -> Case:
    return _case(cid, ambiguous=True, maps=[], notes="a->b 0.9 | a->d 0.8")


def _setup(tmp_path: Path, *cases: Case) -> tuple[Path, Path]:
    d = tmp_path / "cases"
    for c in cases:
        save_case(c, d)
    return d, tmp_path / "REVIEW.md"


def _edit(review: Path, cid: str, *, confirm=False, fix_mark=False, fix: str = "") -> None:
    out = []
    for line in review.read_text(encoding="utf-8").splitlines():
        if f"`{cid}`" in line:
            cells = line.split(" | ")
            cells = [
                c.replace("[ ] confirm", "[x] confirm") if confirm else c for c in cells
            ]
            cells = [c.replace("[ ] fix", "[x] fix") if fix_mark else c for c in cells]
            line = " | ".join(cells)
            if fix:
                assert line.rstrip().endswith("|")
                line = line.rstrip()[:-1].rstrip() + f" {fix} |"
        out.append(line)
    review.write_text("\n".join(out) + "\n", encoding="utf-8")


def _by_id(d: Path) -> dict[str, Case]:
    return {c.id: c for c in load_cases(d)}


def test_render_lists_every_drafted_case(tmp_path):
    d, rv = _setup(
        tmp_path,
        _case("z-one"),
        _ambiguous("a-two"),
        _case("m-done", label_status="confirmed"),
    )
    render_review(d, rv)
    text = rv.read_text(encoding="utf-8")
    assert "`z-one`" in text and "`a-two`" in text
    assert "m-done" not in text
    assert "a -> b" in text
    assert "0.9" in text  # ambiguous notes shown
    assert text.index("a-two") < text.index("z-one")  # sorted by id


def test_render_escapes_pipes(tmp_path):
    d, rv = _setup(tmp_path, _ambiguous("amb"))
    render_review(d, rv)
    assert "a->b 0.9 \\| a->d 0.8" in rv.read_text(encoding="utf-8")


def test_apply_confirms_only_ticked(tmp_path):
    d, rv = _setup(tmp_path, _case("one"), _case("two"))
    render_review(d, rv)
    _edit(rv, "one", confirm=True)
    res = apply_review(d, rv)
    cases = _by_id(d)
    assert cases["one"].label_status == "confirmed"
    assert cases["two"].label_status == "drafted"
    assert res["confirmed"] == ["one"] and res["n_confirmed"] == 1
    assert res["n_unchanged"] == 1


def test_apply_is_idempotent(tmp_path):
    d, rv = _setup(tmp_path, _case("one"), _case("two"))
    render_review(d, rv)
    _edit(rv, "one", confirm=True)
    first = apply_review(d, rv)
    snap = {c.id: c.model_dump() for c in load_cases(d)}
    second = apply_review(d, rv)
    assert snap == {c.id: c.model_dump() for c in load_cases(d)}
    assert first["n_confirmed"] == 1 and second["n_confirmed"] == 0


def test_fix_ticked_without_fix_is_needs_fix(tmp_path):
    d, rv = _setup(tmp_path, _case("one"))
    render_review(d, rv)
    _edit(rv, "one", fix_mark=True)
    res = apply_review(d, rv)
    assert res["needs_fix"] == ["one"]
    assert _by_id(d)["one"].label_status == "drafted"


def test_ambiguous_confirm_without_fix_is_rejected(tmp_path):
    d, rv = _setup(tmp_path, _ambiguous("amb"))
    render_review(d, rv)
    _edit(rv, "amb", confirm=True)
    res = apply_review(d, rv)
    assert res["rejected"] == ["amb"]
    assert _by_id(d)["amb"].label_status == "drafted"


def test_fix_sets_two_maps_and_confirms(tmp_path):
    d, rv = _setup(tmp_path, _ambiguous("amb"))
    render_review(d, rv)
    _edit(rv, "amb", fix="a<-b; c<-d:int_str")
    res = apply_review(d, rv)
    c = _by_id(d)["amb"]
    assert res["confirmed"] == ["amb"]
    assert c.label_status == "confirmed" and c.expected == "repair"
    assert [(m.target, m.source, m.convert) for m in c.maps] == [
        ("a", "b", "none"),
        ("c", "d", "int_str"),
    ]


def test_fix_refuse(tmp_path):
    d, rv = _setup(tmp_path, _ambiguous("amb"))
    render_review(d, rv)
    _edit(rv, "amb", fix="REFUSE")
    apply_review(d, rv)
    c = _by_id(d)["amb"]
    assert (c.expected, c.maps, c.kind, c.label_status) == ("refuse", [], "unmappable", "confirmed")


def test_fix_bad_source_is_error_and_leaves_case(tmp_path):
    d, rv = _setup(tmp_path, _case("one"))
    before = _by_id(d)["one"]
    render_review(d, rv)
    _edit(rv, "one", fix="a<-nope")
    res = apply_review(d, rv)
    assert len(res["errors"]) == 1 and res["errors"][0].startswith("one")
    assert _by_id(d)["one"] == before


def test_fix_bad_convert_is_error(tmp_path):
    d, rv = _setup(tmp_path, _case("one"))
    render_review(d, rv)
    _edit(rv, "one", fix="a<-b:bogus")
    res = apply_review(d, rv)
    assert res["n_errors"] == 1
    assert _by_id(d)["one"].label_status == "drafted"


def test_rows_matched_by_id_not_position(tmp_path):
    d, rv = _setup(tmp_path, _case("one"), _case("two"))
    render_review(d, rv)
    _edit(rv, "two", confirm=True)
    lines = rv.read_text(encoding="utf-8").splitlines()
    rows = [i for i, ln in enumerate(lines) if "`one`" in ln or "`two`" in ln]
    lines[rows[0]], lines[rows[1]] = lines[rows[1]], lines[rows[0]]
    rv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    apply_review(d, rv)
    cases = _by_id(d)
    assert cases["two"].label_status == "confirmed" and cases["one"].label_status == "drafted"


def _rewrite(review: Path, cid: str, fn) -> None:
    lines = review.read_text(encoding="utf-8").splitlines()
    lines = [fn(ln) if f"`{cid}`" in ln else ln for ln in lines]
    review.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_unescaped_pipe_in_fix_is_reported(tmp_path):
    d, rv = _setup(tmp_path, _case("one"))
    render_review(d, rv)
    _rewrite(rv, "one", lambda ln: ln.rstrip()[:-1] + " a<-b | x |")
    res = apply_review(d, rv)
    assert res["n_malformed"] == 1 and res["errors"] == ["one: malformed row"]
    assert _by_id(d)["one"].label_status == "drafted"


def test_deleted_trailing_pipe_is_reported(tmp_path):
    d, rv = _setup(tmp_path, _case("one"))
    render_review(d, rv)
    _edit(rv, "one", confirm=True)
    _rewrite(rv, "one", lambda ln: ln.rstrip()[:-1].rstrip())
    res = apply_review(d, rv)
    assert res["n_malformed"] == 1 and res["n_errors"] == 1
    assert _by_id(d)["one"].label_status == "drafted"


def test_deleted_row_is_missing(tmp_path):
    d, rv = _setup(tmp_path, _case("one"), _case("two"))
    render_review(d, rv)
    lines = [ln for ln in rv.read_text(encoding="utf-8").splitlines() if "`two`" not in ln]
    rv.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = apply_review(d, rv)
    assert res["missing"] == ["two"] and res["n_missing"] == 1


def test_stripped_id_backtick_is_malformed(tmp_path):
    d, rv = _setup(tmp_path, _case("one"))
    render_review(d, rv)
    _rewrite(rv, "one", lambda ln: ln.replace("`one`", "one"))
    res = apply_review(d, rv)
    assert res["n_malformed"] == 1 and res["missing"] == ["one"]


def test_render_apply_roundtrip_with_pipe_in_notes(tmp_path):
    d, rv = _setup(tmp_path, _ambiguous("amb"))
    render_review(d, rv)
    _edit(rv, "amb", fix="a<-b; c<-d:int_str")
    res = apply_review(d, rv)
    assert res["confirmed"] == ["amb"] and res["n_malformed"] == 0
    assert _by_id(d)["amb"].notes == "a->b 0.9 | a->d 0.8"


def test_escaped_pipe_in_fix_is_unescaped_then_errors(tmp_path):
    d, rv = _setup(tmp_path, _case("one"))
    render_review(d, rv)
    _edit(rv, "one", fix="a<-b\\|x")
    res = apply_review(d, rv)
    assert res["n_errors"] == 1 and res["n_malformed"] == 0
    assert _by_id(d)["one"].label_status == "drafted"


def test_duplicate_targets_error(tmp_path):
    d, rv = _setup(tmp_path, _case("one"))
    before = _by_id(d)["one"]
    render_review(d, rv)
    _edit(rv, "one", fix="a<-b; a<-d")
    res = apply_review(d, rv)
    assert res["n_errors"] == 1 and _by_id(d)["one"] == before


def test_contradictory_ticks_error(tmp_path):
    d, rv = _setup(tmp_path, _case("one"))
    before = _by_id(d)["one"]
    render_review(d, rv)
    _edit(rv, "one", confirm=True, fix_mark=True)
    res = apply_review(d, rv)
    assert res["errors"] == ["one: contradictory ticks"] and _by_id(d)["one"] == before


def _fix_errors(tmp_path: Path, case: Case, fix: str) -> list[str]:
    d, rv = _setup(tmp_path, case)
    before = _by_id(d)[case.id]
    render_review(d, rv)
    _edit(rv, case.id, fix=fix)
    res = apply_review(d, rv)
    assert _by_id(d)[case.id] == before  # case unchanged on error
    return res["errors"]


def test_partial_fix_leaving_removed_field_unmapped_is_error(tmp_path):
    errs = _fix_errors(tmp_path, _ambiguous("amb"), "a<-b")
    assert errs == ["amb: unmapped removed field: c"]


@pytest.mark.parametrize(
    "fix, msg",
    [
        ("a<-b; c<-d", "convert none needs equal types"),
        ("a<-b:int_str; c<-d:int_str", "convert int_str needs old integer, new string"),
        ("a<-b; c<-d:int_float", "convert int_float needs old integer, new number"),
        ("a<-b; c<-d:epoch_iso", "convert epoch_iso needs old datetime, new integer"),
    ],
)
def test_fix_convert_must_fit_types(tmp_path, fix, msg):
    errs = _fix_errors(tmp_path, _ambiguous("amb"), fix)
    assert len(errs) == 1 and msg in errs[0], errs


def test_fix_that_cannot_build_payloads_is_error(tmp_path):
    # new_fields has a prefix conflict, so the dry run raises
    case = _ambiguous("amb").model_copy(
        update={"new_fields": {"b": "string", "d": "string", "d.x": "string"}}
    )
    errs = _fix_errors(tmp_path, case, "a<-b; c<-d:int_str")
    assert len(errs) == 1 and errs[0].startswith("amb: dry run failed"), errs


def test_confirm_of_unrunnable_draft_is_error(tmp_path):
    case = _case("one", maps=[ExpectedMap(target="a", source="b")])  # c left unmapped
    d, rv = _setup(tmp_path, case)
    render_review(d, rv)
    _edit(rv, "one", confirm=True)
    res = apply_review(d, rv)
    assert res["errors"] == ["one: unmapped removed field: c"]
    assert _by_id(d)["one"].label_status == "drafted"
