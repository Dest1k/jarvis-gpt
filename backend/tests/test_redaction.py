from __future__ import annotations

from jarvis_gpt.redaction import redact_text, redact_value


def test_header_secrets_are_redacted_for_colon_and_assignment_delimiters():
    raw = (
        "Authorization=ApiKey AUTHSECRET\n"
        "Proxy-Authorization = Digest username=user,response=PROXYSECRET\r\n"
        "Cookie=session=COOKIESECRET; theme=dark\n"
        "Set-Cookie = session=SETCOOKIESECRET; HttpOnly\n"
        "safe=value"
    )

    redacted = redact_text(raw)

    for secret in (
        "AUTHSECRET",
        "PROXYSECRET",
        "COOKIESECRET",
        "SETCOOKIESECRET",
    ):
        assert secret not in redacted
    assert redacted.count("[redacted]") == 4
    assert "safe=value" in redacted


def test_compose_yaml_api_token_assignment_is_redacted():
    """Shared contract for doctor/smoke compose-config canary scanning."""
    canary = "CANARY_TOKEN_SPARK0017_shared"
    raw = (
        "services:\n"
        "  backend:\n"
        "    environment:\n"
        f"      JARVIS_API_TOKEN: {canary}\n"
        "      JARVIS_BACKEND_URL: http://backend:8000\n"
        f"      Authorization: Bearer {canary}\n"
    )

    redacted = redact_text(raw)

    assert canary not in redacted
    assert "JARVIS_API_TOKEN: [redacted]" in redacted
    assert "JARVIS_BACKEND_URL: http://backend:8000" in redacted
    assert "[redacted]" in redacted


def test_redact_value_scrubs_secret_keys_and_nested_text():
    canary = "CANARY_TOKEN_SPARK0017_value"
    payload = {
        "ok": True,
        "checks": [
            {
                "name": "docker compose config",
                "stdout_tail": f"JARVIS_API_TOKEN: {canary}",
                "JARVIS_API_TOKEN": canary,
            }
        ],
    }

    redacted = redact_value(payload)

    assert canary not in str(redacted)
    assert redacted["checks"][0]["JARVIS_API_TOKEN"] == "[redacted]"
    assert "[redacted]" in redacted["checks"][0]["stdout_tail"]
