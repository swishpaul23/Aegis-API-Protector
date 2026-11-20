import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "github-actions" / "list_pending.py"


def _load():
    spec = importlib.util.spec_from_file_location("list_pending", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lists_only_pending_and_skips_corrupt(tmp_path):
    root = tmp_path / ".aegis" / "pending"
    root.mkdir(parents=True)
    (root / "0001.json").write_text(json.dumps({"id": 1, "status": "pending"}), encoding="utf-8")
    (root / "0002.json").write_text(json.dumps({"id": 2, "status": "approved"}), encoding="utf-8")
    (root / "0003.json").write_text("{not json", encoding="utf-8")
    (root / "0004.json").write_text("[1, 2]", encoding="utf-8")
    (root / "0005.json").write_text(json.dumps({"status": "pending"}), encoding="utf-8")
    (root / "0006.json").write_bytes(b"\xff\xfe\x00bad")
    (root / "0007.json").write_text(json.dumps({"id": 7, "status": "pending"}), encoding="utf-8")
    assert _load().list_pending(str(root)) == ["1", "7"]


def test_missing_dir_is_empty(tmp_path):
    assert _load().list_pending(str(tmp_path / "nope")) == []


def test_workflow_heredoc_matches_script(tmp_path):
    import re
    import subprocess
    import sys

    import yaml

    wf = yaml.safe_load((SCRIPT.parent / "aegis-drift.yml").read_text(encoding="utf-8"))
    run = next(s["run"] for s in wf["jobs"]["check"]["steps"] if "<<'PY'" in s.get("run", ""))
    body = re.search(r"<<'PY'\n(.*?)\nPY\n", run, re.S).group(1)
    root = tmp_path / ".aegis" / "pending"
    root.mkdir(parents=True)
    (root / "0001.json").write_text(json.dumps({"id": 1, "status": "pending"}), encoding="utf-8")
    (root / "0002.json").write_text(json.dumps({"id": 2, "status": "approved"}), encoding="utf-8")
    (root / "0003.json").write_text("{bad", encoding="utf-8")
    (root / "0004.json").write_bytes(b"\xff\xfe")
    (root / "0005.json").write_text(json.dumps({"status": "pending"}), encoding="utf-8")
    (root / "0006.json").write_text(json.dumps({"id": "x7", "status": "pending"}), encoding="utf-8")
    env = {"PYTHONIOENCODING": "utf-8"}
    heredoc = subprocess.run(
        [sys.executable, "-c", body], cwd=tmp_path, capture_output=True, text=True, env=env
    ).stdout
    script = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True, env=env
    ).stdout
    assert heredoc == script == "1\nx7\n"
