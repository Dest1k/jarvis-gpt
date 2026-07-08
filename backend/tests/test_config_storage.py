from __future__ import annotations

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
