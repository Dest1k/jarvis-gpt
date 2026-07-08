from __future__ import annotations

import asyncio

from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter, LLMStreamChunk
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
            "Сделай проект с нуля: полностью переосмысли архитектуру, реализуй runtime, "
            "память, диагностику, web интерфейс и mission plan для локального Jarvis.",
            mode="auto",
        )
    )

    assert response.mission_id is not None
    assert "mission plan" in response.answer
    assert storage.counters()["mission_tasks"] >= 4
    storage.close()


def test_agent_executes_next_mission_step(monkeypatch, tmp_path):
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
    mission = agent.create_mission("Build tools runtime")

    result = asyncio.run(agent.execute_next_mission_step(mission["id"]))
    refreshed = storage.get_mission(mission["id"])
    runs = storage.list_tool_runs()

    assert result.result.ok is True
    assert result.task is not None
    assert result.task.status == "done"
    assert refreshed is not None
    assert refreshed["progress"] > 0
    assert runs[0]["tool"] == "mission.brief"
    storage.close()


def test_agent_streams_chat_response(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = FakeStreamingLLM()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=llm,
        bus=EventBus(),
    )

    items = asyncio.run(_collect(agent.stream_chat("hello", mode="chat", max_tokens=32)))
    deltas = [item["content"] for item in items if item["type"] == "delta"]
    done = next(item for item in items if item["type"] == "done")
    messages = storage.recent_messages(done["conversation_id"], limit=5)

    assert deltas == ["Hello", " world"]
    assert done["answer"] == "Hello world"
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "Hello world"
    assert llm.max_tokens == 32
    storage.close()


class FakeStreamingLLM:
    def __init__(self) -> None:
        self.max_tokens: int | None = None

    async def stream_complete(self, messages, *, temperature=None, max_tokens=None):
        self.max_tokens = max_tokens
        yield LLMStreamChunk(kind="delta", content="Hello")
        yield LLMStreamChunk(kind="delta", content=" world")


async def _collect(stream):
    return [item async for item in stream]
