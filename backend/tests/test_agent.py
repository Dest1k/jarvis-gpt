from __future__ import annotations

import asyncio

from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage


def test_agent_creates_mission_from_large_goal(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(
        agent.chat(
            "Сделай проект с нуля: полностью переосмысли архитектуру, реализуй runtime, память, "
            "диагностику, web интерфейс и mission plan для локального Jarvis.",
            mode="auto",
        )
    )

    assert response.mission_id is not None
    assert "mission plan" in response.answer
    assert storage.counters()["mission_tasks"] >= 4
    storage.close()
