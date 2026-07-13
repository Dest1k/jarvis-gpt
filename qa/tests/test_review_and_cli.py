from __future__ import annotations

import hashlib
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
from qa.review.independence import (
    IndependenceLevel,
    ReviewContext,
    assess_independence,
    is_independent_model,
)
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


def review_context(
    context_id: str,
    *,
    run_nonce: str | None = None,
    provider: str = "fixture-provider",
    model: str = "fixture-model",
    profile: str = "fixture-profile",
) -> ReviewContext:
    return ReviewContext.create(
        context_id=context_id,
        run_nonce=run_nonce or hashlib.sha256(context_id.encode("utf-8")).hexdigest()[:32],
        provider=provider,
        model=model,
        profile=profile,
    )


def review(
    packet: ReviewPacket,
    reviewer_id: str,
    verdict: Verdict,
    rationale: str = "bounded review",
    *,
    context: ReviewContext | None = None,
    citations: tuple[str, ...] | None = None,
) -> ReviewResult:
    return ReviewResult.create(
        review_id=f"review-{reviewer_id}",
        reviewer_id=reviewer_id,
        context=context or review_context(f"context-{reviewer_id}"),
        verdict=verdict,
        rationale=rationale,
        evidence_citations=citations or tuple(sorted(packet.evidence_ids + packet.assertion_ids)),
        packet=packet,
    )


def context_anchors(
    first: ReviewResult,
    second: ReviewResult,
) -> tuple[str, str]:
    return first.context.context_digest, second.context.context_digest


def review_anchors(
    first: ReviewResult,
    second: ReviewResult,
) -> tuple[str, str]:
    return first.review_digest, second.review_digest


def adjudicate_reviews(
    first: ReviewResult,
    second: ReviewResult,
    *,
    replay_summary: ReplaySummary | None = None,
):
    return adjudicate(
        first,
        second,
        replay_summary=replay_summary,
        expected_context_digests=context_anchors(first, second),
        expected_review_digests=review_anchors(first, second),
    )


def test_review_outputs_are_separate_immutable_and_digest_checked(tmp_path: Path) -> None:
    packet = bound_packet()
    first = SyntheticReviewer(
        "context-a",
        review_context("context-a"),
        Verdict.PASS,
        "review A",
    ).review(packet)
    second = SyntheticReviewer(
        "context-b",
        review_context("context-b", model="fixture-model-b"),
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
    review_schema = json.loads(
        (ROOT / "qa" / "schemas" / "review.schema.json").read_text(encoding="utf-8")
    )
    assert validate_json_schema(first.to_dict(), review_schema) == []
    independence = assess_independence(
        first.context,
        second.context,
        expected_context_digests=context_anchors(first, second),
    )
    assert independence.level is IndependenceLevel.DIFFERENT_MODEL
    assert is_independent_model(independence.level)

    tampered = json.loads(first_path.read_text(encoding="utf-8"))
    tampered["rationale"] = "changed after review"
    first_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_review_result(first_path)


@pytest.mark.parametrize(
    ("second_context", "expected"),
    [
        (
            review_context("pair-profile", profile="fixture-profile-b"),
            IndependenceLevel.DIFFERENT_PROFILE,
        ),
        (review_context("pair-model", model="fixture-model-b"), IndependenceLevel.DIFFERENT_MODEL),
        (
            review_context("pair-provider", provider="fixture-provider-b"),
            IndependenceLevel.DIFFERENT_PROVIDER,
        ),
        (review_context("pair-context"), IndependenceLevel.SAME_MODEL_CLEAN_CONTEXT),
    ],
)
def test_independence_is_computed_from_factual_context_differences(
    second_context: ReviewContext,
    expected: IndependenceLevel,
) -> None:
    first_context = review_context("pair-first")
    assessment = assess_independence(
        first_context,
        second_context,
        expected_context_digests=(
            first_context.context_digest,
            second_context.context_digest,
        ),
    )
    assert assessment.verified
    assert assessment.level is expected


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("context_id", ""),
        ("run_nonce", 1),
        ("provider", "Fixture-Provider"),
        ("model", None),
        ("profile", "../profile"),
        ("context_digest", "0" * 64),
    ],
)
def test_review_context_rejects_missing_wrong_type_or_noncanonical_facts(
    field: str,
    value: object,
) -> None:
    document: dict[str, object] = review_context("strict-context").to_dict()
    document[field] = value
    with pytest.raises(ValueError):
        ReviewContext.from_dict(document)
    document = review_context("strict-context").to_dict()
    document.pop(field)
    with pytest.raises(ValueError, match="incomplete or unexpected"):
        ReviewContext.from_dict(document)


