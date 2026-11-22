# Real-API Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure how often Aegis repairs real, human-labeled breaking API changes, and how often it wrongly applies a fix.

**Architecture:** A one-time miner turns public OpenAPI spec git history into candidate cases; a human confirms labels; frozen JSON cases feed a runner that calls `Healer` offline and a scorer that classifies each outcome by loaded record values. Harness (Tasks 1-5) is built and tested on hand-written seed cases before the miner exists.

**Tech Stack:** Python 3.11, Pydantic v2, pytest, existing `aegis_healer`, `pyyaml` (dev extra), `git` CLI for spec history.

**Spec:** `docs/superpowers/specs/2026-11-21-real-api-eval-design.md`

## Global Constraints

- Everything lives under `evals/real_api/`; nothing imports from it inside `src/`; excluded from the sdist (`pyproject.toml` sdist `exclude` gets `evals`).
- Python 3.11; no new runtime deps (`pyyaml` is already in the `dev` extra).
- Healer is always built with `persist=False, meaning=False, quarantine=False`, and a temp store, so a run never writes `.aegis` into the cwd.
- Agent pass: Claude Haiku via the existing `AgentProposer`, `auto_apply_threshold=0.9`, library-default `max_llm_calls`.
- Payload values are deterministic (fixed seed) and distinct per field.
- Headline counts only `split == "test"` and `label_status == "confirmed"`.
- Ruff line length 100; tests under `evals/real_api/tests/` (run with `pytest evals/real_api/tests`).
- Work on branch `eval/real-api`, never `main`. Commit after each task.

## Review Focus

- A Pydantic model with **dotted/nested paths** (`address.city`): `build_model` must nest, and payload reshaping must move values across nesting levels.
- Spec features that don't flatten (`$ref` cycles, `allOf/oneOf`, `additionalProperties`, arrays of objects): the miner must skip or bound them, never hang or crash.
- **Ambiguous pairings** (two removed and two added fields of the same type): the miner must emit one `drafted` case flagged `ambiguous`, not guess a pairing.
- **Missing `ANTHROPIC_API_KEY`** on the agent pass: fail loudly; never emit rules-only numbers labeled as agent numbers.
- **Re-run determinism**: same cases give the same payloads and the same classification twice in a row.

---

### Task 1: Case schema, split, scaffolding

**Files:**
- Create: `evals/__init__.py`, `evals/real_api/__init__.py`, `evals/real_api/cases.py`, `evals/real_api/tests/__init__.py`, `evals/real_api/tests/test_cases.py`
- Modify: `pyproject.toml` (sdist exclude `evals`; ruff per-file-ignores `"evals/**" = ["S"]`)

**Interfaces:**
- Produces:
  - `FieldType = Literal["string","integer","number","boolean","datetime"]`
  - `class ExpectedMap(BaseModel)`: `target: str` (dotted path in the old model), `source: str` (dotted path in the new payload), `convert: Literal["none","int_str","int_float","epoch_iso"] = "none"` (how the old value is represented in the new payload)
  - `class Case(BaseModel)`: `id: str`, `api: str`, `source: dict[str,str]` (repo, old_ref, new_ref, spec_path, schema), `kind: Literal["rename","move","retype","split-merge","unmappable"]`, `old_fields: dict[str,FieldType]`, `new_fields: dict[str,FieldType]`, `expected: Literal["repair","refuse"]`, `maps: list[ExpectedMap]` (empty when `refuse`), `label_status: Literal["drafted","confirmed"] = "drafted"`, `ambiguous: bool = False`, `notes: str = ""`
  - `split_of(case_id: str) -> Literal["dev","test"]`: stable hash, 30% dev
  - `load_cases(dir: Path) -> list[Case]`, `save_case(case: Case, dir: Path) -> Path` (one `<id>.json` per case, sorted keys)

