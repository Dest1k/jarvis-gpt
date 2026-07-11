from __future__ import annotations

import asyncio
import base64
import json

from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.approval_executor import ApprovalExecutor
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.dispatcher import DispatcherManager
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.executive_runtime import ExecutiveCoordinator
from jarvis_gpt.llm import LLMStreamChunk
from jarvis_gpt.storage import JarvisStorage


def _result(content: str, ok: bool = True, finish_reason: str | None = None):
    raw = {"choices": [{"finish_reason": finish_reason}]} if finish_reason else None
    return type("Result", (), {"ok": ok, "content": content, "error": None, "raw": raw})()


def _execution_write_call(path, *, action_id: str, content: bytes = b"approved") -> str:
    return json.dumps(
        {
            "tool": "execution.apply",
            "arguments": {
                "payload": {
                    "protocol": "jarvis.execution.v1",
                    "action": {
                        "kind": "fs.write",
                        "action_id": action_id,
                        "path": str(path),
                        "content_base64": base64.b64encode(content).decode("ascii"),
                    },
                }
            },
        }
    )


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
    # Loop mechanics test: keep the answer self-check out of the call count.
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
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


def test_agentic_loop_learns_persona_insight_from_dialogue(monkeypatch, tmp_path):
    # The operator reveals a durable fact in passing; the model saves it through
    # the real persona.insight tool (no monkeypatched registry) so future turns
    # see it in the persona block. This is the reasoning-first replacement for
    # regex persona extraction.
    from jarvis_gpt.persona import load_persona

    class InsightThenAnswerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(
                    '{"tool": "persona.insight", '
                    '"arguments": {"field": "tech_stack", "value": "Proxmox"}}'
                )
            return _result("Запомнил: Proxmox теперь часть твоего стека.")

    llm = InsightThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    # Persona-learning test: keep the answer self-check out of the call count.
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})

    response = asyncio.run(agent.chat("кстати, я перевёл домашний кластер на Proxmox"))

    assert llm.calls == 2
    assert "Proxmox" in response.answer
    persona = load_persona(storage)
    assert "Proxmox" in persona["tech_stack"]
    assert any(
        event.type == "tool_call" and event.payload.get("tool") == "persona.insight"
        for event in response.events
    )
    audit_actions = {item["action"] for item in storage.list_audit(limit=20)}
    assert "persona.insight" in audit_actions
    storage.close()


def test_agentic_loop_inspects_system_without_the_word_wmi(monkeypatch, tmp_path):
    # An everyday phrasing with no "wmi"/"cim" keyword: the deterministic native
    # heuristics do not fire, so the model itself reaches for the safe
    # system.inspect tool and picks the WMI class from its own understanding.
    class InspectThenAnswerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(
                    '{"tool": "system.inspect", "arguments": {"action": "wmi.query", '
                    '"payload": {"class_name": "Win32_Battery", '
                    '"properties": ["EstimatedChargeRemaining"]}}}'
                )
            return _result("Заряд батареи: 87%.")

    llm = InspectThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
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
                "summary": "Battery 87%",
                "data": {"action": "wmi.query"},
            },
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("сколько заряда осталось на ноуте?"))

    assert llm.calls == 2
    assert captured["tool"] == "system.inspect"
    assert captured["arguments"]["payload"]["class_name"] == "Win32_Battery"
    assert "87%" in response.answer
    storage.close()


def test_agentic_answer_auto_continues_after_length_finish(monkeypatch, tmp_path):
    class LengthThenDoneLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result("Первая часть", finish_reason="length")
            return _result("и нормальный финал.", finish_reason="stop")

    llm = LengthThenDoneLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)

    response = asyncio.run(agent.chat("Объясни устройство локального runtime", mode="chat"))

    assert llm.calls == 2
    assert "Первая часть" in response.answer
    assert "нормальный финал" in response.answer
    assert "лимиту" not in response.answer
    done = [event for event in response.events if event.type == "assistant_done"][-1]
    assert done.payload["continuations"] == 1
    storage.close()


