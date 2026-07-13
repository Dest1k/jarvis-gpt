"""Verified review-context identities and pairwise independence levels."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9._:/-]{0,126}[a-z0-9])?$")
_RUN_NONCE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class IndependenceLevel(StrEnum):
    DETERMINISTIC_ONLY = "DETERMINISTIC_ONLY"
    SAME_MODEL_CLEAN_CONTEXT = "SAME_MODEL_CLEAN_CONTEXT"
    DIFFERENT_PROFILE = "DIFFERENT_PROFILE"
    DIFFERENT_MODEL = "DIFFERENT_MODEL"
    DIFFERENT_PROVIDER = "DIFFERENT_PROVIDER"
    HUMAN_ADJUDICATED = "HUMAN_ADJUDICATED"


def _identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"review context {field} is not a canonical identifier")
    return value


@dataclass(frozen=True, slots=True)
class ReviewContext:
    context_id: str
    run_nonce: str
    provider: str
    model: str
    profile: str
    context_digest: str

    def __post_init__(self) -> None:
        for field in ("context_id", "provider", "model", "profile"):
            _identity(getattr(self, field), field)
        if not isinstance(self.run_nonce, str) or not _RUN_NONCE.fullmatch(self.run_nonce):
            raise ValueError("review context run_nonce must be 128-bit lowercase hex")
        if not isinstance(self.context_digest, str) or not _DIGEST.fullmatch(self.context_digest):
            raise ValueError("review context digest must be lowercase SHA-256")
        if not self.verified:
            raise ValueError("review context digest mismatch")

    @classmethod
    def create(
        cls,
        *,
        context_id: str,
        provider: str,
        model: str,
        profile: str,
        run_nonce: str | None = None,
    ) -> ReviewContext:
        body = {
            "context_id": _identity(context_id, "context_id"),
            "run_nonce": secrets.token_hex(16) if run_nonce is None else run_nonce,
            "provider": _identity(provider, "provider"),
            "model": _identity(model, "model"),
            "profile": _identity(profile, "profile"),
        }
        if not isinstance(body["run_nonce"], str) or not _RUN_NONCE.fullmatch(body["run_nonce"]):
            raise ValueError("review context run_nonce must be 128-bit lowercase hex")
        return cls(**body, context_digest=_context_digest(body))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReviewContext:
        fields = {
            "context_id",
            "run_nonce",
            "provider",
            "model",
            "profile",
            "context_digest",
        }
        if set(data) != fields:
            raise ValueError("review context fields are incomplete or unexpected")
        run_nonce = data.get("run_nonce")
        context_digest = data.get("context_digest")
        if not isinstance(run_nonce, str) or not isinstance(context_digest, str):
            raise ValueError("review context nonce and digest must be strings")
        context = cls(
            context_id=_identity(data.get("context_id"), "context_id"),
            run_nonce=run_nonce,
            provider=_identity(data.get("provider"), "provider"),
            model=_identity(data.get("model"), "model"),
            profile=_identity(data.get("profile"), "profile"),
            context_digest=context_digest,
        )
        if not _RUN_NONCE.fullmatch(context.run_nonce):
            raise ValueError("review context run_nonce must be 128-bit lowercase hex")
        if not _DIGEST.fullmatch(context.context_digest) or not context.verified:
            raise ValueError("review context digest mismatch")
        return context

    def _digest_body(self) -> dict[str, str]:
        return {
            "context_id": self.context_id,
            "run_nonce": self.run_nonce,
            "provider": self.provider,
            "model": self.model,
            "profile": self.profile,
        }

    @property
    def verified(self) -> bool:
        return self.context_digest == _context_digest(self._digest_body())

    def to_dict(self) -> dict[str, str]:
        return {**self._digest_body(), "context_digest": self.context_digest}


def _context_digest(body: Mapping[str, str]) -> str:
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class IndependenceAssessment:
    verified: bool
    level: IndependenceLevel | None
    reason: str


def assess_independence(
    first: ReviewContext,
    second: ReviewContext,
    *,
    expected_context_digests: tuple[str, str] | None = None,
) -> IndependenceAssessment:
    if not first.verified or not second.verified:
        return IndependenceAssessment(False, None, "Review context digest is unverified.")
    if (
        not isinstance(expected_context_digests, tuple)
        or len(expected_context_digests) != 2
        or any(
            not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
            for digest in expected_context_digests
        )
    ):
        return IndependenceAssessment(
            False,
            None,
            "Two out-of-band review context anchors are required.",
        )
    if expected_context_digests != (first.context_digest, second.context_digest):
        return IndependenceAssessment(False, None, "Review context anchor mismatch.")
    if expected_context_digests[0] == expected_context_digests[1]:
        return IndependenceAssessment(False, None, "Review context anchor was reused.")
    if first.context_id == second.context_id:
        return IndependenceAssessment(False, None, "Review context_id was reused.")
    if first.run_nonce == second.run_nonce:
        return IndependenceAssessment(False, None, "Review run_nonce was reused.")
    if first.provider != second.provider:
        level = IndependenceLevel.DIFFERENT_PROVIDER
    elif first.model != second.model:
        level = IndependenceLevel.DIFFERENT_MODEL
    elif first.profile != second.profile:
        level = IndependenceLevel.DIFFERENT_PROFILE
    else:
        level = IndependenceLevel.SAME_MODEL_CLEAN_CONTEXT
    return IndependenceAssessment(True, level, "Review contexts are distinct and typed.")


def is_independent_model(level: IndependenceLevel) -> bool:
    return level in {
        IndependenceLevel.DIFFERENT_MODEL,
        IndependenceLevel.DIFFERENT_PROVIDER,
    }
