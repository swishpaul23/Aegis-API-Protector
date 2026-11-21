# Aegis Phase 3: Meaning Drift — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect "same field name, different meaning" drift (dollars→cents, seconds→milliseconds, UTC→local time, `active`→`A`) from a learned per-field baseline, block affected records by default (configurable per field), and let a human either apply a proposed fix or keep the data as correct, using the existing pending/quarantine/replay flow.

**Architecture:** A new `aegis_healer.meaning` package: `profile.py` (aggregate per-field baseline stored in `.aegis/profiles/`), `detect.py` (rules comparing a batch to the baseline), `config.py` (per-field block/flag/off policy from `.aegis/config.toml`), `resolve.py` (the "keep data" decision). The Healer runs the check after schema healing, on the post-mapping records. A scale fix is data: an optional `scale` factor on `FieldMapping`, applied after the transform.

**Tech Stack:** Python ≥3.11 (stdlib `tomllib`, `math`, `statistics`), Pydantic v2, existing aegis_healer modules. No LLM calls in this phase.

**Spec:** Design approved by the user on 2026-11-20 (conversation): baseline from healthy runs (aggregate counts only); checks for scale (~100×/1000×), timezone, category; per-field config with **block as default** (`block | flag | off`); meaning fixes never auto-apply; review offers apply-fix or keep-data (resets the field baseline); replay never releases records tied to an unresolved fix. Plus the Global Constraints of `docs/superpowers/plans/2026-11-20-hitl-pipeline.md`.

## Global Constraints

- Everything in `docs/superpowers/plans/2026-11-20-hitl-pipeline.md` → Global Constraints still holds (HITL rule, privacy default `masked`, `.aegis/` committed with no payload values except `.aegis/quarantine/`, never print secrets, fakes-only tests, ruff clean, Windows-first: pathlib + explicit `encoding="utf-8"` on every text read/write).
- Meaning fixes are **never** auto-applied, regardless of threshold.
- No LLM calls anywhere in this phase.
- Profiles (`.aegis/profiles/*.json`) store only aggregates: histogram bucket counts, decimal-place counts, timezone-class counts, and category values that pass the privacy category rule (`agent/privacy.py`: fullmatch `^[A-Za-z][A-Za-z_-]*$`, length ≤ 20, all-lower or all-upper). Never raw numbers, free text, IDs or dates.
- Constants (module-level in `meaning/detect.py` / `meaning/profile.py`, exact values): `MIN_BASELINE = 20`, `MIN_BATCH = 5`, `BUCKETS_PER_DECADE = 4`, `SCALE_TRIGGER = 1.75` (decades), `SCALE_SNAP = 0.35` (decades), `SCALE_STEPS = (2, 3)` (decades → factors 100 and 1000), `TZ_BASELINE_SHARE = 0.95`, `TZ_BATCH_SHARE = 0.80`, `CATEGORY_BASELINE_SHARE = 0.95`, `CATEGORY_MAX_DISTINCT = 30`, `CATEGORY_NEW_SHARE = 0.50`, `DECAY_AT = 5000` (halve all counts of a field when its total exceeds this).
- Policy default `"block"`; config file `.aegis/config.toml`; unknown policy values → `ValueError` naming the key (not the value).

## Review Focus

1. **No false alarm on normal variation**: values that vary within ±1 decade (e.g. amounts 5–900 vs baseline 10–500) must not trigger. Test in Task 3.
2. **Fix composes with an existing rename**: if active mappings have `amt → amount`, the scale fix must keep reading `amt` (rebased), not break the rename. Test in Task 5.
3. **Replay gate**: quarantined records tied to a pending meaning fix are never released while it is `pending` or `rejected`, even though they validate. Test in Task 4.
4. **Keep-data resets the baseline**: after "keep", the next run on the same new-scale data is not flagged again. Test in Task 6.
5. **Mixed batch**: schema drift healed + meaning drift in the same run → schema fix saved as before, meaning-drifted records blocked, status `pending`. Test in Task 5.

---

### Task 1: `scale` on FieldMapping

**Files:** Modify `src/aegis_healer/types.py`, `src/aegis_healer/mapping.py`; Test `tests/test_scale.py`

**Interfaces:**
- `FieldMapping.scale: float | None = None` — must be finite and non-zero when set (validator). Applied by `map_value` *after* the transform. Only numbers (int/float, not bool) can be scaled; anything else → `ValueError("scale needs a number")`. Result: `value * scale`; if the result is a float with `.is_integer()` and the input was an int, return `int(result)`.
- Mapping signatures used for pending de-dup (`healer._mapping_sig`) include `scale`.