- [ ] **Step 1: Write failing tests** in `test_cases.py`: `test_split_is_stable_and_roughly_30_70` (same id gives the same split across calls; over 1000 synthetic ids, dev share is between 0.25 and 0.35); `test_case_roundtrip` (`save_case` then `load_cases` returns an equal `Case`); `test_refuse_case_must_have_no_maps` (constructing `Case(expected="refuse", maps=[...])` raises `ValidationError`).
- [ ] **Step 2: Run** `pytest evals/real_api/tests/test_cases.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `cases.py` per Interfaces. `split_of` uses `hashlib.sha256(id)`; a model validator enforces the refuse/maps rule. Apply the `pyproject.toml` edits.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(eval): case schema and stable dev/test split`.

### Task 2: Model builder and payload generator

**Files:**
- Create: `evals/real_api/payloads.py`, `evals/real_api/tests/test_payloads.py`

**Interfaces:**
- Consumes: `Case`, `ExpectedMap`, `FieldType` (Task 1)
- Produces:
  - `build_model(name: str, fields: dict[str, FieldType]) -> type[BaseModel]`: dotted paths become nested models; `datetime` -> `datetime`
  - `make_old_records(case: Case, n: int = 5, seed: int = 0) -> list[dict]`: nested dicts valid for the old model; every field gets a distinct value per record and per field
  - `make_new_payload(case: Case, old_records: list[dict]) -> list[dict]`: reshapes each old record per `case.maps` (move value from `target` path to `source` path, applying `convert`); fields not in `maps` and present in `new_fields` carry over under the same path; for `refuse` cases, the unmappable target is simply absent

- [ ] **Step 1: Write failing tests:** `test_build_model_nests_dotted_paths` (fields `{"address.city":"string"}` validates `{"address":{"city":"x"}}`); `test_rename_reshapes` (map `user_name -> userName`, new payload has `userName` equal to the old value and no `user_name`); `test_move_across_nesting` (`email` -> `contact.email`); `test_convert_epoch_iso_roundtrip` (old datetime value appears in the new payload as an int epoch; reading it back gives the same instant); `test_values_distinct_and_seeded` (two string fields never share a value in one record; two calls with the same seed are equal); `test_refuse_case_drops_target`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Use `pydantic.create_model`, building nested models bottom-up from the dotted paths. Generate values from `random.Random(seed)` salted with the field path. Support the four `convert` kinds only; unknown combinations raise `ValueError`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(eval): model builder and deterministic payloads`.

### Task 3: Outcome scoring

**Files:**
- Create: `evals/real_api/scoring.py`, `evals/real_api/tests/test_scoring.py`

**Interfaces:**
- Consumes: `Case` (Task 1); `aegis_healer.HealResult`
- Produces:
  - `Outcome = Literal["repaired","wrong_applied","correct_refusal","safe_deferral","crashed"]`
  - `classify(case: Case, result: HealResult | None, expected_records: list[BaseModel]) -> Outcome` (`result is None` means the run raised)
  - `summarize(rows: list[tuple[Case, Outcome]]) -> dict`: overall and per-`api` and per-`kind` counts plus rates; `repair_rate = repaired / N`, `wrong_applied_rate = wrong_applied / N`

- [ ] **Step 1: Write failing tests** using hand-built `HealResult` objects: healed with records equal to expected -> `repaired`; healed with different values -> `wrong_applied`; `expected="refuse"` + healed -> `wrong_applied`; `refuse` + `pending` -> `correct_refusal`; `repair` + `pending` -> `safe_deferral`; `result=None` -> `crashed`; `test_summarize_rates` (4 rows, exact rates, per-api breakdown keys present).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** exactly the table in the spec. Equality is `result.records == expected_records` (Pydantic model equality).
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(eval): outcome classification and summary`.

### Task 4: Runner

**Files:**
- Create: `evals/real_api/runner.py`, `evals/real_api/tests/test_runner.py`, `evals/real_api/tests/seed_cases.py`

**Interfaces:**
- Consumes: Tasks 1-3; `aegis_healer.Healer`, `aegis_healer.agent.graph.AgentProposer`
- Produces:
  - `run_case(case: Case, proposer: Proposer | None, tmp_root: Path) -> tuple[Outcome, Usage]`
  - `run_all(cases: list[Case], mode: Literal["rules","agent"], tmp_root: Path) -> list[tuple[Case, Outcome, Usage]]`; `mode="agent"` raises `RuntimeError("ANTHROPIC_API_KEY not set")` before running anything if the key is missing
  - `seed_cases.py`: `SEEDS: list[Case]`: three hand-written cases (rename, move with `to_str` retype, unmappable)

