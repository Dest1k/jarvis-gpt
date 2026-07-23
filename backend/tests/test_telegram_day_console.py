"""Telegram day-console batch: forward-as-task, quiet hours, keyboards, cards."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from datetime import time as dtime

import httpx
from jarvis_gpt.notify import (
    answer_action_keyboard,
    in_quiet_hours,
    parse_quiet_hours,
    progress_stop_keyboard,
    remove_reply_keyboard,
)
from jarvis_gpt.telegram_bridge import (
    _build_forward_task_prompt,
    _console_action_for_text,
    _format_briefing_card,
    _format_status_card,
    _is_forwarded_message,
    _quiet_command_spec,
)

from tests.test_telegram_bridge import _bridge


def test_parse_quiet_hours_and_window():
    assert parse_quiet_hours("") is None
    assert parse_quiet_hours("23:00-08:00") == (dtime(23, 0), dtime(8, 0))
    assert parse_quiet_hours("22-7") == (dtime(22, 0), dtime(7, 0))
    tz = timezone(timedelta(hours=3), name="Europe/Moscow")
    night = datetime(2026, 7, 19, 23, 30, tzinfo=tz)
    morning = datetime(2026, 7, 20, 7, 0, tzinfo=tz)
    day = datetime(2026, 7, 20, 12, 0, tzinfo=tz)
    assert in_quiet_hours("23:00-08:00", now=night, tz_name="Europe/Moscow") is True
    assert in_quiet_hours("23:00-08:00", now=morning, tz_name="Europe/Moscow") is True
    assert in_quiet_hours("23:00-08:00", now=day, tz_name="Europe/Moscow") is False


def test_forward_detection_and_prompt():
    msg = {
        "forward_date": 1710000000,
        "forward_from": {"id": 1, "first_name": "Ann", "username": "ann"},
        "text": "https://example.com/article look at this",
        "entities": [
            {"type": "url", "offset": 0, "length": len("https://example.com/article")},
        ],
    }
    assert _is_forwarded_message(msg) is True
    assert _is_forwarded_message({"text": "hi"}) is False
    prompt = _build_forward_task_prompt(msg, msg["text"])
    assert "forward-as-task" in prompt
    assert "Ann" in prompt or "@ann" in prompt
    assert "https://example.com/article" in prompt
    assert "Сделай полезное" in prompt


def test_forward_origin_channel():
    msg = {
        "forward_origin": {
            "type": "channel",
            "chat": {"title": "AI News", "username": "ainews"},
        },
        "text": "headline",
    }
    assert _is_forwarded_message(msg) is True
    assert "AI News" in _build_forward_task_prompt(msg, "headline")


def test_console_action_mapping():
    assert _console_action_for_text("📋 Сводка") == "briefing"
    assert _console_action_for_text("📊 Статус") == "status"
    assert _console_action_for_text("/status") == "status"
    assert _console_action_for_text("/help") == "help"
    assert _console_action_for_text("🛑 Стоп") == "stop"
    assert _console_action_for_text("📋") == "briefing"
    assert _console_action_for_text("📌") == "inbox_list"
    assert _console_action_for_text("🛑") == "stop"
    assert _console_action_for_text("⏰") == "quiet_help"
    assert _console_action_for_text("/quiet") == "quiet"
    assert _console_action_for_text("привет") is None


def test_keyboard_shapes():
    assert remove_reply_keyboard() == {"remove_keyboard": True}
    assert answer_action_keyboard()["inline_keyboard"][0][0]["callback_data"] == "a:inbox"
    assert progress_stop_keyboard()["inline_keyboard"][0][0]["callback_data"] == "a:stop"


def test_guest_start_removes_operator_console():
    sent: list[dict] = []

    def tg_handler(request):
        if request.url.path.endswith("/sendMessage"):
            sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bridge = _bridge(
        tg_handler,
        lambda _request: httpx.Response(404),
        session_presets={42: "user"},
    )
    update = {
        "update_id": 70,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "/start",
        },
    }

    asyncio.run(bridge._handle(update))

    assert sent[-1]["reply_markup"] == {"remove_keyboard": True}
    assert "Пульт внизу" not in sent[-1]["text"]
    assert "Статус" not in sent[-1]["text"]


def test_admin_start_also_removes_operator_console():
    sent: list[dict] = []

    def tg_handler(request):
        if request.url.path.endswith("/sendMessage"):
            sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    bridge = _bridge(
        tg_handler,
        lambda _request: httpx.Response(404),
        session_presets={42: "admin"},
    )
    update = {
        "update_id": 71,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "/start",
        },
    }

    asyncio.run(bridge._handle(update))

    assert sent[-1]["reply_markup"] == {"remove_keyboard": True}
    assert "Пульт внизу" not in sent[-1]["text"]
    assert "/status" in sent[-1]["text"]


def test_guest_stale_status_button_is_rejected_before_backend():
    api_paths: list[str] = []
    sent: list[dict] = []

    def tg_handler(request):
        if request.url.path.endswith("/sendMessage"):
            sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def api_handler(request):
        api_paths.append(request.url.path)
        return httpx.Response(500)

    bridge = _bridge(
        tg_handler,
        api_handler,
        session_presets={42: "user"},
    )
    update = {
        "update_id": 72,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "📊 Статус",
        },
    }

    asyncio.run(bridge._handle(update))

    assert "/api/status" not in api_paths
    assert sent[-1]["reply_markup"] == {"remove_keyboard": True}
    assert "владельцу или администратору" in sent[-1]["text"]


def test_status_and_briefing_formatters():
    status = _format_status_card(
        {
            "settings": {"profile": {"name": "qwen36-vl"}},
            "counters": {"missions": 2, "memories": 9, "files": 4},
            "health": [{"name": "disk", "status": "warn", "message": "low"}],
        }
    )
    assert "qwen36-vl" in status and "low" in status
    briefing = _format_briefing_card(
        {
            "headline": "Runtime is stable",
            "focus": ["Focus: ship"],
            "suggestions": ["Rest"],
        }
    )
    assert "Runtime is stable" in briefing and "Rest" in briefing


def test_forwarded_message_becomes_task_prompt_in_bridge():
    chat_bodies: list[dict] = []

    def tg_handler(request):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def api_handler(request):
        if request.url.path == "/api/chat":
            chat_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "conversation_id": "c1",
                    "message_id": "m1",
                    "answer": "Суммировал пересланное.",
                    "events": [],
                },
            )
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    bridge = _bridge(tg_handler, api_handler)
    update = {
        "update_id": 77,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "https://example.com/x",
            "forward_date": 1710000000,
            "forward_from": {"id": 9, "first_name": "Bob"},
        },
    }
    asyncio.run(bridge._handle(update))
    assert len(chat_bodies) == 1
    assert "forward-as-task" in chat_bodies[0]["message"]
    assert "Bob" in chat_bodies[0]["message"]
    assert "https://example.com/x" in chat_bodies[0]["message"]


def test_console_status_button_skips_chat():
    api_paths: list[str] = []
    sent: list[str] = []

    def tg_handler(request):
        if request.url.path.endswith("/sendMessage"):
            payload = json.loads(request.content)
            sent.append(payload.get("text") or "")
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        api_paths.append(request.url.path)
        if request.url.path == "/api/status":
            return httpx.Response(
                200,
                json={
                    "settings": {"profile": {"name": "qwen36-vl"}},
                    "counters": {"missions": 1, "memories": 2, "files": 3},
                    "health": [],
                    "recent_events": [],
                    "notices": [],
                    "service_mode": {},
                },
            )
        return httpx.Response(404)

    bridge = _bridge(tg_handler, api_handler)
    update = {
        "update_id": 78,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "📊 Статус",
        },
    }
    asyncio.run(bridge._handle(update))
    assert "/api/chat" not in api_paths
    assert "/api/status" in api_paths
    assert any("qwen36-vl" in text for text in sent)


def test_action_callback_inbox_uses_last_answer():
    memory_bodies: list[dict] = []
    sent: list[str] = []

    def tg_handler(request):
        if request.url.path.endswith("/answerCallbackQuery"):
            return httpx.Response(200, json={"ok": True, "result": True})
        if request.url.path.endswith("/sendMessage"):
            payload = json.loads(request.content)
            sent.append(payload.get("text") or "")
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/memory":
            memory_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "mem_1",
                    "namespace": "inbox",
                    "content": "x",
                    "tags": [],
                    "importance": 0.6,
                    "created_at": "t",
                    "updated_at": "t",
                },
            )
        return httpx.Response(404)

    bridge = _bridge(tg_handler, api_handler)
    bridge._last_answers[42] = "Важный вывод про GPU."
    update = {
        "update_id": 79,
        "callback_query": {
            "id": "cq9",
            "from": {"id": 42, "is_bot": False},
            "data": "a:inbox",
            "message": {"chat": {"id": 42, "type": "private"}, "message_id": 3},
        },
    }
    asyncio.run(bridge._handle(update))
    assert memory_bodies and "GPU" in memory_bodies[0]["content"]
    assert any("inbox" in text.lower() or "📥" in text for text in sent)


def test_quiet_command_spec_parser():
    assert _quiet_command_spec("/quiet") == ""
    assert _quiet_command_spec("/quiet 23:00-08:00") == "23:00-08:00"
    assert _quiet_command_spec("/quiet off") == "clear"
    assert _quiet_command_spec("/quiet clear") == "clear"
    assert _quiet_command_spec("not quiet") is None


def test_quiet_command_patches_preferences():
    patches: list[dict] = []
    sent: list[str] = []

    def tg_handler(request):
        if request.url.path.endswith("/sendMessage"):
            payload = json.loads(request.content)
            sent.append(payload.get("text") or "")
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/preferences" and request.method == "PATCH":
            patches.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "operator_name": "Admin",
                    "communication_style": "concise",
                    "daily_briefing": True,
                    "voice_reply": False,
                    "preferred_profile": "gemma4-turbo",
                    "quiet_hours": "23:00-08:00",
                    "working_roots": [],
                },
            )
        return httpx.Response(404)

    bridge = _bridge(tg_handler, api_handler)
    update = {
        "update_id": 91,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "/quiet 23:00-08:00",
        },
    }
    asyncio.run(bridge._handle(update))
    assert patches == [{"quiet_hours": "23:00-08:00"}]
    assert any("23:00-08:00" in text for text in sent)


def test_answer_does_not_send_action_chips_by_default():
    markups: list[dict] = []

    def tg_handler(request):
        if request.url.path.endswith("/sendMessage"):
            payload = json.loads(request.content)
            if payload.get("reply_markup"):
                markups.append(payload["reply_markup"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 11}})

    def api_handler(request):
        if request.url.path == "/api/chat":
            return httpx.Response(
                200,
                json={
                    "conversation_id": "c1",
                    "message_id": "m1",
                    "answer": "Готово.",
                    "events": [],
                },
            )
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    bridge = _bridge(tg_handler, api_handler)
    update = {
        "update_id": 80,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "привет",
        },
    }
    asyncio.run(bridge._handle(update))
    # Normal answers must not attach Inbox/+1ч/Ещё chips under every message.
    assert not any(
        any(
            btn.get("callback_data") in {"a:inbox", "a:r60", "a:more"}
            for row in m.get("inline_keyboard", [])
            for btn in row
        )
        for m in markups
    )
    assert bridge._last_answers.get(42) == "Готово."
