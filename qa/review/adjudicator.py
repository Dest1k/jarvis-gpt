"""Fail-closed adjudication that cannot promote a deterministic failure."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from ..models import Verdict
from ..replay import ReplaySummary, load_replay_report
from ..validators.context import ValidationContext
from .reviewer import _exclusive_json, load_review_result
from .schemas import AdjudicationResult, ReviewResult


def adjudicate(
    first: ReviewResult,
    second: ReviewResult,
    *,
    replay_summary: ReplaySummary | None = None,
) -> AdjudicationResult:
    if replay_summary is not None:
        first.packet.verify_replay(replay_summary)
        second.packet.verify_replay(replay_summary)
    if not first.packet.provenance_verified or not second.packet.provenance_verified:
        raise ValueError("adjudication requires anchored verified review packets")
    if first.review_id == second.review_id or first.reviewer_id == second.reviewer_id:
        raise ValueError("adjudication requires two separate review outputs and contexts")
    if first.packet.packet_digest != second.packet.packet_digest:
        raise ValueError("reviews do not refer to the same immutable packet")
    packet = first.packet
    failures = packet.deterministic_failures
    if packet.replayed_verdict is Verdict.FAIL or failures:
        verdict = Verdict.FAIL
        rationale = "Deterministic failures are authoritative and cannot be overridden."
    elif packet.replayed_verdict not in {Verdict.PASS, Verdict.INCONCLUSIVE}:
        verdict = Verdict.INCONCLUSIVE
        rationale = "A non-terminal replay classification cannot be promoted by review."
    elif not packet.bounded_evidence:
        verdict = Verdict.INCONCLUSIVE
        rationale = "Bounded evidence is absent; semantic confidence cannot create PASS."
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
        schema="jarvis.qa.adjudication.v1",
        verdict=verdict,
        rationale=rationale,
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
    )


def write_adjudication(
    path: Path, result: AdjudicationResult, *, canaries: Iterable[str] = ()
) -> None:
    _exclusive_json(path, result.to_dict(), canaries=canaries)
