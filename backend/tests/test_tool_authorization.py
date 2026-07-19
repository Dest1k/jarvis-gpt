from __future__ import annotations

import asyncio

import pytest
from jarvis_gpt.agent import AgentContext, AgentRuntime
from jarvis_gpt.authorization import (
    LEGACY_OWNER_USER_ID,
    ActorContext,
    AuthorizationError,
    CapabilityDefinition,
    bind_actor,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import OperatorTurnAuthorization, ToolRegistry, ToolSpec


def _registry(monkeypatch, tmp_path) -> tuple[ToolRegistry, JarvisStorage]:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return ToolRegistry(settings, storage, LLMRouter(settings)), storage


def _actor(identity: dict[str, object], *, source: str = "test") -> ActorContext:
    return ActorContext(
        user_id=str(identity["user_id"]),
        preset_key=str(identity["preset_key"]),
        source=source,
        identity_id=str(identity["identity_id"]),
        policy_epoch=int(identity["policy_epoch"]),
    )


def test_non_owner_cannot_call_cross_tenant_storage_maintenance(
    monkeypatch, tmp_path
) -> None:
    _tools, storage = _registry(monkeypatch, tmp_path)
    identity = _tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="storage-guards",
        provider_subject_id="ordinary-user",
        bootstrap_preset="user",
    )

    with bind_actor(_actor(identity)):
        operations = (
            lambda: storage.claim_due_reminders(all_users=True),
            lambda: storage.list_pending_screen_watch_notifications(all_users=True),
            storage.recover_interrupted_approval_executions,
            lambda: storage.pending_approval_reconciliations(all_users=True),
            lambda: storage.record_audit(
                actor="user",
                action="forged",
                target_type="user",
                summary="forged audit",
                user_id=LEGACY_OWNER_USER_ID,
            ),
            storage.backup_database,
        )
        for operation in operations:
            with pytest.raises(AuthorizationError, match="requires owner system scope"):
                operation()

    storage.close()


def test_tool_spec_has_stable_non_overridable_security_id() -> None:
    spec = ToolSpec(
        name="test.inspect",
        description="inspect",
        category="test",
        input_schema={},
        handler=lambda _context, _arguments: ToolRunResponse(
            tool="test.inspect", ok=True, summary="ok"
        ),
    )
    assert spec.security_id == "tool.test.inspect"
    assert spec.info().security_id == "tool.test.inspect"

    with pytest.raises(ValueError, match="stable security_id"):
        ToolSpec(
            name="test.inspect",
            description="inspect",
            category="test",
            input_schema={},
            handler=spec.handler,
            security_id="tool.some_other_name",
        )


def test_tool_registry_rejects_incompatible_duplicate_name_without_replacement(
    monkeypatch, tmp_path
) -> None:
    tools, storage = _registry(monkeypatch, tmp_path)

    def original_handler(_context, _arguments):
        return ToolRunResponse(tool="test.duplicate", ok=True, summary="original")

    original = ToolSpec(
        name="test.duplicate",
        description="original declaration",
        category="test",
        input_schema={"type": "object", "additionalProperties": False},
        handler=original_handler,
    )
    tools.add(original)

    equivalent = ToolSpec(
        name="test.duplicate",
        description="original declaration",
        category="test",
        input_schema={"type": "object", "additionalProperties": False},
        handler=original_handler,
    )
    tools.add(equivalent)
    assert tools.get("test.duplicate") is original

    incompatible = ToolSpec(
        name="test.duplicate",
        description="same capability, more dangerous implementation",
        category="test",
        input_schema={"type": "object"},
        handler=lambda _context, _arguments: ToolRunResponse(
            tool="test.duplicate", ok=True, summary="replacement"
        ),
        danger_level="danger",
    )
    with pytest.raises(ValueError, match="Conflicting tool registration"):
        tools.add(incompatible)

    assert tools.get("test.duplicate") is original
    storage.close()


