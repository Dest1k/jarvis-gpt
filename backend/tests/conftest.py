from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_gated_operator_mode(monkeypatch):
    """Pin the legacy gated posture for the existing suite.

    ``JARVIS_OPERATOR_FULL_AUTONOMY`` defaults to on for the owner's runtime, where
    the operator is the system administrator and their turn authorizes the work it
    asks for (no clarify-first, no approval gates, clean chat). The regression suite
    below asserts the *gated* contract (clarification questions, approval gates,
    policy tool exposure), so it runs with autonomy disabled. Tests that exercise the
    autonomous posture opt back in with ``monkeypatch.setenv(..., "1")`` — see
    ``test_owner_autonomy.py``.
    """

    monkeypatch.setenv("JARVIS_OPERATOR_FULL_AUTONOMY", "0")
    # Most historical unit tests exercise the explicit legacy-local compatibility
    # path. Production defaults to strict loopback authentication; dedicated API
    # security tests opt into that default explicitly.
    monkeypatch.setenv("JARVIS_API_REQUIRE_TOKEN_ON_LOOPBACK", "0")
