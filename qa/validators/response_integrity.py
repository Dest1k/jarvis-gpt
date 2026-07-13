"""Detect empty, duplicated, truncated, and internal assistant output."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..models import AssertionResult
from .format_contracts import field_value

_INTERNAL_PATTERNS = {
    "call_marker": re.compile(r"(?i)(?:^|\s)call\s*:\s*\S+"),
    "tool_envelope": re.compile(
        r"(?is)[{[][^}\]]*(?:"
        r"\"(?:tool|function|tool_calls|function_call)\"\s*:|"
        r"\"name\"\s*:\s*\"[^\"]+\"[^}\]]*\"arguments\"\s*:"
        r")"
    ),
    "role_or_protocol": re.compile(
        r"(?im)(?:^\s*(?:system|developer|assistant|tool|function)\s*:|<\|(?:system|assistant|tool)[^>]*\|>)"
    ),
    "traceback": re.compile(r"(?im)^Traceback \(most recent call last\):"),
    "transport_frame": re.compile(r"(?im)^\s*(?:data|event)\s*:"),
    "internal_schema": re.compile(r"(?i)\b(?:internal[_ -]?schema|protocol[_ -]?envelope)\b"),
}


def _repeated_final(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if len(lines) >= 2 and len(lines) % 2 == 0:
        midpoint = len(lines) // 2
        if lines[:midpoint] == lines[midpoint:]:
            return True
    if len(normalized) % 2 == 0:
        midpoint = len(normalized) // 2
        return normalized[:midpoint].strip() == normalized[midpoint:].strip()
    return False


def validate_response_integrity(
    observation: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[AssertionResult]:
    field = str(spec.get("field", "final"))
    raw = field_value(observation, field, "")
    final = raw if isinstance(raw, str) else ""
    finals = observation.get("finals", [final])
    if not isinstance(finals, list):
        finals = [final]
    markers = [name for name, pattern in _INTERNAL_PATTERNS.items() if pattern.search(final)]
    duplicate = len(finals) != 1 or len(set(str(item) for item in finals)) != len(finals)
    duplicate = duplicate or _repeated_final(final)
    finish_reason = str(observation.get("finish_reason", "")).lower()
    truncated = bool(observation.get("truncated", False)) or finish_reason in {
        "length",
        "cancelled",
        "error",
    }
    if observation.get("stream_terminal") is False:
        truncated = True
    return [
        AssertionResult("response.non_empty_final", bool(final.strip()), "non-empty final", final),
        AssertionResult("response.no_internal_markers", not markers, [], markers),
        AssertionResult("response.single_final", not duplicate, "one unique final", len(finals)),
        AssertionResult(
            "response.not_truncated", not truncated, "terminal complete", finish_reason
        ),
    ]
