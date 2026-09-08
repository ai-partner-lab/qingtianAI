from __future__ import annotations

from datetime import datetime
from importlib.resources import files
import json
import math
from pathlib import Path
import re
from typing import Any

from .models import QingtianError, content_hash, require_strict_json


class ContractValidationError(QingtianError):
    """A document does not satisfy the supported JSON Schema contract subset."""


TYPE_CHECKS = {
    "null": lambda value: value is None,
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    ),
    "string": lambda value: isinstance(value, str),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}

SUPPORTED_KEYWORDS = {
    "$schema",
    "$id",
    "title",
    "description",
    "type",
    "const",
    "enum",
    "required",
    "properties",
    "additionalProperties",
    "items",
    "minItems",
    "minLength",
    "minimum",
    "pattern",
    "format",
    "default",
}

RFC3339_DATETIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)


def _json_equal(left: Any, right: Any) -> bool:
    # JSON booleans are not numbers, even though Python bool subclasses int.
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    return left == right


def _fail(path: str, message: str) -> None:
    raise ContractValidationError(f"{path}: {message}")


def _is_datetime(value: str) -> bool:
    if RFC3339_DATETIME.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _lint_schema(schema: Any, *, path: str = "$schema") -> None:
    if not isinstance(schema, dict):
        _fail(path, "schema must be an object")
    if path == "$schema":
        try:
            require_strict_json(schema, path=path)
        except (TypeError, ValueError) as exc:
            _fail(path, str(exc))
    unknown = set(schema) - SUPPORTED_KEYWORDS
    if unknown:
        _fail(path, f"unsupported schema keywords: {sorted(unknown)}")

    dialect = schema.get("$schema")
    if dialect is not None and dialect != "https://json-schema.org/draft/2020-12/schema":
        _fail(path, "unsupported $schema dialect")
    for keyword in ("$id", "title", "description"):
        if keyword in schema and (
            not isinstance(schema[keyword], str) or not schema[keyword]
        ):
            _fail(path, f"{keyword} must be a non-empty string")

    if "type" in schema:
        declared = schema["type"]
        names = [declared] if isinstance(declared, str) else declared
        if (
            not isinstance(names, list)
            or not names
            or any(not isinstance(name, str) or name not in TYPE_CHECKS for name in names)
            or len(names) != len(set(names))
        ):
            _fail(path, "type must declare one or more unique supported JSON types")

    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or not choices:
            _fail(path, "enum must be a non-empty array")
        for index, choice in enumerate(choices):
            if any(_json_equal(choice, previous) for previous in choices[:index]):
                _fail(path, "enum values must be unique")

    required = schema.get("required")
    if "required" in schema and (
        not isinstance(required, list)
        or any(not isinstance(name, str) or not name for name in required)
        or len(required) != len(set(required))
    ):
        _fail(path, "required must be an array of unique non-empty strings")

    properties = schema.get("properties")
    if "properties" in schema:
        if not isinstance(properties, dict):
            _fail(path, "properties must be an object")
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                _fail(path, "property names must be non-empty strings")
            _lint_schema(child, path=f"{path}.properties.{name}")

    if "additionalProperties" in schema and not isinstance(
        schema["additionalProperties"], bool
    ):
        _fail(path, "additionalProperties must be a boolean")

    if "items" in schema:
        if not isinstance(schema["items"], dict):
            _fail(path, "items must be a schema object")
        _lint_schema(schema["items"], path=f"{path}.items")

    for keyword in ("minItems", "minLength"):
        value = schema.get(keyword)
        if keyword in schema and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            _fail(path, f"{keyword} must be a non-negative integer")

    if "minimum" in schema:
        minimum = schema["minimum"]
        if (
            not isinstance(minimum, (int, float))
            or isinstance(minimum, bool)
            or (isinstance(minimum, float) and not math.isfinite(minimum))
        ):
            _fail(path, "minimum must be a finite number")

    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str):
            _fail(path, "pattern must be a string")
        try:
            re.compile(pattern)
        except re.error as exc:
            _fail(path, f"pattern is not a valid regular expression: {exc}")

    if "format" in schema and schema["format"] != "date-time":
        _fail(path, "format must be 'date-time'")


