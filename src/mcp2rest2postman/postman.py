"""Build a Postman collection (v2.1) from a :class:`ServerSnapshot`."""

from __future__ import annotations

import fnmatch
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import __version__, scripts
from . import mcp_schemas as ms
from .client import SUPPORTED_PROTOCOL_VERSIONS
from .discovery import ServerSnapshot
from .schema_values import (
    SchemaResolver,
    is_missing,
    required_and_optional,
    sample_object,
    sample_value,
    sanitize_for_ajv,
    wrong_type_value,
)

POSTMAN_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
_NAMESPACE = uuid.UUID("5b0b7e8e-6c7e-4c55-9a8c-6d63a2f1c0de")


@dataclass
class GeneratorOptions:
    """Knobs controlling what the generated collection contains."""

    base_url: str = "http://localhost:3000/mcp"
    collection_name: Optional[str] = None
    #: Header name -> value. Values are only embedded when ``embed_header_values``.
    headers: Dict[str, str] = field(default_factory=dict)
    embed_header_values: bool = False
    include_tools: List[str] = field(default_factory=list)  # glob patterns
    exclude_tools: List[str] = field(default_factory=list)  # glob patterns
    #: Per-tool argument overrides: {"tool": {"arg": value}}.
    tool_values: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    negative_tests: bool = True
    wrong_type_tests: bool = True
    optional_variants: bool = True
    protocol_tests: bool = True
    #: Versions checked in the "91 Protocol versions" folder (empty: none).
    protocol_versions: List[str] = field(default_factory=lambda: list(SUPPORTED_PROTOCOL_VERSIONS))
    include_resources: bool = True
    include_prompts: bool = True
    teardown: bool = True
    #: Move tool / prompt argument values out of the request bodies into
    #: ``{{tool_arg}}`` environment variables.
    body_variables: bool = False


