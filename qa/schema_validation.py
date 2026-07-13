"""Strict standard-library JSON Schema subset used by assurance contracts."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

_KEYWORDS = frozenset(
    {
        "$defs",
        "$id",
        "$ref",
        "$schema",
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "description",
        "else",
        "enum",
        "format",
        "if",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "not",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "then",
        "title",
        "type",
        "uniqueItems",
    }
)
_TYPE_NAMES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_FORMATS = frozenset({"date-time", "uri"})


def _valid_json_value(value: Any, depth: int = 0) -> bool:
    if depth > 64:
        return False
    if value is None or isinstance(value, bool | str | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_valid_json_value(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _valid_json_value(item, depth + 1)
            for key, item in value.items()
        )
    return False


def _valid_absolute_uri(value: object) -> bool:
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        return False
    try:
        return bool(urlsplit(value).scheme)
    except ValueError:
        return False


def _schema_shape_errors(schema: Any, path: str = "$schema") -> list[str]:
    if not isinstance(schema, Mapping):
        return [f"{path}: schema must be an object"]
    errors: list[str] = []
    for keyword in sorted(set(schema) - _KEYWORDS):
        errors.append(f"{path}: unsupported schema keyword {keyword!r}")
    for keyword in ("title", "description"):
        if keyword in schema and not isinstance(schema[keyword], str):
            errors.append(f"{path}.{keyword}: expected a string")
    for keyword in ("$id", "$schema"):
        if keyword in schema and not _valid_absolute_uri(schema[keyword]):
            errors.append(f"{path}.{keyword}: expected an absolute URI")
    expected_type = schema.get("type")
    valid_type_names: list[str] = []
    if expected_type is not None:
        names = [expected_type] if isinstance(expected_type, str) else expected_type
        if (
            not isinstance(names, list)
            or not names
            or any(not isinstance(name, str) or name not in _TYPE_NAMES for name in names)
            or len(names) != len(set(names))
        ):
            errors.append(f"{path}.type: expected a known type or unique non-empty type array")
        else:
            valid_type_names = names
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list)
        or any(not isinstance(item, str) or not item for item in required)
        or len(required) != len(set(required))
    ):
        errors.append(f"{path}.required: expected a unique string array")
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        errors.append(f"{path}.enum: expected a non-empty array")
    elif isinstance(enum, list):
        if not all(_valid_json_value(item) for item in enum):
            errors.append(f"{path}.enum: values must be finite JSON values")
        if any(
            _strict_equal(left, right)
            for index, left in enumerate(enum)
            for right in enum[index + 1 :]
        ):
            errors.append(f"{path}.enum: values must be unique")
        if valid_type_names and any(
            not any(_type_matches(item, name) for name in valid_type_names) for item in enum
        ):
            errors.append(f"{path}.enum: values conflict with declared type")
    if "const" in schema:
        constant = schema["const"]
        if not _valid_json_value(constant):
            errors.append(f"{path}.const: expected a finite JSON value")
        elif valid_type_names and not any(
            _type_matches(constant, name) for name in valid_type_names
        ):
            errors.append(f"{path}.const: value conflicts with declared type")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            errors.append(f"{path}.properties: expected an object")
        else:
            for name, child in properties.items():
                if not isinstance(name, str):
                    errors.append(f"{path}.properties: property names must be strings")
                errors.extend(_schema_shape_errors(child, f"{path}.properties.{name}"))
    definitions = schema.get("$defs")
    if definitions is not None:
        if not isinstance(definitions, Mapping):
            errors.append(f"{path}.$defs: expected an object")
        else:
            for name, child in definitions.items():
                if not isinstance(name, str) or not name:
                    errors.append(f"{path}.$defs: definition names must be non-empty strings")
                errors.extend(_schema_shape_errors(child, f"{path}.$defs.{name}"))
    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool | Mapping):
        errors.append(f"{path}.additionalProperties: expected boolean or schema")
    elif isinstance(additional, Mapping):
        errors.extend(_schema_shape_errors(additional, f"{path}.additionalProperties"))
    items = schema.get("items")
    if items is not None and not isinstance(items, bool | Mapping):
        errors.append(f"{path}.items: expected boolean or schema")
    elif isinstance(items, Mapping):
        errors.extend(_schema_shape_errors(items, f"{path}.items"))
    for keyword in ("minItems", "maxItems", "minLength", "maxLength"):
        value = schema.get(keyword)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            errors.append(f"{path}.{keyword}: expected a non-negative integer")
    for keyword in ("minimum", "maximum"):
        value = schema.get(keyword)
        if value is not None and (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            errors.append(f"{path}.{keyword}: expected a finite number")
    for minimum, maximum in (("minItems", "maxItems"), ("minLength", "maxLength")):
        if minimum in schema and maximum in schema:
            low, high = schema[minimum], schema[maximum]
            if (
                isinstance(low, int)
                and isinstance(high, int)
                and not isinstance(low, bool)
                and low > high
            ):
                errors.append(f"{path}: {minimum} exceeds {maximum}")
    if "minimum" in schema and "maximum" in schema:
        low, high = schema["minimum"], schema["maximum"]
        if (
            isinstance(low, int | float)
            and isinstance(high, int | float)
            and not isinstance(low, bool)
            and low > high
        ):
            errors.append(f"{path}: minimum exceeds maximum")
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            errors.append(f"{path}.pattern: expected a string")
        else:
            try:
                re.compile(pattern)
            except re.error:
                errors.append(f"{path}.pattern: invalid regular expression")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        errors.append(f"{path}.uniqueItems: expected a boolean")
    for keyword in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(keyword)
        if branches is not None:
            if not isinstance(branches, list) or not branches:
                errors.append(f"{path}.{keyword}: expected a non-empty schema array")
            else:
                for index, child in enumerate(branches):
                    errors.extend(_schema_shape_errors(child, f"{path}.{keyword}[{index}]"))
    for keyword in ("not", "if", "then", "else"):
        child = schema.get(keyword)
        if child is not None:
            errors.extend(_schema_shape_errors(child, f"{path}.{keyword}"))
    if ("then" in schema or "else" in schema) and "if" not in schema:
        errors.append(f"{path}: then/else requires if")
    reference = schema.get("$ref")
    if reference is not None and (
        not isinstance(reference, str) or not reference.startswith("#/")
    ):
        errors.append(f"{path}.$ref: only local JSON pointers are supported")
    value_format = schema.get("format")
    if value_format is not None and (
        not isinstance(value_format, str) or value_format not in _FORMATS
    ):
        errors.append(f"{path}.format: unsupported format")
    keyword_domains = {
        "object": {"properties", "required", "additionalProperties"},
        "array": {"items", "minItems", "maxItems", "uniqueItems"},
        "string": {"minLength", "maxLength", "pattern", "format"},
        "number": {"minimum", "maximum"},
    }
    if valid_type_names:
        declared = set(valid_type_names)
        for domain, keywords in keyword_domains.items():
            applicable = domain in declared or (domain == "number" and "integer" in declared)
            if not applicable:
                for keyword in sorted(keywords.intersection(schema)):
                    errors.append(f"{path}.{keyword}: keyword conflicts with declared type")
    return errors


def _resolve_reference(root: Mapping[str, Any], reference: str) -> Mapping[str, Any] | None:
    value: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value if isinstance(value, Mapping) else None


def _strict_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _strict_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _strict_equal(left[key], right[key]) for key in left
        )
    return type(left) is type(right) and left == right


def _type_matches(instance: Any, name: str) -> bool:
    return {
        "object": lambda value: isinstance(value, dict),
        "array": lambda value: isinstance(value, list),
        "string": lambda value: isinstance(value, str),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda value: isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value),
        "boolean": lambda value: isinstance(value, bool),
        "null": lambda value: value is None,
    }[name](instance)


def _evaluate(
    instance: Any,
    schema: Mapping[str, Any],
    root: Mapping[str, Any],
    path: str,
    depth: int,
) -> list[str]:
    if depth > 64:
        return [f"{path}: schema recursion limit exceeded"]
    errors: list[str] = []
    reference = schema.get("$ref")
    if isinstance(reference, str):
        resolved = _resolve_reference(root, reference)
        if resolved is None:
            return [f"{path}: unresolved schema reference"]
        errors.extend(_evaluate(instance, resolved, root, path, depth + 1))
    expected_type = schema.get("type")
    if expected_type is not None:
        names = [expected_type] if isinstance(expected_type, str) else expected_type
        if not any(_type_matches(instance, name) for name in names):
            return [f"{path}: expected {expected_type}"]
    if "const" in schema and not _strict_equal(instance, schema["const"]):
        errors.append(f"{path}: value does not match const")
    if "enum" in schema and not any(_strict_equal(instance, item) for item in schema["enum"]):
        errors.append(f"{path}: value is outside enum")
    if isinstance(instance, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        for key, child in properties.items():
            if key in instance:
                errors.extend(_evaluate(instance[key], child, root, f"{path}.{key}", depth + 1))
        extras = sorted(set(instance) - set(properties))
        additional = schema.get("additionalProperties", True)
        if additional is False:
            errors.extend(f"{path}: additional property {key!r}" for key in extras)
        elif isinstance(additional, Mapping):
            for key in extras:
                errors.extend(
                    _evaluate(instance[key], additional, root, f"{path}.{key}", depth + 1)
                )
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if schema.get("uniqueItems") is True and any(
            _strict_equal(left, right)
            for index, left in enumerate(instance)
            for right in instance[index + 1 :]
        ):
            errors.append(f"{path}: array items are not unique")
        items = schema.get("items")
        if items is False and instance:
            errors.append(f"{path}: array items are forbidden")
        elif isinstance(items, Mapping):
            for index, item in enumerate(instance):
                errors.extend(_evaluate(item, items, root, f"{path}[{index}]", depth + 1))
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append(f"{path}: does not match pattern")
        if schema.get("format") == "date-time":
            candidate = instance[:-1] + "+00:00" if instance.endswith("Z") else instance
            try:
                parsed = datetime.fromisoformat(candidate)
                valid = parsed.tzinfo is not None
            except ValueError:
                valid = False
            if not valid:
                errors.append(f"{path}: invalid date-time")
        elif schema.get("format") == "uri":
            parsed = urlsplit(instance)
            if not parsed.scheme:
                errors.append(f"{path}: invalid URI")
    if isinstance(instance, int | float) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above maximum")
    for keyword in ("allOf",):
        for child in schema.get(keyword, []):
            errors.extend(_evaluate(instance, child, root, path, depth + 1))
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if branches:
            matches = sum(
                not _evaluate(instance, child, root, path, depth + 1)
                for child in branches
            )
            required_matches = 1 if keyword == "oneOf" else None
            if (required_matches is not None and matches != required_matches) or (
                keyword == "anyOf" and matches == 0
            ):
                errors.append(f"{path}: {keyword} match count is {matches}")
    if "not" in schema and not _evaluate(instance, schema["not"], root, path, depth + 1):
        errors.append(f"{path}: forbidden schema matched")
    conditional = schema.get("if")
    if isinstance(conditional, Mapping):
        condition_matches = not _evaluate(instance, conditional, root, path, depth + 1)
        branch = schema.get("then") if condition_matches else schema.get("else")
        if isinstance(branch, Mapping):
            errors.extend(_evaluate(instance, branch, root, path, depth + 1))
    return errors


def validate_json_schema(instance: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Validate schema structure first, then evaluate without type coercion."""

    shape_errors = _schema_shape_errors(schema)
    if shape_errors:
        return shape_errors
    return _evaluate(instance, schema, schema, path, 0)
