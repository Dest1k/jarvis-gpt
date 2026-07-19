from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from jarvis_gpt.agent import AgentContext, AgentRuntime, _operator_action_scopes
from jarvis_gpt.authorization import (
    LEGACY_OWNER_USER_ID,
    ActorContext,
    AuthorizationError,
    AuthorizationService,
    bind_actor,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.supervisor import RuntimeSupervisor
from jarvis_gpt.tools import (
    MAX_ACTIVE_RECURRING_AGENT_TASKS_PER_USER,
    MODEL_NATIVE_ACTIONS,
    WINDOWS_NATIVE_ACTION_SECURITY_IDS,
)


class _CapturingLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def complete(self, messages, **_kwargs):
        self.calls.append(messages)
        return SimpleNamespace(ok=True, content="Ограниченный ответ")


def _runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = _CapturingLLM()
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm)
    return agent, storage, llm, AuthorizationService(storage)


def _actor(identity: dict[str, object], *, preset_key: str | None = None) -> ActorContext:
    return ActorContext(
        user_id=str(identity["user_id"]),
        preset_key=preset_key or str(identity["preset_key"]),
        source="test-session",
        identity_id=str(identity["identity_id"]),
        policy_epoch=int(identity["policy_epoch"]),
    )


def _create_chat_only_preset(
    service: AuthorizationService,
    *,
    user_id: str,
    key: str = "chat_only_test",
) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    preset_id = f"preset_{uuid.uuid4().hex}"
    version_id = f"presetv_{uuid.uuid4().hex}"
    with service.storage.transaction(immediate=True) as conn:
        chat_capability = conn.execute(
            "SELECT id FROM security_ids WHERE security_id = 'chat.use'"
        ).fetchone()
        assert chat_capability is not None
        conn.execute(
            """
            INSERT INTO permission_presets(
                id, preset_key, display_name, kind, active_version_id,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, 'custom', NULL, ?, ?, ?)
            """,
            (preset_id, key, "Chat only", LEGACY_OWNER_USER_ID, now, now),
        )
        conn.execute(
            """
            INSERT INTO permission_preset_versions(
                id, preset_id, version, state, created_by, created_at,
                published_at, change_reason
            ) VALUES (?, ?, 1, 'published', ?, ?, ?, 'test')
            """,
            (version_id, preset_id, LEGACY_OWNER_USER_ID, now, now),
        )
        conn.execute(
            """
            INSERT INTO preset_security_ids(
                preset_version_id, security_id_id, effect, can_delegate
            ) VALUES (?, ?, 'grant', 0)
            """,
            (version_id, chat_capability["id"]),
        )
        conn.execute(
            "UPDATE permission_presets SET active_version_id = ? WHERE id = ?",
            (version_id, preset_id),
        )
    service.assign_preset(
        user_id=user_id,
        preset_key=key,
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="test chat-only preset",
    )


def test_custom_chat_only_uses_restricted_surface_without_personal_context(
    monkeypatch, tmp_path
) -> None:
    agent, storage, llm, service = _runtime(monkeypatch, tmp_path)
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="local",
        provider_subject_id="chat-only",
    )
    _create_chat_only_preset(service, user_id=str(identity["user_id"]))
    actor = service.actor_for_user(str(identity["user_id"]), source="test-session")
    assert actor is not None and actor.preset_key == "chat_only_test"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("chat-only principal attempted to read personal context")

    monkeypatch.setattr(storage, "search_memory", forbidden)
    monkeypatch.setattr(storage, "search_file_chunks", forbidden)
    monkeypatch.setattr("jarvis_gpt.persona.load_persona", forbidden)

    with bind_actor(actor):
        response = asyncio.run(agent.chat("Привет"))

    assert response.answer == "Ограниченный ответ"
    assert response.events == []
    prompt = str(llm.calls[0])
    assert "нет доступа к экрану, файлам, памяти" in prompt
    storage.close()


