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

import httpx

_TIMEOUT_SEC = 8.0


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


async def push_telegram_alert(
    text: str,
    *,
    target_chat_ids: Iterable[int] | None = None,
    env: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Send ``text`` to authorised alert targets. Returns True if it reached ≥1 chat.

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
                response = await http.post(
                    "/sendMessage",
                    json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                )
                if response.status_code == 200:
                    delivered += 1
            except httpx.HTTPError:
                continue
    finally:
        if owns_client:
            await http.aclose()
    return delivered > 0
