from __future__ import annotations

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.host_bridge import HostBridgeStatus
from jarvis_gpt.learning import LearningEngine
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.telemetry import TelemetryCollector


def test_learning_tick_saves_lessons_from_pending_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.create_approval(
        title="Dangerous host action",
        description="Needs review",
        requested_action="host.exec",
        risk="danger",
    )

    result = LearningEngine(storage).tick(limit=10)

    assert result["lesson_count"] >= 1
    assert storage.search_memory("approval gate", limit=5)
    storage.close()


def test_telemetry_performance_plan_and_host_bridge_status(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-turbo")
    ensure_runtime_dirs(settings)

    plan = TelemetryCollector(settings).performance_plan()
    bridge = HostBridgeStatus(settings).snapshot()

    assert plan["profile"] == "gemma4-turbo"
    assert plan["recommended_dispatcher"]["model_path"].endswith("gemma4-26b-a4b-nvfp4")
    assert bridge["port"] == 8765
