"""Reviewed out-of-band SHA-256 anchors for committed assurance fixtures."""

from __future__ import annotations

from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_TRUSTED_MANIFEST_SHA256 = {
    "qa/tests/fixtures/calibration_evidence.jsonl": (
        "fc72e8462525a24a19b63ba5fc7c65051cf014491c486878e27a758a338b092d"
    ),
}


def trusted_manifest_sha256(evidence_path: Path) -> str | None:
    """Return a reviewed pin only for an exact committed repository path."""

    try:
        relative = evidence_path.resolve(strict=True).relative_to(_REPOSITORY_ROOT)
    except (OSError, ValueError):
        return None
    return _TRUSTED_MANIFEST_SHA256.get(relative.as_posix())
