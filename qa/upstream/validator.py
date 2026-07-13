"""Standard-library-only upstream candidate and provenance validator.

The validator is intentionally offline. It verifies structure, internal
consistency, explicitly referenced local evidence paths, and SHA-256 digests.
It does not discover evidence, contact upstream repositories, execute donor
code, or make legal/security judgments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from ..output import safe_json_text
from ..safe_paths import (
    SafePathError,
    bounded_file_bytes,
    bounded_file_digest,
    validate_relative_path,
)

ORIGIN_KINDS = (
    "internal_human",
    "commissioned_internal",
    "inspired_by",
    "external_dependency",
    "external_adapter",
    "vendored",
    "ported_code",
    "forked",
    "generated_fixture",
)
ADOPTION_MODES = (
    "idea_only",
    "test_corpus",
    "external_dependency",
    "black_box_adapter",
    "ported_module",
    "fork",
)
EXTERNAL_ORIGIN_KINDS = frozenset(
    {
        "inspired_by",
        "external_dependency",
        "external_adapter",
        "vendored",
        "ported_code",
        "forked",
    }
)
COPIED_CODE_MODES = frozenset({"test_corpus", "ported_module", "fork"})
NON_COPYING_MODES = frozenset({"idea_only", "external_dependency", "black_box_adapter"})
ALLOWED_EVIDENCE_PREFIXES = ("docs/upstream", "docs/assurance", "qa")
MAX_UPSTREAM_EVIDENCE_BYTES = 1024 * 1024
MAX_UPSTREAM_SOURCE_BYTES = 16 * 1024 * 1024

_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{2,63}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HOST_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)
_REPOSITORY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._~-]*[A-Za-z0-9])?$")
_FORBIDDEN_LOCAL_PARTS = frozenset({".audit", ".git"})
_LICENSE_CLASSES = frozenset(
    {
        "permissive",
        "weak_copyleft",
        "strong_copyleft",
        "custom",
        "source_available",
        "no_license",
        "unknown",
    }
)
_LICENSE_STATUSES = frozenset(
    {
        "VERIFIED",
        "EXPLICIT_REVIEW_APPROVED",
        "EXPLICIT_REVIEW_REQUIRED",
        "BLOCKED",
        "UNKNOWN",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "component",
        "origin_kind",
        "finding_ids",
        "capability_gap",
        "commissioned_by",
        "implementation_agent",
        "external_code_imported",
        "adoption_mode",
        "repository_url",
        "pinned_commit_sha",
        "license",
        "source_files",
        "dependency_review",
        "security_review",
        "isolated_spike",
        "tests",
        "provenance_record",
        "human_approval",
    }
)
_EXTERNAL_ONLY_FIELDS = frozenset(
    {
        "adoption_mode",
        "repository_url",
        "pinned_commit_sha",
        "license",
        "source_files",
        "dependency_review",
        "security_review",
        "isolated_spike",
        "tests",
        "provenance_record",
        "human_approval",
    }
)
_PROVENANCE_EXTERNAL_ONLY_FIELDS = frozenset(
    {
        "adoption_mode",
        "repository_url",
        "pinned_commit_sha",
        "license_snapshot",
        "source_files",
        "imported_paths",
        "transformation_notes",
        "retained_notices",
    }
)
_INTERNAL_ONLY_FIELDS = frozenset({"commissioned_by", "implementation_agent"})
_PROVENANCE_FIELDS = frozenset(
    {
        "schema_version",
        "record_id",
        "candidate_id",
        "component",
        "origin_kind",
        "commissioned_by",
        "implementation_agent",
        "adoption_mode",
        "repository_url",
        "pinned_commit_sha",
        "license_snapshot",
        "external_code_imported",
        "source_files",
        "imported_paths",
        "transformation_notes",
        "retained_notices",
        "recorded_at",
        "recorded_by",
    }
)


class Verdict(str, Enum):
    """Machine gate result, distinct from the human adoption decision."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ValidationIssue:
    """One deterministic error or unresolved blocker."""

    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


@dataclass(frozen=True)
class ValidationResult:
    """Immutable validation result."""

    verdict: Verdict
    issues: tuple[ValidationIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True, slots=True)
class _VerifiedEvidence:
    relative_path: str
    sha256: str
    content: bytes


@dataclass
class _Context:
    evidence_root: Path | None = None
    source_root: Path | None = None
    destination_root: Path | None = None
    errors: list[ValidationIssue] = field(default_factory=list)
    blockers: list[ValidationIssue] = field(default_factory=list)

    def fail(self, code: str, path: str, message: str) -> None:
        self.errors.append(ValidationIssue("ERROR", code, path, message))

    def block(self, code: str, path: str, message: str) -> None:
        self.blockers.append(ValidationIssue("BLOCKER", code, path, message))

    def result(self) -> ValidationResult:
        if self.errors:
            verdict = Verdict.FAIL
        elif self.blockers:
            verdict = Verdict.BLOCKED
        else:
            verdict = Verdict.PASS
        return ValidationResult(verdict, tuple(self.errors + self.blockers))


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _required(record: Mapping[str, Any], names: Sequence[str], ctx: _Context) -> None:
    for name in names:
        if name not in record:
            ctx.fail("MISSING_FIELD", name, "required field is missing")


