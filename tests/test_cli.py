import json
import sys

from mcp2rest2postman.cli import main

from conftest import DEMO_SERVER
from helpers import by_name


def test_generate_from_url(tmp_path, json_url, capsys):
    out = tmp_path / "c.json"
    snap = tmp_path / "s.json"
    env = tmp_path / "e.json"
    code = main(
        [
            "generate", "--url", json_url, "-o", str(out), "--snapshot-out", str(snap),
            "--environment-out", str(env), "-H", "X-Api-Key: k",
        ]
    )
    assert code == 0
    col = json.loads(out.read_text())
    assert {v["key"]: v["value"] for v in col["variable"]}["baseUrl"] == json_url
    assert "add - valid, all attributes" in by_name(col)
    assert json.loads(snap.read_text())["format"] == "mcp2rest2postman/snapshot@1"
    assert json.loads(env.read_text())["values"][1]["key"] == "header_x_api_key"
    assert "requests" in capsys.readouterr().err


def test_generate_from_stdio_and_snapshot(tmp_path):
    snap = tmp_path / "s.json"
    cmd = f'"{sys.executable}" "{DEMO_SERVER}" --stdio'
    assert main(["generate", "-q", "--stdio", cmd, "-o", str(tmp_path / "a.json"), "--snapshot-out", str(snap)]) == 0
    a = json.loads((tmp_path / "a.json").read_text())
    assert {v["key"]: v["value"] for v in a["variable"]}["baseUrl"] == "http://localhost:3000/mcp"
    out = tmp_path / "b.json"
    assert main(["generate", "-q", "--from-snapshot", str(snap), "--base-url", "http://x/mcp", "-o", str(out)]) == 0
    b = json.loads(out.read_text())
    assert a["item"] == b["item"]


def test_generate_to_stdout(json_url, capsys):
    assert main(["generate", "-q", "--url", json_url, "-o", "-", "--no-negative-tests"]) == 0
    col = json.loads(capsys.readouterr().out)
    assert not any("->" in n and "add" in n for n in by_name(col))


def test_values_file(tmp_path, json_url):
    values = tmp_path / "v.json"
    values.write_text(json.dumps({"add": {"a": 40, "b": 2}}))
    out = tmp_path / "c.json"
    assert main(["generate", "-q", "--url", json_url, "--values", str(values), "-o", str(out)]) == 0
    raw = by_name(json.loads(out.read_text()))["add - valid, all attributes"]["request"]["body"]["raw"]
    assert json.loads(raw)["params"]["arguments"] == {"a": 40, "b": 2}


def test_inspect(json_url, capsys):
    assert main(["inspect", "--url", json_url]) == 0
    out = capsys.readouterr().out
    assert "greet(name*, language, excited)" in out
    assert main(["inspect", "--url", json_url, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tools"]


def test_errors_have_exit_codes(tmp_path, capsys):
    assert main(["inspect", "--url", "http://127.0.0.1:9/mcp", "--timeout", "2"]) == 2
    assert main(["generate", "--from-snapshot", str(tmp_path / "missing.json")]) == 1
    assert "error:" in capsys.readouterr().err
