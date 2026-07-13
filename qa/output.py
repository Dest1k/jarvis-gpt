"""Single fail-closed boundary for generated JSON output."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redaction import RedactionResult, credential_like_paths, redact_value


class UnsafeOutputError(ValueError):
    """Raised before output when credential-like material remains after redaction."""


@dataclass(frozen=True, slots=True)
class OutputLimits:
    max_depth: int = 10
    max_items: int = 200
    max_string_length: int = 20_000

    def __post_init__(self) -> None:
        if self.max_depth < 1 or self.max_items < 1 or self.max_string_length < 1:
            raise ValueError("output limits must be positive")


DEFAULT_OUTPUT_LIMITS = OutputLimits()


def bound_value(value: Any, limits: OutputLimits = DEFAULT_OUTPUT_LIMITS, depth: int = 0) -> Any:
    """Bound an already-redacted value without inspecting a truncated secret suffix."""

    if depth >= limits.max_depth:
        return "[MAX_DEPTH]"
    if isinstance(value, str):
        if len(value) <= limits.max_string_length:
            return value
        return value[: limits.max_string_length] + "[TRUNCATED]"
    if isinstance(value, Mapping):
        bounded: dict[str, Any] = {}
        for key, item in list(value.items())[: limits.max_items]:
            key_text = str(key)
            if key_text in bounded:
                raise ValueError("mapping keys collide after string conversion")
            bounded[key_text] = bound_value(item, limits, depth + 1)
        return bounded
    if isinstance(value, list | tuple):
        return [bound_value(item, limits, depth + 1) for item in value[: limits.max_items]]
    return value


def _materialize_canaries(canaries: Iterable[str]) -> tuple[str, ...]:
    return tuple(canaries)


def _sanitize_output(
    value: Any,
    canaries: tuple[str, ...],
    limits: OutputLimits,
) -> RedactionResult:
    redacted = redact_value(value, canaries)
    bounded = bound_value(redacted.value, limits)
    findings = credential_like_paths(bounded, canaries=canaries)
    if findings:
        raise UnsafeOutputError(
            f"unsafe output rejected: {len(findings)} credential-like location(s)"
        )
    return RedactionResult(bounded, redacted.events)


def sanitize_output(
    value: Any,
    *,
    canaries: Iterable[str] = (),
    limits: OutputLimits = DEFAULT_OUTPUT_LIMITS,
) -> RedactionResult:
    """Redact, bound, and post-scan a structured output value."""

    return _sanitize_output(value, _materialize_canaries(canaries), limits)


def _safe_json_text(
    value: Any,
    *,
    canaries: tuple[str, ...],
    limits: OutputLimits,
    indent: int | None,
    sort_keys: bool,
    separators: tuple[str, str] | None,
    append_newline: bool,
) -> str:
    sanitized = _sanitize_output(value, canaries, limits)
    dump_options: dict[str, Any] = {
        "allow_nan": False,
        "ensure_ascii": False,
        "indent": indent,
        "sort_keys": sort_keys,
    }
    if separators is not None:
        dump_options["separators"] = separators
    elif indent is None:
        dump_options["separators"] = (",", ":")
    text = json.dumps(sanitized.value, **dump_options)
    decoded = json.loads(text)
    serialized_findings = credential_like_paths(decoded, canaries=canaries)
    raw_canary_present = any(canary and canary in text for canary in canaries)
    if serialized_findings or raw_canary_present:
        raise UnsafeOutputError("unsafe serialized output rejected")
    if append_newline:
        text += "\n"
    return text


def safe_json_text(
    value: Any,
    *,
    canaries: Iterable[str] = (),
    limits: OutputLimits = DEFAULT_OUTPUT_LIMITS,
    indent: int | None = None,
    sort_keys: bool = True,
    separators: tuple[str, str] | None = None,
    append_newline: bool = False,
) -> str:
    """Return sanitized UTF-8-compatible JSON text after a serialized post-scan."""

    return _safe_json_text(
        value,
        canaries=_materialize_canaries(canaries),
        limits=limits,
        indent=indent,
        sort_keys=sort_keys,
        separators=separators,
        append_newline=append_newline,
    )


def safe_json_bytes(
    value: Any,
    *,
    canaries: Iterable[str] = (),
    limits: OutputLimits = DEFAULT_OUTPUT_LIMITS,
    indent: int | None = None,
    sort_keys: bool = True,
    separators: tuple[str, str] | None = None,
    append_newline: bool = False,
) -> bytes:
    """Return deterministic sanitized JSON bytes."""

    canary_values = _materialize_canaries(canaries)
    return _safe_json_text(
        value,
        canaries=canary_values,
        limits=limits,
        indent=indent,
        sort_keys=sort_keys,
        separators=separators,
        append_newline=append_newline,
    ).encode("utf-8")


def write_json_exclusive(
    path: Path,
    value: Any,
    *,
    canaries: Iterable[str] = (),
    limits: OutputLimits = DEFAULT_OUTPUT_LIMITS,
    indent: int | None = 2,
    sort_keys: bool = True,
    separators: tuple[str, str] | None = None,
    append_newline: bool = True,
) -> bytes:
    """Sanitize fully before exclusively creating and syncing one JSON file."""

    data = safe_json_bytes(
        value,
        canaries=canaries,
        limits=limits,
        indent=indent,
        sort_keys=sort_keys,
        separators=separators,
        append_newline=append_newline,
    )
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return data
