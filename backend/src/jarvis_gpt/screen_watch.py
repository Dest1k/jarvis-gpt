"""Deterministic parsing and result contracts for proactive screen watches.

The watcher itself reuses the reminders scheduler.  This module intentionally keeps the
natural-language matcher pure so a request is either recognised with a bounded schedule or
left to the normal one-shot screen route; no LLM is asked to decide whether a long-running
desktop observation was requested.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

DEFAULT_INTERVAL_SEC = 300


@dataclass(frozen=True)
class ScreenWatchRequest:
    condition: str
    interval_sec: int
    duration_sec: int
    keep: bool
    interval_clamped: bool = False
    duration_clamped: bool = False


@dataclass(frozen=True)
class ScreenConditionCheck:
    """Outcome of one capture + VLM classification.

    ``met=None`` means the observation could not be made or the classifier did not obey
    the YES/NO contract.  The supervisor treats that exactly like a missed poll and keeps
    the watch pending; transient capture/model failures never generate a false alert.
    """

    met: bool | None
    detail: str = ""
    error: str | None = None


_WATCH_ANCHORS = (
    re.compile(
        r"\b(?:следи|наблюдай|мониторь)\s+(?:(?:за|на)\s+)?"
        r"(?:(?:моим|моем|моём|этим)\s+)?(?:экран(?:ом|е)?|монитор(?:ом|е)?)\b"
    ),
    re.compile(r"\bсмотри\s+на\s+(?:(?:мой|моём|моем)\s+)?(?:экран|монитор)\b"),
    re.compile(r"\b(?:watch|monitor)\s+(?:(?:my|the)\s+)?screen\b"),
    re.compile(r"\bkeep\s+watching\s+(?:(?:my|the)\s+)?screen\b"),
)

_CONDITION_PATTERNS = (
    re.compile(r"\bпока\s+не\s+(?P<condition>.+)$"),
    re.compile(
        r"\b(?:скажи|сообщи|напиши|уведоми)(?:\s+мне)?\s*,?\s*"
        r"(?:когда|как\s+только)\s+(?P<condition>.+)$"
    ),
    re.compile(
        r"\bдай\s+знать\s*,?\s*(?:когда|как\s+только)\s+(?P<condition>.+)$"
    ),
    re.compile(
        r"\bкак\s+только\s+(?P<condition>.+?)(?:\s*,?\s*"
        r"(?:скажи|сообщи|напиши|уведоми)(?:\s+мне)?)?$"
    ),
    re.compile(r"\buntil\s+(?P<condition>.+)$"),
    re.compile(
        r"\b(?:notify|tell|ping|alert)\s+me\s+when\s+(?P<condition>.+)$"
    ),
    re.compile(r"\blet\s+me\s+know\s+when\s+(?P<condition>.+)$"),
)

_INTERVAL_RE = re.compile(
    r"\b(?:кажд(?:ые|ую|ый)|every)\s+(?P<count>\d{1,4})\s*"
    r"(?P<unit>секунд\w*|сек\.?|минут\w*|мин\.?|час\w*|seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    flags=re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"\b(?:в\s+течение|на|for)\s+(?P<count>\d{1,4})\s*"
    r"(?P<unit>секунд\w*|сек\.?|минут\w*|мин\.?|час\w*|seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    flags=re.IGNORECASE,
)
_KEEP_RE = re.compile(
    r"\b(?:постоянно|вс[её]\s+время|продолжай\s+следить|не\s+останавливайся|"
    r"keep\s+watching|do\s+not\s+stop|don't\s+stop)\b",
    flags=re.IGNORECASE,
)
_TRAILING_NOTIFY_RE = re.compile(
    r"(?:\s*[,;—-]?\s*(?:скажи|сообщи|напиши|уведоми)(?:\s+мне)?\s*)$",
    flags=re.IGNORECASE,
)


def parse_screen_watch_request(
    text: str,
    *,
    min_interval_sec: int,
    default_duration_sec: int,
    max_duration_sec: int,
) -> ScreenWatchRequest | None:
    """Parse a high-confidence long-running screen observation request.

    Both a watch anchor and a notification/until connector are required.  Consequently
    ordinary turns such as ``посмотри на экран`` and ``что на экране`` remain one-shot
    captures and can never accidentally create a background watcher.
    """

    original = unicodedata.normalize("NFC", str(text or "")).strip()
    normalized = original.lower()
    if not normalized or not any(pattern.search(normalized) for pattern in _WATCH_ANCHORS):
        return None

    condition = ""
    for pattern in _CONDITION_PATTERNS:
        match = pattern.search(normalized)
        if match:
            start, end = match.span("condition")
            condition = original[start:end]
            break
    if not condition:
        return None

    requested_interval = _seconds_from_match(_INTERVAL_RE.search(normalized))
    safe_min = max(5, int(min_interval_sec))
    interval = requested_interval or DEFAULT_INTERVAL_SEC
    interval_clamped = interval < safe_min
    interval = max(safe_min, interval)

    safe_max_duration = max(safe_min, int(max_duration_sec))
    safe_default_duration = max(safe_min, min(int(default_duration_sec), safe_max_duration))
    requested_duration = _seconds_from_match(_DURATION_RE.search(normalized))
    duration = requested_duration or safe_default_duration
    duration_clamped = duration > safe_max_duration
    duration = max(interval, min(duration, safe_max_duration))
    if interval > safe_max_duration:
        interval = safe_max_duration
        interval_clamped = True
        duration = safe_max_duration

    condition = _INTERVAL_RE.sub(" ", condition)
    condition = _DURATION_RE.sub(" ", condition)
    condition = _KEEP_RE.sub(" ", condition)
    condition = _TRAILING_NOTIFY_RE.sub("", condition)
    condition = re.sub(r"\s+", " ", condition).strip(" \t\r\n,.;:!?—-")
    if condition.startswith("что "):
        condition = condition[4:].strip()
    if len(condition) < 3:
        return None

    return ScreenWatchRequest(
        condition=condition[:500],
        interval_sec=interval,
        duration_sec=duration,
        keep=bool(_KEEP_RE.search(normalized)),
        interval_clamped=interval_clamped,
        duration_clamped=duration_clamped,
    )


def parse_screen_condition_answer(answer: str | None) -> ScreenConditionCheck:
    """Enforce the VLM's first-line YES/NO protocol without guessing."""

    raw = str(answer or "").strip()
    if not raw:
        return ScreenConditionCheck(met=None, error="empty vision response")
    first, _, remainder = raw.partition("\n")
    match = re.match(r"^\s*(YES|NO|ДА|НЕТ)(?=\b|\s|[:;,.!—-])", first, flags=re.IGNORECASE)
    if not match:
        return ScreenConditionCheck(met=None, error="vision response did not start with YES/NO")
    token = match.group(1).upper()
    detail_first = first[match.end() :].strip(" \t:;,.!—-")
    detail = "\n".join(part for part in (detail_first, remainder.strip()) if part).strip()
    return ScreenConditionCheck(met=token in {"YES", "ДА"}, detail=detail[:1000])


def extract_screen_capture_path(data: Any) -> str | None:
    """Read the verified bridge's nested screen path without duplicating shape logic."""

    if not isinstance(data, dict):
        return None
    native = data.get("native")
    if not isinstance(native, dict):
        return None
    parsed = native.get("result")
    observation = parsed if isinstance(parsed, dict) else native
    native_data = observation.get("data")
    if not isinstance(native_data, dict):
        return None
    path = native_data.get("path")
    return path if isinstance(path, str) and path.strip() else None


def human_duration(seconds: int) -> str:
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} ч"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f"{minutes} мин"
    return f"{seconds} с"


def _seconds_from_match(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    count = int(match.group("count"))
    unit = match.group("unit").lower().rstrip(".")
    if unit.startswith(("час", "hour", "hr")):
        return count * 3600
    if unit.startswith(("мин", "minute", "min")):
        return count * 60
    return count
