"""Telegram bot frontend bridge — secure identity sessions + agent relay."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import sqlite3
import time

import httpx
import pytest
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.telegram_bridge import (
    TelegramBridge,
    TelegramConfig,
    TelegramConversationIsolationError,
    TelegramConversationMigrationError,
    TelegramConversationStore,
    _chunks,
    _configure_logging,
    _looks_like_audio,
    _looks_like_image,
    _quick_capture_body,
    _retryable_backend_http_error,
    load_config,
)

BRIDGE_SECRET = "bridge-test-secret-with-at-least-32-chars"


def _cfg(**over) -> TelegramConfig:
    base = {
        "bot_token": "T",
        "allowed_chat_ids": frozenset({42}),
        "backend_url": "http://backend.test",
        "bridge_secret": "bridge-secret",
        "realm_id": "telegram:700001",
        "bot_id": 700001,
    }
    base.update(over)
    return TelegramConfig(**base)


def _bridge(
    tg_handler,
    api_handler,
    *,
    session_presets=None,
    session_payloads: list[dict] | None = None,
    **cfg_over,
):
    presets = {42: "owner", **(session_presets or {})}

    def scoped_api_handler(request):
        if request.url.path == "/api/integrations/telegram/session":
            payload = json.loads(request.content)
            if session_payloads is not None:
                session_payloads.append(payload)
            telegram_id = payload["telegram_user"]["id"]
            invite_claimed = bool(payload.get("owner_invite_proof"))
            return httpx.Response(
                200,
                json={
                    "realm_id": payload["realm_id"],
                    "bot_id": payload["bot_id"],
                    "session_token": f"session-{telegram_id}",
                    "user": {
                        "id": f"user-{telegram_id}",
                        "preset_key": (
                            "owner" if invite_claimed else presets.get(telegram_id, "guest")
                        ),
                        "owner_invite_claimed": invite_claimed,
                    },
                },
            )
        return api_handler(request)

    tg = httpx.AsyncClient(
        base_url="https://api.telegram.org/botT",
        transport=httpx.MockTransport(tg_handler),
    )
    api = httpx.AsyncClient(
        base_url="http://backend.test",
        transport=httpx.MockTransport(scoped_api_handler),
    )
    cfg = _cfg(**cfg_over)
    bridge = TelegramBridge(cfg, tg_client=tg, api_client=api)
    bridge._initialize_bot_identity({"id": cfg.bot_id})
    return bridge


def test_load_config_fails_closed_without_token():
    with pytest.raises(SystemExit):
        load_config({"TELEGRAM_ALLOWED_CHAT_IDS": "42"})


def test_load_config_fails_closed_without_bridge_secret():
    with pytest.raises(SystemExit, match="JARVIS_TELEGRAM_BRIDGE_SECRET"):
        load_config({"TELEGRAM_BOT_TOKEN": "T"})


def test_load_config_allows_empty_optional_allowlist():
    cfg = load_config(
        {
            "TELEGRAM_BOT_TOKEN": "T",
            "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
        }
    )
    assert cfg.allowed_chat_ids == frozenset()


def test_load_config_parses_allowlist():
    cfg = load_config(
        {
            "TELEGRAM_BOT_TOKEN": "T",
            "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
            "JARVIS_TELEGRAM_REALM_ID": "telegram:700001",
            "JARVIS_TELEGRAM_BOT_ID": "700001",
            "TELEGRAM_ALLOWED_CHAT_IDS": "42, 7 99",
            "TELEGRAM_OWNER_CHAT_IDS": "42",
        }
    )
    assert cfg.allowed_chat_ids == frozenset({42, 7, 99})
    assert cfg.owner_chat_ids == frozenset({42})
    assert cfg.conversation_store_path.name == "jarvis.sqlite3"
    assert cfg.legacy_conversation_store_path.name == "telegram_bridge.sqlite3"


def test_load_config_treats_old_store_override_as_migration_source(tmp_path):
    old_store = tmp_path / "old-telegram.sqlite3"
    cfg = load_config(
        {
            "TELEGRAM_BOT_TOKEN": "T",
            "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
            "JARVIS_TELEGRAM_REALM_ID": "telegram:700001",
            "JARVIS_TELEGRAM_BOT_ID": "700001",
            "JARVIS_TELEGRAM_LEGACY_REALM_ID": "telegram:700001",
            "JARVIS_TELEGRAM_LEGACY_SOURCE_REALM_ID": "legacy-bot-realm",
            "TELEGRAM_CONVERSATION_STORE_PATH": str(old_store),
        }
    )

    assert cfg.realm_id == "telegram:700001"
    assert cfg.conversation_store_path.name == "jarvis.sqlite3"
    assert cfg.conversation_store_path.resolve() != old_store.resolve()
    assert cfg.legacy_conversation_store_path.resolve() == old_store.resolve()
    assert cfg.legacy_conversation_realm_id == "telegram:700001"
    assert cfg.legacy_conversation_source_realm_id == "legacy-bot-realm"


def test_load_config_rejects_truncated_realm_collisions():
    with pytest.raises(SystemExit, match="must not exceed 120"):
        load_config(
            {
                "TELEGRAM_BOT_TOKEN": "T",
                "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
                "JARVIS_TELEGRAM_REALM_ID": "r" * 121,
                "JARVIS_TELEGRAM_BOT_ID": "700001",
            }
        )


def test_getme_derives_canonical_realm_before_legacy_store_migration(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(legacy_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL UNIQUE,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        legacy.execute(
            "INSERT INTO telegram_conversations VALUES "
            "(42, 'legacy-getme-history', 'guest', 'now')"
        )
    tg = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    api = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    bridge = TelegramBridge(
        _cfg(
            realm_id="",
            bot_id=0,
            conversation_store_path=main_path,
            legacy_conversation_store_path=legacy_path,
            legacy_conversation_realm_id="telegram:700001",
        ),
        tg_client=tg,
        api_client=api,
    )

    assert not main_path.exists()
    bridge._initialize_bot_identity({"id": 700001})

    assert bridge._bot_identity() == ("telegram:700001", 700001)
    assert bridge._conversation_store is not None
    assert bridge._conversation_store.load_all() == {42: "legacy-getme-history"}
    asyncio.run(bridge.aclose())


def test_getme_mismatch_fails_before_history_database_is_touched(tmp_path):
    main_path = tmp_path / "jarvis.sqlite3"
    tg = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    api = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    bridge = TelegramBridge(
        _cfg(
            realm_id="telegram:700001",
            bot_id=700001,
            conversation_store_path=main_path,
        ),
        tg_client=tg,
        api_client=api,
    )

    with pytest.raises(RuntimeError, match="JARVIS_TELEGRAM_BOT_ID"):
        bridge._initialize_bot_identity({"id": 700002})

    assert not main_path.exists()
    asyncio.run(bridge.aclose())


def test_load_config_bounds_bridge_worker_pool():
    cfg = load_config(
        {
            "TELEGRAM_BOT_TOKEN": "T",
            "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
            "JARVIS_TELEGRAM_REALM_ID": "telegram:700001",
            "JARVIS_TELEGRAM_BOT_ID": "700001",
            "JARVIS_TELEGRAM_MAX_CONCURRENT_UPDATES": "999",
            "JARVIS_TELEGRAM_MAX_PENDING_UPDATES": "8",
            "JARVIS_TELEGRAM_MAX_PENDING_PER_USER": "3",
            "JARVIS_TELEGRAM_BRIDGE_RATE_LIMIT_PER_MINUTE": "5",
        }
    )
    assert cfg.max_concurrent_updates == 32
    assert cfg.max_pending_updates == 8
    assert cfg.max_pending_per_user == 3
    assert cfg.intake_rate_per_minute == 5


def test_multiple_users_do_not_require_bridge_side_owner_assignment():
    cfg = load_config(
        {
            "TELEGRAM_BOT_TOKEN": "T",
            "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
            "JARVIS_TELEGRAM_REALM_ID": "telegram:700001",
            "JARVIS_TELEGRAM_BOT_ID": "700001",
            "TELEGRAM_ALLOWED_CHAT_IDS": "42, 99",
        }
    )
    assert cfg.owner_chat_ids == frozenset()


def test_load_config_requires_tls_for_remote_backend():
    with pytest.raises(SystemExit, match="must use HTTPS"):
        load_config(
            {
                "TELEGRAM_BOT_TOKEN": "T",
                "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
                "JARVIS_TELEGRAM_REALM_ID": "telegram:700001",
                "JARVIS_TELEGRAM_BOT_ID": "700001",
                "JARVIS_BACKEND_URL": "http://jarvis.example.test:8000",
            }
        )

    secure = load_config(
        {
            "TELEGRAM_BOT_TOKEN": "T",
            "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
            "JARVIS_TELEGRAM_REALM_ID": "telegram:700001",
            "JARVIS_TELEGRAM_BOT_ID": "700001",
            "JARVIS_BACKEND_URL": "https://jarvis.example.test",
        }
    )
    assert secure.backend_url == "https://jarvis.example.test"


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
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 999, "type": "private"},
            "from": {"id": 999, "is_bot": False},
            "text": "hi",
        },
    }
    asyncio.run(bridge._handle(update))
    assert api_calls == []  # the backend agent is never called for a non-allowlisted chat


def test_group_chat_denied_even_if_id_allowed():
    api_calls: list[str] = []

    def api_handler(request):
        api_calls.append(str(request.url))
        return httpx.Response(200, json={})

    bridge = _bridge(lambda r: httpx.Response(200, json={"ok": True, "result": {}}), api_handler)
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "group"},
            "from": {"id": 42, "is_bot": False},
            "text": "hi",
        },
    }
    asyncio.run(bridge._handle(update))
    assert api_calls == []


def test_bridge_worker_pool_is_concurrent_across_users_and_single_flight_per_user():
    bridge = _bridge(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}}),
        lambda _request: httpx.Response(200, json={}),
        allowed_chat_ids=frozenset({42, 99}),
        max_concurrent_updates=2,
        max_pending_updates=4,
        max_pending_per_user=2,
        intake_rate_per_minute=10,
    )

    def update(update_id: int, chat_id: int) -> dict:
        return {
            "update_id": update_id,
            "message": {
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": chat_id, "is_bot": False},
                "text": "work",
            },
        }

    async def scenario() -> None:
        entered: list[tuple[int, int]] = []
        two_users_entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_handle(item: dict) -> None:
            chat_id = int(item["message"]["chat"]["id"])
            entered.append((chat_id, int(item["update_id"])))
            if {entry[0] for entry in entered} == {42, 99}:
                two_users_entered.set()
            await release.wait()

        bridge._handle = slow_handle  # type: ignore[method-assign]
        assert bridge._enqueue_update(update(1, 42)) is True
        assert bridge._enqueue_update(update(2, 42)) is True
        assert bridge._enqueue_update(update(3, 42)) is False
        assert bridge._enqueue_update(update(4, 99)) is True

        await asyncio.wait_for(two_users_entered.wait(), timeout=1)
        assert (42, 1) in entered
        assert (99, 4) in entered
        assert (42, 2) not in entered

        release.set()
        await asyncio.gather(*tuple(bridge._update_tasks))
        assert (42, 2) in entered
        await bridge.aclose()

    asyncio.run(scenario())


def test_bridge_intake_limit_is_per_user_and_windowed():
    bridge = _bridge(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}}),
        lambda _request: httpx.Response(200, json={}),
        intake_rate_per_minute=2,
    )
    assert bridge._consume_bridge_intake(42, now=100.0) is True
    assert bridge._consume_bridge_intake(42, now=101.0) is True
    assert bridge._consume_bridge_intake(42, now=102.0) is False
    assert bridge._consume_bridge_intake(99, now=102.0) is True
    assert bridge._consume_bridge_intake(42, now=161.0) is True


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
    msg = {
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False},
        "text": "здравствуй",
    }
    asyncio.run(bridge._handle({"update_id": 1, "message": msg}))
    assert len(chat_bodies) == 1
    assert chat_bodies[0]["message"] == "здравствуй"
    assert chat_bodies[0]["request_id"] == "telegram:700001:1"
    assert "access_mode" not in chat_bodies[0]
    # Telegram-first: stamp chat id so reminders fire back into this DM.
    assert chat_bodies[0]["notification_chat_id"] == 42
    # The bridge allocates the id before the backend call, closing the crash window where
    # a completed first turn could be orphaned before its returned id was remembered.
    assert chat_bodies[0]["conversation_id"].startswith("tg_")
    assert any(m.get("text") == "Привет!" for m in sent)
    # A backend-normalized id is remembered for the next turn.
    assert bridge.conversations[42] == "c1"


def test_non_owner_allowed_chat_uses_backend_scoped_surface():
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
        "message": {
            "chat": {"id": 99, "type": "private"},
            "from": {"id": 99, "is_bot": False},
            "text": "привет",
        },
    }

    asyncio.run(bridge._handle(update))

    assert len(chat_bodies) == 1
    assert chat_bodies[0]["message"] == "привет"
    assert "access_mode" not in chat_bodies[0]
    assert chat_bodies[0]["notification_chat_id"] == 99
    assert chat_bodies[0]["conversation_id"].startswith("tg_")
    assert api_calls == [
        "/api/preferences",
        "/api/files",
        "/api/chat",
        "/api/files",
    ]


def test_bridge_does_not_make_permission_decisions_for_non_owner():
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
            "from": {"id": 99, "is_bot": False},
            "caption": "посмотри",
            "photo": [{"file_id": "p1", "width": 100}],
        },
    }

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(bridge._handle(update))

    # Authorization is enforced by the backend using the scoped user session. The bridge
    # neither elevates the user nor silently turns a non-owner into an owner.
    assert api_calls == ["/api/preferences", "/api/files", "/api/chat"]


def test_reset_command_rotates_conversation_without_calling_agent():
    api_calls: list[str] = []

    def api_handler(request):
        api_calls.append(request.url.path)
        return httpx.Response(200, json={})

    bridge = _bridge(lambda r: httpx.Response(200, json={"ok": True, "result": {}}), api_handler)
    bridge.conversations[42] = "old"
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "/new",
        },
    }
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
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "/start",
        },
    }

    asyncio.run(bridge._handle(update))

    assert bridge.conversations[42] == "existing"
    assert "/api/chat" not in api_calls


def test_owner_invite_bypasses_allowlist_once_and_redacts_durable_secret(tmp_path):
    raw_token = "A" * 43
    command = f"/start owner_{raw_token}"
    session_payloads: list[dict] = []
    api_calls: list[str] = []
    sent_messages: list[dict] = []

    def tg_handler(request):
        if request.url.path.endswith("/sendMessage"):
            sent_messages.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        api_calls.append(request.url.path)
        return httpx.Response(200, json={})

    bridge = _bridge(
        tg_handler,
        api_handler,
        allowed_chat_ids=frozenset({42}),
        session_payloads=session_payloads,
    )
    update = {
        "update_id": 71,
        "message": {
            "chat": {"id": 99, "type": "private"},
            "from": {"id": 99, "is_bot": False, "username": "JBL61R"},
            "text": command,
        },
    }

    async def run_invite():
        accepted = bridge._enqueue_update(update)
        tasks = tuple(bridge._update_tasks)
        if tasks:
            await asyncio.gather(*tasks)
        return accepted

    assert asyncio.run(run_invite()) is True
    proof = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    assert session_payloads == [
        {
            "realm_id": "telegram:700001",
            "bot_id": 700001,
            "update_id": 71,
            "telegram_user": {
                "id": 99,
                "username": "JBL61R",
                "first_name": None,
                "last_name": None,
                "language_code": None,
                "is_premium": False,
            },
            "chat": {"id": 99, "type": "private"},
            "owner_invite_proof": proof,
        }
    ]
    assert "/api/chat" not in api_calls
    assert any("статус owner активирован" in item.get("text", "") for item in sent_messages)

    rejected = {
        "update_id": 72,
        "message": {
            "chat": {"id": 100, "type": "private"},
            "from": {"id": 100, "is_bot": False},
            "text": "/start owner_short",
        },
    }
    assert bridge._enqueue_update(rejected) is False

    store = TelegramConversationStore(
        tmp_path / "telegram.sqlite3",
        realm_id="telegram:700001",
    )
    assert store.persist_updates([(71, 99, update)]) == 1
    with sqlite3.connect(tmp_path / "telegram.sqlite3") as conn:
        persisted = conn.execute(
            "SELECT payload_json FROM telegram_update_inbox WHERE update_id = 71"
        ).fetchone()[0]
    assert raw_token not in persisted
    durable = json.loads(persisted)
    assert durable["_jarvis_owner_invite_proof"] == proof
    assert durable["message"]["text"] == "/start owner_[redacted]"


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
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "owner-1",
        },
    }
    guest_update = {
        "update_id": 2,
        "message": {
            "chat": {"id": 99, "type": "private"},
            "from": {"id": 99, "is_bot": False},
            "text": "guest-1",
        },
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
    assert chat_bodies[3]["conversation_id"] == guest_id


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
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "before",
        },
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


def test_access_mode_change_preserves_persisted_conversation(tmp_path):
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
        "message": {
            "chat": {"id": 99, "type": "private"},
            "from": {"id": 99, "is_bot": False},
            "text": "guest",
        },
    }
    asyncio.run(guest._handle(update))
    guest_id = chat_bodies[-1]["conversation_id"]
    asyncio.run(guest.aclose())

    promoted_owner = _bridge(
        tg_handler,
        api_handler,
        session_presets={99: "owner"},
        allowed_chat_ids=frozenset({99}),
        owner_chat_ids=frozenset({99}),
        conversation_store_path=state_path,
    )
    update["message"]["text"] = "owner"
    asyncio.run(promoted_owner._handle(update))
    asyncio.run(promoted_owner.aclose())

    assert chat_bodies[-1]["conversation_id"] == guest_id


def test_legacy_binding_store_migrates_into_main_database(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    legacy = TelegramConversationStore(legacy_path)
    legacy.bind(42, "tg_existing_owner", "owner", user_id="user-42")

    main = TelegramConversationStore(main_path, legacy_path=legacy_path)

    assert main.load_all() == {42: "tg_existing_owner"}
    # Migration is non-destructive and idempotent so rollback remains possible.
    assert legacy.load_all() == {42: "tg_existing_owner"}
    assert TelegramConversationStore(main_path, legacy_path=legacy_path).load_all() == {
        42: "tg_existing_owner"
    }


def test_changed_legacy_snapshot_is_imported_on_the_next_start(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(legacy_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL UNIQUE,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        legacy.execute(
            "INSERT INTO telegram_conversations VALUES (42, 'tg_first', 'guest', 'now')"
        )

    first = TelegramConversationStore(main_path, legacy_path=legacy_path)
    assert first.load_all() == {42: "tg_first"}
    with sqlite3.connect(legacy_path) as legacy:
        legacy.execute(
            "INSERT INTO telegram_conversations VALUES (99, 'tg_late', 'guest', 'later')"
        )

    restarted = TelegramConversationStore(main_path, legacy_path=legacy_path)
    assert restarted.load_all() == {42: "tg_first", 99: "tg_late"}


@pytest.mark.parametrize(
    ("rows", "error"),
    [
        (
            [(7, "tg_duplicate", "guest"), (99, "tg_duplicate", "guest")],
            "multiple chats",
        ),
        ([(7, "tg_invalid_mode", "administrator")], "invalid access_mode"),
    ],
)
def test_invalid_external_legacy_rows_fail_before_primary_store_is_created(
    tmp_path, rows, error
):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(legacy_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER NOT NULL,
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
            rows,
        )

    with pytest.raises(TelegramConversationMigrationError, match=error):
        TelegramConversationStore(
            main_path,
            realm_id="bot-a",
            legacy_path=legacy_path,
            legacy_realm_id="bot-a",
        )

    assert not main_path.exists()


def test_realm_aware_collision_rolls_back_inline_schema_upgrade(tmp_path):
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
        before = "\n".join(main.iterdump())
        journal_before = main.execute("PRAGMA journal_mode").fetchone()[0]
    with sqlite3.connect(legacy_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE telegram_conversations (
                realm_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                user_id TEXT
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO telegram_conversations VALUES (
                'bot-a', 99, 'tg_collision', 'guest', CURRENT_TIMESTAMP, 'user-b'
            )
            """
        )

    with pytest.raises(TelegramConversationMigrationError, match="different chats"):
        TelegramConversationStore(
            main_path,
            realm_id="bot-a",
            legacy_path=legacy_path,
            legacy_realm_id="bot-a",
        )

    with sqlite3.connect(main_path) as main:
        assert "\n".join(main.iterdump()) == before
        assert main.execute("PRAGMA journal_mode").fetchone()[0] == journal_before
        assert [
            row[1] for row in main.execute("PRAGMA table_info(telegram_conversations)")
        ] == ["chat_id", "conversation_id"]


