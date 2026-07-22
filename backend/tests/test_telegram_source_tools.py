from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from jarvis_gpt.authorization import LEGACY_OWNER_USER_ID, ActorContext, bind_actor
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.telegram_sources import (
    TelegramReaderBatch,
    TelegramReaderCapability,
    TelegramReaderPost,
    TelegramSourceAccessDenied,
    TelegramSourceService,
)
from jarvis_gpt.tools import ToolRegistry


class _NoopLLM:
    async def complete(self, *_args, **_kwargs):
        return SimpleNamespace(ok=True, content="{}")


def _actor(identity: dict[str, object], *, preset: str | None = None) -> ActorContext:
    return ActorContext(
        user_id=str(identity["user_id"]),
        preset_key=preset or str(identity["preset_key"]),
        source="test-session",
        identity_id=str(identity["identity_id"]),
        policy_epoch=int(identity["policy_epoch"]),
    )


def _runtime(monkeypatch, tmp_path, *, authorized_reader=None):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(
        settings,
        storage,
        _NoopLLM(),
        telegram_authorized_reader=authorized_reader,
    )
    return tools, storage


class _AuthorizedReader:
    def capability(self) -> TelegramReaderCapability:
        return TelegramReaderCapability(
            provider_name="test_reader",
            reader_identity_sha256="a" * 64,
            configured=True,
            authenticated=True,
            state="ready",
            supports_history=True,
            supports_media=True,
        )

    def read_history(
        self, source, *, limit: int, before_message_id: int | None = None
    ) -> TelegramReaderBatch:
        assert source.source_type == "supergroup"
        assert source.access_scope == "private"
        assert limit == 500
        assert before_message_id is None
        return TelegramReaderBatch(
            posts=(
                TelegramReaderPost(
                    message_id=41,
                    text="Reader history: 機密資料 and 비밀 기록.",
                    date=datetime(2026, 7, 20, 10, 30, tzinfo=UTC),
                    permalink="https://t.me/c/123/41",
                ),
            ),
            complete=True,
        )


class _DemotingAuthorizedReader(_AuthorizedReader):
    def __init__(self, demote) -> None:
        self._demote = demote

    def read_history(
        self, source, *, limit: int, before_message_id: int | None = None
    ) -> TelegramReaderBatch:
        self._demote()
        return super().read_history(
            source,
            limit=limit,
            before_message_id=before_message_id,
        )


class _DemotingCapabilityReader(_AuthorizedReader):
    def __init__(self, demote) -> None:
        self._demote = demote

    def capability(self) -> TelegramReaderCapability:
        capability = super().capability()
        self._demote()
        return capability


def _channel_update(*, update_id: int, message_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "channel_post": {
            "message_id": message_id,
            "date": 1_784_680_000,
            "chat": {
                "id": -1001234567890,
                "type": "channel",
                "title": "Global News",
                "username": "global_news",
            },
            "text": text,
        },
    }


def test_admin_tools_register_ingest_search_and_analyze_unicode_sources(
    monkeypatch, tmp_path
):
    tools, storage = _runtime(monkeypatch, tmp_path)
    admin = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="telegram-source-tools",
        provider_subject_id="admin",
        bootstrap_preset="admin",
    )

    with bind_actor(_actor(admin)):
        added = asyncio.run(
            tools.run(
                "telegram.sources.add",
                {
                    "realm_id": "telegram:777001",
                    "source_chat_id": -1001234567890,
                    "title": "Global News",
                    "username": "global_news",
                },
            )
        )
        assert added.ok is True
        source_id = added.data["source"]["id"]

        bridge_ingest = TelegramSourceService(
            storage.database_path,
            allow_bot_ingest=True,
        )
        ingested = bridge_ingest.ingest_bot_channel_update(
            _channel_update(
                update_id=10,
                message_id=20,
                text="Новости memory reliability. 秘密项目. 비밀 프로젝트. 秘密プロジェクト.",
            ),
            realm_id="telegram:777001",
        )
        assert ingested["inserted_versions"] == 1

        for query in ("Новости", "memory", "秘密项目", "비밀", "秘密プロジェクト"):
            result = asyncio.run(
                tools.run(
                    "telegram.sources.search",
                    {"query": query, "source_ids": [source_id]},
                )
            )
            assert result.ok is True
            assert result.data["hits"][0]["source_id"] == source_id
            assert result.data["hits"][0]["citation"].startswith("telegram:")

        cross_language = asyncio.run(
            tools.run(
                "telegram.sources.search",
                {
                    "query": "secret project",
                    "languages": ["zh"],
                    "translated_queries": {"zh": "秘密项目"},
                    "source_ids": [source_id],
                },
            )
        )
        assert cross_language.ok is True
        assert cross_language.data["hits"][0]["source_id"] == source_id
        assert cross_language.data["retrieval_mode"] == "multilingual_unicode_lexical"
        assert cross_language.data["language_coverage"]["translation_complete"] is True

        analyzed = asyncio.run(
            tools.run(
                "telegram.sources.analyze",
                {"source_ids": [source_id]},
            )
        )
        assert analyzed.ok is True
        assert analyzed.data["post_count"] == 1
        assert analyzed.data["items"][0]["provenance"]["source_chat_id"] == -1001234567890
    storage.close()


