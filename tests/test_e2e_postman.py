"""Run generated collections with newman / Postman CLI against the demo server.

Skipped when neither runner is installed.
"""

import json
import os
import shutil
import subprocess

import pytest

from mcp2rest2postman.cli import main

POSTMAN = os.environ.get("POSTMAN_CLI") or shutil.which("postman")
NEWMAN = shutil.which("newman")

pytestmark = pytest.mark.skipif(not (NEWMAN or POSTMAN), reason="newman / postman CLI not installed")


def run_collection(path, report, environment=None):
    env_args = ["-e", str(environment)] if environment else []
    if NEWMAN:
        cmd = [NEWMAN, "run", str(path), *env_args, "-r", "json", "--reporter-json-export", str(report)]
        subprocess.run(cmd, capture_output=True, timeout=120)
        run = json.loads(report.read_text())["run"]
        return run["stats"]["assertions"]["total"], len(run["failures"])
    proc = subprocess.run(
        [POSTMAN, "collection", "run", str(path), *env_args], capture_output=True, text=True, timeout=120
    )
    return None, proc.returncode


def test_generated_collection_passes_against_server(tmp_path, http_url):
    col = tmp_path / "c.json"
    assert main(["generate", "-q", "--url", http_url, "-o", str(col)]) == 0
    total, failures = run_collection(col, tmp_path / "r.json")
    assert failures == 0
    if total is not None:
        assert total > 100


def test_drift_is_detected(tmp_path, json_url):
    snap = tmp_path / "s.json"
    assert main(["generate", "-q", "--url", json_url, "-o", str(tmp_path / "x.json"), "--snapshot-out", str(snap)]) == 0
    data = json.loads(snap.read_text())
    data["tools"][0]["inputSchema"]["required"] = ["a"]  # pretend the contract changed
    data["tools"].append({"name": "ghost", "inputSchema": {"type": "object"}})
    snap.write_text(json.dumps(data))
    col = tmp_path / "c.json"
    assert main(["generate", "-q", "--from-snapshot", str(snap), "--base-url", json_url, "-o", str(col)]) == 0
    _, failures = run_collection(col, tmp_path / "r.json")
    assert failures > 0


def test_body_variables_collection_passes_with_environment(tmp_path, http_url):
    col = tmp_path / "c.postman_collection.json"
    env = tmp_path / "c.postman_environment.json"
    assert main(["generate", "-q", "--url", http_url, "--body-variables", "-o", str(col)]) == 0
    assert "{{add_a}}" in col.read_text()
    total, failures = run_collection(col, tmp_path / "r.json", environment=env)
    assert failures == 0
    if total is not None:
        assert total > 100
