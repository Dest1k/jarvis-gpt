"""Telegram bot frontend bridge — allowlist security + relay to the backend agent."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3

import httpx
import pytest
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.telegram_bridge import (
    TelegramBridge,
    TelegramConfig,
    TelegramConversationMigrationError,
    TelegramConversationStore,
    _chunks,
    _configure_logging,
    _looks_like_audio,
    load_config,
)


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
    cfg = load_config(
        {
            "TELEGRAM_BOT_TOKEN": "T",
            "TELEGRAM_ALLOWED_CHAT_IDS": "42, 7 99",
            "TELEGRAM_OWNER_CHAT_IDS": "42",
        }
    )
    assert cfg.allowed_chat_ids == frozenset({42, 7, 99})
    assert cfg.owner_chat_ids == frozenset({42})
    assert cfg.conversation_store_path.name == "jarvis.sqlite3"
    assert cfg.legacy_conversation_store_path.name == "telegram_bridge.sqlite3"


def test_load_config_treats_old_store_override_as_migration_source(tmp_path):
    legacy_path = tmp_path / "custom-telegram.sqlite3"
    cfg = load_config(
        {
            "TELEGRAM_BOT_TOKEN": "T",
            "TELEGRAM_ALLOWED_CHAT_IDS": "42",
            "TELEGRAM_CONVERSATION_STORE_PATH": str(legacy_path),
        }
    )

    assert cfg.conversation_store_path.name == "jarvis.sqlite3"
    assert cfg.conversation_store_path != legacy_path
    assert cfg.legacy_conversation_store_path == legacy_path


def test_load_config_requires_explicit_owner_for_multiple_users():
    with pytest.raises(SystemExit, match="TELEGRAM_OWNER_CHAT_IDS"):
        load_config({"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_ALLOWED_CHAT_IDS": "42, 99"})


def test_logging_never_exposes_bot_token(monkeypatch):
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_secret"
    telegram_url = f"https://api.telegram.org/bot{token}/getMe"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    httpx_log = logging.getLogger("httpx")

    monkeypatch.setattr(root, "handlers", [handler])
    monkeypatch.setattr(root, "level", logging.WARNING)
    monkeypatch.setattr(httpx_log, "level", logging.NOTSET)
    _configure_logging(token)

    # httpx normally logs the full credential-bearing request URL at INFO.
    httpx_log.info('HTTP Request: GET %s "HTTP/1.1 200 OK"', telegram_url)
    try:
        response = httpx.Response(401, request=httpx.Request("GET", telegram_url))
        response.raise_for_status()
    except httpx.HTTPStatusError:
        root.exception("Telegram request failed")

    output = stream.getvalue()
    assert "HTTP/1.1 200 OK" not in output
    assert token not in output
    assert "bot[REDACTED]/getMe" in output


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
    assert chat_bodies[0]["access_mode"] == "owner"
    assert chat_bodies[0]["notification_chat_id"] == 42
    # The bridge allocates the id before the backend call, closing the crash window where
    # a completed first turn could be orphaned before its returned id was remembered.
    assert chat_bodies[0]["conversation_id"].startswith("tg_")
    assert any(m.get("text") == "Привет!" for m in sent)
    # A backend-normalized id is remembered for the next turn.
    assert bridge.conversations[42] == "c1"


def test_non_owner_allowed_chat_uses_isolated_guest_surface():
    chat_bodies: list[dict] = []
    api_calls: list[str] = []

    def api_handler(request):
        api_calls.append(request.url.path)
        if request.url.path == "/api/chat":
            chat_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "conversation_id": "guest-c1",
                    "message_id": "m1",
                    "answer": "гостевой ответ",
                    "events": [],
                },
            )
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    bridge = _bridge(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}}),
        api_handler,
        allowed_chat_ids=frozenset({42, 99}),
        owner_chat_ids=frozenset({42}),
    )
    update = {
        "update_id": 2,
        "message": {"chat": {"id": 99, "type": "private"}, "text": "привет"},
    }

    asyncio.run(bridge._handle(update))

    assert len(chat_bodies) == 1
    assert chat_bodies[0]["message"] == "привет"
    assert chat_bodies[0]["access_mode"] == "guest"
    assert chat_bodies[0]["notification_chat_id"] is None
    assert chat_bodies[0]["conversation_id"].startswith("tg_")
    assert api_calls == ["/api/chat"]


def test_guest_attachment_never_reaches_file_or_agent_api():
    api_calls: list[str] = []

    def api_handler(request):
        api_calls.append(request.url.path)
        return httpx.Response(500)

    bridge = _bridge(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}}),
        api_handler,
        allowed_chat_ids=frozenset({42, 99}),
        owner_chat_ids=frozenset({42}),
    )
    update = {
        "update_id": 3,
        "message": {
            "chat": {"id": 99, "type": "private"},
            "caption": "посмотри",
            "photo": [{"file_id": "p1", "width": 100}],
        },
    }

    asyncio.run(bridge._handle(update))

    assert api_calls == []


def test_reset_command_rotates_conversation_without_calling_agent():
    api_calls: list[str] = []

    def api_handler(request):
        api_calls.append(request.url.path)
        return httpx.Response(200, json={})

    bridge = _bridge(lambda r: httpx.Response(200, json={"ok": True, "result": {}}), api_handler)
    bridge.conversations[42] = "old"
    update = {"update_id": 1, "message": {"chat": {"id": 42, "type": "private"}, "text": "/new"}}
    asyncio.run(bridge._handle(update))
    assert bridge.conversations[42].startswith("tg_")
    assert bridge.conversations[42] != "old"
    assert "/api/chat" not in api_calls


def test_start_command_preserves_existing_conversation():
    api_calls: list[str] = []

    def api_handler(request):
        api_calls.append(request.url.path)
        return httpx.Response(200, json={})

    bridge = _bridge(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}}),
        api_handler,
    )
    bridge.conversations[42] = "existing"
    update = {
        "update_id": 1,
        "message": {"chat": {"id": 42, "type": "private"}, "text": "/start"},
    }

    asyncio.run(bridge._handle(update))

    assert bridge.conversations[42] == "existing"
    assert "/api/chat" not in api_calls


def test_conversation_ids_survive_restart_and_stay_isolated_per_chat(tmp_path):
    chat_bodies: list[dict] = []

    def tg_handler(_request):
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/chat":
            payload = json.loads(request.content)
            chat_bodies.append(payload)
            return httpx.Response(
                200,
                json={
                    "conversation_id": payload["conversation_id"],
                    "message_id": "m",
                    "answer": "ok",
                    "events": [],
                },
            )
        return httpx.Response(404)

    cfg = {
        "allowed_chat_ids": frozenset({42, 99}),
        "owner_chat_ids": frozenset({42}),
        "conversation_store_path": tmp_path / "telegram.sqlite3",
    }
    owner_update = {
        "update_id": 1,
        "message": {"chat": {"id": 42, "type": "private"}, "text": "owner-1"},
    }
    guest_update = {
        "update_id": 2,
        "message": {"chat": {"id": 99, "type": "private"}, "text": "guest-1"},
    }

    first = _bridge(tg_handler, api_handler, **cfg)
    asyncio.run(first._handle(owner_update))
    asyncio.run(first._handle(guest_update))
    owner_id, guest_id = (body["conversation_id"] for body in chat_bodies)
    assert owner_id != guest_id
    asyncio.run(first.aclose())

    second = _bridge(tg_handler, api_handler, **cfg)
    owner_update["message"]["text"] = "owner-2"
    guest_update["message"]["text"] = "guest-2"
    asyncio.run(second._handle(owner_update))
    asyncio.run(second._handle(guest_update))
    asyncio.run(second.aclose())

    assert chat_bodies[2]["conversation_id"] == owner_id
    assert chat_bodies[2]["access_mode"] == "owner"
    assert chat_bodies[3]["conversation_id"] == guest_id
    assert chat_bodies[3]["access_mode"] == "guest"


def test_reset_rotation_is_persisted_before_the_next_turn(tmp_path):
    chat_bodies: list[dict] = []

    def tg_handler(_request):
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/chat":
            payload = json.loads(request.content)
            chat_bodies.append(payload)
            return httpx.Response(
                200,
                json={
                    "conversation_id": payload["conversation_id"],
                    "message_id": "m",
                    "answer": "ok",
                    "events": [],
                },
            )
        return httpx.Response(404)

    state_path = tmp_path / "telegram.sqlite3"
    first = _bridge(
        tg_handler,
        api_handler,
        conversation_store_path=state_path,
    )
    turn = {
        "update_id": 1,
        "message": {"chat": {"id": 42, "type": "private"}, "text": "before"},
    }
    asyncio.run(first._handle(turn))
    previous_id = chat_bodies[-1]["conversation_id"]
    turn["message"]["text"] = "/reset"
    asyncio.run(first._handle(turn))
    reset_id = first.conversations[42]
    assert reset_id != previous_id
    asyncio.run(first.aclose())

    second = _bridge(
        tg_handler,
        api_handler,
        conversation_store_path=state_path,
    )
    turn["message"]["text"] = "after"
    asyncio.run(second._handle(turn))
    asyncio.run(second.aclose())

    assert chat_bodies[-1]["conversation_id"] == reset_id


def test_access_mode_change_rotates_persisted_conversation(tmp_path):
    chat_bodies: list[dict] = []

    def tg_handler(_request):
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/chat":
            payload = json.loads(request.content)
            chat_bodies.append(payload)
            return httpx.Response(
                200,
                json={
                    "conversation_id": payload["conversation_id"],
                    "message_id": "m",
                    "answer": "ok",
                    "events": [],
                },
            )
        return httpx.Response(404)

    state_path = tmp_path / "telegram.sqlite3"
    guest = _bridge(
        tg_handler,
        api_handler,
        allowed_chat_ids=frozenset({42, 99}),
        owner_chat_ids=frozenset({42}),
        conversation_store_path=state_path,
    )
    update = {
        "update_id": 1,
        "message": {"chat": {"id": 99, "type": "private"}, "text": "guest"},
    }
    asyncio.run(guest._handle(update))
    guest_id = chat_bodies[-1]["conversation_id"]
    asyncio.run(guest.aclose())

    promoted_owner = _bridge(
        tg_handler,
        api_handler,
        allowed_chat_ids=frozenset({99}),
        owner_chat_ids=frozenset({99}),
        conversation_store_path=state_path,
    )
    update["message"]["text"] = "owner"
    asyncio.run(promoted_owner._handle(update))
    asyncio.run(promoted_owner.aclose())

    assert chat_bodies[-1]["access_mode"] == "owner"
    assert chat_bodies[-1]["conversation_id"] != guest_id


def test_owner_to_guest_change_rotates_once_and_stays_stable(tmp_path):
    state_path = tmp_path / "jarvis.sqlite3"
    store = TelegramConversationStore(state_path)
    store.bind(42, "tg_owner_history", "owner")

    guest_id = store.get_or_create(42, "guest")

    assert guest_id != "tg_owner_history"
    assert TelegramConversationStore(state_path).get_or_create(42, "guest") == guest_id


def test_legacy_binding_store_migrates_into_main_database(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    legacy = TelegramConversationStore(legacy_path)
    legacy.bind(42, "tg_existing_owner", "owner")

    main = TelegramConversationStore(main_path, legacy_path=legacy_path)

    assert main.load_all() == {42: "tg_existing_owner"}
    # Migration is non-destructive and idempotent so rollback remains possible.
    assert legacy.load_all() == {42: "tg_existing_owner"}
    assert TelegramConversationStore(main_path, legacy_path=legacy_path).load_all() == {
        42: "tg_existing_owner"
    }


def test_legacy_migration_preserves_authoritative_main_row_for_same_chat(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    main = TelegramConversationStore(main_path)
    main.bind(42, "tg_main_owner", "owner")
    legacy = TelegramConversationStore(legacy_path)
    legacy.bind(42, "tg_stale_legacy", "guest")

    migrated = TelegramConversationStore(main_path, legacy_path=legacy_path).load_all()

    assert migrated == {42: "tg_main_owner"}
    assert legacy.load_all() == {42: "tg_stale_legacy"}
    assert TelegramConversationStore(main_path, legacy_path=legacy_path).load_all() == migrated


def test_cross_store_conversation_collision_fails_closed_without_mutation(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    main = TelegramConversationStore(main_path)
    main.bind(7, "tg_shared", "guest")
    legacy = TelegramConversationStore(legacy_path)
    legacy.bind(99, "tg_shared", "guest")
    with sqlite3.connect(main_path) as database:
        main_before = "\n".join(database.iterdump())
    with sqlite3.connect(legacy_path) as database:
        legacy_before = "\n".join(database.iterdump())

    with pytest.raises(TelegramConversationMigrationError, match="different chats"):
        TelegramConversationStore(main_path, legacy_path=legacy_path)

    with sqlite3.connect(main_path) as database:
        assert "\n".join(database.iterdump()) == main_before
    with sqlite3.connect(legacy_path) as database:
        assert "\n".join(database.iterdump()) == legacy_before


def test_legacy_duplicate_conversation_ids_fail_closed_before_main_is_created(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(legacy_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        legacy.executemany(
            """
            INSERT INTO telegram_conversations(
                chat_id, conversation_id, access_mode, updated_at
            ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [(7, "tg_legacy_shared", "guest"), (99, "tg_legacy_shared", "guest")],
        )

    with pytest.raises(TelegramConversationMigrationError, match="multiple chats"):
        TelegramConversationStore(main_path, legacy_path=legacy_path)

    assert not main_path.exists()
    with sqlite3.connect(legacy_path) as legacy:
        assert legacy.execute(
            "SELECT chat_id, conversation_id FROM telegram_conversations ORDER BY chat_id"
        ).fetchall() == [(7, "tg_legacy_shared"), (99, "tg_legacy_shared")]


