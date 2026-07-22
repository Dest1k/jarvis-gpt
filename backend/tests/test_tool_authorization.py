from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from jarvis_gpt.agent import AgentContext, AgentRuntime
from jarvis_gpt.authorization import (
    LEGACY_OWNER_USER_ID,
    ActorContext,
    AuthorizationError,
    AuthorizationService,
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


def test_catalog_sync_required_preset_change_revokes_direct_grant_sessions(
    monkeypatch, tmp_path
) -> None:
    tools, storage = _registry(monkeypatch, tmp_path)
    service = tools.permissions
    security_id = "test.catalog.privileged"
    unrestricted = CapabilityDefinition(
        security_id,
        "catalog privilege",
        "test",
        source="test_required_catalog",
    )
    service.sync_capabilities((unrestricted,), catalog_key="test.required_catalog.v1")
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="required-catalog",
        provider_subject_id="direct-user",
        bootstrap_preset="user",
    )
    service.set_user_permission(
        user_id=str(identity["user_id"]),
        security_id=security_id,
        effect="grant",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="seed a direct grant before tightening the role floor",
    )
    session = service.create_user_session(
        user_id=str(identity["user_id"]),
        identity_id=str(identity["identity_id"]),
        auth_method="test",
    )
    before = service.get_user(str(identity["user_id"]))
    assert before is not None
    assert service.authorize(
        str(identity["user_id"]), security_id, record=False
    ).allowed

    restricted = CapabilityDefinition(
        security_id,
        "catalog privilege",
        "test",
        source="test_required_catalog",
        required_presets=("owner", "admin"),
    )
    result = service.sync_capabilities(
        (restricted,), catalog_key="test.required_catalog.v1"
    )

    assert result["reconciled_presets"] == 0
    assert result["required_presets_changed"] == 1
    denied = service.authorize(
        str(identity["user_id"]), security_id, record=False
    )
    assert not denied.allowed
    assert denied.reason_code == "preset_not_eligible"
    assert service.authenticate_session(str(session["session_token"])) is None
    after = service.get_user(str(identity["user_id"]))
    assert after is not None
    assert int(after["policy_epoch"]) == int(before["policy_epoch"]) + 1
    storage.close()


def test_required_presets_column_is_added_to_an_existing_catalog(
    monkeypatch, tmp_path
) -> None:
    tools, storage = _registry(monkeypatch, tmp_path)
    database_path = storage.database_path
    with storage.transaction(immediate=True) as conn:
        conn.execute("ALTER TABLE security_ids DROP COLUMN required_presets_json")
        conn.execute(
            "DELETE FROM iam_migrations WHERE key = 'capability_required_presets_v1'"
        )
    storage.close()

    migrated_storage = JarvisStorage(database_path)
    migrated_storage.initialize()
    service = AuthorizationService(migrated_storage)
    capability = CapabilityDefinition(
        "test.migrated.privilege",
        "migrated privilege",
        "test",
        source="test_required_migration",
        default_presets=("admin",),
        required_presets=("owner", "admin"),
    )
    service.sync_capabilities((capability,), catalog_key="test.required_migration.v1")

    with migrated_storage.locked_connection() as conn:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(security_ids)")
        }
        row = conn.execute(
            "SELECT required_presets_json FROM security_ids WHERE security_id = ?",
            (capability.security_id,),
        ).fetchone()
        marker = conn.execute(
            "SELECT 1 FROM iam_migrations "
            "WHERE key = 'capability_required_presets_v1'"
        ).fetchone()
    assert "required_presets_json" in columns
    assert row["required_presets_json"] == '["admin","owner"]'
    assert marker is not None
    migrated_storage.close()


