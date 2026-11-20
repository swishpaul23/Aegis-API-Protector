# Demo: Phase 1 vendor API drift

`v1.json` is the original API. `v2.json` is the drifted one: camelCase keys, `full_name`
split in two, `is_active` became `status: "active"`, the signup date became a Unix
timestamp, the address moved under `contactInfo.mailingAddress`, and `user_id` became a
string ID.

```powershell
cd examples\demo
python -m http.server 8000          # terminal 1
```

In terminal 2 (same folder, venv active, `ANTHROPIC_API_KEY` in `.env` or the environment).
Aegis creates its `.aegis\` folder in whatever folder you run it from, here `examples\demo`.
Without a key, steps 2 to 4 stop at "agent disabled" (rules only).

```powershell
# 1. Original API: no drift (exit 0)
aegis check http://localhost:8000/v1.json --model models:UserProfile

# 2. Drifted API. The matcher fixes emailAddress and city, the agent proposes the rest,
#    and refuses user_id: "a3f1-b8c2-1001" is not an int (exit 2).
aegis check http://localhost:8000/v2.json --model models:UserProfile

# 3. That's a real contract change, so you decide: edit models.py,
#    user_id: int  ->  user_id: int | str
# 4. Run again: healed, or pending review if any agent fix is below 0.9 confidence
aegis check http://localhost:8000/v2.json --model models:UserProfile
aegis review

# 5. Mappings are now in .aegis/ (commit them). A second run is clean:
aegis check http://localhost:8000/v2.json --model models:UserProfile
aegis history models.UserProfile
```

Want a model for a new API? `aegis init http://localhost:8000/v1.json --name UserProfile --out mine.py`
(it infers `signup_date: datetime` from the data, where the hand-written `models.py` uses `str`:
both are valid contracts.)
