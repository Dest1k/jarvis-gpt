"""Security boundary tests for Telegram identity binding and scoped backend sessions."""

from __future__ import annotations

import asyncio
import json

import httpx
from jarvis_gpt.telegram_bridge import TelegramBridge, TelegramConfig


def _bridge(api_handler, *, allowed_chat_ids: frozenset[int] = frozenset()):
    tg = httpx.AsyncClient(
        base_url="https://api.telegram.org/botT",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"ok": True, "result": {}})
        ),
    )
    api = httpx.AsyncClient(
        base_url="http://backend.test",
        transport=httpx.MockTransport(api_handler),
    )
    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="T",
            allowed_chat_ids=allowed_chat_ids,
            backend_url="http://backend.test",
            bridge_secret="bridge-secret",
            realm_id="telegram:700001",
            bot_id=700001,
        ),
        tg_client=tg,
        api_client=api,
    )
    bridge._initialize_bot_identity({"id": 700001})
    return bridge


def _update(user_id: int = 77, *, update_id: int = 1, text: str = "привет") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": user_id, "type": "private"},
            "from": {
                "id": user_id,
                "is_bot": False,
                "username": "alice",
                "first_name": "Alice",
                "language_code": "ru",
            },
            "text": text,
        },
    }


def test_ambiguous_senders_are_rejected_before_backend_identity_call():
    api_calls: list[str] = []
    bridge = _bridge(
        lambda request: (
            api_calls.append(request.url.path),
            httpx.Response(500),
        )[1]
    )
    cases = []
    missing_from = _update(update_id=1)
    missing_from["message"].pop("from")
    cases.append(missing_from)
    bot_sender = _update(update_id=2)
    bot_sender["message"]["from"]["is_bot"] = True
    cases.append(bot_sender)
    mismatched_sender = _update(update_id=3)
    mismatched_sender["message"]["from"]["id"] = 78
    cases.append(mismatched_sender)
    anonymous_sender = _update(update_id=4)
    anonymous_sender["message"]["sender_chat"] = {"id": -1001, "type": "channel"}
    cases.append(anonymous_sender)
    invalid_user_id = _update(update_id=5)
    invalid_user_id["message"]["chat"]["id"] = -77
    invalid_user_id["message"]["from"]["id"] = -77
    cases.append(invalid_user_id)

    async def run_cases():
        for item in cases:
            await bridge._handle(item)
        await bridge.aclose()

    asyncio.run(run_cases())
    assert api_calls == []


def test_empty_allowlist_auto_registers_and_scopes_every_user_backend_call():
    registration: list[dict] = []
    user_calls: list[tuple[str, str | None]] = []
    chat_payloads: list[dict] = []

    def api_handler(request: httpx.Request):
        if request.url.path == "/api/integrations/telegram/session":
            assert request.headers.get("x-jarvis-bridge-secret") == "bridge-secret"
            registration.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "realm_id": "telegram:700001",
                    "bot_id": 700001,
                    "session_token": "short-lived-session",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "user": {"id": "user-77", "preset_key": "user", "created": True},
                },
            )
        user_calls.append(
            (request.url.path, request.headers.get("x-jarvis-user-session"))
        )
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/preferences":
            return httpx.Response(200, json={"voice_reply": False})
        if request.url.path == "/api/chat":
            chat_payloads.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "conversation_id": "conversation-77",
                    "message_id": "message-1",
                    "answer": "готово",
                    "events": [],
                },
            )
        return httpx.Response(404)

    bridge = _bridge(api_handler)
    asyncio.run(bridge._handle(_update()))

    assert registration == [
        {
            "realm_id": "telegram:700001",
            "bot_id": 700001,
            "update_id": 1,
            "telegram_user": {
                "id": 77,
                "username": "alice",
                "first_name": "Alice",
                "last_name": None,
                "language_code": "ru",
                "is_premium": False,
            },
            "chat": {"id": 77, "type": "private"},
        }
    ]
    assert user_calls == [
        ("/api/preferences", "short-lived-session"),
        ("/api/files", "short-lived-session"),
        ("/api/chat", "short-lived-session"),
        ("/api/files", "short-lived-session"),
    ]
    assert chat_payloads[0]["message"] == "привет"
    assert "access_mode" not in chat_payloads[0]
    # Telegram-first delivery stamps the chat so reminders/progress fire back here.
    assert chat_payloads[0].get("notification_chat_id") == 77


def test_optional_allowlist_remains_a_restriction_not_a_registration_requirement():
    api_calls: list[str] = []
    bridge = _bridge(
        lambda request: (
            api_calls.append(request.url.path),
            httpx.Response(500),
        )[1],
        allowed_chat_ids=frozenset({42}),
    )

    asyncio.run(bridge._handle(_update(user_id=77)))
    assert api_calls == []


def test_backend_replay_conflict_is_not_forwarded_to_agent():
    paths: list[str] = []

    def api_handler(request: httpx.Request):
        paths.append(request.url.path)
        return httpx.Response(409, json={"detail": "Telegram update replay mismatch"})

    bridge = _bridge(api_handler)
    asyncio.run(bridge._handle(_update()))

    assert paths == ["/api/integrations/telegram/session"]


def test_bridge_offers_existing_scoped_session_for_backend_reuse():
    offered_sessions: list[str | None] = []

    def api_handler(request: httpx.Request):
        if request.url.path == "/api/integrations/telegram/session":
            offered_sessions.append(request.headers.get("x-jarvis-user-session"))
            return httpx.Response(
                200,
                json={
                    "realm_id": "telegram:700001",
                    "bot_id": 700001,
                    "session_token": "reusable-session",
                    "session_id": "session-1",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "user": {"id": "user-77", "preset_key": "user"},
                },
            )
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/chat":
            return httpx.Response(
                200,
                json={
                    "conversation_id": "conversation-77",
                    "message_id": "message-1",
                    "answer": "готово",
                    "events": [],
                },
            )
        return httpx.Response(404)

    bridge = _bridge(api_handler)

    async def run_updates():
        await bridge._handle(_update(update_id=1))
        await bridge._handle(_update(update_id=2))
        await bridge.aclose()

    asyncio.run(run_updates())
    assert offered_sessions == [None, "reusable-session"]
