"""Self-replanning missions: budget-driven self-continuation + genuine-block escalation."""

from __future__ import annotations

import asyncio

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


def _mission_obj() -> Mission:
    return Mission(
        id="m1",
        title="M",
        goal="g",
        status="running",
        progress=0.0,
        created_at="2026-07-18T00:00:00+00:00",
        updated_at="2026-07-18T00:00:00+00:00",
    )


def _agent(monkeypatch, tmp_path, env=None):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings, storage=storage, llm=LLMRouter(settings), bus=EventBus()
    )
    return agent, storage


def _run(stopped_reason, completed, executed_steps, steps=None) -> MissionRunResponse:
    return MissionRunResponse(
        mission=_mission_obj(),
        steps=steps or [],
        completed=completed,
        stopped_reason=stopped_reason,
        executed_steps=executed_steps,
    )


def _patch_run_mission(monkeypatch, agent, scripted):
    calls = {"n": 0}

    async def fake_run_mission(mission_id, *, max_steps=None):
        calls["n"] += 1
        # repeat the last scripted run once the list is exhausted
        return scripted[min(calls["n"] - 1, len(scripted) - 1)]

    async def fake_deliverable(mission, run, context):
        return None

    monkeypatch.setattr(agent, "run_mission", fake_run_mission)
    monkeypatch.setattr(agent, "_ensure_goal_file_deliverable", fake_deliverable)
    return calls


def _patch_push(monkeypatch):
    pushes: list[str] = []

    async def fake_push(text, **_kwargs):
        pushes.append(text)
        return True

    monkeypatch.setattr("jarvis_gpt.agent.push_telegram_alert", fake_push)
    return pushes


# ---- self-continuation on budget -------------------------------------------------


def test_autonomous_run_continues_until_done_on_budget(monkeypatch, tmp_path):
    agent, storage = _agent(
        monkeypatch, tmp_path, env={"JARVIS_MISSION_SELF_REPLAN_MAX_ROUNDS": "3"}
    )
    calls = _patch_run_mission(
        monkeypatch,
        agent,
        [_run("budget", False, 5), _run("budget", False, 5), _run("completed", True, 3)],
    )
    _patch_push(monkeypatch)

    run, deliverable = asyncio.run(
        agent._run_mission_autonomously({"id": "m1", "title": "M"}, None)
    )

    assert calls["n"] == 3  # initial + 2 continuations, last one completed
    assert run.completed is True
    storage.close()


def test_autonomous_run_stops_on_no_progress(monkeypatch, tmp_path):
    agent, storage = _agent(
        monkeypatch, tmp_path, env={"JARVIS_MISSION_SELF_REPLAN_MAX_ROUNDS": "5"}
    )
    calls = _patch_run_mission(
        monkeypatch,
        agent,
        # first round makes progress, second executes nothing → stop re-running
        [_run("budget", False, 5), _run("budget", False, 0)],
    )
    _patch_push(monkeypatch)

    asyncio.run(agent._run_mission_autonomously({"id": "m1", "title": "M"}, None))

    assert calls["n"] == 2  # initial + one continuation that made no progress → stop
    storage.close()


def test_autonomous_run_respects_max_rounds(monkeypatch, tmp_path):
    agent, storage = _agent(
        monkeypatch, tmp_path, env={"JARVIS_MISSION_SELF_REPLAN_MAX_ROUNDS": "2"}
    )
    calls = _patch_run_mission(
        monkeypatch, agent, [_run("budget", False, 5)]  # always budget + progress
    )
    _patch_push(monkeypatch)

    asyncio.run(agent._run_mission_autonomously({"id": "m1", "title": "M"}, None))

    assert calls["n"] == 3  # initial + max_rounds(2) continuations, then stop
    storage.close()


def _step(summary: str, ok: bool = True, tool: str = "reason") -> MissionStepOutcome:
    return MissionStepOutcome(task=None, result=ToolRunResponse(tool=tool, ok=ok, summary=summary))


