"""Natural-language reminder time parsing (Russian), hand-rolled, no heavy deps.

Turns "напомни завтра в 10 …", "через 2 часа", "каждый день в 9", "в понедельник в 14:00"
into a concrete UTC due-time (+ an optional recurrence descriptor), and advances a recurring
due-time strictly past a given moment so downtime never causes a fire-storm.

Times are parsed and rendered in the operator's local zone (Europe/Moscow by default) but
STORED as UTC ISO strings that match storage.utc_now() exactly, so a plain string
``due_at <= now`` comparison in SQL is correct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from datetime import time as dtime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Europe/Moscow"
DEFAULT_TIME = dtime(9, 0)

# Base forms; substring matching also catches inflections (понедельник/понедельникам).
_WEEKDAYS = {
    "понедельник": 0,
    "вторник": 1,
    "сред": 2,  # среда / среду
    "четверг": 3,
    "пятниц": 4,  # пятница / пятницу
    "суббот": 5,  # суббота / субботу
    "воскресень": 6,
}
_UNIT_SECONDS = {"минут": 60, "час": 3600, "дн": 86400, "недел": 604800}


def reminder_zone(name: str = DEFAULT_TZ):
    """The operator's zone, with a fixed +03:00 fallback when tz data is missing."""

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone(timedelta(hours=3), name=DEFAULT_TZ)


def to_utc_iso(dt: datetime) -> str:
    """UTC ISO string byte-compatible with storage.utc_now() ('...+00:00', seconds)."""

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=reminder_zone())
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def render_local(due_at_iso: str, *, tz=None) -> str:
    """Render a stored UTC due-time as a friendly local wall-clock string."""

    tz = tz or reminder_zone()
    try:
        dt = datetime.fromisoformat(due_at_iso)
    except ValueError:
        return due_at_iso
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


@dataclass
class ParsedWhen:
    due_local: datetime | None
    recurrence: dict | None
    matched: bool


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("ё", "е")).strip().lower()


def _weekday_in(text: str) -> int | None:
    for stem, idx in _WEEKDAYS.items():
        if stem in text:
            return idx
    return None


def _time_of_day(text: str) -> dtime | None:
    if "полноч" in text:
        return dtime(0, 0)
    if "полдень" in text or "полудень" in text:
        return dtime(12, 0)
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", text)
    if m:
        return dtime(int(m.group(1)) % 24, int(m.group(2)) % 60)
    m = re.search(r"\bв\s+(\d{1,2})(?:\s*час)?\b", text)
    if m:
        hour = int(m.group(1)) % 24
        if ("вечера" in text or "вечером" in text) and hour < 12:
            hour += 12
        if ("утра" in text or "утром" in text) and hour == 12:
            hour = 0
        return dtime(hour, 0)
    return None


def _at_on(day: datetime, at: dtime) -> datetime:
    return day.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)


def _next_at(now: datetime, at: dtime) -> datetime:
    candidate = _at_on(now, at)
    return candidate if candidate > now else candidate + timedelta(days=1)


def _next_on_weekday(now: datetime, weekday: int, at: dtime) -> datetime:
    candidate = _at_on(now, at)
    delta = (weekday - candidate.weekday()) % 7
    candidate += timedelta(days=delta)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _next_weekday_workday(now: datetime, at: dtime) -> datetime:
    candidate = _next_at(now, at)
    while candidate.weekday() >= 5:  # Sat/Sun
        candidate += timedelta(days=1)
        candidate = _at_on(candidate, at)
    return candidate


def _parse_iso_when(text: str, *, tz) -> ParsedWhen | None:
    """Accept model-emitted ISO-8601 timestamps (with or without timezone).

    The weak local model often converts "через 5 минут" into an absolute ISO
    string before calling reminders.create. That must round-trip exactly — never
    fall through to the HH:MM heuristic that misreads ``…T19:55:00+03:00`` as
    ``55:00`` → 07:00.
    """

    raw = (text or "").strip()
    if not raw:
        return None
    # Require a date-looking prefix so bare "19:55" stays on the time-of-day path.
    if not re.match(r"^\d{4}-\d{2}-\d{2}[tT\s]", raw):
        return None
    candidate = raw.replace("Z", "+00:00").replace("z", "+00:00")
    # fromisoformat accepts " " or "T" separators; normalize space → T for older Pythons.
    if " " in candidate[:20] and "T" not in candidate[:20] and "t" not in candidate[:20]:
        candidate = candidate.replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return ParsedWhen(dt.astimezone(tz), None, True)


