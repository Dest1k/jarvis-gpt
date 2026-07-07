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
    assert settings.model_dir.name == "gemma4-mono"


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
