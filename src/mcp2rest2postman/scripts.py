"""JavaScript snippets embedded in the generated Postman collection.

Every test script starts with :data:`HELPERS`, which decodes the HTTP body
(plain JSON *or* Server-Sent Events, as allowed by the Streamable HTTP
transport) and finds the JSON-RPC response whose ``id`` matches the request.
Snippets are self-contained per request because Postman does not share
functions between collection-level and request-level scripts.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

HELPERS = r"""// --- mcp2rest2postman: decode JSON or SSE body and match the JSON-RPC id ---
const mcp = (() => {
  const body = pm.response.text() || '';
  const ctype = String(pm.response.headers.get('Content-Type') || '').toLowerCase();
  const parseSse = (txt) => {
    const out = []; let data = [];
    const flush = () => { if (data.length) { try { out.push(JSON.parse(data.join('\n'))); } catch (e) { /* ignore */ } data = []; } };
    txt.split(/\r?\n/).forEach((line) => {
      if (line === '') { flush(); } else if (line.startsWith('data:')) { data.push(line.slice(line.charAt(5) === ' ' ? 6 : 5)); }
    });
    flush();
    return out;
  };
  let messages = [];
  if (body.trim()) {
    if (ctype.includes('text/event-stream')) { messages = parseSse(body); } else {
      try { messages = [JSON.parse(body)]; } catch (e) { messages = parseSse(body); }
    }
  }
  let requestId;
  try { requestId = JSON.parse(pm.request.body.raw).id; } catch (e) { requestId = undefined; }
  const flat = [].concat(...messages.map((m) => (Array.isArray(m) ? m : [m])));
  const isResponse = (m) => m && typeof m === 'object' && ('result' in m || 'error' in m);
  const message = flat.find((m) => isResponse(m) && (requestId === undefined || m.id === requestId)) || null;
  return { messages: flat, message: message, result: message ? message.result : undefined, error: message ? message.error : undefined };
})();
"""

COLLECTION_PREREQUEST = r"""// --- mcp2rest2postman: drop empty session / protocol headers ---
if (!pm.collectionVariables.get('mcpSessionId') && !pm.variables.get('mcpSessionId')) {
  pm.request.headers.remove('Mcp-Session-Id');
}
if (!pm.variables.get('protocolVersion')) {
  pm.request.headers.remove('MCP-Protocol-Version');
}
"""


def _js(value: Any) -> str:
    """Serialise a Python value as a JavaScript literal."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _lines(*chunks: str) -> List[str]:
    text = "\n".join(c.rstrip("\n") for c in chunks if c)
    return text.split("\n")


def jsonrpc_success(result_schema: Dict[str, Any], label: str) -> str:
    return f"""pm.test('HTTP 200 OK', function () {{
  pm.response.to.have.status(200);
}});
pm.test('JSON-RPC 2.0 response matching the request id', function () {{
  pm.expect(mcp.message, 'no JSON-RPC response found in body: ' + pm.response.text().slice(0, 300)).to.be.an('object');
  pm.expect(mcp.message.jsonrpc).to.eql('2.0');
}});
pm.test('no JSON-RPC error', function () {{
  pm.expect(mcp.error, JSON.stringify(mcp.error)).to.not.exist;
}});
pm.test('result is a valid {label}', function () {{
  pm.expect(mcp.result).to.have.jsonSchema({_js(result_schema)});
}});
"""


def initialize_tests(
    result_schema: Dict[str, Any], server_name: str, capabilities: Iterable[str]
) -> List[str]:
    caps = sorted(capabilities)
    return _lines(
        HELPERS,
        jsonrpc_success(result_schema, "InitializeResult"),
        f"""pm.test('server identifies itself as {server_name}', function () {{
  pm.expect(mcp.result.serverInfo.name).to.eql({_js(server_name)});
}});
pm.test('server still announces the discovered capabilities', function () {{
  const caps = Object.keys(mcp.result.capabilities || {{}});
  pm.expect(caps).to.include.members({_js(caps)});
}});
// Keep the session and the negotiated protocol version for the next requests.
const sessionId = pm.response.headers.get('Mcp-Session-Id');
pm.collectionVariables.set('mcpSessionId', sessionId || '');
if (mcp.result && mcp.result.protocolVersion) {{
  pm.collectionVariables.set('protocolVersion', mcp.result.protocolVersion);
}}
""",
    )


def notification_tests() -> List[str]:
    return _lines(
        """pm.test('notification accepted (HTTP 202)', function () {
  pm.expect(pm.response.code).to.be.oneOf([200, 202, 204]);
});
"""
    )