def test_legacy_binding_is_claimed_once_and_stale_tenant_is_rejected(tmp_path):
    database_path = tmp_path / "jarvis.sqlite3"
    store = TelegramConversationStore(database_path, realm_id="bot-a")
    with sqlite3.connect(database_path) as database:
        database.execute(
            """
            INSERT INTO telegram_conversations(
                realm_id, chat_id, conversation_id, access_mode, user_id
            ) VALUES ('bot-a', 42, 'tg_unclaimed_legacy', 'guest', NULL)
            """
        )

    user_a = store.get_or_create(42, "guest", user_id="user-a")
    assert user_a == "tg_unclaimed_legacy"
    assert store.get_or_create(42, "guest", user_id="user-a") == user_a

    with pytest.raises(TelegramConversationIsolationError, match="another backend user"):
        store.get_or_create(42, "guest", user_id="user-b")
    with pytest.raises(TelegramConversationIsolationError, match="another backend user"):
        store.rotate(42, "guest", user_id="user-b")
    with pytest.raises(TelegramConversationIsolationError, match="another backend user"):
        store.bind(42, "tg_rebound", "guest", user_id="user-b")
    with sqlite3.connect(database_path) as database:
        row = database.execute(
            """
            SELECT conversation_id, user_id
            FROM telegram_conversations
            WHERE realm_id = 'bot-a' AND chat_id = 42
            """
        ).fetchone()
    assert row == (user_a, "user-a")