def test_review_context_factory_rejects_explicit_empty_nonce() -> None:
    with pytest.raises(ValueError, match="run_nonce"):
        ReviewContext.create(
            context_id="empty-nonce",
            run_nonce="",
            provider="fixture-provider",
            model="fixture-model",
            profile="fixture-profile",
        )


def test_review_context_direct_construction_and_replace_validate_facts() -> None:
    context = review_context("direct-context")
    with pytest.raises(ValueError, match="canonical identifier"):
        ReviewContext(
            context_id="Direct-Context",
            run_nonce=context.run_nonce,
            provider=context.provider,
            model=context.model,
            profile=context.profile,
            context_digest=context.context_digest,
        )
    with pytest.raises(ValueError, match="canonical identifier"):
        replace(context, provider="Fixture-Provider")


def test_missing_or_swapped_context_anchors_cannot_authorize_pass() -> None:
    packet = bound_packet()
    first = review(packet, "anchor-a", Verdict.PASS)
    second = review(packet, "anchor-b", Verdict.PASS)

    missing = adjudicate(first, second)
    assert missing.verdict is Verdict.INCONCLUSIVE
    assert missing.independence_verified is False
    assert missing.context_anchor_sha256s == ()
    assert missing.review_anchors_verified is False
    assert missing.review_anchor_sha256s == ()
    adjudication_schema = json.loads(
        (ROOT / "qa" / "schemas" / "adjudication.schema.json").read_text(encoding="utf-8")
    )
    assert validate_json_schema(missing.to_dict(), adjudication_schema) == []

    swapped = adjudicate(
        first,
        second,
        expected_context_digests=(
            second.context.context_digest,
            first.context.context_digest,
        ),
        expected_review_digests=review_anchors(first, second),
    )
    assert swapped.verdict is Verdict.INCONCLUSIVE
    assert swapped.independence_verified is False

    with pytest.raises(ValueError, match="context anchors"):
        adjudicate(
            first,
            second,
            expected_context_digests=("not-a-digest", "also-not-a-digest"),
            expected_review_digests=review_anchors(first, second),
        )


def test_review_result_requires_exact_top_level_packet_digest() -> None:
    document = review(bound_packet(), "packet-binding", Verdict.PASS).to_dict()
    document["packet_digest"] = "0" * 64
    body = dict(document)
    body.pop("review_digest")
    document["review_digest"] = canonical_digest(body)
    with pytest.raises(ValueError, match="packet digest binding mismatch"):
        ReviewResult.from_dict(document)


@pytest.mark.parametrize("collision", ["context_id", "run_nonce"])
def test_duplicate_context_or_run_nonce_cannot_authorize_pass(collision: str) -> None:
    packet = bound_packet()
    first_context = review_context("collision-a", run_nonce="a" * 32)
    second_context = review_context(
        "collision-a" if collision == "context_id" else "collision-b",
        run_nonce="b" * 32 if collision == "context_id" else "a" * 32,
    )
    result = adjudicate_reviews(
        review(packet, "collision-review-a", Verdict.PASS, context=first_context),
        review(packet, "collision-review-b", Verdict.PASS, context=second_context),
    )
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.independence_verified is False
    assert result.independence_level is None


@pytest.mark.parametrize(
    "citations",
    [
        (),
        ("",),
        ("source",),
        ("evidence:*",),
        ("evidence:missing",),
        ("evidence:source.child",),
        ("evidence:source", "evidence:source"),
        ("assertion:calibration.recorded",),
        ("evidence:source",),
        (1,),
    ],
)
def test_substantive_review_rejects_invalid_or_insufficient_citations(
    citations: tuple[object, ...],
) -> None:
    packet = bound_packet()
    with pytest.raises(ValueError):
        ReviewResult.create(
            review_id="invalid-citations",
            reviewer_id="invalid-citations",
            context=review_context("invalid-citations"),
            verdict=Verdict.PASS,
            rationale="bounded review",
            evidence_citations=citations,
            packet=packet,
        )