def test_agentic_loop_gates_dangerous_tool_with_approval(monkeypatch, tmp_path):
    target = tmp_path / "agentic-approved.txt"
    tool_call = _execution_write_call(target, action_id="agentic-approved-write")

    class DangerThenAnswerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(tool_call)
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
    assert pending[0]["risk"] == "danger"
    assert pending[0]["payload"]["tool"] == "execution.apply"
    assert pending[0]["payload"]["arguments"]["payload"]["protocol"] == "jarvis.execution.v1"
    assert not target.exists()
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
    agent.storage.set_runtime_value(
        "experience.autonomy_policy",
        {"max_autonomous_steps": 2, "verify_answers": False},
    )

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


def test_mission_step_approval_carries_mission_id(monkeypatch, tmp_path):
    target = tmp_path / "mission-gated.txt"
    tool_call = _execution_write_call(target, action_id="mission-gated-write")

    class MissionDangerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(tool_call)
            return _result("Шаг требует подтверждения оператора для действия на хосте.")

    agent, storage = _agent(monkeypatch, tmp_path, MissionDangerLLM())

    async def fail_run(name, arguments=None, **kwargs):
        raise AssertionError(f"dangerous tool {name} must not run autonomously")

    monkeypatch.setattr(agent.tools, "run", fail_run)
    mission = agent.create_mission("Проверить дату на хосте")

    response = asyncio.run(agent.execute_next_mission_step(mission["id"]))

    assert response.task is not None
    pending = storage.list_approvals(limit=10, status="pending")
    assert len(pending) == 1
    assert response.result.ok is False
    assert response.task.status == "blocked"
    assert response.result.data["approval_ids"] == [pending[0]["id"]]
    payload = pending[0]["payload"]
    if isinstance(payload, str):
        import json as _json

        payload = _json.loads(payload)
    assert payload.get("mission_id") == mission["id"]
    assert payload.get("tool") == "execution.apply"
    assert payload["arguments"]["payload"]["protocol"] == "jarvis.execution.v1"
    assert not target.exists()
    storage.close()


def test_approval_execution_resumes_blocked_mission_step(monkeypatch, tmp_path):
    target = tmp_path / "mission-approved.txt"
    tool_call = _execution_write_call(target, action_id="mission-approved-write")

    class MissionDangerThenResumeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(tool_call)
            if self.calls == 2:
                return _result("Шаг требует допуска оператора.")
            return _result("Шаг завершён после допуска: команда на хосте выполнена.")

    llm = MissionDangerThenResumeLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    profile = {
        "schema": "jarvis.host-profile.v1",
        "fingerprint_sha256": "a" * 64,
        "host": {"os": {}, "architecture": {}, "accelerators": {}, "tools": {}},
    }
    agent.executive = ExecutiveCoordinator(storage=storage, host_profile=profile)
    agent.tools.executive = agent.executive
    goal = f"Write {target}"
    mission = storage.create_mission(title=goal, goal=goal, tasks=[goal])
    agent.executive.create_for_mission(mission)

    blocked = asyncio.run(agent.execute_next_mission_step(mission["id"]))
    approval = storage.list_approvals(limit=1, status="pending")[0]
    storage.update_approval(approval["id"], status="approved", result={"operator": "test"})
    executor = ApprovalExecutor(
        storage=storage,
        llm=agent.llm,
        dispatcher=DispatcherManager(agent.settings, repo_root=tmp_path),
        tools=agent.tools,
        mission_resumer=agent.resume_mission_after_approval,
    )

    result = asyncio.run(executor.execute(approval["id"]))
    refreshed = storage.get_mission(mission["id"])
    task = refreshed["tasks"][0]
    hits = storage.search_memory("после допуска", limit=5)

    assert blocked.task is not None
    assert blocked.task.status == "blocked"
    assert result.ok is True
    assert result.approval is not None
    assert result.approval["status"] == "executed"
    assert result.data["tool_run"]["tool"] == "execution.apply"
    assert result.data["mission_resume"]["ok"] is True
    assert target.read_bytes() == b"approved"
    assert task["status"] == "done"
    assert "после допуска" in task["notes"]
    assert hits
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
