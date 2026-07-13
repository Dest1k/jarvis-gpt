"""Fail-closed redaction applied before any evidence is persisted."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

REDACTED = "[REDACTED]"
_SECRET_KEYS = {
    "api_key",
    "api_token",
    "authorization",
    "bearer",
    "credential",
    "credentials",
    "jarvis_api_token",
    "password",
    "secret",
    "token",
}
_ASSIGNMENT = re.compile(
    r"(?i)\b(jarvis_api_token|api[_-]?key|api[_-]?token|access[_-]?token|"
    r"authorization|password|secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_CANARY_SHAPE = re.compile(
    r"(?i)\bcanary[_:/.-](?:token|secret|credential)[_:/.-][^\s,;]+"
)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    value: Any
    events: tuple[str, ...]


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _is_secret_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return normalized in _SECRET_KEYS or normalized.endswith(
        ("_api_key", "_api_token", "_access_token", "_password", "_secret")
    )


def redact_text(text: str, canaries: Iterable[str] = ()) -> RedactionResult:
    events: list[str] = []
    output = text
    for canary in sorted({item for item in canaries if item}, key=len, reverse=True):
        if canary in output:
            output = output.replace(canary, REDACTED)
            events.append("explicit_canary")
    output, count = _BEARER.subn(f"Bearer {REDACTED}", output)
    events.extend("bearer_credential" for _ in range(count))

    def replace_assignment(match: re.Match[str]) -> str:
        events.append("credential_assignment")
        return f"{match.group(1)}{match.group(2)}{REDACTED}"

    output = _ASSIGNMENT.sub(replace_assignment, output)
    output, count = _CANARY_SHAPE.subn(REDACTED, output)
    events.extend("canary_shape" for _ in range(count))
    return RedactionResult(output, tuple(events))


def redact_value(value: Any, canaries: Iterable[str] = (), path: str = "$") -> RedactionResult:
    canary_values = tuple(item for item in canaries if item)
    if isinstance(value, str):
        result = redact_text(value, canary_values)
        return RedactionResult(result.value, tuple(f"{path}:{event}" for event in result.events))
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        events: list[str] = []
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            if (
                _is_secret_key(key_text)
                and item is not None
                and item != ""
                and item != REDACTED
            ):
                redacted[key_text] = REDACTED
                events.append(f"{child_path}:credential_key")
                continue
            child = redact_value(item, canary_values, child_path)
            redacted[key_text] = child.value
            events.extend(child.events)
        return RedactionResult(redacted, tuple(events))
    if isinstance(value, list | tuple):
        redacted_items: list[Any] = []
        events: list[str] = []
        for index, item in enumerate(value):
            child = redact_value(item, canary_values, f"{path}[{index}]")
            redacted_items.append(child.value)
            events.extend(child.events)
        return RedactionResult(redacted_items, tuple(events))
    return RedactionResult(value, ())


def credential_like_paths(value: Any, path: str = "$") -> tuple[str, ...]:
    """Return paths that still look secret-bearing after sanitation."""

    findings: list[str] = []
    if isinstance(value, str):
        if _BEARER.search(value) or _ASSIGNMENT.search(value) or _CANARY_SHAPE.search(value):
            findings.append(path)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if (
                _is_secret_key(str(key))
                and item is not None
                and item != ""
                and item != REDACTED
            ):
                findings.append(child_path)
            findings.extend(credential_like_paths(item, child_path))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            findings.extend(credential_like_paths(item, f"{path}[{index}]"))
    return tuple(findings)
