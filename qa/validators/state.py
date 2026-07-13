"""Runtime identity, claimed-state, secret, and exit-result consistency checks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from ..models import AssertionResult
from ..redaction import credential_like_paths
from .format_contracts import field_value


def validate_identity(
    observation: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[AssertionResult]:
    checks = {
        "conversation_id": spec.get("expected_conversation_id"),
        "runtime_id": spec.get("expected_runtime_id"),
        "namespace": spec.get("expected_namespace"),
    }
    assertions: list[AssertionResult] = []
    for field, expected in checks.items():
        if expected is not None:
            actual = observation.get(field)
            assertions.append(
                AssertionResult(f"identity.{field}", actual == expected, expected, actual)
            )
    forbidden = {str(item) for item in spec.get("forbidden_conversation_ids", [])}
    conversation = str(observation.get("conversation_id", ""))
    assertions.append(
        AssertionResult(
            "identity.conversation_isolated",
            bool(conversation) and conversation not in forbidden,
            "new conversation identity",
            conversation,
        )
    )
    runtime = observation.get("runtime_id")
    transcript_runtime = observation.get("transcript_runtime_id")
    transcript_conversation = observation.get("transcript_conversation_id")
    assertions.append(
        AssertionResult(
            "identity.transcript_runtime",
            bool(runtime) and runtime == transcript_runtime,
            runtime,
            transcript_runtime,
        )
    )
    assertions.append(
        AssertionResult(
            "identity.transcript_conversation",
            bool(conversation) and conversation == transcript_conversation,
            conversation,
            transcript_conversation,
        )
    )
    return assertions


def validate_claimed_state(
    observation: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[AssertionResult]:
    claims = field_value(observation, str(spec.get("claims_field", "claimed_state")), {})
    observed = field_value(observation, str(spec.get("observed_field", "observed_state")), {})
    if not isinstance(claims, Mapping) or not isinstance(observed, Mapping):
        return [
            AssertionResult(
                "state.claims_observed_objects",
                False,
                "two objects",
                {"claims": type(claims).__name__, "observed": type(observed).__name__},
            )
        ]
    keys = spec.get("keys") or sorted(claims)
    return [
        AssertionResult(
            f"state.claim_matches.{key}",
            key in claims and key in observed and claims[key] == observed[key],
            claims.get(key),
            observed.get(key),
        )
        for key in keys
    ] or [AssertionResult("state.non_empty_claim", False, "at least one claim", claims)]


def validate_canary_absence(
    observation: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[AssertionResult]:
    serialized = json.dumps(observation, ensure_ascii=False, sort_keys=True)
    canaries = [str(item) for item in spec.get("canaries", []) if str(item)]
    present = ["configured_canary" for canary in canaries if canary in serialized]
    leaked_paths = credential_like_paths(observation)
    prewrite_events = int(observation.get("prewrite_redaction_events", 0) or 0)
    return [
        AssertionResult(
            "state.canary_absent",
            not present and not leaked_paths and prewrite_events == 0,
            "no credential-like value before persistence",
            {
                "configured_canaries": len(present),
                "credential_paths": list(leaked_paths),
                "prewrite_redaction_events": prewrite_events,
            },
        )
    ]


def validate_exit_consistency(
    observation: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[AssertionResult]:
    process_exit = field_value(observation, str(spec.get("process_field", "process_exit_code")))
    machine = field_value(observation, str(spec.get("result_field", "machine_result")), {})
    if not isinstance(machine, Mapping):
        machine = {}
    machine_exit = machine.get("exit_code")
    if machine_exit is None and isinstance(machine.get("ok"), bool):
        machine_exit = 0 if machine["ok"] else 1
    return [
        AssertionResult(
            "state.exit_code_matches_result",
            isinstance(process_exit, int)
            and isinstance(machine_exit, int)
            and process_exit == machine_exit,
            machine_exit,
            process_exit,
        )
    ]
