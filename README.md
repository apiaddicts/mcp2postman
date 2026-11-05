# mcp2rest2postman

Turn an **MCP server** (Model Context Protocol) into a **Postman collection** that
initializes a session, calls every tool, resource and prompt, and **tests the
responses**. The tests check that responses follow the MCP schema, that the
server still matches the contract it published, and that required and optional
attributes behave as declared.

```
MCP server ──(initialize, tools/list, resources/list, prompts/list)──▶ snapshot ──▶ Postman collection
 (HTTP or stdio)                                                                    + environment
```

* Works with **Streamable HTTP** servers (`--url`) and **stdio** servers (`--stdio`).
* Handles both `application/json` and `text/event-stream` (SSE) responses, in
  the CLI and in the generated Postman scripts.
* Builds **positive and negative tests** for each tool, based on its `inputSchema`:
  * valid call with all attributes,
  * valid call with required attributes only,
  * valid call with the required attributes plus one optional attribute, for each optional attribute,
  * call without each required attribute, which must be rejected,
  * call with a wrong type for each attribute, which must be rejected.
* **Validates responses** with JSON Schema: `InitializeResult`, `CallToolResult`,
  `structuredContent` against the tool's `outputSchema`, `ReadResourceResult`,
  `GetPromptResult`, and the JSON-RPC error shape.
* **Detects drift.** `tools/list` and `prompts/list` fail when a tool or prompt
  disappears or its input schema changes after the collection was generated.
* Has no runtime dependencies (Python ≥ 3.9 standard library only).
* Licensed under Apache 2.0.

## Installation

```bash
pip install git+https://github.com/apiaddicts/mcp2rest2postman.git
# or, from a clone
pip install -e .
```

This installs the `mcp2rest2postman` command. You can also run it with
`python -m mcp2rest2postman`.

## Quick start

Try it with the bundled demo server:

```bash
python examples/demo_server.py --http --port 8765 &

mcp2rest2postman inspect  --url http://localhost:8765/mcp
mcp2rest2postman generate --url http://localhost:8765/mcp \
    -o demo.postman_collection.json \
    --environment-out demo.postman_environment.json

# run it (any of these)
postman collection run demo.postman_collection.json
newman run demo.postman_collection.json
```

Output of `inspect`:

```
Demo MCP Server 1.0.0
  protocol:     2025-06-18
  capabilities: prompts, resources, tools

Tools (3):
  - add(a*, b*)
  - greet(name*, language, excited)
  - search_items(query*, limit, tags, filters)
...
(* = required)
```

With the demo server, the generated collection has 38 requests and 150
assertions, and all of them pass. `examples/demo.postman_collection.json` is
an example of the output.

You can also import the collection into the Postman app (*Import → File*).
**Run the `00 Lifecycle` folder first.** It opens the MCP session and stores
`Mcp-Session-Id` and the negotiated protocol version, and the other requests
use those values.

## Usage

### `generate`

```text
mcp2rest2postman generate (--url URL | --stdio COMMAND | --from-snapshot FILE) [options]
```