def parse_when(
    text: str,
    *,
    now: datetime | None = None,
    tz=None,
    default_time: dtime = DEFAULT_TIME,
) -> ParsedWhen:
    """Parse a Russian (or light English) time phrase into a local due-time + optional recurrence."""

    tz = tz or reminder_zone()
    now = now.astimezone(tz) if now else datetime.now(tz)
    # Absolute ISO first — before any HH:MM / day.month heuristics can misfire on it.
    iso = _parse_iso_when(text, tz=tz)
    if iso is not None:
        return iso
    t = _norm(text)
    tod = _time_of_day(t)

    # ---- recurrence ----
    m = re.search(r"кажд(?:ый|ые|ую)\s+(\d+)\s*(минут|час|дн|недел)", t)
    if m:
        secs = _UNIT_SECONDS[m.group(2)] * int(m.group(1))
        return ParsedWhen(
            now + timedelta(seconds=secs), {"kind": "interval", "seconds": secs}, True
        )
    if "ежедневно" in t or "каждый день" in t or "каждый вечер" in t or "каждое утро" in t:
        at = tod or (dtime(21, 0) if "вечер" in t else default_time)
        return ParsedWhen(_next_at(now, at), {"kind": "daily", "at": at.strftime("%H:%M")}, True)
    if "по будням" in t or "каждый будний" in t:
        at = tod or default_time
        return ParsedWhen(
            _next_weekday_workday(now, at), {"kind": "weekdays", "at": at.strftime("%H:%M")}, True
        )
    m = re.search(r"кажд(?:ый|ую)\s+(\w+)", t)
    if m:
        wd = _weekday_in(m.group(1))
        if wd is not None:
            at = tod or default_time
            return ParsedWhen(
                _next_on_weekday(now, wd, at),
                {"kind": "weekly", "weekday": wd, "at": at.strftime("%H:%M")},
                True,
            )

    # ---- one-shot ----
    m = re.search(r"через\s+(\d+)\s*(минут|час|дн|недел)", t)
    if m:
        secs = _UNIT_SECONDS[m.group(2)] * int(m.group(1))
        return ParsedWhen(now + timedelta(seconds=secs), None, True)
    # English relative ("in 5 minutes") — models emit this under tool protocols.
    m = re.search(
        r"\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)\b",
        t,
    )
    if m:
        unit = m.group(2)
        n = int(m.group(1))
        if unit.startswith("min"):
            secs = 60 * n
        elif unit.startswith("hour") or unit.startswith("hr"):
            secs = 3600 * n
        elif unit.startswith("day"):
            secs = 86400 * n
        else:
            secs = 604800 * n
        return ParsedWhen(now + timedelta(seconds=secs), None, True)
    at = tod or default_time
    if "послезавтра" in t:
        return ParsedWhen(_at_on(now + timedelta(days=2), at), None, True)
    if "завтра" in t or re.search(r"\btomorrow\b", t):
        return ParsedWhen(_at_on(now + timedelta(days=1), at), None, True)
    if "сегодня" in t or re.search(r"\btoday\b", t):
        return ParsedWhen(_at_on(now, at), None, True)
    wd = _weekday_in(t)
    if wd is not None:
        return ParsedWhen(_next_on_weekday(now, wd, at), None, True)
    md = re.search(r"\b(\d{1,2})[.\s]+(\d{1,2})(?:[.\s]+(\d{2,4}))?\b", t)
    if md:
        day, month = int(md.group(1)), int(md.group(2))
        year = int(md.group(3) or now.year)
        if year < 100:
            year += 2000
        try:
            due = now.replace(
                year=year,
                month=month,
                day=day,
                hour=at.hour,
                minute=at.minute,
                second=0,
                microsecond=0,
            )
            return ParsedWhen(due, None, True)
        except ValueError:
            pass
    if tod is not None:
        return ParsedWhen(_next_at(now, tod), None, True)
    return ParsedWhen(None, None, False)


def compute_next_due(recurrence: dict, *, after: datetime, tz=None) -> datetime | None:
    """Advance a recurring reminder to the first occurrence strictly after ``after``.

    Rolls all the way past ``after`` in one shot (no catch-up burst after downtime).
    """

    tz = tz or reminder_zone()
    after = after.astimezone(tz)
    kind = recurrence.get("kind")
    if kind == "interval":
        secs = int(recurrence.get("seconds") or 0)
        if secs <= 0:
            return None
        step = timedelta(seconds=secs)
        nxt = after + step
        return nxt
    at = _parse_hhmm(recurrence.get("at")) or DEFAULT_TIME
    if kind == "daily":
        return _next_at(after, at)
    if kind == "weekdays":
        return _next_weekday_workday(after, at)
    if kind == "weekly":
        weekday = int(recurrence.get("weekday", 0))
        return _next_on_weekday(after, weekday, at)
    return None


def _parse_hhmm(value: object) -> dtime | None:
    if not isinstance(value, str):
        return None
    m = re.match(r"(\d{1,2}):(\d{2})", value.strip())
    if not m:
        return None
    return dtime(int(m.group(1)) % 24, int(m.group(2)) % 60)