- [ ] Tests: `test_scale_after_transform` (`"1234"` with `to_int`, scale 0.01 → 12.34); `test_scale_int_stays_int` (1500 × 0.01 → 15, `type is int`); `test_scale_rejects_non_number` (str, bool); `test_scale_must_be_finite_nonzero` (0, inf, nan → ValidationError); `test_mapping_sig_includes_scale`.
- [ ] Run → FAIL; implement; full suite + ruff; commit `feat: scale factor on field mappings`.

---

### Task 2: Field profiles (the baseline)

**Files:** Create `src/aegis_healer/meaning/__init__.py`, `src/aegis_healer/meaning/profile.py`; Test `tests/test_meaning_profile.py`

**Interfaces:**
- `FieldProfile(BaseModel)`: `n: float = 0` (records observed with a non-null value), `numeric: dict[int, float]` (bucket → count; bucket = `round(log10(abs(v)) * BUCKETS_PER_DECADE)` for non-zero numbers), `zeros: float`, `decimals: dict[int, float]` (0,1,2,3,4 where 4 means "4 or more"), `tz: dict[str, float]` (keys `utc`, `offset`, `naive`), `categories: dict[str, float]`, `free_text: bool = False` (set once a non-category string or > CATEGORY_MAX_DISTINCT distinct values is seen; categories then cleared and no longer tracked).
- `ModelProfile(BaseModel)`: `model_key: str`, `fields: dict[str, FieldProfile]`, `updated_at: datetime | None`.
- `classify(value) -> tuple[str, Any]`: kinds `"number"` (int/float, not bool, finite), `"datetime"` (datetime objects, or strings with "-" at index 4 that `datetime.fromisoformat` parses after `Z`→`+00:00` and that contain a time part `T` or space), `"category"`, `"text"`, `"skip"` (None, bool, date-only strings, dicts, lists).
- `tz_class(value) -> str` for datetimes: `utc` if offset is zero, `offset` if non-zero, `naive` if none.
- `observe(profile: ModelProfile, records: list[dict], model: type[BaseModel]) -> None` updates counts for every leaf path in `model_paths(model)` present in each record (values via `get_path`); applies decay per field (`DECAY_AT`).
- `load_profile(root: Path, model_key: str) -> ModelProfile`, `save_profile(root: Path, profile: ModelProfile) -> None` → `<root>/profiles/<safe name>.json` (reuse `store.files._safe_name`; atomic write, UTF-8; corrupt file → WARNING with file name only and an empty profile).
- `reset_fields(profile: ModelProfile, paths: list[str]) -> None` deletes those fields' profiles.
- Helpers used by Task 3: `median_bucket(counts: dict[int, float]) -> float | None` (weighted median of bucket keys).

- [ ] Tests: classification table (ints, floats, bool, None, "2026-01-02T03:04:05Z", "2026-01-02", "active", "Alice Smith", "a3f1-…"); bucket/decimal counting; tz classes; category cap → free_text; decay halves counts past DECAY_AT; save/load round-trip; **privacy**: after observing records with emails/names/amounts, the JSON file contains none of those values (only bucket keys/counts and category tokens); corrupt file → empty profile + warning; reset_fields.
- [ ] Run → FAIL; implement; full suite + ruff; commit `feat: per-field baseline profiles`.

---

### Task 3: Meaning-drift detection rules

**Files:** Create `src/aegis_healer/meaning/detect.py`; Modify `src/aegis_healer/types.py` (add `MeaningFinding`); Test `tests/test_meaning_detect.py`

**Interfaces:**
- `MeaningFinding(BaseModel)`: `path: str`, `kind: Literal["scale", "timezone", "category"]`, `detail: str` (plain-English, value-free, e.g. `"typical value is ~100x larger than the baseline"`, `"timestamps changed from UTC to a local offset"`, `"3 of 5 values are new categories"`), `suggested_scale: float | None = None`, `baseline_n: int`, `batch_n: int`, `new_categories: list[str] = []` (category-rule tokens only).
- `detect(profile: ModelProfile, records: list[dict], model: type[BaseModel]) -> list[MeaningFinding]`, deterministic, findings sorted by path. A field is checked only if its baseline has `n >= MIN_BASELINE` and the batch has `>= MIN_BATCH` usable values of the same kind.
  - **scale:** `diff = (batch median of log10|v|) - (baseline median_bucket / BUCKETS_PER_DECADE)` over non-zero numbers. Finding when `abs(diff) >= SCALE_TRIGGER`. `k = round(diff)`; if `abs(k) in SCALE_STEPS` and `abs(diff - k) <= SCALE_SNAP` → `suggested_scale = 10 ** -k` (e.g. ~100x larger → 0.01), else `None`. Detail names the approximate factor (`~100x larger` / `~1000x smaller`, or `~10^d.d x` when not snapped).
  - **timezone:** baseline's dominant tz class has share `>= TZ_BASELINE_SHARE` and the batch's share of a *different* class `>= TZ_BATCH_SHARE`. No fix.
  - **category:** baseline not `free_text`, categories cover `>= CATEGORY_BASELINE_SHARE` of the field's string values, and the share of batch category values not in the baseline `>= CATEGORY_NEW_SHARE`. `new_categories` = sorted distinct new tokens (≤ 10). Free-text batch values on a category field also count as new (but are never listed). No fix.
