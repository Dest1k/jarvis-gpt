from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import jarvis_gpt.telemetry as telemetry_module
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.host_bridge import HostBridgeStatus
from jarvis_gpt.learning import LearningEngine
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.supervisor import RuntimeSupervisor, _evaluate_alerts
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


def test_learning_tick_deduplicates_lessons(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.create_approval(
        title="Repeatable host action",
        description="Needs review",
        requested_action="host.exec",
        risk="danger",
    )
    engine = LearningEngine(storage)

    first = engine.tick(limit=10)
    second = engine.tick(limit=10)

    assert first["lesson_count"] >= 1
    assert second["lesson_count"] == 0
    assert second["skipped_duplicates"] >= 1
    assert "consolidated" in second
    storage.close()


def test_learning_journal_survives_chat_deletion_and_feeds_lessons(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    conversation_id = storage.create_conversation("Learning source")
    storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="Запомни: мне важны тихие фоновые проверки без лишних вкладок.",
    )
    storage.record_tool_run(
        tool="web.search",
        ok=True,
        summary="Search completed",
        arguments={"query": "quiet background browsing"},
        data={"results": [{"url": "https://example.com"}]},
    )

    assert storage.delete_conversation(conversation_id) is True
    result = LearningEngine(storage).tick(limit=20)
    observations = storage.list_learning_observations(limit=20)

    assert result["examined"]["learning_observations"] >= 3
    assert any(item["kind"] == "conversation.message" for item in observations)
    assert any(item["kind"] == "conversation.deleted" for item in observations)
    assert any(item["kind"] == "tool.web.search" for item in observations)
    assert storage.search_memory("фоновые проверки", limit=10)
    storage.close()


def test_learning_tick_can_distill_with_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.record_learning_observation(
        kind="operator.feedback",
        summary="operator flagged answer",
        content="Too verbose and did not answer the actual admin task",
        payload={"rating": "down", "comment": "be shorter and finish the task"},
    )

    class FakeLLM:
        background = False

        @contextmanager
        def background_priority(self):
            self.background = True
            try:
                yield
            finally:
                self.background = False

        async def complete(self, *args, **kwargs):
            assert self.background is True
            return SimpleNamespace(
                ok=True,
                content=(
                    '{"lessons":[{"content":"When the operator flags verbosity, answer '
                    'briefly and close the concrete admin task first.",'
                    '"tags":["operator","brevity"],"importance":0.86}]}'
                ),
            )

    fake_llm = FakeLLM()
    fake_llm.settings = settings

    result = asyncio.run(LearningEngine(storage, llm=fake_llm).tick_async(limit=10))

    assert result["lesson_count"] >= 1
    assert any(
        "briefly and close" in item["content"]
        for item in storage.search_memory("briefly close admin task", limit=10)
    )
    storage.close()


def test_telemetry_performance_plan_and_host_bridge_status(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-turbo")
    ensure_runtime_dirs(settings)

    plan = TelemetryCollector(settings).performance_plan()
    bridge = HostBridgeStatus(settings).snapshot()

    assert plan["profile"] == "gemma4-turbo"
    assert plan["recommended_dispatcher"]["model_path"].endswith("gemma4-26b-a4b-nvfp4")
    assert plan["vllm_extra_args"]["language_model_only"] is False
    assert bridge["port"] == 8765
    assert bridge["script_available"] is True
    assert bridge["bundled_script_path"].replace("\\", "/").endswith(
        "scripts/windows_rpc_bridge.py"
    )


def test_live_telemetry_reuses_fast_gpu_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-turbo")
    ensure_runtime_dirs(settings)
    calls = {"gpu": 0}

    def fake_gpu_snapshot():
        calls["gpu"] += 1
        return {
            "available": True,
            "gpus": [
                {
                    "name": "Fake GPU",
                    "memory_used_ratio": 0.25,
                    "utilization_gpu": 42,
                }
            ],
        }

    monkeypatch.setattr(telemetry_module, "_nvidia_snapshot", fake_gpu_snapshot)
    collector = TelemetryCollector(settings)

    first = collector.live_snapshot()
    second = collector.live_snapshot()

    assert calls["gpu"] == 1
    assert first["gpu"]["gpus"][0]["utilization_gpu"] == 42
    assert second["gpu"]["gpus"][0]["memory_used_ratio"] == 0.25
    assert second["docker"]["deferred"] is True


def test_supervisor_status_reflects_autonomy_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    status = RuntimeSupervisor(settings=settings, storage=storage).status()

    assert status["enabled"] is False
    assert "telemetry.persist" in status["capabilities"]
    assert "health.persist" in status["capabilities"]
    assert "learning.deduplicate" in status["capabilities"]
    assert "cognition.background_pulse" in status["capabilities"]
    assert status["cognition_enabled"] is True
    assert status["cognition_interval_sec"] == 300
    assert "background.mission.runner" in status["capabilities"]
    assert status["health_interval_sec"] == 300
    assert status["mission_interval_sec"] == 120
    storage.close()


def test_supervisor_records_health_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    supervisor = RuntimeSupervisor(settings=settings, storage=storage)

    asyncio.run(supervisor._record_health())

    status = supervisor.status()
    assert status["last_health_at"] is not None
    assert storage.latest_health(limit=5)
    storage.close()


def test_supervisor_keeps_health_loop_running_when_autonomy_is_disabled(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    supervisor = RuntimeSupervisor(settings=settings, storage=storage)

    async def scenario():
        await supervisor.start()
        # Health, reminders AND self-healing are reliability/user-facing loops: all run
        # even with background autonomy off (self-healing keeps the local brain alive).
        names = {task.get_name() for task in supervisor._tasks}
        assert names == {
            "jarvis-health-loop",
            "jarvis-reminder-loop",
            "jarvis-self-heal-loop",
        }
        await supervisor.stop()

    asyncio.run(scenario())
    storage.close()


def test_health_recording_survives_observability_sink_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    supervisor = RuntimeSupervisor(settings=settings, storage=storage)

    def fail_event(**_kwargs):
        raise RuntimeError("event sink unavailable")

    monkeypatch.setattr(storage, "add_event", fail_event)
    asyncio.run(supervisor._record_health())

    assert supervisor.status()["last_health_at"] is not None
    assert supervisor.status()["last_health_attempt_ok"] is True
    assert storage.latest_complete_health(limit=20)
    storage.close()


def test_supervisor_background_cognition_persists_pulse(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.add_event(kind="test.signal", title="Operator asked for a living background brain")

    class FakeLLM:
        background = False

        @contextmanager
        def background_priority(self):
            self.background = True
            try:
                yield
            finally:
                self.background = False

        async def complete(self, _messages, **_kwargs):
            assert self.background is True
            return SimpleNamespace(
                ok=True,
                content=(
                    '{"summary":"Observed runtime and found one follow-up.",'
                    '"insights":["Keep background cognition observational."],'
                    '"questions":["Should proactive jobs be auto-created?"],'
                    '"suggested_jobs":[{"title":"Refresh learning","kind":"learning.tick",'
                    '"cadence":"background","priority":30,"payload":{"limit":20}}]}'
                ),
            )

    supervisor = RuntimeSupervisor(settings=settings, storage=storage, llm=FakeLLM())

    asyncio.run(supervisor._run_cognition())

    status = supervisor.status()
    pulse = storage.get_runtime_value("cognition.last_pulse")
    observations = storage.list_learning_observations(limit=5)
    assert status["last_cognition_at"] is not None
    assert pulse["summary"] == "Observed runtime and found one follow-up."
    assert pulse["suggested_jobs"][0]["kind"] == "learning.tick"
    assert any(item["kind"] == "cognition.pulse" for item in observations)


class _CaptureBus:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, payload: dict) -> None:
        self.published.append(payload)


def _snapshot(
    *, temp: float = 50.0, vram: float = 0.4, disk: float = 0.3, mem: float = 0.3
) -> dict:
    return {
        "gpu": {"available": True, "gpus": [{"name": "RTX 5090", "temperature_c": temp,
                                             "memory_used_ratio": vram}]},
        "disks": [{"path": "D:/", "used_ratio": disk, "free": 100 * 1024**3}],
        "memory": {"used_ratio": mem},
    }


def test_evaluate_alerts_flags_each_breached_threshold():
    breaches = _evaluate_alerts(
        _snapshot(temp=95.0, vram=0.99, disk=0.99, mem=0.99),
        gpu_temp_c=85.0,
        gpu_vram_ratio=0.97,
        disk_ratio=0.95,
        memory_ratio=0.95,
    )
    assert set(breaches) == {"gpu_temp", "gpu_vram", "disk", "memory"}
    assert breaches["disk"]["level"] == "error"  # >= 0.98 escalates
    # A calm box trips nothing.
    assert _evaluate_alerts(
        _snapshot(),
        gpu_temp_c=85.0,
        gpu_vram_ratio=0.97,
        disk_ratio=0.95,
        memory_ratio=0.95,
    ) == {}


def test_evaluate_alerts_tolerates_a_partial_snapshot():
    # nvidia-smi offline, no memory probe — only the disk breach survives.
    breaches = _evaluate_alerts(
        {"gpu": {"available": False}, "disks": [{"path": "D:/", "used_ratio": 0.96, "free": 0}]},
        gpu_temp_c=85.0,
        gpu_vram_ratio=0.97,
        disk_ratio=0.95,
        memory_ratio=0.95,
    )
    assert set(breaches) == {"disk"}


def test_health_alerts_are_edge_triggered_and_recover(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    bus = _CaptureBus()
    supervisor = RuntimeSupervisor(settings=settings, storage=storage, bus=bus)

    pushes: list[str] = []

    async def fake_push(text, **_kwargs):
        pushes.append(text)
        return True

    monkeypatch.setattr("jarvis_gpt.supervisor.push_telegram_alert", fake_push)

    async def scenario():
        await supervisor._check_health_alerts(_snapshot(temp=95.0))  # breach
        await supervisor._check_health_alerts(_snapshot(temp=95.0))  # still hot — no re-alert
        await supervisor._check_health_alerts(_snapshot(temp=50.0))  # recovered

    asyncio.run(scenario())

    alerts = [p for p in bus.published if p.get("action") == "alert"]
    fired = [p for p in alerts if not p["alert"]["recovered"]]
    recovered = [p for p in alerts if p["alert"]["recovered"]]
    assert len(fired) == 1  # edge-triggered: one alert for a standing breach
    assert len(recovered) == 1
    assert fired[0]["alert"]["key"] == "gpu_temp"
    assert len(pushes) == 2  # one breach push + one recovery push
    storage.close()


def test_health_alerts_can_be_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_HEALTH_ALERTS_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    bus = _CaptureBus()
    supervisor = RuntimeSupervisor(settings=settings, storage=storage, bus=bus)

    asyncio.run(supervisor._check_health_alerts(_snapshot(temp=99.0)))

    assert [p for p in bus.published if p.get("action") == "alert"] == []
    storage.close()
    storage.close()
