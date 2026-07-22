"""Out-of-band operator notifications (push to the owner's phone).

Deliberately tiny and dependency-free beyond ``httpx`` (already a core dep). The
supervisor uses this to push proactive health alerts to Telegram without pulling in
the whole long-poll bridge. ``TELEGRAM_ALLOWED_CHAT_IDS`` is only an access-control
list; proactive recipients live in the separate ``TELEGRAM_ALERT_CHAT_IDS`` list so
granting somebody permission to talk to the bot cannot leak owner-only alerts.

Fails closed and fails quiet: when the token/allowlist are unset it returns ``False``
without raising, so a box that never configured Telegram simply keeps alerting through
the runtime event log + UI bus and skips the phone push.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .telegram_journal import record_telegram_outbound

if TYPE_CHECKING:
    from .storage import JarvisStorage

_TIMEOUT_SEC = 8.0
_QUIET_RANGE_RE = re.compile(
    r"^\s*(\d{1,2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?\s*$"
)


def _parse_chat_ids(value: str | None) -> tuple[int, ...]:
    ids: list[int] = []
    for part in re.split(r"[,\s]+", str(value or "").strip()):
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return tuple(dict.fromkeys(ids))


def telegram_targets(
    env: Mapping[str, str] | None = None,
    *,
    requested_chat_ids: Iterable[int] | None = None,
) -> tuple[str, tuple[int, ...]]:
    """Resolve ``(bot_token, authorised alert recipients)``.

    Explicit recipients are intersected with the bridge allowlist. For untargeted
    alerts, ``TELEGRAM_ALERT_CHAT_IDS`` is authoritative. A single-chat installation
    retains the old zero-config behaviour; a multi-user installation without an alert
    list fails closed instead of broadcasting private notifications to every user.
    """

    source = os.environ if env is None else env
    token = (source.get("TELEGRAM_BOT_TOKEN") or "").strip()
    allowed = _parse_chat_ids(source.get("TELEGRAM_ALLOWED_CHAT_IDS"))
    allowed_set = set(allowed)
    if requested_chat_ids is not None:
        candidates = tuple(dict.fromkeys(int(item) for item in requested_chat_ids))
    else:
        configured = _parse_chat_ids(source.get("TELEGRAM_ALERT_CHAT_IDS"))
        candidates = configured or (allowed if len(allowed) == 1 else ())
    return token, tuple(chat_id for chat_id in candidates if chat_id in allowed_set)


def parse_quiet_hours(spec: str | None) -> tuple[time, time] | None:
    """Parse ``quiet_hours`` preference into ``(start, end)`` wall-clock times.

    Accepted forms: ``23:00-08:00``, ``23-8``, ``22:30–07:00``. Empty/invalid → None.
    """

    raw = str(spec or "").strip()
    if not raw:
        return None
    match = _QUIET_RANGE_RE.match(raw)
    if match is None:
        return None
    sh, sm, eh, em = match.groups()
    start = time(int(sh) % 24, int(sm or 0) % 60)
    end = time(int(eh) % 24, int(em or 0) % 60)
    if start == end:
        return None
    return start, end


def in_quiet_hours(
    spec: str | None,
    *,
    now: datetime | None = None,
    tz_name: str = "Europe/Moscow",
) -> bool:
    """True when local wall-clock is inside the operator's quiet window (may wrap midnight)."""

    bounds = parse_quiet_hours(spec)
    if bounds is None:
        return False
    start, end = bounds
    try:
        tz: timezone | ZoneInfo = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        # Windows test hosts may lack tzdata; Moscow is a fixed +03:00 fallback.
        tz = timezone(timedelta(hours=3), name=tz_name or "Europe/Moscow")
    current = now
    if current is None:
        current = datetime.now(tz)
    elif current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    else:
        current = current.astimezone(tz)
    local_t = time(current.hour, current.minute)
    if start < end:
        return start <= local_t < end
    # Wraps midnight: e.g. 23:00-08:00
    return local_t >= start or local_t < end


