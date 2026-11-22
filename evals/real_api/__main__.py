"""CLI: python -m evals.real_api {mine|review|run|report}."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

import aegis_healer
from aegis_healer.agent import Usage
from aegis_healer.agent.graph import DEFAULT_MODEL
from evals.real_api.cases import Case, load_cases, save_case, split_of
from evals.real_api.miner.candidates import mine
from evals.real_api.miner.sources import SOURCES
from evals.real_api.miner.specs import clone_repo
from evals.real_api.report import Row, write_report
from evals.real_api.review import apply_review, render_review
from evals.real_api.runner import run_all

HERE = Path(__file__).resolve().parent
DEFAULT_CASES = HERE / "cases"
DEFAULT_REVIEW = HERE / "REVIEW.md"
DEFAULT_OUT = HERE / "results"
MODES = ("rules", "agent")


def _meta() -> dict:
    return {
        "aegis_version": aegis_healer.__version__,
        "model": os.environ.get("AEGIS_MODEL") or DEFAULT_MODEL,
        "date": datetime.date.today().isoformat(),
    }


def _cmd_mine(args: argparse.Namespace) -> int:
    sources = [s for s in SOURCES if args.api in (None, s.api)]
    if not sources:
        print(f"no such api: {args.api}", file=sys.stderr)
        return 2
    cases_dir = Path(args.cases)
    for src in sources:
        if not src.pairs:
            print(f"{src.api}: no pairs configured, skipping")
            continue
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            clone_repo(src.repo_url, repo)
            found = mine(src, repo)
        written = skipped = 0
        for case in found:
            if (cases_dir / f"{case.id}.json").exists():
                skipped += 1  # never clobber confirmed labels
                continue
            save_case(case, cases_dir)
            written += 1
        print(f"{src.api}: written {written}, skipped-existing {skipped}")
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    if args.action == "render":
        render_review(Path(args.cases), Path(args.review))
        print(f"wrote {args.review}")
        return 0
    res = apply_review(Path(args.cases), Path(args.review))
    for key in ("confirmed", "rejected", "needs_fix", "errors", "malformed", "missing"):
        items = res[key]
        print(f"{key}: {len(items)}")
        for item in items:
            print(f"  {item}")
    print(f"unchanged: {res['n_unchanged']}")
    return 1 if (res["errors"] or res["malformed"] or res["missing"]) else 0


def _new_run_dir(out: Path, date: str, split: str) -> Path:
    n = 1
    while (out / f"{date}-{split}-{n}").exists():
        n += 1
    run_dir = out / f"{date}-{split}-{n}"
    run_dir.mkdir(parents=True)
    return run_dir


def _dump_rows(rows: list[Row], path: Path, meta: dict, mode: str) -> None:
    payload = {
        "meta": meta,
        "split": meta["split"],
        "mode": mode,
        "rows": [
            {"id": c.id, "outcome": o, "usage": u.model_dump(), "label_status": c.label_status}
            for c, o, u in rows
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _cmd_run(args: argparse.Namespace) -> int:
    modes = list(MODES) if args.mode == "both" else [args.mode]
    if "agent" in modes and not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set (needed for agent mode)", file=sys.stderr)
        return 2
    cases = [c for c in load_cases(Path(args.cases)) if split_of(c.id) == args.split]
    if not cases:
        print(f"error: no cases in the {args.split} split ({args.cases})", file=sys.stderr)
        return 1
    out = Path(args.out)
    meta = _meta()
    meta["split"] = args.split
    run_dir = _new_run_dir(out, meta["date"], args.split)
    meta["run"] = run_dir.name
    meta["modes_run"] = ", ".join(modes)
    rows: dict[str, list[Row]] = {"rules": [], "agent": []}
    with tempfile.TemporaryDirectory() as tmp:
        for mode in modes:
            rows[mode] = run_all(cases, mode, Path(tmp) / mode)
            _dump_rows(rows[mode], run_dir / f"raw-{mode}.json", meta, mode)
    _, md = write_report(rows["rules"], rows["agent"], run_dir, meta)
    print(f"{len(cases)} {args.split} cases; report: {md}")
    return 0


def _load_raw(path: Path, by_id: dict[str, Case]) -> tuple[dict, list[Row]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for r in data["rows"]:
        case = by_id.get(r["id"])
        if case is None:
            raise SystemExit(f"error: raw row {r['id']!r} has no case file")
        if case.label_status != r["label_status"]:
            print(
                f"warning: {r['id']}: label_status was {r['label_status']} at run time, "
                f"now {case.label_status}; using the stored one",
                file=sys.stderr,
            )
            case = case.model_copy(update={"label_status": r["label_status"]})
        rows.append((case, r["outcome"], Usage(**r["usage"])))
    return data["meta"], rows


def _cmd_report(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        runs = sorted(p for p in out.glob("*-*-*") if p.is_dir()) if out.is_dir() else []
        if not runs:
            print(f"error: no run directories in {out}", file=sys.stderr)
            return 2
        run_dir = runs[-1]
    by_id = {c.id: c for c in load_cases(Path(args.cases))}
    rows: dict[str, list[Row]] = {m: [] for m in MODES}
    meta: dict | None = None
    for mode in MODES:
        raw = run_dir / f"raw-{mode}.json"
        if raw.exists():
            meta, rows[mode] = _load_raw(raw, by_id)
    if meta is None:
        print(f"error: no raw-*.json in {run_dir}", file=sys.stderr)
        return 2
    _, md = write_report(rows["rules"], rows["agent"], run_dir, meta)
    print(f"report: {md}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evals.real_api", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser, out: bool = False, review: bool = False) -> None:
        sp.add_argument("--cases", default=str(DEFAULT_CASES), help="cases directory")
        if out:
            sp.add_argument("--out", default=str(DEFAULT_OUT), help="results directory")
        if review:
            sp.add_argument("--review", default=str(DEFAULT_REVIEW), help="REVIEW.md path")

    m = sub.add_parser("mine", help="mine draft cases from public spec history")
    m.add_argument("--api", help="only this source (e.g. github)")
    common(m)
    r = sub.add_parser("review", help="render or apply REVIEW.md")
    r.add_argument("action", choices=["render", "apply"])
    common(r, review=True)
    n = sub.add_parser("run", help="run the eval and write the report")
    n.add_argument("--split", choices=["test", "dev"], default="test")
    n.add_argument("--mode", choices=["rules", "agent", "both"], default="both")
    common(n, out=True)
    rp = sub.add_parser("report", help="re-render the report from the latest raw files")
    rp.add_argument("run_dir", nargs="?", help="run directory (default: latest under --out)")
    common(rp, out=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handler = {"mine": _cmd_mine, "review": _cmd_review, "run": _cmd_run, "report": _cmd_report}
    return handler[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