def _validate_semantics(instance: Any, schema: dict[str, Any]) -> None:
    if not isinstance(instance, dict):
        return
    schema_id = schema.get("$id")
    if schema_id == "urn:qingtian-ai:schema:checkpoint:v1":
        if "snapshot" not in instance or "content_hash" not in instance:
            _fail("$", "checkpoint hash validation requires snapshot and content_hash")
        if content_hash(instance["snapshot"]) != instance["content_hash"]:
            _fail("$.content_hash", "checkpoint snapshot hash mismatch")
    elif schema_id == "urn:qingtian-ai:schema:verification-receipt:v1":
        if "receipt_hash" not in instance:
            _fail("$", "receipt hash validation requires receipt_hash")
        unsigned = {key: value for key, value in instance.items() if key != "receipt_hash"}
        if content_hash(unsigned) != instance["receipt_hash"]:
            _fail("$.receipt_hash", "verification receipt hash mismatch")
    elif schema_id == "urn:qingtian-ai:schema:evidence:v1":
        subject_type = instance.get("subject_type")
        subject_id = instance.get("subject_id")
        if (
            isinstance(subject_type, str)
            and isinstance(subject_id, str)
            and re.fullmatch(
                rf"{re.escape(subject_type)}_[A-Za-z0-9_-]+", subject_id
            )
            is None
        ):
            _fail("$.subject_id", "evidence subject ID does not match subject_type")


def validate(instance: Any, schema: dict[str, Any], *, path: str = "$") -> None:
    """Validate the explicit keyword subset used by this release's schemas.

    This is intentionally not advertised as a general JSON Schema implementation.
    Unknown validation keywords fail closed so future contracts cannot silently skip
    rules that the portable runtime does not understand.
    """

    if path == "$":
        _lint_schema(schema)
        try:
            require_strict_json(instance, path=path)
        except (TypeError, ValueError) as exc:
            _fail(path, str(exc))

    unknown = set(schema) - SUPPORTED_KEYWORDS
    if unknown:
        _fail(path, f"unsupported schema keywords: {sorted(unknown)}")

    declared_types = schema.get("type")
    if declared_types is not None:
        type_names = [declared_types] if isinstance(declared_types, str) else declared_types
        if (
            not isinstance(type_names, list)
            or not type_names
            or any(name not in TYPE_CHECKS for name in type_names)
        ):
            _fail(path, "schema declares an unsupported type")
        if not any(TYPE_CHECKS[name](instance) for name in type_names):
            _fail(path, f"expected type {type_names}, got {type(instance).__name__}")

    if "const" in schema and not _json_equal(instance, schema["const"]):
        _fail(path, f"expected constant {schema['const']!r}")
    if "enum" in schema and not any(_json_equal(instance, choice) for choice in schema["enum"]):
        _fail(path, f"value is outside enum {schema['enum']!r}")

    if isinstance(instance, str):
        if len(instance) < int(schema.get("minLength", 0)):
            _fail(path, f"string is shorter than {schema['minLength']}")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            _fail(path, f"string does not match pattern {schema['pattern']!r}")
        if "format" in schema:
            if schema["format"] != "date-time":
                _fail(path, f"unsupported schema format: {schema['format']!r}")
            if not _is_datetime(instance):
                _fail(path, "value is not an offset-aware RFC 3339 date-time")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            _fail(path, f"number is below minimum {schema['minimum']}")

    if isinstance(instance, list):
        if len(instance) < int(schema.get("minItems", 0)):
            _fail(path, f"array contains fewer than {schema['minItems']} items")
        if "items" in schema:
            for index, item in enumerate(instance):
                validate(item, schema["items"], path=f"{path}[{index}]")

    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in instance]
        if missing:
            _fail(path, f"missing required properties: {missing}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(instance) - set(properties))
            if extra:
                _fail(path, f"unexpected properties: {extra}")
        for name, value in instance.items():
            if name in properties:
                validate(value, properties[name], path=f"{path}.{name}")

    if path == "$":
        _validate_semantics(instance, schema)


def validate_files(schema_path: str | Path, document_path: str | Path) -> dict[str, Any]:
    schema_file = Path(schema_path).resolve()
    document_file = Path(document_path).resolve()
    schema = json.loads(schema_file.read_text(encoding="utf-8"))
    document = json.loads(document_file.read_text(encoding="utf-8"))
    validate(document, schema)
    return {
        "status": "valid",
        "schema": schema_file.name,
        "schema_id": schema.get("$id"),
        "schema_hash": content_hash(schema),
        "document": document_file.name,
        "document_hash": content_hash(document),
    }


def bundled_schema(name: str) -> dict[str, Any]:
    if re.fullmatch(r"[a-z][a-z0-9-]*", name) is None:
        raise ContractValidationError(f"invalid bundled schema name: {name!r}")
    filename = f"{name}.schema.json"
    resource = files("qingtian_core.resources.schemas").joinpath(filename)
    try:
        return json.loads(resource.read_text(encoding="utf-8"))
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise ContractValidationError(f"bundled schema is unavailable: {filename}") from exc