def _reject_unknown_fields(
    record: Mapping[str, Any], allowed: frozenset[str], path: str, ctx: _Context
) -> None:
    for name in sorted(set(record) - allowed):
        issue_path = f"{path}.{name}" if path else name
        ctx.fail("UNKNOWN_FIELD", issue_path, "field is not defined by the schema")


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_timestamp(value: Any, path: str, ctx: _Context) -> None:
    if not _nonempty_string(value):
        ctx.fail("INVALID_TIMESTAMP", path, "timestamp must be a non-empty RFC 3339 value")
        return
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        ctx.fail("INVALID_TIMESTAMP", path, "timestamp is not a valid RFC 3339 value")
        return
    if parsed.tzinfo is None:
        ctx.fail("INVALID_TIMESTAMP", path, "timestamp must include an explicit UTC offset")


def _validate_repo_url(value: Any, path: str, ctx: _Context) -> None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
        or "\\" in value
        or "%" in value
    ):
        ctx.fail("INVALID_REPOSITORY_URL", path, "repository URL must be an exact string")
        return
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        parsed = None
        port = None
    hostname = parsed.hostname if parsed is not None else None
    segments = parsed.path.split("/")[1:] if parsed is not None else []
    if (
        parsed is None
        or parsed.scheme != "https"
        or not hostname
        or not _HOST_RE.fullmatch(hostname)
        or parsed.netloc != hostname
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.path.endswith("/")
        or "//" in parsed.path
        or len(segments) < 2
        or any(
            segment in {"", ".", ".."} or not _REPOSITORY_SEGMENT_RE.fullmatch(segment)
            for segment in segments
        )
        or parsed.query
        or parsed.fragment
        or value != f"https://{hostname}{parsed.path}"
    ):
        ctx.fail(
            "INVALID_REPOSITORY_URL",
            path,
            "repository URL must be canonical HTTPS without credentials, query, or fragment",
        )


def _relative_posix_path(value: Any) -> bool:
    try:
        safe = validate_relative_path(value)
    except (SafePathError, ValueError):
        return False
    return not any(part.casefold() in _FORBIDDEN_LOCAL_PARTS for part in PurePosixPath(safe).parts)


def _validate_evidence_reference(value: Any, path: str, ctx: _Context) -> _VerifiedEvidence | None:
    if not _is_mapping(value):
        ctx.fail("INVALID_EVIDENCE_REFERENCE", path, "evidence reference must be an object")
        return None
    _reject_unknown_fields(value, frozenset({"path", "sha256"}), path, ctx)
    _required(value, ("path", "sha256"), ctx)
    relative = value.get("path")
    expected_hash = value.get("sha256")
    if not _relative_posix_path(relative):
        ctx.fail(
            "UNSAFE_EVIDENCE_PATH",
            f"{path}.path",
            "evidence path must be repository-relative under an allowed sanitized prefix",
        )
        return None
    relative_parts = PurePosixPath(relative).parts
    matched_prefix: tuple[str, ...] | None = None
    for prefix in ALLOWED_EVIDENCE_PREFIXES:
        prefix_parts = PurePosixPath(prefix).parts
        if (
            len(relative_parts) > len(prefix_parts)
            and relative_parts[: len(prefix_parts)] == prefix_parts
        ):
            matched_prefix = prefix_parts
            break
    if matched_prefix is None:
        ctx.fail(
            "UNSAFE_EVIDENCE_PATH",
            f"{path}.path",
            "evidence path must be repository-relative under an allowed sanitized prefix",
        )
        return None
    if not isinstance(expected_hash, str) or not _SHA256_RE.fullmatch(expected_hash):
        ctx.fail("INVALID_SHA256", f"{path}.sha256", "SHA-256 must be 64 lowercase hex")
        return None
    if ctx.evidence_root is None:
        ctx.block(
            "EVIDENCE_ROOT_REQUIRED",
            path,
            "local evidence root is required before evidence can be verified",
        )
        return None
    allowed_root = ctx.evidence_root.joinpath(*matched_prefix)
    bounded_relative = "/".join(relative_parts[len(matched_prefix) :])
    try:
        content = bounded_file_bytes(
            allowed_root,
            bounded_relative,
            max_bytes=MAX_UPSTREAM_EVIDENCE_BYTES,
        )
    except SafePathError as exc:
        if exc.code in {"FILE_MISSING", "ROOT_UNAVAILABLE"}:
            ctx.block("EVIDENCE_MISSING", f"{path}.path", "referenced evidence file is absent")
        else:
            ctx.fail(
                "UNSAFE_EVIDENCE_PATH",
                f"{path}.path",
                "evidence path could not be read within its exact allowed prefix",
            )
        return None
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != expected_hash:
        ctx.fail(
            "EVIDENCE_HASH_MISMATCH",
            f"{path}.sha256",
            f"declared {expected_hash}, observed {actual_hash}",
        )
        return None
    return _VerifiedEvidence(relative, actual_hash, content)


