"""Scheduled agent tasks: a recurring 'do X and report' reminder runs a full agent turn
on its wall-clock schedule and delivers the answer to the owner.

Built on the reminders substrate (payload.kind == "agent_task"); the supervisor's reminder
loop fires it, and the reminders.create tool classifies task-vs-nudge.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.supervisor import RuntimeSupervisor
from jarvis_gpt.tools import ToolRegistry, _scheduled_task_prompt


class _FakeAgent:
    def __init__(self, answer: str = "Готово.") -> None:
        self.answer_text = answer
        self.calls: list[tuple[str, str | None]] = []

    async def chat(self, message, conversation_id=None, **kwargs):
        self.calls.append((message, conversation_id))
        return SimpleNamespace(answer=self.answer_text)


def _supervisor(monkeypatch, tmp_path, *, agent=None, env=None):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    executor = SimpleNamespace(agent=agent) if agent is not None else None
    supervisor = RuntimeSupervisor(settings=settings, storage=storage, autonomy_executor=executor)
    return supervisor, storage


def _patch_push(monkeypatch) -> list[str]:
    pushes: list[str] = []

    async def fake_push(text, **_kwargs):
        pushes.append(text)
        return True

    monkeypatch.setattr("jarvis_gpt.supervisor.push_telegram_alert", fake_push)
    return pushes


def _patch_push_rich(monkeypatch) -> list[dict]:
    pushes: list[dict] = []

    async def fake_push(text, **kwargs):
        pushes.append({"text": text, **kwargs})
        return True

    monkeypatch.setattr("jarvis_gpt.supervisor.push_telegram_alert", fake_push)
    return pushes


def _due_task(storage, *, text, prompt, conversation_id=None, deliver="telegram"):
    return storage.create_reminder(
        text=text,
        due_at="2000-01-01T00:00:00+00:00",  # in the past -> claimed on the next tick
        recurrence=None,
        conversation_id=conversation_id,
        source_text=text,
        payload={"kind": "agent_task", "prompt": prompt, "deliver": deliver},
    )


async def _fire_and_drain(supervisor) -> None:
    await supervisor._fire_due_reminders()
    pending = list(supervisor._scheduled_runs)
    if pending:
        await asyncio.gather(*pending)


# --------------------------------------------------------------------------- #
# Classification (pure).
# --------------------------------------------------------------------------- #


def test_scheduled_task_prompt_detects_work_verb():
    prompt = _scheduled_task_prompt("каждое утро в 9 присылай сводку по ИИ")
    assert prompt == "присылай сводку по ИИ"
    assert _scheduled_task_prompt("каждый вечер проверяй систему и отчитывайся") is not None


def test_scheduled_task_prompt_ignores_passive_nudge():
    assert _scheduled_task_prompt("напомни завтра купить хлеб") is None
    assert _scheduled_task_prompt("позвонить маме") is None


# --------------------------------------------------------------------------- #
# Tool: reminders.create classifies task vs nudge.
# --------------------------------------------------------------------------- #


def _registry(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return ToolRegistry(settings, storage, LLMRouter(settings)), storage


def test_reminders_create_marks_agent_task(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    result = asyncio.run(
        tools.run(
            "reminders.create",
            {"text": "каждое утро в 9 присылай сводку по ИИ"},
            allow_danger=True,
        )
    )
    assert result.ok is True
    assert result.data["agent_task"] is True
    payload = result.data["reminder"]["payload"]
    assert payload["kind"] == "agent_task"
    assert "сводку по ИИ" in payload["prompt"]
    storage.close()


def test_reminders_create_recovers_recurrence_from_text(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    # The model split the request: recurrence sits in `text`, `when` is a bare time.
    result = asyncio.run(
        tools.run(
            "reminders.create",
            {"text": "каждое утро в 9 присылай сводку по ИИ", "when": "в 9"},
            allow_danger=True,
        )
    )
    assert result.ok is True
    recurrence = result.data["reminder"]["recurrence"]
    assert recurrence and recurrence["kind"] == "daily"  # recovered, not one-shot
    assert result.data["agent_task"] is True
    storage.close()


def test_reminders_create_plain_nudge_has_no_task_payload(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    result = asyncio.run(
        tools.run(
            "reminders.create",
            {"text": "напомни завтра в 10 позвонить маме"},
            notification_chat_id=4242,
        )
    )
    assert result.ok is True
    assert result.data["agent_task"] is False
    payload = result.data["reminder"]["payload"] or {}
    # PassivePassive nudge** is not an agent_task, but still stamps Telegram delivery so the
    # phone gets the fire (Telegram-first).
    assert payload.get("kind") != "agent_task"
    assert payload.get("deliver") == "telegram"
    assert payload.get("telegram_chat_id") == 4242
    storage.close()


# --------------------------------------------------------------------------- #
# Supervisor: firing an agent_task reminder runs the agent + delivers.
# --------------------------------------------------------------------------- #


def test_agent_task_reminder_runs_agent_and_pushes(monkeypatch, tmp_path):
    agent = _FakeAgent("Сводка по ИИ: три события.")
    supervisor, storage = _supervisor(monkeypatch, tmp_path, agent=agent)
    pushes = _patch_push(monkeypatch)
    _due_task(storage, text="каждое утро сводка", prompt="сделай сводку по ИИ")

    asyncio.run(_fire_and_drain(supervisor))

    assert agent.calls == [("сделай сводку по ИИ", None)]
    assert any("Сводка по ИИ" in text for text in pushes)
    storage.close()


def test_plain_reminder_does_not_run_agent(monkeypatch, tmp_path):
    agent = _FakeAgent()
    supervisor, storage = _supervisor(monkeypatch, tmp_path, agent=agent)
    pushes = _patch_push_rich(monkeypatch)
    reminder = storage.create_reminder(
        text="позвонить маме",
        due_at="2000-01-01T00:00:00+00:00",
        source_text="позвонить маме",
        payload={"deliver": "telegram", "telegram_chat_id": 9001},
    )

    asyncio.run(_fire_and_drain(supervisor))

    assert agent.calls == []
    # Passive nudge must still push to Telegram (no agent turn) with snooze buttons.
    assert any("позвонить маме" in item["text"] for item in pushes)
    assert any(item["text"].startswith("⏰") for item in pushes)
    markup = next(item.get("reply_markup") for item in pushes if item.get("reply_markup"))
    callback_data = [
        btn["callback_data"]
        for row in markup["inline_keyboard"]
        for btn in row
    ]
    assert any(f"r:{reminder['id']}:s10" == item for item in callback_data)
    assert any(item.endswith(":ok") for item in callback_data)
    storage.close()


def test_plain_reminder_respects_deliver_none(monkeypatch, tmp_path):
    agent = _FakeAgent()
    supervisor, storage = _supervisor(monkeypatch, tmp_path, agent=agent)
    pushes = _patch_push(monkeypatch)
    storage.create_reminder(
        text="тихий пинг только в web",
        due_at="2000-01-01T00:00:00+00:00",
        source_text="тихий",
        payload={"deliver": "none"},
    )

    asyncio.run(_fire_and_drain(supervisor))

    assert agent.calls == []
    assert pushes == []
    storage.close()


def test_disabled_flag_skips_the_agent_turn(monkeypatch, tmp_path):
    agent = _FakeAgent()
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, agent=agent, env={"JARVIS_SCHEDULED_TASKS_ENABLED": "0"}
    )
    _patch_push(monkeypatch)
    _due_task(storage, text="каждое утро сводка", prompt="сделай сводку")

    asyncio.run(_fire_and_drain(supervisor))

    assert agent.calls == []  # flag off -> the task reminder is a passive nudge
    storage.close()


def test_reminders_create_marks_daily_briefing(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    result = asyncio.run(
        tools.run(
            "reminders.create",
            {"text": "каждое утро в 9 присылай сводку по системе"},
            allow_danger=True,
        )
    )
    assert result.ok is True
    assert result.data["briefing"] is True
    assert result.data["agent_task"] is False
    payload = result.data["reminder"]["payload"]
    assert payload["kind"] == "briefing"
    storage.close()


def test_briefing_reminder_uses_experience_not_agent(monkeypatch, tmp_path):
    agent = _FakeAgent("should not run")
    supervisor, storage = _supervisor(monkeypatch, tmp_path, agent=agent)
    pushes = _patch_push(monkeypatch)

    class _Experience:
        def daily_briefing(self, dispatcher_status=None):
            return {
                "headline": "Runtime is stable",
                "operator_name": "Owner",
                "focus": ["Focus: ship Telegram remainder"],
                "risks": [],
                "suggestions": ["Check VRAM"],
                "pending_approvals": 0,
            }

    supervisor.autonomy_executor = SimpleNamespace(
        agent=agent, experience=_Experience()
    )
    storage.create_reminder(
        text="Утренняя сводка",
        due_at="2000-01-01T00:00:00+00:00",
        source_text="каждое утро сводка",
        payload={
            "kind": "briefing",
            "prompt": "daily_briefing",
            "deliver": "telegram",
            "telegram_chat_id": 42,
        },
    )

    asyncio.run(_fire_and_drain(supervisor))

    assert agent.calls == []
    assert any("Runtime is stable" in text for text in pushes)
    assert any(text.startswith("📋") for text in pushes)
    storage.close()


def test_passive_reminder_defers_during_quiet_hours(monkeypatch, tmp_path):
    agent = _FakeAgent()
    supervisor, storage = _supervisor(monkeypatch, tmp_path, agent=agent)
    pushes = _patch_push(monkeypatch)
    storage.set_runtime_value(
        "experience.preferences",
        {"quiet_hours": "00:00-23:59"},  # always quiet for this smoke
    )
    storage.create_reminder(
        text="тихий hold",
        due_at="2000-01-01T00:00:00+00:00",
        payload={"deliver": "telegram", "telegram_chat_id": 42},
    )
    asyncio.run(_fire_and_drain(supervisor))
    assert agent.calls == []
    assert pushes == []  # held, not pushed
    deferred = storage.get_runtime_value("telegram.quiet_deferred", [])
    assert isinstance(deferred, list) and deferred
    assert any("тихий hold" in str(item.get("text") or "") for item in deferred)
    # Leave quiet hours and flush.
    storage.set_runtime_value("experience.preferences", {"quiet_hours": ""})
    asyncio.run(supervisor._flush_quiet_deferred_pushes())
    assert any("тихий hold" in text for text in pushes)
    storage.close()


def test_storage_reschedule_snoozes_fired_reminder(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    reminder = storage.create_reminder(
        text="позвонить",
        due_at="2000-01-01T00:00:00+00:00",
        payload={"deliver": "telegram"},
    )
    # Simulate fire.
    with storage._lock:
        conn = storage.connect()
        conn.execute(
            "UPDATE reminders SET status='fired', fired_at=? WHERE id=?",
            ("2000-01-01T00:01:00+00:00", reminder["id"]),
        )
        conn.commit()
    updated = storage.reschedule_reminder(
        reminder["id"], due_at="2000-01-01T01:00:00+00:00"
    )
    assert updated is not None
    assert updated["status"] == "pending"
    assert updated["due_at"] == "2000-01-01T01:00:00+00:00"
    storage.close()
