import re

from mcp2rest2postman.schema_values import (
    is_missing,
    primary_type,
    required_and_optional,
    sample_object,
    sample_value,
    sanitize_for_ajv,
    wrong_type_value,
)


def test_const_default_examples_enum_take_precedence():
    assert sample_value({"type": "string", "const": "x"}) == "x"
    assert sample_value({"type": "integer", "default": 7}) == 7
    assert sample_value({"type": "string", "examples": ["ex"]}) == "ex"
    assert sample_value({"type": "string", "enum": [None, "b"]}) == "b"


def test_string_formats_and_lengths():
    assert sample_value({"type": "string", "format": "email"}) == "user@example.com"
    assert sample_value({"type": "string", "format": "uuid"}).count("-") == 4
    assert len(sample_value({"type": "string", "minLength": 30})) >= 30
    assert len(sample_value({"type": "string", "maxLength": 3})) <= 3


def test_string_pattern_is_respected_when_possible():
    value = sample_value({"type": "string", "pattern": "^[0-9]+$"})
    assert re.search("^[0-9]+$", value)
    value = sample_value({"type": "string", "pattern": "^[A-Z]{2}[0-9]{2}$"})
    assert value == "AB12"


def test_numbers_respect_bounds():
    assert sample_value({"type": "integer", "minimum": 5}) == 5
    assert sample_value({"type": "integer", "exclusiveMinimum": 5}) == 6
    assert sample_value({"type": "number", "maximum": -3}) == -3
    assert sample_value({"type": "integer", "minimum": 1, "multipleOf": 4}) == 4
    assert isinstance(sample_value({"type": "integer"}), int)


def test_arrays_and_nested_objects():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}, "minItems": 2},
            "nested": {
                "type": "object",
                "properties": {"x": {"type": "boolean"}, "y": {"type": "number"}},
                "required": ["x"],
            },
        },
        "required": ["tags"],
    }
    full = sample_object(schema, include_optional=True)
    assert len(full["tags"]) == 2 and full["nested"] == {"x": True, "y": 1}
    assert sample_object(schema, include_optional=False) == {"tags": full["tags"]}


def test_refs_and_combinators():
    schema = {
        "type": "object",
        "$defs": {"Color": {"type": "string", "enum": ["red", "blue"]}},
        "properties": {
            "color": {"$ref": "#/$defs/Color"},
            "maybe": {"anyOf": [{"type": "null"}, {"type": "integer", "minimum": 3}]},
            "both": {"allOf": [{"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}]},
        },
        "required": ["color", "maybe", "both"],
    }
    value = sample_object(schema)
    assert value["color"] == "red"
    assert value["maybe"] == 3
    assert value["both"] == {"a": "example-a"}


def test_required_and_optional_split():
    req, opt, props = required_and_optional(
        {"type": "object", "properties": {"a": {}, "b": {}, "c": {}}, "required": ["b"]}
    )
    assert req == ["b"] and opt == ["a", "c"] and set(props) == {"a", "b", "c"}


def test_wrong_type_values():
    value, why = wrong_type_value({"type": "string"})
    assert not isinstance(value, str) and "instead of string" in why
    value, _ = wrong_type_value({"type": "number"})
    assert isinstance(value, str)
    value, _ = wrong_type_value({"type": ["integer", "null"]})
    assert isinstance(value, str)
    value, _ = wrong_type_value({"type": "array"})
    assert not isinstance(value, list)
    value, _ = wrong_type_value({})
    assert is_missing(value)


def test_primary_type():
    assert primary_type({"type": ["null", "boolean"]}) == "boolean"
    assert primary_type({"properties": {}}) == "object"
    assert primary_type({}) is None


def test_sanitize_for_ajv():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "a": {"type": "string", "format": "email"},
            "b": {"type": "string", "format": "made-up"},
            "$schema": {"type": "string"},
        },
    }
    clean = sanitize_for_ajv(schema)
    assert "$schema" not in clean
    assert clean["properties"]["a"]["format"] == "email"
    assert "format" not in clean["properties"]["b"]
    # a *property* literally named "$schema" must survive
    assert "$schema" in clean["properties"]
