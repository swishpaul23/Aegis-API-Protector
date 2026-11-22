# Real-API eval: design

Date: 2026-11-21. Status: awaiting approval.

## Intent

Produce one defensible number for the resume and README: what share of **real breaking API
changes** Aegis repairs correctly, how often it wrongly applies a fix, and how often it
correctly refuses or defers. Today the repo only has hand-written drift fixtures, so the claim
"it heals API drift" is untested against anything Stuart didn't write himself.

Audience: recruiters (headline number) and data engineers/interviewers (method that survives
being poked at).

## Goals

- 70+ labeled cases, mined from real spec history of 3+ public APIs, no single API above 40%.
- Ground truth is human-checked, not produced by the same heuristic that found the case.
- Three outcomes reported, not one: **repaired correctly**, **correctly refused/deferred**,
  **wrongly auto-applied** (silent corruption, the headline safety metric).
- A rules-only baseline next to rules+agent, so the agent's lift is visible.
- Reproducible offline: cases are frozen files; only the agent run needs an Anthropic key.

## Non-goals

- Live calls to real APIs (needs keys, flaky, can't be frozen).
- Meaning-drift evaluation (separate feature; `meaning=False` for this eval).
- Shipping the eval inside the PyPI package.

## Anti-goals (a technically working eval that would still be a failure)

- Tuning Healer against the cases it is later scored on. Cases are split into **dev** (used while
  fixing bugs) and **test** (frozen before the first scored run, never inspected for tuning).
  The headline number is test only.
- Counting a case the script labeled itself. Only cases with a human-confirmed label count.
- Overstating realism. Schema changes are real; record **values** are synthetic. The README
  says so in one line.

## Approaches considered

1. **Mined + human-checked (chosen by Stuart).** Mine candidates from spec git history, draft
   labels, Stuart confirms. Costs ~15-20 min of review; survives scrutiny.
2. Fully automatic labels from the spec diff. Rejected: the heuristic that finds the case also
   writes the answer key, so the score is circular and skews to easy renames.
3. Hand-pick changes from changelogs. Rejected: too slow to reach 70, and cherry-picking risk.

## Design

Lives in `evals/real_api/` (repo root, excluded from the wheel and sdist).

```
evals/real_api/
  README.md            method, caveats, how to reproduce, the one-line honesty note
  miner/               one-time tool: spec history -> candidate cases
    sources.py         API list: repo URL, spec path, version pairs to diff
    specs.py           git show <ref>:<path>, OpenAPI -> flat {schema: {field path: type}}
    diff.py            removed/added/retyped fields per schema between two refs
    candidates.py      pairs removed+added fields into candidate drifts, writes cases
  cases/               frozen, committed: one JSON per case
  review.py            renders cases/ as REVIEW.md table; reads Stuart's confirmations back
  runner.py            per case: build old model, make payloads, run Healer, record outcome
  scoring.py           outcome classification + aggregates
  report.py            results/<date>.json + results/<date>.md (counts, rates, per-API, per-kind)
  __main__.py          python -m evals.real_api {mine|review|run|report} [--split test|dev]
  tests/               unit tests for specs parsing, scoring, payload generation
```

**Case file** (JSON): `id`, `api`, `source` (repo, old ref, new ref, spec path, schema name),
`split` (dev|test), `kind` (rename | move | retype | split-merge | unmappable), `old_fields`,
`new_fields`, `expected` (`repair` with per-field expected values, or `refuse`), `notes`,
`label_status` (`drafted` | `confirmed`).

**Payloads.** Generated deterministically (fixed seed) from each field's type, with a distinct
value per field so a wrong mapping produces a visibly wrong record rather than a lucky match.
Old payload validates against the old model; new payload is the same values re-shaped per the
label.

**Run.** Per case: `Healer(persist=False, meaning=False, quarantine=False, proposer=...)` against
a Pydantic model built from the old fields, fed the new-shape payloads. Two passes: rules-only
(`proposer=None`) and rules+agent (Claude Haiku, `auto_apply_threshold=0.9`, same default users
get). `max_llm_calls` stays at the library default.

**Scoring** (value-level, so equivalent mappings both count):

| Outcome | Condition |
|---|---|
| repaired correctly | expected=repair, status `healed`, loaded records equal expected values |
| wrongly auto-applied | status `healed` but values differ, or expected=refuse and status `healed` |
| correctly refused | expected=refuse, status `pending`/`failed`, nothing applied |
| safe deferral | expected=repair, status `pending`/`failed` (a human would decide; not an error) |
| crashed | exception; counted as a failure and listed |

Headline = repaired correctly / N. Reported beside it: wrongly auto-applied count and rate.

**Report.** Markdown + JSON with N, per-API and per-kind breakdown, rules-only vs agent,
tokens and estimated cost (the repo already tracks `Usage`), Aegis version, model, date. Resume
and README numbers are copied from this file only.

## Constraints

- Python 3.11, existing deps only (+ `pyyaml`, already a dev extra). Mining needs network to
  public GitHub repos; running the eval does not.
- Agent run costs cents on Haiku; needs `ANTHROPIC_API_KEY` locally.
- Stuart's review (~15-20 min) is a required step before the headline is valid.
- This session cannot push to the repo (GitHub app not installed); work lands on a local branch
  for Stuart to push, or access gets fixed first.

## Decisions Stuart made

- Option 1 (mined + human-checked ground truth).
- Not tied to Stripe; mix of APIs is fine.

## Left to the builder (my calls, flagged)

- Which APIs: start with `github/rest-api-description`, `stripe/openapi`, and one or two more
  chosen by whether their history yields enough real renames/moves; swap out any that don't.
- Dev/test split: 30/70, assigned by hash of case id (stable), test never used for tuning.
- Unmappable cases are included deliberately (aim for 10-15) so refusal behavior is measured.
- If fewer than 70 confirmed cases are reachable, report the real N; do not pad.
