"""Bounded proactive screen observation: parsing, capture/VLM, scheduling and delivery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from jarvis_gpt.agent import AgentContext, AgentRuntime, _operator_action_scopes
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.screen_watch import (
    ScreenConditionCheck,
    extract_screen_capture_path,
    parse_screen_condition_answer,
    parse_screen_watch_request,
)
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.supervisor import RuntimeSupervisor


class _VisionLLM:
    def __init__(self, answer: str = "YES\nУсловие видно.") -> None:
        self.answer = answer
        self.calls: list[list[dict]] = []

    async def complete(self, messages, **_kwargs):
        self.calls.append(messages)
        return SimpleNamespace(ok=True, content=self.answer)


class _CaptureTools:
    def __init__(self, path, *, ok: bool = True) -> None:
        self.path = path
        self.ok = ok
        self.calls: list[tuple[str, dict]] = []

    async def run(self, name, args, **_kwargs):
        self.calls.append((name, args))
        if not self.ok:
            return ToolRunResponse(tool=name, ok=False, summary="bridge offline")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"not-a-real-png-but-bounded")
        return ToolRunResponse(
            tool=name,
            ok=True,
            summary="captured",
            data={
                "native": {
                    "result": {"data": {"path": str(self.path)}}
                }
            },
        )


class _WatchAgent:
    def __init__(self, check: ScreenConditionCheck) -> None:
        self.check = check
        self.calls: list[str] = []

    async def check_screen_condition(self, condition: str):
        self.calls.append(condition)
        return self.check


def _settings_storage(monkeypatch, tmp_path, *, env=None, profile="qwen36-vl"):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_IDS", "42")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, str(value))
    settings = load_settings(profile)
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return settings, storage


def _watch_reminder(storage, *, keep=False, expired=False, conversation_id=None):
    expires = datetime.now(UTC) + timedelta(hours=1)
    if expired:
        expires = datetime.now(UTC) - timedelta(seconds=31)
    return storage.create_reminder(
        text="Наблюдение за экраном: сборка завершена",
        due_at="2000-01-01T00:00:00+00:00",
        recurrence={"kind": "interval", "seconds": 300},
        conversation_id=conversation_id,
        payload={
            "kind": "screen_watch",
            "condition": "сборка завершена",
            "keep": keep,
            "expires_at": expires.isoformat(),
            "deliver": "telegram",
            "telegram_chat_id": 42,
        },
    )


def _staged_notice(storage, settings, *, conversation_id=None):
    reminder = _watch_reminder(storage, conversation_id=conversation_id)
    claimed = storage.claim_due_reminders(
        datetime.now(UTC).isoformat(),
        tz_name=settings.reminder_tz,
    )
    assert [item["id"] for item in claimed] == [reminder["id"]]
    staged = storage.stage_screen_watch_notification(
        reminder["id"],
        expected_fire_count=1,
        terminal_status="fired",
        text="condition met",
        event_kind="screen_watch.fire",
        level="info",
        met=True,
    )
    assert staged is not None
    return staged


async def _fire_and_drain(supervisor: RuntimeSupervisor) -> None:
    await supervisor._fire_due_reminders()
    pending = list(supervisor._screen_watch_runs.values())
    if pending:
        await asyncio.gather(*pending)


def _supervisor(settings, storage, agent):
    executor = SimpleNamespace(agent=agent) if agent is not None else None
    return RuntimeSupervisor(settings=settings, storage=storage, autonomy_executor=executor)


def _patch_push(monkeypatch):
    pushes: list[str] = []

    async def fake_push(text, **_kwargs):
        pushes.append(text)
        return True

    monkeypatch.setattr("jarvis_gpt.supervisor.push_telegram_alert", fake_push)
    return pushes


def test_parser_recognises_ru_interval_duration_and_floor():
    parsed = parse_screen_watch_request(
        "Следи за моим экраном каждые 30 секунд в течение 2 часов и скажи когда СБОРКА ГОТОВА",
        min_interval_sec=120,
        default_duration_sec=7200,
        max_duration_sec=21600,
    )
    assert parsed is not None
    assert parsed.condition == "СБОРКА ГОТОВА"
    assert parsed.interval_sec == 120
    assert parsed.duration_sec == 7200
    assert parsed.interval_clamped is True
    assert parsed.keep is False


def test_parser_english_keep_and_one_shot_screen_is_ignored():
    parsed = parse_screen_watch_request(
        "Keep watching my screen every 5 minutes and tell me when Download finished for 1 hour",
        min_interval_sec=120,
        default_duration_sec=7200,
        max_duration_sec=21600,
    )
    assert parsed is not None
    assert parsed.keep is True
    assert parsed.interval_sec == 300
    assert parsed.duration_sec == 3600
    assert parsed.condition == "Download finished"
    assert (
        parse_screen_watch_request(
            "посмотри на экран и скажи, что там",
            min_interval_sec=120,
            default_duration_sec=7200,
            max_duration_sec=21600,
        )
        is None
    )


def test_condition_protocol_and_nested_path_are_strict():
    yes = parse_screen_condition_answer("YES — окно готово\nКнопка активна")
    no = parse_screen_condition_answer("НЕТ\nИндикатор ещё идёт")
    malformed = parse_screen_condition_answer("Похоже, готово")
    assert yes.met is True and "окно готово" in yes.detail
    assert no.met is False
    assert malformed.met is None and malformed.error
    assert (
        extract_screen_capture_path(
            {"native": {"result": {"data": {"path": "D:/jarvis/screen.png"}}}}
        )
        == "D:/jarvis/screen.png"
    )


def test_agent_capture_classifies_and_deletes_temporary_png(monkeypatch, tmp_path):
    settings, storage = _settings_storage(monkeypatch, tmp_path)
    shot = settings.cache_dir / "screens" / "screen-test.png"
    tools = _CaptureTools(shot)
    llm = _VisionLLM("YES\nГотово отображается в окне.")
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, tools=tools)

    result = asyncio.run(agent.check_screen_condition("сборка завершена"))

    assert result.met is True
    assert "Готово" in result.detail
    assert tools.calls[0][0] == "system.inspect"
    assert tools.calls[0][1]["payload"]["ocr"] is False
    assert not shot.exists()
    assert "недоверенные данные" in llm.calls[0][0]["content"]
    storage.close()


def test_non_vision_profile_does_not_capture(monkeypatch, tmp_path):
    settings, storage = _settings_storage(monkeypatch, tmp_path, profile="gemma4-turbo")
    tools = _CaptureTools(settings.cache_dir / "screens" / "screen-never.png")
    agent = AgentRuntime(settings=settings, storage=storage, llm=_VisionLLM(), tools=tools)

    result = asyncio.run(agent.check_screen_condition("готово"))

    assert result.met is None
    assert tools.calls == []
    storage.close()


def test_direct_route_creates_bounded_watch_before_one_shot_capture(monkeypatch, tmp_path):
    settings, storage = _settings_storage(
        monkeypatch,
        tmp_path,
        env={"JARVIS_OPERATOR_FULL_AUTONOMY": "1"},
    )
    agent = AgentRuntime(settings=settings, storage=storage, llm=_VisionLLM())
    context = AgentContext(
        conversation_id="conv-1",
        memory_hits=[],
        file_hits=[],
        notification_chat_id=42,
    )

    action = agent._screen_watch_direct_action(
        "Следи за экраном и скажи когда появится окно Успех", context
    )

    assert action is not None and "Слежу за экраном" in action.answer
    item = storage.list_reminders(status="pending")[0]
    assert item["payload"]["kind"] == "screen_watch"
    assert item["payload"]["condition"] == "появится окно Успех"
    assert item["payload"]["telegram_chat_id"] == 42
    assert item["recurrence"] == {"kind": "interval", "seconds": 300}
    assert item["conversation_id"] == "conv-1"
    storage.close()


def test_watch_phrasing_is_an_explicit_operator_capture_command():
    scopes = _operator_action_scopes(
        "Следи за экраном и скажи, когда появится окно Успех"
    )

    assert {"explicit", "capture", "native"}.issubset(scopes)


def test_supported_watch_grammar_uses_current_operator_turn_not_generic_scopes(
    monkeypatch, tmp_path
):
    settings, storage = _settings_storage(
        monkeypatch,
        tmp_path,
        env={"JARVIS_OPERATOR_FULL_AUTONOMY": "0"},
    )
    agent = AgentRuntime(settings=settings, storage=storage, llm=_VisionLLM())
    message = "keep watching my screen and tell me when Download finished"
    context = AgentContext(
        conversation_id="conv-supported-grammar",
        memory_hits=[],
        file_hits=[],
        operator_message=message,
        operator_message_id="message-1",
        operator_scopes=frozenset(),
    )

    action = agent._screen_watch_direct_action(message, context)

    assert action is not None
    assert storage.list_reminders(status="pending")[0]["payload"]["condition"] == (
        "Download finished"
    )
    storage.close()


def test_direct_route_enforces_max_active(monkeypatch, tmp_path):
    settings, storage = _settings_storage(
        monkeypatch,
        tmp_path,
        env={"JARVIS_OPERATOR_FULL_AUTONOMY": "1", "JARVIS_SCREEN_WATCH_MAX_ACTIVE": "1"},
    )
    _watch_reminder(storage)
    agent = AgentRuntime(settings=settings, storage=storage, llm=_VisionLLM())

    action = agent._screen_watch_direct_action(
        "наблюдай за экраном и сообщи когда появится Готово", None
    )

    assert action is not None and "лимит 1" in action.answer
    assert len(storage.list_reminders(status="pending")) == 1
    storage.close()


def test_true_one_shot_pushes_once_and_cancels(monkeypatch, tmp_path):
    settings, storage = _settings_storage(monkeypatch, tmp_path)
    agent = _WatchAgent(ScreenConditionCheck(True, "Вижу зелёную галочку."))
    supervisor = _supervisor(settings, storage, agent)
    pushes = _patch_push(monkeypatch)
    reminder = _watch_reminder(storage)

    asyncio.run(_fire_and_drain(supervisor))

    assert agent.calls == ["сборка завершена"]
    assert len(pushes) == 1 and "сборка завершена" in pushes[0]
    assert storage.get_reminder(reminder["id"])["status"] == "fired"
    storage.close()


def test_true_keep_stays_pending_and_false_or_error_are_fail_soft(monkeypatch, tmp_path):
    for check in (
        ScreenConditionCheck(True, "Совпало."),
        ScreenConditionCheck(False, "Ещё нет."),
        ScreenConditionCheck(None, error="temporary VLM failure"),
    ):
        case_dir = tmp_path / str(check.met)
        settings, storage = _settings_storage(monkeypatch, case_dir)
        agent = _WatchAgent(check)
        supervisor = _supervisor(settings, storage, agent)
        pushes = _patch_push(monkeypatch)
        reminder = _watch_reminder(storage, keep=True)

        asyncio.run(_fire_and_drain(supervisor))

        assert storage.get_reminder(reminder["id"])["status"] == "pending"
        assert len(pushes) == (1 if check.met is True else 0)
        storage.close()


def test_expired_watch_cancels_without_capture_and_notifies(monkeypatch, tmp_path):
    settings, storage = _settings_storage(monkeypatch, tmp_path)
    agent = _WatchAgent(ScreenConditionCheck(True))
    supervisor = _supervisor(settings, storage, agent)
    pushes = _patch_push(monkeypatch)
    reminder = _watch_reminder(storage, expired=True)

    asyncio.run(_fire_and_drain(supervisor))

    assert agent.calls == []
    assert len(pushes) == 1 and "истечения срока" in pushes[0]
    assert storage.get_reminder(reminder["id"])["status"] == "cancelled"
    storage.close()


def test_poll_scheduled_at_expiry_uses_scheduler_cadence_grace(monkeypatch, tmp_path):
    settings, storage = _settings_storage(
        monkeypatch,
        tmp_path,
        env={"JARVIS_REMINDER_INTERVAL_SEC": "60"},
    )
    agent = _WatchAgent(ScreenConditionCheck(False, "not yet"))
    supervisor = _supervisor(settings, storage, agent)
    pushes = _patch_push(monkeypatch)
    boundary = datetime.now(UTC) - timedelta(seconds=40)
    reminder = storage.create_reminder(
        text="boundary watch",
        due_at=boundary.isoformat(),
        recurrence={"kind": "interval", "seconds": 300},
        payload={
            "kind": "screen_watch",
            "condition": "build complete",
            "keep": False,
            "expires_at": boundary.isoformat(),
            "deliver": "telegram",
            "telegram_chat_id": 42,
        },
    )

    asyncio.run(_fire_and_drain(supervisor))

    assert agent.calls == ["build complete"]
    assert storage.get_reminder(reminder["id"])["status"] == "cancelled"
    assert len(pushes) == 1
    storage.close()


def test_disabled_watch_never_falls_through_as_passive_reminder(monkeypatch, tmp_path):
    settings, storage = _settings_storage(
        monkeypatch, tmp_path, env={"JARVIS_SCREEN_WATCH_ENABLED": "0"}
    )
    agent = _WatchAgent(ScreenConditionCheck(True))
    supervisor = _supervisor(settings, storage, agent)
    pushes = _patch_push(monkeypatch)
    reminder = _watch_reminder(storage)

    asyncio.run(_fire_and_drain(supervisor))

    assert agent.calls == [] and pushes == []
    current = storage.get_reminder(reminder["id"])
    assert current["status"] == "pending"
    assert current["fire_count"] == 0
    assert current["due_at"] == reminder["due_at"]
    storage.close()


def test_inflight_watch_is_not_started_twice(monkeypatch, tmp_path):
    settings, storage = _settings_storage(monkeypatch, tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowAgent:
        def __init__(self):
            self.calls = 0

        async def check_screen_condition(self, _condition):
            self.calls += 1
            started.set()
            await release.wait()
            return ScreenConditionCheck(False)

    agent = _SlowAgent()
    supervisor = _supervisor(settings, storage, agent)
    _patch_push(monkeypatch)
    reminder = _watch_reminder(storage, keep=True)

    async def scenario():
        await supervisor._fire_due_reminders()
        await started.wait()
        with storage._lock:
            conn = storage.connect()
            conn.execute(
                "UPDATE reminders SET due_at = ? WHERE id = ?",
                ("2000-01-01T00:00:00+00:00", reminder["id"]),
            )
            conn.commit()
        await supervisor._fire_due_reminders()
        assert agent.calls == 1
        release.set()
        await asyncio.gather(*list(supervisor._screen_watch_runs.values()))

    asyncio.run(scenario())
    assert agent.calls == 1
    storage.close()


def test_cancelled_inflight_watch_cannot_deliver(monkeypatch, tmp_path):
    settings, storage = _settings_storage(monkeypatch, tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowPositiveAgent:
        async def check_screen_condition(self, _condition):
            started.set()
            await release.wait()
            return ScreenConditionCheck(True, "untrusted detail")

    supervisor = _supervisor(settings, storage, _SlowPositiveAgent())
    pushes = _patch_push(monkeypatch)
    reminder = _watch_reminder(storage)

    async def scenario():
        await supervisor._fire_due_reminders()
        await started.wait()
        assert storage.cancel_reminder(reminder["id"]) is not None
        release.set()
        await asyncio.gather(*list(supervisor._screen_watch_runs.values()))

    asyncio.run(scenario())

    assert pushes == []
    assert storage.get_reminder(reminder["id"])["status"] == "cancelled"
    storage.close()


def test_failed_telegram_delivery_is_retried_from_persisted_outbox(monkeypatch, tmp_path):
    settings, storage = _settings_storage(monkeypatch, tmp_path)
    agent = _WatchAgent(ScreenConditionCheck(True, "do not persist this detail"))
    supervisor = _supervisor(settings, storage, agent)
    attempts: list[str] = []

    async def flaky_push(text, **_kwargs):
        attempts.append(text)
        return len(attempts) > 1

    monkeypatch.setattr("jarvis_gpt.supervisor.push_telegram_alert", flaky_push)
    reminder = _watch_reminder(storage)

    async def scenario():
        await _fire_and_drain(supervisor)
        first = storage.get_reminder(reminder["id"])
        assert first["status"] == "fired"
        assert first["payload"]["notification"]["state"] == "pending"
        await supervisor._fire_due_reminders()

    asyncio.run(scenario())

    current = storage.get_reminder(reminder["id"])
    assert len(attempts) == 2
    assert agent.calls == ["сборка завершена"]
    assert current["payload"]["notification"]["state"] == "delivered"
    assert "do not persist this detail" not in str(current)
    storage.close()


def test_telegram_outbox_retries_only_undelivered_recipient(monkeypatch, tmp_path):
    settings, storage = _settings_storage(
        monkeypatch,
        tmp_path,
        env={
            "TELEGRAM_ALLOWED_CHAT_IDS": "42,99",
            "TELEGRAM_ALERT_CHAT_IDS": "42,99",
        },
    )
    agent = _WatchAgent(ScreenConditionCheck(True))
    supervisor = _supervisor(settings, storage, agent)
    attempts: list[int] = []
    failed_once = False

    async def partially_flaky_push(_text, *, target_chat_ids, **_kwargs):
        nonlocal failed_once
        target = tuple(target_chat_ids)[0]
        attempts.append(target)
        if target == 99 and not failed_once:
            failed_once = True
            return False
        return True

    monkeypatch.setattr("jarvis_gpt.supervisor.push_telegram_alert", partially_flaky_push)
    expires = datetime.now(UTC) + timedelta(hours=1)
    reminder = storage.create_reminder(
        text="multi-target watch",
        due_at="2000-01-01T00:00:00+00:00",
        recurrence={"kind": "interval", "seconds": 300},
        payload={
            "kind": "screen_watch",
            "condition": "build complete",
            "keep": False,
            "expires_at": expires.isoformat(),
            "deliver": "telegram",
        },
    )

    async def scenario():
        await _fire_and_drain(supervisor)
        first = storage.get_reminder(reminder["id"])["payload"]["notification"]
        assert first["state"] == "pending"
        assert first["telegram_target_ids"] == [42, 99]
        assert first["telegram_delivered_ids"] == [42]
        await supervisor._fire_due_reminders()

    asyncio.run(scenario())

    current = storage.get_reminder(reminder["id"])["payload"]["notification"]
    assert attempts == [42, 99, 99]
    assert current["state"] == "delivered"
    assert current["telegram_delivered_ids"] == [42, 99]
    storage.close()


def test_notification_delivery_has_single_in_process_lease(monkeypatch, tmp_path):
    settings, storage = _settings_storage(monkeypatch, tmp_path)
    supervisor = _supervisor(settings, storage, None)
    staged = _staged_notice(storage, settings)
    started = asyncio.Event()
    release = asyncio.Event()
    attempts: list[int] = []

    async def slow_push(_text, *, target_chat_ids, **_kwargs):
        attempts.append(tuple(target_chat_ids)[0])
        started.set()
        await release.wait()
        return True

    monkeypatch.setattr("jarvis_gpt.supervisor.push_telegram_alert", slow_push)

    async def scenario():
        first = asyncio.create_task(supervisor._deliver_screen_watch_notice(staged))
        await started.wait()
        second = asyncio.create_task(supervisor._deliver_screen_watch_notice(staged))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(scenario())
    # A flush can retain an old pending snapshot after the first sender completed.
    asyncio.run(supervisor._deliver_screen_watch_notice(staged))

    assert attempts == [42]
    notification = storage.get_reminder(staged["id"])["payload"]["notification"]
    assert notification["state"] == "delivered"
    storage.close()


def test_local_outbox_delivery_rolls_back_artifacts_and_retries(monkeypatch, tmp_path):
    settings, storage = _settings_storage(monkeypatch, tmp_path)
    conversation_id = storage.create_conversation("watch test")
    staged = _staged_notice(storage, settings, conversation_id=conversation_id)
    supervisor = _supervisor(settings, storage, None)
    pushes = _patch_push(monkeypatch)
    original_insert = storage._insert_learning_observation

    def fail_after_message_insert(*_args, **_kwargs):
        raise RuntimeError("simulated sqlite failure")

    monkeypatch.setattr(storage, "_insert_learning_observation", fail_after_message_insert)
    asyncio.run(supervisor._deliver_screen_watch_notice(staged))

    first = storage.get_reminder(staged["id"])
    assert first["payload"]["notification"]["state"] == "pending"
    assert first["payload"]["notification"]["local_delivered"] is False
    assert storage.list_messages(conversation_id) == []
    assert not [event for event in storage.list_events() if event["kind"] == "screen_watch.fire"]

    monkeypatch.setattr(storage, "_insert_learning_observation", original_insert)
    asyncio.run(supervisor._deliver_screen_watch_notice(first))

    final = storage.get_reminder(staged["id"])["payload"]["notification"]
    assert final["state"] == "delivered"
    assert len(storage.list_messages(conversation_id)) == 1
    watch_events = [
        event for event in storage.list_events() if event["kind"] == "screen_watch.fire"
    ]
    assert len(watch_events) == 1
    assert len(pushes) == 1
    storage.close()
