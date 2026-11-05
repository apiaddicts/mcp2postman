import json

import pytest

from mcp2rest2postman.discovery import ServerSnapshot
from mcp2rest2postman.postman import POSTMAN_SCHEMA, GeneratorOptions, build_collection, build_environment

from helpers import body, by_name, flatten

SNAPSHOT = ServerSnapshot(
    protocol_version="2025-06-18",
    server_info={"name": "srv", "version": "1"},
    capabilities={"tools": {}, "prompts": {}},
    tools=[
        {
            "name": "create_user",
            "description": "Creates a user",
            "inputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "email": {"type": "string", "format": "email"},
                    "age": {"type": "integer", "minimum": 18},
                    "nickname": {"type": "string"},
                    "admin": {"type": "boolean"},
                },
                "required": ["email", "age"],
            },
            "outputSchema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {"id": {"type": "string", "format": "custom-id"}},
                "required": ["id"],
            },
        },
        {"name": "noop", "inputSchema": {"type": "object"}},
    ],
    prompts=[{"name": "p", "arguments": [{"name": "x", "required": True}, {"name": "y"}]}],
)


@pytest.fixture
def collection():
    return build_collection(SNAPSHOT, GeneratorOptions(base_url="http://h/mcp"))


def test_collection_shape(collection):
    assert collection["info"]["schema"] == POSTMAN_SCHEMA
    assert collection["info"]["name"] == "srv (MCP)"
    folders = [f["name"] for f in collection["item"]]
    assert folders == ["00 Lifecycle", "01 Tools", "03 Prompts", "90 Protocol", "99 Teardown"]
    variables = {v["key"]: v["value"] for v in collection["variable"]}
    assert variables == {"baseUrl": "http://h/mcp", "protocolVersion": "2025-06-18", "mcpSessionId": ""}
    # collection is deterministic
    assert collection == build_collection(SNAPSHOT, GeneratorOptions(base_url="http://h/mcp"))


def test_jsonrpc_bodies_are_valid_and_ids_unique(collection):
    ids = []
    for item in flatten(collection["item"]):
        if item["request"]["method"] != "POST":
            continue
        payload = body(item)
        assert payload["jsonrpc"] == "2.0"
        if "id" in payload:
            ids.append(payload["id"])
        else:
            assert payload["method"].startswith("notifications/")
    assert len(ids) == len(set(ids))


def test_initialize_has_no_session_headers(collection):
    init = by_name(collection)["initialize"]
    keys = [h["key"] for h in init["request"]["header"]]
    assert "Mcp-Session-Id" not in keys
    assert "mcpSessionId" in "\n".join(init["event"][0]["script"]["exec"])


def test_required_and_optional_variants(collection):
    items = by_name(collection)
    all_args = body(items["create_user - valid, all attributes"])["params"]["arguments"]
    assert set(all_args) == {"email", "age", "nickname", "admin"}
    assert all_args["email"] == "user@example.com" and all_args["age"] == 18
    req_only = body(items["create_user - valid, required attributes only"])["params"]["arguments"]
    assert set(req_only) == {"email", "age"}
    for opt in ("nickname", "admin"):
        args = body(items[f"create_user - valid, required + optional '{opt}'"])["params"]["arguments"]
        assert set(args) == {"email", "age", opt}
    for req in ("email", "age"):
        args = body(items[f"create_user - missing required '{req}' -> error"])["params"]["arguments"]
        assert req not in args
    wrong = body(items["create_user - wrong type for required 'age' -> error"])["params"]["arguments"]
    assert isinstance(wrong["age"], str)
    assert "create_user - wrong type for optional 'admin' -> error" in items


def test_tool_without_properties(collection):
    names = [n for n in by_name(collection) if n.startswith("noop")]
    assert names == ["noop - valid, all attributes"]


def test_output_schema_is_sanitised_in_tests(collection):
    script = "\n".join(by_name(collection)["create_user - valid, all attributes"]["event"][0]["script"]["exec"])
    assert "structuredContent matches the tool outputSchema" in script
    assert "draft/2020-12" not in script
    assert "custom-id" not in script


def test_tools_list_drift_check_embeds_snapshot(collection):
    script = "\n".join(by_name(collection)["tools/list"]["event"][0]["script"]["exec"])
    assert '"create_user"' in script and "inputSchema unchanged" in script


def test_prompts(collection):
    items = by_name(collection)
    assert body(items["prompts/get p - all arguments"])["params"]["arguments"] == {"x": "example-x", "y": "example-y"}
    assert body(items["prompts/get p - required arguments only"])["params"]["arguments"] == {"x": "example-x"}
    assert body(items["prompts/get p - missing required 'x' -> error"])["params"]["arguments"] == {"y": "example-y"}


def test_options_reduce_output():
    opts = GeneratorOptions(
        negative_tests=False,
        optional_variants=False,
        protocol_tests=False,
        include_prompts=False,
        teardown=False,
        exclude_tools=["no*"],
    )
    names = list(by_name(build_collection(SNAPSHOT, opts)))
    assert not any("->" in n for n in names)
    assert not any(n.startswith("noop") for n in names)
    assert "create_user - valid, required attributes only" in names
    assert not any("optional '" in n for n in names)


def test_include_filter_and_value_overrides():
    opts = GeneratorOptions(include_tools=["create_*"], tool_values={"create_user": {"age": 42, "extra": 1}})
    items = by_name(build_collection(SNAPSHOT, opts))
    assert not any(n.startswith("noop") for n in items)
    assert body(items["create_user - valid, all attributes"])["params"]["arguments"]["age"] == 42
    assert body(items["create_user - valid, all attributes"])["params"]["arguments"]["extra"] == 1
    assert "extra" not in body(items["create_user - valid, required attributes only"])["params"]["arguments"]


def test_custom_headers_become_variables():
    opts = GeneratorOptions(headers={"Authorization": "Bearer s3cret"})
    col = build_collection(SNAPSHOT, opts)
    assert "s3cret" not in json.dumps(col)
    headers = by_name(col)["tools/list"]["request"]["header"]
    assert {"key": "Authorization", "value": "{{header_authorization}}"} in headers
    opts.embed_header_values = True
    assert "s3cret" in json.dumps(build_collection(SNAPSHOT, opts))
    env = build_environment(opts, "e")
    assert {v["key"] for v in env["values"]} == {"baseUrl", "header_authorization"}
