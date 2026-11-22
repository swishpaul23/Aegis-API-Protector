"""Human review round-trip: render REVIEW.md for drafted cases, apply ticks and fixes back.

Row columns: id | api | kind | change | ambiguous | source | notes | [ ] confirm | [ ] fix | fix.
Tick a box by changing ``[ ]`` to ``[x]``. The fix column (last) accepts ``REFUSE`` or maps
``target<-source`` / ``target<-source:convert`` separated by ``;``.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import ValidationError

from evals.real_api.cases import Case, ExpectedMap, load_cases, save_case

HEADER = (
    "| id | api | kind | change | ambiguous | source | notes | confirm | fix? | fix |\n"
    "|---|---|---|---|---|---|---|---|---|---|"
)
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
            _source(c),
            c.notes if c.ambiguous else "",
        ]
        rows.append("| " + " | ".join(_esc(x) for x in cells) + " | [ ] confirm | [ ] fix |  |")
    Path(out).write_text(_INTRO + HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _parse_rows(review: Path) -> dict[str, tuple[bool, bool, str]]:
    rows: dict[str, tuple[bool, bool, str]] = {}
    for line in Path(review).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in _SPLIT.split(line)[1:-1]]
        if len(cells) != 10 or not (m := re.fullmatch(r"`(.+)`", cells[0])):
            continue
        fix = cells[9].replace("\\|", "|")
        rows[m.group(1)] = (
            bool(_TICK.match(cells[7])),
            bool(_TICK.match(cells[8])),
            fix,
        )
    return rows


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


def _apply_fix(case: Case, text: str) -> Case:
    expected, maps = _parse_fix(text)
    for m in maps:
        if m.target not in case.old_fields:
            raise ValueError(f"target {m.target!r} not in old_fields")
        if m.source not in case.new_fields:
            raise ValueError(f"source {m.source!r} not in new_fields")
    update = {"expected": expected, "maps": maps, "label_status": "confirmed"}
    if expected == "refuse":
        update["kind"] = "unmappable"
    return Case.model_validate({**case.model_dump(), **update})


def apply_review(cases_dir: Path, review: Path) -> dict[str, list[str] | int]:
    """Apply ticks/fixes from a REVIEW.md to the case files.

    Only drafted cases are considered, so re-applying a file is a no-op for settled cases.
    Returns id lists ``confirmed``, ``rejected`` (confirm ticked on a case with no maps),
    ``needs_fix`` (fix ticked, fix column empty) and ``errors`` (``"id: reason"``), plus
    ``n_<name>`` counts for each and ``n_unchanged`` (drafted cases left as they were).
    """
    rows = _parse_rows(review)
    res: dict[str, list[str]] = {k: [] for k in ("confirmed", "rejected", "needs_fix", "errors")}
    unchanged = 0
    known = {c.id: c for c in load_cases(cases_dir)}
    for cid in sorted(set(rows) - set(known)):
        res["errors"].append(f"{cid}: no such case")
    for cid, case in sorted(known.items()):
        if case.label_status != "drafted" or cid not in rows:
            continue
        confirm, fix_mark, fix = rows[cid]
        if fix:
            try:
                new = _apply_fix(case, fix)
            except (ValueError, ValidationError) as e:
                res["errors"].append(f"{cid}: {str(e).splitlines()[0]}")
                continue
        elif confirm:
            if case.ambiguous or (case.expected == "repair" and not case.maps):
                res["rejected"].append(cid)
                continue
            new = case.model_copy(update={"label_status": "confirmed"})
        elif fix_mark:
            res["needs_fix"].append(cid)
            continue
        else:
            unchanged += 1
            continue
        save_case(new, cases_dir)
        res["confirmed"].append(cid)
    out: dict[str, list[str] | int] = dict(res)
    for k, v in res.items():
        out[f"n_{k}"] = len(v)
    out["n_unchanged"] = unchanged
    return out