def test_exact_packet_citations_and_separate_contexts_can_pass() -> None:
    packet = bound_packet()
    assert packet.evidence_ids == ("evidence:source",)
    assert packet.assertion_ids == ("assertion:calibration.recorded",)
    first = review(packet, "exact-a", Verdict.PASS)
    second = review(packet, "exact-b", Verdict.PASS)
    result = adjudicate_reviews(first, second)
    assert result.verdict is Verdict.PASS
    assert result.independence_verified
    assert result.review_anchors_verified
    assert result.independence_level is IndependenceLevel.SAME_MODEL_CLEAN_CONTEXT
    assert result.reviews == (first, second)
    assert [item.review_digest for item in result.reviews] == [
        first.review_digest,
        second.review_digest,
    ]
    adjudication_schema = json.loads(
        (ROOT / "qa" / "schemas" / "adjudication.schema.json").read_text(encoding="utf-8")
    )
    assert validate_json_schema(result.to_dict(), adjudication_schema) == []
    invalid_level = result.to_dict()
    invalid_level["independence_level"] = None
    assert validate_json_schema(invalid_level, adjudication_schema)


def test_metadata_only_bounded_evidence_cannot_support_pass(tmp_path: Path) -> None:
    packet = generated_packet(
        tmp_path,
        bounded_evidence={
            "transport": "offline",
            "tags": ["fixture"],
            "source": {"provider": "fixture-provider", "profile": "fixture-profile"},
            "note": "fixture metadata only",
        },
    )
    assert packet.bounded_evidence
    assert packet.evidence_ids == ()
    with pytest.raises(ValueError, match="requires an evidence citation"):
        review(packet, "metadata-pass", Verdict.PASS)
    result = adjudicate_reviews(
        review(packet, "metadata-a", Verdict.INCONCLUSIVE),
        review(packet, "metadata-b", Verdict.INCONCLUSIVE),
    )
    assert result.verdict is Verdict.INCONCLUSIVE


@pytest.mark.parametrize(
    "bounded_evidence",
    [
        {"source": {"kind": "reference", "content": "sanitized fixture"}},
        {
            "source": {
                "kind": "reference",
                "assertion_ids": ["assertion:format.exact"],
                "content": {"provider": "fixture-provider"},
            }
        },
    ],
)
def test_malformed_or_metadata_only_typed_evidence_is_rejected(
    tmp_path: Path,
    bounded_evidence: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="typed bounded evidence"):
        generated_packet(tmp_path, bounded_evidence=bounded_evidence)


@pytest.mark.parametrize(
    "forbidden_level",
    [IndependenceLevel.DETERMINISTIC_ONLY, IndependenceLevel.HUMAN_ADJUDICATED],
)
def test_review_schema_rejects_self_asserted_nonsemantic_level(
    forbidden_level: IndependenceLevel,
) -> None:
    document = review(bound_packet(), "typed-context", Verdict.PASS).to_dict()
    document["independence_level"] = forbidden_level.value
    body = dict(document)
    body.pop("review_digest")
    document["review_digest"] = canonical_digest(body)
    with pytest.raises(ValueError, match="fields are incomplete or unexpected"):
        ReviewResult.from_dict(document)


def test_stale_review_digest_is_rejected_at_write_and_adjudication(
    tmp_path: Path,
) -> None:
    packet = bound_packet()
    original = review(packet, "stale-a", Verdict.PASS)
    stale = replace(original, reviewer_id="stale-changed")
    with pytest.raises(ValueError, match="review result digest mismatch"):
        write_review_result(tmp_path / "stale.json", stale)
    with pytest.raises(ValueError, match="review result digest mismatch"):
        adjudicate_reviews(stale, review(packet, "stale-b", Verdict.PASS))


