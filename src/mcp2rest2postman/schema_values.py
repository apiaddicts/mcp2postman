"""JSON-schema helpers: sample values, invalid values and Postman-safe schemas.

MCP tools describe their arguments with JSON Schema (``inputSchema``). To build
meaningful requests the generator needs:

* a **valid sample** for every property (respecting ``const``, ``default``,
  ``examples``, ``enum``, formats, bounds, ``pattern``...),
* a **value of the wrong type** for negative tests,
* a **sanitised schema** that the Ajv (draft-07) validator embedded in the
  Postman sandbox can compile.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

_MISSING = object()

#: Formats understood by the Ajv build that ships inside the Postman sandbox.
AJV_FORMATS = {
    "date",
    "time",
    "date-time",
    "uri",
    "uri-reference",
    "uri-template",
    "url",
    "email",
    "hostname",
    "ipv4",
    "ipv6",
    "regex",
    "uuid",
    "json-pointer",
    "json-pointer-uri-fragment",
    "relative-json-pointer",
}

_FORMAT_SAMPLES = {
    "date": "2024-01-01",
    "time": "12:00:00Z",
    "date-time": "2024-01-01T12:00:00Z",
    "duration": "P1D",
    "uri": "https://example.com",
    "url": "https://example.com",
    "uri-reference": "https://example.com/path",
    "iri": "https://example.com",
    "uri-template": "https://example.com/{id}",
    "email": "user@example.com",
    "idn-email": "user@example.com",
    "hostname": "example.com",
    "idn-hostname": "example.com",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "uuid": "123e4567-e89b-42d3-a456-426614174000",
    "regex": "^[a-z]+$",
    "json-pointer": "/a/b",
    "relative-json-pointer": "0/a",
    "byte": "ZXhhbXBsZQ==",
    "binary": "example",
    "password": "P4ssw0rd!",
}

_PATTERN_CANDIDATES = [
    "example",
    "abc",
    "ABC",
    "Example1",
    "123",
    "1",
    "a",
    "A1",
    "abc-123",
    "abc_123",
    "2024-01-01",
    "user@example.com",
    "https://example.com",
    "123e4567-e89b-42d3-a456-426614174000",
    "+34600000000",
    "AB12",
    "ab12",
]


class SchemaResolver:
    """Resolves local ``$ref`` pointers (``#/...``) inside a root schema."""

    def __init__(self, root: Any):
        self.root = root

    def resolve(self, schema: Any, _depth: int = 0) -> Any:
        if not isinstance(schema, dict) or "$ref" not in schema or _depth > 32:
            return schema
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#"):
            return {k: v for k, v in schema.items() if k != "$ref"}
        node: Any = self.root
        for part in [p for p in ref[1:].split("/") if p]:
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                return {}
        merged = dict(node) if isinstance(node, dict) else {}
        # Keep any nested "$ref" of the target so it is resolved recursively.
        merged.update({k: v for k, v in schema.items() if k != "$ref"})
        return self.resolve(merged, _depth + 1)


def _types(schema: Dict[str, Any]) -> List[str]:
    t = schema.get("type")
    if isinstance(t, list):
        return [x for x in t if isinstance(x, str)]
    if isinstance(t, str):
        return [t]
    if "properties" in schema or "required" in schema or "additionalProperties" in schema:
        return ["object"]
    if "items" in schema or "prefixItems" in schema:
        return ["array"]
    if "enum" in schema and schema["enum"]:
        return [_json_type(schema["enum"][0])]
    if any(k in schema for k in ("minLength", "maxLength", "pattern", "format")):
        return ["string"]
    if any(k in schema for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")):
        return ["number"]
    return []


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def primary_type(schema: Dict[str, Any], resolver: Optional[SchemaResolver] = None) -> Optional[str]:
    """Return the first non-null JSON type a schema accepts, or ``None`` if unconstrained."""
    resolver = resolver or SchemaResolver(schema)
    schema = _flatten(resolver.resolve(schema), resolver)
    types = [t for t in _types(schema) if t != "null"]
    return types[0] if types else None


def _flatten(schema: Any, resolver: SchemaResolver) -> Dict[str, Any]:
    """Collapse ``allOf`` and pick the first usable branch of ``anyOf``/``oneOf``."""
    if not isinstance(schema, dict):
        return {}
    schema = resolver.resolve(schema)
    out = {k: v for k, v in schema.items() if k not in ("allOf", "anyOf", "oneOf")}
    for sub in schema.get("allOf") or []:
        sub = _flatten(sub, resolver)
        for key, value in sub.items():
            if key == "properties":
                out.setdefault("properties", {}).update(value)
            elif key == "required":
                out["required"] = list(dict.fromkeys(list(out.get("required", [])) + list(value)))
            else:
                out.setdefault(key, value)
    for combinator in ("oneOf", "anyOf"):
        branches = schema.get(combinator) or []
        chosen = None
        for branch in branches:
            branch = _flatten(branch, resolver)
            if _types(branch) != ["null"]:
                chosen = branch
                break
        if chosen is None and branches:
            chosen = _flatten(branches[0], resolver)
        if chosen:
            for key, value in chosen.items():
                if key == "properties":
                    out.setdefault("properties", {}).update(value)
                elif key == "required":
                    out["required"] = list(dict.fromkeys(list(out.get("required", [])) + list(value)))
                else:
                    out.setdefault(key, value)
            break
    return out


def _string_sample(schema: Dict[str, Any], name: str) -> str:
    fmt = schema.get("format")
    min_len = int(schema.get("minLength", 0) or 0)
    max_len = schema.get("maxLength")
    pattern = schema.get("pattern")
    candidates: List[str] = []
    if isinstance(fmt, str) and fmt in _FORMAT_SAMPLES:
        candidates.append(_FORMAT_SAMPLES[fmt])
    base = re.sub(r"[^a-zA-Z0-9]", "", name).lower() or "value"
    candidates.append(f"example-{base}")
    candidates.extend(_PATTERN_CANDIDATES)

    def fits(value: str) -> bool:
        if len(value) < min_len:
            return False
        if isinstance(max_len, int) and len(value) > max_len:
            return False
        if isinstance(pattern, str):
            try:
                return re.search(pattern, value) is not None
            except re.error:
                return True
        return True

    for value in candidates:
        if fits(value):
            return value
    # Adjust length as a last resort.
    value = candidates[0]
    if len(value) < min_len:
        value = (value * (min_len // max(len(value), 1) + 1))[:min_len]
    if isinstance(max_len, int) and len(value) > max_len:
        value = value[: max(max_len, 0)]
    return value


def _number_sample(schema: Dict[str, Any], integer: bool) -> Any:
    lo = schema.get("minimum")
    hi = schema.get("maximum")
    ex_lo = schema.get("exclusiveMinimum")
    ex_hi = schema.get("exclusiveMaximum")
    # draft-04 boolean exclusives
    if ex_lo is True:
        ex_lo, lo = lo, None
    if ex_hi is True:
        ex_hi, hi = hi, None
    step = 1 if integer else 0.5
    if isinstance(ex_lo, (int, float)) and not isinstance(ex_lo, bool):
        lo = ex_lo + step if lo is None else max(lo, ex_lo + step)
    if isinstance(ex_hi, (int, float)) and not isinstance(ex_hi, bool):
        hi = ex_hi - step if hi is None else min(hi, ex_hi - step)
    if lo is not None:
        value = lo
    elif hi is not None:
        value = min(hi, 1)
    else:
        value = 1
    multiple = schema.get("multipleOf")
    if isinstance(multiple, (int, float)) and multiple > 0:
        k = -(-value // multiple)  # ceil
        value = k * multiple
        if hi is not None and value > hi:
            value = (hi // multiple) * multiple
    if integer:
        value = int(-(-value // 1)) if not float(value).is_integer() else int(value)
    elif isinstance(value, float) and value.is_integer():
        value = int(value)
    return value


def sample_value(
    schema: Any,
    name: str = "value",
    include_optional: bool = True,
    resolver: Optional[SchemaResolver] = None,
    _depth: int = 0,
) -> Any:
    """Return a value that satisfies *schema* (best effort)."""
    if schema is True or schema is None:
        return "example"
    if not isinstance(schema, dict):
        return "example"
    resolver = resolver or SchemaResolver(schema)
    schema = _flatten(schema, resolver)
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    if "default" in schema:
        return copy.deepcopy(schema["default"])
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return copy.deepcopy(examples[0])
    if "example" in schema:
        return copy.deepcopy(schema["example"])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        non_null = [e for e in enum if e is not None]
        return copy.deepcopy(non_null[0] if non_null else enum[0])

    types = [t for t in _types(schema) if t != "null"] or (["null"] if "null" in _types(schema) else [])
    kind = types[0] if types else "string"
    if _depth > 8:
        return {} if kind == "object" else [] if kind == "array" else None

    if kind == "string":
        return _string_sample(schema, name)
    if kind == "integer":
        return _number_sample(schema, integer=True)
    if kind == "number":
        return _number_sample(schema, integer=False)
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    if kind == "array":
        items = schema.get("items")
        if isinstance(items, list):  # draft-04 tuple form
            return [
                sample_value(s, name, include_optional, resolver, _depth + 1) for s in items
            ]
        prefix = schema.get("prefixItems")
        if isinstance(prefix, list) and prefix:
            return [sample_value(s, name, include_optional, resolver, _depth + 1) for s in prefix]
        count = max(int(schema.get("minItems", 1) or 1), 1)
        if isinstance(schema.get("maxItems"), int):
            count = min(count, schema["maxItems"])
        values = []
        for i in range(count):
            value = sample_value(items or {}, name, include_optional, resolver, _depth + 1)
            if schema.get("uniqueItems") and isinstance(value, (str, int, float)) and i:
                value = f"{value}-{i}" if isinstance(value, str) else value + i
            values.append(value)
        return values
    if kind == "object":
        return sample_object(schema, include_optional, resolver, _depth + 1)
    return "example"


def sample_object(
    schema: Dict[str, Any],
    include_optional: bool = True,
    resolver: Optional[SchemaResolver] = None,
    _depth: int = 0,
) -> Dict[str, Any]:
    resolver = resolver or SchemaResolver(schema)
    schema = _flatten(schema, resolver)
    props = schema.get("properties") or {}
    required = [r for r in schema.get("required") or [] if isinstance(r, str)]
    out: Dict[str, Any] = {}
    for key, sub in props.items():
        if key in required or include_optional:
            out[key] = sample_value(sub, key, include_optional, resolver, _depth)
    for key in required:
        if key not in out:
            out[key] = "example"
    return out


def wrong_type_value(schema: Any, resolver: Optional[SchemaResolver] = None) -> Tuple[Any, Optional[str]]:
    """Return ``(value, description)`` with a value violating the schema's type.

    Returns ``(_MISSING, None)`` when the schema does not constrain the type
    (every JSON value would be accepted), in which case no negative test can
    be generated.
    """
    if not isinstance(schema, dict):
        return _MISSING, None
    resolver = resolver or SchemaResolver(schema)
    flat = _flatten(schema, resolver)
    raw_types = _types(flat)
    if not raw_types and isinstance(flat.get("enum"), list):
        raw_types = list({_json_type(v) for v in flat["enum"]})
    if not raw_types:
        return _MISSING, None
    accepted = set(raw_types)
    if "number" in accepted:
        accepted.add("integer")
    candidates = [
        ("string", "not-a-valid-value"),
        ("boolean", True),
        ("integer", 12345),
        ("object", {"unexpected": True}),
        ("array", ["unexpected"]),
    ]
    # Prefer a value that is "obviously" different from the expected type.
    preference = {
        "string": ["integer", "boolean", "object"],
        "integer": ["string", "boolean"],
        "number": ["string", "boolean"],
        "boolean": ["string", "integer"],
        "array": ["string", "object"],
        "object": ["string", "array"],
        "null": ["string"],
    }
    lookup = dict(candidates)
    for wanted in preference.get(raw_types[0], []):
        if wanted not in accepted and not (wanted == "integer" and "number" in accepted):
            return lookup[wanted], f"{wanted} instead of {'/'.join(raw_types)}"
    for kind, value in candidates:
        if kind not in accepted:
            return value, f"{kind} instead of {'/'.join(raw_types)}"
    return _MISSING, None


def is_missing(value: Any) -> bool:
    return value is _MISSING


def sanitize_for_ajv(schema: Any) -> Any:
    """Return a copy of *schema* that Postman's embedded Ajv (draft-07) can compile.

    * removes ``$schema`` / ``$id`` declarations (Ajv only knows draft-07),
    * drops ``format`` values Ajv does not know (they would throw),
    * rewrites draft 2019/2020 ``$defs`` references so they keep working.
    """
    if isinstance(schema, list):
        return [sanitize_for_ajv(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("$schema", "$id", "id") and isinstance(value, str):
            continue
        if key == "format" and isinstance(value, str) and value not in AJV_FORMATS:
            continue
        if key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
            out[key] = (
                {k: sanitize_for_ajv(v) for k, v in value.items()} if isinstance(value, dict) else value
            )
        else:
            out[key] = sanitize_for_ajv(value)
    return out


def required_and_optional(schema: Any) -> Tuple[List[str], List[str], Dict[str, Any]]:
    """Return ``(required, optional, properties)`` for an object schema."""
    if not isinstance(schema, dict):
        return [], [], {}
    resolver = SchemaResolver(schema)
    flat = _flatten(schema, resolver)
    props = {k: v for k, v in (flat.get("properties") or {}).items()}
    required = [r for r in flat.get("required") or [] if isinstance(r, str)]
    optional = [k for k in props if k not in required]
    return required, optional, props
