"""Crash/restart reliability contracts for Telegram polling and attachment relay."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from jarvis_gpt.telegram_bridge import (
    TelegramBridge,
    TelegramConfig,
    TelegramConversationStore,
)

REALM = "telegram:700001"


def _config(database_path) -> TelegramConfig:
    return TelegramConfig(
        bot_token="test-token",
        allowed_chat_ids=frozenset({42}),
        backend_url="http://backend.test",
        bridge_secret="test-bridge-secret",
        realm_id=REALM,
        bot_id=700001,
        conversation_store_path=database_path,
    )


def _private_update(*, update_id: int = 10) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42, "is_bot": False, "username": "operator"},
            "caption": "analyze both files",
            "photo": [{"file_id": "photo-file", "width": 100, "file_size": 5}],
            "document": {
                "file_id": "document-file",
                "file_name": "evidence.txt",
                "mime_type": "text/plain",
            },
        },
    }


def _session_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "realm_id": payload["realm_id"],
            "bot_id": payload["bot_id"],
            "session_token": "session-42",
            "user": {"id": "owner-user", "preset_key": "owner"},
        },
    )


def _tg_handler(get_file_calls: list[str], sent: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            payload = json.loads(request.content)
            file_id = payload["file_id"]
            get_file_calls.append(file_id)
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": f"files/{file_id}"}},
            )
        if request.method == "GET" and "/files/" in request.url.path:
            return httpx.Response(200, content=request.url.path.rsplit("/", 1)[-1].encode())
        if request.url.path.endswith("/sendMessage"):
            sent.append(str((json.loads(request.content)).get("text") or ""))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        if request.url.path.endswith("/sendChatAction"):
            return httpx.Response(200, json={"ok": True, "result": True})
        raise AssertionError(f"unexpected Telegram request: {request.method} {request.url.path}")

    return handler


def _bridge(database_path, tg_handler, api_handler) -> TelegramBridge:
    bridge = TelegramBridge(
        _config(database_path),
        tg_client=httpx.AsyncClient(
            transport=httpx.MockTransport(tg_handler),
            base_url="https://api.telegram.test",
        ),
        api_client=httpx.AsyncClient(
            transport=httpx.MockTransport(api_handler),
            base_url="http://backend.test",
        ),
    )
    bridge._initialize_bot_identity({"id": 700001})
    return bridge


def test_partial_attachment_relay_retries_after_restart_without_text_only_ack(tmp_path):
    database = tmp_path / "jarvis.sqlite3"
    update = _private_update()
    get_file_calls: list[str] = []
    sent: list[str] = []
    upload_names: list[str] = []
    chat_bodies: list[dict] = []
    document_attempts = 0

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal document_attempts
        if request.url.path == "/api/integrations/telegram/session":
            return _session_response(json.loads(request.content))
        if request.url.path == "/api/preferences":
            return httpx.Response(200, json={"voice_reply": False})
        if request.url.path == "/api/files" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/files/upload":
            body = request.content.decode("latin-1")
            name = "evidence.txt" if "evidence.txt" in body else "photo.jpg"
            upload_names.append(name)
            if name == "evidence.txt":
                document_attempts += 1
                if document_attempts == 1:
                    return httpx.Response(503, json={"detail": "temporary"})
            file_id = "doc-1" if name == "evidence.txt" else "photo-1"
            return httpx.Response(
                200,
                json={
                    "file": {
                        "id": file_id,
                        "name": name,
                        "mime_type": "text/plain" if name.endswith("txt") else "image/jpeg",
                        "size": 5,
                    }
                },
            )
        if request.url.path == "/api/chat":
            chat_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"response": "done", "conversation_id": "c1"})
        raise AssertionError(f"unexpected backend request: {request.url.path}")

    async def scenario() -> None:
        first = _bridge(database, _tg_handler(get_file_calls, sent), api_handler)
        assert await first._handle(update) is False
        assert chat_bodies == []
        await first.aclose()

        restarted = _bridge(database, _tg_handler(get_file_calls, sent), api_handler)
        assert await restarted._handle(update) is not False
        await restarted.aclose()

    asyncio.run(scenario())

    assert get_file_calls.count("photo-file") == 1
    assert get_file_calls.count("document-file") == 2
    assert upload_names == ["photo.jpg", "evidence.txt", "evidence.txt"]
    assert len(chat_bodies) == 1
    assert {item["id"] for item in chat_bodies[0]["attachments"]} == {
        "photo-1",
        "doc-1",
    }


def test_transient_attachment_failure_keeps_durable_update_retryable(tmp_path):
    database = tmp_path / "jarvis.sqlite3"
    update = _private_update(update_id=12)
    update["message"].pop("photo")
    chats = 0

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal chats
        if request.url.path == "/api/integrations/telegram/session":
            return _session_response(json.loads(request.content))
        if request.url.path == "/api/files/upload":
            return httpx.Response(503, json={"detail": "temporary"})
        if request.url.path == "/api/chat":
            chats += 1
            return httpx.Response(200, json={"response": "wrong"})
        raise AssertionError(f"unexpected backend request: {request.url.path}")

    async def scenario() -> tuple[str, str]:
        bridge = _bridge(database, _tg_handler([], []), api_handler)
        store = bridge._conversation_store
        assert store is not None
        store.persist_updates([(12, 42, update)], next_offset=13)
        claimed = store.claim_pending_updates(limit=1, lease_seconds=60)
        assert len(claimed) == 1
        payload, lease = claimed[0]
        await bridge._process_queued_update(42, payload, lease_token=lease)
        with store._connect() as conn:
            status, error = conn.execute(
                """
                SELECT status, last_error FROM telegram_update_inbox
                WHERE realm_id = ? AND update_id = 12
                """,
                (REALM,),
            ).fetchone()
        await bridge.aclose()
        return str(status), str(error)

    status, error = asyncio.run(scenario())
    assert (status, error) == ("failed", "transient_backend_failure")
    assert chats == 0


def test_durable_bridge_backpressure_never_drops_or_reorders_same_chat(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "jarvis.sqlite3"
    bridge = TelegramBridge(_config(database))
    bridge._initialize_bot_identity({"id": 700001})
    store = bridge._conversation_store
    assert store is not None
    first = _private_update(update_id=60)
    second = _private_update(update_id=61)
    store.persist_updates([(60, 42, first), (61, 42, second)], next_offset=62)
    monkeypatch.setattr(bridge, "_enqueue_update", lambda *_args, **_kwargs: False)

    bridge._drain_durable_inbox()

    with store._connect() as conn:
        rows = conn.execute(
            """
            SELECT update_id, status, last_error FROM telegram_update_inbox
            WHERE realm_id = ? ORDER BY update_id
            """,
            (REALM,),
        ).fetchall()
    assert rows == [
        (60, "failed", "transient_backend_failure"),
        (61, "pending", None),
    ]
    assert store.claim_pending_updates(limit=2, lease_seconds=60) == []
    asyncio.run(bridge.aclose())


def test_expired_upload_session_is_retried_and_not_cached_as_rejection(tmp_path):
    database = tmp_path / "jarvis.sqlite3"
    update = _private_update(update_id=13)
    update["message"].pop("photo")
    upload_attempts = 0
    chat_bodies: list[dict] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal upload_attempts
        if request.url.path == "/api/integrations/telegram/session":
            return _session_response(json.loads(request.content))
        if request.url.path == "/api/files/upload":
            upload_attempts += 1
            if upload_attempts == 1:
                return httpx.Response(401, json={"detail": "session expired"})
            return httpx.Response(
                200,
                json={
                    "file": {
                        "id": "doc-13",
                        "name": "evidence.txt",
                        "mime_type": "text/plain",
                        "size": 5,
                    }
                },
            )
        if request.url.path == "/api/preferences":
            return httpx.Response(200, json={"voice_reply": False})
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/chat":
            chat_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "conversation_id": "c1",
                    "message_id": "m13",
                    "answer": "done",
                    "events": [],
                },
            )
        raise AssertionError(f"unexpected backend request: {request.url.path}")

    async def scenario() -> None:
        bridge = _bridge(database, _tg_handler([], []), api_handler)
        assert await bridge._handle(update) is False
        assert await bridge._handle(update) is not False
        await bridge.aclose()

    asyncio.run(scenario())

    assert upload_attempts == 2
    assert len(chat_bodies) == 1
    assert chat_bodies[0]["attachments"][0]["id"] == "doc-13"
    store = TelegramConversationStore(database, realm_id=REALM)
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT status FROM telegram_attachment_relay WHERE realm_id = ?",
            (REALM,),
        ).fetchall()
    assert [row[0] for row in rows] == ["success"]


def test_permanent_attachment_rejection_is_cached_and_persisted_as_delivery_record(tmp_path):
    database = tmp_path / "jarvis.sqlite3"
    update = _private_update()
    update["message"].pop("photo")
    update["message"]["caption"] = ""
    get_file_calls: list[str] = []
    sent: list[str] = []
    uploads = 0
    chat_bodies: list[dict] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        nonlocal uploads
        if request.url.path == "/api/integrations/telegram/session":
            return _session_response(json.loads(request.content))
        if request.url.path == "/api/files/upload":
            uploads += 1
            return httpx.Response(413, json={"detail": "too large"})
        if request.url.path == "/api/files":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/preferences":
            return httpx.Response(200, json={"voice_reply": False})
        if request.url.path == "/api/chat":
            chat_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "conversation_id": "c1",
                    "message_id": "m1",
                    "answer": "Файл не принят; содержимое недоступно для анализа.",
                    "events": [],
                    "duration_ms": 0,
                },
            )
        raise AssertionError(f"unexpected backend request: {request.url.path}")

    async def scenario() -> None:
        first = _bridge(database, _tg_handler(get_file_calls, sent), api_handler)
        await first._handle(update)
        await first.aclose()
        restarted = _bridge(database, _tg_handler(get_file_calls, sent), api_handler)
        await restarted._handle(update)
        await restarted.aclose()

    asyncio.run(scenario())

    assert uploads == 1
    assert get_file_calls == ["document-file"]
    assert len(chat_bodies) == 2
    assert all(body["request_id"] == f"{REALM}:10" for body in chat_bodies)
    assert all("evidence.txt" in body["message"] for body in chat_bodies)
    assert all("backend_upload_rejected" in body["message"] for body in chat_bodies)
    assert all("document-file" not in body["message"] for body in chat_bodies)
    store = TelegramConversationStore(database, realm_id=REALM)
    with store._connect() as conn:
        serialized = "\n".join(
            str(value)
            for row in conn.execute("SELECT * FROM telegram_attachment_relay")
            for value in row
        )
    assert "document-file" not in serialized
    assert sum("не передано" in message for message in sent) == 2
    assert sum("содержимое недоступно" in message.casefold() for message in sent) == 2


@pytest.mark.parametrize(
    "batch",
    [
        [
            {
                "update_id": 40,
                "message": {
                    "chat": {"id": -100123, "type": "group"},
                    "from": {"id": 99, "is_bot": False},
                    "text": "rejected group message",
                },
            }
        ],
        [
            {
                "update_id": 50,
                "channel_post": {
                    "message_id": 1,
                    "date": 1_753_000_000,
                    "chat": {"id": -100123, "type": "channel", "title": "News"},
                    "text": "channel only",
                },
            }
        ],
    ],
    ids=["rejected", "channel-only"],
)
def test_poll_checkpoint_survives_restart_for_non_inbox_batches(tmp_path, batch):
    database = tmp_path / "jarvis.sqlite3"
    bridge = TelegramBridge(_config(database))
    calls = 0

    async def fake_tg(method: str, **_params):
        nonlocal calls
        if method == "getMe":
            return {"id": 700001, "username": "Jarvis"}
        if method == "getUpdates":
            calls += 1
            if calls == 1:
                return batch
            raise asyncio.CancelledError
        raise AssertionError(method)

    bridge._tg = fake_tg

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await bridge.run()
        await bridge.aclose()

    asyncio.run(scenario())

    restarted = TelegramConversationStore(database, realm_id=REALM)
    assert restarted.next_update_offset() == batch[0]["update_id"] + 1
