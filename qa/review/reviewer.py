"""Read-only reviewer interface and immutable packet/review writers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from ..evidence import validate_evidence_file
from ..models import Verdict
from ..safe_paths import canonical_directory, create_exclusive_directory, safe_output_path
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


def _exclusive_json(path: Path, document: dict[str, object]) -> Path:
    root = canonical_directory(path.parent, create=True)
    target = safe_output_path(root, path.name)
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return target


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
    exact_root = create_exclusive_directory(output_dir)
    outputs: list[Path] = []
    for record in records:
        packet = ReviewPacket.create(record)
        path = exact_root / f"{packet.case_id}.review-packet.json"
        outputs.append(_exclusive_json(path, packet.to_dict()))
    return outputs
