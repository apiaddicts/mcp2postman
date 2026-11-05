# Changelog

## 0.2.0

### Added
- `--body-variables`: tool and prompt argument values move from request
  bodies into environment variables (`{{<tool>_<arg>}}`,
  `{{prompt_<prompt>_<arg>}}`). Non-string values are inserted without quotes,
  so their JSON type is kept. (SA-3276)
- `--config` / `--server`: read the server and its auth from an
  `mcp-config.json`.

### Changed
- The Postman environment file is always written, next to the collection by
  default, and always contains `baseUrl`. `--environment-out` only sets its
  path. (SA-3276)
- Test scripts resolve variables before reading the JSON-RPC request id.
- The client sends its own `User-Agent`.

## 0.1.0

- First release: MCP (Streamable HTTP or stdio) to Postman collection, with
  tests for required and optional attributes, response schema validation and
  drift checks. (SA-3275)
