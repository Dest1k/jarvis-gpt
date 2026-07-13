"""Immutable review packet, review result, and adjudication contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..evidence import canonical_record_sha256
from ..models import Verdict
from ..output import sanitize_output
from ..safe_paths import validate_case_id
from .independence import IndependenceLevel

SEMANTIC_VERDICTS = {Verdict.PASS, Verdict.FAIL, Verdict.INCONCLUSIVE}
_PACKET_VERIFICATION_TOKEN = object()


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw_json(item) for item in value]
    return value


def canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
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
    recorded_verdict: Verdict
    replayed_verdict: Verdict
    source_record_sha256: str
    source_record_canonical_sha256: str
    source_evidence_sha256: str
    source_manifest_sha256: str
    source_replay_sha256: str
    packet_digest: str
    _verification_token: object | None = dataclass_field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def provenance_verified(self) -> bool:
        return bool(
            self._verification_token is _PACKET_VERIFICATION_TOKEN
            and self.packet_digest == self.expected_digest()
        )

    @classmethod
    def create(
        cls,
        record: Mapping[str, Any],
        *,
        replay_case: Any,
        replay_summary: Any,
        evidence_path: Path,
        expected_manifest_sha256: str | None = None,
        replay_context: Any = None,
        canaries: Iterable[str] = (),
    ) -> ReviewPacket:
        from ..evidence import verify_evidence_bundle
        from ..replay import ReplayCase, ReplaySummary, replay_record, replay_records

        if not isinstance(replay_case, ReplayCase) or not isinstance(
            replay_summary, ReplaySummary
        ):
            raise TypeError("review packet requires a verified replay case and summary")
        if not replay_summary.integrity_verified:
            raise ValueError("review packet requires a fresh verified replay")
        case_id = validate_case_id(record.get("case_id"))
        if not isinstance(evidence_path, Path):
            raise TypeError("review packet evidence path must be a Path")
        records, errors, integrity = verify_evidence_bundle(
            evidence_path,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        if errors or integrity is None:
            raise ValueError("review packet requires a verified evidence bundle")
        fresh_summary = replay_records(
            records,
            integrity,
            context=replay_context,
        )
        if not fresh_summary.integrity_verified or fresh_summary != replay_summary:
            raise ValueError("review packet fresh replay binding mismatch")
        matching_records = [
            source for source in records if source.get("case_id") == case_id
        ]
        if len(matching_records) != 1 or (
            canonical_record_sha256(matching_records[0])
            != canonical_record_sha256(record)
        ):
            raise ValueError("review packet verified source record mismatch")
        matching_cases = [case for case in replay_summary.cases if case.case_id == case_id]
        if matching_cases != [replay_case] or not replay_case.matches:
            raise ValueError("review packet replay case binding mismatch")
        if record.get("verdict") != replay_case.recorded_verdict.value:
            raise ValueError("review packet recorded verdict mismatch")
        if (
            canonical_record_sha256(record)
            != replay_case.source_record_canonical_sha256
        ):
            raise ValueError("review packet source record binding mismatch")
        recomputed_case = replay_record(
            record,
            source_record_sha256=replay_case.source_record_sha256,
            source_record_canonical_sha256=(
                replay_case.source_record_canonical_sha256
            ),
            context=replay_context,
        )
        if recomputed_case != replay_case:
            raise ValueError("review packet replay substitution detected")
        observation = record.get("observation", {})
        if not isinstance(observation, Mapping):
            observation = {}
        actual_output = observation.get(
            "final", observation.get("output", observation.get("stdout", dict(observation)))
        )
        sanitized = sanitize_output(
            {
                "sanitized_request": record.get("sanitized_request", {}),
                "expected_contract": record.get("expected_contract", {}),
                "actual_output": actual_output,
                "bounded_evidence": record.get("bounded_evidence", {}),
            },
            canaries=canaries,
        ).value
        body = {
            "schema": "jarvis.qa.review-packet.v2",
            "packet_id": f"review-{case_id}-{replay_case.source_record_sha256[:12]}",
            "case_id": case_id,
            **sanitized,
            "deterministic_failures": list(replay_case.deterministic_failures),
            "recorded_verdict": replay_case.recorded_verdict.value,
            "replayed_verdict": replay_case.replayed_verdict.value,
            "source_record_sha256": replay_case.source_record_sha256,
            "source_record_canonical_sha256": (
                replay_case.source_record_canonical_sha256
            ),
            "source_evidence_sha256": replay_summary.evidence_sha256,
            "source_manifest_sha256": replay_summary.manifest_sha256,
            "source_replay_sha256": replay_summary.replay_digest,
        }
        digest = canonical_digest(body)
        packet = cls(
            schema=body["schema"],
            packet_id=body["packet_id"],
            case_id=case_id,
            sanitized_request=_freeze_json(body["sanitized_request"]),
            expected_contract=_freeze_json(body["expected_contract"]),
            actual_output=_freeze_json(body["actual_output"]),
            bounded_evidence=_freeze_json(body["bounded_evidence"]),
            deterministic_failures=tuple(body["deterministic_failures"]),
            recorded_verdict=replay_case.recorded_verdict,
            replayed_verdict=replay_case.replayed_verdict,
            source_record_sha256=replay_case.source_record_sha256,
            source_record_canonical_sha256=(
                replay_case.source_record_canonical_sha256
            ),
            source_evidence_sha256=replay_summary.evidence_sha256,
            source_manifest_sha256=replay_summary.manifest_sha256,
            source_replay_sha256=replay_summary.replay_digest,
            packet_digest=digest,
        )
        object.__setattr__(
            packet,
            "_verification_token",
            _PACKET_VERIFICATION_TOKEN,
        )
        return packet

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "packet_id": self.packet_id,
            "case_id": self.case_id,
            "sanitized_request": _thaw_json(self.sanitized_request),
            "expected_contract": _thaw_json(self.expected_contract),
            "actual_output": _thaw_json(self.actual_output),
            "bounded_evidence": _thaw_json(self.bounded_evidence),
            "deterministic_failures": list(self.deterministic_failures),
            "recorded_verdict": self.recorded_verdict.value,
            "replayed_verdict": self.replayed_verdict.value,
            "source_record_sha256": self.source_record_sha256,
            "source_record_canonical_sha256": self.source_record_canonical_sha256,
            "source_evidence_sha256": self.source_evidence_sha256,
            "source_manifest_sha256": self.source_manifest_sha256,
            "source_replay_sha256": self.source_replay_sha256,
            "packet_digest": self.packet_digest,
        }

    def expected_digest(self) -> str:
        body = self.to_dict()
        body.pop("packet_digest")
        return canonical_digest(body)

    def verify_replay(self, replay_summary: Any) -> None:
        from ..replay import ReplaySummary

        if self.packet_digest != self.expected_digest():
            raise ValueError("review packet digest mismatch")
        if not isinstance(replay_summary, ReplaySummary) or not (
            replay_summary.integrity_verified
        ):
            raise ValueError("packet replay report is not verified")
        if (
            self.source_evidence_sha256 != replay_summary.evidence_sha256
            or self.source_manifest_sha256 != replay_summary.manifest_sha256
            or self.source_replay_sha256 != replay_summary.replay_digest
        ):
            raise ValueError("packet replay report binding mismatch")
        matching = [
            case for case in replay_summary.cases if case.case_id == self.case_id
        ]
        if len(matching) != 1:
            raise ValueError("packet replay case is missing or duplicated")
        replay_case = matching[0]
        if (
            replay_case.recorded_verdict is not self.recorded_verdict
            or replay_case.replayed_verdict is not self.replayed_verdict
            or replay_case.deterministic_failures != self.deterministic_failures
            or replay_case.source_record_sha256 != self.source_record_sha256
            or replay_case.source_record_canonical_sha256
            != self.source_record_canonical_sha256
        ):
            raise ValueError("packet replay case binding mismatch")

    def verify_source(
        self,
        evidence_path: Path,
        *,
        expected_manifest_sha256: str | None = None,
        replay_summary: Any = None,
        replay_context: Any = None,
    ) -> None:
        from ..evidence import verify_evidence_bundle
        from ..replay import ReplaySummary, replay_records

        if not isinstance(evidence_path, Path):
            raise TypeError("review packet evidence path must be a Path")
        records, errors, integrity = verify_evidence_bundle(
            evidence_path,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        if errors or integrity is None:
            raise ValueError("review packet source evidence is not verified")
        fresh_replay = replay_records(records, integrity, context=replay_context)
        if not fresh_replay.integrity_verified:
            raise ValueError("review packet source replay is not verified")
        if replay_summary is not None:
            if not isinstance(replay_summary, ReplaySummary) or not (
                replay_summary.integrity_verified
            ):
                raise ValueError("packet replay report is not verified")
            if replay_summary.to_dict() != fresh_replay.to_dict():
                raise ValueError("packet replay source substitution detected")
        self.verify_replay(fresh_replay)
        matching_records = [
            record
            for record in records
            if record.get("case_id") == self.case_id
            and canonical_record_sha256(record)
            == self.source_record_canonical_sha256
        ]
        matching_cases = [
            replay_case
            for replay_case in fresh_replay.cases
            if replay_case.case_id == self.case_id
            and replay_case.source_record_sha256 == self.source_record_sha256
            and replay_case.source_record_canonical_sha256
            == self.source_record_canonical_sha256
        ]
        if len(matching_records) != 1 or len(matching_cases) != 1:
            raise ValueError("review packet verified source is missing or duplicated")
        expected = type(self).create(
            matching_records[0],
            replay_case=matching_cases[0],
            replay_summary=fresh_replay,
            evidence_path=evidence_path,
            expected_manifest_sha256=expected_manifest_sha256,
            replay_context=replay_context,
        )
        if self.to_dict() != expected.to_dict():
            raise ValueError("review packet source content mismatch")
        object.__setattr__(
            self,
            "_verification_token",
            _PACKET_VERIFICATION_TOKEN,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewPacket:
        fields = {
            "schema",
            "packet_id",
            "case_id",
            "sanitized_request",
            "expected_contract",
            "actual_output",
            "bounded_evidence",
            "deterministic_failures",
            "recorded_verdict",
            "replayed_verdict",
            "source_record_sha256",
            "source_record_canonical_sha256",
            "source_evidence_sha256",
            "source_manifest_sha256",
            "source_replay_sha256",
            "packet_digest",
        }
        if set(data) != fields:
            raise ValueError("review packet fields are incomplete or unexpected")
        if data.get("schema") != "jarvis.qa.review-packet.v2":
            raise ValueError("unsupported review packet schema")
        if not isinstance(data.get("packet_id"), str) or not data["packet_id"]:
            raise ValueError("invalid review packet identity")
        if not isinstance(data.get("case_id"), str):
            raise ValueError("invalid review packet case identifier")
        for field in ("sanitized_request", "expected_contract", "bounded_evidence"):
            if not isinstance(data.get(field), Mapping):
                raise ValueError(f"review packet {field} must be an object")
        for field in (
            "source_record_sha256",
            "source_record_canonical_sha256",
            "source_evidence_sha256",
            "source_manifest_sha256",
            "source_replay_sha256",
            "packet_digest",
        ):
            value = data.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"invalid review packet {field}")
        failures = data.get("deterministic_failures")
        if not isinstance(failures, list) or any(
            not isinstance(item, str) or not item for item in failures
        ) or len(failures) != len(set(failures)):
            raise ValueError("invalid review packet deterministic failures")
        if sanitize_output(data).value != data:
            raise ValueError("review packet contains unsanitized output material")
        packet = cls(
            schema=data["schema"],
            packet_id=data["packet_id"],
            case_id=data["case_id"],
            sanitized_request=_freeze_json(data["sanitized_request"]),
            expected_contract=_freeze_json(data["expected_contract"]),
            actual_output=_freeze_json(data.get("actual_output")),
            bounded_evidence=_freeze_json(data["bounded_evidence"]),
            deterministic_failures=tuple(failures),
            recorded_verdict=Verdict(data["recorded_verdict"]),
            replayed_verdict=Verdict(data["replayed_verdict"]),
            source_record_sha256=data["source_record_sha256"],
            source_record_canonical_sha256=data[
                "source_record_canonical_sha256"
            ],
            source_evidence_sha256=data["source_evidence_sha256"],
            source_manifest_sha256=data["source_manifest_sha256"],
            source_replay_sha256=data["source_replay_sha256"],
            packet_digest=data["packet_digest"],
        )
        validate_case_id(packet.case_id)
        if packet.recorded_verdict is not packet.replayed_verdict:
            raise ValueError("review packet replay verdict mismatch")
        if packet.replayed_verdict is Verdict.FAIL and not packet.deterministic_failures:
            raise ValueError("failed review packet lacks replayed deterministic failures")
        if packet.replayed_verdict in {Verdict.PASS, Verdict.INCONCLUSIVE} and (
            packet.deterministic_failures
        ):
            raise ValueError("non-failed review packet contains deterministic failures")
        if packet.expected_digest() != packet.packet_digest:
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
        canaries: Iterable[str] = (),
    ) -> ReviewResult:
        if not packet.provenance_verified:
            raise ValueError("semantic review requires an anchored verified packet")
        if verdict not in SEMANTIC_VERDICTS:
            raise ValueError("semantic review verdict must be PASS, FAIL, or INCONCLUSIVE")
        if not review_id or not reviewer_id or not rationale.strip():
            raise ValueError("review identity and rationale are required")
        sanitized_review = sanitize_output(
            {
                "rationale": rationale,
                "evidence_citations": list(evidence_citations),
            },
            canaries=canaries,
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
