"""Append-only sanitized JSONL evidence and structural validation."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .models import CampaignIdentity, CampaignSummary, CaseResult, Scenario, Verdict
from .output import OutputLimits, safe_json_bytes, sanitize_output, write_json_exclusive
from .overlay import verified_git_repository_root
from .redaction import credential_like_paths
from .safe_paths import (
    MAX_CONFIGURABLE_FILE_BYTES,
    SafePathError,
    bounded_file_bytes,
    bounded_file_digest,
    canonical_directory,
    safe_output_path,
    validate_campaign_identifier,
    validate_case_id,
    validate_relative_path,
)
from .trusted_anchors import trusted_manifest_sha256

EVIDENCE_SCHEMA = "jarvis.qa.evidence.v1"
MANIFEST_SCHEMA = "jarvis.qa.campaign-manifest.v2"
AUDIT_CONTENT_MANIFEST_SCHEMA = "jarvis.qa.audit-content-manifest.v1"
AUDIT_CONTENT_COMPARISON_SCHEMA = "jarvis.qa.audit-content-comparison.v1"
AUDIT_FILE_HASH_CONVENTION = "sha256_raw_file_bytes_v1"
AUDIT_LINK_HASH_CONVENTION = "sha256_utf8_readlink_metadata_v1"
AUDIT_MANIFEST_LIMITS = OutputLimits(
    max_depth=8,
    max_items=20_000,
    max_string_length=4096,
)
MAX_AUDIT_MANIFEST_ENTRIES = 10_000
MAX_AUDIT_MANIFEST_BYTES = 16 * 1024 * 1024
_ZERO_DIGEST = "0" * 64
_SHA256_LENGTH = 64
_EVIDENCE_VERIFICATION_TOKEN = object()
EVIDENCE_REQUIRED_FIELDS = frozenset(
    {
        "schema",
        "campaign_id",
        "namespace",
        "case_id",
        "verdict",
        "required",
        "semantic_review_required",
        "sanitized_request",
        "expected_contract",
        "validators",
        "observation",
        "assertions",
        "deterministic_failures",
        "bounded_evidence",
        "replay",
    }
)
EVIDENCE_OPTIONAL_FIELDS = frozenset(
    {"title", "error", "observed_at_utc", "redaction_event_count"}
)
EVIDENCE_ALLOWED_FIELDS = EVIDENCE_REQUIRED_FIELDS | EVIDENCE_OPTIONAL_FIELDS
DETERMINISTIC_REPLAY_VERDICTS = frozenset(
    {Verdict.PASS, Verdict.FAIL, Verdict.INCONCLUSIVE}
)
CLASSIFICATION_REPLAY_VERDICTS = frozenset(
    {
        Verdict.BLOCKED_BY_ENV,
        Verdict.BLOCKED_BY_SPEC,
        Verdict.SKIP,
        Verdict.ERROR,
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceIntegrity:
    evidence_path: Path
    manifest_path: Path
    evidence_sha256: str
    manifest_sha256: str
    evidence_size: int
    record_sha256s: tuple[str, ...]
    record_canonical_sha256s: tuple[str, ...]
    terminal_chain_sha256: str
    _verification_token: object | None = dataclass_field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def provenance_verified(self) -> bool:
        return self._verification_token is _EVIDENCE_VERIFICATION_TOKEN


@dataclass(frozen=True, slots=True)
class EvidenceAnchor:
    evidence_sha256: str
    manifest_sha256: str


def evidence_manifest_path(evidence_path: Path) -> Path:
    return evidence_path.with_suffix(".manifest.json")


def _strict_json_loads(payload: str, *, label: str) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("duplicate JSON object key")
            document[key] = value
        return document

    try:
        return json.loads(
            payload,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value}")
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise ValueError(f"{label}: invalid JSON: {detail}") from exc


def _raw_lines(payload: bytes, *, label: str) -> list[bytes]:
    if not payload:
        raise ValueError(f"{label}: evidence is empty")
    if not payload.endswith(b"\n"):
        raise ValueError(f"{label}: evidence must end with a newline")
    lines = payload.splitlines(keepends=True)
    if not lines or any(not line.strip() for line in lines):
        raise ValueError(f"{label}: blank JSONL record")
    return lines


def _parse_evidence_bytes(
    payload: bytes, *, label: str
) -> tuple[list[dict[str, Any]], list[bytes]]:
    lines = _raw_lines(payload, label=label)
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{label}:{line_number}: invalid UTF-8") from exc
        record = _strict_json_loads(text, label=f"{label}:{line_number}")
        if not isinstance(record, dict):
            raise ValueError(f"{label}:{line_number}: record must be an object")
        records.append(record)
    return records, lines


def _record_sha256s(lines: Sequence[bytes]) -> tuple[str, ...]:
    return tuple(hashlib.sha256(line).hexdigest() for line in lines)


def canonical_record_sha256(record: Mapping[str, Any]) -> str:
    payload = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _terminal_chain(record_digests: Sequence[str]) -> str:
    chain = _ZERO_DIGEST
    for digest in record_digests:
        chain = hashlib.sha256(bytes.fromhex(chain) + bytes.fromhex(digest)).hexdigest()
    return chain


def _record_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counter = Counter(record.get("verdict") for record in records)
    return {verdict.value: counter.get(verdict.value, 0) for verdict in Verdict}


def _record_exit_code(records: Sequence[Mapping[str, Any]]) -> int:
    if not records or any(record.get("verdict") == Verdict.ERROR.value for record in records):
        return 3
    if any(record.get("verdict") == Verdict.FAIL.value for record in records):
        return 1
    incomplete = {
        Verdict.INCONCLUSIVE.value,
        Verdict.BLOCKED_BY_ENV.value,
        Verdict.BLOCKED_BY_SPEC.value,
        Verdict.SKIP.value,
    }
    if any(
        record.get("required") is True and record.get("verdict") in incomplete
        for record in records
    ):
        return 2
    return 0


def _manifest_document(
    records: Sequence[Mapping[str, Any]],
    evidence_name: str,
    evidence_bytes: bytes,
    lines: Sequence[bytes],
) -> dict[str, Any]:
    record_digests = _record_sha256s(lines)
    return {
        "schema": MANIFEST_SCHEMA,
        "campaign_id": records[0]["campaign_id"],
        "namespace": records[0]["namespace"],
        "evidence_file": evidence_name,
        "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "evidence_size": len(evidence_bytes),
        "record_count": len(records),
        "case_ids": [record["case_id"] for record in records],
        "record_sha256s": list(record_digests),
        "record_canonical_sha256s": [
            canonical_record_sha256(record) for record in records
        ],
        "terminal_chain_sha256": _terminal_chain(record_digests),
        "counts": _record_counts(records),
        "exit_code": _record_exit_code(records),
    }


def _replay_contract(result: CaseResult) -> dict[str, Any]:
    if result.verdict in DETERMINISTIC_REPLAY_VERDICTS:
        return {"mode": "deterministic"}
    if result.verdict in CLASSIFICATION_REPLAY_VERDICTS:
        if not result.error:
            raise ValueError(f"{result.verdict.value} requires a replay reason")
        return {
            "mode": "classification",
            "reason": result.error,
            "assertion_names": [assertion.name for assertion in result.assertions],
        }
    raise ValueError(f"unsupported replay verdict {result.verdict.value}")


def validate_replay_contract(record: Mapping[str, Any], verdict: Verdict) -> list[str]:
    """Validate the typed replay mode without trusting a free-form marker."""

    errors: list[str] = []
    replay = record.get("replay")
    if not isinstance(replay, Mapping):
        return ["replay must be an object"]
    mode = replay.get("mode")
    if mode == "deterministic":
        if set(replay) != {"mode"}:
            errors.append("deterministic replay has unexpected fields")
        if verdict not in DETERMINISTIC_REPLAY_VERDICTS:
            errors.append(f"{verdict.value} cannot use deterministic replay mode")
        return errors
    if mode != "classification":
        return ["replay mode must be deterministic or classification"]
    if set(replay) != {"mode", "reason", "assertion_names"}:
        errors.append("classification replay fields are incomplete or unexpected")
    if verdict not in CLASSIFICATION_REPLAY_VERDICTS:
        errors.append(f"{verdict.value} cannot use classification replay mode")

    reason = replay.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("classification replay reason must be non-empty")
    if record.get("error") != reason:
        errors.append("classification replay reason must match error")

    assertions = record.get("assertions")
    assertion_names = replay.get("assertion_names")
    if not isinstance(assertions, list) or not isinstance(assertion_names, list) or any(
        not isinstance(name, str) for name in assertion_names
    ):
        errors.append("classification replay assertion_names must be a string array")
        return errors
    actual_names = [
        str(assertion.get("name"))
        for assertion in assertions
        if isinstance(assertion, Mapping)
    ]
    if assertion_names != actual_names:
        errors.append("classification replay assertion_names mismatch")

    expected_names = {
        Verdict.BLOCKED_BY_ENV: {"runner.environment_available"},
        Verdict.BLOCKED_BY_SPEC: {"runner.specification_complete"},
        Verdict.SKIP: {"runner.optional_skip"},
        Verdict.ERROR: {"runner.assertions_present", "runner.completed_without_error"},
    }
    expected_passed = verdict is Verdict.SKIP
    matching = [
        assertion
        for assertion in assertions
        if isinstance(assertion, Mapping)
        and assertion.get("name") in expected_names.get(verdict, set())
        and assertion.get("passed") is expected_passed
    ]
    if len(matching) != 1 or len(assertions) != 1:
        errors.append(f"{verdict.value} lacks its exact runner classification assertion")
    if verdict is Verdict.SKIP and record.get("required") is not False:
        errors.append("SKIP requires required=false")
    return errors


class EvidenceStore:
    """A campaign-owned JSONL file finalized once with a raw-byte manifest."""

    def __init__(
        self,
        output_root: Path,
        identity: CampaignIdentity,
        *,
        canaries: Iterable[str] = (),
    ) -> None:
        self.output_root = canonical_directory(output_root, create=True)
        self.identity = identity
        self.canaries = tuple(canaries)
        self.path = safe_output_path(
            self.output_root, f"{identity.campaign_id}.jsonl"
        )
        self.manifest_path = safe_output_path(
            self.output_root, f"{identity.campaign_id}.manifest.json"
        )
        self._handle = self.path.open("x+b")
        self._finalized = False
        self._case_ids: set[str] = set()
        self._written_size = 0
        self._written_sha256 = hashlib.sha256()
        self._written_record_sha256s: list[str] = []
        self.anchor: EvidenceAnchor | None = None

    def append(self, scenario: Scenario, result: CaseResult) -> dict[str, Any]:
        if self._finalized or self._handle.closed:
            raise RuntimeError("evidence store is finalized")
        if scenario.scenario_id != result.case_id:
            raise ValueError("scenario and result case identifiers differ")
        if result.case_id in self._case_ids:
            raise ValueError("duplicate evidence case identifier")
        record: dict[str, Any] = {
            "schema": EVIDENCE_SCHEMA,
            "campaign_id": self.identity.campaign_id,
            "namespace": self.identity.namespace,
            "case_id": result.case_id,
            "title": scenario.title,
            "verdict": result.verdict.value,
            "required": result.required,
            "semantic_review_required": result.semantic_review_required,
            "sanitized_request": dict(scenario.request),
            "expected_contract": dict(scenario.expected_contract),
            "validators": [dict(item) for item in scenario.validators],
            "observation": dict(result.observation),
            "assertions": [assertion.to_dict() for assertion in result.assertions],
            "deterministic_failures": list(result.deterministic_failures),
            "bounded_evidence": dict(result.bounded_evidence),
            "replay": _replay_contract(result),
            "error": result.error,
            "observed_at_utc": result.observed_at_utc,
        }
        sanitized = sanitize_output(record, canaries=self.canaries)
        persisted = dict(sanitized.value)
        persisted["redaction_event_count"] = len(sanitized.events)
        record_errors = validate_evidence_records([persisted])
        if record_errors:
            raise ValueError(f"refusing invalid evidence record: {'; '.join(record_errors)}")
        line = safe_json_bytes(
            persisted,
            canaries=self.canaries,
            separators=(",", ":"),
            append_newline=True,
        )
        self._handle.seek(0, os.SEEK_END)
        self._handle.write(line)
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._written_size += len(line)
        self._written_sha256.update(line)
        self._written_record_sha256s.append(hashlib.sha256(line).hexdigest())
        self._case_ids.add(result.case_id)
        return persisted

    def write_manifest(self, summary: CampaignSummary) -> EvidenceAnchor:
        if self._finalized or self._handle.closed:
            raise RuntimeError("evidence store is finalized")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.seek(0)
        evidence_bytes = self._handle.read()
        records, lines = _parse_evidence_bytes(evidence_bytes, label=str(self.path))
        if (
            len(evidence_bytes) != self._written_size
            or hashlib.sha256(evidence_bytes).digest()
            != self._written_sha256.digest()
            or _record_sha256s(lines) != tuple(self._written_record_sha256s)
        ):
            raise ValueError("persisted evidence changed after append")
        validation_errors = validate_evidence_records(records)
        if validation_errors:
            raise ValueError(f"cannot finalize invalid evidence: {'; '.join(validation_errors)}")
        if summary.identity != self.identity:
            raise ValueError("campaign summary identity mismatch")
        if summary.counts != _record_counts(records) or summary.exit_code != _record_exit_code(
            records
        ):
            raise ValueError("campaign summary does not match persisted evidence")
        if [result.case_id for result in summary.results] != [
            record["case_id"] for record in records
        ]:
            raise ValueError("campaign summary order does not match persisted evidence")
        document = _manifest_document(records, self.path.name, evidence_bytes, lines)
        manifest_bytes = write_json_exclusive(
            self.manifest_path,
            document,
            canaries=self.canaries,
        )
        self._finalized = True
        self._handle.close()
        self.anchor = EvidenceAnchor(
            evidence_sha256=document["evidence_sha256"],
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )
        return self.anchor

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()


def load_evidence(path: Path) -> list[dict[str, Any]]:
    root = canonical_directory(path.parent)
    payload = bounded_file_bytes(
        root,
        path.name,
        max_bytes=MAX_CONFIGURABLE_FILE_BYTES,
    )
    records, _ = _parse_evidence_bytes(payload, label=str(path))
    return records


def _manifest_errors(
    manifest: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> list[str]:
    fields = set(expected)
    errors: list[str] = []
    missing = sorted(fields - set(manifest))
    unexpected = sorted(set(manifest) - fields)
    if missing:
        errors.append(f"manifest missing fields: {', '.join(missing)}")
    if unexpected:
        errors.append(f"manifest unexpected fields: {', '.join(unexpected)}")
    if missing or unexpected:
        return errors
    for field in (
        "schema",
        "campaign_id",
        "namespace",
        "evidence_file",
        "evidence_sha256",
        "terminal_chain_sha256",
    ):
        if not isinstance(manifest.get(field), str):
            errors.append(f"manifest {field} must be a string")
    for field in ("evidence_size", "record_count", "exit_code"):
        value = manifest.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"manifest {field} must be a non-negative integer")
    for field in ("case_ids", "record_sha256s", "record_canonical_sha256s"):
        value = manifest.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(f"manifest {field} must be a string array")
    counts = manifest.get("counts")
    expected_count_fields = {verdict.value for verdict in Verdict}
    if not isinstance(counts, Mapping) or set(counts) != expected_count_fields or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in counts.values()
    ):
        errors.append("manifest counts must contain exact non-negative verdict counts")
    for field, expected_value in expected.items():
        if manifest.get(field) != expected_value:
            errors.append(f"manifest {field} mismatch")
    return errors


def verify_evidence_bundle(
    path: Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> tuple[list[dict[str, Any]], list[str], EvidenceIntegrity | None]:
    root = canonical_directory(path.parent)
    evidence_bytes = bounded_file_bytes(
        root,
        path.name,
        max_bytes=MAX_CONFIGURABLE_FILE_BYTES,
    )
    records, lines = _parse_evidence_bytes(evidence_bytes, label=str(path))
    errors = validate_evidence_records(records)
    if errors:
        return records, errors, None
    expected_manifest = _manifest_document(records, path.name, evidence_bytes, lines)
    manifest_path = evidence_manifest_path(path)
    try:
        manifest_bytes = bounded_file_bytes(
            root,
            manifest_path.name,
            max_bytes=MAX_CONFIGURABLE_FILE_BYTES,
        )
    except (SafePathError, ValueError) as exc:
        errors.append(f"manifest unavailable: {getattr(exc, 'code', type(exc).__name__)}")
        return records, errors, None
    try:
        manifest_text = manifest_bytes.decode("utf-8")
    except UnicodeDecodeError:
        errors.append("manifest is not valid UTF-8")
        return records, errors, None
    try:
        manifest = _strict_json_loads(manifest_text, label=str(manifest_path))
    except ValueError as exc:
        errors.append(str(exc))
        return records, errors, None
    if not isinstance(manifest, Mapping):
        errors.append("manifest must be an object")
        return records, errors, None
    errors.extend(_manifest_errors(manifest, expected_manifest))
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    trusted_digest = expected_manifest_sha256
    if trusted_digest is None:
        trusted_digest = trusted_manifest_sha256(path)
    if trusted_digest is None:
        errors.append("trusted manifest SHA-256 anchor is required")
    elif (
        not isinstance(trusted_digest, str)
        or len(trusted_digest) != 64
        or any(character not in "0123456789abcdef" for character in trusted_digest)
    ):
        errors.append("trusted manifest SHA-256 anchor is invalid")
    elif manifest_digest != trusted_digest:
        errors.append("trusted manifest SHA-256 anchor mismatch")
    if errors:
        return records, errors, None
    integrity = EvidenceIntegrity(
        evidence_path=path.resolve(),
        manifest_path=manifest_path.resolve(),
        evidence_sha256=expected_manifest["evidence_sha256"],
        manifest_sha256=manifest_digest,
        evidence_size=expected_manifest["evidence_size"],
        record_sha256s=tuple(expected_manifest["record_sha256s"]),
        record_canonical_sha256s=tuple(
            expected_manifest["record_canonical_sha256s"]
        ),
        terminal_chain_sha256=expected_manifest["terminal_chain_sha256"],
    )
    object.__setattr__(
        integrity,
        "_verification_token",
        _EVIDENCE_VERIFICATION_TOKEN,
    )
    return records, [], integrity


def validate_evidence_records(records: list[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for index, record in enumerate(records, start=1):
        missing = sorted(EVIDENCE_REQUIRED_FIELDS - record.keys())
        if missing:
            errors.append(f"record {index}: missing {', '.join(missing)}")
            continue
        unexpected = sorted(set(record) - EVIDENCE_ALLOWED_FIELDS)
        if unexpected:
            errors.append(f"record {index}: unexpected fields {', '.join(unexpected)}")
        if record.get("schema") != EVIDENCE_SCHEMA:
            errors.append(f"record {index}: unsupported schema")
        if "title" in record and (
            not isinstance(record["title"], str) or not record["title"]
        ):
            errors.append(f"record {index}: title must be a non-empty string")
        if "error" in record and record["error"] is not None and not isinstance(
            record["error"], str
        ):
            errors.append(f"record {index}: error must be a string or null")
        if "observed_at_utc" in record:
            observed_at = record["observed_at_utc"]
            try:
                candidate = (
                    observed_at[:-1] + "+00:00"
                    if isinstance(observed_at, str) and observed_at.endswith("Z")
                    else observed_at
                )
                parsed_at = datetime.fromisoformat(candidate)
                if parsed_at.tzinfo is None:
                    raise ValueError("timezone is required")
            except (TypeError, ValueError):
                errors.append(f"record {index}: observed_at_utc must be a date-time")
        redaction_count = record.get("redaction_event_count")
        if "redaction_event_count" in record and (
            not isinstance(redaction_count, int)
            or isinstance(redaction_count, bool)
            or redaction_count < 0
        ):
            errors.append(
                f"record {index}: redaction_event_count must be a non-negative integer"
            )
        try:
            campaign_id = validate_campaign_identifier(
                record.get("campaign_id"), label="campaign_id"
            )
            namespace = validate_campaign_identifier(record.get("namespace"), label="namespace")
            case_id = validate_case_id(record.get("case_id"))
            if campaign_id == namespace:
                errors.append(f"record {index}: campaign_id and namespace must differ")
        except ValueError as exc:
            errors.append(f"record {index}: {exc}")
            campaign_id = ""
            case_id = ""
        try:
            if not isinstance(record["verdict"], str):
                raise ValueError("verdict must be a string")
            verdict = Verdict(record["verdict"])
        except ValueError:
            errors.append(f"record {index}: invalid verdict")
            continue
        if not isinstance(record.get("required"), bool):
            errors.append(f"record {index}: required must be boolean")
        if not isinstance(record.get("semantic_review_required"), bool):
            errors.append(f"record {index}: semantic_review_required must be boolean")
        validators = record.get("validators")
        if not isinstance(validators, list) or not validators:
            errors.append(f"record {index}: validators must be a non-empty array")
        else:
            for validator_index, validator in enumerate(validators, start=1):
                try:
                    Scenario.from_dict(
                        {
                            "scenario_id": "EVIDENCE-VALIDATOR-001",
                            "title": "persisted validator contract",
                            "transport": "offline",
                            "request": {},
                            "expected_contract": {},
                            "validators": [validator],
                        }
                    )
                except (TypeError, ValueError) as exc:
                    errors.append(
                        f"record {index}: validator {validator_index} is invalid: {exc}"
                    )
        if not isinstance(record.get("observation"), Mapping):
            errors.append(f"record {index}: observation must be an object")
        if not isinstance(record.get("bounded_evidence"), Mapping):
            errors.append(f"record {index}: bounded_evidence must be an object")
        if not isinstance(record.get("sanitized_request"), Mapping):
            errors.append(f"record {index}: sanitized_request must be an object")
        if not isinstance(record.get("expected_contract"), Mapping):
            errors.append(f"record {index}: expected_contract must be an object")
        assertions = record.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            errors.append(f"record {index}: assertions must be a non-empty array")
            continue
        assertion_fields = {"name", "passed", "expected", "actual", "detail"}
        if any(
            not isinstance(item, Mapping)
            or set(item) != assertion_fields
            or not isinstance(item.get("name"), str)
            or not item.get("name")
            or not isinstance(item.get("passed"), bool)
            or not isinstance(item.get("detail"), str)
            for item in assertions
        ):
            errors.append(f"record {index}: invalid assertion shape")
            continue
        if verdict is Verdict.PASS:
            if not assertions:
                errors.append(f"record {index}: empty PASS")
            elif any(not bool(item.get("passed")) for item in assertions if isinstance(item, dict)):
                errors.append(f"record {index}: PASS contains failed assertion")
        failures = [
            str(item.get("name"))
            for item in assertions
            if isinstance(item, dict) and not bool(item.get("passed"))
        ]
        declared_failures = record["deterministic_failures"]
        if not isinstance(declared_failures, list) or any(
            not isinstance(item, str) or not item for item in declared_failures
        ):
            errors.append(f"record {index}: deterministic_failures must be a string array")
            continue
        if len(declared_failures) != len(set(declared_failures)):
            errors.append(f"record {index}: deterministic_failures must be unique")
        if sorted(failures) != sorted(declared_failures):
            errors.append(f"record {index}: deterministic_failures mismatch")
        if verdict is Verdict.FAIL and not failures:
            errors.append(f"record {index}: FAIL has no failed assertion")
        if verdict in {Verdict.PASS, Verdict.INCONCLUSIVE} and failures:
            errors.append(f"record {index}: {verdict.value} has deterministic failures")
        errors.extend(
            f"record {index}: {error}"
            for error in validate_replay_contract(record, verdict)
        )
        key = (campaign_id, case_id)
        if key in seen:
            errors.append(f"record {index}: duplicate campaign/case key")
        seen.add(key)
        leaked = credential_like_paths(record)
        if leaked:
            errors.append(f"record {index}: unredacted credential-like data at {', '.join(leaked)}")
    return errors


def validate_evidence_file(
    path: Path,
    *,
    expected_manifest_sha256: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    records, errors, _ = verify_evidence_bundle(
        path,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    return records, errors


def result_record(result: CaseResult) -> dict[str, Any]:
    """Small helper for tests and adapters that do not own an EvidenceStore."""

    document = asdict(result)
    document["verdict"] = result.verdict.value
    return document


@dataclass(frozen=True, slots=True)
class AuditManifestArtifact:
    path: Path
    sha256: str
    entry_count: int


@dataclass(frozen=True, slots=True)
class AuditManifestComparison:
    before_sha256: str
    after_sha256: str
    differences: tuple[dict[str, Any], ...]
    result: AuditManifestArtifact

    @property
    def ok(self) -> bool:
        return not self.differences


def _audit_reparse(stat_result: os.stat_result) -> bool:
    return stat.S_ISLNK(stat_result.st_mode) or bool(
        int(getattr(stat_result, "st_file_attributes", 0)) & 0x0400
    )


def _audit_entry_digest(
    audit_root: Path,
    relative_path: str,
    entry_path: Path,
    entry_stat: os.stat_result,
) -> tuple[str, int, str | None]:
    if _audit_reparse(entry_stat):
        try:
            metadata = os.readlink(entry_path)
        except OSError as exc:
            raise SafePathError(
                "LINK_METADATA_UNAVAILABLE", "link metadata is inaccessible"
            ) from exc
        payload = str(metadata).encode("utf-8", errors="surrogatepass")
        file_type = "symlink" if stat.S_ISLNK(entry_stat.st_mode) else "reparse"
        return file_type, int(entry_stat.st_size), hashlib.sha256(payload).hexdigest()
    if stat.S_ISDIR(entry_stat.st_mode):
        return "directory", int(entry_stat.st_size), None
    if stat.S_ISREG(entry_stat.st_mode):
        first_digest = bounded_file_digest(
            audit_root,
            relative_path,
            max_bytes=MAX_CONFIGURABLE_FILE_BYTES,
        )
        second_digest = bounded_file_digest(
            audit_root,
            relative_path,
            max_bytes=MAX_CONFIGURABLE_FILE_BYTES,
        )
        if first_digest != second_digest:
            raise SafePathError("ENTRY_RACE", "audit entry changed during scan")
        return "regular_file", first_digest.size, first_digest.sha256
    raise SafePathError("SPECIAL_FILE", "special audit entries are unsupported")


def _audit_stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(getattr(value, "st_file_attributes", 0)),
    )


def _audit_scan(repository_root: str | os.PathLike[str]) -> list[dict[str, Any]]:
    repository = canonical_directory(repository_root)
    audit_root = canonical_directory(repository / ".audit")
    if audit_root.parent != repository:
        raise SafePathError("AUDIT_ROOT_ESCAPE", "audit root must be a direct repository child")
    entries: list[dict[str, Any]] = []

    def scan(directory: Path, relative_parent: str) -> None:
        try:
            before = os.lstat(directory)
        except OSError as exc:
            raise SafePathError(
                "DIRECTORY_INACCESSIBLE", "audit directory is inaccessible"
            ) from exc
        if _audit_reparse(before) or not stat.S_ISDIR(before.st_mode):
            raise SafePathError("UNSAFE_DIRECTORY", "audit directory is unsafe")
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: (entry.name.casefold(), entry.name))
        except OSError as exc:
            raise SafePathError(
                "DIRECTORY_INACCESSIBLE", "audit directory is inaccessible"
            ) from exc
        for child in children:
            relative = f"{relative_parent}/{child.name}" if relative_parent else child.name
            relative = validate_relative_path(relative, label="audit manifest path")
            try:
                child_stat = os.lstat(child.path)
            except OSError as exc:
                raise SafePathError("ENTRY_INACCESSIBLE", "audit entry is inaccessible") from exc
            file_type, size, digest = _audit_entry_digest(
                audit_root,
                relative,
                Path(child.path),
                child_stat,
            )
            entries.append(
                {
                    "relative_path": relative,
                    "file_type": file_type,
                    "size": size,
                    "sha256": digest,
                }
            )
            if len(entries) > MAX_AUDIT_MANIFEST_ENTRIES:
                raise SafePathError("ENTRY_LIMIT", "audit manifest entry limit exceeded")
            if file_type == "directory":
                scan(Path(child.path), relative)
            try:
                child_after = os.lstat(child.path)
            except OSError as exc:
                raise SafePathError("ENTRY_RACE", "audit entry changed during scan") from exc
            if (
                _audit_stat_identity(child_stat) != _audit_stat_identity(child_after)
                or _audit_reparse(child_stat) != _audit_reparse(child_after)
            ):
                raise SafePathError("ENTRY_RACE", "audit entry changed during scan")
            if file_type in {"symlink", "reparse"}:
                after_type, after_size, after_digest = _audit_entry_digest(
                    audit_root,
                    relative,
                    Path(child.path),
                    child_after,
                )
                if (file_type, size, digest) != (after_type, after_size, after_digest):
                    raise SafePathError("ENTRY_RACE", "audit link changed during scan")
        try:
            after = os.lstat(directory)
        except OSError as exc:
            raise SafePathError("DIRECTORY_RACE", "audit directory changed during scan") from exc
        if _audit_stat_identity(before) != _audit_stat_identity(after) or _audit_reparse(
            after
        ):
            raise SafePathError("DIRECTORY_RACE", "audit directory changed during scan")

    scan(audit_root, "")
    entries.sort(key=lambda entry: (entry["relative_path"].casefold(), entry["relative_path"]))
    return entries


def _canonical_entries_bytes(entries: list[dict[str, Any]]) -> bytes:
    return json.dumps(
        entries,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _repository_identity(repository: Path) -> str:
    return hashlib.sha256(
        os.path.normcase(str(repository)).replace("\\", "/").encode("utf-8")
    ).hexdigest()


def _build_audit_content_manifest(repository: Path) -> dict[str, Any]:
    entries = _audit_scan(repository)
    document: dict[str, Any] = {
        "schema": AUDIT_CONTENT_MANIFEST_SCHEMA,
        "scope": ".audit",
        "repository_root_sha256": _repository_identity(repository),
        "file_hash_convention": AUDIT_FILE_HASH_CONVENTION,
        "link_hash_convention": AUDIT_LINK_HASH_CONVENTION,
        "entry_count": len(entries),
        "entries_sha256": hashlib.sha256(_canonical_entries_bytes(entries)).hexdigest(),
        "entries": entries,
    }
    sanitized = sanitize_output(document, limits=AUDIT_MANIFEST_LIMITS)
    if sanitized.events or sanitized.value != document:
        raise ValueError("audit manifest metadata requires redaction")
    return document


def build_audit_content_manifest(
    repository_root: str | os.PathLike[str],
    git_executable: str | os.PathLike[str],
) -> dict[str, Any]:
    """Hash the complete .audit tree without copying or returning file content."""

    _, repository = verified_git_repository_root(repository_root, git_executable)
    return _build_audit_content_manifest(repository)


def _direct_manifest_name(value: object, *, label: str) -> str:
    relative = validate_relative_path(value, label=label)
    if len(PurePosixPath(relative).parts) != 1:
        raise SafePathError("UNSAFE_MANIFEST_NAME", f"{label} must be a direct child")
    return relative


def _external_manifest_root(repository: Path, output_root: str | os.PathLike[str]) -> Path:
    external = canonical_directory(output_root)
    if (
        external == repository
        or external.is_relative_to(repository)
        or repository.is_relative_to(external)
    ):
        raise SafePathError(
            "OUTPUT_NOT_EXTERNAL", "manifest output root must be disjoint from repository"
        )
    return external


def _external_output_path(
    repository: Path,
    output_root: str | os.PathLike[str],
    output_name: object,
) -> Path:
    external = _external_manifest_root(repository, output_root)
    name = _direct_manifest_name(output_name, label="audit manifest output name")
    return safe_output_path(external, name)


def write_audit_content_manifest(
    repository_root: str | os.PathLike[str],
    output_root: str | os.PathLike[str],
    output_name: object,
    git_executable: str | os.PathLike[str],
    *,
    canaries: Iterable[str] = (),
) -> AuditManifestArtifact:
    _, repository = verified_git_repository_root(repository_root, git_executable)
    target = _external_output_path(repository, output_root, output_name)
    document = _build_audit_content_manifest(repository)
    sanitized = sanitize_output(
        document,
        canaries=canaries,
        limits=AUDIT_MANIFEST_LIMITS,
    )
    if sanitized.events or sanitized.value != document:
        raise ValueError("audit manifest metadata requires redaction")
    payload = write_json_exclusive(
        target,
        document,
        canaries=canaries,
        limits=AUDIT_MANIFEST_LIMITS,
    )
    return AuditManifestArtifact(
        path=target,
        sha256=hashlib.sha256(payload).hexdigest(),
        entry_count=len(document["entries"]),
    )


def _validate_audit_manifest(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("audit manifest must be an object")
    expected_fields = {
        "schema",
        "scope",
        "repository_root_sha256",
        "file_hash_convention",
        "link_hash_convention",
        "entry_count",
        "entries_sha256",
        "entries",
    }
    if set(document) != expected_fields:
        raise ValueError("audit manifest fields are incomplete or unexpected")
    if document["schema"] != AUDIT_CONTENT_MANIFEST_SCHEMA or document["scope"] != ".audit":
        raise ValueError("audit manifest identity is invalid")
    repository_identity = document["repository_root_sha256"]
    if (
        not isinstance(repository_identity, str)
        or len(repository_identity) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in repository_identity)
    ):
        raise ValueError("audit manifest repository identity is invalid")
    if (
        document["file_hash_convention"] != AUDIT_FILE_HASH_CONVENTION
        or document["link_hash_convention"] != AUDIT_LINK_HASH_CONVENTION
    ):
        raise ValueError("audit manifest hash convention is invalid")
    entries = document["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_AUDIT_MANIFEST_ENTRIES:
        raise ValueError("audit manifest entries are invalid")
    if (
        not isinstance(document["entry_count"], int)
        or isinstance(document["entry_count"], bool)
        or document["entry_count"] != len(entries)
    ):
        raise ValueError("audit manifest entry count is invalid")
    paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "relative_path",
            "file_type",
            "size",
            "sha256",
        }:
            raise ValueError("audit manifest entry shape is invalid")
        relative = validate_relative_path(entry["relative_path"], label="audit manifest path")
        file_type = entry["file_type"]
        size = entry["size"]
        digest = entry["sha256"]
        if file_type not in {"regular_file", "directory", "symlink", "reparse"}:
            raise ValueError("audit manifest file type is invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("audit manifest size is invalid")
        if file_type == "directory":
            if digest is not None:
                raise ValueError("directory audit entry cannot carry a byte digest")
        elif not isinstance(digest, str) or len(digest) != _SHA256_LENGTH or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("audit manifest digest is invalid")
        paths.append(relative)
    expected_order = sorted(paths, key=lambda value: (value.casefold(), value))
    if paths != expected_order or len({path.casefold() for path in paths}) != len(paths):
        raise ValueError("audit manifest paths are not sorted and unique")
    entries_digest = hashlib.sha256(_canonical_entries_bytes(entries)).hexdigest()
    if document["entries_sha256"] != entries_digest:
        raise ValueError("audit manifest entries digest mismatch")
    sanitized = sanitize_output(document, limits=AUDIT_MANIFEST_LIMITS)
    if sanitized.events or sanitized.value != document:
        raise ValueError("audit manifest metadata requires redaction")
    return document


def _load_audit_manifest(
    manifest_root: str | os.PathLike[str],
    manifest_name: object,
    expected_sha256: str,
) -> tuple[dict[str, Any], str]:
    if len(expected_sha256) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        raise ValueError("expected audit manifest SHA-256 is invalid")
    root = canonical_directory(manifest_root)
    relative = _direct_manifest_name(manifest_name, label="audit manifest name")
    payload = bounded_file_bytes(root, relative, max_bytes=MAX_AUDIT_MANIFEST_BYTES)
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError("audit manifest trusted SHA-256 mismatch")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("audit manifest is not UTF-8") from exc
    document = _strict_json_loads(text, label="audit manifest")
    return _validate_audit_manifest(document), actual_sha256


def compare_audit_content_manifests(
    repository_root: str | os.PathLike[str],
    manifest_root: str | os.PathLike[str],
    before_name: object,
    after_name: object,
    git_executable: str | os.PathLike[str],
    *,
    expected_before_sha256: str,
    expected_after_sha256: str,
    result_name: object,
) -> AuditManifestComparison:
    _, repository = verified_git_repository_root(repository_root, git_executable)
    external = _external_manifest_root(repository, manifest_root)
    result_path = _external_output_path(repository, external, result_name)
    before_relative = _direct_manifest_name(before_name, label="before manifest name")
    after_relative = _direct_manifest_name(after_name, label="after manifest name")
    if before_relative.casefold() == after_relative.casefold():
        raise ValueError("before and after manifests must be distinct")
    before, before_sha256 = _load_audit_manifest(
        external,
        before_relative,
        expected_before_sha256,
    )
    after, after_sha256 = _load_audit_manifest(
        external,
        after_relative,
        expected_after_sha256,
    )
    repository_identity = _repository_identity(repository)
    if (
        before["repository_root_sha256"] != repository_identity
        or after["repository_root_sha256"] != repository_identity
    ):
        raise ValueError("audit manifests do not match the requested repository")
    live = _build_audit_content_manifest(repository)
    before_by_path = {entry["relative_path"]: entry for entry in before["entries"]}
    after_by_path = {entry["relative_path"]: entry for entry in after["entries"]}
    differences: list[dict[str, Any]] = []
    for relative in sorted(before_by_path.keys() - after_by_path.keys()):
        differences.append({"code": "PATH_REMOVED", "relative_path": relative})
    for relative in sorted(after_by_path.keys() - before_by_path.keys()):
        differences.append({"code": "PATH_ADDED", "relative_path": relative})
    for relative in sorted(before_by_path.keys() & after_by_path.keys()):
        before_entry = before_by_path[relative]
        after_entry = after_by_path[relative]
        for field, code in (
            ("file_type", "FILE_TYPE_CHANGED"),
            ("size", "FILE_SIZE_CHANGED"),
            ("sha256", "FILE_HASH_CHANGED"),
        ):
            if before_entry[field] != after_entry[field]:
                differences.append(
                    {"code": code, "relative_path": relative, "field": field}
                )
    if after != live:
        differences.append({"code": "AFTER_MANIFEST_STALE"})
    comparison_document: dict[str, Any] = {
        "schema": AUDIT_CONTENT_COMPARISON_SCHEMA,
        "scope": ".audit",
        "status": "PASS" if not differences else "FAIL",
        "before_manifest": before_relative,
        "before_sha256": before_sha256,
        "after_manifest": after_relative,
        "after_sha256": after_sha256,
        "difference_count": len(differences),
        "differences": differences,
    }
    sanitized = sanitize_output(comparison_document, limits=AUDIT_MANIFEST_LIMITS)
    if sanitized.events or sanitized.value != comparison_document:
        raise ValueError("audit manifest comparison metadata requires redaction")
    result_payload = write_json_exclusive(
        result_path,
        comparison_document,
        limits=AUDIT_MANIFEST_LIMITS,
    )
    result = AuditManifestArtifact(
        path=result_path,
        sha256=hashlib.sha256(result_payload).hexdigest(),
        entry_count=len(differences),
    )
    return AuditManifestComparison(
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        differences=tuple(differences),
        result=result,
    )