def test_explicit_denies_remove_memory_file_and_persona_context_and_revoke_session(
    monkeypatch, tmp_path
) -> None:
    agent, storage, _llm, service = _runtime(monkeypatch, tmp_path)
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="local",
        provider_subject_id="standard-user",
        bootstrap_preset="user",
    )
    user_id = str(identity["user_id"])
    original_actor = service.actor_for_user(user_id, source="test-session")
    assert original_actor is not None

    with bind_actor(original_actor):
        storage.add_memory(
            content="MEMORY_SECRET_SENTINEL",
            namespace="profile",
            tags=["test"],
        )
        storage.set_runtime_value(
            "experience.persona",
            {"display_name": "PERSONA_SECRET_SENTINEL"},
        )
        stored_path = tmp_path / "FILE_SECRET_SENTINEL.txt"
        stored_path.write_text("FILE_SECRET_SENTINEL", encoding="utf-8")
        file_record = storage.create_file_record(
            name=stored_path.name,
            stored_path=stored_path,
            sha256=hashlib.sha256(stored_path.read_bytes()).hexdigest(),
            size=stored_path.stat().st_size,
            mime_type="text/plain",
            status="ready",
        )
        storage.add_file_chunks(file_record["id"], ["FILE_SECRET_SENTINEL"])

    session = service.create_user_session(
        user_id=user_id,
        identity_id=str(identity["identity_id"]),
        auth_method="test",
    )
    assert service.authenticate_session(str(session["session_token"])) is not None

    for security_id in ("memory.read.own", "files.read.own", "persona.read.own"):
        decision = service.set_user_permission(
            user_id=user_id,
            security_id=security_id,
            effect="deny",
            can_delegate=False,
            granted_by=LEGACY_OWNER_USER_ID,
            reason="least privilege test",
        )
        assert decision["effect"] == "deny"
        assert decision["reason_code"] == "explicit_deny"

    assert service.authenticate_session(str(session["session_token"])) is None
    actor = service.actor_for_user(user_id, source="test-session")
    assert actor is not None
    with bind_actor(actor):
        capabilities = agent._context_capabilities()
        context = agent._prepare_context(
            "секрет",
            None,
            capabilities=capabilities,
        )
        messages = agent._build_llm_messages(context, "секрет")

    assert capabilities["can_read_memory"] is False
    assert capabilities["can_read_files"] is False
    assert capabilities["can_read_persona"] is False
    assert context.memory_hits == []
    assert context.file_hits == []
    rendered = str(messages)
    assert "MEMORY_SECRET_SENTINEL" not in rendered
    assert "FILE_SECRET_SENTINEL" not in rendered
    assert "PERSONA_SECRET_SENTINEL" not in rendered
    storage.close()


def test_explicit_chat_deny_overrides_chat_only_preset(monkeypatch, tmp_path) -> None:
    agent, storage, llm, service = _runtime(monkeypatch, tmp_path)
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="local",
        provider_subject_id="chat-denied",
    )
    user_id = str(identity["user_id"])
    _create_chat_only_preset(service, user_id=user_id, key="chat_denied_test")
    service.set_user_permission(
        user_id=user_id,
        security_id="chat.use",
        effect="deny",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="test deny precedence",
    )
    actor = service.actor_for_user(user_id, source="test-session")
    assert actor is not None

    with bind_actor(actor), pytest.raises(AuthorizationError, match="explicit_deny"):
        asyncio.run(agent.chat("Привет"))

    assert llm.calls == []
    storage.close()


def test_mission_create_rechecks_missions_write_before_storage(monkeypatch, tmp_path) -> None:
    agent, storage, _llm, service = _runtime(monkeypatch, tmp_path)
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="local",
        provider_subject_id="mission-denied",
        bootstrap_preset="user",
    )
    user_id = str(identity["user_id"])
    service.set_user_permission(
        user_id=user_id,
        security_id="missions.write.own",
        effect="deny",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="mission deny test",
    )
    actor = service.actor_for_user(user_id, source="test-session")
    assert actor is not None

    with bind_actor(actor), pytest.raises(AuthorizationError, match="missions.write.own"):
        agent.create_mission("Create a mission that must not persist")

    with bind_actor(actor):
        assert storage.list_missions(limit=10) == []
    storage.close()


def test_mission_execution_resume_and_abort_recheck_write_permission(
    monkeypatch, tmp_path
) -> None:
    agent, storage, _llm, service = _runtime(monkeypatch, tmp_path)
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="local",
        provider_subject_id="mission-mutation-denied",
        bootstrap_preset="user",
    )
    user_id = str(identity["user_id"])
    actor = service.actor_for_user(user_id, source="test-session")
    assert actor is not None
    with bind_actor(actor):
        mission = agent.create_mission("Run a guarded mission")
        task_id = str(mission["tasks"][0]["id"])
        storage.update_mission_task(
            task_id,
            mission_id=str(mission["id"]),
            status="blocked",
            notes="awaiting approval",
        )
    service.set_user_permission(
        user_id=user_id,
        security_id="missions.write.own",
        effect="deny",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="revoke mission mutations",
    )
    denied_actor = service.actor_for_user(user_id, source="test-session")
    assert denied_actor is not None
    approval = {
        "id": "approval-test",
        "payload": {"mission_id": mission["id"], "task_id": task_id},
    }
    with bind_actor(denied_actor):
        with pytest.raises(AuthorizationError, match="missions.write.own"):
            asyncio.run(agent.execute_next_mission_step(str(mission["id"])))
        with pytest.raises(AuthorizationError, match="missions.write.own"):
            asyncio.run(agent.run_mission(str(mission["id"])))
        resumed = asyncio.run(
            agent.resume_mission_after_approval(
                approval,
                ToolRunResponse(tool="test", ok=True, summary="approved"),
            )
        )
        aborted = asyncio.run(agent.abort_mission_after_approval(approval, "test abort"))
        current = storage.get_mission(str(mission["id"]))

    assert resumed is not None and resumed.data["authorization_denied"] is True
    assert aborted is not None and aborted.data["authorization_denied"] is True
    assert current is not None
    task = next(item for item in current["tasks"] if item["id"] == task_id)
    assert task["status"] == "blocked"
    assert task["notes"] == "awaiting approval"
    storage.close()