- [ ] **Step 1: Write failing tests** with `FakeProposer` from `tests/fakes.py` (add `pythonpath` import as other tests do): `test_rename_seed_repaired_by_rules` (`mode="rules"` -> `repaired`, no LLM); `test_unmappable_seed_refused_rules_only` (`correct_refusal`); `test_agent_mode_requires_key` (monkeypatch env, expect `RuntimeError`, and that no case ran); `test_run_leaves_no_store_in_cwd` (after a run, `tmp_path`-relative `.aegis` absent in cwd); `test_runner_is_deterministic` (two runs give identical outcomes).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Per case: `build_model` from `old_fields`; `old = make_old_records`; `new = make_new_payload`; `expected_records = [Model(**r) for r in old]`; `Healer(store=<MappingStore rooted in tmp_root/case.id>, persist=False, meaning=False, quarantine=False, proposer=proposer)`; call `healer.validate(new, Model)`; wrap in `try/except Exception` -> `None` for `classify`. Check `aegis_healer.store` for the exact `MappingStore` constructor to root it in the temp dir.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(eval): offline runner with rules and agent modes`.

### Task 5: Report

**Files:**
- Create: `evals/real_api/report.py`, `evals/real_api/tests/test_report.py`

**Interfaces:**
- Consumes: `summarize`, `run_all` rows (Task 3-4)
- Produces: `write_report(rules_rows, agent_rows, out_dir: Path, meta: dict) -> tuple[Path, Path]` writing `<date>.json` and `<date>.md`. `meta` has `aegis_version`, `model`, `date`. The markdown has: headline line, a rules-only vs rules+agent table, per-API and per-kind tables, wrongly-applied list (case ids), total tokens and `est_cost_usd`, and the one-line honesty note ("schema changes are real; record values are synthetic").

- [ ] **Step 1: Write failing tests:** `test_report_contains_headline_and_wrong_applied_ids`; `test_report_headline_counts_only_confirmed_test_split` (feed one dev row and one drafted row; assert N excludes them).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Filtering for the headline happens here (`split_of(case.id) == "test"` and `label_status == "confirmed"`); the full tables use the same filter, with a separate "dev split" table for the others.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(eval): markdown and json report`.

### Task 6: Miner

**Files:**
- Create: `evals/real_api/miner/__init__.py`, `sources.py`, `specs.py`, `diff.py`, `candidates.py`; `evals/real_api/tests/test_miner.py`, `evals/real_api/tests/fixtures/spec_old.json`, `spec_new.json` (tiny hand-made OpenAPI docs)