def test_tool_registry_rejects_duplicate_security_id_even_with_another_name(
    monkeypatch, tmp_path
) -> None:
    tools, storage = _registry(monkeypatch, tmp_path)
    original = ToolSpec(
        name="test.capability",
        description="original declaration",
        category="test",
        input_schema={},
        handler=lambda _context, _arguments: ToolRunResponse(
            tool="test.capability", ok=True, summary="original"
        ),
    )
    tools.add(original)

    collision = ToolSpec(
        name="test.other",
        description="collision",
        category="test",
        input_schema={},
        handler=lambda _context, _arguments: ToolRunResponse(
            tool="test.other", ok=True, summary="replacement"
        ),
    )
    # ToolSpec validates this invariant at construction time.  Mutate it here
    # solely to prove that the registry independently fails closed as well.
    object.__setattr__(collision, "security_id", original.security_id)

    with pytest.raises(ValueError, match="Conflicting tool registration"):
        tools.add(collision)

    assert tools.get("test.capability") is original
    assert tools.get("test.other") is None
    storage.close()


def test_permission_denial_precedes_hitl_and_does_not_consume_one_use_authority(
    monkeypatch, tmp_path
) -> None:
    tools, storage = _registry(monkeypatch, tmp_path)
    executed: list[tuple[str, str]] = []

    def handler(context, _arguments):
        assert context.authorization_decision is not None
        assert context.authorization_decision.allowed is True
        executed.append((context.actor.user_id, context.authorization_decision.security_id))
        return ToolRunResponse(tool="test.review", ok=True, summary="done")

    tools.add(
        ToolSpec(
            name="test.review",
            description="review action",
            category="test",
            input_schema={},
            handler=handler,
            danger_level="review",
        )
    )
    guest_identity = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="local",
        provider_subject_id="guest-1",
    )
    arguments = {"value": "exact"}
    turn_authorization = OperatorTurnAuthorization.bind(
        conversation_id="conv-test",
        user_message_id="msg-test",
        tool="test.review",
        arguments=arguments,
    )

    with bind_actor(_actor(guest_identity)):
        assert "test.review" not in {item.name for item in tools.list()}
        denied = asyncio.run(
            tools.run(
                "test.review",
                arguments,
                conversation_id="conv-test",
                user_message_id="msg-test",
                authorization=turn_authorization,
                allow_danger=True,
            )
        )

    assert denied.ok is False
    assert denied.data["policy_decision"]["code"] == "security_id_denied"
    assert executed == []

    # The denied attempt did not burn the exact one-use HITL capability.  The
    # legacy owner still needs and successfully consumes it at the independent gate.
    allowed = asyncio.run(
        tools.run(
            "test.review",
            arguments,
            conversation_id="conv-test",
            user_message_id="msg-test",
            authorization=turn_authorization,
        )
    )
    assert allowed.ok is True
    assert executed and executed[0][1] == "tool.test.review"
    storage.close()


def test_user_sees_only_explicitly_allowlisted_tenant_tools_and_never_gets_owner_autonomy(
    monkeypatch, tmp_path
) -> None:
    tools, storage = _registry(monkeypatch, tmp_path)
    tools.add(
        ToolSpec(
            name="test.safe",
            description="safe action",
            category="test",
            input_schema={},
            handler=lambda _context, _arguments: ToolRunResponse(
                tool="test.safe", ok=True, summary="done"
            ),
        )
    )
    tools.add(
        ToolSpec(
            name="test.danger",
            description="danger action",
            category="test",
            input_schema={},
            handler=lambda _context, _arguments: ToolRunResponse(
                tool="test.danger", ok=True, summary="done"
            ),
            danger_level="danger",
        )
    )
    identity = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="local",
        provider_subject_id="user-1",
        bootstrap_preset="user",
    )
    agent = AgentRuntime(
        settings=tools.settings,
        storage=storage,
        llm=tools.llm,
        tools=tools,
    )

    with bind_actor(_actor(identity)):
        visible = {item.name for item in tools.list()}
        # A plugin labelling itself "safe" is not enough to receive every tenant's data.
        assert "test.safe" not in visible
        assert "test.danger" not in visible
        assert "memory.search" in visible
        assert agent._owner_autonomy_active() is False
        context = AgentContext(conversation_id="conv-user", memory_hits=[], file_hits=[])
        assert context.actor.user_id == identity["user_id"]
        assert "test.danger" not in {item.name for item in agent._tools_for_context(context)}

    storage.close()