def reminder_inline_keyboard(reminder_id: str) -> dict[str, Any]:
    """Inline snooze/done buttons for a fired passive reminder.

    ``callback_data`` is capped at 64 bytes by Telegram; the compact form
    ``r:<id>:<action>`` stays well under that for ``rem_<16hex>`` ids.
    """

    rid = str(reminder_id or "").strip()
    return {
        "inline_keyboard": [
            [
                {"text": "⏳ 10 мин", "callback_data": f"r:{rid}:s10"},
                {"text": "⏳ 1 час", "callback_data": f"r:{rid}:s60"},
                {"text": "✅ Готово", "callback_data": f"r:{rid}:ok"},
            ]
        ]
    }


def answer_action_keyboard() -> dict[str, Any]:
    """Optional inline chips under an agent answer (inbox / remind / more).

    Disabled by default on every chat turn — the day-console reply keyboard and
    reminder-specific snooze buttons cover the same actions without cluttering
    normal answers. Kept for explicit callers / tests.
    """

    return {
        "inline_keyboard": [
            [
                {"text": "📥 Inbox", "callback_data": "a:inbox"},
                {"text": "⏰ +1ч", "callback_data": "a:r60"},
                {"text": "➕ Ещё", "callback_data": "a:more"},
            ]
        ]
    }


def progress_stop_keyboard() -> dict[str, Any]:
    """Stop chip on the mid-turn progress message."""

    return {
        "inline_keyboard": [[{"text": "🛑 Стоп", "callback_data": "a:stop"}]]
    }


def operator_reply_keyboard() -> dict[str, Any]:
    """Persistent day-console reply keyboard for the phone operator."""

    return {
        "keyboard": [
            [{"text": "📋 Сводка"}, {"text": "📊 Статус"}],
            [{"text": "📥 Inbox"}, {"text": "🛑 Стоп"}],
            [{"text": "🆕 Новый чат"}, {"text": "❓ Помощь"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


async def push_telegram_alert(
    text: str,
    *,
    target_chat_ids: Iterable[int] | None = None,
    env: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
    reply_markup: Mapping[str, Any] | dict[str, Any] | None = None,
    disable_notification: bool = False,
    storage: JarvisStorage | None = None,
) -> bool:
    """Send ``text`` to authorised alert targets. Returns True if it reached ≥1 chat.

    Optional ``reply_markup`` (Telegram InlineKeyboardMarkup) is attached when set.
    ``disable_notification`` silences the phone buzz (used in quiet hours).
    Never raises: unconfigured credentials or any transport error resolve to ``False``
    so a health-alert push can never take down the supervisor loop that called it.
    """

    token, chat_ids = telegram_targets(env, requested_chat_ids=target_chat_ids)
    if not token or not chat_ids:
        return False
    owns_client = client is None
    http = client or httpx.AsyncClient(
        base_url=f"https://api.telegram.org/bot{token}",
        timeout=_TIMEOUT_SEC,
    )
    delivered = 0
    try:
        for chat_id in chat_ids:
            try:
                payload: dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                    "disable_notification": bool(disable_notification),
                }
                if reply_markup is not None:
                    payload["reply_markup"] = dict(reply_markup)
                response = await http.post("/sendMessage", json=payload)
                if response.status_code == 200:
                    delivered += 1
                    if storage is not None:
                        try:
                            body = response.json()
                            result = body.get("result") if isinstance(body, dict) else None
                            token_match = re.match(r"^([1-9][0-9]{0,18}):", token)
                            if (
                                isinstance(result, dict)
                                and body.get("ok") is True
                                and token_match is not None
                            ):
                                with storage.transaction(immediate=True) as conn:
                                    record_telegram_outbound(
                                        conn,
                                        realm_id=f"telegram:{token_match.group(1)}",
                                        chat_id=chat_id,
                                        text=text,
                                        telegram_message=result,
                                        metadata={"source": "notify"},
                                    )
                        except Exception:
                            # Delivery already succeeded. A journal outage must not make
                            # the supervisor retry and duplicate the Telegram alert.
                            pass
            except httpx.HTTPError:
                continue
    finally:
        if owns_client:
            await http.aclose()
    return delivered > 0