def test_autonomous_run_accumulates_steps_across_rounds(monkeypatch, tmp_path):
    agent, storage = _agent(
        monkeypatch, tmp_path, env={"JARVIS_MISSION_SELF_REPLAN_MAX_ROUNDS": "3"}
    )
    scripted = [
        _run("budget", False, 2, steps=[_step("s1"), _step("s2")]),
        _run("budget", False, 2, steps=[_step("s3"), _step("s4")]),
        _run("completed", True, 1, steps=[_step("s5")]),
    ]
    calls = {"n": 0}

    async def fake_run_mission(mission_id, *, max_steps=None):
        calls["n"] += 1
        return scripted[min(calls["n"] - 1, len(scripted) - 1)]

    captured: dict = {}

    async def fake_deliverable(mission, run, context):
        captured["run"] = run
        return None

    monkeypatch.setattr(agent, "run_mission", fake_run_mission)
    monkeypatch.setattr(agent, "_ensure_goal_file_deliverable", fake_deliverable)
    _patch_push(monkeypatch)

    run, _ = asyncio.run(agent._run_mission_autonomously({"id": "m1", "title": "M"}, None))

    # 3 rounds ran 2 + 2 + 1 = 5 steps; the merged run the deliverable backstop / report /
    # escalation all see must reflect all 5, not just the last round's 1.
    assert captured["run"].executed_steps == 5
    assert len(captured["run"].steps) == 5
    assert run.executed_steps == 5
    assert run.completed is True
    storage.close()


def test_autonomous_run_single_when_disabled(monkeypatch, tmp_path):
    agent, storage = _agent(
        monkeypatch, tmp_path, env={"JARVIS_MISSION_SELF_REPLAN_ENABLED": "0"}
    )
    calls = _patch_run_mission(
        monkeypatch, agent, [_run("budget", False, 5)]
    )
    pushes = _patch_push(monkeypatch)

    asyncio.run(agent._run_mission_autonomously({"id": "m1", "title": "M"}, None))

    assert calls["n"] == 1  # no self-continuation
    assert pushes == []  # and no escalation path
    storage.close()


# ---- escalation on a genuine block -----------------------------------------------


def _blocked_run(steps) -> MissionRunResponse:
    return _run("blocked", False, len(steps), steps=steps)


def test_escalates_on_genuine_block(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)
    pushes = _patch_push(monkeypatch)
    run = _blocked_run(
        [
            MissionStepOutcome(
                task=None,
                result=ToolRunResponse(tool="web.fetch", ok=False, summary="HTTP 500"),
            )
        ]
    )

    asyncio.run(agent._maybe_escalate_mission({"id": "m1", "title": "Сбор данных"}, run, None))

    assert len(pushes) == 1
    assert "вмешательство" in pushes[0]
    assert "web.fetch" in pushes[0] or "HTTP 500" in pushes[0]
    events = [e for e in storage.list_events(limit=50) if e.get("kind") == "mission.escalation"]
    assert events
    storage.close()


def test_no_escalation_on_verification_only_block(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)
    pushes = _patch_push(monkeypatch)
    run = _blocked_run(
        [
            MissionStepOutcome(
                task=None,
                result=ToolRunResponse(
                    tool="reason", ok=False, summary=f"{_EXECUTIVE_UNVERIFIED_MARKER} готово"
                ),
            )
        ]
    )

    asyncio.run(agent._maybe_escalate_mission({"id": "m1", "title": "План"}, run, None))

    assert pushes == []  # content produced, only the verification gate tripped
    storage.close()


def test_no_escalation_when_file_delivered(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)
    pushes = _patch_push(monkeypatch)
    run = _blocked_run(
        [
            MissionStepOutcome(
                task=None,
                result=ToolRunResponse(tool="web.fetch", ok=False, summary="HTTP 500"),
            )
        ]
    )

    asyncio.run(
        agent._maybe_escalate_mission(
            {"id": "m1", "title": "Отчёт"}, run, {"path": "report.md", "format": "md"}
        )
    )

    assert pushes == []  # a file was delivered → not a failure to escalate
    storage.close()


def test_no_escalation_on_completed_or_budget(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)
    pushes = _patch_push(monkeypatch)

    mission = {"id": "m1", "title": "M"}
    asyncio.run(agent._maybe_escalate_mission(mission, _run("completed", True, 2), None))
    asyncio.run(agent._maybe_escalate_mission(mission, _run("budget", False, 5), None))

    assert pushes == []  # done needs no help; budget self-continues, never escalates
    storage.close()
