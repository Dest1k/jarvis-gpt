from __future__ import annotations

from pathlib import Path

from jarvis_gpt.authorization import (
    LEGACY_OWNER_USER_ID,
    ActorContext,
    bind_actor,
)
from jarvis_gpt.storage import JarvisStorage, _sqlite_fts_tokenizer, utc_now

_LANGUAGE_CASES = (
    ("ru", "Архивное сообщение хранит янтарный компас.", "янтарный компас"),
    ("en", "The archived answer preserves a sapphire ledger.", "sapphire ledger"),
    ("zh", "旧消息完整保存龙舟计划和来源。", "龙舟计划"),
    ("ko", "오래된 답변은 달빛계획과 출처를 보존합니다.", "달빛계획"),
    ("ja", "古いメッセージは桜計画と出典を保存します。", "桜計画"),
)


def _assert_multilingual_hits(
    storage: JarvisStorage,
    message_ids: dict[str, str],
    memory_ids: dict[str, str],
) -> None:
    for language, _content, query in _LANGUAGE_CASES:
        message_hits = storage.search_messages(query, limit=5)
        assert message_ids[language] in {item["id"] for item in message_hits}
        assert all(item["role"] in {"user", "assistant"} for item in message_hits)

        memory_hits = storage.search_memory(query, limit=5)
        assert memory_ids[language] in {item["id"] for item in memory_hits}

        chunk_hits = storage.search_file_chunks(query, limit=5)
        assert any(query in item["content"] for item in chunk_hits)


def test_trigram_search_covers_old_ru_en_zh_ko_ja_rows_after_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "jarvis.sqlite3"
    storage = JarvisStorage(database_path)
    storage.initialize()
    conversation_id = storage.create_conversation("Multilingual archive")

    message_ids: dict[str, str] = {}
    memory_ids: dict[str, str] = {}
    chunks: list[str] = []
    for index, (language, content, query) in enumerate(_LANGUAGE_CASES):
        message_ids[language] = storage.add_message(
            conversation_id=conversation_id,
            role="user" if index % 2 == 0 else "assistant",
            content=content,
            metadata={"language": language},
        )
        memory_ids[language] = storage.add_memory(
            namespace="multilingual-regression",
            content=content,
            tags=[language, "search-regression"],
            importance=0.8,
        )["id"]
        chunks.append(f"{language}: {content} Search key: {query}")

    file_record = storage.create_file_record(
        name="multilingual-corpus.txt",
        stored_path=tmp_path / "multilingual-corpus.txt",
        sha256="1" * 64,
        size=1024,
        mime_type="text/plain",
        status="indexed",
        chunk_count=len(chunks),
    )
    storage.add_file_chunks(file_record["id"], chunks, status="indexed")

    # The target rows precede the historical 60-row candidate window that the old
    # message search used. FTS must still find them by relevance.
    for index in range(96):
        storage.add_message(
            conversation_id=conversation_id,
            role="user" if index % 2 == 0 else "assistant",
            content=f"Unrelated recent filler message {index:03d}.",
        )

    _assert_multilingual_hits(storage, message_ids, memory_ids)
    assert storage.search_recent_user_messages("sapphire ledger", limit=5) == []
    assert message_ids["ru"] in {
        item["id"] for item in storage.search_recent_user_messages("янтарный компас", limit=5)
    }
    storage.close()

    reopened = JarvisStorage(database_path)
    reopened.initialize()
    _assert_multilingual_hits(reopened, message_ids, memory_ids)
    reopened.close()


