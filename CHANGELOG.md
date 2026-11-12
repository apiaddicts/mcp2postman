# Changelog

## 1.0.0 - 2026-11-11

First release published on PyPI: `pip install mcp2postman`.

### Added
- Support for MCP protocol versions `2026-07-28`, `2025-11-25` and
  `2025-06-18`. Discovery proposes the newest one and, when a server rejects
  it with an error instead of negotiating, retries with the next older one.
  Connection failures are not retried. A negotiated version outside the list
  is reported as a snapshot warning. (SA-3290)
- New "91 Protocol versions" folder: one `initialize` per version plus an
  unknown version (`1999-01-01`), checking the negotiation rules without
  touching `mcpSessionId`. `--protocol-versions V1,V2` (or `none`) selects the
  versions, and `--no-protocol-tests` also skips the folder. (SA-3290)
- Package metadata for PyPI: project URLs, supported Python versions
  (3.10 to 3.13) and a source distribution that includes the tests and
  examples.

### Changed
- The tool is now called `mcp2postman`, like the repository: package on PyPI,
  Python module (`import mcp2postman`, `python -m mcp2postman`), command,
  MCP `clientInfo` name and `User-Agent`. Snapshots now use the format
  `mcp2postman/snapshot@1`; regenerate snapshots saved by 0.x versions.
- Each `tool: <name>` folder now holds one subfolder per test kind
  ("valid", "missing required -> error", "wrong type -> error"), so a single
  kind of test can be run or reviewed on its own. Empty kinds, for example
  with `--no-negative-tests`, are left out. (SA-3277)
- The README installs from PyPI and uses absolute links, so it renders on the
  PyPI project page.

### Removed
- Python 3.9 support. It reached end of life in October 2025 and is no longer
  available on the CI runners. Python 3.10 or later is required.

## 0.2.0 - 2026-11-05

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
