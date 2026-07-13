from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import qa.safe_paths as safe_paths
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


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "",
        "../escape",
        "/ABSOLUTE",
        "C:/ABSOLUTE",
        "CASE/ESCAPE",
        "CASE\\ESCAPE",
        "C:ESCAPE",
        r"\\server\share",
        r"\\.\NUL",
        r"\\?\C:\escape",
        "CON",
        "CASE..ESCAPE",
    ],
)
def test_review_packet_rejects_unsafe_path_derived_identifiers(unsafe_id: str) -> None:
    record = packet_record()
    record["case_id"] = unsafe_id
    with pytest.raises(ValueError):
        ReviewPacket.create(record)


def test_unsafe_evidence_id_cannot_create_packet_outside_output_root(tmp_path: Path) -> None:
    records = [json.loads(line) for line in CALIBRATION.read_text(encoding="utf-8").splitlines()]
    records[0]["case_id"] = "../ESCAPED"
    evidence = tmp_path / "unsafe.jsonl"
    evidence.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "packets"
    with pytest.raises(ValueError, match="invalid evidence"):
        build_review_packets(evidence, output)
    assert not output.exists()
    assert not (tmp_path / "ESCAPED.review-packet.json").exists()


def test_review_writer_rejects_reparse_output_root(tmp_path: Path) -> None:
    packet = ReviewPacket.create(packet_record())
    result = review(packet, "context-a", Verdict.PASS)
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    try:
        os.symlink(real_root, linked_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this host")
    with pytest.raises(ValueError):
        write_review_result(linked_root / "review.json", result)
    assert not (real_root / "review.json").exists()


def test_review_writer_rejects_simulated_reparse_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = ReviewPacket.create(packet_record())
    result = review(packet, "context-a", Verdict.PASS)
    output_root = tmp_path / "output"
    output_root.mkdir()
    root_stat = os.lstat(output_root)
    original = safe_paths._is_reparse

    def simulated(stat_result: os.stat_result) -> bool:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
        ) == (root_stat.st_dev, root_stat.st_ino) or original(stat_result)

    monkeypatch.setattr(safe_paths, "_is_reparse", simulated)
    with pytest.raises(ValueError):
        write_review_result(output_root / "review.json", result)
    assert not (output_root / "review.json").exists()


def test_review_writer_rejects_nested_reparse_ancestor_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet = ReviewPacket.create(packet_record())
    result = review(packet, "context-a", Verdict.PASS)
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    ancestor_stat = os.lstat(ancestor)
    original = safe_paths._is_reparse

    def simulated(stat_result: os.stat_result) -> bool:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
        ) == (ancestor_stat.st_dev, ancestor_stat.st_ino) or original(stat_result)

    monkeypatch.setattr(safe_paths, "_is_reparse", simulated)
    output = ancestor / "nested" / "review.json"
    with pytest.raises(ValueError):
        write_review_result(output, result)
    assert not output.parent.exists()


def test_cli_validate_and_replay_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate-suite", str(ROOT / "qa" / "suites" / "operator_core")]) == 0
    assert json.loads(capsys.readouterr().out)["scenarios"] == 1
    assert main(["validate-evidence", str(CALIBRATION)]) == 0
    assert json.loads(capsys.readouterr().out)["records"] == 8
    assert main(["replay", str(CALIBRATION)]) == 0
    replay_output = json.loads(capsys.readouterr().out)
    assert replay_output["counts"]["FAIL"] == 6


def test_cli_default_adjudication_output_stays_in_review_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    packet = ReviewPacket.create(packet_record())
    first_path = tmp_path / "review-a.json"
    second_path = tmp_path / "review-b.json"
    write_review_result(first_path, review(packet, "context-a", Verdict.PASS))
    write_review_result(second_path, review(packet, "context-b", Verdict.PASS))

    assert main(["adjudicate", str(first_path), str(second_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    adjudication = tmp_path / "REVIEW-001.adjudication.json"
    assert Path(output["output"]).resolve() == adjudication.resolve()
    assert adjudication.is_file()