def test_catalog_sync_reconciles_tightened_builtin_grants_and_revokes_sessions(
    monkeypatch, tmp_path
) -> None:
    tools, storage = _registry(monkeypatch, tmp_path)
    service = tools.permissions
    security_id = "test.catalog.manage"
    initial = CapabilityDefinition(
        security_id,
        "test capability",
        "test",
        risk_level=0,
        source="test_catalog",
        default_presets=("admin",),
    )
    service.sync_capabilities((initial,), catalog_key="test.catalog.v1")
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="catalog",
        provider_subject_id="admin",
        bootstrap_preset="admin",
    )
    session = service.create_user_session(
        user_id=str(identity["user_id"]),
        identity_id=str(identity["identity_id"]),
        auth_method="test",
    )
    assert service.authorize(str(identity["user_id"]), security_id, record=False).allowed
    assert service.authenticate_session(str(session["session_token"])) is not None

    tightened = CapabilityDefinition(
        security_id,
        "test capability",
        "test",
        risk_level=4,
        source="test_catalog",
    )
    result = service.sync_capabilities((tightened,), catalog_key="test.catalog.v1")

    assert result["reconciled_presets"] >= 1
    assert not service.authorize(
        str(identity["user_id"]), security_id, record=False
    ).allowed
    assert service.authenticate_session(str(session["session_token"])) is None
    with storage.locked_connection() as conn:
        audit = conn.execute(
            """
            SELECT action FROM security_audit_log
            WHERE target_id = 'test.catalog.v1'
            ORDER BY rowid DESC LIMIT 1
            """
        ).fetchone()
    assert audit["action"] == "catalog.builtin_policy.reconcile"
    storage.close()


def test_tool_discovery_cache_drops_expired_direct_grant(monkeypatch, tmp_path) -> None:
    tools, storage = _registry(monkeypatch, tmp_path)
    tools.add(
        ToolSpec(
            name="test.temporary",
            description="temporary action",
            category="test",
            input_schema={},
            handler=lambda _context, _arguments: ToolRunResponse(
                tool="test.temporary", ok=True, summary="done"
            ),
        )
    )
    identity = tools.permissions.upsert_external_identity(
        provider="test",
        realm_id="cache",
        provider_subject_id="temporary-user",
    )
    tools.permissions.set_user_permission(
        user_id=str(identity["user_id"]),
        security_id="tool.test.temporary",
        effect="grant",
        can_delegate=False,
        granted_by=str(identity["user_id"]),
        reason="temporary test",
        valid_until="2099-01-01T00:00:00+00:00",
    )
    actor = tools.permissions.actor_for_user(str(identity["user_id"]), source="test")
    assert actor is not None
    clock = 1_000.0
    monkeypatch.setattr("jarvis_gpt.tools.time.time", lambda: clock)
    with bind_actor(actor):
        assert "test.temporary" in {item.name for item in tools.list()}
        with storage.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE user_permissions SET valid_until = '2000-01-01T00:00:00+00:00'
                WHERE user_id = ? AND revoked_at IS NULL
                """,
                (identity["user_id"],),
            )
        assert "test.temporary" in {item.name for item in tools.list()}
        clock += 6
        assert "test.temporary" not in {item.name for item in tools.list()}
    storage.close()
