"""Registry for deterministic assurance validators."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..models import AssertionResult
from .artifacts import validate_artifact
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
    observation: Mapping[str, Any], specs: Iterable[Mapping[str, Any]]
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
        kind = str(spec.get("kind", ""))
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


__all__ = ["run_validators"]
