"""Pair removed and added fields across a schema change and draft eval cases."""

from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

from evals.real_api.cases import Case, ExpectedMap, FieldType
from evals.real_api.miner.diff import SchemaDiff, diff_schema
from evals.real_api.miner.sources import Source
from evals.real_api.miner.specs import fetch_spec, flatten_schemas

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


def _greedy(rem: list[str], add: list[str], edges: dict[str, dict[str, float]]):
    pairs = sorted(
        ((edges[r][a], r, a) for r in rem for a in add if a in edges[r]),
        key=lambda x: (-x[0], x[1], x[2]),
    )
    used_r: set[str] = set()
    used_a: set[str] = set()
    chosen = []
    for _, r, a in pairs:
        if r not in used_r and a not in used_a:
            used_r.add(r)
            used_a.add(a)
            chosen.append((r, a))
    return chosen


def _plan(old: dict[str, FieldType], new: dict[str, FieldType], d: SchemaDiff) -> list[dict]:
    """Return case plans: kind, maps, old/new paths involved, ambiguous flag."""
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
                )
            )
    edges: dict[str, dict[str, float]] = {}
    for r, rt in d.removed.items():
        edges[r] = {}
        for a, at in d.added.items():
            score = _score(r, a)
            if score is not None and _convert_kind(rt, at):
                edges[r][a] = score + (0.01 if rt == at else 0.0)
    for rem, add in _components(edges):
        chosen = _greedy(rem, add, edges)
        ambiguous = len(rem) > 1 or len(add) > 1 or any(len(edges[r]) > 1 for r in rem)
        kinds = {_kind(r, a, old[r], new[a]) for r, a in chosen}
        kind = "retype" if "retype" in kinds else sorted(kinds)[0]
        plans.append(
            dict(
                kind=kind,
                maps=[(r, a, _convert_kind(old[r], new[a])) for r, a in chosen],
                old=[r for r, _ in chosen],
                new=[a for _, a in chosen],
                ambiguous=ambiguous,
            )
        )
    for r in sorted(d.removed):
        if not edges[r]:
            plans.append(dict(kind="unmappable", maps=[], old=[r], new=[], ambiguous=False))
    return plans


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", name)


def mine(source: Source, repo_dir: Path) -> list[Case]:
    cases: list[Case] = []
    counters: dict[str, int] = defaultdict(int)
    for old_ref, new_ref in source.pairs:
        old_flat = flatten_schemas(fetch_spec(repo_dir, old_ref, source.spec_path))
        new_flat = flatten_schemas(fetch_spec(repo_dir, new_ref, source.spec_path))
        for schema in sorted(old_flat.keys() & new_flat.keys()):
            old, new = old_flat[schema], new_flat[schema]
            d = diff_schema(old, new)
            if not d.removed or not (d.added or d.retyped):
                continue
            unchanged = [p for p in sorted(old) if p in new and old[p] == new[p]]
            context = unchanged[:CONTEXT_FIELDS]
            for plan in _plan(old, new, d):
                counters[schema] += 1
                refuse = plan["kind"] == "unmappable"
                cases.append(
                    Case(
                        id=f"{source.api}-{_slug(schema)}-{counters[schema]}",
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
                        new_fields={p: new[p] for p in context + plan["new"]},
                        expected="refuse" if refuse else "repair",
                        maps=[
                            ExpectedMap(target=t, source=s, convert=c) for t, s, c in plan["maps"]
                        ],
                        label_status="drafted",
                        ambiguous=plan["ambiguous"],
                    )
                )
    return cases