def test_adjudication_writer_rederives_authoritative_decision(tmp_path: Path) -> None:
    packet = bound_packet("CAL-FAIL-TOOL-ENVELOPE")
    result = adjudicate_reviews(
        review(packet, "writer-a", Verdict.PASS),
        review(packet, "writer-b", Verdict.PASS),
    )
    assert result.verdict is Verdict.FAIL
    forged = replace(result, verdict=Verdict.PASS, rationale="forged promotion")
    with pytest.raises(ValueError, match="does not match verified review inputs"):
        write_adjudication(
            tmp_path / "forged.json",
            forged,
            expected_context_digests=context_anchors(*result.reviews),
            expected_review_digests=review_anchors(*result.reviews),
        )
    assert not (tmp_path / "forged.json").exists()


def test_relabelled_copied_review_fails_retained_context_anchors() -> None:
    packet = bound_packet()
    original = review(
        packet,
        "copy-a",
        Verdict.PASS,
        context=review_context("actual-context-a", run_nonce="a" * 32),
    )
    retained_second = review(
        packet,
        "copy-b",
        Verdict.PASS,
        context=review_context(
            "actual-context-b",
            run_nonce="b" * 32,
            model="fixture-model-b",
        ),
    )
    copied_document = original.to_dict()
    copied_document["review_id"] = "review-copy-b"
    copied_document["reviewer_id"] = "copy-b"
    copied_document["context"] = review_context(
        "self-issued-copy",
        run_nonce="c" * 32,
        model="self-issued-model",
    ).to_dict()
    body = dict(copied_document)
    body.pop("review_digest")
    copied_document["review_digest"] = canonical_digest(body)
    copied = ReviewResult.from_dict(copied_document)
    copied.packet.verify_source(CALIBRATION)

    result = adjudicate(
        original,
        copied,
        expected_context_digests=(
            original.context.context_digest,
            retained_second.context.context_digest,
        ),
        expected_review_digests=review_anchors(original, retained_second),
    )
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.independence_verified is False


def test_recomputed_semantic_review_fails_retained_review_anchors() -> None:
    packet = bound_packet()
    first = review(packet, "semantic-a", Verdict.PASS)
    original_second = review(packet, "semantic-b", Verdict.FAIL)
    forged_document = original_second.to_dict()
    forged_document["verdict"] = Verdict.PASS.value
    forged_document["rationale"] = "recomputed semantic promotion"
    body = dict(forged_document)
    body.pop("review_digest")
    forged_document["review_digest"] = canonical_digest(body)
    forged_second = ReviewResult.from_dict(forged_document)
    forged_second.packet.verify_source(CALIBRATION)

    result = adjudicate(
        first,
        forged_second,
        expected_context_digests=context_anchors(first, original_second),
        expected_review_digests=review_anchors(first, original_second),
    )
    assert result.verdict is Verdict.INCONCLUSIVE
    assert result.independence_verified is False
    assert result.review_anchors_verified is False


def test_adjudicator_preserves_disagreement_and_deterministic_fail() -> None:
    packet = bound_packet()
    disagreement = adjudicate_reviews(
        review(packet, "a", Verdict.PASS),
        review(packet, "b", Verdict.FAIL),
    )
    assert disagreement.verdict is Verdict.INCONCLUSIVE
    assert len(disagreement.reviews) == 2

    failed_packet = bound_packet("CAL-FAIL-TOOL-ENVELOPE")
    authoritative = adjudicate_reviews(
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
        document["packet_digest"] = packet_document["packet_digest"]
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
        document = review(packet, reviewer_id, Verdict.INCONCLUSIVE).to_dict()
        packet_document = document["packet"]
        packet_document["bounded_evidence"] = {
            "source": {
                "kind": "reference",
                "assertion_ids": ["assertion:format.exact"],
                "content": "substituted fixture",
            }
        }
        packet_document["evidence_ids"] = ["evidence:source"]
        packet_body = dict(packet_document)
        packet_body.pop("packet_digest")
        packet_document["packet_digest"] = canonical_digest(packet_body)
        document["packet_digest"] = packet_document["packet_digest"]
        document["verdict"] = Verdict.PASS.value
        document["evidence_citations"] = [
            "evidence:source",
            "assertion:format.exact",
        ]
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
            expected_context_digests=context_anchors(*loaded_reviews),
            expected_review_digests=review_anchors(*loaded_reviews),
            expected_manifest_sha256=manifest_sha256,
        )


