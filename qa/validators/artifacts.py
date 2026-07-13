"""Exact artifact path, existence, hash, and source-integrity checks."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..models import AssertionResult
from .format_contracts import field_value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalized(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


def _actual_sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        return _sha256(path)
    except OSError:
        return None


def validate_artifact(
    observation: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[AssertionResult]:
    field = str(spec.get("field", "artifact"))
    recorded = field_value(observation, field, observation)
    if not isinstance(recorded, Mapping):
        recorded = {}
    observed_path = str(recorded.get("path", ""))
    expected_path = str(spec.get("expected_path", ""))
    expected_hash = spec.get("expected_sha256")
    source_path_value = spec.get("source_path")
    source_before = spec.get("source_sha256_before")
    source_contract_complete = (source_path_value is None and source_before is None) or (
        isinstance(source_path_value, str)
        and bool(source_path_value)
        and _valid_sha256(source_before)
    )
    contract_complete = bool(expected_path) and _valid_sha256(expected_hash) and (
        source_contract_complete
    )
    exact_path = bool(observed_path and expected_path) and (
        _normalized(observed_path) == _normalized(expected_path)
    )
    # Only touch the contract-approved paths. Recorded existence and hashes are
    # claims, never evidence.
    artifact_path = Path(expected_path) if exact_path else None
    observed_hash = _actual_sha256(artifact_path)
    exists = observed_hash is not None
    hash_ok = bool(
        _valid_sha256(expected_hash)
        and observed_hash
        and observed_hash.lower() == str(expected_hash).lower()
    )
    source_path = (
        Path(source_path_value) if source_contract_complete and source_path_value else None
    )
    source_after = _actual_sha256(source_path)
    source_ok = source_before is None and source_path_value is None
    if source_path is not None and _valid_sha256(source_before):
        source_ok = bool(source_after and source_after.lower() == str(source_before).lower())
    return [
        AssertionResult(
            "artifact.contract_complete",
            contract_complete,
            "expected_path + expected_sha256; source_path + source_sha256_before as a pair",
            {
                "expected_path": bool(expected_path),
                "expected_sha256": _valid_sha256(expected_hash),
                "source_contract_complete": source_contract_complete,
            },
        ),
        AssertionResult("artifact.exact_path", exact_path, expected_path, observed_path),
        AssertionResult("artifact.exists", exists, True, exists),
        AssertionResult("artifact.sha256", hash_ok, expected_hash, observed_hash),
        AssertionResult("artifact.source_unchanged", source_ok, source_before, source_after),
    ]
