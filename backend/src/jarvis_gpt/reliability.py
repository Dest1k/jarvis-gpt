from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

PolicyEffect = Literal["allow", "require_approval", "deny", "defer"]
ToolOutcome = Literal["not_started", "completed", "failed", "unknown"]


@dataclass(frozen=True)
class PolicyDecision:
    """Machine-readable policy result returned at the tool boundary."""

    effect: PolicyEffect
    code: str
    source: str
    reason: str
    remediation: str
    retryable: bool = False
    outcome: ToolOutcome = "not_started"
    missing_capability: str | None = None
    binding: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "protocol": "jarvis.policy-decision.v1",
            "effect": self.effect,
            "code": self.code,
            "source": self.source,
            "reason": self.reason,
            "remediation": self.remediation,
            "retryable": self.retryable,
            "outcome": self.outcome,
        }
        if self.missing_capability:
            value["missing_capability"] = self.missing_capability
        if self.binding:
            value["binding"] = dict(self.binding)
        return value


@dataclass(frozen=True)
class ToolFailure:
    """Stable failure semantics so callers do not guess whether replay is safe."""

    kind: str
    outcome: ToolOutcome
    outcome_known: bool
    retryable: bool
    requires_operator: bool
    remediation: str
    fallback: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "protocol": "jarvis.tool-failure.v1",
            "kind": self.kind,
            "outcome": self.outcome,
            "outcome_known": self.outcome_known,
            "retryable": self.retryable,
            "requires_operator": self.requires_operator,
            "remediation": self.remediation,
        }
        if self.fallback:
            value["fallback"] = self.fallback
        return value
