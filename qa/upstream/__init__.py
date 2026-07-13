"""Offline, fail-closed upstream adoption validation."""

from .validator import (
    ADOPTION_MODES,
    ORIGIN_KINDS,
    ValidationIssue,
    ValidationResult,
    Verdict,
    validate_candidate,
    validate_candidate_file,
    validate_provenance,
)

__all__ = [
    "ADOPTION_MODES",
    "ORIGIN_KINDS",
    "ValidationIssue",
    "ValidationResult",
    "Verdict",
    "validate_candidate",
    "validate_candidate_file",
    "validate_provenance",
]