def test_required_presets_block_direct_and_custom_grants_and_follow_demotion(
    monkeypatch, tmp_path
) -> None:
    tools, storage = _registry(monkeypatch, tmp_path)
    service = tools.permissions
    security_id = "test.privileged.read"
    capability = CapabilityDefinition(
        security_id,
        "privileged read",
        "test",
        source="test_required_presets",
        default_presets=("admin",),
        required_presets=("owner", "admin"),
    )
    service.sync_capabilities((capability,), catalog_key="test.required_presets.v1")

    catalog_entry = next(
        item for item in service.list_security_ids() if item["security_id"] == security_id
    )
    assert catalog_entry["required_presets"] == ["admin", "owner"]

    direct_identity = service.upsert_external_identity(
        provider="test",
        realm_id="required-presets",
        provider_subject_id="direct-user",
        bootstrap_preset="user",
    )
    direct_result = service.set_user_permission(
        user_id=str(direct_identity["user_id"]),
        security_id=security_id,
        effect="grant",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="prove direct grants cannot cross the role floor",
    )
    assert direct_result["effect"] == "deny"
    assert direct_result["reason_code"] == "preset_not_eligible"
    assert direct_result["source"] == "capability_policy"

    custom_identity = service.upsert_external_identity(
        provider="test",
        realm_id="required-presets",
        provider_subject_id="custom-user",
        bootstrap_preset="user",
    )
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with storage.transaction(immediate=True) as conn:
        capability_row = conn.execute(
            "SELECT id FROM security_ids WHERE security_id = ?",
            (security_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO permission_presets(
                id, preset_key, display_name, kind, active_version_id,
                created_by, created_at, updated_at
            ) VALUES (
                'preset_test_privileged_custom', 'test_privileged_custom',
                'Test privileged custom', 'custom', 'presetv_test_privileged_custom_1',
                ?, ?, ?
            )
            """,
            (LEGACY_OWNER_USER_ID, now, now),
        )
        conn.execute(
            """
            INSERT INTO permission_preset_versions(
                id, preset_id, version, state, created_by, created_at,
                published_at, change_reason
            ) VALUES (
                'presetv_test_privileged_custom_1', 'preset_test_privileged_custom',
                1, 'published', ?, ?, ?, 'required preset regression'
            )
            """,
            (LEGACY_OWNER_USER_ID, now, now),
        )
        conn.execute(
            """
            INSERT INTO preset_security_ids(
                preset_version_id, security_id_id, effect, can_delegate
            ) VALUES ('presetv_test_privileged_custom_1', ?, 'grant', 0)
            """,
            (capability_row["id"],),
        )
    service.assign_preset(
        user_id=str(custom_identity["user_id"]),
        preset_key="test_privileged_custom",
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="prove custom presets cannot cross the role floor",
    )
    custom_decision = service.authorize(
        str(custom_identity["user_id"]), security_id, record=False
    )
    assert not custom_decision.allowed
    assert custom_decision.reason_code == "preset_not_eligible"

    admin_identity = service.upsert_external_identity(
        provider="test",
        realm_id="required-presets",
        provider_subject_id="admin-user",
        bootstrap_preset="admin",
    )
    session = service.create_user_session(
        user_id=str(admin_identity["user_id"]),
        identity_id=str(admin_identity["identity_id"]),
        auth_method="test",
    )
    assert service.authorize(
        str(admin_identity["user_id"]), security_id, record=False
    ).allowed

    service.assign_preset(
        user_id=str(admin_identity["user_id"]),
        preset_key="user",
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="demotion must revoke privileged access immediately",
    )
    demoted = service.authorize(
        str(admin_identity["user_id"]), security_id, record=False
    )
    assert not demoted.allowed
    assert demoted.reason_code == "preset_not_eligible"
    assert service.authenticate_session(str(session["session_token"])) is None

    storage.close()


def test_admin_read_only_technical_tools_are_visible_revocable_and_metadata_only(
    monkeypatch, tmp_path
) -> None:
    tools, storage = _registry(monkeypatch, tmp_path)
    service = tools.permissions
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="admin-technical-tools",
        provider_subject_id="admin-reader",
        bootstrap_preset="admin",
    )
    user_id = str(identity["user_id"])
    admin_actor = _actor(identity)
    technical_tools = {
        "runtime.status",
        "environment.profile",
        "system.inspect",
    }
    admin_native_reads = {
        "native.capabilities.read",
        "native.process.top.read",
        "native.window.list.read",
        "native.wmi.query",
        "native.hardware.gpu.read",
    }

    with bind_actor(admin_actor):
        listed = {item.name for item in tools.list()}
        assert technical_tools <= listed
        for tool_name in technical_tools:
            spec = tools.get(tool_name)
            assert spec is not None
            assert spec.default_presets == ("admin",)
            assert spec.required_presets == ("owner", "admin")
            assert spec.history_persistence == "metadata_only"
            assert service.authorize(user_id, spec.security_id, record=False).allowed
        for security_id in admin_native_reads:
            assert service.authorize(user_id, security_id, record=False).allowed
        for security_id in ("privacy.clipboard.read", "privacy.screen.capture"):
            privacy_denial = service.authorize(user_id, security_id, record=False)
            assert not privacy_denial.allowed
            assert privacy_denial.reason_code == "not_granted"

        sentinel = "ADMIN_TECHNICAL_PROFILE_SENTINEL_91827"
        storage.set_runtime_value("environment.host_profile", {"host": sentinel})
        profile = asyncio.run(tools.run("environment.profile", {}))
        runtime = asyncio.run(tools.run("runtime.status", {}))
        assert profile.ok is True
        assert sentinel in str(profile.data)
        assert runtime.ok is True

    service.assign_preset(
        user_id=user_id,
        preset_key="user",
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="technical access must be revoked without retaining snapshots",
    )
    demoted_actor = service.actor_for_user(user_id, source="demoted-technical-test")
    assert demoted_actor is not None
    with bind_actor(demoted_actor):
        assert technical_tools.isdisjoint({item.name for item in tools.list()})
        for tool_name in technical_tools:
            denial = service.authorize(
                user_id,
                f"tool.{tool_name}",
                record=False,
            )
            assert not denial.allowed
            assert denial.reason_code == "preset_not_eligible"
        retained = {
            "tool_runs": storage.list_tool_runs(limit=20),
            "learning": storage.list_learning_observations(limit=20),
            "audit": storage.list_audit(limit=20),
        }
    serialized = str(retained)
    assert sentinel not in serialized
    assert "settings" not in serialized
    assert "jarvis.tool-history.metadata-only.v1" in serialized
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
