# Aegis Phase 2: Self-Healing Pipeline with Human-in-the-Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the `aegis-healer` library into a self-healing *pipeline*: records keep flowing when an API drifts, high-confidence fixes apply automatically, low-confidence fixes wait for a human, and records blocked on a human decision are quarantined and replayed after approval, all within a Claude Haiku budget the user controls.

**Architecture:** Builds on the existing package (`src/aegis_healer`). New pieces: a privacy mode for what the agent sees (`agent/privacy.py`), LLM usage accounting with a per-run call cap (`agent/graph.py`, `HealResult.usage`), an append-only run log (`runlog.py`), a quarantine store plus replay (`store/files.py`, `quarantine.py`), a pending-fix notification hook (`notify.py`), a `Pipeline` wrapper tying it together (`pipeline.py`), and a GitHub Actions example where approving a pending fix = merging a PR.

**Tech Stack:** Python ≥3.11, Pydantic v2, LangGraph, langchain-anthropic (Claude Haiku 5.5, model id `claude-haiku-5-5`), httpx, Typer + Rich, pytest, ruff.

**Spec:** Notion "Aegis | API Healer — Package Design" (https://app.notion.com/p/41ed43a6f4fa8135bbc3d74c55d047aa) + `docs/superpowers/plans/2026-11-19-aegis-healer.md` (Scope changes + Global Constraints) + the user's directive of 2026-11-20: "the goal is to build a self healing pipeline, with HITL for low confidence fixes", "proceed with Haiku".

## Global Constraints

- Everything in `docs/superpowers/plans/2026-11-19-aegis-healer.md` → Global Constraints still holds (names `aegis-healer` / `aegis_healer` / `aegis`; default `auto_apply_threshold = 0.9`; deterministic fixes never need approval; fixes are data, never code; transform whitelist; key only from `ANTHROPIC_API_KEY`; default model `claude-haiku-5-5`; no database required).
- HITL rule (unchanged, must not regress): an agent fix applies automatically only if every agent mapping has `confidence >= auto_apply_threshold` and the threshold is `< 1.0`; otherwise it becomes a pending fix that a human approves or rejects.
- `.aegis/` is meant to be committed to git: nothing in it may contain payload values unless the user explicitly opts in. Quarantined records are the one exception and live in `.aegis/quarantine/`, which `aegis` adds to a generated `.aegis/.gitignore`.
- Privacy mode default is `"masked"`: no names, emails, addresses or other free text reach the LLM unless the user sets `privacy="full"`.
- Never print or log `ANTHROPIC_API_KEY`, header values, webhook URLs, or query strings.
- All new behaviour is covered by tests that use fakes; no test may call a real LLM or the network.
- Ruff clean (`ruff check .`), `pytest -q` green, after every task.
- Windows is the primary user platform: paths via `pathlib`, UTF-8 file IO, no POSIX-only calls.

## Review Focus

1. **Masking leaks**: a nested dict/list value or a numeric-looking string ID (e.g. `"a3f1-b8c2-1001"`) must be masked, not passed through. Test in Task 1.
2. **Budget cap mid-run**: when `max_llm_calls` is hit during a multi-group batch, the run must end `failed` with a clear message, never `healed` with half the fields. Test in Task 2.
3. **Replay after approval**: quarantined records must validate with the approved mappings and be removed from quarantine only after they validate. Test in Task 4.
4. **Notification failure**: a webhook that times out or returns 500 must not crash the run or change its status. Test in Task 5.
5. **Pipeline idempotency**: running `Pipeline.run()` twice on the same drifted batch must not create two pending fixes for the same drift. Test in Task 6.

---

### Task 1: Privacy mode for agent samples

**Files:**
- Create: `src/aegis_healer/agent/privacy.py`
- Modify: `src/aegis_healer/healer.py` (new `privacy` param, applied to `ProposalRequest.samples`)
- Modify: `src/aegis_healer/cli/check_cmd.py` (`--privacy` option)
- Test: `tests/test_privacy.py`

**Interfaces:**
- Produces: `Privacy = Literal["full", "masked", "schema"]`; `mask_value(value: Any) -> Any`; `prepare_samples(samples: list[dict], mode: Privacy) -> list[dict]`.
- `Healer(..., privacy: Privacy = "masked")`; `aegis check --privacy [full|masked|schema]` (default `masked`).

Masking rules for `mask_value` (recursive over dicts and lists):
- `bool`, `None` → unchanged.
- `int`/`float` → unchanged if it looks like a Unix timestamp in seconds (`946684800 <= v <= 4102444800`), otherwise `"<int>"` / `"<float>"`.
- `str`: ISO date/datetime → `"<datetime>"`; email (`@` with a dot after it) → `"<email>"`; a "category-like" value — length ≤ 20, matches `^[A-Za-z][A-Za-z_-]*$`, all lowercase or all uppercase (e.g. `active`, `INACTIVE`, `in_progress`) → unchanged; anything else → `"<string, N chars>"` where N is the length.
- `schema` mode replaces every leaf with its type name (`"str"`, `"int"`, …); `full` returns a deep copy unchanged.

- [ ] **Step 1: Failing tests** in `tests/test_privacy.py`:
  - `test_masks_free_text_and_emails`: `mask_value({"emailAddress": "alice@example.com", "first_name": "Alice", "street": "742 Evergreen Terrace"})` == `{"emailAddress": "<email>", "first_name": "<string, 5 chars>", "street": "<string, 21 chars>"}`
  - `test_keeps_categories_bools_epochs`: `mask_value({"status": "active", "flag": True, "ts": 1686787200})` is unchanged
  - `test_masks_ids_and_numbers` (Review Focus 1): `mask_value({"userId": "a3f1-b8c2-1001", "n": 42})` == `{"userId": "<string, 14 chars>", "n": "<int>"}`
  - `test_masks_nested_and_lists`: nested dicts and lists of dicts are masked recursively
  - `test_schema_mode`: `prepare_samples([{"a": "x", "b": 1}], "schema") == [{"a": "str", "b": "int"}]`
  - `test_healer_sends_masked_samples_by_default`: with a `FakeProposer` from `tests/fakes.py`, `Healer(...).validate(PHASE1_V2, InternalUserProfile)` → `fake.requests[0].samples[0]["emailAddress"] == "<email>"` and `"Alice"` does not appear in `json.dumps(fake.requests[0].samples)`
  - `test_healer_full_mode_opt_in`: `privacy="full"` → raw values reach the request
- [ ] **Step 2:** `pytest tests/test_privacy.py -v` → FAIL
- [ ] **Step 3:** Implement. Masking applies only to what is sent to the proposer; matcher and validator keep using real samples.
- [ ] **Step 4:** `pytest -q` → all PASS; `ruff check .` clean
- [ ] **Step 5:** Commit `feat: privacy mode for agent samples (masked by default)`

---

### Task 2: LLM usage accounting and a per-run call budget

**Files:**
- Modify: `src/aegis_healer/agent/__init__.py`, `src/aegis_healer/agent/graph.py`, `src/aegis_healer/healer.py`, `src/aegis_healer/cli/check_cmd.py`
- Modify: `tests/fakes.py` (fakes report usage)
- Test: `tests/test_usage.py`

**Interfaces:**
- Produces (`agent/__init__.py`): `Usage(BaseModel)`: `calls: int = 0`, `input_tokens: int = 0`, `output_tokens: int = 0`; method `add(other: Usage) -> None`; property `est_cost_usd -> float` using Haiku 5.5 list prices `0.10` USD per million input tokens and `0.50` USD per million output tokens (constants `INPUT_USD_PER_MTOK = 0.10`, `OUTPUT_USD_PER_MTOK = 0.50` in `agent/__init__.py`).
- `class BudgetExceeded(AgentError)`.
- `Proposer.propose(request, check, budget: int | None = None) -> MappingPatch`; proposers expose `last_usage: Usage` after each call. `AgentProposer` counts one call per LLM invoke and raises `BudgetExceeded` *before* an invoke that would exceed `budget`.
- `AgentProposer` builds the structured LLM with `include_raw=True` and reads `raw.usage_metadata["input_tokens"/"output_tokens"]` when present (treat missing as 0); the parsed patch comes from the `"parsed"` key; a non-`None` `"parsing_error"` counts as a failed attempt (retry path).
- `Healer(..., max_llm_calls: int | None = 10)`: the budget is shared across all drift groups in one `validate()` call. `HealResult.usage: Usage`.
- CLI: `aegis check --max-llm-calls N` (default 10); after a run that used the agent, print one line `LLM: <calls> call(s), <in> in / <out> out tokens, ~$<cost:.4f>`.

- [ ] **Step 1: Failing tests** in `tests/test_usage.py`:
  - `test_usage_summed_and_costed`: `Usage(calls=2, input_tokens=2_000_000, output_tokens=1_000_000).est_cost_usd == pytest.approx(0.70)`
  - `test_agent_reads_usage_metadata`: a fake structured LLM returning `{"raw": obj_with usage_metadata={"input_tokens": 1500, "output_tokens": 400}, "parsed": GOOD, "parsing_error": None}` → `proposer.last_usage == Usage(calls=1, input_tokens=1500, output_tokens=400)`
  - `test_parsing_error_retries`: first response has `parsing_error` set, second is good → returns the good patch, `last_usage.calls == 2`
  - `test_budget_exceeded_mid_batch` (Review Focus 2): a batch with two drift groups that each need the agent, `max_llm_calls=1` → `status == "failed"`, `"budget" in message.lower()`, nothing saved
  - `test_healresult_usage_reported`: healed V2 via a fake proposer reporting usage → `result.usage.calls >= 1`
  - `test_cli_prints_llm_line`: the CLI output contains `LLM: ` and `~$` when the agent ran
- [ ] **Step 2:** run → FAIL
- [ ] **Step 3:** Implement. Update `tests/fakes.py` so `FakeProposer` accepts `budget`, sets `last_usage = Usage(calls=1, input_tokens=1000, output_tokens=200)`, and raises `BudgetExceeded` when `budget is not None and budget < 1`. Keep existing tests green.
- [ ] **Step 4:** `pytest -q` PASS; ruff clean
- [ ] **Step 5:** Commit `feat: LLM usage and cost reporting with a per-run call budget`

---

### Task 3: Append-only run log

**Files:**
- Create: `src/aegis_healer/runlog.py`
- Modify: `src/aegis_healer/healer.py` (log every `validate()` when `persist=True`)
- Modify: `src/aegis_healer/cli/__init__.py`, create `src/aegis_healer/cli/runs_cmd.py` (`aegis runs`)
- Test: `tests/test_runlog.py`

**Interfaces:**
- Produces: `RunRecord(BaseModel)`: `at: datetime` (UTC), `model_key: str`, `status: Status`, `records: int`, `healed_or_valid: int`, `failed_records: int`, `fields_fixed: list[str]` (target paths applied), `pending_id: int | None`, `usage: Usage`, `message: str`.
- `append_run(root: Path, record: RunRecord) -> None` writes one JSON line to `<root>/runs.jsonl` (UTF-8, append mode, one `write` per line).
- `read_runs(root: Path, limit: int = 20) -> list[RunRecord]` returns the most recent `limit`, newest first; skips malformed lines.
- The Healer gets the store root via a new optional `MappingStore.root: Path | None` attribute (FileStore already has `root`); if absent, skip logging.
- `aegis runs [--limit 20] [--db]` prints a table: time, model, status, records, fixed fields, LLM calls, cost.
- No payload values in run records (field paths and counts only).

- [ ] **Step 1: Failing tests**: `test_append_and_read_newest_first`; `test_malformed_line_skipped`; `test_healer_logs_each_run` (V1 ok then V2 healed → 2 lines, statuses `ok`, `healed`); `test_persist_false_does_not_log`; `test_no_payload_values_in_log` (`"a@x.io" not in runs.jsonl text` after healing V2); `test_cli_runs_table`.
- [ ] **Step 2:** run → FAIL
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest -q` PASS; ruff clean
- [ ] **Step 5:** Commit `feat: append-only run log and aegis runs`

---

### Task 4: Quarantine and replay (records wait for the human, then flow)

**Files:**
- Create: `src/aegis_healer/quarantine.py`
- Modify: `src/aegis_healer/store/files.py` (quarantine folder + `.aegis/.gitignore`)
- Modify: `src/aegis_healer/cli/review_cmd.py` (after approving, offer/perform replay), create `src/aegis_healer/cli/replay_cmd.py` (`aegis replay`)
- Test: `tests/test_quarantine.py`

**Interfaces:**
- Produces (`quarantine.py`):
  - `Quarantine(root: Path)` with `add(model_key: str, records: list[dict], reason: str, pending_id: int | None) -> str` (returns a batch id `YYYYMMDDTHHMMSSZ-<6 hex>`), `list(model_key: str | None = None) -> list[QuarantineBatch]`, `load(batch_id: str) -> QuarantineBatch`, `remove(batch_id: str) -> None`.
  - `QuarantineBatch(BaseModel)`: `id`, `model_key`, `reason`, `pending_id: int | None`, `created_at`, `records: list[dict]`.
  - Files: `<root>/quarantine/<batch_id>.json`. On first use, write `<root>/.gitignore` containing the line `quarantine/` (create or append if missing; never duplicate the line).
  - `replay(quarantine: Quarantine, healer: Healer, model: type[BaseModel], batch_id: str | None = None) -> ReplayResult` re-validates quarantined records with the current mappings via `healer.validate`; removes a batch only if every record now validates; returns `ReplayResult(released: list[BaseModel], still_blocked: int, batches_cleared: list[str])`.
- `Healer(..., quarantine: bool = True)`: when status is `pending` or `failed` and `persist=True`, the failing raw records are added to quarantine (with `pending_id`), and `HealResult.quarantine_id: str | None` is set. Valid records are still returned in `result.records` (the pipeline keeps flowing).
- CLI: `aegis replay --model module:Class [--batch ID] [--db]` prints released / still blocked; exit 0 if everything released, 1 otherwise. `aegis review` prints `Run 'aegis replay --model …' to release quarantined records.` after an approval when quarantine has batches for that model key.

- [ ] **Step 1: Failing tests**: `test_pending_quarantines_failing_records` (V1 + V2 batch, agent below threshold → status pending, `len(records) == 1`, one quarantine batch with 1 record, `pending_id` matches); `test_gitignore_written_once`; `test_replay_after_approval_releases` (Review Focus 3: approve pending → `replay` → `len(released) == 1`, batch removed); `test_replay_without_approval_keeps_batch` (still_blocked == 1, batch still present); `test_quarantine_false_disables`; `test_cli_replay_exit_codes`.
- [ ] **Step 2:** run → FAIL
- [ ] **Step 3:** Implement. Use the store's atomic write helper style (temp file + `os.replace`).
- [ ] **Step 4:** `pytest -q` PASS; ruff clean
- [ ] **Step 5:** Commit `feat: quarantine blocked records and replay them after approval`

---

### Task 5: Notify a human when a fix needs review

**Files:**
- Create: `src/aegis_healer/notify.py`
- Modify: `src/aegis_healer/healer.py` (`on_pending` callback), `src/aegis_healer/cli/check_cmd.py` (webhook from env)
- Test: `tests/test_notify.py`

**Interfaces:**
- Produces: `PendingNotice(BaseModel)`: `model_key`, `pending_id: int | None`, `fields: list[str]`, `min_confidence: float | None`, `quarantined: int`, `message: str`.
- `Healer(..., on_pending: Callable[[PendingNotice], None] | None = None)` called once when a run ends `pending`. Exceptions raised by the callback are caught and logged with `logging.getLogger("aegis_healer")` at WARNING; they never change the result.
- `webhook_notifier(url: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None) -> Callable[[PendingNotice], None]` POSTs JSON `{"text": "<one-line summary>", "aegis": <notice as dict>}` (the `text` key makes it work with Slack and Discord-compatible incoming webhooks). https only (http allowed only for `localhost`/`127.0.0.1`); never follows redirects; never logs the URL.
- CLI: if env `AEGIS_WEBHOOK_URL` is set, `aegis check` passes `webhook_notifier(url)` as `on_pending`.
- The notice contains field paths and counts only, never payload values.

- [ ] **Step 1: Failing tests**: `test_on_pending_called_once_with_fields`; `test_callback_exception_does_not_change_status`; `test_webhook_posts_text_and_payload` (MockTransport captures JSON with `"text"` and `aegis.pending_id`); `test_webhook_500_and_timeout_swallowed` (Review Focus 4); `test_webhook_rejects_plain_http_remote` (`webhook_notifier("http://example.com/x")` raises `ValueError`); `test_no_values_in_notice`.
- [ ] **Step 2:** run → FAIL
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest -q` PASS; ruff clean
- [ ] **Step 5:** Commit `feat: pending-fix notifications (callback + webhook)`

---

### Task 6: `Pipeline`: the self-healing ingestion loop

**Files:**
- Create: `src/aegis_healer/pipeline.py`
- Modify: `src/aegis_healer/__init__.py` (export `Pipeline`, `PipelineRun`)
- Modify: `src/aegis_healer/healer.py` (pending-fix de-duplication)
- Create: `examples/pipeline/run_pipeline.py`, `examples/pipeline/README.md`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `Pipeline(fetch: Callable[[], Any], model: type[BaseModel], sink: Callable[[list[BaseModel]], None], healer: Healer | None = None, records_path: str | None = None)`; `run() -> PipelineRun`; `PipelineRun(BaseModel)`: `status`, `loaded: int`, `quarantined: int`, `pending_id: int | None`, `result: HealResult` (arbitrary types allowed).
- `run()` = fetch → `healer.validate` → `sink(result.records)` if any → also replays quarantined batches for this model (via Task 4's `replay`) and sinks released records → returns counts. A fetch exception propagates (the caller's scheduler handles retries); a sink exception propagates *after* nothing new was saved by this run beyond the healer's own store writes.
- De-duplication (Review Focus 5): before `add_pending`, the Healer checks `store.list_pending(model_key)` for a pending fix with the same `base_version` and the same set of `(target, sources, transform)` mappings; if found, it reuses that id instead of creating a new one.
- Example `examples/pipeline/run_pipeline.py`: fetches `http://localhost:8000/v2.json` (the demo server from `examples/demo`), uses `examples/demo/models.py:UserProfile`, sinks into a local SQLite file `pipeline.db` (stdlib `sqlite3`, parameterized inserts, table created if missing), prints the `PipelineRun`. README explains: run it, see pending + quarantine, `aegis review`, run again → quarantined records released into the sink.

- [ ] **Step 1: Failing tests**: `test_run_loads_valid_and_quarantines_rest`; `test_second_run_does_not_duplicate_pending` (Review Focus 5); `test_run_after_approval_releases_quarantine_into_sink`; `test_fetch_error_propagates`; `test_example_script_imports` (import `examples/pipeline/run_pipeline.py` as a module without running `main`).
- [ ] **Step 2:** run → FAIL
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest -q` PASS; ruff clean
- [ ] **Step 5:** Commit `feat: Pipeline self-healing ingestion loop with quarantine replay`

---

### Task 7: HITL through pull requests (GitHub Actions example) + docs

**Files:**
- Create: `examples/github-actions/aegis-drift.yml`, `examples/github-actions/README.md`
- Modify: `README.md` (Pipeline section, HITL section, privacy, budget, runs, quarantine, notifications, Anthropic key setup), `examples/demo/README.md` (mention `--privacy`, `--max-llm-calls`)

**Interfaces:** none new in code.

Workflow behaviour (`aegis-drift.yml`, a template users copy into `.github/workflows/`):
- Triggers: `schedule` (cron `17 */6 * * *`) and `workflow_dispatch`.
- `permissions: contents: write, pull-requests: write`.
- Steps: checkout; setup-python 3.12; `pip install aegis-healer`; run `aegis check "$AEGIS_URL" --model "$AEGIS_MODEL_SPEC" --privacy masked --max-llm-calls 10` with `ANTHROPIC_API_KEY` from `secrets.ANTHROPIC_API_KEY`, `AEGIS_URL` / `AEGIS_MODEL_SPEC` from repository variables; capture the exit code without failing the job (`set +e`).
- Exit 0 with changes under `.aegis/mappings` or `.aegis/history` → commit on branch `aegis/auto-<run_id>` and open a PR titled `Aegis: auto-applied schema fix` (informational, still reviewable).
- Exit 1 (pending) → for each pending id (`aegis review` has no list-ids mode, so parse `.aegis/pending/*.json` with a small inline Python step reading `status == "pending"`), run `aegis review --approve <id>` on branch `aegis/review-<run_id>`, commit, and open a PR titled `Aegis: fix needs review (#<id>)` whose body includes the `aegis check` output. **Merging the PR is the human approval; closing it is the rejection.**
- Exit 2 → fail the job (drift Aegis can't fix).
- Use `gh pr create` with `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}`; pin actions by major tag with a comment to pin SHAs.
- `.aegis/quarantine/` stays out of git (Task 4's `.gitignore`).

- [ ] **Step 1:** Write the workflow and its README (explain setup: secrets, variables, what each PR means, how rejecting works).
- [ ] **Step 2:** Validate YAML parses (`python -c "import yaml,sys; yaml.safe_load(open(...))"`, installing `pyyaml` in the dev venv only if missing) and that the inline Python step runs against a temp `.aegis/pending` fixture.
- [ ] **Step 3:** Update `README.md` sections listed above; include "Get an Anthropic API key" steps (Console → workspace with a monthly spend limit → create key → put in `.env` as `ANTHROPIC_API_KEY=`), and the cost note (~$0.0005 per agent call; agent only runs when rules can't fix drift).
- [ ] **Step 4:** `pytest -q` PASS; ruff clean
- [ ] **Step 5:** Commit `docs: HITL via pull requests, pipeline, privacy, budget`
