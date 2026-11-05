"""Discovery of an MCP server's surface (tools, resources, prompts).

The result of a discovery is a :class:`ServerSnapshot`, a plain data object
that can be serialised to JSON. Snapshots decouple *talking to a server* from
*generating a collection*: a snapshot can be stored, reviewed in Git and used
later to regenerate the collection offline.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .client import MCPClient, MCPError, Transport

SNAPSHOT_FORMAT = "mcp2rest2postman/snapshot@1"


@dataclass
class ServerSnapshot:
    """Everything the generator needs to know about an MCP server."""

    protocol_version: str
    server_info: Dict[str, Any] = field(default_factory=dict)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    instructions: Optional[str] = None
    tools: List[Dict[str, Any]] = field(default_factory=list)
    resources: List[Dict[str, Any]] = field(default_factory=list)
    resource_templates: List[Dict[str, Any]] = field(default_factory=list)
    prompts: List[Dict[str, Any]] = field(default_factory=list)
    source: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["format"] = SNAPSHOT_FORMAT
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServerSnapshot":
        fmt = data.get("format", SNAPSHOT_FORMAT)
        if fmt != SNAPSHOT_FORMAT:
            raise ValueError(f"Unsupported snapshot format: {fmt}")
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Union[str, Path]) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", "utf-8")

    @classmethod
    def load(cls, path: Union[str, Path]) -> "ServerSnapshot":
        return cls.from_dict(json.loads(Path(path).read_text("utf-8")))

    # -- convenience -----------------------------------------------------
    @property
    def server_name(self) -> str:
        return str(self.server_info.get("title") or self.server_info.get("name") or "MCP server")


def discover(
    transport: Transport,
    protocol_version: Optional[str] = None,
    source: Optional[Dict[str, Any]] = None,
    log: Callable[[str], None] = lambda _msg: None,
) -> ServerSnapshot:
    """Connect through *transport*, run the MCP handshake and list everything.

    List operations are only attempted for capabilities the server announces.
    A failing list operation does not abort discovery: it is recorded as a
    warning in the snapshot.
    """
    client = MCPClient(transport) if protocol_version is None else MCPClient(transport, protocol_version)
    init = client.initialize()
    caps = client.capabilities
    snapshot = ServerSnapshot(
        protocol_version=init.get("protocolVersion") or client.protocol_version,
        server_info=init.get("serverInfo") or {},
        capabilities=caps,
        instructions=init.get("instructions"),
        source=source or {},
    )
    log(
        f"Connected to {snapshot.server_name} "
        f"(protocol {snapshot.protocol_version}, capabilities: {', '.join(sorted(caps)) or 'none'})"
    )

    def attempt(label: str, fn: Callable[[], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        try:
            items = fn()
            log(f"  {label}: {len(items)}")
            return items
        except MCPError as err:
            msg = f"{label} failed: {err}"
            snapshot.warnings.append(msg)
            log("  " + msg)
            return []

    if "tools" in caps:
        snapshot.tools = attempt("tools", client.list_tools)
    if "resources" in caps:
        snapshot.resources = attempt("resources", client.list_resources)
        snapshot.resource_templates = attempt("resource templates", client.list_resource_templates)
    if "prompts" in caps:
        snapshot.prompts = attempt("prompts", client.list_prompts)
    return snapshot
