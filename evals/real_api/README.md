# Real-API eval

Measures how well `aegis-healer` repairs **real breaking API changes**, how often it wrongly
applies a fix, and how often it correctly refuses or defers.

Schema changes are real (mined from public API spec history); record values are synthetic.

## What it measures and why

The repo's own drift fixtures are hand-written, so they cannot back the claim "it heals API
drift". This eval mines field renames, moves and retypes from the git history of public OpenAPI
specs (GitHub, Stripe, ...), has a human confirm the expected repair for each, then feeds every
case through the real `Healer` and scores the loaded values, not just the status.

Each case is scored into one outcome:

| Outcome | Meaning |
|---|---|
| repaired | Expected a repair; healed and loaded values equal the expected values. |
| wrong_applied | Healed, but values differ, or healed a case that should be refused. Silent corruption: the headline safety metric. |
| correct_refusal | Expected a refusal; nothing was applied. |
| safe_deferral | Expected a repair but nothing was applied (left pending/failed for a human). Not an error. |
| crashed | An exception. Counted as a failure and listed. |

Headline = repaired / N, reported beside the wrongly-applied count and rate. Rules-only and
rules+agent are reported side by side so the agent's lift is visible.

## Dev/test rule

Cases split ~30% dev / 70% test by a stable hash of the case id (`split_of`).

- **Headline = confirmed cases in the test split, run once.** Never tune against it.
- **Dev** is for finding and fixing bugs. Use `--split dev`.
- Only human-confirmed cases count toward the headline; drafted ones appear under dev only.

## Review (REVIEW.md)

1. `python -m evals.real_api review render` writes `REVIEW.md`, one row per drafted case.
2. Tick `[x] confirm` to accept a row as shown. Tick `[x] fix` only if you will fill the last
   column. A filled fix column confirms the case by itself.
3. Fix column syntax: `REFUSE`, or maps `target<-source[:convert]` separated by `;`, e.g.
   `user_id<-account.user_id:int_str; full_name<-fullName`. `convert` is one of `none`,
   `int_str`, `int_float`, `epoch_iso`. `target` is a path in the old model, `source` a path in
   the new payload.
4. Ambiguous rows cannot be confirmed as-is; they need a written fix (or `REFUSE`).
5. `python -m evals.real_api review apply` writes the labels back. It lists confirmed,
   rejected, needs_fix, errors, malformed and missing rows, and exits non-zero if any of
   errors, malformed or missing is non-empty, so nothing is lost silently.

## Reproduce

From the repo root, with the dev dependencies installed:

```
python -m evals.real_api mine [--api github]          # network; writes cases/, never overwrites
python -m evals.real_api review render                # then edit REVIEW.md by hand
python -m evals.real_api review apply
python -m evals.real_api run --split dev --mode rules # iterate on bugs here (own run dir)
export ANTHROPIC_API_KEY=...                          # agent mode only
python -m evals.real_api run                          # headline: test split, both modes
python -m evals.real_api report [RUN_DIR]            # re-render one run (default: latest)
```

`mine` skips sources with no version pairs configured and skips any case id that already has
a file, so confirmed labels survive re-mining. `run` fails with exit code 2 before doing any
work if agent mode is requested without `ANTHROPIC_API_KEY`. Running the rules mode needs no
network and no key. `AEGIS_MODEL` overrides the agent model.

Options: `--cases DIR` and `--out DIR` override the cases and results directories.

Outputs: every `run` creates its own directory `results/<date>-<split>-<n>/` (n is the next
free integer from 1; existing runs are never overwritten, so dev and test runs on the same day
coexist). It holds `raw-<mode>.json` (meta, split, mode, and per-case outcome, usage and
label_status) plus the report `<date>.md` / `<date>.json` (N, per-API and per-kind breakdown,
tokens, cost, Aegis version, model, date, split, run). `report [RUN_DIR]` re-renders from that
run's own raw files and stored meta only; it warns if a case's label_status changed since the
run and uses the stored one. A mode with no raw file in the run shows as not run. A run with
zero cases in the split exits 1 and creates nothing.

**Numbers in the resume or README come from `results/*.md` only.**

## Known limits

- Schema flattening follows one `$ref` hop; a referenced schema's own `$ref`s are not followed.
- At most 2000 paths per schema; expansion stops beyond that.
- The rules matcher handles style renames and same-leaf moves only; the agent handles the rest.
- Record values are synthetic, so value-format edge cases from real traffic are not covered.
- Meaning drift is out of scope (`meaning=False`).
- N is whatever is reachable and confirmed; it is not padded.
