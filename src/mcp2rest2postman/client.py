"""Minimal, dependency-free MCP client used to discover a server's surface.

Two transports are supported:

* **Streamable HTTP** (MCP 2025-03-26 and later): JSON-RPC messages are POSTed
  to a single endpoint and answered either with ``application/json`` or with a
  ``text/event-stream`` carrying the response.
* **stdio**: the server is launched as a subprocess and newline-delimited
  JSON-RPC messages are exchanged over stdin/stdout.

Only the client-side features needed for discovery are implemented
(``initialize``, list operations and, optionally, ``tools/call``).
"""

from __future__ import annotations

import itertools
import json
import queue
import shlex
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from . import __version__

#: MCP protocol revisions this tool knows, newest first (SA-3290).
SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18")
#: Proposed during ``initialize``. Per the spec, a server that does not support
#: it answers with another version it supports, so proposing the newest is safe.
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
CLIENT_INFO = {"name": "mcp2rest2postman", "version": __version__}
USER_AGENT = f"mcp2rest2postman/{__version__}"


class MCPError(Exception):
    """A JSON-RPC error returned by the server or a transport failure."""

    def __init__(self, message: str, code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        return f"[{self.code}] {base}" if self.code is not None else base


class ConnectionFailed(MCPError):
    """The server could not be reached or the process died (retrying will not help)."""


@dataclass
class HttpExchange:
    """Raw information about one HTTP exchange (useful for diagnostics)."""

    status: int
    headers: Dict[str, str]
    body: str


def parse_sse(body: str) -> List[Dict[str, Any]]:
    """Parse a ``text/event-stream`` payload and return decoded JSON ``data`` events."""
    events: List[Dict[str, Any]] = []
    data_lines: List[str] = []

    def flush() -> None:
        if data_lines:
            raw = "\n".join(data_lines)
            data_lines.clear()
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                pass

    for line in body.splitlines():
        if line == "":
            flush()
        elif line.startswith(":"):
            continue
        elif line.startswith("data:"):
            data_lines.append(line[6:] if line[5:6] == " " else line[5:])
    flush()
    return events


def _find_response(messages: Iterable[Any], request_id: Any) -> Optional[Dict[str, Any]]:
    for msg in messages:
        batch = msg if isinstance(msg, list) else [msg]
        for item in batch:
            if isinstance(item, dict) and item.get("id") == request_id and (
                "result" in item or "error" in item
            ):
                return item
    return None


class Transport:
    """Base transport. Subclasses implement :meth:`request` and :meth:`notify`."""

    def request(self, method: str, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def notify(self, method: str, params: Optional[Mapping[str, Any]] = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "Transport":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class HttpTransport(Transport):
    """Streamable HTTP transport."""

    def __init__(
        self,
        url: str,
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url
        self.extra_headers = dict(headers or {})
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self.protocol_version: Optional[str] = None
        self._ids = itertools.count(1)
        self.last_exchange: Optional[HttpExchange] = None

    # -- helpers ---------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            # urllib's default "Python-urllib/x.y" is blocked by many WAFs
            # (e.g. Cloudflare error 1010); identify ourselves instead.
            "User-Agent": USER_AGENT,
        }
        headers.update(self.extra_headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, payload: Mapping[str, Any]) -> HttpExchange:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                exchange = HttpExchange(
                    status=resp.status,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=resp.read().decode("utf-8", errors="replace"),
                )
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace") if err.fp else ""
            exchange = HttpExchange(
                status=err.code,
                headers={k.lower(): v for k, v in (err.headers or {}).items()},
                body=body,
            )
        except urllib.error.URLError as err:
            raise ConnectionFailed(f"Cannot reach MCP server at {self.url}: {err.reason}") from err
        self.last_exchange = exchange
        session = exchange.headers.get("mcp-session-id")
        if session:
            self.session_id = session
        return exchange

    @staticmethod
    def _decode(exchange: HttpExchange) -> List[Any]:
        ctype = exchange.headers.get("content-type", "")
        if not exchange.body.strip():
            return []
        if "text/event-stream" in ctype:
            return parse_sse(exchange.body)
        try:
            return [json.loads(exchange.body)]
        except json.JSONDecodeError:
            # Some servers omit the content type: try SSE as a fallback.
            return parse_sse(exchange.body)

    # -- API -------------------------------------------------------------
    def request(self, method: str, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        request_id = next(self._ids)
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = dict(params)
        exchange = self._post(payload)
        messages = self._decode(exchange)
        response = _find_response(messages, request_id)
        if response is None:
            raise MCPError(
                f"No JSON-RPC response for '{method}' (HTTP {exchange.status}): "
                f"{exchange.body[:300]!r}"
            )
        if "error" in response:
            err = response["error"] or {}
            raise MCPError(err.get("message", "Unknown error"), err.get("code"), err.get("data"))
        return response.get("result") or {}

    def notify(self, method: str, params: Optional[Mapping[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = dict(params)
        exchange = self._post(payload)
        if exchange.status >= 400:
            raise MCPError(f"Notification '{method}' rejected with HTTP {exchange.status}")


class StdioTransport(Transport):
    """stdio transport: launches the server as a subprocess."""

    def __init__(
        self,
        command: Union[str, Sequence[str]],
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv:
            raise MCPError("Empty stdio command")
        self.timeout = timeout
        self._ids = itertools.count(1)
        self._queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
        full_env = None
        if env:
            import os

            full_env = dict(os.environ)
            full_env.update(env)
        try:
            self.proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=full_env,
                cwd=cwd,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as err:
            raise ConnectionFailed(f"Cannot start stdio server {argv!r}: {err}") from err
        self._stderr: List[str] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._queue.put(json.loads(line))
            except json.JSONDecodeError:
                continue
        self._queue.put(None)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr.append(line)
            if len(self._stderr) > 200:
                del self._stderr[:100]

    def _send(self, payload: Mapping[str, Any]) -> None:
        if self.proc.stdin is None or self.proc.poll() is not None:
            raise ConnectionFailed("stdio server is not running. stderr: " + "".join(self._stderr[-20:]))
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        request_id = next(self._ids)
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self._send(payload)
        while True:
            try:
                msg = self._queue.get(timeout=self.timeout)
            except queue.Empty as err:
                raise MCPError(f"Timeout waiting for response to '{method}'") from err
            if msg is None:
                raise ConnectionFailed(
                    "stdio server closed the connection. stderr: " + "".join(self._stderr[-20:])
                )
            # Answer server->client pings so the server does not stall.
            if msg.get("method") == "ping" and "id" in msg:
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
                continue
            if msg.get("id") == request_id and ("result" in msg or "error" in msg):
                if "error" in msg:
                    err = msg["error"] or {}
                    raise MCPError(err.get("message", "Unknown error"), err.get("code"), err.get("data"))
                return msg.get("result") or {}

    def notify(self, method: str, params: Optional[Mapping[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self._send(payload)

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.wait(timeout=3)
            except (subprocess.TimeoutExpired, OSError):
                self.proc.kill()


@dataclass
class MCPClient:
    """High-level helper on top of a :class:`Transport`."""

    transport: Transport
    protocol_version: str = DEFAULT_PROTOCOL_VERSION
    initialize_result: Dict[str, Any] = field(default_factory=dict)

    def initialize(self) -> Dict[str, Any]:
        result = self.transport.request(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            },
        )
        self.initialize_result = result
        negotiated = result.get("protocolVersion") or self.protocol_version
        if isinstance(self.transport, HttpTransport):
            self.transport.protocol_version = negotiated
        self.transport.notify("notifications/initialized")
        return result

    @property
    def capabilities(self) -> Dict[str, Any]:
        return self.initialize_result.get("capabilities") or {}

    def _paginate(self, method: str, key: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        seen = set()
        while True:
            params = {"cursor": cursor} if cursor else None
            result = self.transport.request(method, params)
            items.extend(result.get(key) or [])
            cursor = result.get("nextCursor")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        return items

    def list_tools(self) -> List[Dict[str, Any]]:
        return self._paginate("tools/list", "tools")

    def list_resources(self) -> List[Dict[str, Any]]:
        return self._paginate("resources/list", "resources")

    def list_resource_templates(self) -> List[Dict[str, Any]]:
        return self._paginate("resources/templates/list", "resourceTemplates")

    def list_prompts(self) -> List[Dict[str, Any]]:
        return self._paginate("prompts/list", "prompts")

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        return self.transport.request("tools/call", {"name": name, "arguments": dict(arguments)})

    def close(self) -> None:
        self.transport.close()
