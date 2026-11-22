# Aegis | API Healer

Detect when an API you depend on changes shape, and repair your pipeline's field mappings
automatically: plain rules first, a LangGraph agent (Claude Haiku) only for the hard cases,
and a validation gate in front of everything.

```bash
pip install aegis-healer
```

## Why

APIs drift: `user_name` becomes `userName`, an address moves under `contactInfo`, a date
becomes a Unix timestamp. Pipelines either crash or, worse, silently load bad data. Aegis
catches the drift, proposes a fix, proves the fix works on real records, and only then
applies it.

## Library

```python
import httpx
from aegis_healer import Healer
from app.models import User  # your Pydantic model: the contract you expect

healer = Healer(auto_apply_threshold=0.9)
result = healer.validate(httpx.get(URL).json(), User, records_path="data")

result.status  # "ok" | "healed" | "pending" | "failed"
result.records  # list[User], validated
result.raise_for_status()
```

## CLI

```bash
aegis init   https://api.example.com/users --name User   # generate models.py from a live response
aegis check  https://api.example.com/users --model models:User [--threshold 0.9] [-H "Authorization: Bearer ..."] [--privacy masked] [--max-llm-calls 10] [--no-meaning]
aegis review [--approve ID] [--reject ID] [--keep ID]     # interactive by default
aegis history models.User
aegis rollback models.User 2
aegis runs [--limit N]                                    # run log
aegis replay --model models:User [--batch ID] [--out FILE] [--discard-dead]
aegis profile models.User                                 # learned baseline per field
```

`aegis check` exits `0` (ok or healed), `1` (fix pending review) or `2` (failed), so it
drops straight into CI.

## How it works

```
detect → diff → rule matcher → (agent, only for what's left) → validate → apply or queue
```

1. **Detect**: validate records against your model and list missing, unexpected and retyped fields.
2. **Rule matcher**: fixes only drift with exactly one sensible answer: same name in a
   different style (`userName` → `user_name`), same name in a new place when the field was
   just wrapped or unwrapped (`contact.email` → `email`), or a safe type conversion. A
   field whose parent object changed (`billing.currency` → `limits.currency`) goes to the
   agent instead. If two fields could match, it doesn't guess.
3. **Agent**: a LangGraph loop (propose → check → retry with the errors) on the fields the
   rules couldn't settle. It can also declare a field **unmappable** with a reason, e.g.
   a UUID string can't become an `int` without inventing data.
4. **Validate**: the full fix is applied to real sample records and must pass your model.
5. **Apply or queue**: rule fixes need no approval. Agent fixes apply automatically only if
   every one is at or above `auto_apply_threshold` (default `0.9`; `1.0` means a human
   approves every agent fix). Everything else waits in `aegis review`.

## Self-healing pipeline

`Pipeline` wraps fetch, heal and load, and holds back records you can't load safely yet:

```python
from aegis_healer import Healer, Pipeline


def fetch():
    return httpx.get(URL).json()


def sink(records):  # must be idempotent: upsert by key
    ...


run = Pipeline(fetch, User, sink, healer=Healer(), records_path="data").run()
run.status, run.loaded, run.quarantined, run.pending_id
```

```
drift -> rules -> agent -> confidence >= threshold ?
    yes: apply -> sink
    no:  pending fix + quarantine records + notify
         -> human review (approve) -> replay quarantined records -> sink
```

Each run fetches, heals, loads valid records, then replays quarantined records whose fix was
approved since. Sinks must be idempotent because an API may re-serve records. While a fix is
pending, later runs reuse it (no new agent calls, no second pending fix). See
`examples/pipeline`.

Use one writer per `.aegis/` folder. If you also run the GitHub PR flow
(`examples/github-actions`) against the same `.aegis/`, pull the merged approvals into the
Pipeline host before its next run, and don't let both write mappings at the same time.

## Human review

- **CLI**: `aegis review` (interactive), or `aegis review --approve ID` / `--reject ID`
  (and `--keep ID` for meaning fixes, see Meaning drift).
- **Webhook**: get pinged when a fix goes pending (see Notifications). `AEGIS_WEBHOOK_URL`
  is read by the `aegis` CLI only; library and `Pipeline` users pass
  `Healer(on_pending=webhook_notifier(url))`.
- **Pull requests**: `examples/github-actions` runs `aegis check` on a schedule and opens a
  PR per fix. Merging the PR is the approval; closing it means "not this fix" (the next
  run proposes again unless you change the model or set `AEGIS_PAUSED=true`).

## Meaning drift

Same field name, same type, different meaning: `amount` goes from dollars to cents, or
timestamps go from UTC to local time. Schema validation passes, so this is the quiet kind of
break. Aegis learns a baseline per field from healthy runs (aggregate counts only: size
buckets, decimal places, timezone classes, short lowercase or uppercase category words; never
raw numbers, text, IDs or dates; stored in `.aegis/profiles/`, see `aegis profile MODEL_KEY`)
and compares each new batch to it. No LLM is involved.