def _validate_source_files(
    value: Any, path: str, ctx: _Context, *, allow_empty: bool
) -> dict[str, str]:
    if not isinstance(value, list):
        ctx.fail("INVALID_SOURCE_FILES", path, "source_files must be an array")
        return {}
    if not value and not allow_empty:
        ctx.fail("SOURCE_FILES_REQUIRED", path, "this adoption mode requires source files")
    if value and ctx.source_root is None:
        ctx.block(
            "SOURCE_ROOT_REQUIRED",
            path,
            "an explicit reviewed source root is required for raw-byte verification",
        )
    seen: set[str] = set()
    manifest: dict[str, str] = {}
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not _is_mapping(item):
            ctx.fail("INVALID_SOURCE_FILE", item_path, "source file must be an object")
            continue
        _reject_unknown_fields(item, frozenset({"path", "sha256"}), item_path, ctx)
        _required(item, ("path", "sha256"), ctx)
        source_path = item.get("path")
        digest = item.get("sha256")
        path_valid = False
        if not _relative_posix_path(source_path):
            ctx.fail("INVALID_SOURCE_PATH", f"{item_path}.path", "source path is unsafe")
        elif source_path in seen:
            ctx.fail("DUPLICATE_SOURCE_PATH", f"{item_path}.path", "source path is duplicated")
        else:
            seen.add(source_path)
            path_valid = True
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            ctx.fail("INVALID_SHA256", f"{item_path}.sha256", "SHA-256 must be lowercase hex")
            continue
        if not path_valid:
            continue
        manifest[source_path] = digest
        if ctx.source_root is None:
            continue
        try:
            actual = bounded_file_digest(
                ctx.source_root,
                source_path,
                max_bytes=MAX_UPSTREAM_SOURCE_BYTES,
            )
        except SafePathError:
            ctx.fail(
                "SOURCE_FILE_UNAVAILABLE",
                f"{item_path}.path",
                "reviewed source is missing or outside its exact safe root",
            )
            continue
        if actual.sha256 != digest:
            ctx.fail(
                "SOURCE_HASH_MISMATCH",
                f"{item_path}.sha256",
                "reviewed source raw-byte SHA-256 does not match the manifest",
            )
    return manifest


def _validate_gate_review(value: Any, path: str, ctx: _Context) -> None:
    if not _is_mapping(value):
        ctx.fail("INVALID_GATE_REVIEW", path, "gate review must be an object")
        return
    _reject_unknown_fields(value, frozenset({"status", "evidence"}), path, ctx)
    _required(value, ("status",), ctx)
    status = value.get("status")
    if status not in {"PASS", "FAIL", "BLOCKED", "NOT_REVIEWED"}:
        ctx.fail("INVALID_REVIEW_STATUS", f"{path}.status", "review status is invalid")
        return
    if status in {"PASS", "FAIL"}:
        if "evidence" not in value:
            ctx.fail(
                "REVIEW_EVIDENCE_REQUIRED",
                f"{path}.evidence",
                "asserted review result requires immutable evidence",
            )
        else:
            _validate_evidence_reference(value["evidence"], f"{path}.evidence", ctx)
    if status == "FAIL":
        ctx.fail("GATE_REVIEW_FAILED", f"{path}.status", "review recorded FAIL")
    elif status in {"BLOCKED", "NOT_REVIEWED"}:
        ctx.block("GATE_REVIEW_INCOMPLETE", f"{path}.status", f"review is {status}")


def _validate_license(value: Any, path: str, ctx: _Context) -> _VerifiedEvidence | None:
    if not _is_mapping(value):
        ctx.fail("INVALID_LICENSE_RECORD", path, "license must be an object")
        return None
    allowed = frozenset(
        {
            "classification",
            "identifier",
            "verification_status",
            "repository_path",
            "notice_verified",
            "evidence",
        }
    )
    _reject_unknown_fields(value, allowed, path, ctx)
    _required(
        value,
        (
            "classification",
            "identifier",
            "verification_status",
            "repository_path",
            "notice_verified",
        ),
        ctx,
    )
    classification = value.get("classification")
    status = value.get("verification_status")
    identifier = value.get("identifier")
    repository_path = value.get("repository_path")
    notice_verified = value.get("notice_verified")
    if classification not in _LICENSE_CLASSES:
        ctx.fail("INVALID_LICENSE_CLASS", f"{path}.classification", "unknown classification")
        return None
    if status not in _LICENSE_STATUSES:
        ctx.fail("INVALID_LICENSE_STATUS", f"{path}.verification_status", "unknown status")
        return None
    if not isinstance(notice_verified, bool):
        ctx.fail("INVALID_NOTICE_STATUS", f"{path}.notice_verified", "must be a boolean")

    if classification in {"unknown", "no_license"} or status in {"UNKNOWN", "BLOCKED"}:
        code = "LICENSE_UNKNOWN" if classification == "unknown" else "LICENSE_BLOCKED"
        ctx.block(code, path, "license does not permit this adoption gate to pass")
        return None

    if classification == "permissive":
        if status != "VERIFIED":
            ctx.block("LICENSE_VERIFICATION_REQUIRED", path, "permissive license is unverified")
            return None
        if notice_verified is not True:
            ctx.block("NOTICE_VERIFICATION_REQUIRED", path, "required notices are unverified")
            return None
    elif status != "EXPLICIT_REVIEW_APPROVED":
        ctx.block("LICENSE_REVIEW_REQUIRED", path, "explicit license review is required")
        return None

    if not _nonempty_string(identifier) or not _relative_posix_path(repository_path):
        ctx.fail(
            "LICENSE_SOURCE_REQUIRED",
            path,
            "verified license requires identifier and repository-relative source path",
        )
    if "evidence" not in value:
        ctx.fail(
            "LICENSE_EVIDENCE_REQUIRED",
            f"{path}.evidence",
            "verified or approved license requires immutable evidence",
        )
        return None
    return _validate_evidence_reference(value["evidence"], f"{path}.evidence", ctx)