**Interfaces:**
- Consumes: `Case`, `ExpectedMap` (Task 1)
- Produces:
  - `sources.SOURCES: list[Source]`, `Source(api: str, repo_url: str, spec_path: str, pairs: list[tuple[str,str]])` (old ref, new ref); start with `github/rest-api-description`, `stripe/openapi`, plus one or two more that actually yield cases (swap out any that don't)
  - `specs.fetch_spec(repo_dir: Path, ref: str, spec_path: str) -> dict`: `git show ref:path` on a local bounded-depth clone; `specs.flatten_schemas(spec: dict) -> dict[str, dict[str, FieldType]]`: schema name -> dotted field path -> type; resolves `$ref` with a visited-set cycle guard and a depth cap of 4; skips `oneOf/anyOf` and arrays of objects
  - `diff.diff_schema(old: dict[str,FieldType], new: dict[str,FieldType]) -> SchemaDiff` (`removed`, `added`, `retyped`)
  - `candidates.mine(source: Source, repo_dir: Path) -> list[Case]`: only schemas with at least one removed field AND at least one added or retyped field; pairs removed/added fields by identical type plus name similarity (normalized edit ratio >= 0.6) or identical leaf name at a different path; if more than one pairing is plausible, emit one case with `ambiguous=True`; unpaired removed fields with no plausible partner become `unmappable` cases; every case is `label_status="drafted"`, ids `<api>-<schema>-<n>`

- [ ] **Step 1: Write failing tests** against the fixture specs: `test_flatten_resolves_refs_and_skips_cycles` (a self-referencing schema terminates); `test_diff_finds_removed_added_retyped`; `test_mine_pairs_rename_and_move`; `test_mine_flags_ambiguous_pairing` (two same-type removed + two same-type added -> one case, `ambiguous=True`); `test_mine_emits_unmappable_for_orphan_removed_field`; `test_all_mined_cases_are_drafted`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** per Interfaces. Map OpenAPI `type`/`format` to `FieldType` (`string`+`date-time` -> `datetime`; anything else unmappable is skipped).
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(eval): spec-history miner`.

### Task 7: Review round-trip

**Files:**
- Create: `evals/real_api/review.py`, `evals/real_api/tests/test_review.py`

**Interfaces:**
- Consumes: `load_cases`, `save_case` (Task 1)
- Produces: `render_review(cases_dir: Path, out: Path) -> None` writes `REVIEW.md`: one row per drafted case (`id`, api, kind, old field -> new field per map or "REFUSE", ambiguous flag, source link) with a `[ ] confirm` / `[ ] fix` marker; `apply_review(cases_dir: Path, review: Path) -> dict[str,int]` flips ticked-confirm cases to `confirmed`, leaves unticked as `drafted`, and returns counts; a case marked `fix` is listed in the return value and left `drafted`.

- [ ] **Step 1: Write failing tests:** `test_render_lists_every_drafted_case`; `test_apply_confirms_only_ticked`; `test_apply_is_idempotent`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.** Parse ticked boxes from the table by case id; never edit any field other than `label_status`.
- [ ] **Step 4: Run tests.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(eval): review file round-trip`.

### Task 8: CLI and README

**Files:**
- Create: `evals/real_api/__main__.py`, `evals/real_api/README.md`, `evals/real_api/tests/test_cli.py`

**Interfaces:**
- Consumes: everything above
- Produces: `python -m evals.real_api` subcommands: `mine [--api NAME]` (clones into a temp dir, writes `cases/`), `review render|apply`, `run [--split test|dev] [--mode rules|agent|both]`, `report`. Default `--split test`.

- [ ] **Step 1: Write failing tests** with Typer or argparse's runner: `test_run_defaults_to_test_split`; `test_run_agent_without_key_exits_nonzero_with_message`; `test_review_render_then_apply_via_cli`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement** with `argparse` (no new deps). README covers: method, the three outcomes, dev/test rule, the honesty line, reproduce steps, and "numbers in the resume come from `results/*.md` only".
- [ ] **Step 4: Run** `pytest evals/real_api/tests -v` and `ruff check evals`. Expected: all PASS, no lint errors.
- [ ] **Step 5: Commit** `feat(eval): cli and readme`.

### Task 9: Produce the real result (operational, needs the human)

- [ ] **Step 1:** Run `python -m evals.real_api mine`. Expected: at least 90 drafted cases across 3+ APIs (over-mine so 70+ survive review). If an API yields few usable cases, swap it per the spec; report the real N rather than padding.
- [ ] **Step 2:** Check the mix: no API above 40%, 10-15 `unmappable`. Rebalance sources if not.
- [ ] **Step 3:** Run `python -m evals.real_api review render`, commit `cases/` and `REVIEW.md`, and **stop: Stuart reviews and ticks the file** (about 15-20 minutes). Do not continue without it.
- [ ] **Step 4:** `review apply`; commit the confirmed cases. This freezes the test split: from here, no Healer change is justified by a test-split result.
- [ ] **Step 5:** Run `--split dev --mode both` first, fix real Healer bugs it exposes (each with its own regression test in `tests/`), then run `--split test --mode both` exactly once for the headline. Save `results/<date>.md`.
- [ ] **Step 6:** Open a PR or hand Stuart the branch; the resume bullet uses only the numbers in `results/<date>.md`.
