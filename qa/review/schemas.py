"""Immutable review packet, review result, and adjudication contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from ..models import Verdict
from ..redaction import redact_value
from .independence import IndependenceLevel

SEMANTIC_VERDICTS = {Verdict.PASS, Verdict.FAIL, Verdict.INCONCLUSIVE}


def canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewPacket:
    schema: str
    packet_id: str
    case_id: str
    sanitized_request: Mapping[str, Any]
    expected_contract: Mapping[str, Any]
    actual_output: Any
    bounded_evidence: Mapping[str, Any]
    deterministic_failures: tuple[str, ...]
    source_evidence_digest: str
    packet_digest: str

    @classmethod
    def create(cls, record: Mapping[str, Any]) -> ReviewPacket:
        case_id = str(record.get("case_id", "")).strip()
        if not case_id:
            raise ValueError("review packet needs a case_id")
        observation = record.get("observation", {})
        if not isinstance(observation, Mapping):
            observation = {}
        actual_output = observation.get(
            "final", observation.get("output", observation.get("stdout", dict(observation)))
        )
        sanitized = redact_value(
            {
                "sanitized_request": record.get("sanitized_request", {}),
                "expected_contract": record.get("expected_contract", {}),
                "actual_output": actual_output,
                "bounded_evidence": record.get("bounded_evidence", {}),
            }
        ).value
        source_digest = canonical_digest(dict(record))
        body = {
            "schema": "jarvis.qa.review-packet.v1",
            "packet_id": f"review-{case_id}-{source_digest[:12]}",
            "case_id": case_id,
            **sanitized,
            "deterministic_failures": [
                str(item) for item in record.get("deterministic_failures", [])
            ],
            "source_evidence_digest": source_digest,
        }
        digest = canonical_digest(body)
        return cls(
            schema=body["schema"],
            packet_id=body["packet_id"],
            case_id=case_id,
            sanitized_request=dict(body["sanitized_request"]),
            expected_contract=dict(body["expected_contract"]),
            actual_output=body["actual_output"],
            bounded_evidence=dict(body["bounded_evidence"]),
            deterministic_failures=tuple(body["deterministic_failures"]),
            source_evidence_digest=source_digest,
            packet_digest=digest,
        )

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["deterministic_failures"] = list(self.deterministic_failures)
        return document

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewPacket:
        if data.get("schema") != "jarvis.qa.review-packet.v1":
            raise ValueError("unsupported review packet schema")
        packet = cls(
            schema=str(data["schema"]),
            packet_id=str(data["packet_id"]),
            case_id=str(data["case_id"]),
            sanitized_request=dict(data.get("sanitized_request", {})),
            expected_contract=dict(data.get("expected_contract", {})),
            actual_output=data.get("actual_output"),
            bounded_evidence=dict(data.get("bounded_evidence", {})),
            deterministic_failures=tuple(
                str(item) for item in data.get("deterministic_failures", [])
            ),
            source_evidence_digest=str(data["source_evidence_digest"]),
            packet_digest=str(data["packet_digest"]),
        )
        body = packet.to_dict()
        body.pop("packet_digest")
        if canonical_digest(body) != packet.packet_digest:
            raise ValueError("review packet digest mismatch")
        return packet


@dataclass(frozen=True, slots=True)
class ReviewResult:
    schema: str
    review_id: str
    reviewer_id: str
    independence_level: IndependenceLevel
    verdict: Verdict
    rationale: str
    evidence_citations: tuple[str, ...]
    packet: ReviewPacket
    created_at_utc: str
    review_digest: str

    @classmethod
    def create(
        cls,
        *,
        review_id: str,
        reviewer_id: str,
        independence_level: IndependenceLevel,
        verdict: Verdict,
        rationale: str,
        evidence_citations: tuple[str, ...],
        packet: ReviewPacket,
    ) -> ReviewResult:
        if verdict not in SEMANTIC_VERDICTS:
            raise ValueError("semantic review verdict must be PASS, FAIL, or INCONCLUSIVE")
        if not review_id or not reviewer_id or not rationale.strip():
            raise ValueError("review identity and rationale are required")
        sanitized_review = redact_value(
            {
                "rationale": rationale,
                "evidence_citations": list(evidence_citations),
            }
        ).value
        sanitized_rationale = str(sanitized_review["rationale"])
        sanitized_citations = tuple(str(item) for item in sanitized_review["evidence_citations"])
        created = datetime.now(UTC).isoformat()
        body = {
            "schema": "jarvis.qa.review-result.v1",
            "review_id": review_id,
            "reviewer_id": reviewer_id,
            "independence_level": independence_level.value,
            "verdict": verdict.value,
            "rationale": sanitized_rationale,
            "evidence_citations": list(sanitized_citations),
            "packet": packet.to_dict(),
            "created_at_utc": created,
        }
        return cls(
            schema=body["schema"],
            review_id=review_id,
            reviewer_id=reviewer_id,
            independence_level=independence_level,
            verdict=verdict,
            rationale=sanitized_rationale,
            evidence_citations=sanitized_citations,
            packet=packet,
            created_at_utc=created,
            review_digest=canonical_digest(body),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "review_id": self.review_id,
            "reviewer_id": self.reviewer_id,
            "independence_level": self.independence_level.value,
            "verdict": self.verdict.value,
            "rationale": self.rationale,
            "evidence_citations": list(self.evidence_citations),
            "packet": self.packet.to_dict(),
            "created_at_utc": self.created_at_utc,
            "review_digest": self.review_digest,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewResult:
        if data.get("schema") != "jarvis.qa.review-result.v1":
            raise ValueError("unsupported review result schema")
        packet = ReviewPacket.from_dict(data["packet"])
        result = cls(
            schema=str(data["schema"]),
            review_id=str(data["review_id"]),
            reviewer_id=str(data["reviewer_id"]),
            independence_level=IndependenceLevel(str(data["independence_level"])),
            verdict=Verdict(str(data["verdict"])),
            rationale=str(data["rationale"]),
            evidence_citations=tuple(str(item) for item in data.get("evidence_citations", [])),
            packet=packet,
            created_at_utc=str(data["created_at_utc"]),
            review_digest=str(data["review_digest"]),
        )
        if result.verdict not in SEMANTIC_VERDICTS:
            raise ValueError("invalid semantic review verdict")
        body = result.to_dict()
        body.pop("review_digest")
        if canonical_digest(body) != result.review_digest:
            raise ValueError("review result digest mismatch")
        return result


@dataclass(frozen=True, slots=True)
class AdjudicationResult:
    schema: str
    verdict: Verdict
    rationale: str
    deterministic_failures_preserved: tuple[str, ...]
    reviews: tuple[ReviewResult, ReviewResult]
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "verdict": self.verdict.value,
            "rationale": self.rationale,
            "deterministic_failures_preserved": list(self.deterministic_failures_preserved),
            "reviews": [review.to_dict() for review in self.reviews],
            "created_at_utc": self.created_at_utc,
        }
