"""Out-of-band operator notifications (push to the owner's phone).

Deliberately tiny and dependency-free beyond ``httpx`` (already a core dep). The
supervisor uses this to push proactive health alerts to Telegram without pulling in
the whole long-poll bridge: it reuses the exact same ``TELEGRAM_BOT_TOKEN`` /
``TELEGRAM_ALLOWED_CHAT_IDS`` credentials, so once the owner has wired the bot the
alerts flow to the same chat with no extra configuration.

Fails closed and fails quiet: when the token/allowlist are unset it returns ``False``
without raising, so a box that never configured Telegram simply keeps alerting through
the runtime event log + UI bus and skips the phone push.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

import httpx

_TIMEOUT_SEC = 8.0


def telegram_targets(env: Mapping[str, str] | None = None) -> tuple[str, tuple[int, ...]]:
    """Resolve ``(bot_token, chat_ids)`` from the environment.

    Mirrors ``telegram_bridge`` credential parsing so the owner configures Telegram
    once. Returns an empty token / empty tuple when unconfigured — the caller treats
    that as "no phone push wired" rather than an error.
    """

    source = os.environ if env is None else env
    token = (source.get("TELEGRAM_BOT_TOKEN") or "").strip()
    ids: list[int] = []
    for part in re.split(r"[,\s]+", (source.get("TELEGRAM_ALLOWED_CHAT_IDS") or "").strip()):
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return token, tuple(dict.fromkeys(ids))


async def push_telegram_alert(
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Send ``text`` to every allow-listed chat. Returns True if it reached ≥1 chat.

    Never raises: unconfigured credentials or any transport error resolve to ``False``
    so a health-alert push can never take down the supervisor loop that called it.
    """

    token, chat_ids = telegram_targets(env)
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
