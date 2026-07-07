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