def test_custom_realm_refuses_implicit_claim_of_inline_legacy_history(tmp_path):
    database_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(database_path) as database:
        database.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL UNIQUE
            )
            """
        )
        database.execute(
            "INSERT INTO telegram_conversations VALUES (42, 'tg_legacy')"
        )
        before = "\n".join(database.iterdump())

    with pytest.raises(TelegramConversationMigrationError, match="explicit matching"):
        TelegramConversationStore(database_path, realm_id="bot-a")

    with sqlite3.connect(database_path) as database:
        assert "\n".join(database.iterdump()) == before


def test_unfinished_or_cross_realm_schema_fails_closed_without_mutation(tmp_path):
    unfinished_path = tmp_path / "unfinished.sqlite3"
    with sqlite3.connect(unfinished_path) as database:
        database.execute(
            "CREATE TABLE telegram_conversations_legacy_v1 "
            "(chat_id INTEGER, conversation_id TEXT)"
        )
        unfinished_before = "\n".join(database.iterdump())
    with pytest.raises(TelegramConversationMigrationError, match="unfinished"):
        TelegramConversationStore(unfinished_path)
    with sqlite3.connect(unfinished_path) as database:
        assert "\n".join(database.iterdump()) == unfinished_before

    global_unique_path = tmp_path / "global-unique.sqlite3"
    with sqlite3.connect(global_unique_path) as database:
        database.execute(
            """
            CREATE TABLE telegram_conversations (
                realm_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL UNIQUE,
                conversation_id TEXT NOT NULL,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                user_id TEXT,
                PRIMARY KEY(realm_id, chat_id)
            )
            """
        )
        global_before = "\n".join(database.iterdump())
    with pytest.raises(TelegramConversationMigrationError, match="cross-realm"):
        TelegramConversationStore(global_unique_path, realm_id="bot-a")
    with sqlite3.connect(global_unique_path) as database:
        assert "\n".join(database.iterdump()) == global_before


def test_legacy_migration_rejects_conflicting_tenant_owner_without_mutation(tmp_path):
    main_path = tmp_path / "jarvis.sqlite3"
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main = TelegramConversationStore(main_path, realm_id="bot-a")
    main.bind(42, "tg_shared", "guest", user_id="user-a")
    with sqlite3.connect(legacy_path) as legacy:
        legacy.execute(
            """
            CREATE TABLE telegram_conversations (
                realm_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                user_id TEXT
            )
            """
        )
        legacy.execute(
            """
            INSERT INTO telegram_conversations VALUES (
                'bot-a', 42, 'tg_shared', 'guest', CURRENT_TIMESTAMP, 'user-b'
            )
            """
        )
    with sqlite3.connect(main_path) as database:
        before = "\n".join(database.iterdump())

    with pytest.raises(TelegramConversationMigrationError, match="user ownership"):
        TelegramConversationStore(
            main_path,
            realm_id="bot-a",
            legacy_path=legacy_path,
        )

    with sqlite3.connect(main_path) as database:
        assert "\n".join(database.iterdump()) == before


def test_conversation_bindings_are_isolated_by_bot_realm_and_migrate_old_schema(tmp_path):
    database_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL UNIQUE,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO telegram_conversations VALUES (42, 'legacy-conv', 'guest', 'now')"
        )

    realm_a = TelegramConversationStore(
        database_path,
        realm_id="bot-a",
        legacy_realm_id="bot-a",
    )
    assert realm_a.load_all() == {42: "legacy-conv"}
    realm_b = TelegramConversationStore(database_path, realm_id="bot-b")
    assert realm_b.load_all() == {}
    realm_b.bind(42, "bot-b-conv", "guest", user_id="user-b")

    assert realm_a.load_all() == {42: "legacy-conv"}
    assert realm_b.load_all() == {42: "bot-b-conv"}


def test_default_realm_upgrade_requires_explicit_mapping_and_migrates_all_state(tmp_path):
    database_path = tmp_path / "jarvis.sqlite3"
    legacy = TelegramConversationStore(database_path)
    legacy.bind(42, "legacy-default-conversation", "guest", user_id="user-42")
    legacy.persist_updates(
        [
            (
                7,
                42,
                {
                    "update_id": 7,
                    "message": {
                        "chat": {"id": 42, "type": "private"},
                        "from": {"id": 42, "is_bot": False},
                        "text": "pending",
                    },
                },
            )
        ]
    )
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            CREATE TABLE external_identities (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                realm_id TEXT NOT NULL,
                provider_subject_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                UNIQUE(provider, realm_id, provider_subject_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO external_identities VALUES "
            "('identity-42', 'telegram', 'default', '42', 'user-42')"
        )
        conn.execute(
            """
            CREATE TABLE telegram_updates (
                realm_id TEXT NOT NULL,
                update_id INTEGER NOT NULL,
                PRIMARY KEY(realm_id, update_id)
            )
            """
        )
        conn.execute("INSERT INTO telegram_updates VALUES ('default', 6)")
        conn.execute(
            "INSERT INTO telegram_store_migrations(source_key, realm_id) "
            "VALUES ('old-default-marker', 'default')"
        )

    with pytest.raises(TelegramConversationMigrationError, match="explicit matching"):
        TelegramConversationStore(
            database_path,
            realm_id="telegram:700001",
        )

    upgraded = TelegramConversationStore(
        database_path,
        realm_id="telegram:700001",
        legacy_realm_id="telegram:700001",
    )
    assert upgraded.load_all() == {42: "legacy-default-conversation"}
    assert upgraded.next_update_offset() == 8
    with sqlite3.connect(database_path) as conn:
        for table in (
            "telegram_conversations",
            "telegram_update_inbox",
            "telegram_store_migrations",
            "telegram_updates",
            "external_identities",
        ):
            assert conn.execute(
                f'SELECT DISTINCT realm_id FROM "{table}" ORDER BY realm_id'
            ).fetchall() == [("telegram:700001",)]


def test_default_realm_upgrade_rejects_mixed_canonical_state_without_mutation(tmp_path):
    database_path = tmp_path / "jarvis.sqlite3"
    legacy = TelegramConversationStore(database_path)
    legacy.bind(42, "legacy-default-conversation", "guest", user_id="user-42")
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            INSERT INTO telegram_conversations(
                realm_id, chat_id, conversation_id, access_mode, user_id
            ) VALUES ('telegram:700001', 99, 'canonical-conversation', 'guest', 'user-99')
            """
        )
        before = "\n".join(conn.iterdump())

    with pytest.raises(TelegramConversationMigrationError, match="both contain state"):
        TelegramConversationStore(
            database_path,
            realm_id="telegram:700001",
            legacy_realm_id="telegram:700001",
        )

    with sqlite3.connect(database_path) as conn:
        assert "\n".join(conn.iterdump()) == before