def _validate_tests(value: Any, path: str, ctx: _Context) -> None:
    if not _is_mapping(value):
        ctx.fail("INVALID_TEST_RECORD", path, "tests must be an object")
        return
    kinds = ("regression", "failure", "rollback")
    _reject_unknown_fields(value, frozenset(kinds), path, ctx)
    _required(value, kinds, ctx)
    for kind in kinds:
        entries = value.get(kind)
        kind_path = f"{path}.{kind}"
        if not isinstance(entries, list) or not entries:
            ctx.fail("TEST_EVIDENCE_REQUIRED", kind_path, "at least one test is required")
            continue
        for index, entry in enumerate(entries):
            _validate_evidence_reference(entry, f"{kind_path}[{index}]", ctx)


def _validate_human_approval(value: Any, path: str, ctx: _Context) -> None:
    if not _is_mapping(value):
        ctx.fail("INVALID_HUMAN_APPROVAL", path, "human_approval must be an object")
        return
    allowed = frozenset({"status", "approved_by", "approved_at", "evidence"})
    _reject_unknown_fields(value, allowed, path, ctx)
    _required(value, ("status",), ctx)
    status = value.get("status")
    if status not in {"APPROVED", "REJECTED", "PENDING"}:
        ctx.fail("INVALID_APPROVAL_STATUS", f"{path}.status", "approval status is invalid")
        return
    if status == "PENDING":
        ctx.block("HUMAN_APPROVAL_REQUIRED", f"{path}.status", "human approval is pending")
        return
    for field_name in ("approved_by", "approved_at", "evidence"):
        if field_name not in value:
            ctx.fail(
                "APPROVAL_EVIDENCE_REQUIRED",
                f"{path}.{field_name}",
                "recorded decision requires this field",
            )
    if not _nonempty_string(value.get("approved_by")):
        ctx.fail("INVALID_APPROVER", f"{path}.approved_by", "approver must be named")
    if "approved_at" in value:
        _validate_timestamp(value.get("approved_at"), f"{path}.approved_at", ctx)
    if "evidence" in value:
        _validate_evidence_reference(value["evidence"], f"{path}.evidence", ctx)
    if status == "REJECTED":
        ctx.fail("HUMAN_APPROVAL_REJECTED", f"{path}.status", "human decision rejected adoption")


def _validate_common_identity(record: Mapping[str, Any], ctx: _Context) -> str | None:
    _required(record, ("schema_version", "candidate_id", "component", "origin_kind"), ctx)
    if record.get("schema_version") != "1.0":
        ctx.fail("SCHEMA_VERSION", "schema_version", "supported schema_version is 1.0")
    candidate_id = record.get("candidate_id")
    if not isinstance(candidate_id, str) or not _ID_RE.fullmatch(candidate_id):
        ctx.fail("INVALID_CANDIDATE_ID", "candidate_id", "candidate ID is invalid")
    if not _nonempty_string(record.get("component")):
        ctx.fail("INVALID_COMPONENT", "component", "component must be non-empty")
    origin = record.get("origin_kind")
    if origin not in ORIGIN_KINDS:
        ctx.fail("INVALID_ORIGIN_KIND", "origin_kind", "origin kind is unsupported")
        return None
    return origin


def _validate_finding_or_gap(record: Mapping[str, Any], ctx: _Context) -> None:
    finding_ids = record.get("finding_ids")
    gap = record.get("capability_gap")
    valid_findings = isinstance(finding_ids, list) and bool(finding_ids)
    if finding_ids is not None:
        if not isinstance(finding_ids, list) or any(
            not _nonempty_string(item) for item in finding_ids
        ):
            ctx.fail("INVALID_FINDING_IDS", "finding_ids", "finding_ids must be non-empty strings")
        elif len(finding_ids) != len(set(finding_ids)):
            ctx.fail("DUPLICATE_FINDING_ID", "finding_ids", "finding IDs must be unique")
    if gap is not None and not _nonempty_string(gap):
        ctx.fail("INVALID_CAPABILITY_GAP", "capability_gap", "capability gap is empty")
    if not valid_findings and not _nonempty_string(gap):
        ctx.fail(
            "FINDING_OR_GAP_REQUIRED",
            "finding_ids",
            "a reproduced finding or concrete capability gap is required",
        )


