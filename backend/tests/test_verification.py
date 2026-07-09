from __future__ import annotations

import asyncio

from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter, LLMStreamChunk
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.verification import deterministic_mission_report, parse_verdict


def _result(content: str, ok: bool = True, finish_reason: str | None = None):
    raw = {"choices": [{"finish_reason": finish_reason}]} if finish_reason else None
    return type("Result", (), {"ok": ok, "content": content, "error": None, "raw": raw})()


def _agent(monkeypatch, tmp_path, llm, *, llm_enabled: bool = True):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1" if llm_enabled else "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, bus=EventBus())
    return agent, storage


REVISE_VERDICT = (
    '{"verdict": "revise", "score": 0.4, '
    '"missing": ["нет команды проверки"], "fix_hint": "добавь команду"}'
)
PASS_VERDICT = '{"verdict": "pass", "score": 0.92, "missing": [], "fix_hint": ""}'


def _system_text(messages) -> str:
    return "\n".join(m["content"] for m in messages if m["role"] == "system")


def test_parse_verdict_accepts_fenced_json_and_rejects_garbage():
    fenced = f"```json\n{REVISE_VERDICT}\n```"
    verdict = parse_verdict(fenced)
    assert verdict is not None
    assert verdict.verdict == "revise"
    assert verdict.missing == ("нет команды проверки",)
    assert verdict.fix_hint == "добавь команду"

    assert parse_verdict("просто текст без JSON") is None
    assert parse_verdict('{"verdict": "maybe", "score": 0.5}') is None
    clamped = parse_verdict('{"verdict": "pass", "score": 7}')
    assert clamped is not None
    assert clamped.score == 1.0


def test_chat_answer_repaired_after_failed_self_check(monkeypatch, tmp_path):
    class SelfCheckLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            system = _system_text(messages)
            if "answer-verification-v1" in system:
                return _result(REVISE_VERDICT)
            if "Перепиши ответ оператору ЦЕЛИКОМ" in system:
                return _result("Исправленный ответ: статус собран, проверка — Get-Service.")
            if self.calls == 1:
                return _result('{"tool": "runtime.status", "arguments": {}}')
            return _result("Черновой ответ: статус собран.")

    llm = SelfCheckLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)

    async def fake_run(name, arguments=None, **kwargs):
        return type(
            "R",
            (),
            {"tool": name, "ok": True, "summary": "runtime ok", "data": {"profile": "turbo"}},
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("собери статус рантайма и ответь"))

    # tool round + draft + verification + repair
    assert llm.calls == 4
    assert response.answer == "Исправленный ответ: статус собран, проверка — Get-Service."
    verification = [event for event in response.events if event.type == "verification"]
    assert len(verification) == 1
    assert verification[0].payload["verdict"] == "revise"
    assert verification[0].payload["repaired"] is True
    done = [event for event in response.events if event.type == "assistant_done"][-1]
    assert done.payload["verification"]["verdict"] == "revise"
    storage.close()


def test_chat_answer_passes_self_check_unchanged(monkeypatch, tmp_path):
    class PassingLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            system = _system_text(messages)
            if "answer-verification-v1" in system:
                return _result(PASS_VERDICT)
            if self.calls == 1:
                return _result('{"tool": "runtime.status", "arguments": {}}')
            return _result("Ответ по данным инструментов: всё в порядке.")

    llm = PassingLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)

    async def fake_run(name, arguments=None, **kwargs):
        return type(
            "R",
            (),
            {"tool": name, "ok": True, "summary": "ok", "data": {}},
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("собери статус рантайма и ответь"))

    # tool round + draft + verification, no repair round
    assert llm.calls == 3
    assert response.answer == "Ответ по данным инструментов: всё в порядке."
    verification = [event for event in response.events if event.type == "verification"]
    assert len(verification) == 1
    assert verification[0].payload["verdict"] == "pass"
    assert verification[0].payload["repaired"] is False
    storage.close()


def test_verification_respects_policy_opt_out(monkeypatch, tmp_path):
    class ToolThenAnswerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result('{"tool": "runtime.status", "arguments": {}}')
            return _result("Ответ без самопроверки.")

    llm = ToolThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})

    async def fake_run(name, arguments=None, **kwargs):
        return type("R", (), {"tool": name, "ok": True, "summary": "ok", "data": {}})()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("собери статус рантайма и ответь"))

    assert llm.calls == 2
    assert response.answer == "Ответ без самопроверки."
    assert not [event for event in response.events if event.type == "verification"]
    storage.close()


