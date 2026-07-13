from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

import qa.safe_paths as safe_paths
from qa.cli import _emit, main
from qa.evidence import EvidenceStore, verify_evidence_bundle
from qa.models import (
    AssertionResult,
    CampaignIdentity,
    CampaignSummary,
    CaseResult,
    Scenario,
    Verdict,
)
from qa.redaction import credential_like_paths
from qa.replay import (
    ReplaySummary,
    load_replay_report,
    replay_file,
    replay_records,
    write_replay_report,
)
from qa.review.adjudicator import adjudicate, adjudicate_files, write_adjudication
from qa.review.independence import IndependenceLevel, is_independent_model
from qa.review.reviewer import (
    SyntheticReviewer,
    build_review_packets,
    load_review_result,
    write_review_result,
)
from qa.review.schemas import ReviewPacket, ReviewResult, canonical_digest
from qa.schema_validation import validate_json_schema

ROOT = Path(__file__).resolve().parents[2]
CALIBRATION = Path(__file__).parent / "fixtures" / "calibration_evidence.jsonl"


def bound_source(
    case_id: str = "CAL-PASS-STREAM",
) -> tuple[dict[str, object], object, object]:
    records, errors, integrity = verify_evidence_bundle(CALIBRATION)
    assert errors == []
    assert integrity is not None
    replay = replay_records(records, integrity)
    assert replay.integrity_verified
    index = [record["case_id"] for record in records].index(case_id)
    return records[index], replay.cases[index], replay


def bound_packet(case_id: str = "CAL-PASS-STREAM") -> ReviewPacket:
    record, replay_case, replay = bound_source(case_id)
    return ReviewPacket.create(
        record,
        replay_case=replay_case,
        replay_summary=replay,
        evidence_path=CALIBRATION,
    )


def generated_packet_bundle(
    tmp_path: Path, *, bounded_evidence: dict[str, object]
) -> tuple[ReviewPacket, Path, str, ReplaySummary]:
    identity = CampaignIdentity.create("review-test")
    scenario = Scenario.from_dict(
        {
            "scenario_id": "REVIEW-GENERATED-001",
            "title": "generated review fixture",
            "transport": "offline",
            "request": {"observation": {"final": "answer"}},
            "expected_contract": {"useful": True},
            "validators": [{"kind": "format_contract", "exact": "answer"}],
        }
    )
    result = CaseResult(
        case_id=scenario.scenario_id,
        verdict=Verdict.PASS,
        assertions=(AssertionResult("format.exact", True, "answer", "answer"),),
        observation={"final": "answer"},
        bounded_evidence=bounded_evidence,
    )
    store = EvidenceStore(tmp_path / "evidence", identity)
    store.append(scenario, result)
    anchor = store.write_manifest(CampaignSummary(identity, (result,)))
    records, errors, integrity = verify_evidence_bundle(
        store.path,
        expected_manifest_sha256=anchor.manifest_sha256,
    )
    assert errors == []
    assert integrity is not None
    replay = replay_records(records, integrity)
    packet = ReviewPacket.create(
        records[0],
        replay_case=replay.cases[0],
        replay_summary=replay,
        evidence_path=store.path,
        expected_manifest_sha256=anchor.manifest_sha256,
    )
    return packet, store.path, anchor.manifest_sha256, replay


def generated_packet(tmp_path: Path, *, bounded_evidence: dict[str, object]) -> ReviewPacket:
    return generated_packet_bundle(
        tmp_path,
        bounded_evidence=bounded_evidence,
    )[0]


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
    packet = bound_packet()
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
    packet = bound_packet()
    disagreement = adjudicate(review(packet, "a", Verdict.PASS), review(packet, "b", Verdict.FAIL))
    assert disagreement.verdict is Verdict.INCONCLUSIVE
    assert len(disagreement.reviews) == 2

    failed_packet = bound_packet("CAL-FAIL-TOOL-ENVELOPE")
    authoritative = adjudicate(
        review(failed_packet, "a", Verdict.PASS), review(failed_packet, "b", Verdict.PASS)
    )
    assert authoritative.verdict is Verdict.FAIL
    assert "response.no_internal_markers" in authoritative.deterministic_failures_preserved


