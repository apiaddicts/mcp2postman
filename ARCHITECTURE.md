# Architecture

`mcp2rest2postman` works as a two-stage pipeline. **Discovery** talks to a live
MCP server and creates a `ServerSnapshot`. **Generation** turns a snapshot into
a Postman collection. The snapshot is a plain JSON document, so the two stages
are independent. A snapshot can be saved, reviewed in Git and used again
without a running server.

```
            ┌──────────────┐   JSON-RPC    ┌──────────────┐
  --url ───▶│ HttpTransport│──────────────▶│              │
            └──────────────┘               │  MCP server  │
            ┌──────────────┐  stdin/stdout │              │
 --stdio ──▶│StdioTransport│──────────────▶│              │
            └──────┬───────┘               └──────────────┘
                   │ Transport
            ┌──────▼───────┐
            │  MCPClient   │  initialize → notifications/initialized → */list (paginated)
            └──────┬───────┘
            ┌──────▼───────┐      --snapshot-out / --from-snapshot
            │ discovery.py │◀────────────────────────────────▶ snapshot.json
            │ServerSnapshot│
            └──────┬───────┘
            ┌──────▼──────────────┐   uses   ┌─────────────────┐  ┌──────────────┐
            │ postman.py          │─────────▶│ schema_values.py│  │ mcp_schemas  │
            │ CollectionBuilder   │─────────▶│ scripts.py (JS) │  │ (draft-07)   │
            └──────┬──────────────┘          └─────────────────┘  └──────────────┘
                   ▼
     collection.json (v2.1) + environment.json
```

## Modules