def test_telegram_source_tools_fail_closed_for_user_and_unsupported_account(
    monkeypatch, tmp_path
):
    tools, storage = _runtime(monkeypatch, tmp_path)
    ordinary = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="telegram-source-tools",
        provider_subject_id="ordinary",
        bootstrap_preset="user",
    )
    admin = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="telegram-source-tools",
        provider_subject_id="admin-2",
        bootstrap_preset="admin",
    )

    with bind_actor(_actor(ordinary)):
        denied = asyncio.run(tools.run("telegram.sources.list", {}))
        assert denied.ok is False
        assert denied.data["authorization_denied"] is True

    with bind_actor(_actor(admin)):
        unsupported = asyncio.run(
            tools.run(
                "telegram.sources.add",
                {
                    "source_type": "account",
                    "access_scope": "public",
                    "username": "some_account",
                },
            )
        )
        assert unsupported.ok is False
        assert unsupported.data["persisted"] is False
        assert unsupported.data["capability"]["state"] == "bot_api_account_feed_unavailable"
        assert asyncio.run(tools.run("telegram.sources.list", {})).data["count"] == 0

    forged = _actor(ordinary, preset="admin")
    with pytest.raises(TelegramSourceAccessDenied):
        tools.telegram_sources.list(forged)
    storage.close()


def test_authorized_reader_is_injected_without_credentials_and_syncs_history(
    monkeypatch, tmp_path
):
    tools, storage = _runtime(
        monkeypatch,
        tmp_path,
        authorized_reader=_AuthorizedReader(),
    )
    admin = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="telegram-reader-tools",
        provider_subject_id="admin-reader",
        bootstrap_preset="admin",
    )

    with bind_actor(_actor(admin)):
        capability = asyncio.run(
            tools.run(
                "telegram.sources.capability",
                {
                    "provider": "authorized_reader",
                    "source_type": "supergroup",
                    "access_scope": "private",
                    "source_chat_id": -1009876543210,
                },
            )
        )
        assert capability.ok is True
        assert capability.data["state"] == "authorized_reader_available"
        assert "reader_identity_sha256" not in capability.data

        added = asyncio.run(
            tools.run(
                "telegram.sources.add",
                {
                    "provider": "authorized_reader",
                    "realm_id": "telegram:777001",
                    "source_type": "supergroup",
                    "access_scope": "private",
                    "source_chat_id": -1009876543210,
                    "title": "Private Research",
                },
            )
        )
        assert added.ok is True
        source_id = added.data["source"]["id"]
        assert added.data["source"]["provider_name"] == "test_reader"

        synced = asyncio.run(
            tools.run("telegram.sources.sync", {"source_id": source_id})
        )
        assert synced.ok is True
        assert synced.data["state"] == "history_synced"

        found = asyncio.run(
            tools.run(
                "telegram.sources.search",
                {"query": "機密資料", "source_ids": [source_id]},
            )
        )
        assert found.ok is True
        assert found.data["hits"][0]["text"].startswith("Reader history")

    storage.close()


def test_authorized_reader_sync_persists_nothing_after_concurrent_demotion(
    monkeypatch, tmp_path
):
    runtime: dict[str, object] = {}

    def demote() -> None:
        tools = runtime["tools"]
        admin = runtime["admin"]
        tools.permissions.assign_preset(
            user_id=str(admin["user_id"]),
            preset_key="user",
            assigned_by=LEGACY_OWNER_USER_ID,
            reason="test Telegram reader revocation window",
        )

    tools, storage = _runtime(
        monkeypatch,
        tmp_path,
        authorized_reader=_DemotingAuthorizedReader(demote),
    )
    admin = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="telegram-reader-revocation",
        provider_subject_id="admin-reader",
        bootstrap_preset="admin",
    )
    runtime.update(tools=tools, admin=admin)
    with bind_actor(_actor(admin)):
        added = asyncio.run(
            tools.run(
                "telegram.sources.add",
                {
                    "provider": "authorized_reader",
                    "realm_id": "telegram:777001",
                    "source_type": "supergroup",
                    "access_scope": "private",
                    "source_chat_id": -1009876543210,
                },
            )
        )
        result = asyncio.run(
            tools.run(
                "telegram.sources.sync",
                {"source_id": added.data["source"]["id"]},
            )
        )

    assert result.ok is False
    with storage.locked_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM telegram_reader_posts").fetchone()[0] == 0
    storage.close()


def test_authorized_reader_add_persists_nothing_after_capability_demotion(
    monkeypatch, tmp_path
):
    runtime: dict[str, object] = {}

    def demote() -> None:
        tools = runtime["tools"]
        admin = runtime["admin"]
        tools.permissions.assign_preset(
            user_id=str(admin["user_id"]),
            preset_key="user",
            assigned_by=LEGACY_OWNER_USER_ID,
            reason="test Telegram reader capability revocation window",
        )

    tools, storage = _runtime(
        monkeypatch,
        tmp_path,
        authorized_reader=_DemotingCapabilityReader(demote),
    )
    admin = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="telegram-reader-add-revocation",
        provider_subject_id="admin-reader",
        bootstrap_preset="admin",
    )
    runtime.update(tools=tools, admin=admin)

    with bind_actor(_actor(admin)):
        result = asyncio.run(
            tools.run(
                "telegram.sources.add",
                {
                    "provider": "authorized_reader",
                    "realm_id": "telegram:777001",
                    "source_type": "supergroup",
                    "access_scope": "private",
                    "source_chat_id": -1009876543210,
                },
            )
        )

    assert result.ok is False
    with storage.locked_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM telegram_reader_sources").fetchone()[0] == 0
    storage.close()
