import json
import sys

import pytest

from mcp2postman import cli
from mcp2postman.cli import main
from mcp2postman.config import ConfigError, load_server

from conftest import DEMO_SERVER


def write(tmp_path, data, name="mcp-config.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


def test_mcp_servers_http_with_env_secret(tmp_path):
    path = write(tmp_path, {"mcpServers": {"api": {
        "type": "http", "url": "https://h/${SEG}/mcp",
        "headers": {"Authorization": "Bearer ${TOKEN}", "X-Region": "${REGION:-eu}"},
    }}})
    server = load_server(path, environ={"TOKEN": "t0k", "SEG": "v1"})
    assert server.url == "https://h/v1/mcp"
    assert server.headers == {"Authorization": "Bearer t0k", "X-Region": "eu"}


def test_vscode_servers_with_inputs_and_stdio(tmp_path):
    path = write(tmp_path, {
        "inputs": [{"id": "key", "type": "promptString", "password": True}],
        "servers": {
            "local": {"type": "stdio", "command": "node", "args": ["s.js", "${env:MODE}"],
                      "env": {"API_KEY": "${input:key}"}, "cwd": "sub"},
            "other": {"url": "https://x/mcp"},
        },
    })
    asked = []
    server = load_server(path, "local", environ={"MODE": "dev"},
                         prompt=lambda i, spec: asked.append((i, spec["password"])) or "secret")
    assert server.argv == ["node", "s.js", "dev"]
    assert server.env == {"API_KEY": "secret"}
    assert server.cwd == str((tmp_path / "sub").resolve())
    assert asked == [("key", True)]


def test_config_errors(tmp_path):
    two = write(tmp_path, {"mcpServers": {"a": {"url": "u"}, "b": {"url": "u"}}})
    with pytest.raises(ConfigError, match="choose one with --server"):
        load_server(two)
    with pytest.raises(ConfigError, match="not found"):
        load_server(two, "zz")
    with pytest.raises(ConfigError, match="'TOKEN'"):
        load_server(write(tmp_path, {"url": "u", "headers": {"A": "${TOKEN}"}}, "s.json"), environ={})
    with pytest.raises(ConfigError, match="SSE"):
        load_server(write(tmp_path, {"mcpServers": {"a": {"type": "sse", "url": "u"}}}, "t.json"))
    with pytest.raises(ConfigError, match="'url'"):
        load_server(write(tmp_path, {"mcpServers": {"a": {"type": "http"}}}, "w.json"))


def test_cli_config_http_headers_and_override(tmp_path, json_url, monkeypatch):
    monkeypatch.setenv("TOKEN", "abc")
    path = write(tmp_path, {"mcpServers": {"demo": {"url": json_url, "headers": {
        "Authorization": "Bearer ${TOKEN}", "X-Tenant": "t1"}}}})
    sent = {}
    real = cli.HttpTransport

    def spy(url, headers=None, timeout=30.0):
        sent.update(headers or {})
        return real(url, headers=headers, timeout=timeout)

    monkeypatch.setattr(cli, "HttpTransport", spy)
    out, env, snap = tmp_path / "c.json", tmp_path / "e.json", tmp_path / "s.json"
    code = main(["generate", "-q", "--config", path, "-H", "x-tenant: t2", "-o", str(out),
                 "--environment-out", str(env), "--snapshot-out", str(snap)])
    assert code == 0
    assert sent == {"Authorization": "Bearer abc", "x-tenant": "t2"}
    variables = {v["key"]: v["value"] for v in json.loads(out.read_text())["variable"]}
    assert variables["baseUrl"] == json_url
    assert variables["header_authorization"] == ""  # secrets are not embedded by default
    assert "abc" not in snap.read_text()
    assert json.loads(snap.read_text())["source"]["config"]["server"] == "demo"


def test_cli_config_stdio(tmp_path, capsys):
    path = write(tmp_path, {"mcpServers": {"demo": {
        "command": sys.executable, "args": [str(DEMO_SERVER), "--stdio"], "env": {"X": "1"}}}})
    assert main(["inspect", "--config", path]) == 0
    assert "greet(name*, language, excited)" in capsys.readouterr().out


def test_cli_server_requires_config(capsys):
    assert main(["inspect", "--url", "http://x/mcp", "--server", "a"]) == 1
    assert "--server requires --config" in capsys.readouterr().err