def test_two_column_legacy_store_preserves_owner_history_and_is_idempotent(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(legacy_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL
            )
            """
        )
        legacy.execute(
            "INSERT INTO telegram_conversations(chat_id, conversation_id) VALUES (?, ?)",
            (42, "tg_old_schema"),
        )

    first = TelegramConversationStore(main_path, legacy_path=legacy_path)

    assert first.load_all() == {42: "tg_old_schema"}
    with sqlite3.connect(main_path) as main:
        mode = main.execute(
            "SELECT access_mode FROM telegram_conversations WHERE chat_id = 42"
        ).fetchone()[0]
    assert mode == "owner"
    assert TelegramConversationStore(main_path, legacy_path=legacy_path).load_all() == {
        42: "tg_old_schema"
    }


def test_two_column_main_store_is_upgraded_without_losing_owner_binding(tmp_path):
    main_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(main_path) as main:
        main.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL
            )
            """
        )
        main.execute(
            "INSERT INTO telegram_conversations(chat_id, conversation_id) VALUES (?, ?)",
            (42, "tg_existing_owner"),
        )

    store = TelegramConversationStore(main_path)

    assert store.get_or_create(42, "owner") == "tg_existing_owner"
    with sqlite3.connect(main_path) as main:
        columns = {
            row[1]: row for row in main.execute("PRAGMA table_info(telegram_conversations)")
        }
        row = main.execute(
            """
            SELECT chat_id, conversation_id, access_mode, updated_at
            FROM telegram_conversations
            """
        ).fetchone()
    assert {"chat_id", "conversation_id", "access_mode", "updated_at"} <= columns.keys()
    assert row[:3] == (42, "tg_existing_owner", "owner")
    assert row[3]


