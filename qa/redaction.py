"""Fail-closed recursive redaction for generated assurance output."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

REDACTED = "[REDACTED]"

_SECRET_KEYS = {
    "access_token",
    "access_key_id",
    "access_key",
    "api_key",
    "api_secret",
    "api_token",
    "auth_token",
    "auth_header",
    "authorization",
    "authorization_code",
    "authorization_header",
    "aws_access_key_id",
    "bearer",
    "basic_auth",
    "client_secret",
    "client_assertion",
    "code_verifier",
    "connection_string",
    "connect_sid",
    "cookie",
    "cookie_header",
    "cookies",
    "credential",
    "credentials",
    "csrf",
    "csrfmiddlewaretoken",
    "csrf_token",
    "csrftoken",
    "database_password",
    "database_url",
    "db_password",
    "device_code",
    "dsn",
    "id_token",
    "jsessionid",
    "jarvis_api_token",
    "jwt",
    "jwt_secret",
    "jwt_token",
    "oauth_token",
    "oauth_code",
    "password",
    "passwd",
    "passphrase",
    "pwd",
    "private_key",
    "phpsessid",
    "proxy_authorization",
    "proxy_authorization_header",
    "refresh_token",
    "secret",
    "secret_key",
    "session",
    "laravel_session",
    "auth_session",
    "aspnet_sessionid",
    "session_cookie",
    "session_id",
    "sessionid",
    "session_key",
    "session_token",
    "session_ticket",
    "set_cookie",
    "set_cookie_header",
    "ssh_private_key",
    "subscription_key",
    "token",
    "x_api_key",
    "xsrf_token",
    "xsrf",
}
_SECRET_KEY_SUFFIXES = tuple(
    f"_{key}"
    for key in sorted(
        _SECRET_KEYS
        - {
            "authorization",
            "bearer",
            "cookie",
            "cookies",
            "credential",
            "credentials",
            "dsn",
            "jwt",
            "password",
            "passwd",
            "secret",
            "session",
            "token",
        }
    )
)
_GENERIC_SECRET_SUFFIXES = (
    "_token",
    "_secret",
    "_password",
    "_passwd",
    "_passphrase",
    "_credential",
    "_credentials",
    "_code_verifier",
    "_device_code",
    "_private_key",
    "_cookie",
    "_cookies",
    "_session",
    "_session_id",
    "_session_key",
    "_connection_string",
    "_dsn",
    "_authorization",
    "_access_key",
    "_access_key_id",
    "_auth_header",
    "_authorization_code",
    "_authorization_header",
    "_basic_auth",
    "_client_assertion",
    "_cookie_header",
    "_oauth_code",
    "_session_ticket",
    "_subscription_key",
)
_QUOTE_TOKEN = r"(?:\\+[\"']|\\u00(?:22|27)|[\"'])"
_ASSIGNMENT_START = re.compile(
    rf"(?i)(?<![A-Za-z0-9._-])"
    rf"(?P<name_prefix>--?|_+|\.)?"
    rf"(?P<key_open>{_QUOTE_TOKEN})?"
    rf"(?P<name>[A-Za-z][A-Za-z0-9._-]*(?:[ \t]+[A-Za-z][A-Za-z0-9._-]*){{0,3}})"
    rf"(?P<key_close>{_QUOTE_TOKEN})?\s*[:=]\s*"
    rf"(?P<value_open>{_QUOTE_TOKEN})?"
)
_SECRET_FLAG_START = re.compile(
    rf"(?i)(?<![A-Za-z0-9._-])"
    rf"(?P<flag>--?)(?P<name>[A-Za-z][A-Za-z0-9._-]*)"
    rf"(?P<separator>[ \t]+)(?P<value_open>{_QUOTE_TOKEN})?"
)
_AUTH_HEADER = re.compile(
    r"(?im)(?P<name>\b(?:authorization|proxy-authorization))"
    r"(?P<separator>\s*:\s*)(?P<value>[^\r\n]*)"
)
_COOKIE_HEADER = re.compile(
    r"(?im)(?P<name>\b(?:cookie|set-cookie))"
    r"(?P<separator>\s*:\s*)(?P<value>[^\r\n]*)"
)
_BEARER = re.compile(
    r"(?i)(?P<prefix>\bBearer\s+)(?P<value>[A-Za-z0-9._~+/=-]{4,})"
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_URI_USERINFO = re.compile(
    r"(?i)(?P<prefix>\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)"
    r"(?P<value>[^@\s/]+)(?P<suffix>@)"
)
_CANARY_SHAPE = re.compile(
    r"(?i)\bcanary[_:/.-](?:token|secret|credential|session|cookie|key)"
    r"[_:/.-][^\s,;]+"
)
_SAFE_EMPTY_TEXT = frozenset({"", "null", "none"})


@dataclass(frozen=True, slots=True)
class RedactionResult:
    value: Any
    events: tuple[str, ...]


def _canary_values(canaries: Iterable[str]) -> tuple[str, ...]:
    values: set[str] = set()
    for canary in canaries:
        if not isinstance(canary, str):
            raise TypeError("redaction canaries must be strings")
        if canary and canary != REDACTED:
            values.add(canary)
    return tuple(sorted(values, key=len, reverse=True))


def _normalize_key(key: str) -> str:
    split_acronym = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", key)
    split_camel = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", split_acronym)
    return re.sub(r"[^a-z0-9]+", "_", split_camel.lower()).strip("_")


def _is_secret_key(key: str) -> bool:
    normalized = _normalize_key(key)
    if normalized in _SECRET_KEYS or normalized.endswith(
        _SECRET_KEY_SUFFIXES + _GENERIC_SECRET_SUFFIXES
    ):
        return True
    return normalized.startswith(("private_key_", "ssh_private_key_"))


def _safe_path_segment(key: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", key).strip("_.-")
    return normalized[:64] or "<key>"


def _unquote(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
        return stripped[1:-1]
    return stripped


def _is_safe_text_value(value: str) -> bool:
    unquoted = _unquote(value).strip()
    if unquoted == REDACTED or unquoted.lower() in _SAFE_EMPTY_TEXT:
        return True
    return bool(re.fullmatch(r"(?i)Bearer\s+\[REDACTED\]", unquoted))


def _quoted_value_end(text: str, start: int, opening: str) -> int | None:
    if opening in {'"', "'"}:
        index = start
        while index < len(text):
            if text[index] == opening:
                slash_count = 0
                cursor = index - 1
                while cursor >= start and text[cursor] == "\\":
                    slash_count += 1
                    cursor -= 1
                if slash_count % 2 == 0:
                    return index
            index += 1
        return None
    if opening.lower().startswith("\\u00"):
        lowered = text.lower()
        token = opening.lower()
        index = start
        while True:
            index = lowered.find(token, index)
            if index < 0:
                return None
            if index == 0 or text[index - 1] != "\\":
                return index
            index += len(token)
    quote = opening[-1]
    required_slashes = len(opening) - 1
    index = start
    while index < len(text):
        if text[index] == quote:
            slash_count = 0
            cursor = index - 1
            while cursor >= start and text[cursor] == "\\":
                slash_count += 1
                cursor -= 1
            if slash_count == required_slashes:
                return index - slash_count
        index += 1
    return None


def _unquoted_value_end(text: str, start: int) -> int:
    index = start
    while index < len(text) and text[index] not in " \t\r\n,;&}":
        index += 1
    return index


def _redact_named_values(
    text: str,
    pattern: re.Pattern[str],
) -> tuple[str, int]:
    chunks: list[str] = []
    output_cursor = 0
    search_cursor = 0
    count = 0
    while True:
        match = pattern.search(text, search_cursor)
        if match is None:
            break
        if not _is_secret_key(match.group("name")):
            search_cursor = match.end()
            continue
        value_start = match.end()
        opening = match.group("value_open") or ""
        if opening:
            value_end = _quoted_value_end(text, value_start, opening)
            if value_end is None:
                value_end = len(text)
                next_search = value_end
            else:
                next_search = value_end + len(opening)
        else:
            value_end = _unquoted_value_end(text, value_start)
            next_search = value_end
        raw_value = text[value_start:value_end]
        if _is_safe_text_value(raw_value):
            search_cursor = max(next_search, match.end())
            continue
        chunks.append(text[output_cursor:value_start])
        chunks.append(REDACTED)
        output_cursor = value_end
        search_cursor = max(next_search, value_start + 1)
        count += 1
    if not count:
        return text, 0
    chunks.append(text[output_cursor:])
    return "".join(chunks), count


def _redact_credential_assignments(text: str) -> tuple[str, int]:
    return _redact_named_values(text, _ASSIGNMENT_START)


def _redact_credential_flags(text: str) -> tuple[str, int]:
    return _redact_named_values(text, _SECRET_FLAG_START)


def _replace_explicit_canary(text: str, canary: str) -> tuple[str, int]:
    parts = text.split(REDACTED)
    count = sum(part.count(canary) for part in parts)
    if count:
        parts = [part.replace(canary, REDACTED) for part in parts]
    return REDACTED.join(parts), count


def redact_text(text: str, canaries: Iterable[str] = ()) -> RedactionResult:
    events: list[str] = []
    output = text
    for canary in _canary_values(canaries):
        output, count = _replace_explicit_canary(output, canary)
        events.extend("explicit_canary" for _ in range(count))

    def replace_private_key(match: re.Match[str]) -> str:
        events.append("private_key_material")
        return REDACTED

    output = _PRIVATE_KEY.sub(replace_private_key, output)

    def replace_uri_password(match: re.Match[str]) -> str:
        value = match.group("value")
        if _is_safe_text_value(value):
            return match.group(0)
        events.append("connection_credential")
        return f"{match.group('prefix')}{REDACTED}{match.group('suffix')}"

    output = _URI_USERINFO.sub(replace_uri_password, output)

    def replace_header(match: re.Match[str]) -> str:
        value = match.group("value")
        if _is_safe_text_value(value):
            return match.group(0)
        events.append("credential_header")
        return f"{match.group('name')}{match.group('separator')}{REDACTED}"

    output = _AUTH_HEADER.sub(replace_header, output)
    output = _COOKIE_HEADER.sub(replace_header, output)

    def replace_bearer(match: re.Match[str]) -> str:
        if _is_safe_text_value(match.group(0)):
            return match.group(0)
        events.append("bearer_credential")
        return f"{match.group('prefix')}{REDACTED}"

    output = _BEARER.sub(replace_bearer, output)

    def replace_jwt(match: re.Match[str]) -> str:
        events.append("jwt_credential")
        return REDACTED

    output = _JWT.sub(replace_jwt, output)

    output, flag_count = _redact_credential_flags(output)
    events.extend("credential_flag" for _ in range(flag_count))

    output, assignment_count = _redact_credential_assignments(output)
    events.extend("credential_assignment" for _ in range(assignment_count))

    def replace_canary_shape(match: re.Match[str]) -> str:
        events.append("canary_shape")
        return REDACTED

    output = _CANARY_SHAPE.sub(replace_canary_shape, output)
    return RedactionResult(output, tuple(events))


def redact_value(value: Any, canaries: Iterable[str] = (), path: str = "$") -> RedactionResult:
    canary_values = _canary_values(canaries)
    if isinstance(value, str):
        result = redact_text(value, canary_values)
        return RedactionResult(result.value, tuple(f"{path}:{event}" for event in result.events))
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        events: list[str] = []
        for key, item in value.items():
            key_text = str(key)
            if key_text in redacted:
                raise ValueError("mapping keys collide after string conversion")
            child_path = f"{path}.{_safe_path_segment(key_text)}"
            if (
                _is_secret_key(key_text)
                and item is not None
                and item != ""
                and item != REDACTED
            ):
                redacted[key_text] = REDACTED
                events.append(f"{child_path}:credential_key")
                continue
            child = redact_value(item, canary_values, child_path)
            redacted[key_text] = child.value
            events.extend(child.events)
        return RedactionResult(redacted, tuple(events))
    if isinstance(value, list | tuple):
        redacted_items: list[Any] = []
        events: list[str] = []
        for index, item in enumerate(value):
            child = redact_value(item, canary_values, f"{path}[{index}]")
            redacted_items.append(child.value)
            events.extend(child.events)
        return RedactionResult(redacted_items, tuple(events))
    return RedactionResult(value, ())


def credential_like_paths(
    value: Any,
    path: str = "$",
    canaries: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return sanitized paths that still appear to contain credential material."""

    canary_values = _canary_values(canaries)
    findings: list[str] = []
    if isinstance(value, str):
        if redact_text(value, canary_values).value != value:
            findings.append(path)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{_safe_path_segment(key_text)}"
            if redact_text(key_text, canary_values).value != key_text:
                findings.append(f"{path}.<key>")
            if (
                _is_secret_key(key_text)
                and item is not None
                and item != ""
                and item != REDACTED
            ):
                findings.append(child_path)
            findings.extend(credential_like_paths(item, child_path, canary_values))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            findings.extend(credential_like_paths(item, f"{path}[{index}]", canary_values))
    return tuple(findings)