def _validate_internal_candidate(record: Mapping[str, Any], origin: str, ctx: _Context) -> None:
    conflicting = sorted(_EXTERNAL_ONLY_FIELDS.intersection(record))
    for name in conflicting:
        ctx.fail(
            "INTERNAL_ORIGIN_CONFLICT",
            name,
            "internal origin must not carry external adoption fields",
        )
    imported = record.get("external_code_imported", False)
    if not isinstance(imported, bool):
        ctx.fail("INVALID_EXTERNAL_CODE_FLAG", "external_code_imported", "must be a boolean")
    elif imported:
        ctx.fail(
            "INTERNAL_ORIGIN_CONFLICT",
            "external_code_imported",
            "internal origin cannot claim imported external code",
        )
    if origin == "commissioned_internal":
        _required(
            record,
            ("commissioned_by", "implementation_agent", "external_code_imported"),
            ctx,
        )
        if not _nonempty_string(record.get("commissioned_by")):
            ctx.fail("INVALID_COMMISSIONER", "commissioned_by", "commissioner must be named")
        if not _nonempty_string(record.get("implementation_agent")):
            ctx.fail(
                "INVALID_IMPLEMENTATION_AGENT",
                "implementation_agent",
                "implementation agent must be named",
            )
    else:
        for name in sorted(_INTERNAL_ONLY_FIELDS.intersection(record)):
            ctx.fail(
                "INTERNAL_ORIGIN_CONFLICT",
                name,
                "only commissioned_internal may carry commissioning metadata",
            )


def _merge_provenance_result(result: ValidationResult, ctx: _Context) -> None:
    for issue in result.issues:
        code = f"PROVENANCE_{issue.code}"
        path = f"provenance_record.document.{issue.path}"
        if issue.severity == "ERROR":
            ctx.fail(code, path, issue.message)
        else:
            ctx.block(code, path, issue.message)