def test_two_column_legacy_owner_is_preserved_on_first_owner_turn_but_rotated_for_guest(
    tmp_path,
):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(legacy_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL
            )
            """
        )
        legacy.execute(
            "INSERT INTO telegram_conversations(chat_id, conversation_id) VALUES (?, ?)",
            (42, "tg_owner_before_upgrade"),
        )

    chat_bodies: list[dict] = []

    def tg_handler(_request):
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/chat":
            payload = json.loads(request.content)
            chat_bodies.append(payload)
            return httpx.Response(
                200,
                json={
                    "conversation_id": payload["conversation_id"],
                    "message_id": "m",
                    "answer": "ok",
                    "events": [],
                },
            )
        return httpx.Response(404)

    owner = _bridge(
        tg_handler,
        api_handler,
        owner_chat_ids=frozenset({42}),
        conversation_store_path=main_path,
        legacy_conversation_store_path=legacy_path,
    )
    update = {
        "update_id": 1,
        "message": {"chat": {"id": 42, "type": "private"}, "text": "owner turn"},
    }
    asyncio.run(owner._handle(update))
    asyncio.run(owner.aclose())

    assert chat_bodies[-1]["access_mode"] == "owner"
    assert chat_bodies[-1]["conversation_id"] == "tg_owner_before_upgrade"

    guest = _bridge(
        tg_handler,
        api_handler,
        allowed_chat_ids=frozenset({42, 99}),
        owner_chat_ids=frozenset({99}),
        conversation_store_path=main_path,
        legacy_conversation_store_path=legacy_path,
    )
    update["message"]["text"] = "guest turn"
    asyncio.run(guest._handle(update))
    asyncio.run(guest.aclose())

    assert chat_bodies[-1]["access_mode"] == "guest"
    assert chat_bodies[-1]["conversation_id"] != "tg_owner_before_upgrade"


def test_invalid_legacy_access_mode_fails_closed_without_main_mutation(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    main = TelegramConversationStore(main_path)
    main.bind(42, "tg_main_owner", "owner")
    with sqlite3.connect(main_path) as database:
        main_before = "\n".join(database.iterdump())
    with sqlite3.connect(legacy_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO telegram_conversations(
                chat_id, conversation_id, access_mode, updated_at
            ) VALUES (99, 'tg_invalid_mode', 'administrator', CURRENT_TIMESTAMP)
            """
        )

    with pytest.raises(TelegramConversationMigrationError, match="invalid access_mode"):
        TelegramConversationStore(main_path, legacy_path=legacy_path)

    with sqlite3.connect(main_path) as database:
        assert "\n".join(database.iterdump()) == main_before
    with sqlite3.connect(legacy_path) as legacy:
        assert legacy.execute(
            "SELECT access_mode FROM telegram_conversations WHERE chat_id = 99"
        ).fetchone() == ("administrator",)


