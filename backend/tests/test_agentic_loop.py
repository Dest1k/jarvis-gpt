from __future__ import annotations

import asyncio

from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMStreamChunk
from jarvis_gpt.storage import JarvisStorage


def _result(content: str, ok: bool = True):
    return type("Result", (), {"ok": ok, "content": content, "error": None})()


def _agent(monkeypatch, tmp_path, llm):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, bus=EventBus())
    return agent, storage


def test_agentic_loop_runs_safe_tool_then_answers(monkeypatch, tmp_path):
    class ToolThenAnswerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result('{"tool": "web.search", "arguments": {"query": "kazan weather"}}')
            return _result("По собранным данным: в Казани ясно.")

    llm = ToolThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        captured["tool"] = name
        captured["arguments"] = arguments
        return type(
            "R",
            (),
            {
                "tool": name,
                "ok": True,
                "summary": "Web search returned 1 result(s).",
                "data": {"results": [{"title": "t", "url": "u", "snippet": "clear sky"}]},
            },
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("подскажи по погоде, используй что нужно"))

    assert llm.calls == 2
    assert captured["tool"] == "web.search"
    assert captured["arguments"]["query"] == "kazan weather"
    assert response.answer == "По собранным данным: в Казани ясно."
    assert any(
        event.type == "tool_call" and event.payload.get("autonomous")
        for event in response.events
    )
    storage.close()


def test_agentic_loop_gates_dangerous_tool_with_approval(monkeypatch, tmp_path):
    class DangerThenAnswerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(
                    '{"tool": "host.bridge.execute", "arguments": {"command": "Get-Date"}}'
                )
            return _result("Нужно ваше подтверждение, чтобы выполнить команду на хосте.")

    llm = DangerThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)

    async def fail_run(name, arguments=None, **kwargs):
        raise AssertionError(f"dangerous tool {name} must not run autonomously")

    monkeypatch.setattr(agent.tools, "run", fail_run)

    response = asyncio.run(agent.chat("посмотри дату на хосте"))

    assert llm.calls == 2
    assert response.answer.startswith("Нужно ваше подтверждение")
    pending = storage.list_approvals(limit=10, status="pending")
    assert len(pending) == 1
    assert pending[0]["requested_action"] == "tool.run"
    assert any(event.type == "approval" for event in response.events)
    storage.close()


def test_agentic_loop_stops_at_step_budget(monkeypatch, tmp_path):
    # Model keeps asking for a tool; loop must force a final answer at the budget.
    class AlwaysToolLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            if "Лимит шагов" in system:
                return _result("Финальный ответ после лимита.")
            return _result('{"tool": "web.search", "arguments": {"query": "x"}}')

    llm = AlwaysToolLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    agent.storage.set_runtime_value("experience.autonomy_policy", {"max_autonomous_steps": 2})

    async def fake_run(name, arguments=None, **kwargs):
        return type(
            "R",
            (),
            {"tool": name, "ok": True, "summary": "ok", "data": {"results": []}},
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("собери данные и ответь"))

    assert response.answer == "Финальный ответ после лимита."
    # Two tool rounds then a forced final answer = 3 completions.
    assert llm.calls == 3
    storage.close()


def test_agentic_stream_suppresses_tool_json_and_streams_answer(monkeypatch, tmp_path):
    class StreamToolThenAnswerLLM:
        def __init__(self) -> None:
            self.rounds = 0

        async def stream_complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.rounds += 1
            if self.rounds == 1:
                for piece in ['{"tool": "web.search",', ' "arguments": {"query": "x"}}']:
                    yield LLMStreamChunk(kind="delta", content=piece)
                yield LLMStreamChunk(kind="done", finish_reason="stop")
            else:
                for piece in ["Готово: ", "нашёл ответ."]:
                    yield LLMStreamChunk(kind="delta", content=piece)
                yield LLMStreamChunk(kind="done", finish_reason="stop")

    llm = StreamToolThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)

    async def fake_run(name, arguments=None, **kwargs):
        return type(
            "R",
            (),
            {"tool": name, "ok": True, "summary": "ok", "data": {"results": [{"title": "t"}]}},
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    async def collect():
        deltas = []
        events = []
        done = None
        async for message in agent.stream_chat("собери и ответь"):
            if message["type"] == "delta":
                deltas.append(message["content"])
            elif message["type"] == "event":
                events.append(message["event"])
            elif message["type"] == "done":
                done = message
        return deltas, events, done

    deltas, events, done = asyncio.run(collect())
    streamed = "".join(deltas)

    assert "tool" not in streamed  # the JSON tool call must not leak to the user
    assert "Готово: нашёл ответ." in streamed
    assert done["answer"] == "Готово: нашёл ответ."
    assert any(event.get("type") == "tool_call" for event in events)
    storage.close()


def test_mission_step_executes_with_tools_when_llm_enabled(monkeypatch, tmp_path):
    class MissionToolThenReportLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result('{"tool": "runtime.status", "arguments": {}}')
            return _result("Шаг выполнен: проверил статус рантайма. Осталось: ничего.")

    llm = MissionToolThenReportLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        captured["tool"] = name
        return type(
            "R",
            (),
            {"tool": name, "ok": True, "summary": "runtime ok", "data": {"profile": "turbo"}},
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)
    mission = agent.create_mission("Проверить рантайм и отчитаться")

    response = asyncio.run(agent.execute_next_mission_step(mission["id"]))

    assert response.result.ok is True
    assert response.task is not None
    assert response.task.status == "done"
    assert response.result.data["tool_steps"] == 1
    assert response.result.data["autonomous"] is True
    assert "Шаг выполнен" in response.result.summary
    assert captured["tool"] == "runtime.status"
    storage.close()


def test_agentic_stream_plain_answer_has_no_regression(monkeypatch, tmp_path):
    class PlainStreamLLM:
        async def stream_complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            for piece in ["Привет", ", чем помочь?"]:
                yield LLMStreamChunk(kind="delta", content=piece)
            yield LLMStreamChunk(kind="done", finish_reason="stop")

    agent, storage = _agent(monkeypatch, tmp_path, PlainStreamLLM())

    async def collect():
        deltas = []
        async for message in agent.stream_chat("привет"):
            if message["type"] == "delta":
                deltas.append(message["content"])
        return deltas

    deltas = asyncio.run(collect())
    assert "".join(deltas) == "Привет, чем помочь?"
    storage.close()
