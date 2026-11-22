"""CLI: python -m evals.real_api {mine|select|review|run|report}."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import aegis_healer
from aegis_healer.agent import Usage
from evals.real_api import runner
from evals.real_api.cases import Case, load_cases, save_case, split_of
from evals.real_api.miner.candidates import mine
from evals.real_api.miner.sources import SOURCES
from evals.real_api.miner.specs import clone_repo
from evals.real_api.report import Row, write_report
from evals.real_api.review import apply_review, render_review
from evals.real_api.runner import AgentOutage, run_all

HERE = Path(__file__).resolve().parent
DEFAULT_CASES = HERE / "cases"
DEFAULT_REVIEW = HERE / "REVIEW.md"
DEFAULT_OUT = HERE / "results"
MODES = ("rules", "agent")


def _meta() -> dict:
    return {
        "aegis_version": aegis_healer.__version__,
        "model": runner.agent_model(),
        "date": datetime.date.today().isoformat(),
    }


def _cmd_mine(args: argparse.Namespace) -> int:
    sources = [s for s in SOURCES if args.api in (None, s.api)]
    if not sources:
        print(f"no such api: {args.api}", file=sys.stderr)
        return 2
    cases_dir = Path(args.cases)
    with tempfile.TemporaryDirectory() as tmp:
        clones: dict[str, Path] = {}  # several sources (e.g. twilio) share one repo: clone once
        for src in sources:
            if not src.pairs:
                print(f"{src.api}: no pairs configured, skipping")
                continue
            if src.repo_url not in clones:
                clones[src.repo_url] = Path(tmp) / f"repo{len(clones)}"
                clone_repo(src.repo_url, clones[src.repo_url])
            found = mine(src, clones[src.repo_url])
            written = skipped = 0
            for case in found:
                if (cases_dir / f"{case.id}.json").exists():
                    skipped += 1  # never clobber confirmed labels
                    continue
                save_case(case, cases_dir)
                written += 1
            print(f"{src.api}: written {written}, skipped-existing {skipped}")
    return 0


def _id_key(case_id: str) -> str:
    return hashlib.sha256(case_id.encode()).hexdigest()


SHARE_WARN = 0.40


def _cmd_select(args: argparse.Namespace) -> int:
    """Cap drafted cases per api by sha256(id) order; the rest go to <cases>/pool/.

    Looks only at case files (id, api, expected, label_status), never at run results.
    """
    cases_dir = Path(args.cases)
    quota = {"repair": args.repair_per_api, "refuse": args.refuse_per_api}
    drafted = [c for c in load_cases(cases_dir) if c.label_status == "drafted"]
    stats: dict[str, dict[str, int]] = {}
    for api in sorted({c.api for c in drafted}):
        st = stats[api] = {"repair": 0, "refuse": 0, "pooled": 0}
        mine_ = sorted((c for c in drafted if c.api == api), key=lambda c: _id_key(c.id))
        for c in mine_:
            if st[c.expected] < quota[c.expected]:
                st[c.expected] += 1
                continue
            pool = cases_dir / "pool"
            pool.mkdir(exist_ok=True)
            (cases_dir / f"{c.id}.json").replace(pool / f"{c.id}.json")
            st["pooled"] += 1
    total = sum(s["repair"] + s["refuse"] for s in stats.values())
    for api, st in stats.items():
        kept = st["repair"] + st["refuse"]
        share = kept / total if total else 0.0
        print(
            f"{api}: kept repair {st['repair']}, kept refuse {st['refuse']}, "
            f"pooled {st['pooled']}; share {share:.1%}"
        )
        if share > SHARE_WARN:
            print(
                f"WARNING: {api} is {share:.1%} of kept cases (> {SHARE_WARN:.0%})",
                file=sys.stderr,
            )
    print(f"kept total {total}")
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


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _new_run_dir(out: Path, date: str, split: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        run_dir = out / f"{date}-{split}-{n}"
        try:
            run_dir.mkdir(exist_ok=False)
        except FileExistsError:
            n += 1
            continue
        return run_dir


def _test_runs(out: Path) -> list[Path]:
    """Existing test-split run directories (`<date>-test-<n>`) under out."""
    if not out.is_dir():
        return []
    return sorted(p for p in out.glob("*-test-*") if p.is_dir())


def _run_key(run_dir: Path) -> tuple[str, int]:
    """Sort key: newest created_at from the run's raw files, then run number."""
    stamps = []
    for mode in MODES:
        raw = run_dir / f"raw-{mode}.json"
        if raw.exists():
            try:
                stamps.append(json.loads(raw.read_text(encoding="utf-8"))["meta"]["created_at"])
            except (ValueError, KeyError):
                pass
    tail = run_dir.name.rsplit("-", 1)[-1]
    return (max(stamps, default=""), int(tail) if tail.isdigit() else 0)


