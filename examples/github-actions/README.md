# GitHub Actions: human review through pull requests

`aegis-drift.yml` checks your API every 6 hours. Drift Aegis fixes confidently becomes an
informational PR; a low-confidence fix becomes a PR where **merging is the approval and
closing is the rejection**.

## Setup

1. Copy `aegis-drift.yml` to `.github/workflows/aegis-drift.yml`.
2. Repository **Secrets**: `ANTHROPIC_API_KEY` (see the main README to get one). Without
   it Aegis runs rules-only.
3. Repository **Variables**:
   - `AEGIS_URL`: the API endpoint. Treat it as sensitive if it contains a key; it is only
     passed through `env:` and never printed (Aegis also hides query strings in output).
   - `AEGIS_MODEL_SPEC`: your model as `module:Class`, e.g. `app.models:User`.
   - `AEGIS_PIP_SPEC` (until the package is published): e.g.
     `aegis-healer @ git+https://github.com/<owner>/<repo>`. Defaults to `aegis-healer`.
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
| 1 | PR `Aegis: fix needs review (#<id>)` (branch `aegis/review-<run_id>`) with the fix approved in the diff. **Merge = approve. Close = reject** (the pending fix stays unapproved; delete or ignore the branch). |
| 2 | Job fails: drift Aegis can't fix. Edit your model by hand. |

The PR body contains the `aegis check` output.

## Notes

- The inline step that lists pending ids is a heredoc copy of `list_pending.py`; keep the
  two in sync (the file exists so the logic can be tested).
- Actions are pinned by major tag for readability; pin full SHAs in production.
- The workflow commits as `github-actions[bot]`. PRs opened with `GITHUB_TOKEN` do not
  trigger other workflows; use a PAT or GitHub App token if you need CI on these PRs.
