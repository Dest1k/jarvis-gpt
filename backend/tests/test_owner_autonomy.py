"""Owner full-autonomy posture (JARVIS_OPERATOR_FULL_AUTONOMY=1).

These tests opt back into the autonomous posture that the runtime ships with by
default. The rest of the suite pins the gated posture via ``conftest.py``; here we
assert that when the owner grants full autonomy the runtime (a) never stops the
operator's own turn on a clarifying question, (b) exposes the complete toolset and
authorizes the model's chosen tool without an approval gate, and (c) keeps the live
chat to request/analysis/action/result instead of streaming service messages.
"""

from __future__ import annotations

import asyncio

import pytest
from jarvis_gpt.agent import AgentContext, AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import ChatEvent
from jarvis_gpt.storage import JarvisStorage


class _RecordingBus:
    """Duck-typed EventBus that records what would stream to the chat."""

    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.published.append(event)


def _autonomy_agent(monkeypatch, tmp_path, *, full_autonomy: bool, bus=None):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_OPERATOR_FULL_AUTONOMY", "1" if full_autonomy else "0")
    settings = load_settings()
    assert settings.operator_full_autonomy is full_autonomy
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=bus if bus is not None else EventBus(),
    )
    return agent, storage


def _context(conversation_id: str = "conv-autonomy") -> AgentContext:
    return AgentContext(conversation_id=conversation_id, memory_hits=[], file_hits=[])


def test_admit_side_effects_never_clarifies_under_autonomy(monkeypatch, tmp_path):
    agent, storage = _autonomy_agent(monkeypatch, tmp_path, full_autonomy=True)
    context = _context()
    # An intentionally under-specified artifact request that the gated posture
    # would answer with a clarifying question.
    admitted, effective, clarification = agent._admit_side_effects(
        "подготовь отчёт по продажам", context
    )
    assert admitted is True
    assert clarification is None
    assert effective == "подготовь отчёт по продажам"
    assert context.side_effects_admitted is True
    storage.close()


def test_admit_side_effects_still_clarifies_when_gated(monkeypatch, tmp_path):
    agent, storage = _autonomy_agent(monkeypatch, tmp_path, full_autonomy=False)
    admitted, _effective, clarification = agent._admit_side_effects(
        "подготовь отчёт по продажам", _context()
    )
    assert admitted is False
    assert clarification  # gated posture asks exactly one clarifying question
    storage.close()


def test_tools_for_context_exposes_full_toolset_under_autonomy(monkeypatch, tmp_path):
    agent, storage = _autonomy_agent(monkeypatch, tmp_path, full_autonomy=True)
    exposed = {tool.name for tool in agent._tools_for_context(_context())}
    everything = {tool.name for tool in agent.tools.list()}
    assert exposed == everything
    # Review/danger tools are exposed, not just the safe subset.
    assert "windows.native" in exposed
    assert any(
        tool.danger_level in {"review", "danger"} for tool in agent._tools_for_context(_context())
    )
    storage.close()


def test_side_effect_tool_blocked_yields_under_autonomy(monkeypatch, tmp_path):
    agent, storage = _autonomy_agent(monkeypatch, tmp_path, full_autonomy=True)
    context = _context()
    context.side_effects_admitted = False  # would block in the gated posture
    assert agent._side_effect_tool_blocked("documents.generate", context) is None
    storage.close()


@pytest.mark.parametrize(
    ("event", "suppressed"),
    [
        (ChatEvent(type="thought", title="reasoning"), True),
        (ChatEvent(type="memory", title="saved"), True),
        (ChatEvent(type="approval", title="approval"), True),
        (ChatEvent(type="task_kernel", title="route"), True),
        (ChatEvent(type="tool_call", title="windows.native"), False),
        (ChatEvent(type="assistant_done", title="answer"), False),
        (ChatEvent(type="mission_step", title="step"), False),
        (ChatEvent(type="verification", title="verify"), False),
        (
            ChatEvent(type="tool_call", title="blocked", payload={"route": "clarify"}),
            True,
        ),
    ],
)
def test_suppress_from_chat_classification(monkeypatch, tmp_path, event, suppressed):
    agent, storage = _autonomy_agent(monkeypatch, tmp_path, full_autonomy=True)
    assert agent._suppress_from_chat(event) is suppressed
    storage.close()


def test_gated_posture_streams_every_event(monkeypatch, tmp_path):
    agent, storage = _autonomy_agent(monkeypatch, tmp_path, full_autonomy=False)
    # Nothing is suppressed when the owner has not granted full autonomy.
    for etype in ("thought", "memory", "approval", "tool_call", "assistant_done"):
        assert agent._suppress_from_chat(ChatEvent(type=etype, title=etype)) is False
    storage.close()


def test_emit_streams_only_operator_visible_events(monkeypatch, tmp_path):
    bus = _RecordingBus()
    agent, storage = _autonomy_agent(monkeypatch, tmp_path, full_autonomy=True, bus=bus)

    asyncio.run(agent._emit(ChatEvent(type="thought", title="internal reasoning")))
    asyncio.run(agent._emit(ChatEvent(type="tool_call", title="windows.native")))
    asyncio.run(agent._emit(ChatEvent(type="approval", title="approval")))
    asyncio.run(agent._emit(ChatEvent(type="assistant_done", title="answer")))

    streamed = [event.get("type") for event in bus.published]
    assert streamed == ["tool_call", "assistant_done"]
    # Everything is still recorded in the audit event log.
    logged = {event["kind"] for event in storage.list_events(limit=20)}
    assert {"agent.thought", "agent.approval"} <= logged
    storage.close()


def test_chat_turn_emits_no_approval_or_clarify_under_autonomy(monkeypatch, tmp_path):
    agent, storage = _autonomy_agent(monkeypatch, tmp_path, full_autonomy=True)
    response = asyncio.run(agent.chat("подготовь отчёт по выручке за квартал"))
    assert all(event.type != "approval" for event in response.events)
    assert all(
        str(event.payload.get("route") or "") != "clarify" for event in response.events
    )
    assert storage.list_approvals(limit=10) == []
    storage.close()


def test_owner_autonomy_uses_full_profile_step_budget(monkeypatch, tmp_path):
    # The multi-step budget is what lets the model finish "search -> extract ->
    # compute" chains instead of stalling. Autonomy uses the profile's full budget.
    agent, storage = _autonomy_agent(monkeypatch, tmp_path, full_autonomy=True)
    assert agent._max_tool_steps() == agent.settings.profile.max_steps
    assert agent._max_tool_steps() >= 12
    storage.close()


def test_gated_mode_keeps_conservative_step_budget(monkeypatch, tmp_path):
    agent, storage = _autonomy_agent(monkeypatch, tmp_path, full_autonomy=False)
    assert agent._max_tool_steps() <= 3
    storage.close()


def test_tool_protocol_prompt_adds_multistep_guidance_under_autonomy():
    from jarvis_gpt.agent import _tool_protocol_prompt
    from jarvis_gpt.models import ToolInfo

    tools = [ToolInfo(name="web.search", description="search", category="web", input_schema={})]
    autonomous = _tool_protocol_prompt(tools, full_autonomy=True)
    gated = _tool_protocol_prompt(tools, full_autonomy=False)
    assert "Многоходов" in autonomous
    assert "Многоходов" not in gated
