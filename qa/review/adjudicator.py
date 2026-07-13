"""Fail-closed adjudication that cannot promote a deterministic failure."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ..models import Verdict
from .reviewer import load_review_result
from .schemas import AdjudicationResult, ReviewResult


def adjudicate(first: ReviewResult, second: ReviewResult) -> AdjudicationResult:
    if first.review_id == second.review_id or first.reviewer_id == second.reviewer_id:
        raise ValueError("adjudication requires two separate review outputs and contexts")
    if first.packet.packet_digest != second.packet.packet_digest:
        raise ValueError("reviews do not refer to the same immutable packet")
    packet = first.packet
    failures = packet.deterministic_failures
    if failures:
        verdict = Verdict.FAIL
        rationale = "Deterministic failures are authoritative and cannot be overridden."
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


def adjudicate_files(first_path: Path, second_path: Path) -> AdjudicationResult:
    return adjudicate(load_review_result(first_path), load_review_result(second_path))


def write_adjudication(path: Path, result: AdjudicationResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result.to_dict(), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
