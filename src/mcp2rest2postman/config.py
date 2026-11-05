"""Load an MCP server definition from an ``mcp-config.json`` file.

The common client formats are understood:

* ``{"mcpServers": {"name": {...}}}`` - Claude Desktop, Claude Code ``.mcp.json``,
  Cursor, Windsurf and most other clients.
* ``{"servers": {"name": {...}}, "inputs": [...]}`` - VS Code ``mcp.json``.
* A single server object (``{"url": ...}`` or ``{"command": ...}``).

A server entry is either remote::

    {"type": "http", "url": "https://host/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}}

or local (stdio)::

    {"command": "npx", "args": ["-y", "pkg"], "env": {"API_KEY": "${API_KEY}"}, "cwd": "."}

String values may reference secrets instead of embedding them:

* ``${VAR}`` / ``${env:VAR}`` - environment variable (error when unset).
* ``${VAR:-default}`` - environment variable with a default.
* ``${input:id}`` - VS Code input; prompted for on the terminal (hidden when
  the input is declared with ``"password": true``).
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

HTTP_TYPES = {"http", "streamable-http", "streamablehttp", "streamable_http"}
STDIO_TYPES = {"stdio", "local"}
SSE_TYPES = {"sse"}

_VAR = re.compile(r"\$\{([^}]+)\}")


class ConfigError(ValueError):
    """Invalid or unsupported MCP configuration."""


@dataclass
class ServerConfig:
    """One resolved server entry (variables already expanded)."""

    name: str
    url: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None

    @property
    def is_http(self) -> bool:
        return self.url is not None

    @property
    def argv(self) -> List[str]:
        return [self.command or ""] + self.args


Prompt = Callable[[str, Mapping[str, Any]], str]


def _tty_prompt(input_id: str, spec: Mapping[str, Any]) -> str:
    if not sys.stdin.isatty():
        raise ConfigError(
            f"config needs input '{input_id}' but stdin is not a terminal; "
            f"replace ${{input:{input_id}}} with ${{ENV_VAR}} or pass the value with -H/--env"
        )
    label = spec.get("description") or input_id
    if spec.get("password"):
        return getpass.getpass(f"{label}: ")
    sys.stderr.write(f"{label}: ")
    sys.stderr.flush()
    return sys.stdin.readline().rstrip("\n")


class _Expander:
    def __init__(
        self,
        environ: Mapping[str, str],
        inputs: Mapping[str, Mapping[str, Any]],
        prompt: Prompt,
        base_dir: Path,
    ) -> None:
        self.environ = environ
        self.inputs = inputs
        self.prompt = prompt
        self.base_dir = base_dir
        self._answers: Dict[str, str] = {}

    def _one(self, match: "re.Match[str]") -> str:
        expr = match.group(1).strip()
        if expr.startswith("input:"):
            input_id = expr[len("input:"):]
            if input_id not in self._answers:
                spec = self.inputs.get(input_id, {})
                answer = self.prompt(input_id, spec)
                if answer == "" and "default" in spec:
                    answer = str(spec["default"])
                self._answers[input_id] = answer
            return self._answers[input_id]
        if expr in ("workspaceFolder", "workspaceRoot"):
            return str(self.base_dir)
        if expr in ("userHome",):
            return str(Path.home())
        if expr.startswith("env:"):
            expr = expr[len("env:"):]
        name, sep, default = expr.partition(":-")
        if name in self.environ:
            return self.environ[name]
        if sep:
            return default
        raise ConfigError(f"environment variable '{name}' referenced by the MCP config is not set")

    def __call__(self, value: Any) -> Any:
        if isinstance(value, str):
            return _VAR.sub(self._one, value)
        if isinstance(value, list):
            return [self(v) for v in value]
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items()}
        return value


def _servers(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ConfigError("MCP config must be a JSON object")
    for key in ("mcpServers", "servers"):
        if key in data:
            servers = data[key]
            if not isinstance(servers, dict) or not servers:
                raise ConfigError(f"'{key}' must be a non-empty object of servers")
            return servers
    if "url" in data or "command" in data:
        return {"server": data}
    raise ConfigError("MCP config has no 'mcpServers' or 'servers' entry")


def server_names(path: str) -> List[str]:
    return list(_servers(_read(path)))


def _read(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except json.JSONDecodeError as err:
        raise ConfigError(f"{path} is not valid JSON: {err}") from err


def load_server(
    path: str,
    name: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    prompt: Prompt = _tty_prompt,
) -> ServerConfig:
    """Return the server ``name`` (or the only server) from the config at ``path``."""
    data = _read(path)
    servers = _servers(data)
    if name is None:
        if len(servers) != 1:
            raise ConfigError(
                f"{path} defines {len(servers)} servers; choose one with --server "
                f"({', '.join(servers)})"
            )
        name = next(iter(servers))
    if name not in servers:
        raise ConfigError(f"server '{name}' not found in {path} (available: {', '.join(servers)})")
    raw = servers[name]
    if not isinstance(raw, dict):
        raise ConfigError(f"server '{name}' must be an object")
    if raw.get("disabled"):
        print(f"warning: server '{name}' is marked as disabled in {path}", file=sys.stderr)

    inputs = {
        i["id"]: i for i in (data.get("inputs") or []) if isinstance(i, dict) and "id" in i
    }
    base_dir = Path(path).resolve().parent
    expand = _Expander(os.environ if environ is None else environ, inputs, prompt, base_dir)

    kind = str(raw.get("type") or raw.get("transport") or "").lower()
    url = raw.get("url") or raw.get("serverUrl")
    if kind in SSE_TYPES:
        raise ConfigError(
            f"server '{name}' uses the legacy SSE transport, which is not supported; "
            "use its Streamable HTTP endpoint"
        )
    if url and kind not in STDIO_TYPES:
        if kind and kind not in HTTP_TYPES:
            raise ConfigError(f"server '{name}' has unsupported type '{kind}'")
        headers = expand(raw.get("headers") or {})
        if not isinstance(headers, dict):
            raise ConfigError(f"'headers' of server '{name}' must be an object")
        return ServerConfig(name=name, url=expand(url), headers={k: str(v) for k, v in headers.items()})
    if raw.get("command"):
        args = expand(raw.get("args") or [])
        env = expand(raw.get("env") or {})
        if not isinstance(args, list) or not isinstance(env, dict):
            raise ConfigError(f"server '{name}': 'args' must be a list and 'env' an object")
        cwd = raw.get("cwd")
        if cwd:
            cwd = str((base_dir / expand(cwd)).resolve())
        return ServerConfig(
            name=name,
            command=expand(raw["command"]),
            args=[str(a) for a in args],
            env={k: str(v) for k, v in env.items()},
            cwd=cwd,
        )
    raise ConfigError(f"server '{name}' needs a 'url' (HTTP) or a 'command' (stdio)")
