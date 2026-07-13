"""Fail-closed adjudication that cannot promote a deterministic failure."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from ..models import Verdict
from ..replay import ReplaySummary, load_replay_report
from ..validators.context import ValidationContext
from .independence import assess_independence
from .reviewer import _exclusive_json, load_review_result
from .schemas import AdjudicationResult, ReviewResult

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _anchor_pair(
    value: tuple[str, str] | None,
    label: str,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or any(not isinstance(item, str) or not _DIGEST.fullmatch(item) for item in value)
    ):
        raise ValueError(f"{label} must contain two lowercase SHA-256 digests")
    return value


def _verify_review_anchors(
    anchors: tuple[str, ...],
    first: ReviewResult,
    second: ReviewResult,
) -> tuple[bool, str]:
    if not anchors:
        return False, "Two out-of-band review result anchors are required."
    if anchors[0] == anchors[1]:
        return False, "Review result anchor was reused."
    if anchors != (first.review_digest, second.review_digest):
        return False, "Review result anchor mismatch."
    return True, "Review result digests match separately retained anchors."


def adjudicate(
    first: ReviewResult,
    second: ReviewResult,
    *,
    replay_summary: ReplaySummary | None = None,
    expected_context_digests: tuple[str, str] | None = None,
    expected_review_digests: tuple[str, str] | None = None,
) -> AdjudicationResult:
    first.validate_integrity()
    second.validate_integrity()
    if replay_summary is not None:
        first.packet.verify_replay(replay_summary)
        second.packet.verify_replay(replay_summary)
    if not first.packet.provenance_verified or not second.packet.provenance_verified:
        raise ValueError("adjudication requires anchored verified review packets")
    if (
        first.review_id.casefold() == second.review_id.casefold()
        or first.reviewer_id.casefold() == second.reviewer_id.casefold()
    ):
        raise ValueError("adjudication requires two separate review outputs and contexts")
    if first.packet.packet_digest != second.packet.packet_digest:
        raise ValueError("reviews do not refer to the same immutable packet")
    if first.packet_digest != second.packet_digest:
        raise ValueError("reviews do not bind the same packet digest")

    context_anchors = _anchor_pair(expected_context_digests, "context anchors")
    review_anchors = _anchor_pair(expected_review_digests, "review result anchors")
    context_independence = assess_independence(
        first.context,
        second.context,
        expected_context_digests=(
            (context_anchors[0], context_anchors[1]) if context_anchors else None
        ),
    )
    review_anchors_verified, review_anchor_reason = _verify_review_anchors(
        review_anchors,
        first,
        second,
    )
    independence_verified = context_independence.verified and review_anchors_verified
    independence_level = context_independence.level if independence_verified else None
    independence_reason = (
        context_independence.reason
        if not context_independence.verified
        else review_anchor_reason
        if not review_anchors_verified
        else "Review contexts and result digests match separately retained anchors."
    )

    packet = first.packet
    failures = packet.deterministic_failures
    if packet.replayed_verdict is Verdict.FAIL or failures:
        verdict = Verdict.FAIL
        rationale = "Deterministic failures are authoritative and cannot be overridden."
    elif packet.replayed_verdict not in {Verdict.PASS, Verdict.INCONCLUSIVE}:
        verdict = Verdict.INCONCLUSIVE
        rationale = "A non-terminal replay classification cannot be promoted by review."
    elif not independence_verified:
        verdict = Verdict.INCONCLUSIVE
        rationale = "Review independence or anchored output integrity is not verifiable."
    elif not packet.evidence_ids:
        verdict = Verdict.INCONCLUSIVE
        rationale = "Substantive bounded evidence is absent; review cannot create PASS."
    elif first.verdict is not second.verdict:
        verdict = Verdict.INCONCLUSIVE
        rationale = "Semantic reviewers disagree; both original assessments are preserved."
    elif first.verdict is Verdict.PASS:
        verdict = Verdict.PASS
        rationale = "Both reviews pass and bounded evidence is present."
    elif first.verdict is Verdict.FAIL:
        verdict = Verdict.FAIL
        rationale = "Both semantic reviews fail."
    else:
        verdict = Verdict.INCONCLUSIVE
        rationale = "Both reviews are inconclusive."
    return AdjudicationResult(
        schema="jarvis.qa.adjudication.v2",
        verdict=verdict,
        rationale=rationale,
        independence_verified=independence_verified,
        independence_level=independence_level,
        independence_reason=independence_reason,
        context_anchor_sha256s=context_anchors,
        review_anchors_verified=review_anchors_verified,
        review_anchor_sha256s=review_anchors,
        deterministic_failures_preserved=failures,
        reviews=(first, second),
        created_at_utc=datetime.now(UTC).isoformat(),
    )


def adjudicate_files(
    first_path: Path,
    second_path: Path,
    *,
    replay_path: Path,
    evidence_path: Path,
    expected_context_digests: tuple[str, str],
    expected_review_digests: tuple[str, str],
    expected_manifest_sha256: str | None = None,
    context: ValidationContext | None = None,
) -> AdjudicationResult:
    replay = load_replay_report(
        replay_path,
        evidence_path=evidence_path,
        expected_manifest_sha256=expected_manifest_sha256,
        context=context,
    )
    first = load_review_result(first_path)
    second = load_review_result(second_path)
    for review in (first, second):
        review.packet.verify_source(
            evidence_path,
            expected_manifest_sha256=expected_manifest_sha256,
            replay_summary=replay,
            replay_context=context,
        )
    return adjudicate(
        first,
        second,
        replay_summary=replay,
        expected_context_digests=expected_context_digests,
        expected_review_digests=expected_review_digests,
    )


def write_adjudication(
    path: Path,
    result: AdjudicationResult,
    *,
    expected_context_digests: tuple[str, str],
    expected_review_digests: tuple[str, str],
    canaries: Iterable[str] = (),
) -> None:
    verified = adjudicate(
        *result.reviews,
        expected_context_digests=expected_context_digests,
        expected_review_digests=expected_review_digests,
    )
    decision_fields = (
        "schema",
        "verdict",
        "rationale",
        "independence_verified",
        "independence_level",
        "independence_reason",
        "context_anchor_sha256s",
        "review_anchors_verified",
        "review_anchor_sha256s",
        "deterministic_failures_preserved",
        "reviews",
    )
    if any(getattr(result, field) != getattr(verified, field) for field in decision_fields):
        raise ValueError("adjudication result does not match verified review inputs")
    _exclusive_json(path, result.to_dict(), canaries=canaries)
