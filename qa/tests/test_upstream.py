from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

import qa.safe_paths as safe_paths
from qa.redaction import credential_like_paths
from qa.schema_validation import validate_json_schema
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
SOURCE_BYTES = b"def reviewed_fixture():\n    return 'source'\n"
RESULT_BYTES = b"def reviewed_fixture():\n    return 'ported result'\n"
SOURCE_SHA = hashlib.sha256(SOURCE_BYTES).hexdigest()
RESULT_SHA = hashlib.sha256(RESULT_BYTES).hexdigest()


def _source_root(root: Path) -> Path:
    return root / "reviewed-source"


def _destination_root(root: Path) -> Path:
    return root / "destination"


def _write_rooted(root: Path, relative: str, content: bytes) -> None:
    target = root / Path(*relative.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


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


def _load_provenance(root: Path, candidate: dict[str, object]) -> dict[str, object]:
    reference = candidate["provenance_record"]
    assert isinstance(reference, dict)
    target = root / Path(*str(reference["path"]).split("/"))
    document = json.loads(target.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _persist_provenance(
    root: Path,
    candidate: dict[str, object],
    document: dict[str, object],
) -> None:
    payload = json.dumps(document, indent=2, sort_keys=True).encode() + b"\n"
    candidate["provenance_record"] = _write_evidence(root, "PROV-0001.json", payload)


def _external_candidate(root: Path) -> dict[str, object]:
    license_ref = _write_evidence(root, "LICENSE.txt", b"MIT fixture; sanitized\n")
    review_ref = _write_evidence(root, "review.txt", b"bounded review: PASS\n")
    tests_ref = _write_evidence(root, "tests.txt", b"offline regression/failure/rollback\n")
    decision_ref = _write_evidence(root, "decision.md", b"human approval fixture\n")
    _write_rooted(_source_root(root), "src/module.py", SOURCE_BYTES)
    _write_rooted(
        _destination_root(root),
        "qa/fixtures/adopted_module.py",
        RESULT_BYTES,
    )
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


def _validate_external(candidate: dict[str, object], root: Path):
    return validate_candidate(
        candidate,
        evidence_root=root,
        source_root=_source_root(root),
        destination_root=_destination_root(root),
    )


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
    result = _validate_external(_external_candidate(tmp_path), tmp_path)

    assert result.verdict is Verdict.PASS
    assert result.issues == ()


def test_code_bearing_candidate_without_explicit_verification_roots_is_blocked(
    tmp_path: Path,
) -> None:
    candidate = _external_candidate(tmp_path)

    result = validate_candidate(candidate, evidence_root=tmp_path)

    assert result.verdict is Verdict.BLOCKED
    assert {"SOURCE_ROOT_REQUIRED", "PROVENANCE_DESTINATION_ROOT_REQUIRED"}.issubset(_codes(result))


def test_external_candidate_and_provenance_match_machine_schemas(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    candidate_schema = json.loads(
        (root / "docs/upstream/CANDIDATE_SCHEMA.json").read_text(encoding="utf-8")
    )
    provenance_schema = json.loads(
        (root / "docs/upstream/PROVENANCE_SCHEMA.json").read_text(encoding="utf-8")
    )
    candidate = _external_candidate(tmp_path)
    provenance = _load_provenance(tmp_path, candidate)

    assert validate_json_schema(candidate, candidate_schema) == []
    assert validate_json_schema(provenance, provenance_schema) == []

    internal_candidate = {
        "schema_version": "1.0",
        "candidate_id": "INT-0001",
        "component": "web_surfer",
        "origin_kind": "commissioned_internal",
        "capability_gap": "record internal origin",
        "commissioned_by": "Dest",
        "implementation_agent": "Claude",
        "external_code_imported": False,
        "repository_url": REPOSITORY_URL,
    }
    assert validate_json_schema(internal_candidate, candidate_schema)


@pytest.mark.parametrize("identifier", [None, "   "])
def test_verified_license_identifier_matches_schema_and_runtime(
    tmp_path: Path,
    identifier: str | None,
) -> None:
    root = Path(__file__).resolve().parents[2]
    candidate_schema = json.loads(
        (root / "docs/upstream/CANDIDATE_SCHEMA.json").read_text(encoding="utf-8")
    )
    candidate = _external_candidate(tmp_path)
    license_record = candidate["license"]
    assert isinstance(license_record, dict)
    license_record["identifier"] = identifier
    _refresh_provenance(tmp_path, candidate)

    assert validate_json_schema(candidate, candidate_schema)
    result = _validate_external(candidate, tmp_path)
    assert result.verdict is Verdict.FAIL
    assert "LICENSE_SOURCE_REQUIRED" in _codes(result)


def test_populated_license_snapshot_identifier_matches_schema_and_runtime(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    provenance_schema = json.loads(
        (root / "docs/upstream/PROVENANCE_SCHEMA.json").read_text(encoding="utf-8")
    )
    candidate = _external_candidate(tmp_path)
    provenance = _load_provenance(tmp_path, candidate)
    snapshot = provenance["license_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot["identifier"] = "   "

    assert validate_json_schema(provenance, provenance_schema)
    result = validate_provenance(
        provenance,
        source_root=_source_root(tmp_path),
        destination_root=_destination_root(tmp_path),
    )
    assert result.verdict is Verdict.FAIL
    assert "INCOMPLETE_LICENSE_SNAPSHOT" in _codes(result)


def test_offline_cli_reports_machine_readable_pass(tmp_path: Path, capsys) -> None:
    candidate_path = tmp_path / "docs/upstream/candidates/UP-0001.json"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(json.dumps(_external_candidate(tmp_path)), encoding="utf-8")

    exit_code = main(
        [
            str(candidate_path),
            "--evidence-root",
            str(tmp_path),
            "--source-root",
            str(_source_root(tmp_path)),
            "--destination-root",
            str(_destination_root(tmp_path)),
        ]
    )
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

    result = _validate_external(candidate, tmp_path)

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

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.BLOCKED
    assert "LICENSE_UNKNOWN" in _codes(result)


def test_candidate_without_finding_or_capability_gap_fails(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    candidate["finding_ids"] = []

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "FINDING_OR_GAP_REQUIRED" in _codes(result)


def test_copied_code_without_sources_or_provenance_fails(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    candidate["source_files"] = []
    del candidate["provenance_record"]

    result = _validate_external(candidate, tmp_path)

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

    result = _validate_external(candidate, tmp_path)

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

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "EVIDENCE_HASH_MISMATCH" in _codes(result)


def test_unreviewed_security_gate_is_blocked(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    candidate["security_review"] = {"status": "NOT_REVIEWED"}

    result = _validate_external(candidate, tmp_path)

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

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.BLOCKED
    assert "EVIDENCE_MISSING" in _codes(result)


def test_imported_source_must_map_to_exact_reviewed_manifest(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    provenance = _load_provenance(tmp_path, candidate)
    imported_paths = provenance["imported_paths"]
    assert isinstance(imported_paths, list) and isinstance(imported_paths[0], dict)
    imported_paths[0]["source_path"] = "src/unreviewed.py"
    imported_paths[0]["transformation"] = "text cannot replace exact hashes"
    _persist_provenance(tmp_path, candidate, provenance)

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "PROVENANCE_IMPORTED_SOURCE_UNMAPPED" in _codes(result)


def test_reviewed_source_bytes_are_rehashed(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    _write_rooted(_source_root(tmp_path), "src/module.py", b"changed source bytes\n")

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "SOURCE_HASH_MISMATCH" in _codes(result)


def test_missing_reviewed_source_fails(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    (_source_root(tmp_path) / "src/module.py").unlink()

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "SOURCE_FILE_UNAVAILABLE" in _codes(result)


@pytest.mark.parametrize("mutation", [b"changed destination\n", None])
def test_imported_destination_bytes_are_independently_rehashed(
    tmp_path: Path,
    mutation: bytes | None,
) -> None:
    candidate = _external_candidate(tmp_path)
    destination = _destination_root(tmp_path) / "qa/fixtures/adopted_module.py"
    if mutation is None:
        destination.unlink()
        expected_code = "PROVENANCE_IMPORTED_DESTINATION_UNAVAILABLE"
    else:
        destination.write_bytes(mutation)
        expected_code = "PROVENANCE_IMPORTED_RESULT_HASH_MISMATCH"

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert expected_code in _codes(result)


def test_license_snapshot_digest_is_bound_to_verified_evidence(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    provenance = _load_provenance(tmp_path, candidate)
    snapshot = provenance["license_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot["sha256"] = "f" * 64
    _persist_provenance(tmp_path, candidate, provenance)

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "PROVENANCE_LICENSE_DIGEST_MISMATCH" in _codes(result)


@pytest.mark.parametrize("remove", [False, True])
def test_license_snapshot_tamper_or_absence_cannot_pass(
    tmp_path: Path,
    remove: bool,
) -> None:
    candidate = _external_candidate(tmp_path)
    license_record = candidate["license"]
    assert isinstance(license_record, dict)
    evidence = license_record["evidence"]
    assert isinstance(evidence, dict)
    target = tmp_path / Path(*str(evidence["path"]).split("/"))
    if remove:
        target.unlink()
    else:
        target.write_bytes(b"changed sanitized license snapshot\n")

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is (Verdict.BLOCKED if remove else Verdict.FAIL)
    assert ("EVIDENCE_MISSING" if remove else "EVIDENCE_HASH_MISMATCH") in _codes(result)


def test_permissive_identifier_does_not_waive_notice_or_human_review(
    tmp_path: Path,
) -> None:
    candidate = _external_candidate(tmp_path)
    license_record = candidate["license"]
    approval = candidate["human_approval"]
    assert isinstance(license_record, dict) and isinstance(approval, dict)
    license_record["notice_verified"] = False
    approval.clear()
    approval["status"] = "PENDING"
    _refresh_provenance(tmp_path, candidate)

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.BLOCKED
    assert {"NOTICE_VERIFICATION_REQUIRED", "HUMAN_APPROVAL_REQUIRED"}.issubset(_codes(result))


def test_allowed_evidence_prefix_reparse_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _external_candidate(tmp_path)
    ancestor = tmp_path / "docs/upstream/decisions"
    ancestor_stat = os.lstat(ancestor)
    original = safe_paths._is_reparse

    def simulated(stat_result: os.stat_result) -> bool:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
        ) == (ancestor_stat.st_dev, ancestor_stat.st_ino) or original(stat_result)

    monkeypatch.setattr(safe_paths, "_is_reparse", simulated)
    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "UNSAFE_EVIDENCE_PATH" in _codes(result)


@pytest.mark.parametrize(
    ("root_kind", "expected_code"),
    [
        ("source", "SOURCE_FILE_UNAVAILABLE"),
        ("destination", "PROVENANCE_IMPORTED_DESTINATION_UNAVAILABLE"),
    ],
)
def test_source_and_destination_reparse_roots_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_kind: str,
    expected_code: str,
) -> None:
    candidate = _external_candidate(tmp_path)
    ancestor = (
        _source_root(tmp_path) / "src"
        if root_kind == "source"
        else _destination_root(tmp_path) / "qa"
    )
    ancestor_stat = os.lstat(ancestor)
    original = safe_paths._is_reparse

    def simulated(stat_result: os.stat_result) -> bool:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
        ) == (ancestor_stat.st_dev, ancestor_stat.st_ino) or original(stat_result)

    monkeypatch.setattr(safe_paths, "_is_reparse", simulated)
    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert expected_code in _codes(result)


def test_allowed_evidence_prefix_symlink_escape_is_rejected(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    content = b"sanitized outside review\n"
    (outside / "review.txt").write_bytes(content)
    linked = tmp_path / "docs/upstream/linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable on this host: {exc}")
    security = candidate["security_review"]
    assert isinstance(security, dict)
    security["evidence"] = {
        "path": "docs/upstream/linked/review.txt",
        "sha256": hashlib.sha256(content).hexdigest(),
    }

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "UNSAFE_EVIDENCE_PATH" in _codes(result)


def test_source_and_destination_escapes_are_rejected_independently(
    tmp_path: Path,
) -> None:
    source_candidate = _external_candidate(tmp_path / "source-case")
    source_files = source_candidate["source_files"]
    assert isinstance(source_files, list) and isinstance(source_files[0], dict)
    source_files[0]["path"] = "../outside.py"
    _refresh_provenance(tmp_path / "source-case", source_candidate)
    source_result = _validate_external(source_candidate, tmp_path / "source-case")

    destination_candidate = _external_candidate(tmp_path / "destination-case")
    provenance = _load_provenance(tmp_path / "destination-case", destination_candidate)
    imported_paths = provenance["imported_paths"]
    assert isinstance(imported_paths, list) and isinstance(imported_paths[0], dict)
    imported_paths[0]["destination_path"] = "../outside.py"
    _persist_provenance(tmp_path / "destination-case", destination_candidate, provenance)
    destination_result = _validate_external(
        destination_candidate,
        tmp_path / "destination-case",
    )

    assert source_result.verdict is Verdict.FAIL
    assert "INVALID_SOURCE_PATH" in _codes(source_result)
    assert destination_result.verdict is Verdict.FAIL
    assert "PROVENANCE_INVALID_IMPORTED_PATH" in _codes(destination_result)


@pytest.mark.parametrize(
    "repository_url",
    [
        " https://example.invalid/acme/donor",
        "https://example.invalid/acme\\donor",
        "https://example.invalid/acme/../donor",
        "https://user@example.invalid/acme/donor",
        "https://example.invalid/acme/donor?ref=main",
        "https://example.invalid/acme/donor#fragment",
        "http://example.invalid/acme/donor",
        "https://EXAMPLE.invalid/acme/donor",
        "https://example.invalid/acme//donor",
        "https://example.invalid/acme/donor/",
        "https://example.invalid:443/acme/donor",
    ],
)
def test_noncanonical_repository_urls_fail(
    tmp_path: Path,
    repository_url: str,
) -> None:
    candidate = _external_candidate(tmp_path)
    candidate["repository_url"] = repository_url
    _refresh_provenance(tmp_path, candidate)

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "INVALID_REPOSITORY_URL" in _codes(result)


@pytest.mark.parametrize("commit", ["1" * 39, "A" * 40, "g" * 40, "1" * 41])
def test_noncanonical_full_commit_sha_fails(tmp_path: Path, commit: str) -> None:
    candidate = _external_candidate(tmp_path)
    candidate["pinned_commit_sha"] = commit
    _refresh_provenance(tmp_path, candidate)

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "PINNED_COMMIT_REQUIRED" in _codes(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("adoption_mode", "ported_module"),
        ("repository_url", REPOSITORY_URL),
        ("pinned_commit_sha", COMMIT_SHA),
        (
            "license_snapshot",
            {"identifier": "MIT", "repository_path": "LICENSE", "sha256": SOURCE_SHA},
        ),
        ("source_files", [{"path": "src/module.py", "sha256": SOURCE_SHA}]),
        ("imported_paths", []),
    ],
)
def test_internal_provenance_rejects_external_only_fields(
    field: str,
    value: object,
) -> None:
    provenance: dict[str, object] = {
        "schema_version": "1.0",
        "record_id": "PROV-INT-0001",
        "candidate_id": "INT-0001",
        "component": "web_surfer",
        "origin_kind": "commissioned_internal",
        "commissioned_by": "Dest",
        "implementation_agent": "Claude",
        "external_code_imported": False,
        "recorded_at": "2026-07-13T00:30:00Z",
        "recorded_by": "Assurance reviewer",
        field: value,
    }

    result = validate_provenance(provenance)

    assert result.verdict is Verdict.FAIL
    assert "INTERNAL_ORIGIN_CONFLICT" in _codes(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("adoption_mode", "ported_module"),
        ("repository_url", REPOSITORY_URL),
        ("pinned_commit_sha", COMMIT_SHA),
        ("source_files", [{"path": "src/module.py", "sha256": SOURCE_SHA}]),
    ],
)
def test_internal_candidate_rejects_external_only_fields(
    field: str,
    value: object,
) -> None:
    candidate: dict[str, object] = {
        "schema_version": "1.0",
        "candidate_id": "INT-0001",
        "component": "web_surfer",
        "origin_kind": "commissioned_internal",
        "capability_gap": "record internal origin",
        "commissioned_by": "Dest",
        "implementation_agent": "Claude",
        "external_code_imported": False,
        field: value,
    }

    result = validate_candidate(candidate)

    assert result.verdict is Verdict.FAIL
    assert "INTERNAL_ORIGIN_CONFLICT" in _codes(result)


def test_external_candidate_rejects_internal_commissioning_fields(tmp_path: Path) -> None:
    candidate = _external_candidate(tmp_path)
    candidate["commissioned_by"] = "contradictory"

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "EXTERNAL_ORIGIN_CONFLICT" in _codes(result)


def test_external_provenance_rejects_internal_commissioning_fields(
    tmp_path: Path,
) -> None:
    candidate = _external_candidate(tmp_path)
    provenance = _load_provenance(tmp_path, candidate)
    provenance["commissioned_by"] = "contradictory"
    _persist_provenance(tmp_path, candidate, provenance)

    result = _validate_external(candidate, tmp_path)

    assert result.verdict is Verdict.FAIL
    assert "PROVENANCE_EXTERNAL_ORIGIN_CONFLICT" in _codes(result)
