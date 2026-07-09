from __future__ import annotations

from pathlib import Path

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.storage import JarvisStorage


def test_settings_use_external_home(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_PROFILE", "gemma4-mono")

    settings = load_settings()
    ensure_runtime_dirs(settings)

    assert settings.home == tmp_path
    assert settings.database_path.parent.exists()
    assert settings.model_root == tmp_path / "models"
    assert settings.model_dir.name == "gemma4-31b-it-nvfp4"


def test_storage_persists_mission(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    mission = storage.create_mission(
        title="Build runtime",
        goal="Create a local-first runtime",
        tasks=["Design", "Implement", "Verify"],
    )

    assert mission["title"] == "Build runtime"
    assert len(mission["tasks"]) == 3
    assert storage.counters()["missions"] == 1
    storage.close()


def test_storage_creates_consistent_database_backup(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    storage.add_memory(content="Backup me", namespace="runtime")

    backup = storage.backup_database()
    second_backup = storage.backup_database()

    backup_path = Path(backup["path"])
    assert backup["ok"] is True
    assert second_backup["path"] != backup["path"]
    assert backup_path.exists()
    assert backup_path.stat().st_size > 0
    clone = JarvisStorage(backup_path)
    clone.initialize()
    assert clone.counters()["memories"] == 1
    clone.close()
    storage.close()


def test_storage_lists_conversations_and_messages(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    conversation_id = storage.create_conversation("History")
    user_id = storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="remember this",
    )
    assistant_id = storage.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content="remembered",
    )

    conversations = storage.list_conversations()
    messages = storage.list_messages(conversation_id)

    assert conversations[0]["id"] == conversation_id
    assert conversations[0]["message_count"] == 2
    assert [message["id"] for message in messages] == [user_id, assistant_id]
    assert storage.get_conversation(conversation_id)["title"] == "History"
    storage.close()


def test_storage_get_message_by_id_preserves_metadata(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    conversation_id = storage.create_conversation("Trace")
    message_id = storage.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content="observable answer",
        metadata={"duration_ms": 123, "events": [{"type": "thought", "title": "route"}]},
    )

    message = storage.get_message(message_id)

    assert message is not None
    assert message["id"] == message_id
    assert message["conversation_id"] == conversation_id
    assert message["metadata"]["duration_ms"] == 123
    assert message["metadata"]["events"][0]["title"] == "route"
    assert storage.get_message("msg_missing") is None
    storage.close()


def test_storage_deletes_conversation_and_messages(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    conversation_id = storage.create_conversation("Temporary")
    storage.add_message(conversation_id=conversation_id, role="user", content="clear me")
    storage.add_message(conversation_id=conversation_id, role="assistant", content="cleared")

    deleted = storage.delete_conversation(conversation_id)

    assert deleted is True
    assert storage.get_conversation(conversation_id) is None
    assert storage.list_messages(conversation_id) == []
    assert storage.delete_conversation(conversation_id) is False
    storage.close()


def test_storage_updates_task_progress_and_searches_memory(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    mission = storage.create_mission(
        title="Memory mission",
        goal="Improve long-term memory",
        tasks=["Index memory", "Verify search"],
    )
    first_task = mission["tasks"][0]

    updated = storage.update_mission_task(first_task["id"], status="done", notes="Indexed")
    refreshed = storage.get_mission(mission["id"])
    memory = storage.add_memory(
        content="Jarvis memory uses SQLite FTS for local search.",
        namespace="runtime",
        tags=["memory", "fts"],
        importance=0.8,
    )
    hits = storage.search_memory("SQLite FTS", limit=5)

    assert updated is not None
    assert refreshed is not None
    assert refreshed["progress"] == 0.5
    assert memory["id"] in {item["id"] for item in hits}
    assert hits[0]["relevance"] > 0
    assert hits[0]["matched_terms"] == ["SQLite", "FTS"]
    assert "SQLite FTS" in hits[0]["snippet"]
    storage.close()


def test_storage_update_mission_task_requires_matching_mission_when_provided(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    first = storage.create_mission(
        title="First mission",
        goal="Keep task ownership isolated",
        tasks=["Owned task"],
    )
    second = storage.create_mission(
        title="Second mission",
        goal="Should not update first mission",
        tasks=["Other task"],
    )
    first_task = first["tasks"][0]

    rejected = storage.update_mission_task(
        first_task["id"],
        mission_id=second["id"],
        status="done",
        notes="wrong mission",
    )
    unchanged = storage.get_mission(first["id"])["tasks"][0]
    accepted = storage.update_mission_task(
        first_task["id"],
        mission_id=first["id"],
        status="done",
        notes="right mission",
    )

    assert rejected is None
    assert unchanged["status"] == "pending"
    assert unchanged["notes"] is None
    assert accepted is not None
    assert accepted["status"] == "done"
    assert accepted["notes"] == "right mission"
    storage.close()


def test_storage_merges_duplicate_memories_and_hybrid_search(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    first = storage.add_memory(
        content="LLM models live in D:\\jarvis\\models.",
        namespace="environment",
        tags=["models"],
        importance=0.5,
    )
    second = storage.add_memory(
        content="LLM models live in D:\\jarvis\\models.",
        namespace="environment",
        tags=["paths"],
        importance=0.8,
    )
    storage.add_memory(
        content="Operator prefers concise status updates.",
        namespace="preferences",
        tags=["operator"],
        importance=0.7,
    )

    hits = storage.search_memory("where are llm models stored jarvis", limit=5)

    assert first["id"] == second["id"]
    assert second["importance"] == 0.8
    assert {"models", "paths"}.issubset(set(second["tags"]))
    assert hits[0]["namespace"] == "environment"
    assert "D:\\jarvis\\models" in hits[0]["content"]
    storage.close()


def test_storage_mirrors_memory_to_obsidian_like_vault(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    memory = storage.add_memory(
        content="Jarvis should connect [[LLM runtime]] with [[GPU telemetry]] #runtime",
        namespace="architecture",
        tags=["jarvis", "graph"],
        importance=0.9,
    )
    graph = storage.memory_graph()

    note_path = storage.memory_vault.root / "architecture" / f"{memory['id']}.md"
    assert note_path.exists()
    note = note_path.read_text(encoding="utf-8")
    assert "namespace: \"architecture\"" in note
    assert "[[LLM runtime]]" in note
    assert graph["stats"]["notes"] == 1
    assert any(edge["target"] == "link:LLM runtime" for edge in graph["edges"])
    assert any(edge["target"] == "tag:runtime" for edge in graph["edges"])
    assert memory["id"] in graph["backlinks"]["LLM runtime"]
    storage.close()


def test_storage_consolidates_existing_duplicate_memories(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    storage.add_memory(content="Use LAN launch mode by default.", namespace="instructions")
    with storage.connect() as conn:
        conn.execute(
            """
            INSERT INTO memories(id, namespace, content, tags, importance, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "mem_duplicate",
                "instructions",
                "Use LAN launch mode by default.",
                '["legacy"]',
                0.9,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()
    result = storage.consolidate_memories()
    hits = storage.search_memory("LAN launch mode", limit=10, namespaces=["instructions"])

    assert result["removed"] == 1
    assert len(hits) == 1
    assert "legacy" in hits[0]["tags"]
    storage.close()


def test_storage_records_approval_gate(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    approval = storage.create_approval(
        title="Apply host patch",
        description="Needs operator review before changing host state.",
        requested_action="host.patch",
        risk="danger",
        payload={"path": "D:/jarvis"},
    )
    updated = storage.update_approval(
        approval["id"],
        status="approved",
        result={"operator": "test"},
    )
    audit = storage.list_audit(target_type="approval", target_id=approval["id"])

    assert updated is not None
    assert updated["status"] == "approved"
    assert storage.counters()["approvals"] == 1
    assert {entry["action"] for entry in audit} == {"approval.request", "approval.update"}
    storage.close()


def test_storage_persists_runtime_values(tmp_path):
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()

    saved = storage.set_runtime_value("experience.preferences", {"operator_name": "Tony"})
    loaded = storage.get_runtime_value("experience.preferences", {})
    rows = storage.list_runtime_values("experience.")

    assert saved["key"] == "experience.preferences"
    assert loaded == {"operator_name": "Tony"}
    assert rows[0]["value"] == {"operator_name": "Tony"}
    assert storage.counters()["runtime_kv"] == 1
    storage.close()
