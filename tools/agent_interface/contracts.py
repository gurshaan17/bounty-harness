"""Small strict JSON-schema subset shared by CLI validation and published schemas."""

import hashlib
import json
import re
from pathlib import Path

VERSION = "1.0"
MAX_MESSAGE = 65536
SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


class InterfaceError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InterfaceError("invalid_input", "Duplicate JSON key")
            result[key] = value
        return result

    if len(raw) > MAX_MESSAGE:
        raise InterfaceError("invalid_input", "Message exceeds 64 KiB")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        bound(value)
        return value
    except (ValueError, RecursionError, UnicodeError):
        raise InterfaceError("invalid_input", "Malformed or excessively nested JSON") from None


def bound(value, depth=0):
    if depth > 12:
        raise InterfaceError("invalid_input", "Maximum nesting exceeded")
    if isinstance(value, str) and len(value) > 8192:
        raise InterfaceError("invalid_input", "String exceeds limit")
    if isinstance(value, dict):
        for key, item in value.items():
            bound(key, depth + 1)
            bound(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            bound(item, depth + 1)


def validate(value, schema):
    """Reject unsupported schema constructs rather than silently ignoring them."""
    supported = {
        "$schema",
        "$id",
        "title",
        "description",
        "type",
        "enum",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "default",
    }
    if schema.keys() - supported:
        raise InterfaceError("unsupported_capability", "Unsupported schema constraint")
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind] if kind else []
    if kinds and type(value) not in [types[k] for k in kinds]:
        raise InterfaceError("invalid_input", "Incorrect field type")
    if "enum" in schema and value not in schema["enum"]:
        raise InterfaceError("invalid_input", "Unsupported field value")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - value.keys():
            raise InterfaceError("invalid_input", "Required field missing")
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            raise InterfaceError("invalid_input", "Unknown field")
        for key, item in value.items():
            if key in props:
                validate(item, props[key])
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", 10000):
            raise InterfaceError("invalid_input", "Too many items")
        for item in value:
            validate(item, schema.get("items", {}))
    if isinstance(value, str):
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 8192):
            raise InterfaceError("invalid_input", "Invalid string length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise InterfaceError("invalid_input", "Invalid field format")
    if type(value) is int and value < schema.get("minimum", -(2**63)):
        raise InterfaceError("invalid_input", "Integer below minimum")


def validate_named(value, name):
    bound(value)
    if isinstance(value, dict) and value.get("schema_version") != VERSION:
        raise InterfaceError("unsupported_version", "Supported protocol: 1.0")
    validate(value, json.loads((SCHEMAS / f"agent-{name}.schema.json").read_text()))
