"""Immutable review packet, review result, and adjudication contracts."""

from __future__ import annotations

import hashlib
import json
import re
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
from .independence import IndependenceLevel, ReviewContext

SEMANTIC_VERDICTS = {Verdict.PASS, Verdict.FAIL, Verdict.INCONCLUSIVE}
_PACKET_VERIFICATION_TOKEN = object()
_CITATION_COMPONENT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,126}[A-Za-z0-9])?$")
_METADATA_EVIDENCE_KEYS = frozenset(
    {
        "campaign_id",
        "case_id",
        "context",
        "context_id",
        "metadata",
        "model",
        "namespace",
        "observed_at",
        "observed_at_utc",
        "profile",
        "provider",
        "request_id",
        "review_id",
        "reviewer_id",
        "run_nonce",
        "schema",
        "status",
        "tag",
        "tags",
        "timestamp",
        "trace_id",
        "transport",
        "verdict",
    }
)
_CITABLE_EVIDENCE_FIELDS = frozenset({"kind", "assertion_ids", "content"})
_CITABLE_EVIDENCE_KINDS = frozenset(
    {"artifact", "excerpt", "observation", "reference", "state", "transcript"}
)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_thaw_json(item) for item in value]
    return value


def _citation_component(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _CITATION_COMPONENT.fullmatch(value):
        raise ValueError(f"review packet {label} is not a canonical citation ID")
    return value


def _is_substantive_evidence(value: Any, key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    if normalized in _METADATA_EVIDENCE_KEYS:
        return False
    if isinstance(value, Mapping):
        return any(
            _is_substantive_evidence(item, str(child_key)) for child_key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_is_substantive_evidence(item, key) for item in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and not isinstance(value, bool)


def _citation_catalog(
    record: Mapping[str, Any],
    bounded_evidence: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    assertions = record.get("assertions")
    if not isinstance(assertions, list):
        raise ValueError("review packet assertions must be an array")
    assertion_ids = tuple(
        f"assertion:{_citation_component(assertion.get('name'), 'assertion name')}"
        for assertion in assertions
        if isinstance(assertion, Mapping)
    )
    if len(assertion_ids) != len(assertions) or len(assertion_ids) != len(set(assertion_ids)):
        raise ValueError("review packet assertion IDs are missing or duplicated")
    evidence_ids = _evidence_citation_ids(bounded_evidence, assertion_ids)
    return evidence_ids, assertion_ids


def _evidence_citation_ids(
    bounded_evidence: Mapping[str, Any],
    assertion_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if any(not isinstance(key, str) for key in bounded_evidence):
        raise ValueError("review packet bounded evidence keys must be strings")
    evidence_ids: list[str] = []
    for key, value in sorted(bounded_evidence.items()):
        normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
        if normalized in _METADATA_EVIDENCE_KEYS or not isinstance(value, Mapping):
            continue
        if "kind" not in value:
            continue
        if set(value) != _CITABLE_EVIDENCE_FIELDS:
            raise ValueError("typed bounded evidence fields are incomplete or unexpected")
        if value.get("kind") not in _CITABLE_EVIDENCE_KINDS:
            raise ValueError("typed bounded evidence kind is unsupported")
        linked_assertions = value.get("assertion_ids")
        if (
            not isinstance(linked_assertions, list)
            or not linked_assertions
            or any(not isinstance(item, str) for item in linked_assertions)
            or len(linked_assertions) != len(set(linked_assertions))
            or not set(linked_assertions).issubset(assertion_ids)
        ):
            raise ValueError("typed bounded evidence assertion links are invalid")
        if not _is_substantive_evidence(value.get("content"), key):
            raise ValueError("typed bounded evidence content is metadata-only or empty")
        evidence_ids.append(f"evidence:{_citation_component(key, 'evidence key')}")
    return tuple(evidence_ids)


def _catalog_ids(value: Any, namespace: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"review packet {namespace} IDs must be a string array")
    identifiers = tuple(value)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError(f"review packet {namespace} IDs must be unique")
    prefix = f"{namespace}:"
    for identifier in identifiers:
        if not identifier.startswith(prefix):
            raise ValueError(f"review packet {namespace} ID has the wrong namespace")
        _citation_component(identifier[len(prefix) :], f"{namespace} ID")
    return identifiers


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
    assertions: tuple[Mapping[str, Any], ...]
    evidence_ids: tuple[str, ...]
    assertion_ids: tuple[str, ...]
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

        if not isinstance(replay_case, ReplayCase) or not isinstance(replay_summary, ReplaySummary):
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
        matching_records = [source for source in records if source.get("case_id") == case_id]
        if len(matching_records) != 1 or (
            canonical_record_sha256(matching_records[0]) != canonical_record_sha256(record)
        ):
            raise ValueError("review packet verified source record mismatch")
        matching_cases = [case for case in replay_summary.cases if case.case_id == case_id]
        if matching_cases != [replay_case] or not replay_case.matches:
            raise ValueError("review packet replay case binding mismatch")
        if record.get("verdict") != replay_case.recorded_verdict.value:
            raise ValueError("review packet recorded verdict mismatch")
        if canonical_record_sha256(record) != replay_case.source_record_canonical_sha256:
            raise ValueError("review packet source record binding mismatch")
        recomputed_case = replay_record(
            record,
            source_record_sha256=replay_case.source_record_sha256,
            source_record_canonical_sha256=(replay_case.source_record_canonical_sha256),
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
                "assertions": record.get("assertions", []),
            },
            canaries=canaries,
        ).value
        evidence_ids, assertion_ids = _citation_catalog(
            {"assertions": sanitized["assertions"]},
            sanitized["bounded_evidence"],
        )
        body = {
            "schema": "jarvis.qa.review-packet.v3",
            "packet_id": f"review-{case_id}-{replay_case.source_record_sha256[:12]}",
            "case_id": case_id,
            **sanitized,
            "evidence_ids": list(evidence_ids),
            "assertion_ids": list(assertion_ids),
            "deterministic_failures": list(replay_case.deterministic_failures),
            "recorded_verdict": replay_case.recorded_verdict.value,
            "replayed_verdict": replay_case.replayed_verdict.value,
            "source_record_sha256": replay_case.source_record_sha256,
            "source_record_canonical_sha256": (replay_case.source_record_canonical_sha256),
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
            assertions=_freeze_json(body["assertions"]),
            evidence_ids=evidence_ids,
            assertion_ids=assertion_ids,
            deterministic_failures=tuple(body["deterministic_failures"]),
            recorded_verdict=replay_case.recorded_verdict,
            replayed_verdict=replay_case.replayed_verdict,
            source_record_sha256=replay_case.source_record_sha256,
            source_record_canonical_sha256=(replay_case.source_record_canonical_sha256),
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
            "assertions": _thaw_json(self.assertions),
            "evidence_ids": list(self.evidence_ids),
            "assertion_ids": list(self.assertion_ids),
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
        if not isinstance(replay_summary, ReplaySummary) or not (replay_summary.integrity_verified):
            raise ValueError("packet replay report is not verified")
        if (
            self.source_evidence_sha256 != replay_summary.evidence_sha256
            or self.source_manifest_sha256 != replay_summary.manifest_sha256
            or self.source_replay_sha256 != replay_summary.replay_digest
        ):
            raise ValueError("packet replay report binding mismatch")
        matching = [case for case in replay_summary.cases if case.case_id == self.case_id]
        if len(matching) != 1:
            raise ValueError("packet replay case is missing or duplicated")
        replay_case = matching[0]
        if (
            replay_case.recorded_verdict is not self.recorded_verdict
            or replay_case.replayed_verdict is not self.replayed_verdict
            or replay_case.deterministic_failures != self.deterministic_failures
            or replay_case.source_record_sha256 != self.source_record_sha256
            or replay_case.source_record_canonical_sha256 != self.source_record_canonical_sha256
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
            and canonical_record_sha256(record) == self.source_record_canonical_sha256
        ]
        matching_cases = [
            replay_case
            for replay_case in fresh_replay.cases
            if replay_case.case_id == self.case_id
            and replay_case.source_record_sha256 == self.source_record_sha256
            and replay_case.source_record_canonical_sha256 == self.source_record_canonical_sha256
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
            "assertions",
            "evidence_ids",
            "assertion_ids",
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
        if data.get("schema") != "jarvis.qa.review-packet.v3":
            raise ValueError("unsupported review packet schema")
        if not isinstance(data.get("packet_id"), str) or not data["packet_id"]:
            raise ValueError("invalid review packet identity")
        if not isinstance(data.get("case_id"), str):
            raise ValueError("invalid review packet case identifier")
        for field in ("sanitized_request", "expected_contract", "bounded_evidence"):
            if not isinstance(data.get(field), Mapping):
                raise ValueError(f"review packet {field} must be an object")
        raw_assertions = data.get("assertions")
        if not isinstance(raw_assertions, list) or any(
            not isinstance(assertion, Mapping) for assertion in raw_assertions
        ):
            raise ValueError("review packet assertions must be an object array")
        evidence_ids = _catalog_ids(data.get("evidence_ids"), "evidence")
        assertion_ids = _catalog_ids(data.get("assertion_ids"), "assertion")
        expected_evidence_ids, expected_assertion_ids = _citation_catalog(
            {"assertions": raw_assertions},
            data["bounded_evidence"],
        )
        if evidence_ids != expected_evidence_ids or assertion_ids != expected_assertion_ids:
            raise ValueError("review packet citation catalog mismatch")
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
        if (
            not isinstance(failures, list)
            or any(not isinstance(item, str) or not item for item in failures)
            or len(failures) != len(set(failures))
        ):
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
            assertions=_freeze_json(raw_assertions),
            evidence_ids=evidence_ids,
            assertion_ids=assertion_ids,
            deterministic_failures=tuple(failures),
            recorded_verdict=Verdict(data["recorded_verdict"]),
            replayed_verdict=Verdict(data["replayed_verdict"]),
            source_record_sha256=data["source_record_sha256"],
            source_record_canonical_sha256=data["source_record_canonical_sha256"],
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


_REVIEW_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,254}[A-Za-z0-9])?$")


def _review_identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _REVIEW_IDENTIFIER.fullmatch(value):
        raise ValueError(f"review {label} is not a canonical identifier")
    return value


def _validate_review_citations(
    packet: ReviewPacket,
    verdict: Verdict,
    citations: tuple[str, ...],
) -> None:
    if not citations or any(not isinstance(item, str) or not item for item in citations):
        raise ValueError("review citations must be non-empty strings")
    if len(citations) != len(set(citations)):
        raise ValueError("review citations must be unique")
    allowed = set(packet.evidence_ids + packet.assertion_ids)
    unknown = sorted(set(citations) - allowed)
    if unknown:
        raise ValueError("review cites an unknown packet evidence or assertion ID")
    if verdict in {Verdict.PASS, Verdict.FAIL}:
        if not any(item in packet.evidence_ids for item in citations):
            raise ValueError("substantive review requires an evidence citation")
        if not any(item in packet.assertion_ids for item in citations):
            raise ValueError("substantive review requires an assertion citation")


@dataclass(frozen=True, slots=True)
class ReviewResult:
    schema: str
    review_id: str
    reviewer_id: str
    context: ReviewContext
    packet_digest: str
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
        context: ReviewContext,
        verdict: Verdict,
        rationale: str,
        evidence_citations: tuple[str, ...],
        packet: ReviewPacket,
        canaries: Iterable[str] = (),
    ) -> ReviewResult:
        if not packet.provenance_verified:
            raise ValueError("semantic review requires an anchored verified packet")
        if not isinstance(context, ReviewContext) or not context.verified:
            raise ValueError("semantic review requires a verified factual context")
        if verdict not in SEMANTIC_VERDICTS:
            raise ValueError("semantic review verdict must be PASS, FAIL, or INCONCLUSIVE")
        review_id = _review_identity(review_id, "review_id")
        reviewer_id = _review_identity(reviewer_id, "reviewer_id")
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError("review rationale is required")
        if not isinstance(evidence_citations, tuple):
            raise ValueError("review citations must be an immutable tuple")
        sanitized_review = sanitize_output(
            {
                "rationale": rationale,
                "evidence_citations": list(evidence_citations),
            },
            canaries=canaries,
        ).value
        sanitized_rationale = sanitized_review["rationale"]
        sanitized_items = sanitized_review["evidence_citations"]
        if (
            not isinstance(sanitized_rationale, str)
            or not isinstance(sanitized_items, list)
            or any(not isinstance(item, str) for item in sanitized_items)
        ):
            raise ValueError("review sanitization changed required field types")
        sanitized_citations = tuple(sanitized_items)
        _validate_review_citations(packet, verdict, sanitized_citations)
        created = datetime.now(UTC).isoformat()
        body = {
            "schema": "jarvis.qa.review-result.v2",
            "review_id": review_id,
            "reviewer_id": reviewer_id,
            "context": context.to_dict(),
            "packet_digest": packet.packet_digest,
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
            context=context,
            packet_digest=packet.packet_digest,
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
            "context": self.context.to_dict(),
            "packet_digest": self.packet_digest,
            "verdict": self.verdict.value,
            "rationale": self.rationale,
            "evidence_citations": list(self.evidence_citations),
            "packet": self.packet.to_dict(),
            "created_at_utc": self.created_at_utc,
            "review_digest": self.review_digest,
        }

    def expected_digest(self) -> str:
        body = self.to_dict()
        body.pop("review_digest")
        return canonical_digest(body)

    def validate_integrity(self) -> None:
        if self.schema != "jarvis.qa.review-result.v2":
            raise ValueError("unsupported review result schema")
        _review_identity(self.review_id, "review_id")
        _review_identity(self.reviewer_id, "reviewer_id")
        if self.verdict not in SEMANTIC_VERDICTS:
            raise ValueError("invalid semantic review verdict")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("review rationale is required")
        if not isinstance(self.created_at_utc, str) or not self.created_at_utc:
            raise ValueError("review creation time is required")
        if (
            not isinstance(self.review_digest, str)
            or len(self.review_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.review_digest)
        ):
            raise ValueError("review digest is invalid")
        if not self.context.verified:
            raise ValueError("review context digest mismatch")
        if self.packet_digest != self.packet.packet_digest:
            raise ValueError("review packet digest binding mismatch")
        if self.packet.expected_digest() != self.packet.packet_digest:
            raise ValueError("review embedded packet digest mismatch")
        _validate_review_citations(self.packet, self.verdict, self.evidence_citations)
        if self.expected_digest() != self.review_digest:
            raise ValueError("review result digest mismatch")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewResult:
        fields = {
            "schema",
            "review_id",
            "reviewer_id",
            "context",
            "packet_digest",
            "verdict",
            "rationale",
            "evidence_citations",
            "packet",
            "created_at_utc",
            "review_digest",
        }
        if set(data) != fields:
            raise ValueError("review result fields are incomplete or unexpected")
        if data.get("schema") != "jarvis.qa.review-result.v2":
            raise ValueError("unsupported review result schema")
        if not isinstance(data.get("context"), Mapping):
            raise ValueError("review context must be an object")
        if not isinstance(data.get("packet"), Mapping):
            raise ValueError("review packet must be an object")
        citations = data.get("evidence_citations")
        if not isinstance(citations, list) or any(not isinstance(item, str) for item in citations):
            raise ValueError("review citations must be a string array")
        if not isinstance(data.get("rationale"), str) or not data["rationale"].strip():
            raise ValueError("review rationale is required")
        if not isinstance(data.get("created_at_utc"), str) or not data["created_at_utc"]:
            raise ValueError("review creation time is required")
        digest = data.get("review_digest")
        packet_digest = data.get("packet_digest")
        for label, value in (("review", digest), ("packet", packet_digest)):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"review {label} digest is invalid")
        packet = ReviewPacket.from_dict(data["packet"])
        try:
            verdict = Verdict(data["verdict"])
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid semantic review verdict") from exc
        if verdict not in SEMANTIC_VERDICTS:
            raise ValueError("invalid semantic review verdict")
        result = cls(
            schema=data["schema"],
            review_id=_review_identity(data.get("review_id"), "review_id"),
            reviewer_id=_review_identity(data.get("reviewer_id"), "reviewer_id"),
            context=ReviewContext.from_dict(data["context"]),
            packet_digest=packet_digest,
            verdict=verdict,
            rationale=data["rationale"],
            evidence_citations=tuple(citations),
            packet=packet,
            created_at_utc=data["created_at_utc"],
            review_digest=digest,
        )
        if sanitize_output(data).value != data:
            raise ValueError("review result contains unsanitized output material")
        result.validate_integrity()
        return result


@dataclass(frozen=True, slots=True)
class AdjudicationResult:
    schema: str
    verdict: Verdict
    rationale: str
    independence_verified: bool
    independence_level: IndependenceLevel | None
    independence_reason: str
    context_anchor_sha256s: tuple[str, ...]
    review_anchors_verified: bool
    review_anchor_sha256s: tuple[str, ...]
    deterministic_failures_preserved: tuple[str, ...]
    reviews: tuple[ReviewResult, ReviewResult]
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "verdict": self.verdict.value,
            "rationale": self.rationale,
            "independence_verified": self.independence_verified,
            "independence_level": (
                self.independence_level.value if self.independence_level else None
            ),
            "independence_reason": self.independence_reason,
            "context_anchor_sha256s": list(self.context_anchor_sha256s),
            "review_anchors_verified": self.review_anchors_verified,
            "review_anchor_sha256s": list(self.review_anchor_sha256s),
            "deterministic_failures_preserved": list(self.deterministic_failures_preserved),
            "reviews": [review.to_dict() for review in self.reviews],
            "created_at_utc": self.created_at_utc,
        }