def test_adjudicator_never_passes_without_evidence(tmp_path: Path) -> None:
    packet = generated_packet(tmp_path, bounded_evidence={})
    with pytest.raises(ValueError, match="requires an evidence citation"):
        review(packet, "rejected-pass", Verdict.PASS)
    result = adjudicate_reviews(
        review(packet, "a", Verdict.INCONCLUSIVE),
        review(packet, "b", Verdict.INCONCLUSIVE),
    )
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
    assert packet.source_record_canonical_sha256 == replay_case.source_record_canonical_sha256
    assert packet.source_evidence_sha256 == replay.evidence_sha256
    assert packet.source_manifest_sha256 == replay.manifest_sha256
    assert packet.source_replay_sha256 == replay.replay_digest
    packet.verify_replay(replay)
    packet_schema = json.loads(
        (ROOT / "qa" / "schemas" / "review-packet.schema.json").read_text(encoding="utf-8")
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
        promoted_case if case.case_id == replay_case.case_id else case for case in replay.cases
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
    first = review(packet, "context-a", Verdict.PASS)
    second = review(packet, "context-b", Verdict.PASS)
    write_review_result(first_path, first)
    write_review_result(second_path, second)
    replay_path = tmp_path / "replay.json"
    write_replay_report(replay_path, replay_file(CALIBRATION))

    assert (
        main(
            [
                "adjudicate",
                str(first_path),
                str(second_path),
                "--replay",
                str(replay_path),
                "--evidence",
                str(CALIBRATION),
                "--context-anchor-1",
                first.context.context_digest,
                "--context-anchor-2",
                second.context.context_digest,
                "--review-anchor-1",
                first.review_digest,
                "--review-anchor-2",
                second.review_digest,
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["independence_verified"] is True
    assert output["review_anchors_verified"] is True
    assert output["independence_level"] == "SAME_MODEL_CLEAN_CONTEXT"
    adjudication = tmp_path / "CAL-PASS-STREAM.adjudication.json"
    assert Path(output["output"]).resolve() == adjudication.resolve()
    assert adjudication.is_file()
    persisted = json.loads(adjudication.read_text(encoding="utf-8"))
    assert persisted["independence_level"] == "SAME_MODEL_CLEAN_CONTEXT"


def test_cli_rejects_malformed_review_anchor_before_reading_files() -> None:
    valid = "0" * 64
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "adjudicate",
                "missing-a.json",
                "missing-b.json",
                "--replay",
                "missing-replay.json",
                "--evidence",
                "missing-evidence.jsonl",
                "--context-anchor-1",
                valid,
                "--context-anchor-2",
                "1" * 64,
                "--review-anchor-1",
                "malformed",
                "--review-anchor-2",
                "2" * 64,
            ]
        )
    assert raised.value.code == 2


def test_all_generated_outputs_share_redaction_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    canary = "canary" + "://credential/output-tree-b05"
    private_material = (
        "-----BEGIN PRIVATE KEY-----\n" "DISPOSABLEOUTPUTTREEONLY\n" "-----END PRIVATE KEY-----"
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
        "diagnostic": (f"Cookie: sid={disposable_values[4]}\nmarker={canary}"),
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
        bounded_evidence={
            "source": {
                "kind": "reference",
                "assertion_ids": ["assertion:format.exact"],
                "content": "sanitized fixture",
            }
        },
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
        context=review_context("output-context-a"),
        verdict=Verdict.PASS,
        rationale=f"bounded {canary} {private_material}",
        evidence_citations=("evidence:source", "assertion:format.exact"),
        packet=packet,
        canaries=[canary],
    )
    second = ReviewResult.create(
        review_id="output-review-b",
        reviewer_id="output-context-b",
        context=review_context("output-context-b", model="fixture-model-b"),
        verdict=Verdict.PASS,
        rationale=f"bounded {canary}",
        evidence_citations=("evidence:source", "assertion:format.exact"),
        packet=packet,
        canaries=[canary],
    )
    write_review_result(root / "review-a.json", first, canaries=[canary])
    write_review_result(root / "review-b.json", second, canaries=[canary])
    write_adjudication(
        root / "adjudication.json",
        adjudicate_reviews(first, second),
        expected_context_digests=context_anchors(first, second),
        expected_review_digests=review_anchors(first, second),
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
            credential_like_paths(document, canaries=[canary]) == () for document in documents
        )
