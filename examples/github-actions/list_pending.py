"""Print the open pending fixes as "<id> <kind>" lines (kind: schema or meaning).

Keep this file identical in logic to the heredoc in aegis-drift.yml.
"""

import json
import pathlib
import sys


def list_pending(root: str = ".aegis/pending") -> list[str]:
    lines = []
    for path in sorted(pathlib.Path(root).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            kind = data.get("kind", "schema")  # files from before meaning drift are schema
            if data.get("status") == "pending" and kind in ("schema", "meaning"):
                lines.append(f"{data['id']} {kind}")
        except (OSError, ValueError, AttributeError, KeyError):
            continue  # unreadable or malformed: skip
    return lines


if __name__ == "__main__":
    for line in list_pending(*sys.argv[1:2]):
        print(line)
