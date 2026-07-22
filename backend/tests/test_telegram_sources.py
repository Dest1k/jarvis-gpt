"""Public Telegram source registry, durable ingest, and bridge isolation."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from datetime import UTC, datetime

import httpx
import jarvis_gpt.telegram_sources as telegram_sources_module
import pytest
from jarvis_gpt.authorization import ActorContext
from jarvis_gpt.telegram_bridge import TelegramBridge, TelegramConfig
from jarvis_gpt.telegram_sources import (
    TelegramReaderBatch,
    TelegramReaderCapability,
    TelegramReaderMedia,
    TelegramReaderPost,
    TelegramSourceAccessDenied,
    TelegramSourceError,
    TelegramSourceIngestDenied,
    TelegramSourceService,
)

REALM = "telegram:700001"
CHANNEL_ID = -1001234567890


def _actor(user_id: str, preset: str) -> ActorContext:
    return ActorContext(user_id=user_id, preset_key=preset, source="test")


OWNER = _actor("owner-user", "owner")
ADMIN = _actor("admin-user", "admin")
ORDINARY = _actor("ordinary-user", "user")


def _channel_update(
    *,
    update_id: int,
    message_id: int,
    text: str,
    edited: bool = False,
    channel_id: int = CHANNEL_ID,
    username: str = "reliable_news",
) -> dict:
    message = {
        "message_id": message_id,
        "date": 1_753_000_000 + message_id,
        "chat": {
            "id": channel_id,
            "type": "channel",
            "title": "Reliable News",
            "username": username,
        },
        "text": text,
        # Unknown fields, including credential-looking ones, must never be persisted.
        "bot_token": "700001:super-secret-token",
    }
    if edited:
        message["edit_date"] = 1_753_100_000 + message_id
    return {
        "update_id": update_id,
        "edited_channel_post" if edited else "channel_post": message,
    }


def test_public_operations_require_owner_or_admin(tmp_path):
    service = TelegramSourceService(tmp_path / "jarvis.sqlite3")

    operations = (
        lambda: service.capability(
            ORDINARY,
            source_type="channel",
            access_scope="public",
            source_chat_id=CHANNEL_ID,
        ),
        lambda: service.add(
            ORDINARY,
            realm_id=REALM,
            source_chat_id=CHANNEL_ID,
        ),
        lambda: service.list(ORDINARY),
        lambda: service.remove(ORDINARY, source_id="tgsrc_unknown"),
        lambda: service.sync(ORDINARY, source_id="tgsrc_unknown"),
        lambda: service.search(ORDINARY, query="news"),
        lambda: service.analyze(ORDINARY),
    )
    for operation in operations:
        with pytest.raises(TelegramSourceAccessDenied):
            operation()

    assert service.list(OWNER)["count"] == 0
    assert service.list(ADMIN)["count"] == 0


def test_unsupported_accounts_private_sources_and_usernames_fail_closed(tmp_path):
    service = TelegramSourceService(tmp_path / "jarvis.sqlite3")

    account = service.add(
        OWNER,
        realm_id=REALM,
        source_chat_id=123456,
        source_type="account",
        access_scope="public",
        username="some_person",
    )
    private = service.add(
        OWNER,
        realm_id=REALM,
        source_chat_id=CHANNEL_ID,
        source_type="channel",
        access_scope="private",
    )
    unresolved = service.add(
        OWNER,
        realm_id=REALM,
        source_chat_id=None,
        source_type="channel",
        access_scope="public",
        username="mutable_only",
    )

    assert account["capability"]["state"] == "bot_api_account_feed_unavailable"
    assert private["capability"]["state"] == "bot_api_private_source_unavailable"
    assert unresolved["capability"]["state"] == "immutable_chat_id_required"
    assert account["persisted"] is private["persisted"] is unresolved["persisted"] is False
    assert service.list(OWNER)["count"] == 0


def test_registry_is_tenant_scoped_and_identity_ignores_username_changes(tmp_path):
    service = TelegramSourceService(tmp_path / "jarvis.sqlite3")

    first = service.add(
        OWNER,
        realm_id=REALM,
        source_chat_id=CHANNEL_ID,
        title="Old title",
        username="old_name",
    )
    renamed = service.add(
        OWNER,
        realm_id=REALM,
        source_chat_id=CHANNEL_ID,
        title="New title",
        username="new_name",
    )
    admin_source = service.add(
        ADMIN,
        realm_id=REALM,
        source_chat_id=CHANNEL_ID,
        username="new_name",
    )

    assert first["source"]["id"] == renamed["source"]["id"]
    assert renamed["source"]["username"] == "new_name"
    assert admin_source["source"]["id"] != first["source"]["id"]
    assert service.list(OWNER)["count"] == 1
    assert service.list(ADMIN)["count"] == 1


def test_ingest_is_idempotent_per_tenant_message_and_edit_version(tmp_path):
    database = tmp_path / "jarvis.sqlite3"
    service = TelegramSourceService(database, allow_bot_ingest=True)
    owner_source = service.add(
        OWNER,
        realm_id=REALM,
        source_chat_id=CHANNEL_ID,
        username="old_name",
    )["source"]
    service.add(ADMIN, realm_id=REALM, source_chat_id=CHANNEL_ID)

    original = _channel_update(
        update_id=1,
        message_id=10,
        text="Первая версия reliability report",
    )
    edited = _channel_update(
        update_id=2,
        message_id=10,
        text="Исправленная версия reliability report",
        edited=True,
        username="renamed_channel",
    )
    second_edit_same_second = _channel_update(
        update_id=3,
        message_id=10,
        text="Вторая правка в ту же секунду",
        edited=True,
        username="renamed_channel",
    )

    first = service.ingest_bot_channel_update(original, realm_id=REALM)
    replay = service.ingest_bot_channel_update(original, realm_id=REALM)
    first_edit = service.ingest_bot_channel_update(edited, realm_id=REALM)
    edit_replay = service.ingest_bot_channel_update(edited, realm_id=REALM)
    second_edit = service.ingest_bot_channel_update(
        second_edit_same_second,
        realm_id=REALM,
    )
    second_edit_replay = service.ingest_bot_channel_update(
        second_edit_same_second,
        realm_id=REALM,
    )

    assert first["inserted_versions"] == 2
    assert replay["inserted_versions"] == 0
    assert first_edit["inserted_versions"] == 2
    assert edit_replay["inserted_versions"] == 0
    assert second_edit["inserted_versions"] == 2
    assert second_edit_replay["inserted_versions"] == 0
    original_hits = service.search(OWNER, query="Первая версия")["hits"]
    assert sum(hit["text"] == "Первая версия reliability report" for hit in original_hits) == 1
    edited_hit = service.search(OWNER, query="исправленная")["hits"][0]
    assert edited_hit["is_edited"] is True
    assert edited_hit["provenance"]["source_chat_id"] == CHANNEL_ID
    assert edited_hit["provenance"]["message_date"].endswith("+00:00")
    assert edited_hit["provenance"]["edit_date"].endswith("+00:00")
    assert edited_hit["provenance"]["permalink"].endswith("/10")
    assert service.search(ADMIN, query="исправленная")["count"] == 1
    assert service.search(OWNER, query="Вторая правка")["count"] == 1
    assert service.analyze(OWNER)["items"][0]["text"] == "Вторая правка в ту же секунду"

    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM telegram_source_posts").fetchone()[0] == 6
        serialized = "\n".join(
            str(value)
            for row in conn.execute(
                "SELECT text, scripts_json, source_title, source_username "
                "FROM telegram_source_posts"
            )
            for value in row
        )
    assert "super-secret-token" not in serialized
    assert service.list(OWNER)["sources"][0]["id"] == owner_source["id"]
    assert service.list(OWNER)["sources"][0]["username"] == "renamed_channel"


@pytest.mark.parametrize(
    ("text", "query", "script"),
    (
        ("Надёжный поиск по русскому тексту", "НАДЁЖНЫЙ", "cyrillic"),
        ("Reliable English retrieval", "reliable", "latin"),
        ("可靠的中文搜索", "中文搜索", "han"),
        ("신뢰할 수 있는 한국어 검색", "한국어", "hangul"),
        ("信頼できる日本語の検索です", "日本語の検索", "hiragana"),
    ),
)
def test_unicode_search_ru_en_zh_ko_ja(tmp_path, text, query, script):
    service = TelegramSourceService(
        tmp_path / "jarvis.sqlite3",
        allow_bot_ingest=True,
    )
    service.add(OWNER, realm_id=REALM, source_chat_id=CHANNEL_ID)
    service.ingest_bot_channel_update(
        _channel_update(update_id=1, message_id=1, text=text),
        realm_id=REALM,
    )

    result = service.search(OWNER, query=query)

    assert result["count"] == 1
    assert script in result["hits"][0]["scripts"]


def test_multilingual_query_variant_is_not_split_when_passed_as_string(tmp_path):
    service = TelegramSourceService(
        tmp_path / "jarvis.sqlite3",
        allow_bot_ingest=True,
    )
    service.add(OWNER, realm_id=REALM, source_chat_id=CHANNEL_ID)
    service.ingest_bot_channel_update(
        _channel_update(
            update_id=22,
            message_id=22,
            text="秘密项目发布计划",
        ),
        realm_id=REALM,
    )

    result = service.search(
        OWNER,
        query="secret project",
        queries="秘密项目",
    )

    assert result["count"] == 1
    assert result["queries"] == ["secret project", "秘密项目"]
    assert result["hits"][0]["text"] == "秘密项目发布计划"


def test_sync_remove_and_unregistered_channel_states_are_honest(tmp_path):
    service = TelegramSourceService(
        tmp_path / "jarvis.sqlite3",
        allow_bot_ingest=True,
    )
    source = service.add(
        OWNER,
        realm_id=REALM,
        source_chat_id=CHANNEL_ID,
    )["source"]

    waiting = service.sync(OWNER, source_id=source["id"])
    ignored = service.ingest_bot_channel_update(
        _channel_update(
            update_id=1,
            message_id=1,
            text="Other channel",
            channel_id=-1009999999999,
        ),
        realm_id=REALM,
    )
    service.ingest_bot_channel_update(
        _channel_update(update_id=2, message_id=2, text="Observed"),
        realm_id=REALM,
    )
    observed = service.sync(OWNER, source_id=source["id"])
    service.remove(OWNER, source_id=source["id"])
    after_remove = service.ingest_bot_channel_update(
        _channel_update(update_id=3, message_id=3, text="Not ingested"),
        realm_id=REALM,
    )

    assert waiting["state"] == "awaiting_bot_channel_post"
    assert waiting["history_supported"] is False
    assert ignored["state"] == "unregistered_source"
    assert observed["state"] == "live_ingest_observed"
    assert observed["history_supported"] is False
    assert after_remove["state"] == "unregistered_source"
    assert service.list(OWNER)["count"] == 0
    assert service.list(OWNER, include_removed=True)["sources"][0]["status"] == "removed"
    assert service.search(OWNER, query="Observed")["count"] == 0


def test_ingest_requires_explicit_bridge_capability(tmp_path):
    service = TelegramSourceService(tmp_path / "jarvis.sqlite3")
    with pytest.raises(TelegramSourceIngestDenied):
        service.ingest_bot_channel_update(
            _channel_update(update_id=1, message_id=1, text="blocked"),
            realm_id=REALM,
        )


def test_existing_iam_user_deletion_cascades_source_tenant_data(tmp_path):
    database = tmp_path / "jarvis.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE users(id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO users(id) VALUES (?)", (OWNER.user_id,))
    service = TelegramSourceService(database, allow_bot_ingest=True)
    service.add(OWNER, realm_id=REALM, source_chat_id=CHANNEL_ID)
    service.ingest_bot_channel_update(
        _channel_update(update_id=1, message_id=1, text="tenant data"),
        realm_id=REALM,
    )
    reader = _FakeAuthorizedReader(
        _reader_capability(authenticated=True),
        TelegramReaderBatch(
            posts=(
                TelegramReaderPost(
                    message_id=2,
                    text="private tenant data",
                    date=datetime(2026, 7, 20, tzinfo=UTC),
                ),
            ),
            complete=True,
        ),
    )
    reader_service = TelegramSourceService(database, authorized_reader=reader)
    reader_source = reader_service.add(
        OWNER,
        provider="authorized_reader",
        realm_id=REALM,
        source_chat_id=CHANNEL_ID - 1,
        source_type="supergroup",
        access_scope="private",
    )["source"]
    reader_service.sync(OWNER, source_id=reader_source["id"])

    with sqlite3.connect(database) as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (OWNER.user_id,))
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "telegram_sources",
                "telegram_source_posts",
                "telegram_reader_sources",
                "telegram_reader_posts",
                "telegram_source_audit",
            )
        }

    assert counts == {
        "telegram_sources": 0,
        "telegram_source_posts": 0,
        "telegram_reader_sources": 0,
        "telegram_reader_posts": 0,
        "telegram_source_audit": 0,
    }


def _bridge_with_sources(service, api_calls):
    def handler(request: httpx.Request) -> httpx.Response:
        api_calls.append(request.url.path)
        return httpx.Response(500)

    tg = httpx.AsyncClient(
        base_url="https://api.telegram.org/botT",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True})),
    )
    api = httpx.AsyncClient(
        base_url="http://backend.test",
        transport=httpx.MockTransport(handler),
    )
    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="T",
            allowed_chat_ids=frozenset(),
            backend_url="http://backend.test",
            bridge_secret="bridge-secret",
            realm_id=REALM,
            bot_id=700001,
        ),
        tg_client=tg,
        api_client=api,
        telegram_sources=service,
    )
    bridge._initialize_bot_identity({"id": 700001})
    return bridge


def test_bridge_channel_post_bypasses_private_user_iam(tmp_path):
    service = TelegramSourceService(
        tmp_path / "jarvis.sqlite3",
        allow_bot_ingest=True,
    )
    service.add(OWNER, realm_id=REALM, source_chat_id=CHANNEL_ID)
    api_calls: list[str] = []
    bridge = _bridge_with_sources(service, api_calls)

    async def run() -> None:
        await bridge._handle(
            _channel_update(update_id=1, message_id=1, text="Channel evidence")
        )
        await bridge.aclose()

    asyncio.run(run())

    assert api_calls == []
    assert service.search(OWNER, query="channel evidence")["count"] == 1


def test_bridge_fails_closed_when_channel_store_is_unavailable():
    api_calls: list[str] = []
    bridge = _bridge_with_sources(None, api_calls)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="source service is unavailable"):
            await bridge._handle(
                _channel_update(update_id=1, message_id=1, text="Must not disappear")
            )
        await bridge.aclose()

    asyncio.run(run())

    assert api_calls == []


def test_bridge_polls_and_ingests_channel_and_edited_updates(tmp_path):
    service = TelegramSourceService(
        tmp_path / "jarvis.sqlite3",
        allow_bot_ingest=True,
    )
    service.add(OWNER, realm_id=REALM, source_chat_id=CHANNEL_ID)
    api_calls: list[str] = []
    bridge = _bridge_with_sources(service, api_calls)
    polls: list[dict] = []
    batch = [
        _channel_update(update_id=1, message_id=1, text="Initial live post"),
        _channel_update(
            update_id=2,
            message_id=1,
            text="Edited live post",
            edited=True,
        ),
    ]

    async def fake_tg(method: str, **params):
        if method == "getMe":
            return {"id": 700001, "username": "test_bot"}
        if method == "getUpdates":
            polls.append(params)
            if len(polls) == 1:
                return batch
            raise asyncio.CancelledError
        raise AssertionError(method)

    bridge._tg = fake_tg

    async def run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await bridge.run()
        await bridge.aclose()

    asyncio.run(run())

    assert api_calls == []
    assert set(polls[0]["allowed_updates"]) == {
        "message",
        "callback_query",
        "channel_post",
        "edited_channel_post",
    }
    initial_hits = service.search(OWNER, query="Initial live post")["hits"]
    edited_hits = service.search(OWNER, query="Edited live post")["hits"]
    assert sum(hit["text"] == "Initial live post" for hit in initial_hits) == 1
    assert sum(hit["text"] == "Edited live post" for hit in edited_hits) == 1


def test_analysis_returns_latest_versions_with_provenance(tmp_path):
    service = TelegramSourceService(
        tmp_path / "jarvis.sqlite3",
        allow_bot_ingest=True,
    )
    source = service.add(
        OWNER,
        realm_id=REALM,
        source_chat_id=CHANNEL_ID,
    )["source"]
    service.ingest_bot_channel_update(
        _channel_update(update_id=1, message_id=1, text="Old summary text"),
        realm_id=REALM,
    )
    service.ingest_bot_channel_update(
        _channel_update(
            update_id=2,
            message_id=1,
            text="New summary text",
            edited=True,
        ),
        realm_id=REALM,
    )

    analysis = service.analyze(OWNER, source_ids=source["id"])

    assert analysis["post_count"] == 1
    assert analysis["items"][0]["text"] == "New summary text"
    assert analysis["items"][0]["citation"].startswith(f"telegram:{CHANNEL_ID}:1:")
    assert analysis["time_range"]["from"].endswith("+00:00")
    assert "preserve provenance" in analysis["analysis_contract"]


class _FakeAuthorizedReader:
    def __init__(
        self,
        capability: TelegramReaderCapability,
        batch: TelegramReaderBatch | None = None,
    ) -> None:
        self._capability = capability
        self._batch = batch or TelegramReaderBatch(posts=(), complete=True)
        self.calls = []
        self.session_secret = "must-never-be-returned-or-persisted"

    def capability(self) -> TelegramReaderCapability:
        return self._capability

    def read_history(
        self, source, *, limit: int, before_message_id: int | None = None
    ) -> TelegramReaderBatch:
        self.calls.append((source, limit, before_message_id))
        return self._batch


class _PagedAuthorizedReader(_FakeAuthorizedReader):
    def __init__(
        self,
        *,
        fail_call: int | None = None,
        max_message_id: int = 1201,
    ) -> None:
        super().__init__(_reader_capability(authenticated=True))
        self._fail_call = fail_call
        self._posts = tuple(
            TelegramReaderPost(
                message_id=message_id,
                text=f"paged history evidence {message_id}",
                date=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            )
            for message_id in range(1, max_message_id + 1)
        )

    def read_history(
        self, source, *, limit: int, before_message_id: int | None = None
    ) -> TelegramReaderBatch:
        self.calls.append((source, limit, before_message_id))
        if self._fail_call is not None and len(self.calls) == self._fail_call:
            raise RuntimeError("provider unavailable")
        eligible = [
            post
            for post in reversed(self._posts)
            if before_message_id is None or post.message_id < before_message_id
        ]
        page = tuple(eligible[:limit])
        complete = len(eligible) <= limit
        return TelegramReaderBatch(
            posts=page,
            complete=complete,
            next_before_message_id=None if complete else min(post.message_id for post in page),
        )


def _reader_capability(*, authenticated: bool) -> TelegramReaderCapability:
    return TelegramReaderCapability(
        provider_name="tdlib_runtime",
        reader_identity_sha256=hashlib.sha256(b"reader-account-42").hexdigest(),
        configured=True,
        authenticated=authenticated,
        state="ready" if authenticated else "session_missing",
        supports_history=True,
        supports_media=True,
    )


def test_authorized_reader_capability_fails_closed_without_authenticated_session(tmp_path):
    missing = TelegramSourceService(tmp_path / "missing.sqlite3")
    unauthenticated_reader = _FakeAuthorizedReader(
        _reader_capability(authenticated=False)
    )
    unauthenticated = TelegramSourceService(
        tmp_path / "unauthenticated.sqlite3",
        authorized_reader=unauthenticated_reader,
    )

    missing_state = missing.capability(
        OWNER,
        provider="authorized_reader",
        source_type="channel",
        access_scope="private",
        source_chat_id=CHANNEL_ID,
    )
    unauthenticated_state = unauthenticated.add(
        OWNER,
        provider="authorized_reader",
        realm_id=REALM,
        source_type="supergroup",
        access_scope="private",
        source_chat_id=CHANNEL_ID,
    )

    assert missing_state["state"] == "authorized_reader_unconfigured"
    assert unauthenticated_state["capability"]["state"] == (
        "authorized_reader_unauthenticated"
    )
    assert unauthenticated_state["persisted"] is False
    assert unauthenticated_reader.calls == []
    assert "session_secret" not in str(missing_state) + str(unauthenticated_state)


def test_authorized_reader_never_reads_personal_accounts(tmp_path):
    reader = _FakeAuthorizedReader(_reader_capability(authenticated=True))
    service = TelegramSourceService(
        tmp_path / "jarvis.sqlite3",
        authorized_reader=reader,
    )

    result = service.add(
        OWNER,
        provider="authorized_reader",
        realm_id=REALM,
        source_type="account",
        access_scope="public",
        source_chat_id=CHANNEL_ID,
    )

    assert result["capability"]["state"] == "personal_account_reading_forbidden"
    assert result["persisted"] is False
    assert reader.calls == []


def test_authorized_reader_syncs_private_supergroup_history_with_media_provenance(tmp_path):
    post = TelegramReaderPost(
        message_id=77,
        text="Private release evidence 배포 証拠",
        date=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
        version_id="v1",
        permalink="https://t.me/c/123/77",
        media=(
            TelegramReaderMedia(
                kind="document",
                stable_id="opaque-provider-media-id",
                file_name="report.pdf",
                mime_type="application/pdf",
                size=1234,
            ),
        ),
    )
    reader = _FakeAuthorizedReader(
        _reader_capability(authenticated=True),
        TelegramReaderBatch(posts=(post,), complete=True),
    )
    database = tmp_path / "jarvis.sqlite3"
    service = TelegramSourceService(database, authorized_reader=reader)
    source = service.add(
        OWNER,
        provider="authorized_reader",
        realm_id=REALM,
        source_type="supergroup",
        access_scope="private",
        source_chat_id=CHANNEL_ID,
        title="Private Ops",
    )["source"]

    first = service.sync(OWNER, source_id=source["id"])
    replay = service.sync(OWNER, source_id=source["id"])
    hit = service.search(OWNER, query="release evidence")["hits"][0]

    assert first["state"] == "history_synced"
    assert first["inserted_versions"] == 1
    assert replay["inserted_versions"] == 0
    assert source["source_type"] == "supergroup"
    assert source["access_scope"] == "private"
    assert hit["provenance"]["transport"] == "tdlib_runtime"
    assert hit["media"] == [
        {
            "file_name": "report.pdf",
            "kind": "document",
            "mime_type": "application/pdf",
            "size": 1234,
            "stable_id_sha256": hashlib.sha256(
                b"opaque-provider-media-id"
            ).hexdigest(),
        }
    ]
    with sqlite3.connect(database) as conn:
        serialized = "\n".join(
            str(row[0])
            for row in conn.execute(
                "SELECT media_json FROM telegram_reader_posts"
            ).fetchall()
        )
    assert "opaque-provider-media-id" not in serialized
    assert "must-never-be-returned-or-persisted" not in serialized

    switched_capability = _reader_capability(authenticated=True)
    switched_reader = _FakeAuthorizedReader(
        TelegramReaderCapability(
            provider_name=switched_capability.provider_name,
            reader_identity_sha256=hashlib.sha256(b"different-account").hexdigest(),
            configured=True,
            authenticated=True,
            state="ready",
            supports_history=True,
            supports_media=True,
        )
    )
    switched_service = TelegramSourceService(
        database,
        authorized_reader=switched_reader,
    )
    switched = switched_service.sync(OWNER, source_id=source["id"])
    assert switched["state"] == "authorized_reader_identity_changed"
    assert switched_reader.calls == []


def test_authorized_reader_history_pagination_resumes_from_durable_checkpoint(tmp_path):
    database = tmp_path / "jarvis.sqlite3"
    interrupted_reader = _PagedAuthorizedReader(fail_call=2)
    first_service = TelegramSourceService(database, authorized_reader=interrupted_reader)
    source = first_service.add(
        OWNER,
        provider="authorized_reader",
        realm_id=REALM,
        source_type="supergroup",
        access_scope="private",
        source_chat_id=CHANNEL_ID,
    )["source"]

    interrupted = first_service.sync(OWNER, source_id=source["id"])

    assert interrupted["ok"] is False
    assert interrupted["state"] == "authorized_reader_read_failed"
    assert interrupted["received"] == 500
    assert interrupted["source"]["history_before_message_id"] == 702
    assert interrupted["source"]["history_complete"] == 0

    resumed_reader = _PagedAuthorizedReader()
    resumed_service = TelegramSourceService(database, authorized_reader=resumed_reader)
    resumed = resumed_service.sync(OWNER, source_id=source["id"])

    assert resumed["ok"] is True
    assert resumed["complete"] is True
    assert resumed["received"] == 701
    assert resumed_reader.calls[0][2] == 702
    with sqlite3.connect(database) as conn:
        count = conn.execute("SELECT COUNT(*) FROM telegram_reader_posts").fetchone()[0]
    assert count == 1201


def test_incremental_reader_pagination_preserves_boundary_across_restart(tmp_path):
    database = tmp_path / "jarvis.sqlite3"
    initial_service = TelegramSourceService(
        database,
        authorized_reader=_PagedAuthorizedReader(max_message_id=1000),
    )
    source = initial_service.add(
        OWNER,
        provider="authorized_reader",
        realm_id=REALM,
        source_type="supergroup",
        access_scope="private",
        source_chat_id=CHANNEL_ID,
    )["source"]
    assert initial_service.sync(OWNER, source_id=source["id"])["complete"] is True

    interrupted_service = TelegramSourceService(
        database,
        authorized_reader=_PagedAuthorizedReader(
            fail_call=2,
            max_message_id=1601,
        ),
    )
    interrupted = interrupted_service.sync(OWNER, source_id=source["id"])

    assert interrupted["state"] == "authorized_reader_read_failed"
    assert interrupted["source"]["history_before_message_id"] == 1102
    assert interrupted["source"]["history_boundary_message_id"] == 1000
    assert interrupted["source"]["last_message_id"] == 1601
    assert interrupted["source"]["history_complete"] == 0

    resumed_reader = _PagedAuthorizedReader(max_message_id=1601)
    resumed_service = TelegramSourceService(database, authorized_reader=resumed_reader)
    resumed = resumed_service.sync(OWNER, source_id=source["id"])

    assert resumed["complete"] is True
    assert len(resumed_reader.calls) == 1
    assert resumed_reader.calls[0][2] == 1102
    assert resumed["source"]["history_before_message_id"] is None
    assert resumed["source"]["history_boundary_message_id"] is None
    with sqlite3.connect(database) as conn:
        count = conn.execute("SELECT COUNT(*) FROM telegram_reader_posts").fetchone()[0]
    assert count == 1601


def test_authorized_reader_rejects_cursor_that_can_skip_unread_messages(tmp_path):
    class _SkippingReader(_FakeAuthorizedReader):
        def read_history(
            self, source, *, limit: int, before_message_id: int | None = None
        ) -> TelegramReaderBatch:
            posts = tuple(
                TelegramReaderPost(
                    message_id=message_id,
                    text=f"post {message_id}",
                    date=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
                )
                for message_id in range(100, 50, -1)
            )
            return TelegramReaderBatch(
                posts=posts,
                complete=False,
                next_before_message_id=40,
            )

    database = tmp_path / "jarvis.sqlite3"
    service = TelegramSourceService(database, authorized_reader=_SkippingReader(
        _reader_capability(authenticated=True)
    ))
    source = service.add(
        OWNER,
        provider="authorized_reader",
        realm_id=REALM,
        source_type="channel",
        access_scope="private",
        source_chat_id=CHANNEL_ID,
    )["source"]

    with pytest.raises(TelegramSourceError, match="non-progressing history cursor"):
        service.sync(OWNER, source_id=source["id"])

    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM telegram_reader_posts").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["search", "analyze"])
def test_search_and_analysis_do_not_hold_write_lock_during_scoring(
    tmp_path,
    monkeypatch,
    operation,
):
    service = TelegramSourceService(
        tmp_path / "jarvis.sqlite3",
        allow_bot_ingest=True,
    )
    service.add(OWNER, realm_id=REALM, source_chat_id=CHANNEL_ID)
    service.ingest_bot_channel_update(
        _channel_update(update_id=1, message_id=1, text="lock probe evidence"),
        realm_id=REALM,
    )
    entered = threading.Event()
    release = threading.Event()
    errors = []

    if operation == "search":
        original = telegram_sources_module._match_score

        def block_scoring(text, candidates):
            entered.set()
            assert release.wait(5)
            return original(text, candidates)

        monkeypatch.setattr(telegram_sources_module, "_match_score", block_scoring)
        def target():
            return service.search(OWNER, query="lock probe")
    else:
        original = service._post_record

        def block_record(row):
            entered.set()
            assert release.wait(5)
            return original(row)

        monkeypatch.setattr(service, "_post_record", block_record)
        def target():
            return service.analyze(OWNER)

    def run_target():
        try:
            target()
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    worker = threading.Thread(target=run_target)
    worker.start()
    assert entered.wait(2)
    writer = threading.Thread(
        target=lambda: service.ingest_bot_channel_update(
            _channel_update(update_id=2, message_id=2, text="concurrent write"),
            realm_id=REALM,
        )
    )
    writer.start()
    writer.join(timeout=1)
    try:
        assert not writer.is_alive(), "writer blocked behind read-side scoring"
    finally:
        release.set()
        worker.join(timeout=5)
        writer.join(timeout=5)
    assert errors == []
