"""Deterministic file-deliverable backstop for missions.

When a mission goal asks for a file but the local model only *narrates* the
content (a common 26B laziness), the runtime must still produce the file. These
tests pin the goal detection heuristics and the end-to-end synthesis that writes
a real artifact from the work the steps produced.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from jarvis_gpt.agent import (
    AgentContext,
    AgentRuntime,
    _existing_file_is_substantive,
    _goal_file_deliverable,
    _slugify_filename,
    _strip_code_fence,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMResult
from jarvis_gpt.models import (
    Mission,
    MissionRunResponse,
    MissionStepOutcome,
    ToolRunResponse,
)
from jarvis_gpt.storage import JarvisStorage


def test_goal_file_deliverable_detects_file_requests():
    md = _goal_file_deliverable(
        "Спланируй запуск техноблога: придумай 3 названия и создай md-файл с контент-планом"
    )
    assert md is not None and md["output_format"] == "md"

    docx = _goal_file_deliverable("Сделай docx-отчёт про плюсы Python")
    assert docx is not None and docx["output_format"] == "docx"

    xlsx = _goal_file_deliverable("Сделай таблицу расходов на неделю в excel")
    assert xlsx is not None and xlsx["output_format"] == "xlsx"


def test_goal_file_deliverable_prefers_explicit_filename():
    spec = _goal_file_deliverable(
        "Узнай последнюю LTS-версию Node.js и сохрани её в node-lts.md"
    )
    assert spec is not None
    assert spec["filename"] == "node-lts.md"
    assert spec["output_format"] == "md"


def test_goal_file_deliverable_ignores_non_file_goals():
    # Purely creative — no file.
    assert _goal_file_deliverable("Придумай 3 названия для блога") is None
    # Interrogative / how-to — informational, not a create request.
    assert _goal_file_deliverable("Как создать md-файл?") is None
    assert _goal_file_deliverable("Посчитай стоимость поездки в Екатеринбург") is None


def test_slugify_transliterates_and_sanitizes():
    assert _slugify_filename("Контент-план техноблога!") == "kontent-plan-tehnobloga"
    assert _slugify_filename("   ") == "document"


def test_strip_code_fence_removes_wrapping_fence():
    assert _strip_code_fence("```markdown\n# Title\nbody\n```") == "# Title\nbody"
    assert _strip_code_fence("plain text") == "plain text"


def test_existing_file_is_substantive(tmp_path):
    placeholder = tmp_path / "p.md"
    placeholder.write_text("# Report\n\nСоставь план и сохрани", encoding="utf-8")
    assert _existing_file_is_substantive(placeholder, goal="Составь план и сохрани") is False

    real = tmp_path / "r.md"
    real.write_text("# Контент-план\n\n" + ("Раздел с содержанием. " * 30), encoding="utf-8")
    assert _existing_file_is_substantive(real, goal="Составь план") is True

    assert _existing_file_is_substantive(tmp_path / "missing.md", goal="x") is False


class _ContentLLM:
    """Stub that returns clean final file content in one shot."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def complete(self, _messages, **_kwargs) -> LLMResult:
        return LLMResult(ok=True, content=self._content)


def _autonomy_agent(monkeypatch, tmp_path, llm):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    monkeypatch.setenv("JARVIS_OPERATOR_FULL_AUTONOMY", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, bus=EventBus())
    return agent, storage, settings


def _mission_run(goal: str, summary: str) -> tuple[dict, MissionRunResponse]:
    now = "2026-07-16T00:00:00+00:00"
    mission = {
        "id": "mis_test",
        "title": "Техноблог",
        "goal": goal,
        "status": "running",
        "progress": 0.5,
        "created_at": now,
        "updated_at": now,
        "tasks": [],
    }
    run = MissionRunResponse(
        mission=Mission.model_validate(mission),
        steps=[
            MissionStepOutcome(
                task=None,
                result=ToolRunResponse(
                    tool="mission.execute_next",
                    ok=True,
                    summary=summary,
                    data={},
                ),
            )
        ],
        completed=False,
        stopped_reason="blocked",
        executed_steps=1,
    )
    return mission, run


def _operator_context() -> AgentContext:
    context = AgentContext(conversation_id="conv-deliverable", memory_hits=[], file_hits=[])
    context.operator_request_digest = "req-deliverable"
    context.operator_message_id = "msg-deliverable"
    return context


def test_ensure_goal_file_deliverable_writes_missing_file(monkeypatch, tmp_path):
    body = (
        "# Контент-план\n\n1. System Logic\n2. ByteWise\n3. CodePulse\n\n"
        "## Разделы\n- Вступление\n- Основы\n- Практика\n- Инструменты\n- Итоги"
    )
    agent, storage, _settings = _autonomy_agent(monkeypatch, tmp_path, _ContentLLM(body))
    mission, run = _mission_run(
        "Спланируй запуск техноблога: придумай 3 названия и создай md-файл с контент-планом",
        "Придумал названия: System Logic, ByteWise, CodePulse; структура из 5 разделов.",
    )
    deliverable = asyncio.run(
        agent._ensure_goal_file_deliverable(mission, run, _operator_context())
    )
    assert deliverable is not None
    assert deliverable["format"] == "md"
    path = Path(deliverable["path"])
    assert path.is_file()
    written = path.read_text(encoding="utf-8")
    assert "System Logic" in written
    storage.close()


def test_ensure_goal_file_deliverable_noop_without_file_goal(monkeypatch, tmp_path):
    agent, storage, _settings = _autonomy_agent(
        monkeypatch, tmp_path, _ContentLLM("irrelevant")
    )
    mission, run = _mission_run(
        "Придумай 3 названия для техноблога",
        "System Logic, ByteWise, CodePulse.",
    )
    deliverable = asyncio.run(
        agent._ensure_goal_file_deliverable(mission, run, _operator_context())
    )
    assert deliverable is None
    storage.close()
