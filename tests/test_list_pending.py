import importlib.util
import json
from pathlib import Path

import pytest

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

    yaml = pytest.importorskip("yaml")
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


def _workflow():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((SCRIPT.parent / "aegis-drift.yml").read_text(encoding="utf-8"))
    return {s.get("name", ""): s for s in wf["jobs"]["check"]["steps"]}


def test_workflow_install_requires_pip_spec():
    step = _workflow()["Install Aegis"]
    assert step["env"]["AEGIS_PIP_SPEC"] == "${{ vars.AEGIS_PIP_SPEC }}"
    assert "'aegis-healer'" not in json.dumps(step)
    assert 'if [ -z "$AEGIS_PIP_SPEC" ]' in step["run"] and "exit 1" in step["run"]


def test_workflow_gate_and_url_secret():
    steps = _workflow()
    assert 'startswith("aegis/")' in steps["Skip while an Aegis PR is open"]["run"]
    url = steps["Check for drift"]["env"]["AEGIS_URL"]
    assert url == "${{ secrets.AEGIS_URL || vars.AEGIS_URL }}"


def test_workflow_run_blocks_parse_with_bash(tmp_path):
    import shutil
    import subprocess

    if not shutil.which("bash"):
        pytest.skip("bash not available")
    for name, step in _workflow().items():
        if "run" in step:
            script = tmp_path / "step.sh"
            script.write_text(step["run"], encoding="utf-8")
            res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            assert res.returncode == 0, (name, res.stderr)