def _validate_provenance_reference(
    reference: Any,
    candidate: Mapping[str, Any],
    license_evidence: _VerifiedEvidence | None,
    ctx: _Context,
) -> None:
    verified = _validate_evidence_reference(reference, "provenance_record", ctx)
    if verified is None:
        return
    try:
        document = json.loads(verified.content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        ctx.fail("INVALID_PROVENANCE_JSON", "provenance_record", str(exc))
        return
    result = validate_provenance(
        document,
        source_root=ctx.source_root,
        destination_root=ctx.destination_root,
    )
    _merge_provenance_result(result, ctx)
    if not _is_mapping(document):
        return
    pairs = (
        ("candidate_id", "candidate_id"),
        ("component", "component"),
        ("origin_kind", "origin_kind"),
        ("adoption_mode", "adoption_mode"),
        ("repository_url", "repository_url"),
        ("pinned_commit_sha", "pinned_commit_sha"),
        ("external_code_imported", "external_code_imported"),
        ("source_files", "source_files"),
    )
    for provenance_name, candidate_name in pairs:
        if document.get(provenance_name) != candidate.get(candidate_name):
            ctx.fail(
                "PROVENANCE_MISMATCH",
                f"provenance_record.document.{provenance_name}",
                f"does not match candidate field {candidate_name}",
            )
    license_record = candidate.get("license")
    snapshot = document.get("license_snapshot")
    if _is_mapping(license_record) and _is_mapping(snapshot):
        for name in ("identifier", "repository_path"):
            if snapshot.get(name) != license_record.get(name):
                ctx.fail(
                    "PROVENANCE_LICENSE_MISMATCH",
                    f"provenance_record.document.license_snapshot.{name}",
                    "license snapshot does not match candidate",
                )
        if license_evidence is not None and snapshot.get("sha256") != license_evidence.sha256:
            ctx.fail(
                "PROVENANCE_LICENSE_DIGEST_MISMATCH",
                "provenance_record.document.license_snapshot.sha256",
                "license snapshot digest does not match the verified candidate evidence bytes",
            )


def validate_candidate(
    document: Any,
    *,
    evidence_root: str | Path | None = None,
    source_root: str | Path | None = None,
    destination_root: str | Path | None = None,
) -> ValidationResult:
    """Validate one decoded candidate without network access or mutation."""

    ctx = _Context(
        evidence_root=Path(evidence_root) if evidence_root is not None else None,
        source_root=Path(source_root) if source_root is not None else None,
        destination_root=(Path(destination_root) if destination_root is not None else None),
    )
    if not _is_mapping(document):
        ctx.fail("INVALID_DOCUMENT", "$", "candidate document must be a JSON object")
        return ctx.result()
    _reject_unknown_fields(document, _CANDIDATE_FIELDS, "", ctx)
    origin = _validate_common_identity(document, ctx)
    _validate_finding_or_gap(document, ctx)
    if origin is None:
        return ctx.result()
    if origin not in EXTERNAL_ORIGIN_KINDS:
        _validate_internal_candidate(document, origin, ctx)
        return ctx.result()

    for name in sorted(_INTERNAL_ONLY_FIELDS.intersection(document)):
        ctx.fail(
            "EXTERNAL_ORIGIN_CONFLICT",
            name,
            "external origin must not carry internal commissioning metadata",
        )

    required_external = (
        "external_code_imported",
        "adoption_mode",
        "repository_url",
        "pinned_commit_sha",
        "license",
        "source_files",
        "dependency_review",
        "security_review",
        "isolated_spike",
        "tests",
        "provenance_record",
        "human_approval",
    )
    _required(document, required_external, ctx)
    mode = document.get("adoption_mode")
    if mode not in ADOPTION_MODES:
        ctx.fail("INVALID_ADOPTION_MODE", "adoption_mode", "adoption mode is unsupported")
    _validate_repo_url(document.get("repository_url"), "repository_url", ctx)
    commit = document.get("pinned_commit_sha")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        ctx.fail(
            "PINNED_COMMIT_REQUIRED",
            "pinned_commit_sha",
            "pinned commit must be exactly 40 lowercase hexadecimal characters",
        )
    imported = document.get("external_code_imported")
    if not isinstance(imported, bool):
        ctx.fail("INVALID_EXTERNAL_CODE_FLAG", "external_code_imported", "must be a boolean")
    elif mode in COPIED_CODE_MODES and imported is not True:
        ctx.fail(
            "COPIED_CODE_FLAG_REQUIRED",
            "external_code_imported",
            "copied-code adoption must declare imported external code",
        )
    elif mode in NON_COPYING_MODES and imported is not False:
        ctx.fail(
            "NON_COPYING_MODE_CONFLICT",
            "external_code_imported",
            "non-copying adoption mode cannot declare imported code",
        )
    license_evidence = None
    if "license" in document:
        license_evidence = _validate_license(document["license"], "license", ctx)
    if "source_files" in document:
        _validate_source_files(
            document["source_files"],
            "source_files",
            ctx,
            allow_empty=mode == "idea_only",
        )
    for review_name in ("dependency_review", "security_review", "isolated_spike"):
        if review_name in document:
            _validate_gate_review(document[review_name], review_name, ctx)
    if "tests" in document:
        _validate_tests(document["tests"], "tests", ctx)
    if "provenance_record" in document:
        _validate_provenance_reference(
            document["provenance_record"],
            document,
            license_evidence,
            ctx,
        )
    if "human_approval" in document:
        _validate_human_approval(document["human_approval"], "human_approval", ctx)
    return ctx.result()


def _validate_license_snapshot(value: Any, path: str, ctx: _Context) -> None:
    if not _is_mapping(value):
        ctx.fail("INVALID_LICENSE_SNAPSHOT", path, "license_snapshot must be an object")
        return
    allowed = frozenset({"identifier", "repository_path", "sha256"})
    _reject_unknown_fields(value, allowed, path, ctx)
    _required(value, ("identifier", "repository_path", "sha256"), ctx)
    identifier = value.get("identifier")
    repository_path = value.get("repository_path")
    digest = value.get("sha256")
    all_null = identifier is None and repository_path is None and digest is None
    all_valid = (
        _nonempty_string(identifier)
        and _relative_posix_path(repository_path)
        and isinstance(digest, str)
        and bool(_SHA256_RE.fullmatch(digest))
    )
    if not all_null and not all_valid:
        ctx.fail(
            "INCOMPLETE_LICENSE_SNAPSHOT",
            path,
            "license snapshot must be entirely populated or entirely null",
        )


def _validate_imported_paths(
    value: Any,
    path: str,
    ctx: _Context,
    *,
    source_manifest: Mapping[str, str],
) -> None:
    if not isinstance(value, list):
        ctx.fail("INVALID_IMPORTED_PATHS", path, "imported_paths must be an array")
        return
    if value and ctx.destination_root is None:
        ctx.block(
            "DESTINATION_ROOT_REQUIRED",
            path,
            "an explicit destination root is required for raw-byte verification",
        )
    allowed = frozenset(
        {
            "source_path",
            "destination_path",
            "source_sha256",
            "result_sha256",
            "transformation",
        }
    )
    destinations: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not _is_mapping(item):
            ctx.fail("INVALID_IMPORTED_PATH", item_path, "imported path must be an object")
            continue
        _reject_unknown_fields(item, allowed, item_path, ctx)
        _required(item, tuple(sorted(allowed)), ctx)
        valid_paths: dict[str, bool] = {}
        for field_name in ("source_path", "destination_path"):
            valid_paths[field_name] = _relative_posix_path(item.get(field_name))
            if not valid_paths[field_name]:
                ctx.fail(
                    "INVALID_IMPORTED_PATH",
                    f"{item_path}.{field_name}",
                    "path must be safe and repository-relative",
                )
        destination = item.get("destination_path")
        if isinstance(destination, str):
            if destination in destinations:
                ctx.fail(
                    "DUPLICATE_IMPORTED_DESTINATION",
                    f"{item_path}.destination_path",
                    "destination is duplicated",
                )
            destinations.add(destination)
        for field_name in ("source_sha256", "result_sha256"):
            digest = item.get(field_name)
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                ctx.fail(
                    "INVALID_SHA256",
                    f"{item_path}.{field_name}",
                    "SHA-256 must be 64 lowercase hex",
                )
        source_path = item.get("source_path")
        source_sha256 = item.get("source_sha256")
        if valid_paths["source_path"] and isinstance(source_path, str):
            declared_source_sha256 = source_manifest.get(source_path)
            if declared_source_sha256 is None:
                ctx.fail(
                    "IMPORTED_SOURCE_UNMAPPED",
                    f"{item_path}.source_path",
                    "imported source does not exist in the exact reviewed source manifest",
                )
            elif source_sha256 != declared_source_sha256:
                ctx.fail(
                    "IMPORTED_SOURCE_HASH_MISMATCH",
                    f"{item_path}.source_sha256",
                    "imported source digest does not match its reviewed manifest entry",
                )
        result_sha256 = item.get("result_sha256")
        if (
            valid_paths["destination_path"]
            and isinstance(destination, str)
            and isinstance(result_sha256, str)
            and _SHA256_RE.fullmatch(result_sha256)
            and ctx.destination_root is not None
        ):
            try:
                actual_result = bounded_file_digest(
                    ctx.destination_root,
                    destination,
                    max_bytes=MAX_UPSTREAM_SOURCE_BYTES,
                )
            except SafePathError:
                ctx.fail(
                    "IMPORTED_DESTINATION_UNAVAILABLE",
                    f"{item_path}.destination_path",
                    "imported destination is missing or outside its exact safe root",
                )
            else:
                if actual_result.sha256 != result_sha256:
                    ctx.fail(
                        "IMPORTED_RESULT_HASH_MISMATCH",
                        f"{item_path}.result_sha256",
                        "destination raw-byte SHA-256 does not match the provenance record",
                    )
        if not _nonempty_string(item.get("transformation")):
            ctx.fail(
                "TRANSFORMATION_REQUIRED",
                f"{item_path}.transformation",
                "transformation must be described",
            )


def validate_provenance(
    document: Any,
    *,
    source_root: str | Path | None = None,
    destination_root: str | Path | None = None,
) -> ValidationResult:
    """Validate one decoded provenance record without resolving external data."""

    ctx = _Context(
        source_root=Path(source_root) if source_root is not None else None,
        destination_root=(Path(destination_root) if destination_root is not None else None),
    )
    if not _is_mapping(document):
        ctx.fail("INVALID_DOCUMENT", "$", "provenance document must be a JSON object")
        return ctx.result()
    _reject_unknown_fields(document, _PROVENANCE_FIELDS, "", ctx)
    common = (
        "schema_version",
        "record_id",
        "candidate_id",
        "component",
        "origin_kind",
        "recorded_at",
        "recorded_by",
    )
    _required(document, common, ctx)
    if document.get("schema_version") != "1.0":
        ctx.fail("SCHEMA_VERSION", "schema_version", "supported schema_version is 1.0")
    for name in ("record_id", "candidate_id"):
        value = document.get(name)
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            ctx.fail("INVALID_ID", name, "identifier is invalid")
    if not _nonempty_string(document.get("component")):
        ctx.fail("INVALID_COMPONENT", "component", "component must be non-empty")
    if not _nonempty_string(document.get("recorded_by")):
        ctx.fail("INVALID_RECORDER", "recorded_by", "recorder must be named")
    if "recorded_at" in document:
        _validate_timestamp(document.get("recorded_at"), "recorded_at", ctx)
    origin = document.get("origin_kind")
    if origin not in ORIGIN_KINDS:
        ctx.fail("INVALID_ORIGIN_KIND", "origin_kind", "origin kind is unsupported")
        return ctx.result()
    if origin not in EXTERNAL_ORIGIN_KINDS:
        for name in sorted(_PROVENANCE_EXTERNAL_ONLY_FIELDS.intersection(document)):
            ctx.fail(
                "INTERNAL_ORIGIN_CONFLICT",
                name,
                "internal provenance must not carry external adoption fields",
            )
        imported = document.get("external_code_imported", False)
        if imported is not False:
            ctx.fail(
                "INTERNAL_ORIGIN_CONFLICT",
                "external_code_imported",
                "internal provenance cannot declare imported external code",
            )
        if origin == "commissioned_internal":
            _required(
                document,
                ("commissioned_by", "implementation_agent", "external_code_imported"),
                ctx,
            )
            if not _nonempty_string(document.get("commissioned_by")):
                ctx.fail("INVALID_COMMISSIONER", "commissioned_by", "commissioner is required")
            if not _nonempty_string(document.get("implementation_agent")):
                ctx.fail(
                    "INVALID_IMPLEMENTATION_AGENT",
                    "implementation_agent",
                    "implementation agent is required",
                )
        else:
            for name in sorted(_INTERNAL_ONLY_FIELDS.intersection(document)):
                ctx.fail(
                    "INTERNAL_ORIGIN_CONFLICT",
                    name,
                    "only commissioned_internal may carry commissioning metadata",
                )
        return ctx.result()

    for name in sorted(_INTERNAL_ONLY_FIELDS.intersection(document)):
        ctx.fail(
            "EXTERNAL_ORIGIN_CONFLICT",
            name,
            "external provenance must not carry internal commissioning metadata",
        )

    external_fields = (
        "adoption_mode",
        "repository_url",
        "pinned_commit_sha",
        "license_snapshot",
        "external_code_imported",
        "source_files",
        "imported_paths",
        "transformation_notes",
        "retained_notices",
    )
    _required(document, external_fields, ctx)
    mode = document.get("adoption_mode")
    if mode not in ADOPTION_MODES:
        ctx.fail("INVALID_ADOPTION_MODE", "adoption_mode", "adoption mode is unsupported")
    _validate_repo_url(document.get("repository_url"), "repository_url", ctx)
    commit = document.get("pinned_commit_sha")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        ctx.fail("PINNED_COMMIT_REQUIRED", "pinned_commit_sha", "invalid pinned commit SHA")
    if "license_snapshot" in document:
        _validate_license_snapshot(document["license_snapshot"], "license_snapshot", ctx)
    source_manifest: dict[str, str] = {}
    if "source_files" in document:
        source_manifest = _validate_source_files(
            document["source_files"],
            "source_files",
            ctx,
            allow_empty=mode == "idea_only",
        )
    if "imported_paths" in document:
        _validate_imported_paths(
            document["imported_paths"],
            "imported_paths",
            ctx,
            source_manifest=source_manifest,
        )
    imported = document.get("external_code_imported")
    imported_paths = document.get("imported_paths")
    if not isinstance(imported, bool):
        ctx.fail("INVALID_EXTERNAL_CODE_FLAG", "external_code_imported", "must be a boolean")
    elif mode in COPIED_CODE_MODES:
        if imported is not True:
            ctx.fail(
                "COPIED_CODE_FLAG_REQUIRED",
                "external_code_imported",
                "copied-code provenance must declare imported code",
            )
        if not isinstance(imported_paths, list) or not imported_paths:
            ctx.fail(
                "IMPORTED_PATHS_REQUIRED",
                "imported_paths",
                "copied-code provenance requires imported paths",
            )
    elif mode in NON_COPYING_MODES:
        if imported is not False:
            ctx.fail(
                "NON_COPYING_MODE_CONFLICT",
                "external_code_imported",
                "non-copying mode cannot declare imported code",
            )
        if isinstance(imported_paths, list) and imported_paths:
            ctx.fail(
                "NON_COPYING_MODE_CONFLICT",
                "imported_paths",
                "non-copying mode cannot declare imported paths",
            )
    if not isinstance(document.get("transformation_notes"), str):
        ctx.fail(
            "INVALID_TRANSFORMATION_NOTES",
            "transformation_notes",
            "transformation_notes must be a string",
        )
    notices = document.get("retained_notices")
    if not isinstance(notices, list) or any(not _nonempty_string(item) for item in notices):
        ctx.fail("INVALID_RETAINED_NOTICES", "retained_notices", "notices must be strings")
    elif len(notices) != len(set(notices)):
        ctx.fail("DUPLICATE_RETAINED_NOTICE", "retained_notices", "notices must be unique")
    return ctx.result()


def validate_candidate_file(
    candidate_path: str | Path,
    *,
    evidence_root: str | Path | None = None,
    source_root: str | Path | None = None,
    destination_root: str | Path | None = None,
) -> ValidationResult:
    """Load and validate an explicitly selected JSON candidate file."""

    path = Path(candidate_path)
    try:
        if path.stat().st_size > 1024 * 1024:
            issue = ValidationIssue("ERROR", "DOCUMENT_TOO_LARGE", "$", "candidate exceeds 1 MiB")
            return ValidationResult(Verdict.FAIL, (issue,))
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        issue = ValidationIssue("ERROR", "INVALID_CANDIDATE_JSON", "$", str(exc))
        return ValidationResult(Verdict.FAIL, (issue,))
    return validate_candidate(
        document,
        evidence_root=evidence_root,
        source_root=source_root,
        destination_root=destination_root,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline upstream adoption gate")
    parser.add_argument("candidate", type=Path, help="candidate JSON document")
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=Path.cwd(),
        help="repository root for sanitized evidence references (default: cwd)",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        help="exact local root containing the reviewed source manifest bytes",
    )
    parser.add_argument(
        "--destination-root",
        type=Path,
        help="exact local root containing imported destination bytes",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI returning 0=PASS, 1=FAIL, 2=BLOCKED, 3=unexpected validator error."""

    try:
        args = _build_parser().parse_args(argv)
        result = validate_candidate_file(
            args.candidate,
            evidence_root=args.evidence_root,
            source_root=args.source_root,
            destination_root=args.destination_root,
        )
        sys.stdout.write(safe_json_text(result.to_dict(), indent=2, append_newline=True))
        return {Verdict.PASS: 0, Verdict.FAIL: 1, Verdict.BLOCKED: 2}[result.verdict]
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        sys.stdout.write(
            safe_json_text(
                {
                    "verdict": "VALIDATOR_ERROR",
                    "issues": [
                        {
                            "severity": "ERROR",
                            "code": "VALIDATOR_ERROR",
                            "path": "$",
                            "message": str(exc),
                        }
                    ],
                },
                indent=2,
                append_newline=True,
            )
        )
        return 3
