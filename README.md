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
from app.models import User          # your Pydantic model: the contract you expect

healer = Healer(auto_apply_threshold=0.9)
result = healer.validate(httpx.get(URL).json(), User, records_path="data")

result.status      # "ok" | "healed" | "pending" | "failed"
result.records     # list[User], validated
result.raise_for_status()
```

## CLI

```bash
aegis init   https://api.example.com/users --name User   # generate models.py from a live response
aegis check  https://api.example.com/users --model models:User [--threshold 0.9] [-H "Authorization: Bearer ..."]
aegis review                                              # approve / reject pending fixes
aegis history models.User
aegis rollback models.User 2
```

`aegis check` exits `0` (ok or healed), `1` (fix pending review) or `2` (failed), so it
drops straight into CI.

## How it works

```
detect → diff → rule matcher → (agent, only for what's left) → validate → apply or queue
```

1. **Detect**: validate records against your model and list missing, unexpected and retyped fields.
2. **Rule matcher**: fixes only drift with exactly one sensible answer: same name in a
   different style (`userName` → `user_name`), same name in a new place
   (`contact.email` → `email`), or a safe type conversion. If two fields could match, it
   doesn't guess.
3. **Agent**: a LangGraph loop (propose → check → retry with the errors) on the fields the
   rules couldn't settle. It can also declare a field **unmappable** with a reason, e.g.
   a UUID string can't become an `int` without inventing data.
4. **Validate**: the full fix is applied to real sample records and must pass your model.
5. **Apply or queue**: rule fixes need no approval. Agent fixes apply automatically only if
   every one is at or above `auto_apply_threshold` (default `0.9`; `1.0` means a human
   approves every agent fix). Everything else waits in `aegis review`.

## Where fixes live

```
.aegis/
  mappings/<model>.json          current mappings
  history/<model>/0001.json      every version, never edited
  pending/0001.json              fixes waiting for review
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
  produce an applied change.
- **Secrets**: `ANTHROPIC_API_KEY` is read from the environment only. CLI header values
  are never printed.
- **Fetching**: `http`/`https` only, 10 s timeout, 10 MB cap, at most 5 redirects.
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

## Development

```bash
python -m venv .venv && .venv/Scripts/activate     # Windows (source .venv/bin/activate elsewhere)
pip install -e ".[dev]"
pytest && ruff check .
```

See `examples/demo` for an end-to-end run against the original Phase 1 mock API data.
The Phase 1 Docker/FastAPI/Postgres build is kept for reference in `legacy/phase1/`.

## Roadmap

- GitHub Action that runs `aegis check` on a schedule and opens a PR with the fix
- Meaning drift: same field name, different meaning (dollars → cents, UTC → local time)
- Shared store backends (Postgres) and run-result sinks (Databricks Delta, S3 + Athena)

## License

MIT
