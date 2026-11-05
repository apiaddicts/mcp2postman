"""Command line interface: ``mcp2rest2postman``."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__
from .client import DEFAULT_PROTOCOL_VERSION, HttpTransport, MCPError, StdioTransport, Transport
from .config import load_server
from .discovery import ServerSnapshot, discover
from .postman import GeneratorOptions, build_collection, build_environment
from .schema_values import required_and_optional

DEFAULT_BASE_URL = "http://localhost:3000/mcp"


def _parse_header(raw: str) -> Tuple[str, str]:
    if ":" not in raw:
        raise argparse.ArgumentTypeError(f"header must look like 'Name: value', got {raw!r}")
    name, value = raw.split(":", 1)
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError(f"empty header name in {raw!r}")
    return name, value.strip()


def _parse_env(raw: str) -> Tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"env must look like KEY=VALUE, got {raw!r}")
    key, value = raw.split("=", 1)
    return key, value


def _add_source_args(parser: argparse.ArgumentParser, allow_snapshot: bool) -> None:
    src = parser.add_argument_group("MCP server (choose one)")
    group = src.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="Streamable HTTP endpoint of the MCP server, e.g. http://localhost:3000/mcp")
    group.add_argument(
        "--stdio",
        metavar="COMMAND",
        help="command that starts a stdio MCP server, e.g. \"npx -y @modelcontextprotocol/server-everything\"",
    )
    group.add_argument(
        "--config",
        metavar="FILE",
        help="mcp-config.json (Claude/Cursor 'mcpServers' or VS Code 'servers' format); "
        "url/command, args, env and headers (auth) are taken from it",
    )
    if allow_snapshot:
        group.add_argument("--from-snapshot", metavar="FILE", help="reuse a snapshot saved with --snapshot-out")
    src.add_argument(
        "--server",
        metavar="NAME",
        help="server to use from --config (required when the file defines several)",
    )
    src.add_argument(
        "-H",
        "--header",
        dest="headers",
        action="append",
        type=_parse_header,
        default=[],
        metavar="'NAME: VALUE'",
        help="extra HTTP header sent to the server (repeatable), e.g. -H 'Authorization: Bearer xxx'; "
        "overrides a header of the same name from --config",
    )
    src.add_argument(
        "--env",
        dest="env",
        action="append",
        type=_parse_env,
        default=[],
        metavar="KEY=VALUE",
        help="environment variable for the stdio server (repeatable); overrides --config env",
    )
    src.add_argument(
        "--protocol-version",
        default=DEFAULT_PROTOCOL_VERSION,
        help=f"protocol version proposed during initialize (default: {DEFAULT_PROTOCOL_VERSION})",
    )
    src.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for each response (default: 30)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp2rest2postman",
        description="Turn an MCP server into a Postman collection with contract and negative tests.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    gen = sub.add_parser(
        "generate",
        help="discover an MCP server and write a Postman collection",
        description="Discover an MCP server and write a Postman v2.1 collection.",
    )
    _add_source_args(gen, allow_snapshot=True)
    out = gen.add_argument_group("output")
    out.add_argument("-o", "--output", help="collection file ('-' for stdout). Default: <server>.postman_collection.json")
    out.add_argument("--environment-out", metavar="FILE", help="also write a Postman environment with baseUrl and headers")
    out.add_argument("--snapshot-out", metavar="FILE", help="also save the discovered server snapshot (JSON)")
    out.add_argument("--name", help="collection name (default: '<server title> (MCP)')")
    out.add_argument(
        "--base-url",
        help="value of {{baseUrl}} in the collection (default: --url, or "
        f"{DEFAULT_BASE_URL} for stdio servers)",
    )
    out.add_argument(
        "--embed-header-values",
        action="store_true",
        help="store -H values in the collection (by default only empty variables are created; "
        "beware of committing secrets)",
    )
    sel = gen.add_argument_group("content")
    sel.add_argument("--include-tool", action="append", default=[], metavar="GLOB", help="only these tools (repeatable)")
    sel.add_argument("--exclude-tool", action="append", default=[], metavar="GLOB", help="skip these tools (repeatable)")
    sel.add_argument(
        "--values",
        metavar="FILE",
        help='JSON file with argument values per tool: {"tool": {"arg": value}}',
    )
    sel.add_argument("--no-negative-tests", action="store_true", help="skip 'missing required' and 'wrong type' requests")
    sel.add_argument("--no-wrong-type-tests", action="store_true", help="skip 'wrong type' requests")
    sel.add_argument("--no-optional-variants", action="store_true", help="skip one request per optional attribute")
    sel.add_argument("--no-protocol-tests", action="store_true", help="skip generic JSON-RPC conformance requests")
    sel.add_argument("--no-resources", action="store_true", help="skip the resources folder")
    sel.add_argument("--no-prompts", action="store_true", help="skip the prompts folder")
    sel.add_argument("--no-teardown", action="store_true", help="skip the session DELETE request")
    gen.add_argument("-q", "--quiet", action="store_true", help="only print errors")
    gen.set_defaults(func=cmd_generate)

    insp = sub.add_parser(
        "inspect",
        help="print what an MCP server exposes",
        description="Connect to an MCP server and print its tools, resources and prompts.",
    )
    _add_source_args(insp, allow_snapshot=False)
    insp.add_argument("--json", action="store_true", help="print the full snapshot as JSON")
    insp.set_defaults(func=cmd_inspect)
    return parser


def _merge(base: Dict[str, str], override: Sequence[Tuple[str, str]], case_insensitive: bool) -> List[Tuple[str, str]]:
    merged = dict(base)
    for key, value in override:
        if case_insensitive:
            for existing in [k for k in merged if k.lower() == key.lower()]:
                del merged[existing]
        merged[key] = value
    return list(merged.items())


def _apply_config(args: argparse.Namespace) -> None:
    """Resolve --config/--server into --url/--stdio, headers, env and cwd."""
    args.cwd = None
    if args.server and not args.config:
        raise ValueError("--server requires --config")
    if not args.config:
        return
    server = load_server(args.config, args.server)
    args.config_server = server.name
    if server.is_http:
        args.url = server.url
        args.headers = _merge(server.headers, args.headers, case_insensitive=True)
    else:
        args.stdio = server.argv
        args.env = _merge(server.env, args.env, case_insensitive=False)
        args.cwd = server.cwd


def _transport(args: argparse.Namespace) -> Transport:
    headers = dict(args.headers)
    if args.url:
        return HttpTransport(args.url, headers=headers, timeout=args.timeout)
    if headers:
        print("warning: -H headers are not used to talk to a stdio server (only added to the collection)", file=sys.stderr)
    return StdioTransport(args.stdio, env=dict(args.env), cwd=args.cwd, timeout=args.timeout)


def _snapshot(args: argparse.Namespace, log) -> ServerSnapshot:
    if getattr(args, "from_snapshot", None):
        return ServerSnapshot.load(args.from_snapshot)
    _apply_config(args)
    stdio = shlex.join(args.stdio) if isinstance(args.stdio, list) else args.stdio
    source: Dict[str, Any] = {"url": args.url} if args.url else {"stdio": stdio}
    if args.config:
        # Only the location: env values and headers may hold secrets.
        source["config"] = {"file": args.config, "server": args.config_server}
    with _transport(args) as transport:
        return discover(transport, args.protocol_version, source, log=log)


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower() or "mcp-server"


def _load_values(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise ValueError('--values must contain an object like {"tool": {"arg": value}}')
    return data


def _write_json(path: str, data: Any) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if path == "-":
        sys.stdout.write(text)
    else:
        Path(path).write_text(text, "utf-8")


def cmd_generate(args: argparse.Namespace) -> int:
    log = (lambda _m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))
    snapshot = _snapshot(args, log)
    base_url = args.base_url or args.url or (snapshot.source or {}).get("url") or DEFAULT_BASE_URL
    if not args.base_url and not args.url and not (snapshot.source or {}).get("url"):
        log(
            f"note: stdio server -> collection targets {base_url}; expose the server over "
            "Streamable HTTP and pass --base-url (or edit {{baseUrl}})."
        )
    options = GeneratorOptions(
        base_url=base_url,
        collection_name=args.name,
        headers=dict(args.headers),
        embed_header_values=args.embed_header_values,
        include_tools=args.include_tool,
        exclude_tools=args.exclude_tool,
        tool_values=_load_values(args.values),
        negative_tests=not args.no_negative_tests,
        wrong_type_tests=not args.no_wrong_type_tests,
        optional_variants=not args.no_optional_variants,
        protocol_tests=not args.no_protocol_tests,
        include_resources=not args.no_resources,
        include_prompts=not args.no_prompts,
        teardown=not args.no_teardown,
    )
    collection = build_collection(snapshot, options)
    output = args.output or f"{_slug(snapshot.server_name)}.postman_collection.json"
    _write_json(output, collection)
    if args.snapshot_out:
        snapshot.save(args.snapshot_out)
    if args.environment_out:
        _write_json(args.environment_out, build_environment(options, f"{snapshot.server_name} (MCP)"))
    requests = _count_requests(collection["item"])
    if output != "-":
        log(f"Wrote {output} ({requests} requests)")
    for warning in snapshot.warnings:
        log(f"warning: {warning}")
    return 0


def _count_requests(items: List[Dict[str, Any]]) -> int:
    return sum(_count_requests(i["item"]) if "item" in i else 1 for i in items)


def cmd_inspect(args: argparse.Namespace) -> int:
    snapshot = _snapshot(args, lambda _m: None)
    if args.json:
        _write_json("-", snapshot.to_dict())
        return 0
    info = snapshot.server_info
    print(f"{snapshot.server_name} {info.get('version', '')}".rstrip())
    print(f"  protocol:     {snapshot.protocol_version}")
    print(f"  capabilities: {', '.join(sorted(snapshot.capabilities)) or 'none'}")
    print(f"\nTools ({len(snapshot.tools)}):")
    for tool in snapshot.tools:
        required, optional, _ = required_and_optional(tool.get("inputSchema") or {})
        parts = [f"{r}*" for r in required] + optional
        print(f"  - {tool.get('name')}({', '.join(parts)})")
    if snapshot.resources or snapshot.resource_templates:
        print(f"\nResources ({len(snapshot.resources)} + {len(snapshot.resource_templates)} templates):")
        for res in snapshot.resources:
            print(f"  - {res.get('uri')}")
        for tpl in snapshot.resource_templates:
            print(f"  - {tpl.get('uriTemplate')} (template)")
    if snapshot.prompts:
        print(f"\nPrompts ({len(snapshot.prompts)}):")
        for prompt in snapshot.prompts:
            args_ = [f"{a.get('name')}{'*' if a.get('required') else ''}" for a in prompt.get("arguments") or []]
            print(f"  - {prompt.get('name')}({', '.join(args_)})")
    print("\n(* = required)")
    for warning in snapshot.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except MCPError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
