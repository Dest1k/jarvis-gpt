"""Read-only reviewer interface and immutable packet/review writers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from ..evidence import verify_evidence_bundle
from ..models import Verdict
from ..output import sanitize_output, write_json_exclusive
from ..replay import load_replay_report, replay_records
from ..safe_paths import canonical_directory, create_exclusive_directory, safe_output_path
from ..validators.context import ValidationContext
from .independence import IndependenceLevel
from .schemas import ReviewPacket, ReviewResult


class Reviewer(Protocol):
    reviewer_id: str
    independence_level: IndependenceLevel

    def review(self, packet: ReviewPacket) -> ReviewResult: ...


class SyntheticReviewer:
    """Deterministic adapter used only by offline fixtures and contract tests."""

    def __init__(
        self,
        reviewer_id: str,
        independence_level: IndependenceLevel,
        verdict: Verdict,
        rationale: str,
        canaries: Iterable[str] = (),
    ) -> None:
        self.reviewer_id = reviewer_id
        self.independence_level = independence_level
        self.verdict = verdict
        self.rationale = rationale
        self.canaries = tuple(canaries)

    def review(self, packet: ReviewPacket) -> ReviewResult:
        return ReviewResult.create(
            review_id=f"{packet.packet_id}-{self.reviewer_id}",
            reviewer_id=self.reviewer_id,
            independence_level=self.independence_level,
            verdict=self.verdict,
            rationale=self.rationale,
            evidence_citations=tuple(sorted(packet.bounded_evidence)),
            packet=packet,
            canaries=self.canaries,
        )


def _exclusive_json(
    path: Path,
    document: dict[str, object],
    *,
    canaries: Iterable[str] = (),
    require_unchanged: bool = True,
) -> Path:
    root = canonical_directory(path.parent, create=True)
    target = safe_output_path(root, path.name)
    canary_values = tuple(canaries)
    if require_unchanged and sanitize_output(
        document, canaries=canary_values
    ).value != document:
        raise ValueError("digest-bearing output was not sanitized before persistence")
    write_json_exclusive(target, document, canaries=canary_values)
    return target


def write_review_result(
    path: Path, review: ReviewResult, *, canaries: Iterable[str] = ()
) -> None:
    if not review.packet.provenance_verified:
        raise ValueError("review output requires an anchored verified packet")
    _exclusive_json(path, review.to_dict(), canaries=canaries)


def load_review_result(path: Path) -> ReviewResult:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("review result must be an object")
    return ReviewResult.from_dict(document)


def build_review_packets(
    evidence_path: Path,
    output_dir: Path,
    *,
    canaries: Iterable[str] = (),
    validation_context: ValidationContext | None = None,
    expected_manifest_sha256: str | None = None,
) -> list[Path]:
    records, errors, integrity = verify_evidence_bundle(
        evidence_path,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    if errors:
        raise ValueError(f"invalid evidence: {'; '.join(errors)}")
    if integrity is None:
        raise ValueError("invalid evidence: integrity was not established")
    replay = replay_records(records, integrity, context=validation_context)
    if not replay.integrity_verified:
        raise ValueError("review packets require a fresh verified deterministic replay")
    exact_root = create_exclusive_directory(output_dir)
    replay_path = _exclusive_json(
        exact_root / "REPLAY.json",
        replay.to_dict(),
        canaries=canaries,
    )
    persisted_replay = load_replay_report(
        replay_path,
        evidence_path=evidence_path,
        expected_manifest_sha256=expected_manifest_sha256,
        context=validation_context,
    )
    if persisted_replay.replay_digest != replay.replay_digest:
        raise ValueError("persisted replay report binding mismatch")
    outputs: list[Path] = []
    for record, replay_case in zip(records, replay.cases, strict=True):
        packet = ReviewPacket.create(
            record,
            replay_case=replay_case,
            replay_summary=replay,
            evidence_path=evidence_path,
            expected_manifest_sha256=expected_manifest_sha256,
            replay_context=validation_context,
            canaries=canaries,
        )
        packet.verify_replay(persisted_replay)
        path = exact_root / f"{packet.case_id}.review-packet.json"
        outputs.append(_exclusive_json(path, packet.to_dict(), canaries=canaries))
    return outputs