def test_paired_packet_and_review_substitution_cannot_promote_fail() -> None:
    source_packet = bound_packet("CAL-FAIL-TOOL-ENVELOPE")
    packet_document = source_packet.to_dict()
    packet_document["recorded_verdict"] = Verdict.PASS.value
    packet_document["replayed_verdict"] = Verdict.PASS.value
    packet_document["deterministic_failures"] = []
    packet_body = dict(packet_document)
    packet_body.pop("packet_digest")
    packet_document["packet_digest"] = canonical_digest(packet_body)

    forged_reviews: list[ReviewResult] = []
    for reviewer_id in ("forged-a", "forged-b"):
        document = review(
            source_packet,
            reviewer_id,
            Verdict.PASS,
        ).to_dict()
        document["packet"] = packet_document
        review_body = dict(document)
        review_body.pop("review_digest")
        document["review_digest"] = canonical_digest(review_body)
        forged_reviews.append(ReviewResult.from_dict(document))
    assert all(not item.packet.provenance_verified for item in forged_reviews)

    with pytest.raises(ValueError, match="anchored verified review packets"):
        adjudicate(forged_reviews[0], forged_reviews[1])
    with pytest.raises(ValueError, match="packet replay case binding mismatch"):
        adjudicate(
            forged_reviews[0],
            forged_reviews[1],
            replay_summary=replay_file(CALIBRATION),
        )


def test_review_packet_content_is_deeply_immutable(tmp_path: Path) -> None:
    packet = generated_packet(
        tmp_path,
        bounded_evidence={"source": {"detail": ["sanitized fixture"]}},
    )
    nested = packet.bounded_evidence["source"]
    assert isinstance(nested, Mapping)
    assert isinstance(nested["detail"], tuple)
    with pytest.raises(TypeError):
        nested["detail"] = "changed"
    assert packet.provenance_verified


def test_persisted_bounded_evidence_substitution_requires_full_source_rebind(
    tmp_path: Path,
) -> None:
    packet, evidence_path, manifest_sha256, replay = generated_packet_bundle(
        tmp_path,
        bounded_evidence={},
    )
    replay_path = tmp_path / "replay.json"
    write_replay_report(replay_path, replay)
    review_paths: list[Path] = []
    loaded_reviews: list[ReviewResult] = []
    for reviewer_id in ("forged-content-a", "forged-content-b"):
        document = review(packet, reviewer_id, Verdict.PASS).to_dict()
        packet_document = document["packet"]
        packet_document["bounded_evidence"] = {"source": "substituted fixture"}
        packet_body = dict(packet_document)
        packet_body.pop("packet_digest")
        packet_document["packet_digest"] = canonical_digest(packet_body)
        review_body = dict(document)
        review_body.pop("review_digest")
        document["review_digest"] = canonical_digest(review_body)
        path = tmp_path / f"{reviewer_id}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        review_paths.append(path)
        loaded_reviews.append(load_review_result(path))

    for loaded in loaded_reviews:
        loaded.packet.verify_replay(replay)
        assert not loaded.packet.provenance_verified
        with pytest.raises(ValueError, match="source content mismatch"):
            loaded.packet.verify_source(
                evidence_path,
                expected_manifest_sha256=manifest_sha256,
                replay_summary=replay,
            )
    with pytest.raises(ValueError, match="source content mismatch"):
        adjudicate_files(
            review_paths[0],
            review_paths[1],
            replay_path=replay_path,
            evidence_path=evidence_path,
            expected_manifest_sha256=manifest_sha256,
        )


def test_adjudicator_never_passes_without_evidence(tmp_path: Path) -> None:
    packet = generated_packet(tmp_path, bounded_evidence={})
    result = adjudicate(review(packet, "a", Verdict.PASS), review(packet, "b", Verdict.PASS))
    assert result.verdict is Verdict.INCONCLUSIVE


def test_build_review_packets_is_exclusive(tmp_path: Path) -> None:
    output = tmp_path / "packets"
    paths = build_review_packets(CALIBRATION, output)
    assert len(paths) == 8
    assert all(path.is_file() for path in paths)
    persisted_replay = load_replay_report(
        output / "REPLAY.json",
        evidence_path=CALIBRATION,
    )
    assert persisted_replay.integrity_verified
    for path in paths:
        packet = ReviewPacket.from_dict(json.loads(path.read_text(encoding="utf-8")))
        packet.verify_source(
            CALIBRATION,
            replay_summary=persisted_replay,
        )
        assert packet.provenance_verified
    with pytest.raises(FileExistsError):
        build_review_packets(CALIBRATION, output)


