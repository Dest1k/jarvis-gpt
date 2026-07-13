from __future__ import annotations

import json
from pathlib import Path

import pytest

from qa.cli import main
from qa.models import Verdict
from qa.review.adjudicator import adjudicate
from qa.review.independence import IndependenceLevel, is_independent_model
from qa.review.reviewer import (
    SyntheticReviewer,
    build_review_packets,
    load_review_result,
    write_review_result,
)
from qa.review.schemas import ReviewPacket, ReviewResult

ROOT = Path(__file__).resolve().parents[2]
CALIBRATION = Path(__file__).parent / "fixtures" / "calibration_evidence.jsonl"


def packet_record(*, failures: list[str] | None = None, evidence: bool = True) -> dict[str, object]:
    return {
        "case_id": "REVIEW-001",
        "sanitized_request": {"prompt": "review this"},
        "expected_contract": {"useful": True},
        "observation": {"final": "answer"},
        "bounded_evidence": {"excerpt": "bounded"} if evidence else {},
        "deterministic_failures": failures or [],
    }


def review(
    packet: ReviewPacket, reviewer_id: str, verdict: Verdict, rationale: str = "bounded review"
) -> ReviewResult:
    return ReviewResult.create(
        review_id=f"review-{reviewer_id}",
        reviewer_id=reviewer_id,
        independence_level=IndependenceLevel.SAME_MODEL_CLEAN_CONTEXT,
        verdict=verdict,
        rationale=rationale,
        evidence_citations=("excerpt",),
        packet=packet,
    )


def test_review_outputs_are_separate_immutable_and_digest_checked(tmp_path: Path) -> None:
    packet = ReviewPacket.create(packet_record())
    first = SyntheticReviewer(
        "context-a",
        IndependenceLevel.SAME_MODEL_CLEAN_CONTEXT,
        Verdict.PASS,
        "review A",
    ).review(packet)
    second = SyntheticReviewer(
        "context-b",
        IndependenceLevel.DIFFERENT_MODEL,
        Verdict.FAIL,
        "review B",
    ).review(packet)
    first_path = tmp_path / "review-a.json"
    second_path = tmp_path / "review-b.json"
    write_review_result(first_path, first)
    write_review_result(second_path, second)
    with pytest.raises(FileExistsError):
        write_review_result(first_path, first)
    assert first_path.read_bytes() != second_path.read_bytes()
    assert load_review_result(first_path) == first
    assert is_independent_model(second.independence_level)
    assert not is_independent_model(first.independence_level)

    tampered = json.loads(first_path.read_text(encoding="utf-8"))
    tampered["rationale"] = "changed after review"
    first_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_review_result(first_path)


def test_adjudicator_preserves_disagreement_and_deterministic_fail() -> None:
    packet = ReviewPacket.create(packet_record())
    disagreement = adjudicate(review(packet, "a", Verdict.PASS), review(packet, "b", Verdict.FAIL))
    assert disagreement.verdict is Verdict.INCONCLUSIVE
    assert len(disagreement.reviews) == 2

    failed_packet = ReviewPacket.create(packet_record(failures=["response.leak"]))
    authoritative = adjudicate(
        review(failed_packet, "a", Verdict.PASS), review(failed_packet, "b", Verdict.PASS)
    )
    assert authoritative.verdict is Verdict.FAIL
    assert authoritative.deterministic_failures_preserved == ("response.leak",)


def test_adjudicator_never_passes_without_evidence() -> None:
    packet = ReviewPacket.create(packet_record(evidence=False))
    result = adjudicate(review(packet, "a", Verdict.PASS), review(packet, "b", Verdict.PASS))
    assert result.verdict is Verdict.INCONCLUSIVE


def test_build_review_packets_is_exclusive(tmp_path: Path) -> None:
    output = tmp_path / "packets"
    paths = build_review_packets(CALIBRATION, output)
    assert len(paths) == 8
    assert all(path.is_file() for path in paths)
    with pytest.raises(FileExistsError):
        build_review_packets(CALIBRATION, output)


def test_cli_validate_and_replay_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate-suite", str(ROOT / "qa" / "suites" / "operator_core")]) == 0
    assert json.loads(capsys.readouterr().out)["scenarios"] == 1
    assert main(["validate-evidence", str(CALIBRATION)]) == 0
    assert json.loads(capsys.readouterr().out)["records"] == 8
    assert main(["replay", str(CALIBRATION)]) == 0
    replay_output = json.loads(capsys.readouterr().out)
    assert replay_output["counts"]["FAIL"] == 6
