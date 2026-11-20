"""Print the ids of pending fixes, one per line.

Keep this file identical in logic to the heredoc in aegis-drift.yml.
"""

import json
import pathlib
import sys


def list_pending(root: str = ".aegis/pending") -> list[str]:
    ids = []
    for path in sorted(pathlib.Path(root).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") == "pending":
                ids.append(str(data["id"]))
        except (OSError, ValueError, AttributeError, KeyError):
            continue  # unreadable or malformed: skip
    return ids


if __name__ == "__main__":
    for pending_id in list_pending(*sys.argv[1:2]):
        print(pending_id)
