#!/usr/bin/env python3
"""A tiny, dependency-free MCP server used for demos and tests.

Usage::

    python examples/demo_server.py --stdio
    python examples/demo_server.py --http --port 8765          # JSON responses
    python examples/demo_server.py --http --port 8765 --sse    # SSE responses

It exposes three tools (``add``, ``greet``, ``search_items``), one resource,
one resource template and one prompt, and validates tool arguments against
their input schemas so that negative tests have something to assert on.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "demo-mcp-server", "title": "Demo MCP Server", "version": "1.0.0"}

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "add",
        "title": "Add two numbers",
        "description": "Returns the sum of a and b.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "number", "description": "First addend"},
                "b": {"type": "number", "description": "Second addend"},
            },
            "required": ["a", "b"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"sum": {"type": "number"}},
            "required": ["sum"],
        },
    },
    {
        "name": "greet",
        "description": "Greets someone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "language": {"type": "string", "enum": ["en", "es"], "default": "en"},
                "excited": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search_items",
        "description": "Searches the demo catalogue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "tags": {"type": "array", "items": {"type": "string"}},
                "filters": {
                    "type": "object",
                    "properties": {"inStock": {"type": "boolean"}},
                },
            },
            "required": ["query"],
        },
    },
]

RESOURCES = [
    {"uri": "demo://readme", "name": "readme", "mimeType": "text/plain", "description": "Readme"}
]
RESOURCE_TEMPLATES = [
    {"uriTemplate": "demo://items/{id}", "name": "item", "mimeType": "application/json"}
]
PROMPTS = [
    {
        "name": "summarize",
        "description": "Summarise a text.",
        "arguments": [
            {"name": "text", "description": "Text to summarise", "required": True},
            {"name": "style", "description": "bullet or prose", "required": False},
        ],
    }
]

_TYPES = {
    "string": str,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    py = _TYPES.get(expected)
    return py is None or isinstance(value, py)


def validate(schema: Dict[str, Any], value: Any, path: str = "arguments") -> List[str]:
    """A deliberately small JSON-schema validator (enough for the demo tools)."""
    errors: List[str] = []
    expected = schema.get("type")
    if expected and not _type_ok(value, expected):
        return [f"{path}: expected {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        errors.append(f"{path}: too short")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}.{req}: is required")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                errors.extend(validate(sub, value[key], f"{path}.{key}"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(schema["items"], item, f"{path}[{i}]"))
    return errors


def _text(text: str, is_error: bool = False) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        raise JsonRpcError(-32602, f"Unknown tool: {name}")
    errors = validate(tool["inputSchema"], args)
    if errors:
        return _text("Invalid arguments: " + "; ".join(errors), is_error=True)
    if name == "add":
        total = args["a"] + args["b"]
        out = _text(str(total))
        out["structuredContent"] = {"sum": total}
        return out
    if name == "greet":
        hello = "Hola" if args.get("language") == "es" else "Hello"
        return _text(f"{hello}, {args['name']}{'!' if args.get('excited') else '.'}")
    items = [f"{args['query']}-{i}" for i in range(1, min(args.get("limit", 3), 5) + 1)]
    return _text(json.dumps(items))


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def handle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message; returns the response or ``None`` for notifications."""
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if msg_id is None:
        return None  # notification
    try:
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            raise JsonRpcError(-32600, "Invalid Request")
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            result: Dict[str, Any] = {
                "protocolVersion": version,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {},
                    "prompts": {},
                },
                "serverInfo": SERVER_INFO,
                "instructions": "Demo server for mcp2rest2postman.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            if not isinstance(params.get("name"), str):
                raise JsonRpcError(-32602, "Missing tool name")
            args = params.get("arguments") or {}
            if not isinstance(args, dict):
                raise JsonRpcError(-32602, "arguments must be an object")
            result = call_tool(params["name"], args)
        elif method == "resources/list":
            result = {"resources": RESOURCES}
        elif method == "resources/templates/list":
            result = {"resourceTemplates": RESOURCE_TEMPLATES}
        elif method == "resources/read":
            uri = params.get("uri")
            if uri == "demo://readme":
                text = "This is the demo MCP server."
                result = {"contents": [{"uri": uri, "mimeType": "text/plain", "text": text}]}
            elif isinstance(uri, str) and uri.startswith("demo://items/"):
                item = {"id": uri.rsplit("/", 1)[-1]}
                result = {
                    "contents": [
                        {"uri": uri, "mimeType": "application/json", "text": json.dumps(item)}
                    ]
                }
            else:
                raise JsonRpcError(-32002, f"Resource not found: {uri}")
        elif method == "prompts/list":
            result = {"prompts": PROMPTS}
        elif method == "prompts/get":
            if params.get("name") != "summarize":
                raise JsonRpcError(-32602, f"Unknown prompt: {params.get('name')}")
            args = params.get("arguments") or {}
            if "text" not in args:
                raise JsonRpcError(-32602, "Missing required argument: text")
            result = {
                "description": "Summarise a text",
                "messages": [
                    {
                        "role": "user",
                        "content": {"type": "text", "text": f"Summarise: {args['text']}"},
                    }
                ],
            }
        else:
            raise JsonRpcError(-32601, f"Method not found: {method}")
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    except JsonRpcError as err:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": err.code, "message": err.message}}


