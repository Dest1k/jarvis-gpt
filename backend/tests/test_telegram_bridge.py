"""Telegram bot frontend bridge — secure identity sessions + agent relay."""

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
    TelegramConversationStore,
    _chunks,
    _configure_logging,
    _looks_like_audio,
    load_config,
)

BRIDGE_SECRET = "bridge-test-secret-with-at-least-32-chars"


def _cfg(**over) -> TelegramConfig:
    base = {
        "bot_token": "T",
        "allowed_chat_ids": frozenset({42}),
        "backend_url": "http://backend.test",
        "bridge_secret": "bridge-secret",
    }
    base.update(over)
    return TelegramConfig(**base)


def _bridge(tg_handler, api_handler, *, session_presets=None, **cfg_over):
    presets = {42: "owner", **(session_presets or {})}

    def scoped_api_handler(request):
        if request.url.path == "/api/integrations/telegram/session":
            payload = json.loads(request.content)
            telegram_id = payload["telegram_user"]["id"]
            return httpx.Response(
                200,
                json={
                    "session_token": f"session-{telegram_id}",
                    "user": {
                        "id": f"user-{telegram_id}",
                        "preset_key": presets.get(telegram_id, "guest"),
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
    return TelegramBridge(_cfg(**cfg_over), tg_client=tg, api_client=api)


def test_load_config_fails_closed_without_token():
    with pytest.raises(SystemExit):
        load_config({"TELEGRAM_ALLOWED_CHAT_IDS": "42"})


def test_load_config_fails_closed_without_bridge_secret():
    with pytest.raises(SystemExit, match="JARVIS_TELEGRAM_BRIDGE_SECRET"):
        load_config({"TELEGRAM_BOT_TOKEN": "T"})


def test_load_config_allows_empty_optional_allowlist():
    cfg = load_config(
        {"TELEGRAM_BOT_TOKEN": "T", "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET}
    )
    assert cfg.allowed_chat_ids == frozenset()


def test_load_config_parses_allowlist():
    cfg = load_config(
        {
            "TELEGRAM_BOT_TOKEN": "T",
            "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
            "TELEGRAM_ALLOWED_CHAT_IDS": "42, 7 99",
            "TELEGRAM_OWNER_CHAT_IDS": "42",
        }
    )
    assert cfg.allowed_chat_ids == frozenset({42, 7, 99})
    assert cfg.owner_chat_ids == frozenset({42})
    assert cfg.conversation_store_path.name == "jarvis.sqlite3"
    assert cfg.legacy_conversation_store_path.name == "telegram_bridge.sqlite3"


def test_load_config_bounds_bridge_worker_pool():
    cfg = load_config(
        {
            "TELEGRAM_BOT_TOKEN": "T",
            "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
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
                "JARVIS_BACKEND_URL": "http://jarvis.example.test:8000",
            }
        )

    secure = load_config(
        {
            "TELEGRAM_BOT_TOKEN": "T",
            "JARVIS_TELEGRAM_BRIDGE_SECRET": BRIDGE_SECRET,
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
    assert chat_bodies[0]["request_id"] == "telegram:default:1"
    assert "access_mode" not in chat_bodies[0]
    assert "notification_chat_id" not in chat_bodies[0]
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
    assert "notification_chat_id" not in chat_bodies[0]
    assert chat_bodies[0]["conversation_id"].startswith("tg_")
    assert api_calls == ["/api/files", "/api/chat", "/api/files"]


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

    asyncio.run(bridge._handle(update))

    # Authorization is enforced by the backend using the scoped user session. The bridge
    # neither elevates the user nor silently turns a non-owner into an owner.
    assert api_calls == ["/api/files", "/api/chat"]


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

    assert chat_bodies[-1]["conversation_id"] != guest_id


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

    realm_a = TelegramConversationStore(database_path, realm_id="bot-a")
    assert realm_a.load_all() == {42: "legacy-conv"}
    realm_b = TelegramConversationStore(database_path, realm_id="bot-b")
    assert realm_b.load_all() == {}
    realm_b.bind(42, "bot-b-conv", "guest")

    assert realm_a.load_all() == {42: "legacy-conv"}
    assert realm_b.load_all() == {42: "bot-b-conv"}


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
        main_path, realm_id="bot-a", legacy_path=legacy_path
    )
    realm_b = TelegramConversationStore(
        main_path, realm_id="bot-b", legacy_path=legacy_path
    )

    assert realm_a.load_all() == {42: "legacy-conv"}
    assert realm_b.load_all() == {}


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
                "WHERE realm_id = 'default' AND update_id = 7"
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
                "WHERE realm_id = 'default' AND update_id = 7"
            ).fetchone()
        assert final == ("completed", 2)
        assert session_attempts == 2
        await bridge.aclose()

    asyncio.run(scenario())


def test_durable_inbox_retries_chat_with_same_id_and_stops_after_bound(
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
            return httpx.Response(503, json={"detail": "temporarily unavailable"})
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
            exhausted = conn.execute(
                "SELECT status, attempt_count FROM telegram_update_inbox "
                "WHERE realm_id = 'default' AND update_id = 9"
            ).fetchone()
        assert exhausted == ("failed", 3)
        assert len(chat_payloads) == 3
        assert {payload["request_id"] for payload in chat_payloads} == {
            "telegram:default:9"
        }
        assert len({payload["conversation_id"] for payload in chat_payloads}) == 1

        # Exhausted updates are terminal: even a much later drain cannot create a
        # fourth backend turn.
        now[0] += 10_000
        bridge._drain_durable_inbox()
        await asyncio.sleep(0)
        assert len(chat_payloads) == 3
        await bridge.aclose()

    asyncio.run(scenario())


def test_bridge_uses_lazy_bounded_hot_caches_for_unlimited_registered_users(tmp_path):
    database_path = tmp_path / "jarvis.sqlite3"
    store = TelegramConversationStore(database_path)
    with store._connect() as conn:
        conn.executemany(
            """
            INSERT INTO telegram_conversations(
                realm_id, chat_id, conversation_id, access_mode, updated_at
            ) VALUES ('default', ?, ?, 'guest', CURRENT_TIMESTAMP)
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
        "TELEGRAM_ALLOWED_CHAT_IDS": "42",
    }
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
            "from": {"id": 42, "is_bot": False},
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
            "from": {"id": 42, "is_bot": False},
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
    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False},
            "text": "привет",
        },
    }
    asyncio.run(bridge._handle(update))
    assert speak_calls == []  # text in -> text out, never voice