def test_screen_watch_create_requires_dedicated_capability(monkeypatch, tmp_path) -> None:
    agent, storage, _llm, service = _runtime(monkeypatch, tmp_path)
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="local",
        provider_subject_id="screen-watch-denied",
        bootstrap_preset="user",
    )
    user_id = str(identity["user_id"])
    service.set_user_permission(
        user_id=user_id,
        security_id="background.screen_watch.create",
        effect="deny",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="screen watch deny test",
    )
    actor = service.actor_for_user(user_id, source="test-session")
    assert actor is not None
    message = "Следи за экраном каждые 5 минут и скажи когда появится Успех"
    with bind_actor(actor):
        conversation_id = storage.create_conversation("screen watch test")
        context = AgentContext(
            conversation_id=conversation_id,
            memory_hits=[],
            file_hits=[],
            operator_message_id="msg-screen-watch",
            operator_scopes=_operator_action_scopes(message),
            actor=actor,
        )
        with pytest.raises(AuthorizationError, match="background.screen_watch.create"):
            agent._screen_watch_direct_action(message, context)
        assert storage.list_reminders(status="all") == []
    storage.close()


def test_screen_watch_scheduler_rechecks_privacy_permission(monkeypatch, tmp_path) -> None:
    agent, storage, _llm, service = _runtime(monkeypatch, tmp_path)
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="local",
        provider_subject_id="screen-watch-scheduler-denied",
        bootstrap_preset="user",
    )
    user_id = str(identity["user_id"])
    for security_id in ("background.screen_watch.create", "privacy.screen.capture"):
        service.set_user_permission(
            user_id=user_id,
            security_id=security_id,
            effect="grant",
            can_delegate=False,
            granted_by=LEGACY_OWNER_USER_ID,
            reason="prepare watcher",
        )
    actor = service.actor_for_user(user_id, source="test-session")
    assert actor is not None
    with bind_actor(actor):
        reminder = storage.create_reminder(
            text="watch screen",
            due_at="2000-01-01T00:00:00+00:00",
            recurrence={"kind": "interval", "seconds": 3600},
            payload={
                "kind": "screen_watch",
                "condition": "success",
                "expires_at": "2999-01-01T00:00:00+00:00",
            },
        )
    service.set_user_permission(
        user_id=user_id,
        security_id="privacy.screen.capture",
        effect="deny",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="revoke screen privacy",
    )
    fake_agent = SimpleNamespace(calls=[])

    async def forbidden_check(condition):
        fake_agent.calls.append(condition)
        raise AssertionError("denied watcher captured the screen")

    fake_agent.check_screen_condition = forbidden_check
    supervisor = RuntimeSupervisor(
        settings=agent.settings,
        storage=storage,
        autonomy_executor=SimpleNamespace(agent=fake_agent),
    )
    asyncio.run(supervisor._fire_due_reminders())

    assert fake_agent.calls == []
    check_actor = service.actor_for_user(user_id, source="test-check")
    assert check_actor is not None
    with bind_actor(check_actor):
        current = storage.get_reminder(reminder["id"])
    assert current is not None and current["status"] == "cancelled"
    storage.close()


def test_privacy_and_native_action_denies_stop_before_host_bridge(monkeypatch, tmp_path) -> None:
    agent, storage, _llm, service = _runtime(monkeypatch, tmp_path)
    assert set(WINDOWS_NATIVE_ACTION_SECURITY_IDS) == set(MODEL_NATIVE_ACTIONS)
    bridge_calls: list[str] = []

    async def forbidden_bridge(_ctx, action, _payload, _timeout):
        bridge_calls.append(action)
        raise AssertionError("denied host action reached the bridge")

    monkeypatch.setattr("jarvis_gpt.tools._run_native_bridge_command", forbidden_bridge)
    cases = (
        ("system.inspect", "screen.capture", "privacy.screen.capture", False),
        ("system.inspect", "clipboard.read", "privacy.clipboard.read", False),
        ("windows.native", "process.start", "native.process.start", True),
        ("windows.native", "clipboard.write", "privacy.clipboard.write", True),
    )
    for tool_name, action, security_id, allow_danger in cases:
        service.set_user_permission(
            user_id=LEGACY_OWNER_USER_ID,
            security_id=security_id,
            effect="deny",
            can_delegate=False,
            granted_by=LEGACY_OWNER_USER_ID,
            reason="nested action PEP test",
        )
        result = asyncio.run(
            agent.tools.run(
                tool_name,
                {"action": action, "payload": {"text": "secret"}},
                allow_danger=allow_danger,
            )
        )
        assert result.ok is False
        assert result.data["security_id"] == security_id
        assert result.data["policy_decision"]["code"] == "security_id_denied"
    assert bridge_calls == []
    storage.close()


