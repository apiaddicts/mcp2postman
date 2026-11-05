import pytest

from mcp2rest2postman.client import HttpTransport, MCPClient, MCPError, StdioTransport, parse_sse
from mcp2rest2postman.discovery import ServerSnapshot, discover


def test_parse_sse_multiple_events_and_comments():
    body = ': keep-alive\n\nevent: message\ndata: {"a": 1}\n\ndata: {"b":\ndata: 2}\n\ndata: not json\n\n'
    assert parse_sse(body) == [{"a": 1}, {"b": 2}]


def test_http_discovery(http_url):
    with HttpTransport(http_url) as transport:
        snap = discover(transport)
        assert transport.session_id
        assert transport.protocol_version == "2025-06-18"
    assert snap.server_info["name"] == "demo-mcp-server"
    assert {t["name"] for t in snap.tools} == {"add", "greet", "search_items"}
    assert snap.resources[0]["uri"] == "demo://readme"
    assert snap.resource_templates[0]["uriTemplate"] == "demo://items/{id}"
    assert snap.prompts[0]["name"] == "summarize"
    assert snap.source == {}


def test_http_tool_call_and_errors(json_url):
    client = MCPClient(HttpTransport(json_url))
    client.initialize()
    result = client.call_tool("add", {"a": 2, "b": 3})
    assert result["structuredContent"] == {"sum": 5}
    assert client.call_tool("add", {"a": 2})["isError"] is True
    with pytest.raises(MCPError) as err:
        client.call_tool("nope", {})
    assert err.value.code == -32602


def test_http_unreachable():
    with pytest.raises(MCPError):
        HttpTransport("http://127.0.0.1:9/mcp", timeout=2).request("ping")


def test_stdio_discovery(stdio_command):
    with StdioTransport(stdio_command, timeout=10) as transport:
        snap = discover(transport, source={"stdio": "demo"})
    assert len(snap.tools) == 3
    assert snap.source == {"stdio": "demo"}


def test_stdio_bad_command():
    with pytest.raises(MCPError):
        StdioTransport(["/definitely/not/a/command"])


def test_snapshot_roundtrip(tmp_path, json_url):
    with HttpTransport(json_url) as transport:
        snap = discover(transport)
    path = tmp_path / "snap.json"
    snap.save(path)
    again = ServerSnapshot.load(path)
    assert again == snap


def test_snapshot_rejects_unknown_format():
    with pytest.raises(ValueError):
        ServerSnapshot.from_dict({"format": "other", "protocol_version": "x"})
