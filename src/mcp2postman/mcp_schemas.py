"""Draft-07 JSON schemas for the MCP results the generated tests validate.

They are a pragmatic subset of the official MCP schema: strict on the fields
the specification marks as required, permissive (``additionalProperties``
allowed) everywhere else so that servers implementing newer revisions still
pass.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

_ANNOTATED = {"annotations": {"type": "object"}, "_meta": {"type": "object"}}

CONTENT_BLOCK: Dict[str, Any] = {
    "type": "object",
    "required": ["type"],
    "properties": {
        "type": {
            "type": "string",
            "enum": ["text", "image", "audio", "resource_link", "resource"],
        },
        **_ANNOTATED,
    },
    "allOf": [
        {
            "if": {"properties": {"type": {"const": "text"}}},
            "then": {"required": ["text"], "properties": {"text": {"type": "string"}}},
        },
        {
            "if": {"properties": {"type": {"enum": ["image", "audio"]}}},
            "then": {
                "required": ["data", "mimeType"],
                "properties": {"data": {"type": "string"}, "mimeType": {"type": "string"}},
            },
        },
        {
            "if": {"properties": {"type": {"const": "resource_link"}}},
            "then": {
                "required": ["uri", "name"],
                "properties": {"uri": {"type": "string"}, "name": {"type": "string"}},
            },
        },
        {
            "if": {"properties": {"type": {"const": "resource"}}},
            "then": {
                "required": ["resource"],
                "properties": {
                    "resource": {
                        "type": "object",
                        "required": ["uri"],
                        "properties": {"uri": {"type": "string"}},
                    }
                },
            },
        },
    ],
}

INITIALIZE_RESULT: Dict[str, Any] = {
    "type": "object",
    "required": ["protocolVersion", "capabilities", "serverInfo"],
    "properties": {
        "protocolVersion": {"type": "string", "minLength": 1},
        "capabilities": {"type": "object"},
        "serverInfo": {
            "type": "object",
            "required": ["name", "version"],
            "properties": {"name": {"type": "string"}, "version": {"type": "string"}},
        },
        "instructions": {"type": "string"},
    },
}

TOOL: Dict[str, Any] = {
    "type": "object",
    "required": ["name", "inputSchema"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "inputSchema": {"type": "object"},
        "outputSchema": {"type": "object"},
        **_ANNOTATED,
    },
}

LIST_TOOLS_RESULT: Dict[str, Any] = {
    "type": "object",
    "required": ["tools"],
    "properties": {"tools": {"type": "array", "items": TOOL}, "nextCursor": {"type": "string"}},
}

CALL_TOOL_RESULT: Dict[str, Any] = {
    "type": "object",
    "required": ["content"],
    "properties": {
        "content": {"type": "array", "items": CONTENT_BLOCK},
        "structuredContent": {"type": "object"},
        "isError": {"type": "boolean"},
        "_meta": {"type": "object"},
    },
}

RESOURCE: Dict[str, Any] = {
    "type": "object",
    "required": ["uri", "name"],
    "properties": {
        "uri": {"type": "string"},
        "name": {"type": "string"},
        "mimeType": {"type": "string"},
        **_ANNOTATED,
    },
}

LIST_RESOURCES_RESULT: Dict[str, Any] = {
    "type": "object",
    "required": ["resources"],
    "properties": {"resources": {"type": "array", "items": RESOURCE}, "nextCursor": {"type": "string"}},
}

LIST_RESOURCE_TEMPLATES_RESULT: Dict[str, Any] = {
    "type": "object",
    "required": ["resourceTemplates"],
    "properties": {
        "resourceTemplates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["uriTemplate", "name"],
                "properties": {"uriTemplate": {"type": "string"}, "name": {"type": "string"}},
            },
        },
        "nextCursor": {"type": "string"},
    },
}

READ_RESOURCE_RESULT: Dict[str, Any] = {
    "type": "object",
    "required": ["contents"],
    "properties": {
        "contents": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["uri"],
                "properties": {
                    "uri": {"type": "string"},
                    "mimeType": {"type": "string"},
                    "text": {"type": "string"},
                    "blob": {"type": "string"},
                },
                "anyOf": [{"required": ["text"]}, {"required": ["blob"]}],
            },
        }
    },
}

PROMPT: Dict[str, Any] = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "arguments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}, "required": {"type": "boolean"}},
            },
        },
    },
}

LIST_PROMPTS_RESULT: Dict[str, Any] = {
    "type": "object",
    "required": ["prompts"],
    "properties": {"prompts": {"type": "array", "items": PROMPT}, "nextCursor": {"type": "string"}},
}

GET_PROMPT_RESULT: Dict[str, Any] = {
    "type": "object",
    "required": ["messages"],
    "properties": {
        "description": {"type": "string"},
        "messages": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["role", "content"],
                "properties": {
                    "role": {"type": "string", "enum": ["user", "assistant"]},
                    "content": CONTENT_BLOCK,
                },
            },
        },
    },
}

EMPTY_RESULT: Dict[str, Any] = {"type": "object"}

JSONRPC_ERROR: Dict[str, Any] = {
    "type": "object",
    "required": ["code", "message"],
    "properties": {"code": {"type": "integer"}, "message": {"type": "string"}},
}


def call_tool_result(output_schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``CallToolResult`` schema, optionally requiring ``structuredContent``."""
    schema = copy.deepcopy(CALL_TOOL_RESULT)
    if output_schema:
        schema["required"] = ["content", "structuredContent"]
    return schema
