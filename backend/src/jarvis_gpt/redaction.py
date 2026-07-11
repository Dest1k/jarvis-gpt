from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_SECRET_TERM = (
    r"(?:password|passwd|secret|token|credential|authorization|cookie|"
    r"private[_-]?key|api[_-]?key|proxy[_-]?url)"
)
_ASSIGNMENT_TERM = (
    r"(?:password|passwd|secret|token|credential|private[_-]?key|"
    r"api[_-]?key|proxy[_-]?url)"
)
_SECRET_KEY = re.compile(rf"(?i)[A-Za-z0-9_.-]*{_SECRET_TERM}[A-Za-z0-9_.-]*")
_AUTH_VALUE = re.compile(
    r"(?i)\b((?:Bearer|Basic)\s+)[A-Za-z0-9._~+/=-]{4,}"
)
_ASSIGNMENT = re.compile(
    rf"(?i)([\"']?[A-Za-z0-9_.-]*{_ASSIGNMENT_TERM}[A-Za-z0-9_.-]*[\"']?)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;\]\}]+)"
)
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s]+)@")
_AUTH_HEADER = re.compile(
    r"(?im)(?<![A-Za-z0-9_-])((?:proxy-)?authorization\s*[:=]\s*)[^\r\n]+"
)
_COOKIE_HEADER = re.compile(
    r"(?im)(?<![A-Za-z0-9_-])((?:set-)?cookie\s*[:=]\s*)[^\r\n]+"
)
_PRIVATE_KEY = re.compile(
    r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----"
)
_KNOWN_TOKEN = re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{12,})")


def redact_text(value: Any) -> str:
    text = str(value)
    text = _URL_USERINFO.sub(r"\1[redacted]@", text)
    text = _AUTH_VALUE.sub(r"\1[redacted]", text)
    text = _AUTH_HEADER.sub(r"\1[redacted]", text)
    text = _COOKIE_HEADER.sub(r"\1[redacted]", text)
    text = _PRIVATE_KEY.sub("[redacted:private-key]", text)
    text = _KNOWN_TOKEN.sub("[redacted:token]", text)
    return _ASSIGNMENT.sub(r"\1\2[redacted]", text)


def redact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 20:
        return "[redacted:depth]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, nested in value.items():
            key = str(raw_key)
            result[key] = (
                "[redacted]"
                if _SECRET_KEY.search(key)
                else redact_value(nested, depth=depth + 1)
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | memoryview):
        return [redact_value(item, depth=depth + 1) for item in value]
    return redact_text(value)