class BodyVar:
    """Placeholder for an argument value that lives in an environment variable."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


_SENTINEL = "__mcp2rest2postman_var__"
_SENTINEL_RE = re.compile(r'"' + _SENTINEL + r'([A-Za-z0-9_]+)(:s)?"')


def variable_name(*parts: str) -> str:
    """``("create_user", "e-mail")`` -> ``create_user_e_mail``."""
    joined = "_".join(re.sub(r"[^A-Za-z0-9]+", "_", str(p)).strip("_") for p in parts if str(p))
    return joined or "value"


def environment_value(value: Any) -> str:
    """Text stored in the environment so that substitution yields valid JSON.

    Strings are used inside quotes in the body (``"{{var}}"``), so only their
    JSON escaping is stored; any other type is inserted unquoted
    (``{{var}}``) and stored as its JSON literal (``42``, ``true``, ``["a"]``).
    """
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)[1:-1]
    return json.dumps(value, ensure_ascii=False)


def header_variable(name: str) -> str:
    """Collection variable used for a custom header (``Authorization`` -> ``header_authorization``)."""
    return "header_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class CollectionBuilder:
    def __init__(self, snapshot: ServerSnapshot, options: Optional[GeneratorOptions] = None):
        self.snapshot = snapshot
        self.options = options or GeneratorOptions()
        self._next_id = 1
        #: Environment variables created for request bodies: name -> value.
        self.body_variables: Dict[str, Any] = {}

    # ------------------------------------------------------------------ utils
    def _id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _headers(self, with_session: bool = True) -> List[Dict[str, Any]]:
        headers: List[Dict[str, Any]] = [
            {"key": "Content-Type", "value": "application/json"},
            {"key": "Accept", "value": "application/json, text/event-stream"},
        ]
        if with_session:
            headers.append({"key": "Mcp-Session-Id", "value": "{{mcpSessionId}}"})
            headers.append({"key": "MCP-Protocol-Version", "value": "{{protocolVersion}}"})
        for name in self.options.headers:
            headers.append({"key": name, "value": "{{" + header_variable(name) + "}}"})
        return headers

    def _var(self, value: Any, *parts: str) -> BodyVar:
        """Register *value* under a variable named after *parts* and return a placeholder.

        The same name always maps to the same value; a different value for an
        existing name gets a numeric suffix.
        """
        base = variable_name(*parts)
        name, n = base, 2
        while name in self.body_variables and self.body_variables[name] != value:
            name, n = f"{base}_{n}", n + 1
        self.body_variables[name] = value
        return BodyVar(name)

    def _vars(self, arguments: Dict[str, Any], prefix: List[str], literal: Any = ()) -> Dict[str, Any]:
        """Replace argument values by variables (keys in *literal* keep their value)."""
        if not self.options.body_variables:
            return arguments
        return {k: (v if k in literal else self._var(v, *prefix, k)) for k, v in arguments.items()}

    def _raw_body(self, payload: Dict[str, Any]) -> str:
        values = self.body_variables

        def encode(obj: Any) -> Any:
            if isinstance(obj, BodyVar):
                suffix = ":s" if isinstance(values[obj.name], str) else ""
                return f"{_SENTINEL}{obj.name}{suffix}"
            if isinstance(obj, dict):
                return {k: encode(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [encode(v) for v in obj]
            return obj

        raw = json.dumps(encode(payload), indent=2, ensure_ascii=False)
        return _SENTINEL_RE.sub(
            lambda m: '"{{' + m.group(1) + '}}"' if m.group(2) else "{{" + m.group(1) + "}}", raw
        )

    def _url(self) -> Dict[str, Any]:
        return {"raw": "{{baseUrl}}", "host": ["{{baseUrl}}"]}

    def _request(
        self,
        name: str,
        method: str,
        params: Optional[Mapping[str, Any]],
        tests: List[str],
        description: str = "",
        notification: bool = False,
        with_session: bool = True,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"jsonrpc": "2.0"}
        if not notification:
            payload["id"] = self._id()
        payload["method"] = method
        if params is not None:
            payload["params"] = params
        item: Dict[str, Any] = {
            "name": name,
            "request": {
                "method": "POST",
                "header": self._headers(with_session),
                "url": self._url(),
                "body": {
                    "mode": "raw",
                    "raw": self._raw_body(payload),
                    "options": {"raw": {"language": "json"}},
                },
            },
            "event": [{"listen": "test", "script": {"type": "text/javascript", "exec": tests}}],
        }
        if description:
            item["request"]["description"] = description
        return item

    @staticmethod
    def _folder(name: str, items: List[Dict[str, Any]], description: str = "") -> Dict[str, Any]:
        folder: Dict[str, Any] = {"name": name, "item": items}
        if description:
            folder["description"] = description
        return folder

    def _tool_selected(self, name: str) -> bool:
        inc = self.options.include_tools
        exc = self.options.exclude_tools
        if inc and not any(fnmatch.fnmatchcase(name, p) for p in inc):
            return False
        return not any(fnmatch.fnmatchcase(name, p) for p in exc)

    # -------------------------------------------------------------- sections
    def lifecycle(self) -> Dict[str, Any]:
        snap = self.snapshot
        init_params = {
            "protocolVersion": snap.protocol_version,
            "capabilities": {},
            "clientInfo": {"name": "mcp2rest2postman", "version": __version__},
        }
        items = [
            self._request(
                "initialize",
                "initialize",
                init_params,
                scripts.initialize_tests(
                    ms.INITIALIZE_RESULT,
                    str(snap.server_info.get("name", "")),
                    snap.capabilities.keys(),
                ),
                "Opens an MCP session. Stores `Mcp-Session-Id` and the negotiated "
                "protocol version in collection variables for the following requests.",
                with_session=False,
            ),
            self._request(
                "notifications/initialized",
                "notifications/initialized",
                None,
                scripts.notification_tests(),
                "Tells the server the client finished initialisation (no response body expected).",
                notification=True,
            ),
            self._request(
                "ping",
                "ping",
                None,
                scripts._lines(scripts.HELPERS, scripts.jsonrpc_success(ms.EMPTY_RESULT, "empty result")),
                "Liveness check.",
            ),
        ]
        return self._folder("00 Lifecycle", items, "MCP handshake. Run this folder first.")

    def tools(self) -> Optional[Dict[str, Any]]:
        snap = self.snapshot
        if "tools" not in snap.capabilities and not snap.tools:
            return None
        selected = [t for t in snap.tools if self._tool_selected(str(t.get("name", "")))]
        expected = [
            {"name": t.get("name"), "inputSchema": t.get("inputSchema")} for t in snap.tools
        ]
        list_item = self._request(
            "tools/list",
            "tools/list",
            None,
            scripts.list_tests(
                ms.LIST_TOOLS_RESULT, "ListToolsResult", "tools", "name", expected, "inputSchema"
            ),
            "Lists the tools and checks they still match the snapshot used to generate this collection.",
        )
        folders: List[Dict[str, Any]] = [list_item]
        for tool in selected:
            folders.append(self._tool_folder(tool))
        if self.options.protocol_tests:
            folders.append(
                self._request(
                    "tools/call unknown tool -> error",
                    "tools/call",
                    {"name": "mcp2rest2postman_unknown_tool", "arguments": {}},
                    scripts.rejection_tests(
                        "unknown tool",
                        ms.CALL_TOOL_RESULT,
                        ms.JSONRPC_ERROR,
                    ),
                    "Calling a tool that does not exist must fail.",
                )
            )
        return self._folder("01 Tools", folders, f"{len(selected)} tool(s).")

    def _tool_folder(self, tool: Dict[str, Any]) -> Dict[str, Any]:
        name = str(tool.get("name"))
        schema = tool.get("inputSchema") or {"type": "object"}
        resolver = SchemaResolver(schema)
        required, optional, props = required_and_optional(schema)
        overrides = self.options.tool_values.get(name, {})
        output_schema = tool.get("outputSchema")
        ok_schema = ms.call_tool_result(output_schema)
        ajv_output = sanitize_for_ajv(output_schema) if output_schema else None

        def args(include_optional: bool, only: Optional[List[str]] = None) -> Dict[str, Any]:
            built = sample_object(schema, include_optional=include_optional, resolver=resolver)
            if only is not None:
                built = {k: v for k, v in built.items() if k in required or k in only}
                for key in only:
                    if key not in built and key in props:
                        built[key] = sample_value(props[key], key, True, resolver)
            for key, value in overrides.items():
                # Overrides replace generated samples; unknown keys only go in the "all" variant.
                if key in built or (include_optional and key not in props):
                    built[key] = value
            return built

        def call(
            label: str, arguments: Dict[str, Any], tests: List[str], doc: str, literal: Any = ()
        ) -> Dict[str, Any]:
            return self._request(
                f"{name} - {label}",
                "tools/call",
                {"name": name, "arguments": self._vars(arguments, [name], literal)},
                tests,
                doc,
            )

        success = scripts.tool_success_tests(ok_schema, ajv_output)
        # One subfolder per kind of test (SA-3277); empty kinds are left out.
        valid: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []
        wrong: List[Dict[str, Any]] = []
        all_args = args(include_optional=True)
        valid.append(
            call(
                "valid, all attributes",
                all_args,
                success,
                "Every declared property (required and optional) with a valid sample value.",
            )
        )
        if optional:
            valid.append(
                call(
                    "valid, required attributes only",
                    args(include_optional=False),
                    success,
                    "Only the required properties: optional attributes must really be optional.",
                )
            )
        if self.options.optional_variants and len(optional) > 1:
            for key in optional:
                valid.append(
                    call(
                        f"valid, required + optional '{key}'",
                        args(include_optional=False, only=[key]),
                        success,
                        f"Required properties plus the optional `{key}`.",
                    )
                )
        if self.options.negative_tests:
            for key in required:
                broken = {k: v for k, v in all_args.items() if k != key}
                missing.append(
                    call(
                        f"missing required '{key}' -> error",
                        broken,
                        scripts.rejection_tests(
                            f"missing required attribute '{key}'", ms.CALL_TOOL_RESULT, ms.JSONRPC_ERROR
                        ),
                        f"`{key}` is required by the inputSchema: the server must reject the call.",
                    )
                )
            if self.options.wrong_type_tests:
                for key in required + optional:
                    bad, why = wrong_type_value(props.get(key), resolver)
                    if is_missing(bad):
                        continue
                    broken = dict(all_args)
                    broken[key] = bad
                    kind = "required" if key in required else "optional"
                    wrong.append(
                        call(
                            f"wrong type for {kind} '{key}' -> error",
                            broken,
                            scripts.rejection_tests(
                                f"wrong type for '{key}' ({why})", ms.CALL_TOOL_RESULT, ms.JSONRPC_ERROR
                            ),
                            f"`{key}` receives {why}: the server must reject the call.",
                            literal={key},
                        )
                    )
        description = self._tool_description(tool, required, optional, props)
        subfolders = [
            ("valid", valid, "Calls the server must accept."),
            ("missing required -> error", missing, "One required attribute left out per request."),
            ("wrong type -> error", wrong, "One attribute with a value of the wrong type per request."),
        ]
        items = [self._folder(label, reqs, doc) for label, reqs, doc in subfolders if reqs]
        return self._folder(f"tool: {name}", items, description)

    @staticmethod
    def _tool_description(
        tool: Dict[str, Any], required: List[str], optional: List[str], props: Dict[str, Any]
    ) -> str:
        lines = [str(tool.get("description") or tool.get("title") or "").strip(), ""]
        if props:
            lines += ["| Attribute | Type | Required | Description |", "|---|---|---|---|"]
            for key in required + optional:
                sub = props.get(key) or {}
                typ = sub.get("type", "any") if isinstance(sub, dict) else "any"
                if isinstance(typ, list):
                    typ = " / ".join(map(str, typ))
                desc = str(sub.get("description", "")).replace("|", "\\|").replace("\n", " ") if isinstance(sub, dict) else ""
                lines.append(f"| `{key}` | {typ} | {'yes' if key in required else 'no'} | {desc} |")
        if tool.get("outputSchema"):
            lines += ["", "Declares an `outputSchema`: `structuredContent` is validated against it."]
        return "\n".join(lines).strip()

    def resources(self) -> Optional[Dict[str, Any]]:
        snap = self.snapshot
        if not self.options.include_resources or "resources" not in snap.capabilities:
            return None
        items = [
            self._request(
                "resources/list",
                "resources/list",
                None,
                scripts.list_tests(
                    ms.LIST_RESOURCES_RESULT,
                    "ListResourcesResult",
                    "resources",
                    "uri",
                    [{"uri": r.get("uri")} for r in snap.resources],
                ),
            ),
            self._request(
                "resources/templates/list",
                "resources/templates/list",
                None,
                scripts.list_tests(
                    ms.LIST_RESOURCE_TEMPLATES_RESULT,
                    "ListResourceTemplatesResult",
                    "resourceTemplates",
                    "uriTemplate",
                    [{"uriTemplate": r.get("uriTemplate")} for r in snap.resource_templates],
                ),
            ),
        ]
        for res in snap.resources:
            uri = res.get("uri")
            if not isinstance(uri, str):
                continue
            items.append(
                self._request(
                    f"resources/read {res.get('name') or uri}",
                    "resources/read",
                    {"uri": uri},
                    scripts.resource_read_tests(ms.READ_RESOURCE_RESULT, uri),
                    str(res.get("description") or ""),
                )
            )
        if self.options.protocol_tests and self.options.negative_tests:
            items.append(
                self._request(
                    "resources/read missing 'uri' -> error",
                    "resources/read",
                    {},
                    scripts.rejection_tests(
                        "missing required 'uri'", ms.READ_RESOURCE_RESULT, ms.JSONRPC_ERROR, False
                    ),
                )
            )
        return self._folder("02 Resources", items)

    def prompts(self) -> Optional[Dict[str, Any]]:
        snap = self.snapshot
        if not self.options.include_prompts or "prompts" not in snap.capabilities:
            return None
        items = [
            self._request(
                "prompts/list",
                "prompts/list",
                None,
                scripts.list_tests(
                    ms.LIST_PROMPTS_RESULT,
                    "ListPromptsResult",
                    "prompts",
                    "name",
                    [{"name": p.get("name"), "arguments": p.get("arguments") or []} for p in snap.prompts],
                    "arguments",
                ),
            )
        ]
        for prompt in snap.prompts:
            pname = prompt.get("name")
            arguments = [a for a in prompt.get("arguments") or [] if isinstance(a, dict) and a.get("name")]
            required = [a["name"] for a in arguments if a.get("required")]
            optional = [a["name"] for a in arguments if not a.get("required")]
            all_args = {a: f"example-{a}" for a in required + optional}
            pv = ["prompt", str(pname)]
            ok = scripts._lines(scripts.HELPERS, scripts.jsonrpc_success(ms.GET_PROMPT_RESULT, "GetPromptResult"))
            items.append(
                self._request(
                    f"prompts/get {pname} - all arguments",
                    "prompts/get",
                    {"name": pname, "arguments": self._vars(all_args, pv)},
                    ok,
                    str(prompt.get("description") or ""),
                )
            )
            if optional:
                items.append(
                    self._request(
                        f"prompts/get {pname} - required arguments only",
                        "prompts/get",
                        {"name": pname, "arguments": self._vars({a: all_args[a] for a in required}, pv)},
                        ok,
                    )
                )
            if self.options.negative_tests:
                for arg in required:
                    items.append(
                        self._request(
                            f"prompts/get {pname} - missing required '{arg}' -> error",
                            "prompts/get",
                            {"name": pname, "arguments": self._vars({k: v for k, v in all_args.items() if k != arg}, pv)},
                            scripts.rejection_tests(
                                f"missing required argument '{arg}'",
                                ms.GET_PROMPT_RESULT,
                                ms.JSONRPC_ERROR,
                                allow_tool_error=False,
                            ),
                        )
                    )
        return self._folder("03 Prompts", items)

    def protocol(self) -> Optional[Dict[str, Any]]:
        if not self.options.protocol_tests:
            return None
        items = [
            self._request(
                "unknown method -> -32601",
                "mcp2rest2postman/unknown",
                {},
                scripts.error_code_tests([-32601], "unknown method returns 'Method not found'", ms.JSONRPC_ERROR),
                "JSON-RPC requires -32601 (Method not found) for unsupported methods.",
            )
        ]
        return self._folder("90 Protocol", items, "Generic JSON-RPC / MCP conformance checks.")

    #: Proposed in the negative negotiation check: no server supports it.
    UNKNOWN_PROTOCOL_VERSION = "1999-01-01"

    def protocol_versions(self) -> Optional[Dict[str, Any]]:
        versions = self.options.protocol_versions
        if not self.options.protocol_tests or not versions:
            return None
        negotiated = self.snapshot.protocol_version
        known = list(dict.fromkeys(list(SUPPORTED_PROTOCOL_VERSIONS) + list(versions)))
        items = []
        for version in list(versions) + [self.UNKNOWN_PROTOCOL_VERSION]:
            unknown = version == self.UNKNOWN_PROTOCOL_VERSION
            params = {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "mcp2rest2postman", "version": __version__},
            }
            items.append(
                self._request(
                    f"initialize with unknown version {version}" if unknown else f"initialize with {version}",
                    "initialize",
                    params,
                    scripts.protocol_version_tests(
                        ms.INITIALIZE_RESULT, version, must_echo=version == negotiated, known=known
                    ),
                    "Proposes a version no server supports: the server must answer with one it does support."
                    if unknown
                    else f"Proposes protocol `{version}`. The server must answer with the same version "
                    "if it supports it, and otherwise with another version it supports.",
                    with_session=False,
                )
            )
        return self._folder(
            "91 Protocol versions",
            items,
            "Version negotiation (`initialize`). Each request opens a separate session and leaves "
            f"`mcpSessionId` untouched. Discovery negotiated `{negotiated}`.",
        )

    def teardown(self) -> Optional[Dict[str, Any]]:
        if not self.options.teardown:
            return None
        item = {
            "name": "terminate session (DELETE)",
            "request": {
                "method": "DELETE",
                "header": [h for h in self._headers() if h["key"] not in ("Content-Type",)],
                "url": self._url(),
                "description": "Explicitly ends the MCP session. Servers may answer 405 if unsupported.",
            },
            "event": [
                {"listen": "test", "script": {"type": "text/javascript", "exec": scripts.teardown_tests()}}
            ],
        }
        return self._folder("99 Teardown", [item])

    # ----------------------------------------------------------------- build
    def build(self) -> Dict[str, Any]:
        snap = self.snapshot
        name = self.options.collection_name or f"{snap.server_name} (MCP)"
        sections = [
            self.lifecycle(),
            self.tools(),
            self.resources(),
            self.prompts(),
            self.protocol(),
            self.protocol_versions(),
            self.teardown(),
        ]
        variables = [
            {"key": "baseUrl", "value": self.options.base_url, "type": "string"},
            {"key": "protocolVersion", "value": snap.protocol_version, "type": "string"},
            {"key": "mcpSessionId", "value": "", "type": "string"},
        ]
        for header, value in self.options.headers.items():
            variables.append(
                {
                    "key": header_variable(header),
                    "value": value if self.options.embed_header_values else "",
                    "type": "string",
                }
            )
        description = [
            f"Generated by mcp2rest2postman {__version__} from **{snap.server_name}** "
            f"(protocol `{snap.protocol_version}`).",
            "",
            "Run the folders in order: `00 Lifecycle` opens the session used by the rest.",
        ]
        if snap.instructions:
            description += ["", "Server instructions:", "", str(snap.instructions)]
        seed = json.dumps([name, snap.server_info, [t.get("name") for t in snap.tools]], sort_keys=True)
        return {
            "info": {
                "_postman_id": str(uuid.uuid5(_NAMESPACE, seed)),
                "name": name,
                "description": "\n".join(description),
                "schema": POSTMAN_SCHEMA,
            },
            "item": [s for s in sections if s],
            "event": [
                {
                    "listen": "prerequest",
                    "script": {
                        "type": "text/javascript",
                        "exec": scripts.COLLECTION_PREREQUEST.rstrip("\n").split("\n"),
                    },
                }
            ],
            "variable": variables,
        }


def build_collection(snapshot: ServerSnapshot, options: Optional[GeneratorOptions] = None) -> Dict[str, Any]:
    """Convenience wrapper around :class:`CollectionBuilder` (collection only)."""
    return CollectionBuilder(snapshot, options).build()


def build_collection_and_environment(
    snapshot: ServerSnapshot, options: Optional[GeneratorOptions] = None, environment_name: Optional[str] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build the collection and the matching environment (with any body variables)."""
    builder = CollectionBuilder(snapshot, options)
    collection = builder.build()
    name = environment_name or collection["info"]["name"]
    return collection, build_environment(builder.options, name, builder.body_variables)


def build_environment(
    options: GeneratorOptions, name: str, body_variables: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """A Postman environment with ``baseUrl``, custom headers and body variables."""
    values = [{"key": "baseUrl", "value": options.base_url, "type": "default", "enabled": True}]
    for header, value in options.headers.items():
        values.append(
            {
                "key": header_variable(header),
                "value": value if options.embed_header_values else "",
                "type": "secret",
                "enabled": True,
            }
        )
    for key, value in (body_variables or {}).items():
        values.append({"key": key, "value": environment_value(value), "type": "default", "enabled": True})
    return {
        "id": str(uuid.uuid5(_NAMESPACE, "env:" + name)),
        "name": name,
        "values": values,
        "_postman_variable_scope": "environment",
    }