def test_scheduled_task_create_is_separate_from_passive_reminder(monkeypatch, tmp_path) -> None:
    agent, storage, _llm, service = _runtime(monkeypatch, tmp_path)
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="local",
        provider_subject_id="scheduled-create-denied",
        bootstrap_preset="user",
    )
    user_id = str(identity["user_id"])
    service.set_user_permission(
        user_id=user_id,
        security_id="background.scheduled_task.create",
        effect="deny",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="scheduled task deny test",
    )
    actor = service.actor_for_user(user_id, source="test-session")
    assert actor is not None
    with bind_actor(actor):
        passive = asyncio.run(
            agent.tools.run(
                "reminders.create",
                {"text": "напомни завтра в 10 позвонить маме"},
            )
        )
        active = asyncio.run(
            agent.tools.run(
                "reminders.create",
                {"text": "каждый день в 9 присылай сводку по ИИ"},
            )
        )

    assert passive.ok is True and passive.data["agent_task"] is False
    assert active.ok is False
    assert active.data["security_id"] == "background.scheduled_task.create"
    assert service.authorize(
        user_id, "background.scheduled_task.create", record=False
    ).reason_code == "explicit_deny"
    storage.close()


def test_recurring_scheduled_task_limit_is_per_user(monkeypatch, tmp_path) -> None:
    agent, storage, _llm, _service = _runtime(monkeypatch, tmp_path)
    for index in range(MAX_ACTIVE_RECURRING_AGENT_TASKS_PER_USER):
        result = asyncio.run(
            agent.tools.run(
                "reminders.create",
                {"text": f"каждый день в 9 присылай сводку номер {index}"},
                allow_danger=True,
            )
        )
        assert result.ok is True
    overflow = asyncio.run(
        agent.tools.run(
            "reminders.create",
            {"text": "каждый день в 10 присылай ещё одну сводку"},
            allow_danger=True,
        )
    )
    assert overflow.ok is False
    assert overflow.data["limit"] == MAX_ACTIVE_RECURRING_AGENT_TASKS_PER_USER
    storage.close()


def test_scheduled_task_requires_action_level_approval(monkeypatch, tmp_path) -> None:
    agent, storage, _llm, _service = _runtime(monkeypatch, tmp_path)
    result = asyncio.run(
        agent.tools.run(
            "reminders.create",
            {"text": "каждый день в 9 присылай сводку"},
        )
    )

    assert result.ok is False
    assert result.data["policy_decision"]["code"] == "approval_required"
    assert result.data["approval_action"] == "tool.run"
    assert storage.list_reminders(status="all") == []
    storage.close()


def test_scheduler_rechecks_execute_permission_and_cancels_recurring_task(
    monkeypatch, tmp_path
) -> None:
    agent, storage, _llm, service = _runtime(monkeypatch, tmp_path)
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="local",
        provider_subject_id="scheduled-execute-denied",
        bootstrap_preset="user",
    )
    user_id = str(identity["user_id"])
    actor = service.actor_for_user(user_id, source="test-session")
    assert actor is not None
    with bind_actor(actor):
        reminder = storage.create_reminder(
            text="recurring agent task",
            due_at="2000-01-01T00:00:00+00:00",
            recurrence={"kind": "interval", "seconds": 3600},
            payload={"kind": "agent_task", "prompt": "inspect the system"},
        )
    service.set_user_permission(
        user_id=user_id,
        security_id="background.scheduled_task.execute",
        effect="deny",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="scheduled execute deny test",
    )
    fake_agent = SimpleNamespace(calls=[])

    async def forbidden_chat(*args, **kwargs):
        fake_agent.calls.append((args, kwargs))
        raise AssertionError("denied scheduled task executed")

    fake_agent.chat = forbidden_chat
    supervisor = RuntimeSupervisor(
        settings=agent.settings,
        storage=storage,
        autonomy_executor=SimpleNamespace(agent=fake_agent),
    )
    asyncio.run(supervisor._fire_due_reminders())

    assert fake_agent.calls == []
    check_actor = service.actor_for_user(user_id, source="test-check")
    assert check_actor is not None
    with bind_actor(check_actor):
        current = storage.get_reminder(reminder["id"])
    assert current is not None and current["status"] == "cancelled"
    storage.close()
