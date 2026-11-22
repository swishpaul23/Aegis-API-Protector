"""Human review round-trip: render REVIEW.md for drafted cases, apply ticks and fixes back.

Row columns: id | api | kind | change | ambiguous | context | source | notes | [ ] confirm |
[ ] fix | fix. ``context`` shows the schema, the removed and added fields with their types,
and any retyped fields, so a label can be checked without opening the spec.
Tick a box by changing ``[ ]`` to ``[x]``. The fix column (last) accepts ``REFUSE`` or maps
``target<-source`` / ``target<-source:convert`` separated by ``;``.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import ValidationError

from evals.real_api.cases import Case, ExpectedMap, load_cases, save_case
from evals.real_api.payloads import build_model, make_new_payload, make_old_records

COLUMNS = (
    "id",
    "api",
    "kind",
    "change",
    "ambiguous",
    "context",
    "source",
    "notes",
    "confirm",
    "fix?",
    "fix",
)
N_CELLS = len(COLUMNS)
HEADER = "| " + " | ".join(COLUMNS) + " |\n" + "|---" * N_CELLS + "|"
_INTRO = (
    "# Case review\n\n"
    "Tick `[x] confirm` to accept a row as shown. Tick `[x] fix` only if you will fill the last\n"
    "column. A filled fix column confirms the case by itself. Fix syntax: `REFUSE`, or maps\n"
    "`target<-source` / `target<-source:convert` separated by `;` (convert: none, int_str,\n"
    "int_float, epoch_iso). Ambiguous rows cannot be confirmed as-is; they need a fix.\n\n"
)
_SPLIT = re.compile(r"(?<!\\)\|")
_TICK = re.compile(r"^\[[xX]\]")


def _esc(s: str) -> str:
    return " ".join(s.replace("|", "\\|").split())


def _change(case: Case) -> str:
    if case.expected == "refuse":
        return "REFUSE"
    if not case.maps:
        return "(none)"
    parts = []
    for m in case.maps:
        conv = "" if m.convert == "none" else f" ({m.convert})"
        parts.append(f"{m.target} -> {m.source}{conv}")
    return "; ".join(parts)


def _context(case: Case) -> str:
    old, new = case.old_fields, case.new_fields
    removed = ", ".join(f"{p}:{t}" for p, t in sorted(old.items()) if p not in new)
    added = ", ".join(f"{p}:{t}" for p, t in sorted(new.items()) if p not in old)
    retyped = ", ".join(
        f"{p}:{t}->{new[p]}" for p, t in sorted(old.items()) if p in new and new[p] != t
    )
    parts = [f"schema: {case.source.get('schema', '?')}"]
    parts.append(f"removed: {removed or '(none)'}")
    parts.append(f"added: {added or '(none)'}")
    if retyped:
        parts.append(f"retyped: {retyped}")
    return "; ".join(parts)


def _source(case: Case) -> str:
    s = case.source
    refs = f"{s.get('old_ref', '')}..{s.get('new_ref', '')}"
    return f"{s.get('repo', '')} {refs} {s.get('spec_path', '')}"


def render_review(cases_dir: Path, out: Path) -> None:
    """Write REVIEW.md with one row per drafted case, sorted by id."""
    rows = []
    for c in sorted(load_cases(cases_dir), key=lambda c: c.id):
        if c.label_status != "drafted":
            continue
        cells = [
            f"`{c.id}`",
            c.api,
            c.kind,
            _change(c),
            "AMBIGUOUS" if c.ambiguous else "",
            _context(c),
            _source(c),
            c.notes if c.ambiguous else "",
        ]
        rows.append("| " + " | ".join(_esc(x) for x in cells) + " | [ ] confirm | [ ] fix |  |")
    Path(out).write_text(_INTRO + HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


_ID = re.compile(r"^\|\s*`([^`]+)`")


def _parse_rows(
    review: Path,
) -> tuple[dict[str, tuple[bool, bool, str]], list[str]]:
    """Return parsed rows by id, plus raw data-row lines that failed to parse."""
    rows: dict[str, tuple[bool, bool, str]] = {}
    malformed: list[str] = []
    for line in Path(review).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in _SPLIT.split(line)[1:-1]]
        if cells and (cells[0] == "id" or set(cells[0]) <= {"-", ":"}):
            continue  # header or separator
        m = re.fullmatch(r"`(.+)`", cells[0]) if cells else None
        if len(cells) != N_CELLS or not line.endswith("|") or not m or m.group(1) in rows:
            malformed.append(line)
            continue
        rows[m.group(1)] = (
            bool(_TICK.match(cells[N_CELLS - 3])),
            bool(_TICK.match(cells[N_CELLS - 2])),
            cells[N_CELLS - 1].replace("\\|", "|"),
        )
    return rows, malformed


def _parse_fix(text: str) -> tuple[str, list[ExpectedMap]]:
    if text.strip().upper() == "REFUSE":
        return "refuse", []
    maps = []
    for part in (p.strip() for p in text.split(";")):
        if not part:
            continue
        if "<-" not in part:
            raise ValueError(f"bad map {part!r}, expected target<-source[:convert]")
        target, _, rest = part.partition("<-")
        source, _, convert = rest.partition(":")
        maps.append(
            ExpectedMap(
                target=target.strip(), source=source.strip(), convert=convert.strip() or "none"
            )
        )
    if not maps:
        raise ValueError("fix has no maps")
    return "repair", maps


# convert -> (old type, new type) it requires; None = types must be equal
_CONVERT_TYPES: dict[str, tuple[str, str] | None] = {
    "none": None,
    "int_str": ("integer", "string"),
    "int_float": ("integer", "number"),
    "epoch_iso": ("datetime", "integer"),
}


def _check_runnable(case: Case) -> None:
    """Raise ValueError unless the label is achievable and the eval can build the case."""
    if case.expected == "repair":
        targets = {m.target for m in case.maps}
        for path in case.old_fields:
            if path not in case.new_fields and path not in targets:
                raise ValueError(f"unmapped removed field: {path}")
        for m in case.maps:
            if m.target not in case.old_fields or m.source not in case.new_fields:
                continue  # reported below by make_new_payload / the caller
            old_t, new_t = case.old_fields[m.target], case.new_fields[m.source]
            need = _CONVERT_TYPES[m.convert]
            if need is None and old_t != new_t:
                raise ValueError(
                    f"map {m.target}<-{m.source}: convert none needs equal types "
                    f"({old_t} -> {new_t})"
                )
            if need is not None and (old_t, new_t) != need:
                raise ValueError(
                    f"map {m.target}<-{m.source}: convert {m.convert} needs old {need[0]}, "
                    f"new {need[1]} (got {old_t} -> {new_t})"
                )
    try:
        model = build_model("Model", case.old_fields)
        old = make_old_records(case)
        make_new_payload(case, old)
        [model(**r) for r in old]
    except Exception as exc:
        raise ValueError(f"dry run failed: {type(exc).__name__}: {exc}") from exc


def _apply_fix(case: Case, text: str) -> Case:
    expected, maps = _parse_fix(text)
    targets = [m.target for m in maps]
    if len(set(targets)) != len(targets):
        raise ValueError("duplicate target in fix")
    for m in maps:
        if m.target not in case.old_fields:
            raise ValueError(f"target {m.target!r} not in old_fields")
        if m.source not in case.new_fields:
            raise ValueError(f"source {m.source!r} not in new_fields")
    update: dict = {"expected": expected, "maps": maps, "label_status": "confirmed"}
    if expected == "refuse":
        update["kind"] = "unmappable"
    elif case.kind == "unmappable":  # a semantic rename the miner missed
        update["kind"] = "retype" if any(m.convert != "none" for m in maps) else "rename"
    new = Case.model_validate({**case.model_dump(), **update})
    _check_runnable(new)
    return new


def apply_review(cases_dir: Path, review: Path) -> dict[str, list[str] | int]:
    """Apply ticks/fixes from a REVIEW.md to the case files.

    Only drafted cases are considered, so re-applying a file is a no-op for settled cases.
    Returns id lists ``confirmed``, ``rejected`` (confirm ticked on a case with no maps),
    ``needs_fix`` (fix ticked, fix column empty), ``errors`` (``"id: reason"``), ``missing``
    (drafted cases with no row) and ``malformed`` (raw unparseable data rows), plus
    ``n_<name>`` counts for each and ``n_unchanged`` (drafted cases left as they were).
    """
    rows, malformed = _parse_rows(review)
    res: dict[str, list[str]] = {
        k: [] for k in ("confirmed", "rejected", "needs_fix", "errors", "missing")
    }
    unchanged = 0
    known = {c.id: c for c in load_cases(cases_dir)}
    bad_ids = set()
    for line in malformed:
        m = _ID.match(line)
        if m and m.group(1) in known:
            bad_ids.add(m.group(1))
            res["errors"].append(f"{m.group(1)}: malformed row")
    for cid in sorted(set(rows) - set(known)):
        res["errors"].append(f"{cid}: no such case")
    for cid, case in sorted(known.items()):
        if case.label_status != "drafted" or cid in bad_ids:
            continue
        if cid not in rows:
            res["missing"].append(cid)
            continue
        confirm, fix_mark, fix = rows[cid]
        if fix:
            try:
                new = _apply_fix(case, fix)
            except (ValueError, ValidationError) as e:
                res["errors"].append(f"{cid}: {str(e).splitlines()[0]}")
                continue
        elif confirm and fix_mark:
            res["errors"].append(f"{cid}: contradictory ticks")
            continue
        elif confirm:
            if case.ambiguous or (case.expected == "repair" and not case.maps):
                res["rejected"].append(cid)
                continue
            new = case.model_copy(update={"label_status": "confirmed"})
            try:
                _check_runnable(new)
            except ValueError as e:
                res["errors"].append(f"{cid}: {str(e).splitlines()[0]}")
                continue
        elif fix_mark:
            res["needs_fix"].append(cid)
            continue
        else:
            unchanged += 1
            continue
        save_case(new, cases_dir)
        res["confirmed"].append(cid)
    out: dict[str, list[str] | int] = dict(res)
    out["malformed"] = malformed
    for k, v in {**res, "malformed": malformed}.items():
        out[f"n_{k}"] = len(v)
    out["n_unchanged"] = unchanged
    return out