def test_invalid_main_access_mode_fails_closed_without_guest_fallback(tmp_path):
    main_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(main_path) as main:
        main.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL UNIQUE,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        main.execute(
            """
            INSERT INTO telegram_conversations(
                chat_id, conversation_id, access_mode, updated_at
            ) VALUES (42, 'tg_bad_main', 'OWNER', CURRENT_TIMESTAMP)
            """
        )
    with sqlite3.connect(main_path) as database:
        before = "\n".join(database.iterdump())

    with pytest.raises(TelegramConversationMigrationError, match="invalid access_mode"):
        TelegramConversationStore(main_path)

    with sqlite3.connect(main_path) as database:
        assert "\n".join(database.iterdump()) == before


def test_schema_upgrade_rolls_back_when_legacy_collision_is_detected(tmp_path):
    main_path = tmp_path / "jarvis.sqlite3"
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    with sqlite3.connect(main_path) as main:
        main.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL
            )
            """
        )
        main.execute(
            "INSERT INTO telegram_conversations VALUES (7, 'tg_collision')"
        )
    legacy = TelegramConversationStore(legacy_path)
    legacy.bind(99, "tg_collision", "owner")
    with sqlite3.connect(main_path) as main:
        before = "\n".join(main.iterdump())
        journal_mode_before = main.execute("PRAGMA journal_mode").fetchone()[0]

    with pytest.raises(TelegramConversationMigrationError, match="different chats"):
        TelegramConversationStore(main_path, legacy_path=legacy_path)

    with sqlite3.connect(main_path) as main:
        assert "\n".join(main.iterdump()) == before
        assert main.execute("PRAGMA journal_mode").fetchone()[0] == journal_mode_before
        assert [row[1] for row in main.execute("PRAGMA table_info(telegram_conversations)")] == [
            "chat_id",
            "conversation_id",
        ]


def test_unreadable_legacy_schema_fails_closed(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    with sqlite3.connect(legacy_path) as legacy:
        legacy.execute("CREATE TABLE unrelated(value TEXT)")

    with pytest.raises(TelegramConversationMigrationError, match="compatible binding table"):
        TelegramConversationStore(tmp_path / "jarvis.sqlite3", legacy_path=legacy_path)


def test_runtime_database_backup_contains_telegram_bindings(tmp_path):
    database_path = tmp_path / "state" / "jarvis.sqlite3"
    storage = JarvisStorage(database_path)
    storage.initialize()
    try:
        conversations = TelegramConversationStore(database_path)
        conversations.bind(42, "tg_backup_owner", "owner")
        result = storage.backup_database(tmp_path / "backups")
    finally:
        storage.close()

    with sqlite3.connect(result["path"]) as backup:
        row = backup.execute(
            """
            SELECT chat_id, conversation_id, access_mode
            FROM telegram_conversations
            """
        ).fetchone()
    assert row == (42, "tg_backup_owner", "owner")


@pytest.mark.parametrize(
    ("command", "rotates"),
    [
        ("/new@JarvisBot", True),
        ("/NEW@jarvisbot ignored-payload", True),
        ("/start payload", False),
        ("/START@JarvisBot payload", False),
    ],
)
def test_telegram_command_suffix_and_payload_are_normalized(command, rotates):
    api_calls: list[str] = []

    def api_handler(request):
        api_calls.append(request.url.path)
        return httpx.Response(200, json={})

    bridge = _bridge(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}}),
        api_handler,
    )
    bridge.conversations[42] = "existing"
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "text": command,
        },
    }

    asyncio.run(bridge._handle(update))

    if rotates:
        assert bridge.conversations[42].startswith("tg_")
        assert bridge.conversations[42] != "existing"
    else:
        assert bridge.conversations[42] == "existing"
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


def test_looks_like_audio_detection():
    assert _looks_like_audio({"mime_type": "audio/ogg", "name": "voice.ogg"})
    assert _looks_like_audio({"mime_type": None, "name": "clip.mp3"})
    assert not _looks_like_audio({"mime_type": "image/jpeg", "name": "photo.jpg"})
    assert not _looks_like_audio({"mime_type": "text/plain", "name": "notes.txt"})


def test_load_config_voice_replies_toggle():
    common = {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_ALLOWED_CHAT_IDS": "42"}
    assert load_config(common).voice_replies is True
    assert load_config({**common, "TELEGRAM_VOICE_REPLIES": "0"}).voice_replies is False


def _voice_bridge(monkeypatch, *, ogg: bytes | None):
    monkeypatch.setattr("jarvis_gpt.telegram_bridge._wav_to_ogg_opus", lambda wav: ogg)
    tg_posts: list[str] = []
    chat_bodies: list[dict] = []

    def tg_handler(request):
        path = request.url.path
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/x.ogg"}})
        if "/file/botT/" in path:
            return httpx.Response(200, content=b"OggS-voice-bytes")
        tg_posts.append(path)
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        path = request.url.path
        if path == "/api/files/upload":
            return httpx.Response(
                200,
                json={
                    "file": {"id": "v1", "name": "voice.ogg", "mime_type": "audio/ogg", "size": 16},
                    "chunks_indexed": 0,
                },
            )
        if path == "/api/chat":
            chat_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "conversation_id": "c1",
                    "message_id": "m",
                    "answer": "Готово, сэр.",
                    "events": [],
                },
            )
        if path == "/api/voice/speak":
            return httpx.Response(200, content=b"RIFFwav-bytes")
        if path == "/api/files":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    return _bridge(tg_handler, api_handler), tg_posts, chat_bodies


def test_inbound_voice_transcribed_and_answered_with_voice(monkeypatch):
    bridge, tg_posts, chat_bodies = _voice_bridge(monkeypatch, ogg=b"OggS-opus")
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "vf", "mime_type": "audio/ogg", "duration": 3},
        },
    }
    asyncio.run(bridge._handle(update))
    # A voice-only note is relayed as an attachment; the message is a space so the backend
    # folds the transcript in as the real query.
    assert chat_bodies[0]["message"] == " "
    assert chat_bodies[0]["attachments"][0]["id"] == "v1"
    # Spoken input -> a synthesized voice note reply (inline OGG/Opus).
    assert any(p.endswith("/sendVoice") for p in tg_posts)


def test_voice_reply_falls_back_to_audio_when_no_opus(monkeypatch):
    bridge, tg_posts, _ = _voice_bridge(monkeypatch, ogg=None)
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "voice": {"file_id": "vf", "mime_type": "audio/ogg"},
        },
    }
    asyncio.run(bridge._handle(update))
    assert any(p.endswith("/sendAudio") for p in tg_posts)
    assert not any(p.endswith("/sendVoice") for p in tg_posts)


def test_text_input_never_triggers_a_voice_reply():
    speak_calls: list[str] = []

    def tg_handler(request):
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/voice/speak":
            speak_calls.append("speak")
            return httpx.Response(200, content=b"wav")
        if request.url.path == "/api/chat":
            return httpx.Response(
                200,
                json={
                    "conversation_id": "c1",
                    "message_id": "m",
                    "answer": "просто текст",
                    "events": [],
                },
            )
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    bridge = _bridge(tg_handler, api_handler)
    update = {"update_id": 1, "message": {"chat": {"id": 42, "type": "private"}, "text": "привет"}}
    asyncio.run(bridge._handle(update))
    assert speak_calls == []  # text in -> text out, never voice
