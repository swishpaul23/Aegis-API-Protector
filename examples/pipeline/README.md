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

   Valid records are loaded. Records that need a low-confidence fix are quarantined under
   `.aegis\quarantine\` and the output shows a `pending_id`. Running it again does not create
   a second pending fix or duplicate quarantined records.

4. Review and approve the fix:

   ```powershell
   aegis review
   ```

5. Run it again. The approved mappings are applied, the quarantined records are released into
   the sink, and the quarantine batch is deleted only after the insert succeeded:

   ```powershell
   python run_pipeline.py
   ```

Point at another API with `$env:AEGIS_URL = "http://localhost:8000/v1.json"`.

Sinks should be idempotent (upsert by key): an API may re-serve records that were already
loaded or quarantined. This example uses `INSERT OR REPLACE` keyed on `user_id`.