def list_tests(
    result_schema: Dict[str, Any],
    label: str,
    collection_key: str,
    id_key: str,
    expected: List[Dict[str, Any]],
    compare_key: Optional[str] = None,
) -> List[str]:
    """Tests for ``*/list`` calls, including drift detection against the snapshot."""
    compare = ""
    if compare_key:
        compare = f"""
  expected.forEach(function (item) {{
    const onThisPage = items.some(function (x) {{ return x[{_js(id_key)}] === item[{_js(id_key)}]; }});
    if (!onThisPage && mcp.result.nextCursor) {{ return; }} // listed on a later page
    pm.test({_js(id_key)} + ' ' + item[{_js(id_key)}] + ': {compare_key} unchanged since generation', function () {{
      const found = items.find(function (x) {{ return x[{_js(id_key)}] === item[{_js(id_key)}]; }});
      pm.expect(found, 'not found').to.be.an('object');
      pm.expect(found[{_js(compare_key)}]).to.eql(item[{_js(compare_key)}]);
    }});
  }});"""
    return _lines(
        HELPERS,
        jsonrpc_success(result_schema, label),
        f"""if (mcp.result && Array.isArray(mcp.result[{_js(collection_key)}])) {{
  const items = mcp.result[{_js(collection_key)}];
  const expected = {_js(expected)};
  pm.test('all {collection_key} discovered at generation time are still exposed', function () {{
    const names = items.map(function (x) {{ return x[{_js(id_key)}]; }});
    const missing = expected.map(function (x) {{ return x[{_js(id_key)}]; }}).filter(function (n) {{ return names.indexOf(n) < 0; }});
    if (mcp.result.nextCursor) {{ return; }} // paginated: remaining items are on later pages
    pm.expect(missing, 'missing: ' + missing.join(', ')).to.be.empty;
  }});{compare}
}}
""",
    )


def tool_success_tests(
    result_schema: Dict[str, Any], output_schema: Optional[Dict[str, Any]]
) -> List[str]:
    extra = ""
    if output_schema:
        extra = f"""pm.test('structuredContent matches the tool outputSchema', function () {{
  pm.expect(mcp.result.structuredContent, 'structuredContent missing').to.be.an('object');
  pm.expect(mcp.result.structuredContent).to.have.jsonSchema({_js(output_schema)});
}});
"""
    return _lines(
        HELPERS,
        jsonrpc_success(result_schema, "CallToolResult"),
        """pm.test('tool executed without isError', function () {
  const text = mcp.result && Array.isArray(mcp.result.content)
    ? mcp.result.content.filter(function (c) { return c.type === 'text'; }).map(function (c) { return c.text; }).join(' ')
    : '';
  pm.expect(mcp.result && mcp.result.isError, 'tool reported an error: ' + text).to.not.equal(true);
});
""",
        extra,
    )


def rejection_tests(
    reason: str,
    result_schema: Dict[str, Any],
    error_schema: Dict[str, Any],
    allow_tool_error: bool = True,
) -> List[str]:
    """Tests for requests that the server must refuse."""
    accepted = (
        "a JSON-RPC error or a result with isError=true" if allow_tool_error else "a JSON-RPC error"
    )
    check = (
        "!!mcp.error || (!!mcp.result && mcp.result.isError === true)"
        if allow_tool_error
        else "!!mcp.error"
    )
    return _lines(
        HELPERS,
        f"""pm.test('no server error (HTTP < 500)', function () {{
  pm.expect(pm.response.code).to.be.below(500);
}});
pm.test({_js('rejected: ' + reason)}, function () {{
  pm.expect(mcp.message, 'no JSON-RPC response found in body: ' + pm.response.text().slice(0, 300)).to.be.an('object');
  const rejected = {check};
  pm.expect(rejected, 'expected {accepted}, got ' + JSON.stringify(mcp.message)).to.be.true;
}});
pm.test('error payload is well formed', function () {{
  if (mcp.error) {{
    pm.expect(mcp.error).to.have.jsonSchema({_js(error_schema)});
  }} else if (mcp.result) {{
    pm.expect(mcp.result).to.have.jsonSchema({_js(result_schema)});
  }}
}});
""",
    )


def error_code_tests(expected_codes: List[int], reason: str, error_schema: Dict[str, Any]) -> List[str]:
    return _lines(
        HELPERS,
        f"""pm.test('no server error (HTTP < 500)', function () {{
  pm.expect(pm.response.code).to.be.below(500);
}});
pm.test({_js(reason)}, function () {{
  pm.expect(mcp.error, 'expected a JSON-RPC error, got ' + JSON.stringify(mcp.message)).to.be.an('object');
  pm.expect(mcp.error).to.have.jsonSchema({_js(error_schema)});
  pm.expect(mcp.error.code).to.be.oneOf({_js(expected_codes)});
}});
""",
    )


def resource_read_tests(result_schema: Dict[str, Any], uri: str) -> List[str]:
    return _lines(
        HELPERS,
        jsonrpc_success(result_schema, "ReadResourceResult"),
        f"""pm.test('contents belong to {uri}', function () {{
  pm.expect(mcp.result.contents).to.not.be.empty;
  pm.expect(mcp.result.contents.map(function (c) {{ return c.uri; }})).to.include({_js(uri)});
}});
""",
    )


def teardown_tests() -> List[str]:
    return _lines(
        """pm.test('session terminated or termination not supported', function () {
  pm.expect(pm.response.code).to.be.oneOf([200, 202, 204, 400, 404, 405]);
});
pm.collectionVariables.set('mcpSessionId', '');
"""
    )
