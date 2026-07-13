"""Exact artifact path, existence, hash, and source-integrity checks."""

from __future__ import annotations

import hashlib
import os
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


def validate_artifact(
    observation: Mapping[str, Any], spec: Mapping[str, Any]
) -> list[AssertionResult]:
    field = str(spec.get("field", "artifact"))
    recorded = field_value(observation, field, observation)
    if not isinstance(recorded, Mapping):
        recorded = {}
    observed_path = str(recorded.get("path", ""))
    expected_path = str(spec.get("expected_path", ""))
    exact_path = bool(observed_path and expected_path) and (
        _normalized(observed_path) == _normalized(expected_path)
    )
    file_path = Path(observed_path) if observed_path else None
    exists = recorded.get("exists")
    if not isinstance(exists, bool):
        exists = bool(file_path and file_path.is_file())
    observed_hash = recorded.get("sha256")
    if not observed_hash and exists and file_path and file_path.is_file():
        observed_hash = _sha256(file_path)
    expected_hash = spec.get("expected_sha256")
    hash_ok = expected_hash is None or (exists and observed_hash == expected_hash)
    source_before = spec.get("source_sha256_before")
    source_after = recorded.get("source_sha256_after")
    source_ok = source_before is None or source_before == source_after
    return [
        AssertionResult("artifact.exact_path", exact_path, expected_path, observed_path),
        AssertionResult("artifact.exists", exists, True, exists),
        AssertionResult("artifact.sha256", hash_ok, expected_hash, observed_hash),
        AssertionResult("artifact.source_unchanged", source_ok, source_before, source_after),
    ]
