"""Self-healing ingestion: fetch -> heal -> load into SQLite -> quarantine until approved."""

import contextlib
import os
import sqlite3
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "demo"))
from models import UserProfile  # noqa: E402

from aegis_healer import Healer, Pipeline  # noqa: E402

URL = os.environ.get("AEGIS_URL", "http://localhost:8000/v2.json")
DB = "pipeline.db"  # created in the current working directory


def fetch():
    response = httpx.get(URL, timeout=10)
    response.raise_for_status()
    return response.json()


def sink(records):
    with contextlib.closing(sqlite3.connect(DB)) as db, db:  # db: commit or roll back
        db.execute(
            "CREATE TABLE IF NOT EXISTS profiles "
            "(user_id TEXT PRIMARY KEY, email_address TEXT, full_name TEXT, payload TEXT)"
        )
        db.executemany(
            "INSERT OR REPLACE INTO profiles "
            "(user_id, email_address, full_name, payload) VALUES (?, ?, ?, ?)",
            [(str(r.user_id), r.email_address, r.full_name, r.model_dump_json()) for r in records],
        )


def main():
    # 1.0: every agent fix waits for `aegis review` (rule-based fixes still apply on their own).
    run = Pipeline(fetch, UserProfile, sink, healer=Healer(auto_apply_threshold=1.0)).run()
    print(f"status:      {run.status}")
    print(f"loaded:      {run.loaded}")
    print(f"quarantined: {run.quarantined}")
    print(f"pending_id:  {run.pending_id}")
    if run.pending_id is not None:
        print("Fix needs review: run `aegis review`, then run this script again.")


if __name__ == "__main__":
    main()