def test_message_fts_tracks_edit_soft_delete_and_conversation_delete(tmp_path: Path) -> None:
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    conversation_id = storage.create_conversation("Short Unicode")
    message_id = storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="短い印には龍があります。",
    )

    assert [item["id"] for item in storage.search_messages("龍", limit=5)] == [message_id]
    storage.edit_message(message_id, "수정된 짧은 표식에는 별이 있습니다.")
    assert storage.search_messages("龍", limit=5) == []
    assert [item["id"] for item in storage.search_messages("별", limit=5)] == [message_id]
    indexed = (
        storage.connect()
        .execute("SELECT content FROM messages_fts WHERE id = ?", (message_id,))
        .fetchall()
    )
    assert [row["content"] for row in indexed] == ["수정된 짧은 표식에는 별이 있습니다."]

    assert storage.delete_message(message_id) is True
    assert storage.search_messages("별", limit=5) == []
    assert (
        storage.connect()
        .execute("SELECT COUNT(*) FROM messages_fts WHERE id = ?", (message_id,))
        .fetchone()[0]
        == 0
    )

    assistant_id = storage.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content="桜",
    )
    assert storage.search_messages("桜", roles=("user",), limit=5) == []
    assert [
        item["id"] for item in storage.search_messages("桜", roles=("assistant",), limit=5)
    ] == [assistant_id]
    assert storage.delete_conversation(conversation_id) is True
    assert storage.search_messages("桜", limit=5) == []
    assert (
        storage.connect()
        .execute("SELECT COUNT(*) FROM messages_fts WHERE id = ?", (assistant_id,))
        .fetchone()[0]
        == 0
    )
    storage.close()


def test_short_unicode_fallback_covers_message_memory_and_file_indexes(tmp_path: Path) -> None:
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    conversation_id = storage.create_conversation("Short Unicode indexes")
    message_id = storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="代号龍",
    )
    memory_id = storage.add_memory(
        namespace="short-unicode",
        content="代号龍",
        tags=["zh"],
    )["id"]
    file_record = storage.create_file_record(
        name="short-unicode.txt",
        stored_path=tmp_path / "short-unicode.txt",
        sha256="3" * 64,
        size=16,
        mime_type="text/plain",
        status="indexed",
        chunk_count=1,
    )
    storage.add_file_chunks(file_record["id"], ["代号龍"], status="indexed")

    assert [item["id"] for item in storage.search_messages("龍", limit=5)] == [message_id]
    assert memory_id in {item["id"] for item in storage.search_memory("龍", limit=5)}
    assert any(item["content"] == "代号龍" for item in storage.search_file_chunks("龍", limit=5))
    storage.close()


def test_unicode_casefold_and_mixed_short_long_terms_are_merged(tmp_path: Path) -> None:
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    conversation_id = storage.create_conversation("Mixed Unicode")
    message_id = storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="ИИ roadmap marker",
    )
    memory_id = storage.add_memory(
        namespace="unicode-casefold",
        content="ИИ research note",
    )["id"]
    first = storage.create_file_record(
        name="secret-only.txt",
        stored_path=tmp_path / "secret-only.txt",
        sha256="4" * 64,
        size=20,
        mime_type="text/plain",
        status="indexed",
        chunk_count=1,
    )
    storage.add_file_chunks(first["id"], ["短い秘密だけが含まれます"], status="indexed")
    second = storage.create_file_record(
        name="launch-only.txt",
        stored_path=tmp_path / "launch-only.txt",
        sha256="5" * 64,
        size=20,
        mime_type="text/plain",
        status="indexed",
        chunk_count=1,
    )
    storage.add_file_chunks(second["id"], ["launch schedule only"], status="indexed")

    assert message_id in {item["id"] for item in storage.search_messages("ии", limit=10)}
    assert memory_id in {item["id"] for item in storage.search_memory("ии", limit=10)}
    mixed_hits = storage.search_file_chunks("秘密 launch", limit=10)
    assert {first["id"], second["id"]} <= {
        str(item["file_id"]) for item in mixed_hits
    }
    storage.close()


def test_fts_ensure_is_noop_after_current_schema_marker(tmp_path: Path) -> None:
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    conversation_id = storage.create_conversation("Stable FTS")
    storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="stable index marker",
    )
    storage.add_memory(namespace="stable", content="stable index marker")
    file_record = storage.create_file_record(
        name="stable.txt",
        stored_path=tmp_path / "stable.txt",
        sha256="6" * 64,
        size=20,
        mime_type="text/plain",
        status="indexed",
        chunk_count=1,
    )
    storage.add_file_chunks(file_record["id"], ["stable index marker"], status="indexed")
    conn = storage.connect()
    before_markers = conn.execute(
        "SELECT name, schema_version, rebuilt_at FROM fts_index_metadata ORDER BY name"
    ).fetchall()
    before_changes = conn.total_changes

    assert storage._ensure_messages_fts(conn) is True
    assert storage._ensure_memory_fts(conn) is True
    assert storage._ensure_file_chunks_fts(conn) is True

    after_markers = conn.execute(
        "SELECT name, schema_version, rebuilt_at FROM fts_index_metadata ORDER BY name"
    ).fetchall()
    assert [tuple(row) for row in after_markers] == [tuple(row) for row in before_markers]
    assert conn.total_changes == before_changes
    storage.close()


