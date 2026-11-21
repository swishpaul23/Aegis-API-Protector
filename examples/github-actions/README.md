# GitHub Actions: human review through pull requests

`aegis-drift.yml` checks your API every 6 hours. Drift Aegis fixes confidently becomes an
informational PR; a low-confidence fix becomes a PR where **merging is the approval and
closing means "not this fix"** (see below). A meaning-drift fix (same field, new meaning)
is never approved by the workflow: its PR asks you to decide on the branch before merging.

## Setup

1. Copy `aegis-drift.yml` to `.github/workflows/aegis-drift.yml`.
2. Repository **Secrets**:
   - `ANTHROPIC_API_KEY` (see the main README to get one). Without it Aegis runs rules-only.
   - `AEGIS_URL`: the API endpoint, **as a Secret if it contains a key** (e.g.
     `?api_key=...`). The workflow reads `secrets.AEGIS_URL` first and falls back to the
     `AEGIS_URL` Variable for public URLs. Aegis never prints query strings.
3. Repository **Variables**:
   - `AEGIS_MODEL_SPEC`: your model as `module:Class`, e.g. `app.models:User`.
   - `AEGIS_PIP_SPEC` (**required**, no default): what to `pip install`, pinned to a
     version, e.g. `aegis-healer==0.1.1`. The install step fails with a
     clear error when it is empty: installing an unpinned or unregistered name in a job that
     holds your secrets would run whatever package claims that name.
4. Settings, Actions, General: allow "Read and write permissions" and "Allow GitHub Actions
   to create and approve pull requests".
5. Commit your `.aegis/` folder so the workflow has the current mappings. `.aegis/quarantine/`
   is gitignored automatically and is never committed.

If your model needs installed dependencies, add a step before the check, e.g.
`pip install -r requirements.txt`.

## What each outcome means

| Exit | What happens |
|---|---|
| 0, files changed | PR `Aegis: auto-applied schema fix` (branch `aegis/auto-<run_id>`). Already applied; the PR makes it reviewable. |
| 0, no changes | Nothing. |
| 1 | PR `Aegis: fix needs review (#<id>)` (branch `aegis/review-<run_id>`). A schema fix is approved in the diff: **merging approves it.** A meaning fix is committed unapproved and needs a decision on the branch first (see below). |
| 1, no pending fix found | Job fails: aegis exited 1 without a pending fix (possible crash or usage error). |
| 2 | Job fails: drift Aegis can't fix. Edit your model by hand. |
| other | Job fails with the unexpected exit code. |

## Meaning-drift fixes

When the API keeps its field names but changes what the values mean (dollars become cents),
the check exits 1 and the workflow opens a review PR with a meaning fix. **The workflow never
approves a meaning fix**: it commits `.aegis/pending` with the fix still pending, and the PR
body says what to do.

- **Baseline**: detection needs a learned baseline (`.aegis/profiles/`, aggregate counts
  only). CI reads the committed baseline but **never updates it** (a baseline commit on every
  clean run would open a PR every time). Build and refresh it where your pipeline actually
  runs, or locally (`aegis check URL --model ...` a few times on healthy data; a field needs
  20 records of history), then commit `.aegis/profiles/`. Without a committed baseline CI
  finds no meaning drift.
- **Blocked records**: quarantined records are not persisted on ephemeral runners. In CI
  "blocked" means the run opens a review PR; after you merge the decision, the next run
  re-fetches the records. There is no replay in CI.
- **Decide on the review branch**: check out the PR branch, run `aegis review` and choose
  `[a]pply fix` (a suggested scale) or `[k]eep data` (the new values are the new normal;
  resets the field baseline), or run `aegis review --approve ID` / `aegis review --keep ID`.
  On the review branch the meaning fix is still pending, so both work. Commit `.aegis/`
  (mappings, history, pending, profiles), push, then merge.
- **Closing the PR** without a decision leaves the fix pending (it never reaches your default
  branch, so the next run may propose it again).

## Closing a review PR

Closing means "not this fix". It does not remember the decision: the next scheduled run
proposes again (new agent calls, new PR) unless you change your model, the API drift
resolves, or you set the repository variable `AEGIS_PAUSED=true` (stops all runs).
While any Aegis PR (branch `aegis/*`, auto-applied or review) is open, new runs are skipped,
so close or merge it to resume. Runs are serialized (`concurrency`) and time out after 15 minutes.

The PR body contains the `aegis check` output.

## Notes

- `AEGIS_WEBHOOK_URL` (a Secret) is read by the `aegis` CLI, so it works here; add it to the
  check step's `env:` if you want pings. Python `Pipeline` users pass
  `Healer(on_pending=webhook_notifier(url))` instead.
- Single writer: if a `Pipeline` host also uses this repository's `.aegis/`, pull merged
  approvals before each Pipeline run and don't let both write mappings at once (the PR flow
  can go stale against changes it didn't see).

- `runs.jsonl` is a local audit log: commit it if you like, but the workflow deliberately
  doesn't stage it (it changes every run and would open a PR each time). The workflow stages
  only `.aegis/mappings` and `.aegis/history` (plus `.aegis/pending` for review PRs), never
  `.aegis/profiles`.
- The inline step that lists pending fixes (`<id> <kind>` per line, kind `schema` or
  `meaning`) is a heredoc copy of `list_pending.py`; keep the two in sync (the file exists so
  the logic can be tested). Only `schema` ids are approved by the workflow.
- Actions are pinned by major tag for readability; pin full SHAs in production.
- The workflow commits as `github-actions[bot]`. PRs opened with `GITHUB_TOKEN` do not
  trigger other workflows; use a PAT or GitHub App token if you need CI on these PRs.
