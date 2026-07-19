from __future__ import annotations

import asyncio

from jarvis_gpt.authorization import (
    CORE_CAPABILITIES,
    LEGACY_OWNER_USER_ID,
    AuthorizationService,
    bind_actor,
    current_user_id,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.supervisor import RuntimeSupervisor


class _Executor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_due_jobs(self, *, limit: int):
        user_id = current_user_id()
        self.calls.append(user_id)
        return [
            {
                "job": {"id": f"job-{user_id}", "kind": "diagnostics"},
                "ok": True,
                "summary": "done",
            }
        ]


def test_background_scheduler_binds_tenant_and_rechecks_capability(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    permissions = AuthorizationService(storage)
    permissions.sync_capabilities(CORE_CAPABILITIES, catalog_key="core.v1")

    first_identity = permissions.upsert_external_identity(
        provider="test",
        realm_id="scheduler",
        provider_subject_id="first",
        bootstrap_preset="user",
    )
    second_identity = permissions.upsert_external_identity(
        provider="test",
        realm_id="scheduler",
        provider_subject_id="second",
        bootstrap_preset="user",
    )
    denied_identity, allowed_identity = sorted(
        (first_identity, second_identity), key=lambda item: str(item["user_id"])
    )
    allowed_user_id = str(allowed_identity["user_id"])
    permissions.set_user_permission(
        user_id=allowed_user_id,
        security_id="background.autonomy.execute",
        effect="grant",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="scheduler test",
    )

    for identity in (denied_identity, allowed_identity):
        actor = permissions.actor_for_user(str(identity["user_id"]), source="test")
        assert actor is not None
        with bind_actor(actor):
            storage.set_runtime_value(
                "operations.autonomy.jobs",
                [{"id": f"job-{actor.user_id}", "status": "enabled"}],
            )

    executor = _Executor()
    supervisor = RuntimeSupervisor(
        settings=settings,
        storage=storage,
        autonomy_executor=executor,
    )
    asyncio.run(supervisor._run_background_jobs())

    assert executor.calls == [allowed_user_id]
    with storage.locked_connection() as conn:
        decisions = conn.execute(
            """
            SELECT actor_user_id, effect
            FROM authorization_decisions
            WHERE security_id = 'background.autonomy.execute'
            ORDER BY ts, rowid
            """
        ).fetchall()
    assert {str(row["actor_user_id"]): str(row["effect"]) for row in decisions} == {
        str(denied_identity["user_id"]): "deny",
        allowed_user_id: "allow",
    }
    storage.close()