def _dump_rows(rows: list[Row], path: Path, meta: dict, mode: str) -> None:
    payload = {
        "meta": meta,
        "split": meta["split"],
        "mode": mode,
        "rows": [
            {
                "id": c.id,
                "outcome": o,
                "usage": u.model_dump(),
                "label_status": c.label_status,
                "error": e,
            }
            for c, o, u, e in rows
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _cmd_run(args: argparse.Namespace) -> int:
    modes = list(MODES) if args.mode == "both" else [args.mode]
    if "agent" in modes and not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY not set (needed for agent mode)", file=sys.stderr)
        return 2
    cases = [c for c in load_cases(Path(args.cases)) if split_of(c.id) == args.split]
    if args.split == "test":  # the scored split: human-confirmed labels only
        cases = [c for c in cases if c.label_status == "confirmed"]
    if not cases:
        which = "confirmed cases in the test" if args.split == "test" else "cases in the dev"
        print(f"error: no {which} split ({args.cases})", file=sys.stderr)
        return 1
    out = Path(args.out)
    prior = _test_runs(out)
    if args.split == "test" and prior and not args.rerun:
        print(
            f"error: the test split was already run ({', '.join(p.name for p in prior)}). "
            "The headline is one run of the frozen test split; pass --rerun to run it again "
            "(recorded as prior_test_runs in the report).",
            file=sys.stderr,
        )
        return 2
    if "agent" in modes:
        try:  # before any run dir exists: a bad key or outage must not cost a test run
            runner.preflight_agent()
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    meta = _meta()
    meta["split"] = args.split
    meta["prior_test_runs"] = len(prior)
    meta["created_at"] = _now()
    run_dir = _new_run_dir(out, meta["date"], args.split)
    meta["run"] = run_dir.name
    meta["modes_run"] = ", ".join(modes)
    rows: dict[str, list[Row]] = {"rules": [], "agent": []}
    with tempfile.TemporaryDirectory() as tmp:
        for mode in modes:
            try:
                rows[mode] = run_all(cases, mode, Path(tmp) / mode)
            except (AgentOutage, RuntimeError) as e:
                print(
                    f"error: {mode} pass {e}; no {mode} rows written, no report. "
                    f"{run_dir} still counts as a run.",
                    file=sys.stderr,
                )
                return 2
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
        rows.append((case, r["outcome"], Usage(**r["usage"]), r.get("error")))
    return data["meta"], rows


def _cmd_report(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        runs = [
            p
            for p in out.glob("*-*-*")
            if (p / "raw-rules.json").exists() or (p / "raw-agent.json").exists()
        ]
        if not runs:
            print(f"error: no run directories in {out}", file=sys.stderr)
            return 2
        run_dir = max(runs, key=_run_key)
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
    # what actually completed in this run, not what was requested
    done = [m for m in MODES if (run_dir / f"raw-{m}.json").exists()]
    meta = {**meta, "modes_run": ", ".join(done)}
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
    sl = sub.add_parser("select", help="cap drafted cases per api; move the rest to cases/pool/")
    sl.add_argument("--repair-per-api", type=int, default=30)
    sl.add_argument("--refuse-per-api", type=int, default=6)
    common(sl)
    r = sub.add_parser("review", help="render or apply REVIEW.md")
    r.add_argument("action", choices=["render", "apply"])
    common(r, review=True)
    n = sub.add_parser("run", help="run the eval and write the report")
    n.add_argument("--split", choices=["test", "dev"], default="test")
    n.add_argument("--mode", choices=["rules", "agent", "both"], default="both")
    n.add_argument(
        "--rerun", action="store_true", help="allow running the test split again (recorded)"
    )
    common(n, out=True)
    rp = sub.add_parser("report", help="re-render the report from the latest raw files")
    rp.add_argument("run_dir", nargs="?", help="run directory (default: latest under --out)")
    common(rp, out=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    handler = {
        "mine": _cmd_mine,
        "select": _cmd_select,
        "review": _cmd_review,
        "run": _cmd_run,
        "report": _cmd_report,
    }
    return handler[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
