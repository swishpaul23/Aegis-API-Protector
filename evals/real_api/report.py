"""Markdown + JSON report for the real-API eval (see the design spec, "Report")."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aegis_healer.agent import Usage
from evals.real_api.cases import Case, split_of
from evals.real_api.runner import Row
from evals.real_api.scoring import EXCLUDED_FROM_N, OUTCOMES, summarize

HONESTY_NOTE = (
    "Schema changes are real (mined from public API spec history); record values are synthetic."
)

__all__ = ["HONESTY_NOTE", "Row", "write_report"]

# Outcomes listed case by case (with the recorded reason) under each split.
LISTED: tuple[str, ...] = ("wrong_applied", "crashed", "agent_error")


def _is_headline(case: Case) -> bool:
    return split_of(case.id) == "test" and case.label_status == "confirmed"


def _usage_dict(rows: list[Row]) -> dict[str, Any]:
    total = Usage()
    for _, _, u, _ in rows:
        total.add(u)
    return {
        "calls": total.calls,
        "input_tokens": total.input_tokens,
        "output_tokens": total.output_tokens,
        "est_cost_usd": total.est_cost_usd,
    }


def _mode_block(rows: list[Row]) -> dict[str, Any]:
    head = [r for r in rows if _is_headline(r[0])]
    dev = [r for r in rows if not _is_headline(r[0])]
    out: dict[str, Any] = {
        "headline": summarize([(c, o) for c, o, _, _ in head]),
        "dev": summarize([(c, o) for c, o, _, _ in dev]),
        "usage": _usage_dict(rows),
        "headline_usage": _usage_dict(head),
    }
    for o in LISTED:
        out[f"{o}_ids"] = sorted(c.id for c, oc, _, _ in head if oc == o)
        out[f"dev_{o}_ids"] = sorted(c.id for c, oc, _, _ in dev if oc == o)
    out["errors"] = {c.id: e for c, _, _, e in sorted(rows, key=lambda r: r[0].id) if e}
    return out


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _rate(count: int, n: int) -> str:
    if not n:
        return f"n/a ({count}/0)"  # never print 0.0% for an empty denominator
    return f"{_pct(count / n)} ({count}/{n})"


NOT_RUN = "not run"


def _cell(block: dict, count: int, n: int) -> str:
    return _rate(count, n) if block["ran"] else NOT_RUN


def _breakdown(title: str, key: str, rules: dict, agent: dict) -> list[str]:
    rb, ab = rules["headline"], agent["headline"]
    names = sorted(set(rb[key]) | set(ab[key]))
    lines = [
        f"### {title}",
        "",
        f"| {title.split()[-1]} | N | Rules repaired | Agent repaired "
        "| Rules wrong-applied | Agent wrong-applied | Not breaking (excluded from N) |",
        "|---|---|---|---|---|---|---|",
    ]
    empty = {"n": 0, "counts": {o: 0 for o in OUTCOMES}}
    for name in names:
        r, a = rb[key].get(name, empty), ab[key].get(name, empty)
        n = max(r["n"], a["n"])
        lines.append(
            f"| {name} | {n} | {_cell(rules, r['counts']['repaired'], r['n'])} "
            f"| {_cell(agent, a['counts']['repaired'], a['n'])} "
            f"| {_cell(rules, r['counts']['wrong_applied'], r['n'])} "
            f"| {_cell(agent, a['counts']['wrong_applied'], a['n'])} "
            f"| {max(r['counts']['not_breaking'], a['counts']['not_breaking'])} |"
        )
    lines.append("")
    return lines


def _compare_table(title: str, rules: dict, agent: dict, part: str) -> list[str]:
    lines = [f"### {title}", "", "| Outcome | Rules only | Rules + agent |", "|---|---|---|"]
    r, a = rules[part], agent[part]
    for o in OUTCOMES:
        if o in EXCLUDED_FROM_N:
            rc = r["counts"][o] if rules["ran"] else NOT_RUN
            ac = a["counts"][o] if agent["ran"] else NOT_RUN
            lines.append(f"| {o} (excluded from N) | {rc} | {ac} |")
            continue
        lines.append(
            f"| {o} | {_cell(rules, r['counts'][o], r['n'])} "
            f"| {_cell(agent, a['counts'][o], a['n'])} |"
        )
    lines.append("")
    return lines


def _headline(label: str, block: dict) -> str:
    if not block["ran"]:
        return f"**{label}: {NOT_RUN}.**"
    h = block["headline"]
    n, c = h["n"], h["counts"]
    nb = f"{c['not_breaking']} not breaking (excluded from N)"
    if n == 0:
        return f"**{label}: no confirmed breaking test cases (N=0); {nb}.**"
    return (
        f"**{label}: repaired {_rate(c['repaired'], n)} of {n} confirmed breaking test cases; "
        f"wrongly auto-applied {_rate(c['wrong_applied'], n)}; {nb}.**"
    )


_TITLES = {
    "wrong_applied": "Wrongly applied",
    "crashed": "Crashed",
    "agent_error": "Agent errors (called, no tokens returned)",
}


def _listed(rules: dict, agent: dict, prefix: str) -> list[str]:
    out: list[str] = []
    for o in LISTED:
        out += [f"### {_TITLES[o]} (case ids)", ""]
        for label, block in (("Rules only", rules), ("Agent", agent)):
            if not block["ran"]:
                out.append(f"- {label}: {NOT_RUN}")
                continue
            ids = block.get(f"{prefix}{o}_ids", [])
            errs = block.get("errors", {})
            shown = [f"{i} ({errs[i]})" if i in errs else i for i in ids]
            out.append(f"- {label}: {', '.join(shown) if shown else 'none'}")
        out.append("")
    return out


def _modes_ran(meta: dict, rules_rows: list[Row], agent_rows: list[Row]) -> dict[str, bool]:
    """Which modes ran: from meta["modes_run"] when present, else from which have rows."""
    if "modes_run" in meta:
        listed = {m.strip() for m in str(meta["modes_run"]).split(",")}
        return {"rules": "rules" in listed, "agent": "agent" in listed}
    return {"rules": bool(rules_rows), "agent": bool(agent_rows)}


_EXTRA_META = ("split", "run", "modes_run", "prior_test_runs")


def _render(data: dict[str, Any]) -> str:
    meta, rules, agent = data["meta"], data["rules"], data["agent"]
    heads = [_headline("Rules only", rules), _headline("Rules + agent", agent)]
    if not any(b["ran"] and b["headline"]["n"] for b in (rules, agent)):
        heads.insert(0, "No confirmed test cases yet: no headline number.")
    out = [
        "# Aegis real-API eval",
        "",
        f"- Aegis version: {meta['aegis_version']}",
        f"- Model: {meta['model']}",
        f"- Date: {meta['date']}",
        *[f"- {k.replace('_', ' ').capitalize()}: {meta[k]}" for k in _EXTRA_META if k in meta],
        "",
        *[x for h in heads for x in (h, "")],
        f"_{HONESTY_NOTE}_",
        "",
        "## Confirmed test split",
        "",
    ]
    out += _compare_table("Rules only vs rules + agent", rules, agent, "headline")
    out += _breakdown("Per API", "by_api", rules, agent)
    out += _breakdown("Per kind", "by_kind", rules, agent)
    out += _listed(rules, agent, "")
    out += ["## Dev split and unconfirmed cases (not headline)", ""]
    out += _compare_table("Dev / drafted", rules, agent, "dev")
    out += _listed(rules, agent, "dev_")
    out += ["## Usage (agent run)", ""]
    if not agent["ran"]:
        out += [f"Agent {NOT_RUN}.", ""]
        return "\n".join(out)
    for label, u in (
        ("Headline rows only (confirmed test split)", agent["headline_usage"]),
        ("All cases incl. dev/unconfirmed", agent["usage"]),
    ):
        out += [
            f"### {label}",
            "",
            f"- LLM calls: {u['calls']}",
            f"- Input tokens: {u['input_tokens']}",
            f"- Output tokens: {u['output_tokens']}",
            f"- est_cost_usd: ${u['est_cost_usd']:.4f}",
            "",
        ]
    return "\n".join(out)


def write_report(
    rules_rows: list[Row], agent_rows: list[Row], out_dir: Path, meta: dict
) -> tuple[Path, Path]:
    """Write `<date>.json` and `<date>.md` into out_dir; returns (json_path, md_path)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "meta": {
            k: meta[k] for k in ("aegis_version", "model", "date", *_EXTRA_META) if k in meta
        },
        "honesty_note": HONESTY_NOTE,
        "rules": _mode_block(rules_rows),
        "agent": _mode_block(agent_rows),
    }
    for mode, ran in _modes_ran(meta, rules_rows, agent_rows).items():
        data[mode]["ran"] = ran
    date = meta["date"]
    jp, mp = out_dir / f"{date}.json", out_dir / f"{date}.md"
    jp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    mp.write_text(_render(data))
    return jp, mp
