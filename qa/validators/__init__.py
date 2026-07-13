"""Registry for deterministic assurance validators."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..models import AssertionResult, Scenario
from .artifacts import validate_artifact
from .context import ValidationContext
from .format_contracts import validate_format_contract
from .response_integrity import validate_response_integrity
from .state import (
    validate_canary_absence,
    validate_claimed_state,
    validate_exit_consistency,
    validate_identity,
)
from .stream_integrity import validate_ndjson_stream


def run_validators(
    observation: Mapping[str, Any],
    specs: Iterable[Mapping[str, Any]],
    *,
    context: ValidationContext | None = None,
) -> list[AssertionResult]:
    assertions: list[AssertionResult] = []
    registry = {
        "artifact": validate_artifact,
        "canary_absence": validate_canary_absence,
        "claimed_state": validate_claimed_state,
        "exit_consistency": validate_exit_consistency,
        "format_contract": validate_format_contract,
        "identity": validate_identity,
        "response_integrity": validate_response_integrity,
        "stream_integrity": validate_ndjson_stream,
    }
    for spec in specs:
        if not isinstance(spec, Mapping):
            assertions.append(
                AssertionResult(
                    "validator.spec.contract",
                    False,
                    "validator specification object",
                    type(spec).__name__,
                )
            )
            continue
        raw_kind = spec.get("kind")
        kind = raw_kind if isinstance(raw_kind, str) else ""
        try:
            Scenario.from_dict(
                {
                    "scenario_id": "VALIDATOR-SPEC-001",
                    "title": "validator contract check",
                    "transport": "offline",
                    "request": {},
                    "expected_contract": {},
                    "validators": [dict(spec)],
                }
            )
        except (TypeError, ValueError) as exc:
            assertions.append(
                AssertionResult(
                    f"validator.{kind or 'missing'}.contract",
                    False,
                    "strict validator contract",
                    str(exc),
                )
            )
        validator = registry.get(kind)
        if validator is None:
            assertions.append(
                AssertionResult(
                    f"validator.{kind or 'missing'}.known",
                    False,
                    sorted(registry),
                    kind,
                )
            )
            continue
        try:
            if kind == "artifact":
                assertions.extend(validator(observation, spec, context=context))
            else:
                assertions.extend(validator(observation, spec))
        except Exception as exc:  # fail closed at the validator boundary
            assertions.append(
                AssertionResult(
                    f"validator.{kind}.error",
                    False,
                    "validator completed",
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return assertions


__all__ = ["ValidationContext", "run_validators"]
