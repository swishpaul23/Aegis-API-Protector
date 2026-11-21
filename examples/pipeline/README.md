# Pipeline example: self-healing ingestion with human review

`run_pipeline.py` fetches the demo API, heals schema drift, loads valid records into a local
SQLite file (`pipeline.db`, table `profiles`), and quarantines records it cannot load until
you approve the proposed fix.

Windows PowerShell, venv active, `ANTHROPIC_API_KEY` in `.env` or the environment. Run
everything from `examples\pipeline` so `.aegis\` and `pipeline.db` land there.

1. Serve the demo API (terminal 1):

   ```powershell
   cd examples\demo
   python -m http.server 8000
   ```

2. In `examples\demo\models.py`, change `user_id: int` to `user_id: int | str` (the drifted
   API sends string IDs).

3. Run the pipeline (terminal 2):

   ```powershell
   cd examples\pipeline
   python run_pipeline.py
   ```

   Valid records are loaded. The script uses `Healer(auto_apply_threshold=1.0)`, so every
   agent fix waits for your review (otherwise a confident fix would apply on its own and
   there would be nothing to approve). Records that need the fix are quarantined under
   `.aegis\quarantine\` and the output shows a `pending_id`. Running it again reuses the
   pending fix: no new agent calls, no second pending fix, no duplicate quarantined records.

4. Review and approve the fix:

   ```powershell
   aegis review
   ```

5. Run it again. The approved mappings are applied, the quarantined records are released into
   the sink, and the quarantine batch is deleted only after the insert succeeded. If some
   records still fail with the approved fix, only those stay in the batch:

   ```powershell
   python run_pipeline.py
   ```

Point at another API with `$env:AEGIS_URL = "http://localhost:8000/v1.json"`.

Notifications: `AEGIS_WEBHOOK_URL` is read by the `aegis` CLI only. In a script, pass
`Healer(on_pending=webhook_notifier(url))` from `aegis_healer.notify`.

Quarantined records are kept until they are replayed (no automatic purge yet); delete
`.aegis\quarantine\` to discard them. Records that failed validation (schema batches) are
released as soon as they validate with the current mappings. Records blocked for meaning
drift (same field, new meaning, e.g. dollars to cents) sit in their own batch and are
released only after you apply the fix or keep the data in `aegis review`; if you reject it,
the batch is dead: `aegis replay --model ... --discard-dead` deletes it. The meaning check
runs on every run, also when the schema fix is still pending, so drifted records are never
loaded. Use one writer per `.aegis\`: if the GitHub PR flow
also approves fixes for this folder, pull its merged changes before running the script.

Sinks should be idempotent (upsert by key): an API may re-serve records that were already
loaded or quarantined. This example uses `INSERT OR REPLACE` keyed on `user_id`.
