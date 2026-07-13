"""Read-only reviewer interface and immutable packet/review writers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from ..evidence import validate_evidence_file
from ..models import Verdict
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
    ) -> None:
        self.reviewer_id = reviewer_id
        self.independence_level = independence_level
        self.verdict = verdict
        self.rationale = rationale

    def review(self, packet: ReviewPacket) -> ReviewResult:
        return ReviewResult.create(
            review_id=f"{packet.packet_id}-{self.reviewer_id}",
            reviewer_id=self.reviewer_id,
            independence_level=self.independence_level,
            verdict=self.verdict,
            rationale=self.rationale,
            evidence_citations=tuple(sorted(packet.bounded_evidence)),
            packet=packet,
        )


def _exclusive_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def write_review_result(path: Path, review: ReviewResult) -> None:
    _exclusive_json(path, review.to_dict())


def load_review_result(path: Path) -> ReviewResult:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("review result must be an object")
    return ReviewResult.from_dict(document)


def build_review_packets(evidence_path: Path, output_dir: Path) -> list[Path]:
    records, errors = validate_evidence_file(evidence_path)
    if errors:
        raise ValueError(f"invalid evidence: {'; '.join(errors)}")
    output_dir.mkdir(parents=True, exist_ok=False)
    outputs: list[Path] = []
    for record in records:
        packet = ReviewPacket.create(record)
        path = output_dir / f"{packet.case_id}.review-packet.json"
        _exclusive_json(path, packet.to_dict())
        outputs.append(path)
    return outputs
