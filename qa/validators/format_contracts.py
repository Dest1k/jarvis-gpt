"""Deterministic exact text, language, count, and JSON-contract checks."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from ..models import AssertionResult


def field_value(document: Mapping[str, Any], dotted_path: str, default: Any = None) -> Any:
    value: Any = document
    for part in dotted_path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return default
        value = value[part]
    return value


def validate_json_schema(instance: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Validate the small JSON Schema subset used by assurance fixtures."""

    errors: list[str] = []
    supported = {
        "$id",
        "$schema",
        "additionalProperties",
        "const",
        "description",
        "enum",
        "items",
        "maxItems",
        "maxLength",
        "minItems",
        "minLength",
        "properties",
        "required",
        "title",
        "type",
        "pattern",
    }
    for keyword in sorted(set(schema) - supported):
        errors.append(f"{path}: unsupported schema keyword {keyword!r}")
    expected_type = schema.get("type")
    type_checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, int | float) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if isinstance(expected_type, str | list):
        names = [expected_type] if isinstance(expected_type, str) else expected_type
        checkers = [type_checks.get(name) for name in names]
        if any(checker is None for checker in checkers):
            errors.append(f"{path}: unsupported schema type {expected_type!r}")
            return errors
        if not any(checker(instance) for checker in checkers if checker is not None):
            errors.append(f"{path}: expected {expected_type}")
            return errors
    elif expected_type is not None:
        errors.append(f"{path}: schema type must be a string or array")
        return errors
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: value does not match const")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: value is outside enum")
    if isinstance(instance, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in instance:
                    errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for key, child_schema in properties.items():
                if key in instance and isinstance(child_schema, Mapping):
                    errors.extend(
                        validate_json_schema(instance[key], child_schema, f"{path}.{key}")
                    )
        if schema.get("additionalProperties") is False and isinstance(properties, Mapping):
            extra = sorted(set(instance) - set(properties))
            for key in extra:
                errors.append(f"{path}: additional property {key!r}")
        elif isinstance(schema.get("additionalProperties"), Mapping):
            child_schema = schema["additionalProperties"]
            for key in sorted(set(instance) - set(properties)):
                errors.extend(validate_json_schema(instance[key], child_schema, f"{path}.{key}"))
    if isinstance(instance, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(instance) < min_items:
            errors.append(f"{path}: fewer than {min_items} items")
        if isinstance(max_items, int) and len(instance) > max_items:
            errors.append(f"{path}: more than {max_items} items")
        child_schema = schema.get("items")
        if isinstance(child_schema, Mapping):
            for index, item in enumerate(instance):
                errors.extend(validate_json_schema(item, child_schema, f"{path}[{index}]"))
    if isinstance(instance, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        pattern = schema.get("pattern")
        if isinstance(min_length, int) and len(instance) < min_length:
            errors.append(f"{path}: shorter than {min_length}")
        if isinstance(max_length, int) and len(instance) > max_length:
            errors.append(f"{path}: longer than {max_length}")
        if isinstance(pattern, str) and re.search(pattern, instance) is None:
            errors.append(f"{path}: does not match pattern")
    return errors


def validate_format_contract(
    observation: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[AssertionResult]:
    field = str(spec.get("field", "final"))
    actual = field_value(observation, field)
    text = actual if isinstance(actual, str) else ""
    assertions: list[AssertionResult] = []
    if "exact" in spec:
        expected = spec["exact"]
        assertions.append(AssertionResult("format.exact", actual == expected, expected, actual))
    if "fullmatch" in spec:
        pattern = str(spec["fullmatch"])
        assertions.append(
            AssertionResult(
                "format.fullmatch",
                re.fullmatch(pattern, text) is not None,
                pattern,
                text,
            )
        )
    language = spec.get("language")
    if language:
        cyrillic = bool(re.search(r"[А-Яа-яЁё]", text))
        latin = bool(re.search(r"[A-Za-z]", text))
        allow_mixed = bool(spec.get("allow_mixed_language", False))
        if language == "ru":
            passed = cyrillic and (allow_mixed or not latin)
        elif language == "en":
            passed = latin and (allow_mixed or not cyrillic)
        else:
            passed = False
        assertions.append(AssertionResult("format.language", passed, language, text))
    words = re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", text, flags=re.UNICODE)
    if "word_count" in spec:
        expected = int(spec["word_count"])
        assertions.append(
            AssertionResult("format.word_count", len(words) == expected, expected, len(words))
        )
    if "line_count" in spec:
        expected = int(spec["line_count"])
        actual_lines = len(text.splitlines())
        assertions.append(
            AssertionResult("format.line_count", actual_lines == expected, expected, actual_lines)
        )
    if spec.get("json") or "json_schema" in spec:
        try:
            parsed = json.loads(text)
            parse_error = ""
        except json.JSONDecodeError as exc:
            parsed = None
            parse_error = exc.msg
        assertions.append(
            AssertionResult(
                "format.valid_json", not parse_error, "valid JSON", parse_error or "valid"
            )
        )
        schema = spec.get("json_schema")
        if not parse_error and isinstance(schema, Mapping):
            schema_errors = validate_json_schema(parsed, schema)
            assertions.append(
                AssertionResult(
                    "format.json_schema",
                    not schema_errors,
                    "schema match",
                    schema_errors,
                )
            )
    if not assertions:
        assertions.append(
            AssertionResult(
                "format.contract_configured",
                False,
                "at least one deterministic format rule",
                sorted(spec),
            )
        )
    return assertions
