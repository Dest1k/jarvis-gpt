"""Typed contracts shared by the developer-only assurance harness."""

from __future__ import annotations

import re
import secrets
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_INCOMPLETE = 2
EXIT_HARNESS_ERROR = 3


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED_BY_ENV = "BLOCKED_BY_ENV"
    BLOCKED_BY_SPEC = "BLOCKED_BY_SPEC"
    SKIP = "SKIP"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class AssertionResult:
    name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Scenario:
    scenario_id: str
    title: str
    transport: str
    request: Mapping[str, Any]
    expected_contract: Mapping[str, Any]
    validators: tuple[Mapping[str, Any], ...]
    required: bool = True
    semantic_review_required: bool = False
    skip_reason: str | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.skip_reason is not None:
            if self.required:
                raise ValueError("skip_reason is allowed only for an optional scenario")
            if not self.skip_reason.strip():
                raise ValueError("skip_reason must be non-empty")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Scenario:
        scenario_id = str(data.get("scenario_id", "")).strip()
        title = str(data.get("title", "")).strip()
        transport = str(data.get("transport", "")).strip().lower()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{2,79}", scenario_id):
            raise ValueError("scenario_id must be a stable uppercase identifier")
        if not title:
            raise ValueError(f"{scenario_id}: title is required")
        if transport not in {"offline", "http", "cli"}:
            raise ValueError(f"{scenario_id}: unsupported transport {transport!r}")
        request = data.get("request")
        contract = data.get("expected_contract")
        validators = data.get("validators")
        if not isinstance(request, Mapping):
            raise ValueError(f"{scenario_id}: request must be an object")
        if not isinstance(contract, Mapping):
            raise ValueError(f"{scenario_id}: expected_contract must be an object")
        if not isinstance(validators, list) or not validators:
            raise ValueError(f"{scenario_id}: at least one validator is required")
        if not all(isinstance(item, Mapping) and item.get("kind") for item in validators):
            raise ValueError(f"{scenario_id}: every validator needs a kind")
        tags = data.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise ValueError(f"{scenario_id}: tags must be strings")
        required = bool(data.get("required", True))
        skip_reason = data.get("skip_reason")
        if skip_reason is not None and not isinstance(skip_reason, str):
            raise ValueError(f"{scenario_id}: skip_reason must be a string")
        return cls(
            scenario_id=scenario_id,
            title=title,
            transport=transport,
            request=dict(request),
            expected_contract=dict(contract),
            validators=tuple(dict(item) for item in validators),
            required=required,
            semantic_review_required=bool(data.get("semantic_review_required", False)),
            skip_reason=skip_reason,
            tags=tuple(tags),
        )


@dataclass(frozen=True, slots=True)
class CampaignIdentity:
    campaign_id: str
    namespace: str

    def __post_init__(self) -> None:
        pattern = r"[a-z0-9][a-z0-9_.-]{7,127}"
        if not re.fullmatch(pattern, self.campaign_id):
            raise ValueError("invalid campaign_id")
        if not re.fullmatch(pattern, self.namespace):
            raise ValueError("invalid namespace")
        if self.campaign_id == self.namespace:
            raise ValueError("campaign_id and namespace must be distinct")

    @classmethod
    def create(cls, prefix: str = "jarvis-assurance") -> CampaignIdentity:
        safe_prefix = re.sub(r"[^a-z0-9-]+", "-", prefix.lower()).strip("-")
        if not safe_prefix:
            raise ValueError("campaign prefix is empty after normalization")
        stamp = datetime.now(UTC).strftime("%Y%m%dt%H%M%Sz")
        nonce = secrets.token_hex(6)
        return cls(
            campaign_id=f"{safe_prefix}-{stamp}-{nonce}",
            namespace=f"qa.{stamp}.{nonce}",
        )


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    verdict: Verdict
    assertions: tuple[AssertionResult, ...]
    observation: Mapping[str, Any] = field(default_factory=dict)
    bounded_evidence: Mapping[str, Any] = field(default_factory=dict)
    required: bool = True
    semantic_review_required: bool = False
    error: str | None = None
    observed_at_utc: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        if self.verdict is Verdict.PASS:
            if not self.assertions:
                raise ValueError("PASS requires at least one factual assertion")
            if not all(assertion.passed for assertion in self.assertions):
                raise ValueError("PASS cannot contain a failed assertion")
        if self.verdict is Verdict.FAIL and not any(
            not assertion.passed for assertion in self.assertions
        ):
            raise ValueError("FAIL requires a failed deterministic assertion")

    @property
    def deterministic_failures(self) -> tuple[str, ...]:
        return tuple(assertion.name for assertion in self.assertions if not assertion.passed)


@dataclass(frozen=True, slots=True)
class CampaignSummary:
    identity: CampaignIdentity
    results: tuple[CaseResult, ...]

    @property
    def counts(self) -> dict[str, int]:
        counter = Counter(result.verdict.value for result in self.results)
        return {verdict.value: counter.get(verdict.value, 0) for verdict in Verdict}

    @property
    def exit_code(self) -> int:
        if not self.results or any(result.verdict is Verdict.ERROR for result in self.results):
            return EXIT_HARNESS_ERROR
        if any(result.verdict is Verdict.FAIL for result in self.results):
            return EXIT_FAIL
        incomplete = {
            Verdict.INCONCLUSIVE,
            Verdict.BLOCKED_BY_ENV,
            Verdict.BLOCKED_BY_SPEC,
            Verdict.SKIP,
        }
        if any(result.required and result.verdict in incomplete for result in self.results):
            return EXIT_INCOMPLETE
        return EXIT_PASS