- `records` are post-mapping dicts (the same shape the model validates).

- [ ] Tests: dollars→cents (baseline amounts 10–500, batch ×100) → scale finding, `suggested_scale == 0.01`; seconds→ms epoch → 0.001; percent 0.05→5 → 0.01; reverse (cents→dollars) → 100; ×10 → no finding; **normal variation within one decade → no finding (Review Focus 1)**; baseline under MIN_BASELINE → no finding; batch under MIN_BATCH → no finding; UTC→offset → timezone finding; UTC→naive → finding; mixed baseline (60/40) → no tz finding; `active/inactive` → `A/I` → category finding listing `["A", "I"]`; one new value among many known → no finding; free-text field → never a category finding; finding `detail`s contain no record values (assert the batch's raw numbers/strings are absent, except category tokens).
- [ ] Run → FAIL; implement; full suite + ruff; commit `feat: meaning drift detection rules`.

---

### Task 4: Config, store support, and the replay gate

**Files:** Create `src/aegis_healer/meaning/config.py`, `src/aegis_healer/meaning/resolve.py`; Modify `src/aegis_healer/store/__init__.py`, `src/aegis_healer/store/files.py`, `src/aegis_healer/quarantine.py`; Test `tests/test_meaning_config.py`, `tests/test_meaning_store.py`

**Interfaces:**
- `Policy = Literal["block", "flag", "off"]`; `MeaningConfig(BaseModel)`: `default: Policy = "block"`, `fields: dict[str, Policy] = {}`; `policy_for(model_key: str, path: str) -> Policy` — exact key `f"{model_key}.{path}"` wins, then a bare `path` key, then `default`.
- `load_config(root: Path) -> MeaningConfig` reads `<root>/config.toml` table `[meaning]` (keys `default`, `fields`) with `tomllib` (open in binary); missing file/table → defaults; invalid TOML or policy → `ValueError` naming the file or key only.
- `PendingPatch` gains `kind: Literal["schema", "meaning"] = "schema"` and `findings: list[MeaningFinding] = []`; status gains `"kept"`. `MappingStore.add_pending(..., kind="schema", findings=None)` (defaults keep existing callers working). `FileStore.keep_pending(pending_id: int) -> None` sets status `"kept"` (only for `kind == "meaning"` and status `pending`; else `ValueError`). Existing pending JSON without the new keys must still load.
- `resolve.keep_data(store: FileStore, pending_id: int) -> list[str]`: calls `keep_pending`, loads the model profile from `store.root`, `reset_fields` for the finding paths, saves, returns the reset paths.
- **Replay gate (Review Focus 3):** in `quarantine.replay`, a batch with `pending_id is not None` is only considered when `healer.store.get_pending(pending_id).status in ("approved", "kept")`; otherwise it's skipped entirely (counted in `still_blocked` by its record count, never released). A missing pending file → treat as unresolved. Batches with `pending_id None` behave as today.

- [ ] Tests: policy precedence; load from TOML (block/flag/off); invalid policy → ValueError mentioning the key; missing file → defaults; old pending JSON loads (kind schema, findings []); keep_pending only for meaning+pending; keep_data resets profile fields; replay: batch with pending status `pending` → not released even though records validate; `rejected` → not released; `approved`/`kept` → released; existing quarantine tests still pass.
- [ ] Run → FAIL; implement; full suite + ruff; commit `feat: meaning config, kept status, replay gate`.

---

### Task 5: Healer integration

**Files:** Modify `src/aegis_healer/healer.py`, `src/aegis_healer/runlog.py` (code `meaning_drift`), `src/aegis_healer/__init__.py` (export `MeaningFinding`); Test `tests/test_meaning_healer.py`

**Interfaces:**
- `Healer(..., meaning: MeaningConfig | bool | None = None)`: `None` → `load_config(store.root)` when the store has a root, else defaults; `False` → meaning checks off entirely; a `MeaningConfig` → used as given.
- `HealResult.findings: list[MeaningFinding] = []`.
- After the schema body returns `ok` or `healed` (statuses `pending`/`failed` skip the meaning check), in the `validate()` wrapper before quarantine/run-log/notify:
  1. Rebuild post-mapping dicts for the records that validated: re-extract raw records, apply the mappings in effect (`store.get_active(key)` when persisted, else `merge(active, result.applied)`), keep those that validate (same order as `result.records`).
  2. Load the profile (`store.root`), run `detect`, drop findings whose policy is `off`.
  3. No findings → if `persist`, `observe` all those post-mapping records and save the profile; result unchanged.
  4. Findings → for `block` fields: records with a usable value on a blocked field are removed from `result.records` and their **raw** dicts quarantined (pending id below); for `flag` fields: records stay. Build a meaning pending fix: mappings = one per finding with `suggested_scale` (source/target = the field path, rebased onto active renames with the existing `_rebase` so `amt → amount` keeps reading `amt` — Review Focus 2; if the active mapping for that target has a transform, copy it and set `scale`); `kind="meaning"`, `findings=findings`; de-dup with existing pending of kind meaning at the same base_version and the same `(path, kind, suggested_scale)` set. Status `pending`, `result.findings` set, message `"Meaning drift needs review: <paths>"` (paths only). Notify as for any new pending fix. The profile is **not** updated on a run with findings (the baseline must not learn the drift).
  5. Run log message code `meaning_drift` for these runs (privacy-gated like other codes).
- Meaning fixes are never applied by the threshold logic (they never go through the auto-apply path).

- [ ] Tests (fakes only): baseline built over 4 clean runs of 10 records → no findings; then cents batch → `pending`, records blocked + quarantined, finding with scale 0.01, pending kind meaning, notify once; second identical run → same pending id (de-dup), no second notification; `flag` policy → records loaded, still pending + finding; `off` → no finding; `meaning=False` → no profile file written; **mixed batch (Review Focus 5)**; **rename + scale composition (Review Focus 2)**: active `amt→amount`, cents data → proposed mapping source `amt`, scale 0.01, and after approve + replay the released record has dollars; profile not updated on a drift run; persist=False → no profile write, no quarantine.
- [ ] Run → FAIL; implement; full suite + ruff; commit `feat: meaning drift checks in the Healer`.

---

### Task 6: CLI, demo, docs

**Files:** Modify `src/aegis_healer/cli/review_cmd.py`, `src/aegis_healer/cli/check_cmd.py`, `src/aegis_healer/cli/__init__.py`; Create `src/aegis_healer/cli/profile_cmd.py`, `examples/meaning/` (`models.py`, `payments_v1.json` ~25 records in dollars, `payments_v2.json` same records in cents, `README.md`); Modify `README.md`, `examples/github-actions/README.md`; Test `tests/test_meaning_cli.py`

**Interfaces:**
- `aegis review` shows meaning fixes with a findings table (field, kind, detail, suggested fix) and prompt `[a]pply fix  [k]eep data  [s]kip  [d]etails` (`[a]` = `resolve_pending(id, True)`; for a finding without a suggested fix, `[a]` is not offered); flags `--keep ID` (calls `keep_data`). Schema fixes keep the existing prompt. After apply/keep, print the existing replay hint.
- `aegis check` prints meaning findings (field, kind, detail) when present.
- `aegis profile MODEL_KEY [--db]` prints per-field: records seen, kind, typical magnitude (`~10^x` from the median bucket), dominant timezone class, category count — no raw values.
- `aegis check --no-meaning` passes `meaning=False`.
- Demo: `examples/meaning/README.md` walks through (PowerShell): serve folder, run `aegis check …payments_v1.json` 3 times to build the baseline (25 records each, so baseline ≥ 20 after the first run), then `payments_v2.json` → blocked; `aegis review` → apply ×0.01; `aegis replay --model models:Payment --out released.jsonl`.
- README: "Meaning drift" section (what it catches, baseline, config TOML example with block/flag/off, apply vs keep, limits: timezone/category have no automatic fix; needs 20 records of history). GitHub Actions README: meaning fixes with a suggested scale can be approved by merging; "keep data" must be done with `aegis review --keep ID` locally.

- [ ] Tests: review interactive apply on a meaning fix; `--keep` resets baseline and marks kept (Review Focus 4: a following `check` on the same cents data is not flagged once the new baseline has ≥ 20 records — build it with clean runs after keep and assert no finding); `[a]` not offered when no suggestion; `aegis profile` output has no raw values; `--no-meaning`; the demo files load and the demo models validate them.
- [ ] Run → FAIL; implement; full suite + ruff; commit `feat: meaning drift CLI, demo and docs`.