def test_search_messages_is_tenant_scoped_by_default(tmp_path: Path) -> None:
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    now = utc_now()
    second_user_id = "usr_multilingual_search_tenant"
    storage.connect().execute(
        """
        INSERT INTO users(
            id, status, display_name, locale, policy_epoch,
            created_at, updated_at, first_seen_at, last_seen_at
        ) VALUES (?, 'active', 'Second tenant', 'ja', 1, ?, ?, ?, ?)
        """,
        (second_user_id, now, now, now, now),
    )
    storage.connect().commit()

    owner_actor = ActorContext(
        user_id=LEGACY_OWNER_USER_ID,
        preset_key="owner",
        source="test",
    )
    second_actor = ActorContext(
        user_id=second_user_id,
        preset_key="admin",
        source="test",
    )
    with bind_actor(owner_actor):
        owner_conversation = storage.create_conversation("Owner")
        owner_message = storage.add_message(
            conversation_id=owner_conversation,
            role="user",
            content="tenant boundary marker 共有境界",
        )
    with bind_actor(second_actor):
        second_conversation = storage.create_conversation("Second")
        second_message = storage.add_message(
            conversation_id=second_conversation,
            role="assistant",
            content="tenant boundary marker 共有境界",
        )
        assert [item["id"] for item in storage.search_messages("共有境界", limit=10)] == [
            second_message
        ]
    with bind_actor(owner_actor):
        assert [item["id"] for item in storage.search_messages("共有境界", limit=10)] == [
            owner_message
        ]
    storage.close()


def test_initialize_detects_and_rebuilds_legacy_fts_tokenizers(tmp_path: Path) -> None:
    database_path = tmp_path / "state" / "jarvis.sqlite3"
    storage = JarvisStorage(database_path)
    storage.initialize()
    conversation_id = storage.create_conversation("Legacy FTS")
    message_id = storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="旧索引にも銀河移行マーカーがあります。",
    )
    memory_id = storage.add_memory(
        namespace="migration",
        content="旧索引にも銀河移行マーカーがあります。",
        tags=["migration"],
    )["id"]
    file_record = storage.create_file_record(
        name="legacy-index.txt",
        stored_path=tmp_path / "legacy-index.txt",
        sha256="2" * 64,
        size=128,
        mime_type="text/plain",
        status="indexed",
        chunk_count=1,
    )
    storage.add_file_chunks(
        file_record["id"],
        ["旧索引にも銀河移行マーカーがあります。"],
        status="indexed",
    )

    conn = storage.connect()
    for trigger_name in (
        "messages_fts_after_insert",
        "messages_fts_after_update",
        "messages_fts_after_delete",
    ):
        conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')
    conn.execute("DROP TABLE messages_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE messages_fts
        USING fts5(id UNINDEXED, user_id UNINDEXED, conversation_id UNINDEXED, role, content)
        """
    )
    conn.execute("DROP TABLE memories_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE memories_fts
        USING fts5(id UNINDEXED, user_id UNINDEXED, namespace, content, tags)
        """
    )
    conn.execute("DROP TABLE file_chunks_fts")
    conn.execute(
        """
        CREATE VIRTUAL TABLE file_chunks_fts
        USING fts5(file_id UNINDEXED, chunk_id UNINDEXED, user_id UNINDEXED, content)
        """
    )
    conn.commit()
    storage.close()

    reopened = JarvisStorage(database_path)
    reopened.initialize()
    for table in ("messages_fts", "memories_fts", "file_chunks_fts"):
        assert _sqlite_fts_tokenizer(reopened.connect(), table) == "trigram"
    assert message_id in {item["id"] for item in reopened.search_messages("銀河移行", limit=5)}
    assert memory_id in {item["id"] for item in reopened.search_memory("銀河移行", limit=5)}
    assert any("銀河移行" in item["content"] for item in reopened.search_file_chunks("銀河移行"))
    reopened.close()