def test_named_realm_upgrade_requires_explicit_source_and_migrates_all_state(tmp_path):
    database_path = tmp_path / "jarvis.sqlite3"
    source_realm = "legacy-custom-bot"
    target_realm = "telegram:700001"
    legacy = TelegramConversationStore(database_path, realm_id=source_realm)
    legacy.bind(42, "legacy-custom-conversation", "guest", user_id="user-42")
    legacy.persist_updates(
        [
            (
                7,
                42,
                {
                    "update_id": 7,
                    "message": {
                        "chat": {"id": 42, "type": "private"},
                        "from": {"id": 42, "is_bot": False},
                        "text": "pending custom realm",
                    },
                },
            )
        ]
    )
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            CREATE TABLE external_identities (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                realm_id TEXT NOT NULL,
                provider_subject_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                UNIQUE(provider, realm_id, provider_subject_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO external_identities VALUES (?, 'telegram', ?, '42', 'user-42')",
            ("identity-42", source_realm),
        )
        conn.execute(
            """
            CREATE TABLE telegram_updates (
                realm_id TEXT NOT NULL,
                update_id INTEGER NOT NULL,
                PRIMARY KEY(realm_id, update_id)
            )
            """
        )
        conn.execute("INSERT INTO telegram_updates VALUES (?, 6)", (source_realm,))
        conn.execute(
            "INSERT INTO telegram_store_migrations(source_key, realm_id) VALUES (?, ?)",
            ("old-custom-marker", source_realm),
        )

    with pytest.raises(TelegramConversationMigrationError, match="explicit matching"):
        TelegramConversationStore(
            database_path,
            realm_id=target_realm,
            legacy_source_realm_id=source_realm,
        )
    with pytest.raises(TelegramConversationMigrationError, match="contains no Telegram state"):
        TelegramConversationStore(
            database_path,
            realm_id=target_realm,
            legacy_realm_id=target_realm,
            legacy_source_realm_id="mistyped-legacy-realm",
        )

    upgraded = TelegramConversationStore(
        database_path,
        realm_id=target_realm,
        legacy_realm_id=target_realm,
        legacy_source_realm_id=source_realm,
    )

    assert upgraded.load_all() == {42: "legacy-custom-conversation"}
    assert upgraded.next_update_offset() == 8
    with sqlite3.connect(database_path) as conn:
        for table in (
            "telegram_conversations",
            "telegram_update_inbox",
            "telegram_store_migrations",
            "telegram_updates",
            "external_identities",
        ):
            assert conn.execute(
                f'SELECT DISTINCT realm_id FROM "{table}" ORDER BY realm_id'
            ).fetchall() == [(target_realm,)]
        assert conn.execute(
            "SELECT COUNT(*) FROM telegram_store_migrations "
            "WHERE source_key = ? AND realm_id = ?",
            (f"realm-upgrade:{source_realm}:{target_realm}", target_realm),
        ).fetchone() == (1,)

    # The explicit marker makes the same migration configuration restart-safe.
    restarted = TelegramConversationStore(
        database_path,
        realm_id=target_realm,
        legacy_realm_id=target_realm,
        legacy_source_realm_id=source_realm,
    )
    assert restarted.load_all() == {42: "legacy-custom-conversation"}


def test_named_realm_upgrade_rejects_mixed_target_state_without_mutation(tmp_path):
    database_path = tmp_path / "jarvis.sqlite3"
    source_realm = "legacy-custom-bot"
    target_realm = "telegram:700001"
    legacy = TelegramConversationStore(database_path, realm_id=source_realm)
    legacy.bind(42, "legacy-custom-conversation", "guest", user_id="user-42")
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            INSERT INTO telegram_conversations(
                realm_id, chat_id, conversation_id, access_mode, user_id
            ) VALUES (?, 99, 'canonical-conversation', 'guest', 'user-99')
            """,
            (target_realm,),
        )
        before = "\n".join(conn.iterdump())

    with pytest.raises(TelegramConversationMigrationError, match="both contain state"):
        TelegramConversationStore(
            database_path,
            realm_id=target_realm,
            legacy_realm_id=target_realm,
            legacy_source_realm_id=source_realm,
        )

    with sqlite3.connect(database_path) as conn:
        assert "\n".join(conn.iterdump()) == before


def test_realm_less_external_legacy_store_is_claimed_by_one_realm(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(legacy_path) as conn:
        conn.execute(
            """
            CREATE TABLE telegram_conversations (
                chat_id INTEGER PRIMARY KEY,
                conversation_id TEXT NOT NULL UNIQUE,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO telegram_conversations VALUES "
            "(42, 'legacy-conv', 'guest', 'now')"
        )

    realm_a = TelegramConversationStore(
        main_path,
        realm_id="bot-a",
        legacy_path=legacy_path,
        legacy_realm_id="bot-a",
    )
    with pytest.raises(TelegramConversationMigrationError, match="explicit matching"):
        TelegramConversationStore(
            main_path,
            realm_id="bot-b",
            legacy_path=legacy_path,
        )
    with pytest.raises(TelegramConversationMigrationError, match="another bot realm"):
        TelegramConversationStore(
            main_path,
            realm_id="bot-b",
            legacy_path=legacy_path,
            legacy_realm_id="bot-b",
        )

    assert realm_a.load_all() == {42: "legacy-conv"}


def test_realm_aware_external_default_store_requires_mapping_and_preserves_history(
    tmp_path,
):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(legacy_path) as conn:
        conn.execute(
            """
            CREATE TABLE telegram_conversations (
                realm_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                user_id TEXT,
                PRIMARY KEY(realm_id, chat_id)
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO telegram_conversations(
                realm_id, chat_id, conversation_id, access_mode, updated_at, user_id
            ) VALUES ('default', ?, ?, 'owner', CURRENT_TIMESTAMP, NULL)
            """,
            [(42, "legacy-owner"), (99, "legacy-second-user")],
        )

    with pytest.raises(TelegramConversationMigrationError, match="explicit matching"):
        TelegramConversationStore(
            main_path,
            realm_id="telegram:700001",
            legacy_path=legacy_path,
        )
    assert not main_path.exists()

    migrated = TelegramConversationStore(
        main_path,
        realm_id="telegram:700001",
        legacy_path=legacy_path,
        legacy_realm_id="telegram:700001",
    )

    assert migrated.load_all() == {42: "legacy-owner", 99: "legacy-second-user"}
    with sqlite3.connect(main_path) as conn:
        assert conn.execute(
            "SELECT DISTINCT realm_id FROM telegram_conversations"
        ).fetchall() == [("telegram:700001",)]
    with sqlite3.connect(legacy_path) as conn:
        assert conn.execute(
            "SELECT DISTINCT realm_id FROM telegram_conversations"
        ).fetchall() == [("default",)]


def test_getme_migrates_explicit_named_external_source_to_canonical_realm(tmp_path):
    legacy_path = tmp_path / "telegram_bridge.sqlite3"
    main_path = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(legacy_path) as conn:
        conn.execute(
            """
            CREATE TABLE telegram_conversations (
                realm_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                access_mode TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                user_id TEXT,
                PRIMARY KEY(realm_id, chat_id)
            )
            """
        )
        conn.execute(
            "INSERT INTO telegram_conversations VALUES "
            "('old-production-bot', 42, 'legacy-named-history', 'guest', 'now', NULL)"
        )
    tg = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    api = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    bridge = TelegramBridge(
        _cfg(
            realm_id="",
            bot_id=0,
            conversation_store_path=main_path,
            legacy_conversation_store_path=legacy_path,
            legacy_conversation_realm_id="telegram:700001",
            legacy_conversation_source_realm_id="old-production-bot",
        ),
        tg_client=tg,
        api_client=api,
    )

    bridge._initialize_bot_identity({"id": 700001})

    assert bridge._conversation_store is not None
    assert bridge._conversation_store.load_all() == {42: "legacy-named-history"}
    with sqlite3.connect(main_path) as conn:
        assert conn.execute(
            "SELECT DISTINCT realm_id FROM telegram_conversations"
        ).fetchall() == [("telegram:700001",)]
    asyncio.run(bridge.aclose())


def test_durable_update_inbox_advances_offset_only_after_commit_and_recovers_lease(tmp_path):
    database_path = tmp_path / "jarvis.sqlite3"
    store = TelegramConversationStore(database_path, realm_id="bot-a")
    update = {
        "update_id": 7,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "durable",
        },
    }

    assert store.next_update_offset() == 0
    assert store.persist_updates([(7, 42, update)]) == 1
    assert store.next_update_offset() == 8
    first = store.claim_pending_updates(limit=10, lease_seconds=60)
    assert len(first) == 1
    claimed_update, first_lease = first[0]
    assert claimed_update == update
    assert store.finalize_update(7, first_lease, status="pending") is True

    # A restarted bridge can reclaim the durable update; a stale lease cannot finish it.
    restarted = TelegramConversationStore(database_path, realm_id="bot-a")
    second = restarted.claim_pending_updates(limit=10, lease_seconds=60)
    assert len(second) == 1
    _, second_lease = second[0]
    assert second_lease != first_lease
    assert restarted.finalize_update(7, first_lease, status="completed") is False
    assert restarted.finalize_update(7, second_lease, status="completed") is True
    assert restarted.claim_pending_updates(limit=10, lease_seconds=60) == []