| Option | Description |
|---|---|
| `--url URL` | Streamable HTTP endpoint, for example `http://localhost:3000/mcp` |
| `--stdio "CMD"` | Command that starts a stdio server, for example `"npx -y @modelcontextprotocol/server-everything"` |
| `--from-snapshot FILE` | Generate offline from a snapshot saved earlier |
| `-H 'Name: value'` | Extra HTTP header (repeatable). Typical use: `-H 'Authorization: Bearer …'` |
| `--env KEY=VALUE` | Environment variable for the stdio server (repeatable) |
| `-o FILE` | Collection output file (`-` for stdout). The default is `<server>.postman_collection.json` |
| `--environment-out FILE` | Also write a Postman environment with `baseUrl` and the header variables |
| `--snapshot-out FILE` | Also save the discovered server snapshot |
| `--base-url URL` | Value of `{{baseUrl}}`. The default is `--url`, or `http://localhost:3000/mcp` for stdio |
| `--name NAME` | Collection name |
| `--embed-header-values` | Write `-H` values into the collection and environment (see [Secrets](#secrets)) |
| `--include-tool GLOB` / `--exclude-tool GLOB` | Choose which tools get requests (repeatable) |
| `--values FILE` | Argument values for each tool, which replace the generated samples |
| `--no-negative-tests` | Do not generate the *missing required* and *wrong type* requests |
| `--no-wrong-type-tests` | Do not generate the *wrong type* requests |
| `--no-optional-variants` | Do not generate one request per optional attribute |
| `--no-protocol-tests` | Do not generate the generic JSON-RPC checks (unknown method, unknown tool) |
| `--no-resources` / `--no-prompts` / `--no-teardown` | Leave out those folders |
| `--protocol-version V` | Protocol version proposed in `initialize` (the default is `2025-06-18`) |
| `--timeout S` | Seconds to wait for each response during discovery |

### `inspect`

```text
mcp2rest2postman inspect (--url URL | --stdio COMMAND) [-H ...] [--json]
```

Prints the server's tools (required attributes are marked with `*`), resources
and prompts. With `--json`, it prints the full snapshot instead.

### Realistic argument values

The generator creates sample values from each JSON schema. It uses `const`,
`default`, `examples`, `enum`, `format`, bounds, `pattern`, `$ref`, `anyOf`,
`oneOf` and `allOf`. When a tool needs real data, such as an existing ID, put
it in a values file:

```json
{
  "add":   { "a": 40, "b": 2 },
  "greet": { "name": "Ada", "language": "es" }
}
```

```bash
mcp2rest2postman generate --url … --values examples/values.json
```

### stdio servers

Postman can only send HTTP requests. For a stdio server, the CLI starts the
process to discover what it exposes, but the collection targets `{{baseUrl}}`.
To run the collection, expose the server over Streamable HTTP, for example with
a stdio-to-HTTP bridge, and pass `--base-url` or edit the variable:

```bash
mcp2rest2postman generate --stdio "python examples/demo_server.py --stdio" \
    --base-url http://localhost:8000/mcp
```

### Secrets

By default, every `-H` header is sent to the server during discovery. In the
collection, it becomes a reference to an **empty** variable (`Authorization:
{{header_authorization}}`), so you don't commit credentials by accident. Supply
the value when you run the collection:

```bash
postman collection run c.json --env-var "header_authorization=Bearer $TOKEN"
newman run c.json --env-var "header_authorization=Bearer $TOKEN"
```

To store the values in the collection and environment files, use
`--embed-header-values`.

### Snapshots and CI

```bash
# once: discover and store the contract
mcp2rest2postman generate --url $MCP_URL --snapshot-out mcp.snapshot.json -o mcp.postman_collection.json
git add mcp.snapshot.json mcp.postman_collection.json

# in CI: run the collection against the deployed server
postman collection run mcp.postman_collection.json --env-var "baseUrl=$MCP_URL"
```

If someone removes a tool or changes its `inputSchema`, the `tools/list`
drift tests fail.

## What the collection contains

```
<Server> (MCP)
├── 00 Lifecycle
│   ├── initialize                       → stores Mcp-Session-Id + protocolVersion
│   ├── notifications/initialized        → 202 Accepted
│   └── ping
├── 01 Tools
│   ├── tools/list                       → schema + drift checks
│   ├── tool: <name>
│   │   ├── <name> - valid, all attributes
│   │   ├── <name> - valid, required attributes only
│   │   ├── <name> - valid, required + optional '<opt>'      (one per optional attribute)
│   │   ├── <name> - missing required '<req>' -> error       (one per required attribute)
│   │   └── <name> - wrong type for required|optional '<attr>' -> error
│   └── tools/call unknown tool -> error
├── 02 Resources        resources/list, resources/templates/list, resources/read <each>, missing uri -> error
├── 03 Prompts          prompts/list, prompts/get (all / required only / missing required -> error)
├── 90 Protocol         unknown method -> -32601
└── 99 Teardown         DELETE session
```

**Valid calls** pass when the HTTP status is 200, the body contains a JSON-RPC
2.0 response with the request's `id`, there is no `error`, the `result` matches
the MCP schema, `isError` is not `true`, and `structuredContent` matches the
tool's `outputSchema` when the tool declares one.

**Invalid calls** pass when the server rejects them, either with a JSON-RPC
`error` or with a tool result that has `isError: true`. The MCP specification
allows both. The error payload must also be well formed, and the server must
not answer with a 5xx status.

> ⚠️ The collection **really calls every tool**, several times. Don't run it
> against tools with side effects (sending email, payments, deletes) on a
> production server. Use `--exclude-tool` or `--values`, or point it at a test
> environment.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest              # unit + integration tests (end-to-end tests run when newman or postman CLI is installed)
ruff check .
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the code is organized.

## Limitations

* The legacy HTTP+SSE transport (protocol 2024-11-05, separate `/sse` and
  `/messages` endpoints) isn't supported. Use Streamable HTTP or stdio.
* OAuth flows aren't automated. Get a token yourself and pass it with `-H`.
* Generated values satisfy common JSON Schema constraints. A complex `pattern`
  or semantic constraints, such as an ID that has to exist, may need `--values`.
* The `tools/list` drift checks only look at the first page when the server
  paginates.

## License

Apache License 2.0. See [LICENSE.md](LICENSE.md).
