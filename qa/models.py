"""Typed contracts shared by the developer-only assurance harness."""

from __future__ import annotations

import json
import re
import secrets
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from .safe_paths import validate_campaign_identifier, validate_case_id
from .schema_validation import validate_json_schema

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
        validate_case_id(self.scenario_id, label="scenario_id")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("scenario title is required")
        if self.transport not in {"offline", "http", "cli"}:
            raise ValueError("unsupported scenario transport")
        if not isinstance(self.request, Mapping) or not isinstance(
            self.expected_contract, Mapping
        ):
            raise ValueError("scenario request and expected contract must be objects")
        if not self.validators or any(not isinstance(item, Mapping) for item in self.validators):
            raise ValueError("scenario validators must be non-empty objects")
        if not isinstance(self.required, bool) or not isinstance(
            self.semantic_review_required, bool
        ):
            raise ValueError("scenario flags must be booleans")
        if self.skip_reason is not None:
            if self.required:
                raise ValueError("skip_reason is allowed only for an optional scenario")
            if not self.skip_reason.strip():
                raise ValueError("skip_reason must be non-empty")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Scenario:
        if isinstance(data, Mapping) and "skip_reason" in data:
            if data.get("required", True) is not False:
                raise ValueError("skip_reason is allowed only for an optional scenario")
            if isinstance(data.get("skip_reason"), str) and not data["skip_reason"].strip():
                raise ValueError("skip_reason must be non-empty")
        errors = validate_json_schema(data, _scenario_schema())
        if errors:
            raise ValueError(f"invalid scenario contract: {'; '.join(errors[:5])}")
        scenario_id = validate_case_id(data["scenario_id"], label="scenario_id")
        title = data["title"]
        if not title.strip():
            raise ValueError(f"{scenario_id}: title is required")
        transport = data["transport"]
        request = data["request"]
        contract = data["expected_contract"]
        validators = data["validators"]
        tags = data.get("tags", [])
        required = data.get("required", True)
        skip_reason = data.get("skip_reason")
        return cls(
            scenario_id=scenario_id,
            title=title,
            transport=transport,
            request=dict(request),
            expected_contract=dict(contract),
            validators=tuple(dict(item) for item in validators),
            required=required,
            semantic_review_required=data.get("semantic_review_required", False),
            skip_reason=skip_reason,
            tags=tuple(tags),
        )


@dataclass(frozen=True, slots=True)
class CampaignIdentity:
    campaign_id: str
    namespace: str

    def __post_init__(self) -> None:
        validate_campaign_identifier(self.campaign_id, label="campaign_id")
        validate_campaign_identifier(self.namespace, label="namespace")
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
        validate_case_id(self.case_id)
        if not isinstance(self.required, bool) or not isinstance(
            self.semantic_review_required, bool
        ):
            raise ValueError("case flags must be booleans")
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


@lru_cache(maxsize=1)
def _scenario_schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "schemas" / "scenario.schema.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise RuntimeError("scenario schema must be an object")
    return document