def test_stream_answer_gets_correction_addendum(monkeypatch, tmp_path):
    class StreamSelfCheckLLM:
        def __init__(self) -> None:
            self.rounds = 0

        async def stream_complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.rounds += 1
            if self.rounds == 1:
                yield LLMStreamChunk(
                    kind="delta",
                    content='{"tool": "runtime.status", "arguments": {}}',
                )
                yield LLMStreamChunk(kind="done", finish_reason="stop")
            else:
                for piece in ["Стримовый ответ ", "по данным инструментов."]:
                    yield LLMStreamChunk(kind="delta", content=piece)
                yield LLMStreamChunk(kind="done", finish_reason="stop")

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            system = _system_text(messages)
            if "answer-verification-v1" in system:
                return _result(REVISE_VERDICT)
            if "Поправка после самопроверки" in system:
                return _result("Поправка после самопроверки: добавь проверку Get-Service.")
            return _result("не должно вызываться")

    llm = StreamSelfCheckLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)

    async def fake_run(name, arguments=None, **kwargs):
        return type("R", (), {"tool": name, "ok": True, "summary": "ok", "data": {}})()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    async def collect():
        deltas = []
        events = []
        done = None
        async for message in agent.stream_chat("собери статус и ответь"):
            if message["type"] == "delta":
                deltas.append(message["content"])
            elif message["type"] == "event":
                events.append(message["event"])
            elif message["type"] == "done":
                done = message
        return deltas, events, done

    deltas, events, done = asyncio.run(collect())
    streamed = "".join(deltas)

    assert "Стримовый ответ по данным инструментов." in streamed
    assert "Поправка после самопроверки: добавь проверку Get-Service." in streamed
    assert "Поправка после самопроверки" in done["answer"]
    assert any(event.get("type") == "verification" for event in events)
    assert done["events"][-1]["payload"]["verification"]["verdict"] == "revise"
    storage.close()


def test_mission_step_report_revised_by_self_check(monkeypatch, tmp_path):
    class MissionSelfCheckLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            system = _system_text(messages)
            if "answer-verification-v1" in system:
                return _result(REVISE_VERDICT)
            if "Перепиши ответ оператору ЦЕЛИКОМ" in system:
                return _result("Отчёт дополнен: статус проверен, результат зафиксирован.")
            if "mission-report-v1" in system:
                return _result("Отчёт миссии: все шаги выполнены и проверены инструментами.")
            return _result("Шаг выполнен частично.")

    llm = MissionSelfCheckLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    mission = agent.create_mission("Проверить рантайм и отчитаться")

    response = asyncio.run(agent.execute_next_mission_step(mission["id"]))

    assert response.task is not None
    assert response.task.status == "done"
    assert "Отчёт дополнен" in response.result.summary
    assert response.result.data["verification"]["verdict"] == "revise"
    assert response.result.data["verification"]["repaired"] is True
    storage.close()


def test_completed_mission_produces_final_report_offline(monkeypatch, tmp_path):
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
    mission = agent.create_mission("Проверить дисковое пространство на сервере")

    response = asyncio.run(agent.run_mission(mission["id"], max_steps=24))

    assert response.completed is True
    assert response.final_report is not None
    assert "Итог миссии" in response.final_report
    record = agent.mission_report(mission["id"])
    assert record is not None and record["report"] == response.final_report
    memories = storage.search_memory("Mission report", limit=5)
    assert memories
    # Idempotent: a second finalize returns the same stored report.
    again = asyncio.run(agent._maybe_finalize_mission(mission["id"]))
    assert again is not None and again["report"] == response.final_report
    storage.close()


def test_deterministic_mission_report_lists_steps():
    mission = {
        "title": "Тестовая миссия",
        "goal": "Проверить систему",
        "tasks": [
            {"status": "done", "title": "Шаг 1", "notes": "всё ок"},
            {"status": "blocked", "title": "Шаг 2", "notes": ""},
        ],
    }
    report = deterministic_mission_report(mission)
    assert "Итог миссии «Тестовая миссия»" in report
    assert "Выполнено шагов: 1 из 2." in report
    assert "[done] Шаг 1 — всё ок" in report
    assert "[blocked] Шаг 2" in report


def test_arbiter_asks_clarifying_question_instead_of_guessing(monkeypatch, tmp_path):
    calls = []

    class ClarifyRouterLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            calls.append(messages)
            return _result(
                '{"route": "clarify", "confidence": 0.8, '
                '"clarification": "Уточни: NAS нужен для дома или для офиса?", '
                '"rationale": "цель покупки меняет подбор"}'
            )

    agent, storage = _agent(monkeypatch, tmp_path, ClarifyRouterLLM())

    async def fail_tool(name, arguments=None, **kwargs):
        raise AssertionError(f"tool {name} must not run when the arbiter asks to clarify")

    monkeypatch.setattr(agent.tools, "run", fail_tool)

    response = asyncio.run(agent.chat("найди варианты недорогого NAS для дома"))

    assert len(calls) == 1
    assert "intent-router" in calls[0][0]["content"]
    assert response.answer == "Уточни: NAS нужен для дома или для офиса?"
    assert any(
        event.type == "thought" and event.title == "Нужно уточнение"
        for event in response.events
    )
    storage.close()
