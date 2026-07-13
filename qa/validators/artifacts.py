"""Bounded artifact path, hash, and source-integrity checks."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..models import AssertionResult
from ..safe_paths import (
    SafePathError,
    bounded_file_digest,
    validate_relative_path,
    validate_root_alias,
)
from .context import ValidationContext
from .format_contracts import field_value

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = frozenset(
    {
        "kind",
        "field",
        "root",
        "expected_path",
        "expected_sha256",
        "source_root",
        "source_path",
        "source_sha256_before",
    }
)


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _safe_digest(
    context: ValidationContext | None, root_alias: object, relative: object
) -> tuple[str | None, str | None]:
    if context is None:
        return None, "TRUSTED_CONTEXT_REQUIRED"
    try:
        root = context.artifact_root(root_alias)
        result = bounded_file_digest(
            root,
            relative,
            max_bytes=context.max_artifact_bytes,
        )
    except (SafePathError, ValueError) as exc:
        return None, getattr(exc, "code", "UNTRUSTED_ROOT")
    return result.sha256, None


def validate_artifact(
    observation: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    context: ValidationContext | None = None,
) -> list[AssertionResult]:
    unknown = sorted(set(spec) - _FIELDS)
    field = spec.get("field", "artifact")
    root_alias = spec.get("root")
    expected_path = spec.get("expected_path")
    expected_hash = spec.get("expected_sha256")
    source_root = spec.get("source_root")
    source_path = spec.get("source_path")
    source_before = spec.get("source_sha256_before")
    source_absent = source_root is None and source_path is None and source_before is None
    source_complete = source_absent or (
        isinstance(source_root, str)
        and isinstance(source_path, str)
        and _valid_sha256(source_before)
    )
    try:
        canonical_root_alias = validate_root_alias(root_alias)
        root_alias_valid = True
    except ValueError:
        canonical_root_alias = ""
        root_alias_valid = False
    try:
        canonical_source_root = (
            validate_root_alias(source_root) if source_complete and not source_absent else ""
        )
        source_root_valid = source_absent or bool(canonical_source_root)
    except ValueError:
        canonical_source_root = ""
        source_root_valid = False
    try:
        canonical_expected = validate_relative_path(expected_path, label="expected artifact path")
        expected_path_valid = True
    except SafePathError:
        canonical_expected = ""
        expected_path_valid = False
    try:
        canonical_source = (
            validate_relative_path(source_path, label="source artifact path")
            if source_complete and not source_absent
            else ""
        )
        source_path_valid = source_absent or bool(canonical_source)
    except SafePathError:
        canonical_source = ""
        source_path_valid = False
    contract_complete = (
        not unknown
        and isinstance(field, str)
        and bool(field)
        and root_alias_valid
        and expected_path_valid
        and _valid_sha256(expected_hash)
        and source_complete
        and source_root_valid
        and source_path_valid
    )

    recorded = field_value(observation, field, {}) if isinstance(field, str) else {}
    if not isinstance(recorded, Mapping):
        recorded = {}
    observed_path = recorded.get("path")
    try:
        canonical_observed = validate_relative_path(observed_path, label="observed artifact path")
    except SafePathError:
        canonical_observed = ""
    exact_path = bool(contract_complete and canonical_observed == canonical_expected)

    artifact_hash: str | None = None
    artifact_error: str | None = "CONTRACT_INVALID"
    if exact_path:
        artifact_hash, artifact_error = _safe_digest(
            context, canonical_root_alias, canonical_expected
        )
    artifact_safe = artifact_error is None
    hash_ok = bool(
        artifact_safe
        and artifact_hash is not None
        and artifact_hash == expected_hash
    )

    source_hash: str | None = None
    source_error: str | None = None
    source_ok = source_absent
    if source_complete and not source_absent and source_path_valid:
        source_hash, source_error = _safe_digest(
            context, canonical_source_root, canonical_source
        )
        source_ok = bool(source_error is None and source_hash == source_before)
    elif not source_complete or not source_path_valid:
        source_error = "SOURCE_CONTRACT_INVALID"

    return [
        AssertionResult(
            "artifact.contract_complete",
            contract_complete,
            "trusted root alias, canonical relative path, SHA-256, and optional complete source",
            {
                "unknown_fields": unknown,
                "root_alias": root_alias_valid,
                "expected_path": expected_path_valid,
                "expected_sha256": _valid_sha256(expected_hash),
                "source_contract": source_complete and source_root_valid and source_path_valid,
            },
        ),
        AssertionResult("artifact.exact_path", exact_path, canonical_expected, canonical_observed),
        AssertionResult(
            "artifact.safe_regular_file",
            artifact_safe,
            "bounded regular file inside trusted root",
            artifact_error,
        ),
        AssertionResult("artifact.exists", artifact_safe, True, artifact_safe),
        AssertionResult("artifact.sha256", hash_ok, expected_hash, artifact_hash),
        AssertionResult(
            "artifact.source_safe_regular_file",
            source_absent or source_error is None,
            "optional bounded regular source inside trusted root",
            source_error,
        ),
        AssertionResult("artifact.source_unchanged", source_ok, source_before, source_hash),
    ]
