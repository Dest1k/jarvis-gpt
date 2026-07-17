"""Operator-facing mission report wording.

A mission whose steps produced content but that the executive could not independently
verify must not be reported as a hard "blocked / needs intervention" failure — the
executive trust boundary is unchanged, only the report the operator reads is softened.
"""

from __future__ import annotations

from jarvis_gpt.agent import _EXECUTIVE_UNVERIFIED_MARKER, AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import (
    Mission,
    MissionRunResponse,
    MissionStepOutcome,
    ToolRunResponse,
)
from jarvis_gpt.storage import JarvisStorage


def _mission_obj(title: str = "M") -> Mission:
    return Mission(
        id="m1",
        title=title,
        goal="g",
        status="running",
        progress=0.0,
        created_at="2026-07-17T00:00:00+00:00",
        updated_at="2026-07-17T00:00:00+00:00",
    )


def _agent(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings, storage=storage, llm=LLMRouter(settings), bus=EventBus()
    )
    return agent, storage


def _unverified(summary: str) -> ToolRunResponse:
    # How run_mission records a step whose tool succeeded but that lacked independent
    # verification: ok=False with the marker prepended and the real content following.
    return ToolRunResponse(
        tool="reason", ok=False, summary=f"{_EXECUTIVE_UNVERIFIED_MARKER} {summary}"
    )


def test_verification_only_block_reports_done_with_content(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)
    run = MissionRunResponse(
        mission=_mission_obj(),
        steps=[
            MissionStepOutcome(
                task=None,
                result=ToolRunResponse(
                    tool="reason", ok=True, summary="3 названия: Alpha, Beta, Gamma"
                ),
            ),
            MissionStepOutcome(task=None, result=_unverified("Структура из 5 разделов готова")),
        ],
        stopped_reason="blocked",
        executed_steps=2,
    )
    answer = agent._mission_run_answer({"title": "Техноблог"}, run)
    assert "выполнено" in answer
    assert "заблокирована" not in answer
    assert "требует вмешательства" not in answer
    # the unverified step shows its real content with a check, not the raw marker
    assert "Структура из 5 разделов готова" in answer
    assert _EXECUTIVE_UNVERIFIED_MARKER not in answer
    assert answer.count("✓") == 2 and "✗" not in answer
    storage.close()


def test_genuine_tool_failure_stays_blocked(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)
    run = MissionRunResponse(
        mission=_mission_obj(),
        steps=[
            MissionStepOutcome(
                task=None,
                result=ToolRunResponse(
                    tool="web.fetch", ok=False, summary="HTTP 500 from the server"
                ),
            ),
        ],
        stopped_reason="blocked",
        executed_steps=1,
    )
    answer = agent._mission_run_answer({"title": "Сбор данных"}, run)
    assert "заблокирована" in answer
    assert "требует вмешательства" in answer
    assert "✗" in answer
    storage.close()


def test_blocked_with_file_deliverable_reports_done(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)
    run = MissionRunResponse(
        mission=_mission_obj(),
        steps=[MissionStepOutcome(task=None, result=_unverified("черновик готов"))],
        stopped_reason="blocked",
        executed_steps=1,
    )
    answer = agent._mission_run_answer(
        {"title": "План"}, run, deliverable={"path": "plan.md", "format": "md"}
    )
    assert "выполнено" in answer
    assert "**Файл готов:**" in answer
    assert "требует вмешательства" not in answer
    storage.close()