# --------------------------------------------------------------------------- stdio
def serve_stdio() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response: Optional[Dict[str, Any]] = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        else:
            response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


# --------------------------------------------------------------------------- HTTP
def make_handler(use_sse: bool, sessions: Dict[str, bool], lock: threading.Lock):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # silence
            pass

        def _send(self, status: int, body: bytes = b"", headers: Optional[Dict[str, str]] = None):
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - no server-initiated stream
            self._send(405, headers={"Allow": "POST, DELETE"})

        def do_DELETE(self) -> None:  # noqa: N802
            sid = self.headers.get("Mcp-Session-Id")
            with lock:
                existed = sessions.pop(sid or "", None)
            self._send(200 if existed else 404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/mcp":
                self._send(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                body = json.dumps(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
                ).encode()
                self._send(400, body, {"Content-Type": "application/json"})
                return
            extra: Dict[str, str] = {}
            if isinstance(message, dict) and message.get("method") == "initialize":
                sid = uuid.uuid4().hex
                with lock:
                    sessions[sid] = True
                extra["Mcp-Session-Id"] = sid
            else:
                sid = self.headers.get("Mcp-Session-Id")
                with lock:
                    known = bool(sid) and sid in sessions
                if not known:
                    body = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": message.get("id") if isinstance(message, dict) else None,
                            "error": {"code": -32000, "message": "Bad Request: missing or unknown session"},
                        }
                    ).encode()
                    self._send(400 if not sid else 404, body, {"Content-Type": "application/json"})
                    return
            response = handle(message) if isinstance(message, dict) else None
            if response is None:
                self._send(202, headers=extra)
                return
            payload = json.dumps(response)
            if use_sse:
                body = f"event: message\ndata: {payload}\n\n".encode()
                extra["Content-Type"] = "text/event-stream"
            else:
                body = payload.encode()
                extra["Content-Type"] = "application/json"
            self._send(200, body, extra)

    return Handler


def make_http_server(host: str = "127.0.0.1", port: int = 0, use_sse: bool = False) -> ThreadingHTTPServer:
    sessions: Dict[str, bool] = {}
    return ThreadingHTTPServer((host, port), make_handler(use_sse, sessions, threading.Lock()))


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stdio", action="store_true")
    mode.add_argument("--http", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--sse", action="store_true", help="answer POSTs with text/event-stream")
    args = parser.parse_args(argv)
    if args.stdio:
        serve_stdio()
        return
    server = make_http_server(args.host, args.port, args.sse)
    host, port = server.server_address[:2]
    print(f"Demo MCP server listening on http://{host}:{port}/mcp", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
