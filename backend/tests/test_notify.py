from __future__ import annotations

import asyncio

import httpx
from jarvis_gpt.notify import push_telegram_alert, telegram_targets
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.telegram_bridge import TelegramConversationStore


def test_single_allowed_chat_is_default_alert_target() -> None:
    token, targets = telegram_targets(
        {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_CHAT_IDS": "42"}
    )

    assert token == "token"
    assert targets == (42,)


def test_multiple_allowed_chats_do_not_implicitly_receive_private_alerts() -> None:
    _, targets = telegram_targets(
        {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_ALLOWED_CHAT_IDS": "42, 99"}
    )

    assert targets == ()


def test_alert_targets_are_deduplicated_and_intersected_with_allowlist() -> None:
    _, targets = telegram_targets(
        {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_CHAT_IDS": "42, 99",
            "TELEGRAM_ALERT_CHAT_IDS": "99, 123, 99",
        }
    )

    assert targets == (99,)


def test_explicit_target_cannot_escape_allowlist() -> None:
    _, targets = telegram_targets(
        {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_CHAT_IDS": "42, 99",
            "TELEGRAM_ALERT_CHAT_IDS": "42",
        },
        requested_chat_ids=(99, 123),
    )

    assert targets == (99,)


def test_successful_alert_is_appended_to_shared_telegram_journal(tmp_path) -> None:
    database_path = tmp_path / "jarvis.sqlite3"
    storage = JarvisStorage(database_path)
    storage.initialize()
    TelegramConversationStore(database_path, realm_id="telegram:700001")

    async def scenario() -> bool:
        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "message_id": 77,
                        "date": 1_784_714_402,
                        "from": {"id": 700001, "is_bot": True},
                    },
                },
            )

        async with httpx.AsyncClient(
            base_url="https://api.telegram.org/bot700001:test-token",
            transport=httpx.MockTransport(handler),
        ) as client:
            return await push_telegram_alert(
                "Проактивное уведомление",
                env={
                    "TELEGRAM_BOT_TOKEN": "700001:test-token",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "42",
                },
                client=client,
                storage=storage,
            )

    assert asyncio.run(scenario()) is True
    with storage.locked_connection() as conn:
        row = conn.execute(
            """
            SELECT chat_id, direction, sender_kind, source_key, content,
                   json_extract(metadata, '$.source') AS source
            FROM telegram_message_log
            """
        ).fetchone()
    assert tuple(row) == (
        42,
        "outbound",
        "bot",
        "out:42:77",
        "Проактивное уведомление",
        "notify",
    )
