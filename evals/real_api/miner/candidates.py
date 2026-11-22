"""Pair removed and added fields across a schema change and draft eval cases."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

from evals.real_api.cases import Case, ExpectedMap, FieldType
from evals.real_api.miner.diff import SchemaDiff, diff_schema
from evals.real_api.miner.sources import Source
from evals.real_api.miner.specs import fetch_spec, flatten_schemas, path_exists

SIMILARITY = 0.6  # normalized edit ratio on leaf names
CONTEXT_FIELDS = 8  # unchanged fields carried along for realism

_CONVERT: dict[tuple[str, str], str] = {
    ("integer", "string"): "int_str",
    ("integer", "number"): "int_float",
    ("datetime", "integer"): "epoch_iso",
}


def _leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def _parent(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


def _convert_kind(old_t: str, new_t: str) -> str | None:
    return "none" if old_t == new_t else _CONVERT.get((old_t, new_t))


def _score(old_path: str, new_path: str) -> float | None:
    """Name affinity in [0.6, 1.0], or None when the paths are not plausibly the same field."""
    a, b = _leaf(old_path), _leaf(new_path)
    if a == b and old_path != new_path:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    return ratio if ratio >= SIMILARITY else None


def _kind(old_path: str, new_path: str, old_t: str, new_t: str) -> str:
    if old_t != new_t:
        return "retype"
    if _leaf(old_path) == _leaf(new_path) or _parent(old_path) != _parent(new_path):
        return "move"
    return "rename"


def _components(
    edges: dict[str, dict[str, float]],
) -> list[tuple[list[str], list[str]]]:
    """Connected components of the removed/added bipartite graph (isolated removed excluded)."""
    seen: set[str] = set()
    out = []
    for start in sorted(edges):
        if start in seen or not edges[start]:
            continue
        rem, add, stack = [], set(), [start]
        while stack:
            r = stack.pop()
            if r in seen:
                continue
            seen.add(r)
            rem.append(r)
            for a in edges[r]:
                add.add(a)
                stack.extend(x for x in edges if a in edges[x] and x not in seen)
        out.append((sorted(rem), sorted(add)))
    return out


def _plan(old: dict[str, FieldType], new: dict[str, FieldType], d: SchemaDiff) -> list[dict]:
    """Return case plans: kind, maps, old/new paths involved, ambiguous flag, notes."""
    plans: list[dict] = []
    for path, (ot, nt) in sorted(d.retyped.items()):
        conv = _convert_kind(ot, nt)
        if conv:  # a retype with no expressible conversion is skipped
            plans.append(
                dict(
                    kind="retype",
                    maps=[(path, path, conv)],
                    old=[path],
                    new=[path],
                    ambiguous=False,
                    notes="",
                )
            )
    edges: dict[str, dict[str, float]] = {}
    for r, rt in d.removed.items():
        edges[r] = {}
        for a, at in d.added.items():
            score = _score(r, a)
            if score is not None and _convert_kind(rt, at):
                edges[r][a] = score
    for rem, add in _components(edges):
        kinds = {_kind(r, a, old[r], new[a]) for r in rem for a in add if a in edges[r]}
        kind = "retype" if "retype" in kinds else sorted(kinds)[0]
        if len(rem) == 1 and len(add) == 1:
            r, a = rem[0], add[0]
            maps = [(r, a, _convert_kind(old[r], new[a]))]
            notes = ""
            ambiguous = False
        else:  # no guessed key: a human writes the maps
            maps = []
            notes = "candidates: " + "; ".join(
                f"{r}->{{" + ",".join(f"{a}:{edges[r][a]:.2f}" for a in sorted(edges[r])) + "}"
                for r in rem
            )
            ambiguous = True
        plans.append(dict(kind=kind, maps=maps, old=rem, new=add, ambiguous=ambiguous, notes=notes))
    for r in sorted(d.removed):
        if not edges[r]:
            plans.append(
                dict(kind="unmappable", maps=[], old=[r], new=[], ambiguous=False, notes="")
            )
    return plans


def _verified(d: SchemaDiff, schema: str, old_spec: dict, new_spec: dict) -> SchemaDiff:
    """Drop removals/additions that are artifacts of the flattening hop cap.

    flatten_schemas stops at one $ref hop, so a field that moves behind a second hop looks
    removed (or, the other way round, added). Check each against the fully resolved spec.
    """
    return SchemaDiff(
        removed={p: t for p, t in d.removed.items() if not path_exists(new_spec, schema, p)},
        added={p: t for p, t in d.added.items() if not path_exists(old_spec, schema, p)},
        retyped=d.retyped,
    )


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name)


def _case_id(api: str, schema: str, old_ref: str, new_ref: str, plan: dict) -> str:
    """Stable id: independent of pair order and of other cases in the schema."""
    key = json.dumps([old_ref, new_ref, sorted(plan["old"]), sorted(plan["new"])])
    return f"{api}-{_slug(schema)}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def _common_prefix(paths: list[str]) -> int:
    """Number of leading path segments shared by all paths, never including a leaf."""
    split = [p.split(".") for p in paths]
    limit = min(len(s) for s in split) - 1
    n = 0
    while n < limit and len({s[n] for s in split}) == 1:
        n += 1
    return n


def _involved(plan: dict) -> list[str]:
    return list(plan["old"]) + list(plan["new"])


def change_signature(case: Case, plan: dict) -> tuple:
    """Identity of the upstream change, independent of which schema references it.

    A rename inside a shared ``$ref`` component shows up under every schema that embeds it,
    with the embedding path as a prefix. Stripping the longest common prefix of the involved
    paths makes those copies equal.
    """
    k = _common_prefix(_involved(plan))

    def rel(path: str) -> str:
        return ".".join(path.split(".")[k:])

    old_t, new_t = case.old_fields, case.new_fields
    if case.maps:
        body: tuple = tuple(
            sorted(
                (rel(m.target), old_t[m.target], rel(m.source), new_t[m.source], m.convert)
                for m in case.maps
            )
        )
    else:
        body = (
            tuple(sorted((rel(p), old_t[p]) for p in plan["old"])),
            tuple(sorted((rel(p), new_t[p]) for p in plan["new"])),
        )
    s = case.source
    return (case.api, s["old_ref"], s["new_ref"], case.kind, body)


def _dedupe(found: list[tuple[Case, dict]]) -> list[Case]:
    """Keep one case per change signature: the one with the shortest involved paths."""
    best: dict[tuple, tuple[tuple, Case]] = {}
    for case, plan in found:
        paths = _involved(plan)
        rank = (sum(p.count(".") for p in paths), sum(map(len, paths)), case.id)
        sig = change_signature(case, plan)
        if sig not in best or rank < best[sig][0]:
            best[sig] = (rank, case)
    keep = {id(c) for _, c in best.values()}
    return [c for c, _ in found if id(c) in keep]


def _draft(
    source: Source,
    schema: str,
    old_ref: str,
    new_ref: str,
    old: dict,
    new: dict,
    plan: dict,
    context: list[str],
    added: list[str],
) -> Case:
    refuse = plan["kind"] == "unmappable"
    return Case(
        id=_case_id(source.api, schema, old_ref, new_ref, plan),
        api=source.api,
        source={
            "repo": source.repo_url,
            "old_ref": old_ref,
            "new_ref": new_ref,
            "spec_path": source.spec_path,
            "schema": schema,
        },
        kind=plan["kind"],
        old_fields={p: old[p] for p in context + plan["old"]},
        # every field this change added to the schema rides along, so a reviewer can map
        # to it and the payload carries distractors
        new_fields={p: new[p] for p in context + plan["new"] + added},
        expected="refuse" if refuse else "repair",
        maps=[ExpectedMap(target=t, source=s, convert=c) for t, s, c in plan["maps"]],
        label_status="drafted",
        ambiguous=plan["ambiguous"],
        notes=plan["notes"],
    )


def mine(source: Source, repo_dir: Path) -> list[Case]:
    """Draft cases for every configured ref pair, one per distinct upstream change."""
    found: list[tuple[Case, dict]] = []
    for old_ref, new_ref in source.pairs:
        try:
            old_spec = fetch_spec(repo_dir, old_ref, source.spec_path)
            new_spec = fetch_spec(repo_dir, new_ref, source.spec_path)
        except (subprocess.CalledProcessError, ValueError) as e:  # e.g. file absent at a ref
            reason = (getattr(e, "stderr", None) or b"").decode(errors="replace").strip() or str(e)
            print(f"skipped pair {old_ref}..{new_ref} ({source.api} {source.spec_path}): {reason}")
            continue
        old_flat, new_flat = flatten_schemas(old_spec), flatten_schemas(new_spec)
        for schema in sorted(old_flat.keys() & new_flat.keys()):
            old, new = old_flat[schema], new_flat[schema]
            d = _verified(diff_schema(old, new), schema, old_spec, new_spec)
            if not d.removed or not (d.added or d.retyped):
                continue
            unchanged = [p for p in sorted(old) if p in new and old[p] == new[p]]
            context = unchanged[:CONTEXT_FIELDS]
            added = sorted(d.added)
            for plan in _plan(old, new, d):
                case = _draft(source, schema, old_ref, new_ref, old, new, plan, context, added)
                found.append((case, plan))
    return _dedupe(found)