| Module | Responsibility |
|---|---|
| `cli.py` | `argparse` front end (`generate`, `inspect`). Maps flags to `GeneratorOptions`, sets exit codes (0 success, 1 I/O or input error, 2 MCP or transport error). |
| `config.py` | Loads one server from an `mcp-config.json` (`mcpServers`, VS Code `servers` + `inputs`, or a single object). Expands `${VAR}`, `${env:VAR}`, `${VAR:-default}` and prompts for `${input:id}`. Returns a `ServerConfig` that `cli.py` maps to `--url`/`-H` or `--stdio`/`--env`, with command-line values taking precedence. |
| `client.py` | Minimal MCP client with no dependencies. `HttpTransport` implements Streamable HTTP: a POST with `Accept: application/json, text/event-stream`, handling of `Mcp-Session-Id` and `MCP-Protocol-Version`, and decoding of JSON or SSE bodies. `StdioTransport` starts the server process, exchanges newline-delimited JSON-RPC, answers server `ping`s and enforces timeouts with a reader thread. `MCPClient` runs the handshake and paginated list operations. |
| `discovery.py` | `discover()` calls the list operations for each capability the server announces. When an operation fails, discovery records a warning and continues. `ServerSnapshot` serializes the result (`format: mcp2rest2postman/snapshot@1`). |
| `schema_values.py` | JSON Schema helpers. `sample_value` / `sample_object` create valid samples. They handle local `$ref`, `allOf` (merged), `anyOf` and `oneOf` (first branch that isn't null), `const`, `default`, `examples`, `enum`, `format`, string length, `pattern` (tries known candidates), numeric bounds and `multipleOf`, and array sizes. `wrong_type_value` picks a value of a type the schema doesn't accept. `sanitize_for_ajv` makes schemas compile in Postman's sandbox. `required_and_optional` splits the properties of an object. |
| `mcp_schemas.py` | Draft-07 schemas for MCP results (`InitializeResult`, `CallToolResult` and others) and for JSON-RPC errors. They're strict on the fields the spec requires and allow anything else, so servers on newer protocol revisions still pass. |
| `scripts.py` | JavaScript templates for the Postman test scripts. |
| `postman.py` | `CollectionBuilder` creates the folders, requests, variables and environment from a snapshot and `GeneratorOptions`. |

## Key design decisions

### No runtime dependencies
The tool only needs `urllib`, `subprocess`, `argparse` and `json`. It installs
anywhere, including locked-down CI images, and has no supply-chain surface.
The official MCP SDK is async and much larger than discovery needs.

### Snapshot as the contract
The generator reads the snapshot, not the server. This gives three benefits:

1. **Reproducible output.** The same snapshot always produces the same
   collection. IDs are sequential and `_postman_id` is a UUIDv5 of the server
   identity, which keeps Git diffs clean.
2. **Offline regeneration.** `--from-snapshot` with different options needs no
   running server.
3. **Drift detection.** The generated `tools/list` and `prompts/list` tests
   embed the expected names and schemas, so the collection fails when the live
   server no longer matches the snapshot.

### Self-contained test scripts
Postman doesn't share functions between collection-level and request-level
scripts. Every request's test script therefore begins with the same small
`mcp` helper (`scripts.HELPERS`), which:

* reads the body as JSON, or as SSE when `Content-Type: text/event-stream` is
  set or JSON parsing fails,
* flattens JSON-RPC batches,
* finds the response whose `id` matches the request body, ignoring
  notifications that arrive on the same stream.

The collection-level pre-request script only removes the `Mcp-Session-Id` and
`MCP-Protocol-Version` headers while they are empty. This supports stateless
servers that don't issue a session.

### Session handling
`initialize` is sent without session headers. Its test script stores the
`Mcp-Session-Id` response header and the negotiated `protocolVersion` in
collection variables. The following requests send `{{mcpSessionId}}` and
`{{protocolVersion}}`. The `99 Teardown` folder sends `DELETE` to end the
session. Servers that don't support termination can answer with 405 or 404.

### Schema validation inside Postman
The tests use Postman's built-in `pm.expect(obj).to.have.jsonSchema(schema)`,
which runs Ajv with draft-07. Tool `outputSchema`s often declare
`$schema: …/2020-12/schema` or custom formats, and Ajv refuses to compile
those. `sanitize_for_ajv` removes `$schema` and `$id` and drops `format`
values that Ajv doesn't know. It leaves *property names* alone, even a
property called `$schema`.

### Positive vs. negative test semantics
| Request kind | Passes when |
|---|---|
| valid (all, required only, required plus one optional) | HTTP 200, a JSON-RPC response with a matching `id`, no `error`, a result that matches the schema, `isError` not `true`, and `structuredContent` that matches `outputSchema` when the tool declares one |
| missing required or wrong type | HTTP status below 500, and **either** a JSON-RPC `error` **or** `result.isError === true` (the MCP spec allows both for input validation), and a well-formed error payload |
| prompt missing a required argument, resource without `uri` | a JSON-RPC `error` |
| unknown method | `error.code === -32601` |

Wrong-type tests are generated only for attributes whose schema limits the
type. An unconstrained `{}` accepts any value.

## Extending

* **New request kinds:** add a method to `CollectionBuilder` that returns a
  folder, add it in `build()`, and put its assertions in `scripts.py`.
* **New transports**, such as legacy HTTP+SSE: subclass `Transport` and
  implement `request()` and `notify()`. `discover()` doesn't depend on the
  transport.
* **Better samples:** extend `_FORMAT_SAMPLES` or `_PATTERN_CANDIDATES` in
  `schema_values.py`, or use `--values`.

## Testing strategy

| Layer | File | What it covers |
|---|---|---|
| Unit | `tests/test_schema_values.py` | Sample and wrong-type generation, `$ref` and combinators, Ajv sanitizing |
| Unit | `tests/test_postman.py` | Collection structure, variants for required and optional attributes, options, headers and secrets, determinism |
| Integration | `tests/test_client.py` | HTTP (JSON and SSE) and stdio transports against `examples/demo_server.py`, snapshot round trip |
| Integration | `tests/test_cli.py` | CLI flags, output files, exit codes |
| End to end | `tests/test_e2e_postman.py` | Runs generated collections with **newman** or the **Postman CLI** against the demo server. All assertions must pass, and drift must be detected. Skipped when neither runner is installed. |