def test_review_packet_binds_fresh_replay_and_rejects_substitution() -> None:
    record, replay_case, replay = bound_source()
    packet = ReviewPacket.create(
        record,
        replay_case=replay_case,
        replay_summary=replay,
        evidence_path=CALIBRATION,
    )
    assert packet.source_record_sha256 == replay_case.source_record_sha256
    assert (
        packet.source_record_canonical_sha256
        == replay_case.source_record_canonical_sha256
    )
    assert packet.source_evidence_sha256 == replay.evidence_sha256
    assert packet.source_manifest_sha256 == replay.manifest_sha256
    assert packet.source_replay_sha256 == replay.replay_digest
    packet.verify_replay(replay)
    packet_schema = json.loads(
        (ROOT / "qa" / "schemas" / "review-packet.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert validate_json_schema(packet.to_dict(), packet_schema) == []

    substituted = replace(replay, replay_digest="0" * 64)
    with pytest.raises(ValueError, match="fresh verified replay"):
        ReviewPacket.create(
            record,
            replay_case=replay_case,
            replay_summary=substituted,
            evidence_path=CALIBRATION,
        )
    substituted = replace(replay, evidence_sha256="f" * 64, replay_digest="")
    substituted = replace(substituted, replay_digest=substituted.expected_digest())
    assert substituted.integrity_verified is False
    with pytest.raises(ValueError, match="not verified"):
        packet.verify_replay(substituted)


def test_forged_integrity_and_serialized_replay_cannot_create_packet(
    tmp_path: Path,
) -> None:
    records, errors, integrity = verify_evidence_bundle(CALIBRATION)
    assert errors == []
    assert integrity is not None
    forged_integrity = replace(
        integrity,
        evidence_path=tmp_path / "missing.jsonl",
        manifest_path=tmp_path / "missing.manifest.json",
    )
    assert forged_integrity.provenance_verified is False
    forged_replay = replay_records(records, forged_integrity)
    assert forged_replay.integrity_verified is False
    assert forged_replay.cases == ()

    verified_replay = replay_file(CALIBRATION)
    self_asserted_replay = ReplaySummary.create(
        verified_replay.cases,
        integrity=integrity,
    )
    assert self_asserted_replay.integrity_verified is False
    serialized_replay = ReplaySummary.from_dict(verified_replay.to_dict())
    assert serialized_replay.integrity_verified is False
    with pytest.raises(ValueError, match="fresh verified replay"):
        ReviewPacket.create(
            records[0],
            replay_case=verified_replay.cases[0],
            replay_summary=serialized_replay,
            evidence_path=tmp_path / "missing.jsonl",
            expected_manifest_sha256="0" * 64,
        )


def test_verified_integrity_rejects_caller_modified_records() -> None:
    records, errors, integrity = verify_evidence_bundle(CALIBRATION)
    assert errors == []
    assert integrity is not None
    modified = list(records)
    modified_record = dict(modified[1])
    modified_record["verdict"] = Verdict.PASS.value
    modified_record["deterministic_failures"] = []
    modified[1] = modified_record

    replay = replay_records(modified, integrity)
    assert replay.integrity_verified is False
    assert replay.cases == ()
    assert replay.errors == ("verified evidence record binding mismatch",)


def test_persisted_replay_report_detects_field_substitution(tmp_path: Path) -> None:
    report = tmp_path / "replay.json"
    replay = replay_file(CALIBRATION)
    write_replay_report(report, replay)
    assert load_replay_report(report, evidence_path=CALIBRATION) == replay
    document = json.loads(report.read_text(encoding="utf-8"))
    document["counts"]["PASS"] += 1
    report.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="counts mismatch"):
        load_replay_report(report, evidence_path=CALIBRATION)


def test_replay_cannot_promote_deterministic_failure_into_review_packet() -> None:
    record, replay_case, replay = bound_source("CAL-FAIL-TOOL-ENVELOPE")
    tampered_record = dict(record)
    tampered_record["deterministic_failures"] = []
    with pytest.raises(ValueError, match="verified source record mismatch"):
        ReviewPacket.create(
            tampered_record,
            replay_case=replay_case,
            replay_summary=replay,
            evidence_path=CALIBRATION,
        )
    promoted_case = replace(
        replay_case,
        replayed_verdict=Verdict.PASS,
        deterministic_failures=(),
    )
    promoted_cases = tuple(
        promoted_case if case.case_id == replay_case.case_id else case
        for case in replay.cases
    )
    promoted = replace(replay, cases=promoted_cases, replay_digest="")
    promoted = replace(promoted, replay_digest=promoted.expected_digest())
    assert promoted.integrity_verified is False
    with pytest.raises(ValueError, match="fresh verified replay"):
        ReviewPacket.create(
            record,
            replay_case=promoted_case,
            replay_summary=promoted,
            evidence_path=CALIBRATION,
        )


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
    record, replay_case, replay = bound_source()
    record = dict(record)
    record["case_id"] = unsafe_id
    with pytest.raises(ValueError):
        ReviewPacket.create(
            record,
            replay_case=replay_case,
            replay_summary=replay,
            evidence_path=CALIBRATION,
        )


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
    packet = bound_packet()
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
    packet = bound_packet()
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
    packet = bound_packet()
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
    packet = bound_packet()
    first_path = tmp_path / "review-a.json"
    second_path = tmp_path / "review-b.json"
    write_review_result(first_path, review(packet, "context-a", Verdict.PASS))
    write_review_result(second_path, review(packet, "context-b", Verdict.PASS))
    replay_path = tmp_path / "replay.json"
    write_replay_report(replay_path, replay_file(CALIBRATION))

    assert main(
        [
            "adjudicate",
            str(first_path),
            str(second_path),
            "--replay",
            str(replay_path),
            "--evidence",
            str(CALIBRATION),
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    adjudication = tmp_path / "CAL-PASS-STREAM.adjudication.json"
    assert Path(output["output"]).resolve() == adjudication.resolve()
    assert adjudication.is_file()


def test_all_generated_outputs_share_redaction_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = "canary" + "://credential/output-tree-b05"
    private_material = (
        "-----BEGIN PRIVATE KEY-----\n"
        "DISPOSABLEOUTPUTTREEONLY\n"
        "-----END PRIVATE KEY-----"
    )
    disposable_values = (
        canary,
        private_material,
        "disposable-refresh-output",
        "disposable-session-output",
        "disposable-cookie-output",
    )
    observation = {
        "final": "answer",
        "private_key": private_material,
        "refresh_token": disposable_values[2],
        "session_cookie": disposable_values[3],
        "diagnostic": (
            f"Cookie: sid={disposable_values[4]}\nmarker={canary}"
        ),
    }
    identity = CampaignIdentity.create("output-boundary")
    scenario = Scenario.from_dict(
        {
            "scenario_id": "OUTPUT-BOUNDARY-001",
            "title": "output boundary fixture",
            "transport": "offline",
            "request": {"observation": observation},
            "expected_contract": {"exact": "answer"},
            "validators": [{"kind": "format_contract", "exact": "answer"}],
        }
    )
    result = CaseResult(
        case_id=scenario.scenario_id,
        verdict=Verdict.PASS,
        assertions=(AssertionResult("format.exact", True, "answer", "answer"),),
        observation=observation,
        bounded_evidence={"source": "sanitized fixture"},
    )
    root = tmp_path / "generated"
    store = EvidenceStore(root, identity, canaries=[canary])
    store.append(scenario, result)
    anchor = store.write_manifest(CampaignSummary(identity, (result,)))

    replay = replay_file(
        store.path,
        expected_manifest_sha256=anchor.manifest_sha256,
    )
    assert replay.integrity_verified
    write_replay_report(root / "replay.json", replay, canaries=[canary])
    packet_path = build_review_packets(
        store.path,
        root / "packets",
        expected_manifest_sha256=anchor.manifest_sha256,
        canaries=[canary],
    )[0]
    packet = ReviewPacket.from_dict(json.loads(packet_path.read_text(encoding="utf-8")))
    packet.verify_source(
        store.path,
        expected_manifest_sha256=anchor.manifest_sha256,
        replay_summary=replay,
    )
    first = ReviewResult.create(
        review_id="output-review-a",
        reviewer_id="output-context-a",
        independence_level=IndependenceLevel.SAME_MODEL_CLEAN_CONTEXT,
        verdict=Verdict.PASS,
        rationale=f"bounded {canary} {private_material}",
        evidence_citations=("source",),
        packet=packet,
        canaries=[canary],
    )
    second = ReviewResult.create(
        review_id="output-review-b",
        reviewer_id="output-context-b",
        independence_level=IndependenceLevel.DIFFERENT_MODEL,
        verdict=Verdict.PASS,
        rationale=f"bounded {canary}",
        evidence_citations=("source",),
        packet=packet,
        canaries=[canary],
    )
    write_review_result(root / "review-a.json", first, canaries=[canary])
    write_review_result(root / "review-b.json", second, canaries=[canary])
    write_adjudication(
        root / "adjudication.json",
        adjudicate(first, second),
        canaries=[canary],
    )
    _emit(
        {"command": "redaction-smoke", "token": canary, "detail": private_material},
        canaries=[canary],
    )
    emitted = capsys.readouterr().out
    assert credential_like_paths(json.loads(emitted), canaries=[canary]) == ()

    generated_files = [path for path in root.rglob("*") if path.is_file()]
    assert len(generated_files) >= 7
    for path in generated_files:
        text = path.read_text(encoding="utf-8")
        assert all(value not in text for value in disposable_values)
        documents = (
            [json.loads(line) for line in text.splitlines()]
            if path.suffix == ".jsonl"
            else [json.loads(text)]
        )
        assert all(
            credential_like_paths(document, canaries=[canary]) == ()
            for document in documents
        )
