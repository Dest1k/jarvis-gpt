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
    _ExecutedToolResult,
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


# --- Fix A: the same backstop generalized to the plain single-turn chat path ---


def test_chat_backstop_materializes_narrated_file(monkeypatch, tmp_path):
    # The model narrated a file but called no writer — the chat backstop must materialize it.
    body = "# План\n\n1. Один\n2. Два\n3. Три\n\nРазделы: вступление, основа, итог, ссылки."
    agent, storage, _settings = _autonomy_agent(monkeypatch, tmp_path, _ContentLLM(body))
    deliverable = asyncio.run(
        agent._maybe_backstop_chat_file(
            _operator_context(),
            message="Сделай план на неделю и сохрани в plan.md",
            answer="Готово, сохранил план в plan.md.",
            finish_reason="stop",
            blocked_by_approval=False,
            executed_tools=(),
        )
    )
    assert deliverable is not None
    assert deliverable["format"] == "md"
    path = Path(deliverable["path"])
    assert path.is_file()
    assert "План" in path.read_text(encoding="utf-8")
    storage.close()


def test_goal_file_deliverable_preserves_explicit_directory():
    from jarvis_gpt.agent import _destination_path_from_message

    spec = _goal_file_deliverable(
        r"Составь чеклист и сохрани его в файл D:\jarvis-gpt\live_verify_A.md"
    )
    assert spec is not None
    assert spec["filename"] == "live_verify_A.md"
    assert spec.get("output_path") == r"D:\jarvis-gpt\live_verify_A.md"

    # A bare basename carries no directory → no output_path (default dir preserved).
    bare = _goal_file_deliverable("Сделай план и сохрани в plan.md")
    assert bare is not None
    assert "output_path" not in bare
    assert _destination_path_from_message("сохрани в plan.md") is None
    # A relative directory is preserved too (stays under the output root at write time).
    assert _destination_path_from_message("сохрани в out/report.docx") == "out/report.docx"


def test_chat_backstop_honors_explicit_absolute_destination(monkeypatch, tmp_path):
    # An explicit absolute destination inside an allowed root (JARVIS_HOME) must be
    # honored verbatim, not silently redirected to the default document-outputs dir.
    body = (
        "# Чеклист\n\n1. Один\n2. Два\n3. Три\n4. Четыре\n5. Пять\n\n"
        "Достаточно содержательный текст, чтобы файл считался не-заглушкой."
    )
    agent, storage, settings = _autonomy_agent(monkeypatch, tmp_path, _ContentLLM(body))
    dest = tmp_path / "reports" / "verify_A.md"  # under JARVIS_HOME → allowed root
    deliverable = asyncio.run(
        agent._maybe_backstop_chat_file(
            _operator_context(),
            message=f"Составь чеклист из 5 пунктов и сохрани его в файл {dest}",
            answer="Готово, сохранил чеклист.",
            finish_reason="stop",
            blocked_by_approval=False,
            executed_tools=(),
        )
    )
    assert deliverable is not None
    written = Path(deliverable["path"])
    assert written.resolve() == dest.resolve()
    assert written.is_file()
    assert "Чеклист" in written.read_text(encoding="utf-8")
    # Must NOT have fallen back to the default document-outputs directory.
    default = settings.data_dir / "document-outputs" / "verify_A.md"
    assert not default.exists()
    storage.close()


def test_finalize_answer_appends_note_and_writes(monkeypatch, tmp_path):
    # The shared _finalize_answer (Fix A tail) must materialize a narrated file and append
    # the note, returning (answer, deliverable). The note is a pure append (the streaming
    # seam relies on that suffix property).
    body = "# План\n\n1. Один\n2. Два\n3. Три\n\nДостаточно содержательный текст для файла."
    agent, storage, _settings = _autonomy_agent(monkeypatch, tmp_path, _ContentLLM(body))
    original = "Готово, сохранил план в plan.md."
    final, deliverable = asyncio.run(
        agent._finalize_answer(
            _operator_context(),
            message="Сделай план на неделю и сохрани в plan.md",
            answer=original,
            finish_reason="stop",
            blocked_by_approval=False,
            executed_tools=(),
        )
    )
    assert deliverable is not None
    assert final.startswith(original)  # pure append → streaming delta suffix is valid
    assert final != original
    assert "Файл готов" in final
    assert Path(deliverable["path"]).is_file()
    storage.close()


def test_finalize_answer_noop_leaves_answer_unchanged(monkeypatch, tmp_path):
    # An informational answer with no file goal must return the answer verbatim + None.
    agent, storage, _settings = _autonomy_agent(monkeypatch, tmp_path, _ContentLLM("x"))
    original = "Чтобы создать md-файл, сохрани текст с расширением .md."
    final, deliverable = asyncio.run(
        agent._finalize_answer(
            _operator_context(),
            message="Как создать md-файл?",
            answer=original,
            finish_reason="stop",
            blocked_by_approval=False,
            executed_tools=(),
        )
    )
    assert deliverable is None
    assert final == original


