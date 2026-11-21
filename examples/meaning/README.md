# Demo: meaning drift (dollars become cents)

The field names and types never change, so schema checks pass. Only the meaning does.
`payments_v1.json` has 25 payments with `amount` in dollars (12.50 to 480.00).
`payments_v2.json` is the same 25 payments with `amount` in cents (x100).

```powershell
cd examples\meaning
python -m http.server 8000          # terminal 1
```

In terminal 2 (same folder, venv active). No API key is needed: meaning checks never call
the LLM. Aegis creates `.aegis\` in the folder you run it from, here `examples\meaning`.

Aegis learns what "normal" looks like from healthy runs. Each `check` adds this file's 25
records to the baseline, and a field needs 20 records of history before it is checked, so one
clean run is enough. Run it twice to see the baseline grow.

```powershell
# 1. Original API, twice: no drift (exit 0). The baseline now holds 50 amounts.
aegis check http://localhost:8000/payments_v1.json --model models:Payment
aegis check http://localhost:8000/payments_v1.json --model models:Payment
aegis profile models.Payment          # counts, kind, typical size: never the values

# 2. The API switches to cents. The model still validates, but amount is ~100x bigger.
#    The records are blocked and quarantined; a fix is pending (exit 1).
aegis check http://localhost:8000/payments_v2.json --model models:Payment

# 3. Review. [a]pply fix = read amount x0.01 from now on. [k]eep data = the new values
#    are the new normal (use this if cents is intended and your model expects it).
aegis review

# 4. Release the blocked records, now converted back to dollars
aegis replay --model models:Payment --out released.jsonl
```

Try `[k]eep data` at step 3 instead (`aegis review --keep 1`): the baseline for `amount`
resets, the cents values become the new normal after a couple of runs on `payments_v2.json`,
and `aegis replay` releases the blocked records as they are. To skip the checks for one run,
add `--no-meaning` to `aegis check`.
