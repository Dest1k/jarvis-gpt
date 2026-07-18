"""Telegram bot frontend bridge — allowlist security + relay to the backend agent."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from jarvis_gpt.telegram_bridge import TelegramBridge, TelegramConfig, _chunks, load_config


def _cfg(**over) -> TelegramConfig:
    base = {
        "bot_token": "T",
        "allowed_chat_ids": frozenset({42}),
        "backend_url": "http://backend.test",
    }
    base.update(over)
    return TelegramConfig(**base)


def _bridge(tg_handler, api_handler, **cfg_over):
    tg = httpx.AsyncClient(
        base_url="https://api.telegram.org/botT",
        transport=httpx.MockTransport(tg_handler),
    )
    api = httpx.AsyncClient(
        base_url="http://backend.test",
        transport=httpx.MockTransport(api_handler),
    )
    return TelegramBridge(_cfg(**cfg_over), tg_client=tg, api_client=api)


def test_load_config_fails_closed_without_token():
    with pytest.raises(SystemExit):
        load_config({"TELEGRAM_ALLOWED_CHAT_IDS": "42"})


def test_load_config_fails_closed_without_allowlist():
    with pytest.raises(SystemExit):
        load_config({"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_ALLOWED_CHAT_IDS": "  "})


def test_load_config_parses_allowlist():
    cfg = load_config({"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_ALLOWED_CHAT_IDS": "42, 7 99"})
    assert cfg.allowed_chat_ids == frozenset({42, 7, 99})


def test_chunks_splits_long_text():
    pieces = _chunks("a" * 9000)
    assert all(len(p) <= 4096 for p in pieces)
    assert "".join(pieces) == "a" * 9000
    assert len(pieces) >= 3


def test_denied_chat_never_reaches_the_agent():
    api_calls: list[str] = []

    def tg_handler(request):
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        api_calls.append(str(request.url))
        return httpx.Response(200, json={})

    bridge = _bridge(tg_handler, api_handler)
    update = {"update_id": 1, "message": {"chat": {"id": 999, "type": "private"}, "text": "hi"}}
    asyncio.run(bridge._handle(update))
    assert api_calls == []  # the backend agent is never called for a non-allowlisted chat


def test_group_chat_denied_even_if_id_allowed():
    api_calls: list[str] = []

    def api_handler(request):
        api_calls.append(str(request.url))
        return httpx.Response(200, json={})

    bridge = _bridge(lambda r: httpx.Response(200, json={"ok": True, "result": {}}), api_handler)
    update = {"update_id": 1, "message": {"chat": {"id": 42, "type": "group"}, "text": "hi"}}
    asyncio.run(bridge._handle(update))
    assert api_calls == []


def test_text_turn_relays_to_backend_and_replies():
    sent: list[dict] = []
    chat_bodies: list[dict] = []

    def tg_handler(request):
        payload = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/sendMessage"):
            sent.append(payload)
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/chat":
            chat_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "conversation_id": "c1",
                    "message_id": "m1",
                    "answer": "Привет!",
                    "events": [],
                },
            )
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    bridge = _bridge(tg_handler, api_handler)
    msg = {"chat": {"id": 42, "type": "private"}, "text": "здравствуй"}
    asyncio.run(bridge._handle({"update_id": 1, "message": msg}))
    assert len(chat_bodies) == 1
    assert chat_bodies[0]["message"] == "здравствуй"
    assert "conversation_id" not in chat_bodies[0]  # first turn has no prior conversation
    assert any(m.get("text") == "Привет!" for m in sent)
    # conversation id is remembered for the next turn
    assert bridge.conversations[42] == "c1"


def test_reset_command_drops_conversation_without_calling_agent():
    api_calls: list[str] = []

    def api_handler(request):
        api_calls.append(request.url.path)
        return httpx.Response(200, json={})

    bridge = _bridge(lambda r: httpx.Response(200, json={"ok": True, "result": {}}), api_handler)
    bridge.conversations[42] = "old"
    update = {"update_id": 1, "message": {"chat": {"id": 42, "type": "private"}, "text": "/new"}}
    asyncio.run(bridge._handle(update))
    assert 42 not in bridge.conversations
    assert "/api/chat" not in api_calls


def test_inbound_photo_is_uploaded_and_attached():
    uploads: list[bytes] = []
    chat_bodies: list[dict] = []

    def tg_handler(request):
        path = request.url.path
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/x.jpg"}})
        if "/file/botT/" in path:
            return httpx.Response(200, content=b"\x89PNGdata")
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/files/upload":
            uploads.append(request.content)
            return httpx.Response(
                200,
                json={
                    "file": {"id": "f1", "name": "photo.jpg", "mime_type": "image/jpeg", "size": 8},
                    "chunks_indexed": 0,
                },
            )
        if request.url.path == "/api/chat":
            chat_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "conversation_id": "c1",
                    "message_id": "m",
                    "answer": "вижу",
                    "events": [],
                },
            )
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    bridge = _bridge(tg_handler, api_handler)
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "caption": "что это?",
            "photo": [{"file_id": "p1", "width": 90, "file_size": 8}],
        },
    }
    asyncio.run(bridge._handle(update))
    assert uploads  # the photo bytes were relayed to /api/files/upload
    assert chat_bodies[0]["attachments"] == [
        {"id": "f1", "name": "photo.jpg", "mime_type": "image/jpeg", "size": 8}
    ]