def test_orchestration_backstops_narrated_file(monkeypatch, tmp_path):
    # The one real coverage gain: a multistep orchestration that NARRATES a file (its menu
    # has no durable writer) must now be backstopped through _finalize_answer.
    body = "# Отчёт\n\n1. A\n2. B\n3. C\n\nДостаточно содержательный текст отчёта для проверки."
    agent, storage, settings = _autonomy_agent(monkeypatch, tmp_path, _ContentLLM(body))

    class _FakeResult:
        answer = "Собрал отчёт и сохранил в report.md."

    class _FakeOrchestrator:
        def __init__(self, **kwargs):
            pass

        async def run(self, _message):
            return _FakeResult()

    monkeypatch.setattr(
        "jarvis_gpt.task_orchestrator.TaskOrchestrator", _FakeOrchestrator
    )

    direct = asyncio.run(
        agent._run_task_orchestration(
            "сделай A, B и собери отчёт в report.md", _operator_context()
        )
    )
    assert direct is not None
    assert "Файл готов" in direct.answer
    assert (settings.data_dir / "document-outputs" / "report.md").is_file()

    # A research-only multistep goal (no file) must pass through unchanged.
    plain = asyncio.run(
        agent._run_task_orchestration(
            "сравни RTX 5090 и 4090 и сделай вывод", _operator_context()
        )
    )
    assert plain is not None
    assert plain.answer == "Собрал отчёт и сохранил в report.md."
    storage.close()


def test_chat_backstop_noops_when_writer_ran(monkeypatch, tmp_path):
    # A real durable write already happened this turn → backstop must not double-write.
    agent, storage, _settings = _autonomy_agent(monkeypatch, tmp_path, _ContentLLM("x"))
    executed = (
        _ExecutedToolResult(
            tool="documents.generate",
            arguments={},
            result=ToolRunResponse(
                tool="documents.generate", ok=True, summary="written", data={}
            ),
        ),
    )
    deliverable = asyncio.run(
        agent._maybe_backstop_chat_file(
            _operator_context(),
            message="Сделай план и сохрани в plan.md",
            answer="готово",
            finish_reason="stop",
            blocked_by_approval=False,
            executed_tools=executed,
        )
    )
    assert deliverable is None
    storage.close()


def test_chat_backstop_ignores_informational_answer(monkeypatch, tmp_path):
    # An informational reply that merely mentions a file must not trigger a write.
    agent, storage, _settings = _autonomy_agent(monkeypatch, tmp_path, _ContentLLM("x"))
    deliverable = asyncio.run(
        agent._maybe_backstop_chat_file(
            _operator_context(),
            message="Как создать md-файл?",
            answer="Чтобы создать md-файл, сохрани текст с расширением .md.",
            finish_reason="stop",
            blocked_by_approval=False,
            executed_tools=(),
        )
    )
    assert deliverable is None
    storage.close()


def test_chat_backstop_salvages_file_from_tool_material_on_fumble(monkeypatch, tmp_path):
    # A file goal where the model fumbled the final synthesis (protocol_error) but real
    # research tools ran must still materialize the file from that gathered material — a
    # fumbled final turn is exactly when the model narrates a file without writing it.
    body = "# Отчёт\n\n1. A\n2. B\n\nСодержательный текст, синтезированный из материала."
    agent, storage, _settings = _autonomy_agent(monkeypatch, tmp_path, _ContentLLM(body))
    executed = (
        _ExecutedToolResult(
            tool="web.search",
            arguments={},
            result=ToolRunResponse(tool="web.search", ok=True, summary="found 5", data={}),
        ),
    )
    deliverable = asyncio.run(
        agent._maybe_backstop_chat_file(
            _operator_context(),
            message="Собери отчёт про GPU в файл gpu.md",
            answer="До остановки зафиксированы результаты инструментов…",
            finish_reason="protocol_error",
            blocked_by_approval=False,
            executed_tools=executed,
        )
    )
    assert deliverable is not None
    assert Path(deliverable["path"]).is_file()
    storage.close()


def test_chat_backstop_noops_on_blocked_finish(monkeypatch, tmp_path):
    # A protocol/synthesis/approval-blocked finish must never auto-materialize a file.
    agent, storage, _settings = _autonomy_agent(monkeypatch, tmp_path, _ContentLLM("body"))
    deliverable = asyncio.run(
        agent._maybe_backstop_chat_file(
            _operator_context(),
            message="Сделай план и сохрани в plan.md",
            answer="готово",
            finish_reason="protocol_error",
            blocked_by_approval=False,
            executed_tools=(),
        )
    )
    assert deliverable is None
    storage.close()