What can end up in committed files: category words are any short all-lowercase or
all-uppercase word (letters, `_`, `-`, at most 20 characters), so besides status codes like
`active` or `USD`, a field of short usernames or codes could store some of them in
`.aegis/profiles/` and in the findings of `.aegis/pending/` files. Set such fields to `off`
(below) if that matters. Numeric strings such as `"12.50"` are treated as text: they are not
scale-checked.

It catches:

- **Scale**: a number field about 100x or 1000x bigger or smaller (dollars/cents,
  seconds/milliseconds, 0.05/5 percent). Suggested fix: a `scale` on the mapping.
- **Timezone**: a datetime field whose dominant timezone class changed (UTC to offset or
  naive, or any other switch).
- **Category**: a category field that suddenly uses new values (`active` becomes `A`).

A field needs 20 records of history before it is checked, and a batch needs 5 records.
Every run is checked, including runs whose schema fix is pending or failed: valid records
with meaning drift are never loaded. The baseline only learns from fully healthy runs (no
findings, status `ok` or `healed`), so it can't learn the drift. The affected records are
blocked and quarantined in their own batch, and a pending meaning fix is created. On a
schema-clean run the status becomes `pending` (exit code 1); on a run whose schema fix is
pending or failed the status (and exit code) stays, `HealResult.meaning_pending_id` names the
meaning fix and the message ends with `; meaning drift needs review: <fields>`. Meaning fixes
are never auto-applied, whatever the threshold. If the API drifts in schema again before you
decide, the new meaning fix for the same drift replaces the old one (marked `superseded`) and
takes over its quarantined records, so one decision releases them all.

Choose what happens per field in `.aegis/config.toml` (`block` is the default):

```toml
[meaning]
default = "block"          # block | flag | off

[meaning.fields]
amount = "block"                 # a bare field path
"app.models.Payment.note" = "flag"   # model key + path wins over a bare path
created_at = "off"
```

`block` quarantines the records, `flag` loads them but still opens the pending fix and prints
the finding, `off` skips the field. `aegis check --no-meaning` skips all meaning checks for
one run.

Resolve a meaning fix in `aegis review`:

- **[a]pply fix**: the suggested scale becomes a mapping (it composes with renames), then
  `aegis replay` releases the blocked records converted. Only offered when there is a
  suggested fix.
- **[k]eep data** (or `aegis review --keep ID`): the new values are the new normal. The
  baseline for the field is reset and rebuilds from the next runs; the blocked records can
  then be replayed as they are. Use this when the API really changed and your model expects it.
- **[r]eject**: the blocked records are never released; their batch becomes dead. Delete it
  with `aegis replay --model ... --discard-dead`.
- **[s]kip**: decide later. Blocked records stay quarantined.

Known limitations:

- Timezone and category drift have no automatic fix: keep the data, or fix it upstream.
- A field needs 20 records of history before it is checked.
- Records are meaning-checked when a run first sees them. Only records quarantined for
  *schema* drift and later released by replay (after the schema fix is approved) skip the
  check: replay re-validates them against the mappings, not against the baseline.
- Small batches (5 records) against a very wide baseline can occasionally false-alarm; set
  noisy fields to `flag` or `off`.

See `examples/meaning` for a runnable dollars-to-cents demo.

## Privacy

What the agent sees of your API data is set by `--privacy` / `Healer(privacy=)`:

| Mode | Sent to the LLM |
|---|---|
| `full` | Real sample values |
| `masked` (default) | Values replaced by shape-preserving placeholders |
| `schema` | Field names and types only, no values |

Residual risk in `masked`: short single-word values such as `"active"` and epoch-like
numbers pass through by design, because the agent needs them to spot status flags and
timestamps. For highly sensitive APIs use `--privacy schema`.

## Budget and cost

`--max-llm-calls` (default 10) / `Healer(max_llm_calls=)` caps agent calls per run.
`HealResult.usage` (calls, tokens, est_cost_usd) is shown in the `aegis check` output.
At Haiku 5.5's $0.10 / $0.50 per million input / output tokens, an agent call costs about
$0.0005. The agent only runs when the rules can't fix the drift.

## Run log

Every check appends a line to `.aegis/runs.jsonl`; `aegis runs` shows recent runs.
Messages are categorical (no API values) unless you use `--privacy full`.

## Quarantine & replay

Records that can't be loaded yet are saved under `.aegis/quarantine/` (gitignored: it may
hold PII), in two kinds of batch:

- **Schema** batches hold records that failed validation. Replay releases every record that
  validates with the current mappings, whatever happened to the fix (approved, rejected,
  or a later fix that heals them).
- **Meaning** batches hold records that validated but whose values changed meaning. They
  wait on their meaning fix: released only once it is approved or kept; still blocked while
  it is pending (or missing). A batch of a rejected meaning fix is **dead**: replay skips it
  and doesn't count it as blocked; delete it with `aegis replay --discard-dead`.

