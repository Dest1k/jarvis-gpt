from __future__ import annotations

import asyncio

from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.learning import LearningEngine
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.operator_queue import answer_quality_report, operator_queue_snapshot
from jarvis_gpt.storage import JarvisStorage


def _storage(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return settings, storage


def test_message_feedback_persists_metadata_and_learning_journal(monkeypatch, tmp_path):
    _settings, storage = _storage(monkeypatch, tmp_path)
    conversation_id = storage.create_conversation("Feedback test")
    message_id = storage.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content="Вот команда: Get-Process | Sort CPU",
    )

    updated = storage.set_message_feedback(
        message_id,
        rating="down",
        comment="нужна была команда для Linux",
    )
    missing = storage.set_message_feedback("msg_missing", rating="up")

    assert missing is None
    assert updated is not None
    assert updated["metadata"]["feedback"]["rating"] == "down"
    stored = storage.get_message(message_id)
    assert stored["metadata"]["feedback"]["comment"] == "нужна была команда для Linux"
    observations = storage.list_learning_observations(limit=10, kind="operator.feedback")
    assert len(observations) == 1
    assert observations[0]["payload"]["rating"] == "down"
    assert observations[0]["conversation_id"] == conversation_id
    # The journal record survives deleting the visible chat history.
    storage.delete_conversation(conversation_id)
    observations = storage.list_learning_observations(limit=10, kind="operator.feedback")
    assert len(observations) == 1
    storage.close()


def test_learning_tick_turns_quality_signals_into_lessons(monkeypatch, tmp_path):
    _settings, storage = _storage(monkeypatch, tmp_path)
    storage.record_learning_observation(
        kind="operator.feedback",
        role="operator",
        content="Ответ про бэкапы без расписания",
        summary="Operator rated an answer down: нет расписания",
        payload={"rating": "down", "comment": "нет расписания", "message_id": "msg_x"},
    )
    storage.record_learning_observation(
        kind="verification.revise",
        role="verifier",
        content="Проверь диски на сервере",
        summary="Self-check found gaps: не указана команда проверки",
        payload={"verdict": "revise", "missing": ["не указана команда проверки"]},
    )
    approval = storage.create_approval(
        title="Перезапустить хост немедленно",
        description="risky",
        requested_action="tool.run",
        risk="danger",
    )
    storage.update_approval(approval["id"], status="rejected")

    result = LearningEngine(storage).tick()

    contents = [item["content"] for item in result["saved"]]
    assert any("нет расписания" in content for content in contents)
    assert any("не указана команда проверки" in content for content in contents)
    assert any("Перезапустить хост немедленно" in content for content in contents)
    storage.close()


def test_lessons_prompt_is_injected_into_every_turn(monkeypatch, tmp_path):
    settings, storage = _storage(monkeypatch, tmp_path)
    storage.add_memory(
        content="Оператор просит команды и для Windows, и для Linux, когда ОС не названа.",
        namespace="learning",
        tags=["learning", "feedback"],
        importance=0.9,
    )
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    context = agent._prepare_context("как посмотреть загрузку CPU?", None)
    messages = agent._build_llm_messages(context, "как посмотреть загрузку CPU?")

    system_text = "\n".join(m["content"] for m in messages if m["role"] == "system")
    assert "Уроки из опыта Jarvis" in system_text
    assert "и для Windows, и для Linux" in system_text
    storage.close()


def test_operator_queue_surfaces_quality_signals(monkeypatch, tmp_path):
    settings, storage = _storage(monkeypatch, tmp_path)
    storage.record_learning_observation(
        kind="operator.feedback",
        role="operator",
        content="Ответ мимо задачи",
        summary="Operator rated an answer down: не то",
        payload={"rating": "down", "comment": "не то", "message_id": "msg_y"},
    )
    for index in range(3):
        storage.record_learning_observation(
            kind="verification.revise",
            role="verifier",
            content=f"Задача {index}",
            summary="Self-check found gaps: нет источников",
            payload={"verdict": "revise", "missing": ["нет источников"]},
        )

    quality = answer_quality_report(storage)
    queue = operator_queue_snapshot(settings, storage)
    ids = {item["id"] for item in queue["items"]}

    assert len(quality["negative_feedback"]) == 1
    assert len(quality["revises"]) == 3
    assert quality["top_gaps"] == ["нет источников"]
    assert "quality:feedback" in ids
    assert "quality:self-check" in ids
    storage.close()


def test_failed_self_check_writes_learning_observation(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class SelfCheckLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            if "answer-verification-v1" in system:
                return _result(
                    '{"verdict": "revise", "score": 0.3, '
                    '"missing": ["нет фактической проверки"], "fix_hint": ""}'
                )
            if "Перепиши ответ оператору ЦЕЛИКОМ" in system:
                return _result("Исправленный ответ с проверкой.")
            if self.calls == 1:
                return _result('{"tool": "runtime.status", "arguments": {}}')
            return _result("Черновой ответ.")

    agent = AgentRuntime(settings=settings, storage=storage, llm=SelfCheckLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        return type("R", (), {"tool": name, "ok": True, "summary": "ok", "data": {}})()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    asyncio.run(agent.chat("собери статус рантайма и ответь"))

    observations = storage.list_learning_observations(limit=10, kind="verification.revise")
    assert len(observations) == 1
    assert observations[0]["payload"]["missing"] == ["нет фактической проверки"]
    storage.close()


def _result(content: str, ok: bool = True):
    return type("Result", (), {"ok": ok, "content": content, "error": None, "raw": None})()