def test_backend_retry_classifier_requires_machine_outage_marker_for_http_responses():
    request = httpx.Request("POST", "http://backend.test/api/chat")

    def status_error(status: int, *, headers=None, detail="failure"):
        response = httpx.Response(
            status,
            request=request,
            headers=headers,
            json={"detail": detail},
        )
        return httpx.HTTPStatusError(
            f"HTTP {status}",
            request=request,
            response=response,
        )

    assert _retryable_backend_http_error(httpx.ConnectError("offline", request=request))
    assert _retryable_backend_http_error(
        status_error(503, headers={"X-Jarvis-Retry-Class": "llm-outage"})
    )
    assert _retryable_backend_http_error(
        status_error(
            409,
            headers={"X-Jarvis-Retry-Class": "chat-request-in-progress"},
        )
    )
    assert not _retryable_backend_http_error(status_error(500))
    assert not _retryable_backend_http_error(status_error(503))
    assert not _retryable_backend_http_error(status_error(409, detail="conflict"))
    assert _retryable_backend_http_error(
        status_error(409, detail="Telegram update processing lease was superseded")
    )


@pytest.mark.parametrize("history", ["many_attempts", "old"])
def test_transient_update_never_expires_and_preserves_same_chat_order(
    tmp_path,
    history,
):
    database_path = tmp_path / f"jarvis-{history}.sqlite3"
    store = TelegramConversationStore(database_path, realm_id="telegram:700001")
    first = {
        "update_id": 30,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "outage message",
        },
    }
    second = {
        "update_id": 31,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "later message",
        },
    }
    store.persist_updates([(30, 42, first), (31, 42, second)])
    now = time.time()
    attempt_count = 10_000 if history == "many_attempts" else 25
    received_at = now if history == "many_attempts" else now - (90 * 24 * 60 * 60)
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            UPDATE telegram_update_inbox
            SET status = 'failed', attempt_count = ?, received_at = ?, updated_at = ?,
                last_error = 'transient_backend_failure'
            WHERE realm_id = 'telegram:700001' AND update_id = 30
            """,
            (attempt_count, received_at, now - 301),
        )

    claimed = store.claim_pending_updates(limit=10, lease_seconds=60)

    assert [payload["update_id"] for payload, _lease in claimed] == [30]
    with sqlite3.connect(database_path) as conn:
        retained = conn.execute(
            "SELECT status, attempt_count, payload_json FROM telegram_update_inbox "
            "WHERE realm_id = 'telegram:700001' AND update_id = 30"
        ).fetchone()
    assert retained is not None
    assert retained[:2] == ("processing", attempt_count + 1)
    assert json.loads(retained[2]) == first


def test_durable_inbox_retries_transient_session_failure_after_backoff(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "jarvis.sqlite3"
    now = [1_000.0]
    monkeypatch.setattr("jarvis_gpt.telegram_bridge.time.time", lambda: now[0])
    bridge = _bridge(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}}),
        lambda request: (
            httpx.Response(200, json=[])
            if request.url.path == "/api/files"
            else httpx.Response(
                200,
                json={
                    "conversation_id": "c1",
                    "message_id": "m1",
                    "answer": "ok",
                    "events": [],
                },
            )
        ),
        conversation_store_path=database_path,
    )
    store = bridge._conversation_store
    assert store is not None
    update = {
        "update_id": 7,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "retry session",
        },
    }
    store.persist_updates([(7, 42, update)])
    original_open_session = bridge._open_user_session
    session_attempts = 0

    async def flaky_open_session(**kwargs):
        nonlocal session_attempts
        session_attempts += 1
        if session_attempts == 1:
            raise httpx.ConnectError("backend unavailable")
        return await original_open_session(**kwargs)

    bridge._open_user_session = flaky_open_session  # type: ignore[method-assign]

    async def scenario() -> None:
        bridge._drain_durable_inbox()
        await asyncio.gather(*tuple(bridge._update_tasks))
        with sqlite3.connect(database_path) as conn:
            first = conn.execute(
                "SELECT status, attempt_count, last_error FROM telegram_update_inbox "
                "WHERE realm_id = 'telegram:700001' AND update_id = 7"
            ).fetchone()
        assert first == ("failed", 1, "transient_backend_failure")

        # Repeated drains before the retry deadline neither spin nor call the backend.
        bridge._drain_durable_inbox()
        await asyncio.sleep(0)
        assert session_attempts == 1

        now[0] += 2.1
        bridge._drain_durable_inbox()
        await asyncio.gather(*tuple(bridge._update_tasks))
        with sqlite3.connect(database_path) as conn:
            final = conn.execute(
                "SELECT status, attempt_count FROM telegram_update_inbox "
                "WHERE realm_id = 'telegram:700001' AND update_id = 7"
            ).fetchone()
        assert final == ("completed", 2)
        assert session_attempts == 2
        await bridge.aclose()

    asyncio.run(scenario())


def test_durable_inbox_keeps_transient_chat_retryable_after_normal_attempt_bound(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "jarvis.sqlite3"
    now = [2_000.0]
    monkeypatch.setattr("jarvis_gpt.telegram_bridge.time.time", lambda: now[0])
    chat_payloads: list[dict] = []

    def api_handler(request):
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/chat":
            chat_payloads.append(json.loads(request.content))
            if len(chat_payloads) <= 4:
                return httpx.Response(
                    503,
                    json={"detail": "Language model is temporarily unavailable"},
                    headers={"X-Jarvis-Retry-Class": "llm-outage"},
                )
            return httpx.Response(
                200,
                json={
                    "conversation_id": chat_payloads[-1]["conversation_id"],
                    "message_id": "answer-after-outage",
                    "answer": "Модель восстановилась",
                    "events": [],
                },
            )
        return httpx.Response(404)

    bridge = _bridge(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}}),
        api_handler,
        conversation_store_path=database_path,
    )
    store = bridge._conversation_store
    assert store is not None
    update = {
        "update_id": 9,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "retry chat",
        },
    }
    store.persist_updates([(9, 42, update)])

    async def scenario() -> None:
        for retry_delay in (0.0, 2.1, 10.1):
            now[0] += retry_delay
            bridge._drain_durable_inbox()
            await asyncio.gather(*tuple(bridge._update_tasks))

        with sqlite3.connect(database_path) as conn:
            still_pending = conn.execute(
                "SELECT status, attempt_count FROM telegram_update_inbox "
                "WHERE realm_id = 'telegram:700001' AND update_id = 9"
            ).fetchone()
        assert still_pending == ("failed", 3)
        assert len(chat_payloads) == 3
        assert {payload["request_id"] for payload in chat_payloads} == {
            "telegram:700001:9"
        }
        assert len({payload["conversation_id"] for payload in chat_payloads}) == 1

        # A model/container restart routinely takes longer than the first two
        # retry delays. The durable update must remain retryable instead of being
        # silently discarded after the ordinary poison-message attempt bound.
        now[0] += 29.0
        bridge._drain_durable_inbox()
        await asyncio.sleep(0)
        assert len(chat_payloads) == 3

        now[0] += 1.1
        bridge._drain_durable_inbox()
        await asyncio.gather(*tuple(bridge._update_tasks))
        with sqlite3.connect(database_path) as conn:
            retried = conn.execute(
                "SELECT status, attempt_count FROM telegram_update_inbox "
                "WHERE realm_id = 'telegram:700001' AND update_id = 9"
            ).fetchone()
        assert retried == ("failed", 4)
        assert len(chat_payloads) == 4

        now[0] += 300.1
        bridge._drain_durable_inbox()
        await asyncio.gather(*tuple(bridge._update_tasks))
        with sqlite3.connect(database_path) as conn:
            recovered = conn.execute(
                "SELECT status, attempt_count FROM telegram_update_inbox "
                "WHERE realm_id = 'telegram:700001' AND update_id = 9"
            ).fetchone()
        assert recovered == ("completed", 5)
        assert len(chat_payloads) == 5
        await bridge.aclose()

    asyncio.run(scenario())


def test_durable_inbox_still_bounds_non_transient_poison_updates(tmp_path, monkeypatch):
    database_path = tmp_path / "jarvis.sqlite3"
    now = [2_500.0]
    monkeypatch.setattr("jarvis_gpt.telegram_bridge.time.time", lambda: now[0])
    bridge = _bridge(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}}),
        lambda _request: httpx.Response(200, json=[]),
        conversation_store_path=database_path,
    )
    store = bridge._conversation_store
    assert store is not None
    update = {
        "update_id": 11,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "poison update",
        },
    }
    store.persist_updates([(11, 42, update)])
    attempts = 0

    async def broken_handler(_update):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("deterministic bridge bug")

    bridge._handle = broken_handler  # type: ignore[method-assign]

    async def scenario() -> None:
        for retry_delay in (0.0, 2.1, 10.1):
            now[0] += retry_delay
            bridge._drain_durable_inbox()
            await asyncio.gather(*tuple(bridge._update_tasks))

        now[0] += 10_000.0
        bridge._drain_durable_inbox()
        await asyncio.sleep(0)
        with sqlite3.connect(database_path) as conn:
            terminal = conn.execute(
                "SELECT status, attempt_count, last_error FROM telegram_update_inbox "
                "WHERE realm_id = 'telegram:700001' AND update_id = 11"
            ).fetchone()
        assert terminal == ("failed", 3, "RuntimeError")
        assert attempts == 3
        await bridge.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("status_code", [500, 503])
def test_unmarked_http_failure_stops_after_three_and_unblocks_later_update(
    tmp_path,
    monkeypatch,
    status_code,
):
    database_path = tmp_path / f"jarvis-{status_code}.sqlite3"
    now = [2_700.0]
    monkeypatch.setattr("jarvis_gpt.telegram_bridge.time.time", lambda: now[0])
    chat_payloads: list[dict] = []
    replies: list[str] = []

    def tg_handler(request):
        if request.url.path.endswith("/sendMessage"):
            replies.append(str(json.loads(request.content).get("text") or ""))
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/chat":
            payload = json.loads(request.content)
            chat_payloads.append(payload)
            if payload["message"] == "deterministic poison":
                return httpx.Response(
                    status_code,
                    json={"detail": "payload-specific backend failure"},
                )
            return httpx.Response(
                200,
                json={
                    "conversation_id": payload["conversation_id"],
                    "message_id": "later-answer",
                    "answer": "Следующее сообщение обработано",
                    "events": [],
                },
            )
        return httpx.Response(404)

    bridge = _bridge(
        tg_handler,
        api_handler,
        conversation_store_path=database_path,
    )
    store = bridge._conversation_store
    assert store is not None
    updates = [
        (
            20,
            42,
            {
                "update_id": 20,
                "message": {
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False},
                    "text": "deterministic poison",
                },
            },
        ),
        (
            21,
            42,
            {
                "update_id": 21,
                "message": {
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 42, "is_bot": False},
                    "text": "later message",
                },
            },
        ),
    ]
    store.persist_updates(updates)

    async def drain_spawned_tasks() -> None:
        for _ in range(10):
            tasks = tuple(bridge._update_tasks)
            if not tasks:
                await asyncio.sleep(0)
                tasks = tuple(bridge._update_tasks)
            if not tasks:
                return
            await asyncio.gather(*tasks)
            await asyncio.sleep(0)
        raise AssertionError("durable inbox did not quiesce")

    async def scenario() -> None:
        for retry_delay in (0.0, 2.1, 10.1):
            now[0] += retry_delay
            bridge._drain_durable_inbox()
            await drain_spawned_tasks()

        poison = [
            payload for payload in chat_payloads if payload["message"] == "deterministic poison"
        ]
        later = [payload for payload in chat_payloads if payload["message"] == "later message"]
        assert len(poison) == 3
        assert len(later) == 1
        assert replies == ["Следующее сообщение обработано"]
        with sqlite3.connect(database_path) as conn:
            rows = conn.execute(
                "SELECT update_id, status, attempt_count, last_error "
                "FROM telegram_update_inbox WHERE realm_id = 'telegram:700001' "
                "ORDER BY update_id"
            ).fetchall()
        assert rows == [
            (20, "failed", 3, "HTTPStatusError"),
            (21, "completed", 1, None),
        ]

        now[0] += 10_000.0
        bridge._drain_durable_inbox()
        await asyncio.sleep(0)
        assert len(
            [
                payload
                for payload in chat_payloads
                if payload["message"] == "deterministic poison"
            ]
        ) == 3
        await bridge.aclose()

    asyncio.run(scenario())


def test_durable_inbox_retries_guest_chat_and_sends_one_reply(tmp_path, monkeypatch):
    database_path = tmp_path / "jarvis.sqlite3"
    now = [3_000.0]
    monkeypatch.setattr("jarvis_gpt.telegram_bridge.time.time", lambda: now[0])
    chat_payloads: list[dict] = []
    replies: list[str] = []

    def tg_handler(request):
        if request.url.path.endswith("/sendMessage"):
            replies.append(str(json.loads(request.content).get("text") or ""))
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/chat":
            chat_payloads.append(json.loads(request.content))
            if len(chat_payloads) == 1:
                return httpx.Response(503, json={"detail": "guest model unavailable"})
            return httpx.Response(
                200,
                json={
                    "conversation_id": chat_payloads[-1]["conversation_id"],
                    "message_id": "guest-answer-1",
                    "answer": "Ответ после безопасного повтора",
                    "events": [],
                },
            )
        return httpx.Response(404)

    bridge = _bridge(
        tg_handler,
        api_handler,
        session_presets={42: "guest"},
        conversation_store_path=database_path,
    )
    store = bridge._conversation_store
    assert store is not None
    update = {
        "update_id": 10,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "retry guest chat",
        },
    }
    store.persist_updates([(10, 42, update)])

    async def scenario() -> None:
        bridge._drain_durable_inbox()
        await asyncio.gather(*tuple(bridge._update_tasks))
        assert replies == []

        now[0] += 2.1
        bridge._drain_durable_inbox()
        await asyncio.gather(*tuple(bridge._update_tasks))
        with sqlite3.connect(database_path) as conn:
            final = conn.execute(
                "SELECT status, attempt_count FROM telegram_update_inbox "
                "WHERE realm_id = 'telegram:700001' AND update_id = 10"
            ).fetchone()
        assert final == ("completed", 2)
        assert len(chat_payloads) == 2
        assert {payload["request_id"] for payload in chat_payloads} == {
            "telegram:700001:10"
        }
        assert len({payload["conversation_id"] for payload in chat_payloads}) == 1
        assert replies == ["Ответ после безопасного повтора"]
        await bridge.aclose()

    asyncio.run(scenario())


def test_bridge_uses_lazy_bounded_hot_caches_for_unlimited_registered_users(tmp_path):
    database_path = tmp_path / "jarvis.sqlite3"
    store = TelegramConversationStore(database_path, realm_id="telegram:700001")
    with store._connect() as conn:
        conn.executemany(
            """
            INSERT INTO telegram_conversations(
                realm_id, chat_id, conversation_id, access_mode, updated_at
            ) VALUES ('telegram:700001', ?, ?, 'guest', CURRENT_TIMESTAMP)
            """,
            [(chat_id + 1, f"conv-{chat_id}") for chat_id in range(5_000)],
        )

    bridge = _bridge(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}}),
        lambda _request: httpx.Response(200, json={}),
        conversation_store_path=database_path,
    )
    assert bridge.conversations == {}
    for chat_id in range(4_100):
        bridge._cache_conversation(chat_id + 1, f"hot-{chat_id}", "guest")
    assert len(bridge.conversations) == 4_096
    assert len(bridge._conversation_modes) == 4_096


def test_runtime_database_backup_contains_telegram_bindings(tmp_path):
    database_path = tmp_path / "state" / "jarvis.sqlite3"
    storage = JarvisStorage(database_path)
    storage.initialize()
    try:
        conversations = TelegramConversationStore(database_path)
        conversations.bind(
            42,
            "tg_backup_owner",
            "owner",
            user_id="user-42",
        )
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


def test_telegram_command_suffix_and_payload_are_normalized():
    api_calls: list[str] = []

    def api_handler(request):
        api_calls.append(request.url.path)
        return httpx.Response(200, json={})

    bridge = _bridge(
        lambda _request: httpx.Response(200, json={"ok": True, "result": {}}),
        api_handler,
    )
    bridge.conversations[42] = "existing"
    start = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "/start payload",
        },
    }
    reset = {
        "update_id": 2,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "/new@JarvisBot",
        },
    }

    asyncio.run(bridge._handle(start))
    assert bridge.conversations[42] == "existing"
    asyncio.run(bridge._handle(reset))

    assert bridge.conversations[42].startswith("tg_")
    assert bridge.conversations[42] != "existing"
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
            "from": {"id": 42, "is_bot": False},
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
    common = {
        "TELEGRAM_BOT_TOKEN": "T",
        "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
        "JARVIS_TELEGRAM_REALM_ID": "telegram:700001",
        "JARVIS_TELEGRAM_BOT_ID": "700001",
        "TELEGRAM_ALLOWED_CHAT_IDS": "42",
    }
    assert load_config(common).voice_replies is True
    assert load_config({**common, "TELEGRAM_VOICE_REPLIES": "0"}).voice_replies is False


def _voice_bridge(
    monkeypatch,
    *,
    ogg: bytes | None,
    speak_status: int = 200,
    telegram_voice_status: int = 200,
    telegram_voice_description: str = "voice rejected",
    answer: str = "Готово, сэр.",
    preference: bool = False,
):
    monkeypatch.setattr("jarvis_gpt.telegram_bridge._wav_to_ogg_opus", lambda wav: ogg)
    tg_posts: list[str] = []
    tg_messages: list[str] = []
    chat_bodies: list[dict] = []
    speak_bodies: list[dict] = []

    def tg_handler(request):
        path = request.url.path
        if path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "voice/x.ogg"}})
        if "/file/botT/" in path:
            return httpx.Response(200, content=b"OggS-voice-bytes")
        tg_posts.append(path)
        if path.endswith("/sendMessage"):
            tg_messages.append(json.loads(request.content).get("text") or "")
        if path.endswith("/sendVoice") and telegram_voice_status != 200:
            return httpx.Response(
                telegram_voice_status,
                json={"ok": False, "description": telegram_voice_description},
            )
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
                    "answer": answer,
                    "events": [],
                },
            )
        if path == "/api/voice/speak":
            speak_bodies.append(json.loads(request.content))
            return httpx.Response(
                speak_status,
                content=b"RIFFwav-bytes" if speak_status == 200 else b"",
            )
        if path == "/api/preferences" and request.method == "GET":
            return httpx.Response(200, json={"voice_reply": preference})
        if path == "/api/files":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    return (
        _bridge(tg_handler, api_handler),
        tg_posts,
        tg_messages,
        chat_bodies,
        speak_bodies,
    )


def test_inbound_voice_transcribed_and_answered_with_voice(monkeypatch):
    bridge, tg_posts, _, chat_bodies, _ = _voice_bridge(
        monkeypatch, ogg=b"OggS-opus"
    )
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "voice": {"file_id": "vf", "mime_type": "audio/ogg", "duration": 3},
        },
    }
    asyncio.run(bridge._handle(update))
    # A voice-only note is relayed as an attachment; the message is a space so the backend
    # folds the transcript in as the real query.
    assert chat_bodies[0]["message"] == " "
    assert chat_bodies[0]["attachments"][0]["id"] == "v1"
    assert chat_bodies[0]["response_modality"] == "voice"
    # Spoken input -> a synthesized voice note reply (inline OGG/Opus).
    assert any(p.endswith("/sendVoice") for p in tg_posts)
    assert not any(p.endswith("/sendMessage") for p in tg_posts)


def test_voice_reply_falls_back_to_audio_when_no_opus(monkeypatch):
    bridge, tg_posts, _, _, _ = _voice_bridge(monkeypatch, ogg=None)
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "voice": {"file_id": "vf", "mime_type": "audio/ogg"},
        },
    }
    asyncio.run(bridge._handle(update))
    assert any(p.endswith("/sendAudio") for p in tg_posts)
    assert not any(p.endswith("/sendVoice") for p in tg_posts)
    assert not any(p.endswith("/sendMessage") for p in tg_posts)


def test_voice_reply_falls_back_to_audio_when_recipient_forbids_voice_notes(
    monkeypatch, caplog
):
    bridge, tg_posts, tg_messages, _, _ = _voice_bridge(
        monkeypatch,
        ogg=b"OggS-opus",
        telegram_voice_status=400,
        telegram_voice_description="Bad Request: VOICE_MESSAGES_FORBIDDEN",
    )
    update = {
        "update_id": 11,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "voice": {"file_id": "vf", "mime_type": "audio/ogg"},
        },
    }

    with caplog.at_level(logging.INFO, logger="jarvis.telegram"):
        asyncio.run(bridge._handle(update))

    assert sum(path.endswith("/sendVoice") for path in tg_posts) == 1
    assert sum(path.endswith("/sendAudio") for path in tg_posts) == 1
    assert not any(path.endswith("/sendMessage") for path in tg_posts)
    assert tg_messages == []
    assert "retrying as audio" in caplog.text
    assert "voice delivery succeeded chat_id=42" in caplog.text


def test_voice_reply_falls_back_to_text_when_tts_is_unavailable(monkeypatch, caplog):
    bridge, tg_posts, tg_messages, chat_bodies, _ = _voice_bridge(
        monkeypatch,
        ogg=b"OggS-opus",
        speak_status=503,
    )
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "voice": {"file_id": "vf", "mime_type": "audio/ogg"},
        },
    }

    with caplog.at_level(logging.WARNING, logger="jarvis.telegram"):
        asyncio.run(bridge._handle(update))

    assert chat_bodies[0]["response_modality"] == "voice"
    assert any(p.endswith("/sendMessage") for p in tg_posts)
    assert not any(p.endswith("/sendVoice") for p in tg_posts)
    assert not any(p.endswith("/sendAudio") for p in tg_posts)
    assert any("Не удалось доставить голосовой ответ" in text for text in tg_messages)
    assert "reason=http_status_503" in caplog.text


def test_text_input_in_auto_mode_stays_text(monkeypatch):
    bridge, tg_posts, _, chat_bodies, speak_bodies = _voice_bridge(
        monkeypatch,
        ogg=b"OggS-opus",
        answer="просто текст",
        preference=False,
    )
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "привет",
        },
    }
    asyncio.run(bridge._handle(update))
    assert speak_bodies == []
    assert chat_bodies[0]["response_modality"] == "text"
    assert any(p.endswith("/sendMessage") for p in tg_posts)
    assert not any(p.endswith("/sendVoice") for p in tg_posts)
    assert not any(p.endswith("/sendAudio") for p in tg_posts)


def test_explicit_text_request_triggers_one_voice_reply(monkeypatch):
    bridge, tg_posts, _, chat_bodies, speak_bodies = _voice_bridge(
        monkeypatch,
        ogg=b"OggS-opus",
        preference=False,
    )
    update = {
        "update_id": 2,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "Ответь голосом: какой сейчас статус?",
        },
    }

    asyncio.run(bridge._handle(update))

    assert chat_bodies[0]["response_modality"] == "voice"
    assert speak_bodies == [{"text": "Готово, сэр."}]
    assert any(path.endswith("/sendVoice") for path in tg_posts)


def test_persisted_on_preference_drives_text_voice_reply(monkeypatch):
    bridge, tg_posts, _, chat_bodies, speak_bodies = _voice_bridge(
        monkeypatch,
        ogg=b"OggS-opus",
        preference=True,
    )
    update = {
        "update_id": 3,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "username": "renamed-user"},
            "text": "привет",
        },
    }

    asyncio.run(bridge._handle(update))

    assert chat_bodies[0]["response_modality"] == "voice"
    assert len(speak_bodies) == 1
    assert any(path.endswith("/sendVoice") for path in tg_posts)


def test_long_voice_reply_is_split_into_bounded_tts_chunks(monkeypatch, caplog):
    answer = ("Длинная фраза. " * 260).strip()
    bridge, tg_posts, _, _, speak_bodies = _voice_bridge(
        monkeypatch,
        ogg=b"OggS-opus",
        answer=answer,
        preference=True,
    )
    update = {
        "update_id": 4,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "дай подробный ответ",
        },
    }

    caplog.set_level(logging.INFO, logger="jarvis.telegram")
    asyncio.run(bridge._handle(update))

    spoken = [body["text"] for body in speak_bodies]
    assert len(spoken) > 1
    assert all(0 < len(chunk) <= 1500 for chunk in spoken)
    assert " ".join(spoken) == answer
    assert sum(path.endswith("/sendVoice") for path in tg_posts) == len(spoken)
    assert not any(path.endswith("/sendMessage") for path in tg_posts)
    assert "voice delivery succeeded chat_id=42" in caplog.text
    assert answer not in caplog.text


def test_send_voice_failure_is_logged_and_falls_back_to_full_text(monkeypatch, caplog):
    bridge, tg_posts, tg_messages, _, _ = _voice_bridge(
        monkeypatch,
        ogg=b"OggS-opus",
        telegram_voice_status=500,
    )
    update = {
        "update_id": 5,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "voice": {"file_id": "vf", "mime_type": "audio/ogg"},
        },
    }

    with caplog.at_level(logging.WARNING, logger="jarvis.telegram"):
        asyncio.run(bridge._handle(update))

    assert any(path.endswith("/sendVoice") for path in tg_posts)
    assert any("Готово, сэр." in text for text in tg_messages)
    assert "voice delivery failed" in caplog.text
    assert "reason=HTTPStatusError status=500" in caplog.text


def test_forwarded_voice_is_source_material_and_stays_text_in_auto(monkeypatch):
    bridge, tg_posts, _, chat_bodies, speak_bodies = _voice_bridge(
        monkeypatch,
        ogg=b"OggS-opus",
        preference=False,
    )
    update = {
        "update_id": 6,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "forward_date": 1,
            "voice": {"file_id": "vf", "mime_type": "audio/ogg"},
        },
    }

    asyncio.run(bridge._handle(update))

    assert chat_bodies[0]["response_modality"] == "text"
    assert speak_bodies == []
    assert any(path.endswith("/sendMessage") for path in tg_posts)
    assert not any(path.endswith("/sendVoice") for path in tg_posts)


def test_voice_command_persists_on_and_auto_for_internal_session(monkeypatch):
    monkeypatch.setattr(
        "jarvis_gpt.telegram_bridge._wav_to_ogg_opus", lambda wav: b"OggS-opus"
    )
    state = {"voice_reply": False}
    patches: list[dict] = []
    preference_headers: list[str] = []
    chat_bodies: list[dict] = []
    tg_posts: list[str] = []

    def tg_handler(request):
        tg_posts.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        path = request.url.path
        if path == "/api/preferences":
            preference_headers.append(request.headers.get("X-Jarvis-User-Session", ""))
            if request.method == "PATCH":
                patch = json.loads(request.content)
                patches.append(patch)
                state.update(patch)
            return httpx.Response(200, json=state)
        if path == "/api/chat":
            chat_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "conversation_id": "c1",
                    "message_id": "m",
                    "answer": "ответ",
                    "events": [],
                },
            )
        if path == "/api/voice/speak":
            return httpx.Response(200, content=b"wav")
        if path == "/api/files":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    bridge = _bridge(tg_handler, api_handler)

    def update(update_id: int, text: str) -> dict:
        return {
            "update_id": update_id,
            "message": {
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "username": "mutable-name"},
                "text": text,
            },
        }

    asyncio.run(bridge._handle(update(10, "/voice on")))
    asyncio.run(bridge._handle(update(11, "обычный текст")))
    asyncio.run(bridge._handle(update(12, "/voice auto")))
    asyncio.run(bridge._handle(update(13, "ещё текст")))

    assert patches == [{"voice_reply": True}, {"voice_reply": False}]
    assert set(preference_headers) == {"session-42"}
    assert [body["response_modality"] for body in chat_bodies] == ["voice", "text"]
    assert sum(path.endswith("/sendVoice") for path in tg_posts) == 1


def test_voice_off_does_not_fake_unpersistable_third_state():
    patches: list[dict] = []
    messages: list[str] = []

    def tg_handler(request):
        if request.url.path.endswith("/sendMessage"):
            messages.append(json.loads(request.content).get("text") or "")
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/preferences" and request.method == "PATCH":
            patches.append(json.loads(request.content))
        return httpx.Response(404)

    bridge = _bridge(tg_handler, api_handler)
    update = {
        "update_id": 14,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "/voice off",
        },
    }

    asyncio.run(bridge._handle(update))

    assert patches == []
    assert any("Настройку не менял" in text for text in messages)


def test_quick_capture_body_parsers():
    assert _quick_capture_body("/note купить молоко", "/note") == "купить молоко"
    assert _quick_capture_body("+ идея", "") == "идея"
    assert _quick_capture_body("! срочно", "") == "срочно"
    assert _quick_capture_body("обычный текст", "") is None
    assert _quick_capture_body("/note", "/note") == ""


def test_looks_like_image_detects_png():
    assert _looks_like_image("chart.png", "image/png") is True
    assert _looks_like_image("report.docx", "application/vnd.openxmlformats") is False


def test_quick_capture_posts_memory_without_chat():
    memory_bodies: list[dict] = []
    sent: list[str] = []

    def tg_handler(request):
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
                    "id": "mem_abc",
                    "namespace": "inbox",
                    "content": "купить молоко",
                    "tags": ["capture"],
                    "importance": 0.6,
                    "created_at": "t",
                    "updated_at": "t",
                },
            )
        return httpx.Response(404)

    bridge = _bridge(tg_handler, api_handler)
    update = {
        "update_id": 11,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "/note купить молоко",
        },
    }
    asyncio.run(bridge._handle(update))
    assert len(memory_bodies) == 1
    assert memory_bodies[0]["namespace"] == "inbox"
    assert "купить молоко" in memory_bodies[0]["content"]
    assert any("inbox" in text.lower() or "📥" in text for text in sent)


def test_stop_calls_backend_cancel():
    cancel_bodies: list[dict] = []
    sent: list[str] = []

    def tg_handler(request):
        if request.url.path.endswith("/sendMessage"):
            payload = json.loads(request.content)
            sent.append(payload.get("text") or "")
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/chat/cancel":
            cancel_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "cancelled": True})
        return httpx.Response(404)

    bridge = _bridge(tg_handler, api_handler)
    bridge._active_request_ids[42] = "telegram:700001:99"
    update = {
        "update_id": 12,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "/stop",
        },
    }
    asyncio.run(bridge._handle(update))
    assert cancel_bodies == [
        {"notification_chat_id": 42, "request_id": "telegram:700001:99"}
    ]
    assert any("Остановил" in text for text in sent)


def test_reminder_callback_snooze_posts_api():
    snooze_calls: list[tuple[str, dict]] = []
    sent: list[str] = []
    answered: list[str] = []

    def tg_handler(request):
        payload = json.loads(request.content) if request.content else {}
        if request.url.path.endswith("/answerCallbackQuery"):
            answered.append(payload.get("callback_query_id") or "")
        if request.url.path.endswith("/sendMessage"):
            sent.append(payload.get("text") or "")
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path == "/api/reminders/rem_abc123/snooze":
            snooze_calls.append((request.url.path, json.loads(request.content)))
            return httpx.Response(
                200,
                json={"ok": True, "action": "snooze", "detail": "Отложено на 10 мин"},
            )
        return httpx.Response(404)

    bridge = _bridge(tg_handler, api_handler)
    update = {
        "update_id": 13,
        "callback_query": {
            "id": "cq1",
            "from": {"id": 42, "is_bot": False},
            "data": "r:rem_abc123:s10",
            "message": {
                "chat": {"id": 42, "type": "private"},
                "message_id": 7,
            },
        },
    }
    asyncio.run(bridge._handle(update))
    assert snooze_calls == [
        ("/api/reminders/rem_abc123/snooze", {"minutes": 10})
    ]
    assert answered == ["cq1"]
    assert any("Отложено" in text for text in sent)


def test_send_document_uses_send_photo_for_images():
    methods: list[str] = []

    def tg_handler(request):
        methods.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"ok": True, "result": {}})

    def api_handler(request):
        if request.url.path.endswith("/download"):
            return httpx.Response(
                200,
                content=b"\x89PNG\r\n\x1a\n",
                headers={"content-type": "image/png"},
            )
        return httpx.Response(404)

    bridge = _bridge(tg_handler, api_handler)

    async def _run():
        await bridge._open_user_session(
            update_id=1,
            chat_id=42,
            sender={"id": 42, "is_bot": False},
        )
        await bridge._send_document(
            42,
            {
                "id": "f1",
                "name": "chart.png",
                "mime_type": "image/png",
                "size": 12,
            },
        )

    asyncio.run(_run())
    assert "sendPhoto" in methods
    assert "sendDocument" not in methods
