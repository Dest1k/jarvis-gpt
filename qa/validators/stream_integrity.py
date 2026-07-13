"""Deterministically reconstruct NDJSON streams and verify terminal state."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..models import AssertionResult
from .format_contracts import field_value


def validate_ndjson_stream(
    observation: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[AssertionResult]:
    raw = field_value(observation, str(spec.get("field", "ndjson")), "")
    parse_errors: list[str] = []
    events: list[dict[str, Any]] = []
    if isinstance(raw, str):
        lines = [line for line in raw.splitlines() if line.strip()]
        for line_number, line in enumerate(lines, start=1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_errors.append(f"line {line_number}: {exc.msg}")
                continue
            if not isinstance(item, dict):
                parse_errors.append(f"line {line_number}: event is not an object")
                continue
            events.append(item)
    elif isinstance(raw, list) and all(isinstance(item, dict) for item in raw):
        events = [dict(item) for item in raw]
    else:
        parse_errors.append("stream is neither NDJSON text nor an event array")

    event_types = [str(item.get("type", item.get("event", ""))) for item in events]
    allowed_types = set(spec.get("allowed_types", ["meta", "delta", "thought", "progress", "done"]))
    unknown_types = sorted(
        {event_type for event_type in event_types if event_type not in allowed_types}
    )
    meta_indexes = [index for index, item in enumerate(event_types) if item == "meta"]
    terminal_indexes = [index for index, item in enumerate(event_types) if item == "done"]
    deltas = [
        str(item.get("delta", item.get("text", "")))
        for item, event_type in zip(events, event_types, strict=True)
        if event_type == "delta"
    ]
    reconstructed = "".join(deltas)
    terminal_final = ""
    if len(terminal_indexes) == 1:
        terminal = events[terminal_indexes[0]]
        terminal_final = str(terminal.get("answer", terminal.get("final", "")))
    terminal_ok = len(terminal_indexes) == 1 and terminal_indexes[0] == len(events) - 1
    persisted = observation.get("persisted_final")
    persisted_ok = persisted is None or str(persisted) == terminal_final
    return [
        AssertionResult(
            "stream.valid_ndjson", not parse_errors, "valid object per line", parse_errors
        ),
        AssertionResult("stream.has_events", bool(events), "at least one event", len(events)),
        AssertionResult("stream.known_event_types", not unknown_types, [], unknown_types),
        AssertionResult(
            "stream.single_meta_first",
            meta_indexes == [0],
            [0],
            meta_indexes,
        ),
        AssertionResult(
            "stream.single_terminal_last",
            terminal_ok,
            "exactly one final done event",
            terminal_indexes,
        ),
        AssertionResult(
            "stream.delta_equals_terminal",
            terminal_ok and reconstructed == terminal_final,
            terminal_final,
            reconstructed,
        ),
        AssertionResult(
            "stream.terminal_equals_persisted",
            terminal_ok and persisted_ok,
            terminal_final,
            persisted,
        ),
        AssertionResult(
            "stream.no_error_event",
            "error" not in event_types,
            "no error event",
            event_types,
        ),
    ]