`Pipeline.run()` replays them into your sink and changes the quarantine only after the
insert succeeded. Release is partial: every record that now validates is released; a batch
with records that still fail (say, one value no mapping can fix) keeps just those records,
under the same batch id.

`aegis replay --model models:User` previews what would be released without changing
anything. With `--out FILE` (a new file; existing files are never overwritten) it writes the
released records, de-duplicated, **then removes cleared batches and shrinks partial ones**.

Quarantined records are kept until they are replayed: there is no automatic purge yet.
`aegis replay --model ... --discard-dead` deletes dead meaning batches; to discard everything,
delete `.aegis/quarantine/`.

## Notifications

For the `aegis` CLI, set `AEGIS_WEBHOOK_URL` (https; http only for localhost). In Python
(`Healer`, `Pipeline`) the variable is not read: pass
`Healer(on_pending=webhook_notifier(url))` (from `aegis_healer.notify`). The POST body is
JSON with a `text` field, so Slack and Discord-style webhooks work. Redirects are never
followed, and proxy and custom CA environment variables are ignored on purpose, so a
notification can't be rerouted.

## Get an Anthropic API key

1. Open the Console at platform.claude.com and sign in.
2. Settings, Billing: add credit.
3. Workspaces: create a workspace named `aegis`, and set a Monthly limit.
4. API Keys: Create key in that workspace. Copy it now; it is shown once and starts `sk-ant-`.
5. Put it in `.env` (gitignored): `ANTHROPIC_API_KEY=sk-ant-...`

## Writing your own Proposer

Any object with `propose(request, check, budget=None)` and a `last_usage` (`Usage`) works
(an older `propose(request, check)` is still called, without the budget argument):
return a patch that passed `check(patch)`, or raise `AgentError`. Honour `budget` (max LLM
calls for this invocation) and set `last_usage` for each call, even when it raised, so
the Healer can enforce the run budget. Pass it as `Healer(proposer=...)`.

## Where fixes live

```
.aegis/
  mappings/<model>.json          current mappings
  history/<model>/0001.json      every version, never edited
  pending/0001.json              fixes waiting for review
  quarantine/                    held-back records (gitignored)
  runs.jsonl                     run log
```

Plain JSON, written atomically. Commit `.aegis/` to git and every mapping change becomes
reviewable, diffable and revertible. In read-only deployments use
`Healer(persist=False)` (or `aegis check --dry-run`) to heal in memory without writing.
The store sits behind an interface, so a shared database backend can be added for teams
running many workers.

## Security model

- **Fixes are data, not code.** A fix is a list of mappings with a transform from a fixed
  allow-list: `none, to_int, to_float, to_str, to_bool, parse_datetime, epoch_to_iso,
  value_map, concat`. LLM output is never executed.
- **API data is untrusted.** Payload samples are truncated and wrapped as data in the
  prompt. The agent may only map fields it was asked about, from fields that exist, and
  every fix must pass validation, so a payload that says "ignore your instructions" can't
  produce an applied change. Keys are JSON-quoted and `<`/`>` escaped so payload text
  can't forge prompt sections. A proposal that fails its checks is never returned, and
  the Healer re-checks scope before applying anything.
- **Secrets**: `ANTHROPIC_API_KEY` is read from the environment only. CLI header values
  are never printed.
- **Fetching**: `http`/`https` only, 30 s overall deadline, 10 MB cap, at most 5 redirects.
  Redirects are followed by hand: your `--header` values are only sent to the original
  host, and https never downgrades to http. Query strings are hidden in output.
- **Terminal output**: text that came from the API or the LLM is escaped before printing,
  so it can't fake rows or hide text in `aegis review`.
- **`aegis init`** turns untrusted API keys into Python: field names are sanitised to
  identifiers, and original keys only appear as string aliases.

## Configuration

| Setting | Where |
|---|---|
| Auto-apply threshold | `Healer(auto_apply_threshold=)` / `--threshold` |
| Anthropic key | `ANTHROPIC_API_KEY` (the CLI also reads `.env`) |
| Model | `AEGIS_MODEL` (default `claude-haiku-5-5`) |
| Store folder | `AEGIS_DIR` / `--db` (default `.aegis`) |

Without an API key, Aegis runs in rules-only mode.

## Limitations (v0.1)

- Drift *inside* lists of objects (`items[].qty`) is detected as failed records but not
  repaired; lists are treated as single values.
- Models with `extra="forbid"` fail when the API adds a field; there's no "drop" mapping yet.
- Meaning drift detection is statistical, see its limits under Meaning drift.

## Development

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
ruff check .
```

macOS/Linux: `source .venv/bin/activate` instead of the second line.

See `examples/demo` for an end-to-end run against the original Phase 1 mock API data.
The Phase 1 Docker/FastAPI/Postgres build is kept for reference in `legacy/phase1/`.

## Roadmap

- Shared store backends (Postgres) and run-result sinks (Databricks Delta, S3 + Athena)

## License

MIT
