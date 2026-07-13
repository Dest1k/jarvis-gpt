"""Deterministic exact text, language, count, and strict JSON-contract checks."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from ..models import AssertionResult
from ..schema_validation import validate_json_schema

_FORMAT_FIELDS = frozenset(
    {
        "kind",
        "field",
        "exact",
        "fullmatch",
        "language",
        "allow_mixed_language",
        "word_count",
        "line_count",
        "json",
        "json_schema",
    }
)


def field_value(document: Mapping[str, Any], dotted_path: str, default: Any = None) -> Any:
    value: Any = document
    for part in dotted_path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return default
        value = value[part]
    return value


def _contract_errors(spec: Mapping[str, Any]) -> list[str]:
    errors = [f"unknown field {name!r}" for name in sorted(set(spec) - _FORMAT_FIELDS)]
    field = spec.get("field", "final")
    if not isinstance(field, str) or not field or any(not part for part in field.split(".")):
        errors.append("field must be a non-empty dotted string")
    if "fullmatch" in spec:
        pattern = spec["fullmatch"]
        if not isinstance(pattern, str):
            errors.append("fullmatch must be a string")
        else:
            try:
                re.compile(pattern)
            except re.error:
                errors.append("fullmatch is not a valid regular expression")
    if "language" in spec and spec["language"] not in {"ru", "en"}:
        errors.append("language must be ru or en")
    if "allow_mixed_language" in spec and not isinstance(spec["allow_mixed_language"], bool):
        errors.append("allow_mixed_language must be boolean")
    for name in ("word_count", "line_count"):
        if name in spec and (
            not isinstance(spec[name], int) or isinstance(spec[name], bool) or spec[name] < 0
        ):
            errors.append(f"{name} must be a non-negative integer")
    if "json" in spec and spec["json"] is not True:
        errors.append("json must be true when configured")
    if "json_schema" in spec and not isinstance(spec["json_schema"], Mapping):
        errors.append("json_schema must be an object")
    rules = {"exact", "fullmatch", "language", "word_count", "line_count", "json", "json_schema"}
    if not rules.intersection(spec):
        errors.append("at least one deterministic format rule is required")
    return errors


def validate_format_contract(
    observation: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[AssertionResult]:
    contract_errors = _contract_errors(spec)
    if contract_errors:
        return [
            AssertionResult(
                "format.contract_valid",
                False,
                "strict format validator contract",
                contract_errors,
            )
        ]
    field = spec.get("field", "final")
    actual = field_value(observation, field)
    text = actual if isinstance(actual, str) else ""
    assertions: list[AssertionResult] = []
    if "exact" in spec:
        expected = spec["exact"]
        assertions.append(AssertionResult("format.exact", actual == expected, expected, actual))
    if "fullmatch" in spec:
        pattern = spec["fullmatch"]
        assertions.append(
            AssertionResult(
                "format.fullmatch",
                re.fullmatch(pattern, text) is not None,
                pattern,
                text,
            )
        )
    language = spec.get("language")
    if language is not None:
        cyrillic = bool(re.search(r"[А-Яа-яЁё]", text))
        latin = bool(re.search(r"[A-Za-z]", text))
        allow_mixed = spec.get("allow_mixed_language", False)
        passed = cyrillic and (allow_mixed or not latin)
        if language == "en":
            passed = latin and (allow_mixed or not cyrillic)
        assertions.append(AssertionResult("format.language", passed, language, text))
    words = re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", text, flags=re.UNICODE)
    if "word_count" in spec:
        expected = spec["word_count"]
        assertions.append(
            AssertionResult("format.word_count", len(words) == expected, expected, len(words))
        )
    if "line_count" in spec:
        expected = spec["line_count"]
        actual_lines = len(text.splitlines())
        assertions.append(
            AssertionResult("format.line_count", actual_lines == expected, expected, actual_lines)
        )
    if spec.get("json") is True or "json_schema" in spec:
        try:
            parsed = json.loads(
                text,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON constant {value}")
                ),
            )
            parse_error = ""
        except (json.JSONDecodeError, ValueError) as exc:
            parsed = None
            parse_error = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
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
                    "strict schema match",
                    schema_errors,
                )
            )
    return assertions


__all__ = ["field_value", "validate_format_contract", "validate_json_schema"]
