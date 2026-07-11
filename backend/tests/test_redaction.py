from __future__ import annotations

from jarvis_gpt.redaction import redact_text


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
