from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from qa.redaction import credential_like_paths
from qa.upstream import (
    ADOPTION_MODES,
    ORIGIN_KINDS,
    Verdict,
    validate_candidate,
    validate_provenance,
)
from qa.upstream.validator import main

REPOSITORY_URL = "https://example.invalid/acme/donor"
COMMIT_SHA = "1" * 40
SOURCE_SHA = "2" * 64
RESULT_SHA = "3" * 64


def _write_evidence(root: Path, name: str, content: bytes) -> dict[str, str]:
    relative = f"docs/upstream/decisions/{name}"
    target = root / Path(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {"path": relative, "sha256": hashlib.sha256(content).hexdigest()}


def _provenance_for(candidate: dict[str, object]) -> dict[str, object]:
    license_record = candidate["license"]
    assert isinstance(license_record, dict)
    mode = candidate["adoption_mode"]
    copied = mode in {"test_corpus", "ported_module", "fork"}
    source_files = deepcopy(candidate["source_files"])
    imported_paths: list[dict[str, str]] = []
    if copied:
        assert isinstance(source_files, list) and source_files
        source = source_files[0]
        assert isinstance(source, dict)
        imported_paths.append(
            {
                "source_path": str(source["path"]),
                "destination_path": "qa/fixtures/adopted_module.py",
                "source_sha256": str(source["sha256"]),
                "result_sha256": RESULT_SHA,
                "transformation": "bounded test fixture port",
            }
        )
    identifier = license_record.get("identifier")
    repository_path = license_record.get("repository_path")
    evidence = license_record.get("evidence")
    license_sha = evidence.get("sha256") if isinstance(evidence, dict) else None
    return {
        "schema_version": "1.0",
        "record_id": "PROV-0001",
        "candidate_id": candidate["candidate_id"],
        "component": candidate["component"],
        "origin_kind": candidate["origin_kind"],
        "adoption_mode": mode,
        "repository_url": candidate["repository_url"],
        "pinned_commit_sha": candidate["pinned_commit_sha"],
        "license_snapshot": {
            "identifier": identifier,
            "repository_path": repository_path,
            "sha256": license_sha,
        },
        "external_code_imported": candidate["external_code_imported"],
        "source_files": source_files,
        "imported_paths": imported_paths,
        "transformation_notes": "No donor code executed during review.",
        "retained_notices": ["LICENSE"] if identifier else [],
        "recorded_at": "2026-07-13T00:30:00Z",
        "recorded_by": "Assurance reviewer",
    }


def _refresh_provenance(root: Path, candidate: dict[str, object]) -> None:
    payload = json.dumps(_provenance_for(candidate), indent=2, sort_keys=True).encode() + b"\n"
    candidate["provenance_record"] = _write_evidence(root, "PROV-0001.json", payload)


def _external_candidate(root: Path) -> dict[str, object]:
    license_ref = _write_evidence(root, "LICENSE.txt", b"MIT fixture; sanitized\n")
    review_ref = _write_evidence(root, "review.txt", b"bounded review: PASS\n")
    tests_ref = _write_evidence(root, "tests.txt", b"offline regression/failure/rollback\n")
    decision_ref = _write_evidence(root, "decision.md", b"human approval fixture\n")
    candidate: dict[str, object] = {
        "schema_version": "1.0",
        "candidate_id": "UP-0001",
        "component": "bounded_adapter",
        "origin_kind": "ported_code",
        "finding_ids": ["F-JARVIS-0001"],
        "external_code_imported": True,
        "adoption_mode": "ported_module",
        "repository_url": REPOSITORY_URL,
        "pinned_commit_sha": COMMIT_SHA,
        "license": {
            "classification": "permissive",
            "identifier": "MIT",
            "verification_status": "VERIFIED",
            "repository_path": "LICENSE",
            "notice_verified": True,
            "evidence": license_ref,
        },
        "source_files": [{"path": "src/module.py", "sha256": SOURCE_SHA}],
        "dependency_review": {"status": "PASS", "evidence": review_ref},
        "security_review": {"status": "PASS", "evidence": review_ref},
        "isolated_spike": {"status": "PASS", "evidence": review_ref},
        "tests": {
            "regression": [tests_ref],
            "failure": [tests_ref],
            "rollback": [tests_ref],
        },
        "human_approval": {
            "status": "APPROVED",
            "approved_by": "Disposable test approver",
            "approved_at": "2026-07-13T00:40:00Z",
            "evidence": decision_ref,
        },
    }
    _refresh_provenance(root, candidate)
    return candidate


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_policy_schemas_and_component_origins_declare_required_enums() -> None:
    root = Path(__file__).resolve().parents[2]
    candidate_schema = json.loads(
        (root / "docs/upstream/CANDIDATE_SCHEMA.json").read_text(encoding="utf-8")
    )
    provenance_schema = json.loads(
        (root / "docs/upstream/PROVENANCE_SCHEMA.json").read_text(encoding="utf-8")
    )
    origins = json.loads((root / "docs/upstream/COMPONENT_ORIGINS.yml").read_text())
    donors = json.loads((root / "docs/upstream/DONOR_REGISTRY.yml").read_text())

    assert candidate_schema["$defs"]["origin_kind"]["enum"] == list(ORIGIN_KINDS)
    assert candidate_schema["$defs"]["adoption_mode"]["enum"] == list(ADOPTION_MODES)
    assert provenance_schema["$defs"]["origin_kind"]["enum"] == list(ORIGIN_KINDS)
    assert origins["web_surfer"] == {
        "origin_kind": "commissioned_internal",
        "commissioned_by": "Dest",
        "implementation_agent": "Claude",
        "external_code_imported": False,
    }
    assert origins["document_surfer"] == {
        "origin_kind": "commissioned_internal",
        "commissioned_by": "Dest",
        "implementation_agent": "Grok",
        "external_code_imported": False,
    }
    assert donors["donors"] == []


def test_complete_external_candidate_passes_offline(tmp_path: Path) -> None:
    result = validate_candidate(_external_candidate(tmp_path), evidence_root=tmp_path)

    assert result.verdict is Verdict.PASS
    assert result.issues == ()


def test_offline_cli_reports_machine_readable_pass(tmp_path: Path, capsys) -> None:
    candidate_path = tmp_path / "docs/upstream/candidates/UP-0001.json"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(json.dumps(_external_candidate(tmp_path)), encoding="utf-8")

    exit_code = main([str(candidate_path), "--evidence-root", str(tmp_path)])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output == {"issues": [], "verdict": "PASS"}


def test_offline_cli_redacts_credential_like_diagnostics(tmp_path: Path, capsys) -> None:
    canary = "canary-token-disposable-upstream"
    exit_code = main([str(tmp_path / f"missing-{canary}.json")])
    rendered = capsys.readouterr().out
    assert exit_code == 1
    assert canary not in rendered
    assert credential_like_paths(json.loads(rendered)) == ()


def test_missing_commit_sha_fails(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    del candidate["pinned_commit_sha"]

    result = validate_candidate(candidate, evidence_root=tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "PINNED_COMMIT_REQUIRED" in _codes(result)


def test_unknown_license_is_blocked_not_passed(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    candidate["license"] = {
        "classification": "unknown",
        "identifier": None,
        "verification_status": "UNKNOWN",
        "repository_path": None,
        "notice_verified": False,
    }
    _refresh_provenance(tmp_path, candidate)

    result = validate_candidate(candidate, evidence_root=tmp_path)

    assert result.verdict is Verdict.BLOCKED
    assert "LICENSE_UNKNOWN" in _codes(result)


def test_candidate_without_finding_or_capability_gap_fails(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    candidate["finding_ids"] = []

    result = validate_candidate(candidate, evidence_root=tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "FINDING_OR_GAP_REQUIRED" in _codes(result)


def test_copied_code_without_sources_or_provenance_fails(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    candidate["source_files"] = []
    del candidate["provenance_record"]

    result = validate_candidate(candidate, evidence_root=tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "SOURCE_FILES_REQUIRED" in _codes(result)
    assert any(issue.path == "provenance_record" for issue in result.issues)


def test_idea_only_candidate_does_not_require_copied_files(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    candidate["origin_kind"] = "inspired_by"
    candidate["adoption_mode"] = "idea_only"
    candidate["external_code_imported"] = False
    candidate["source_files"] = []
    _refresh_provenance(tmp_path, candidate)

    result = validate_candidate(candidate, evidence_root=tmp_path)

    assert result.verdict is Verdict.PASS


def test_commissioned_internal_candidate_needs_no_upstream_repository() -> None:
    candidate = {
        "schema_version": "1.0",
        "candidate_id": "INT-0001",
        "component": "web_surfer",
        "origin_kind": "commissioned_internal",
        "capability_gap": "Record existing component provenance.",
        "commissioned_by": "Dest",
        "implementation_agent": "Claude",
        "external_code_imported": False,
    }

    result = validate_candidate(candidate)

    assert result.verdict is Verdict.PASS
    assert "repository_url" not in candidate


def test_claimed_evidence_with_wrong_hash_fails(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    security = candidate["security_review"]
    assert isinstance(security, dict)
    evidence = security["evidence"]
    assert isinstance(evidence, dict)
    evidence["sha256"] = "0" * 64

    result = validate_candidate(candidate, evidence_root=tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "EVIDENCE_HASH_MISMATCH" in _codes(result)


def test_unreviewed_security_gate_is_blocked(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    candidate["security_review"] = {"status": "NOT_REVIEWED"}

    result = validate_candidate(candidate, evidence_root=tmp_path)

    assert result.verdict is Verdict.BLOCKED
    assert "GATE_REVIEW_INCOMPLETE" in _codes(result)


def test_copied_provenance_without_imported_paths_fails(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    reference = candidate["provenance_record"]
    assert isinstance(reference, dict)
    provenance_path = tmp_path / Path(*str(reference["path"]).split("/"))
    provenance = json.loads(provenance_path.read_text())
    provenance["imported_paths"] = []

    result = validate_provenance(provenance)

    assert result.verdict is Verdict.FAIL
    assert "IMPORTED_PATHS_REQUIRED" in _codes(result)


def test_missing_local_evidence_is_blocked(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    tests = candidate["tests"]
    assert isinstance(tests, dict)
    regression = tests["regression"]
    assert isinstance(regression, list)
    reference = regression[0]
    assert isinstance(reference, dict)
    target = tmp_path / Path(*str(reference["path"]).split("/"))
    target.unlink()

    result = validate_candidate(candidate, evidence_root=tmp_path)

    assert result.verdict is Verdict.BLOCKED
    assert "EVIDENCE_MISSING" in _codes(result)
