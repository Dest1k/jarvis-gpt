from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.routing import APIRoute
from jarvis_gpt.api import (
    _HTTP_ADMIN_READ_ENDPOINTS,
    _HTTP_ROUTE_CAPABILITIES,
    HTTP_API_CAPABILITIES,
    INTERRUPTED_STREAM_KEY_PREFIX,
    _persist_interrupted_stream,
    _resolve_http_route_capability,
    _route_security_id,
    _uses_separate_route_authorization,
    app,
)
from jarvis_gpt.authorization import LEGACY_OWNER_USER_ID, bind_actor
from jarvis_gpt.models import ChatEvent, ChatResponse
from starlette.requests import Request
from starlette.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    monkeypatch.setattr("jarvis_gpt.api.PrimaryRuntimeLease.acquire", lambda _self: None)
    monkeypatch.setattr("jarvis_gpt.api.PrimaryRuntimeLease.release", lambda _self: None)
    with TestClient(app) as test_client:
        yield test_client


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "app": app,
        }
    )


def _install_custom_preset(
    *,
    preset_key: str,
    security_ids: tuple[str, ...],
) -> None:
    service = app.state.authorization
    now = datetime.now(UTC).isoformat(timespec="seconds")
    preset_id = f"preset_{preset_key}"
    version_id = f"presetv_{preset_key}_1"
    with service.storage.transaction(immediate=True) as conn:
        capability_rows = conn.execute(
            "SELECT id, security_id FROM security_ids WHERE security_id IN ("
            + ",".join("?" for _ in security_ids)
            + ")",
            security_ids,
        ).fetchall()
        capabilities = {str(row["security_id"]): str(row["id"]) for row in capability_rows}
        assert set(capabilities) == set(security_ids)
        conn.execute(
            """
            INSERT INTO permission_presets(
                id, preset_key, display_name, kind, active_version_id,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, 'custom', ?, ?, ?, ?)
            """,
            (
                preset_id,
                preset_key,
                "HTTP technical custom",
                version_id,
                LEGACY_OWNER_USER_ID,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO permission_preset_versions(
                id, preset_id, version, state, created_by, created_at,
                published_at, change_reason
            ) VALUES (?, ?, 1, 'published', ?, ?, ?, ?)
            """,
            (
                version_id,
                preset_id,
                LEGACY_OWNER_USER_ID,
                now,
                now,
                "hard-floor bypass regression",
            ),
        )
        conn.executemany(
            """
            INSERT INTO preset_security_ids(
                preset_version_id, security_id_id, effect, can_delegate
            ) VALUES (?, ?, 'grant', 0)
            """,
            [(version_id, capabilities[security_id]) for security_id in security_ids],
        )


def test_every_general_http_api_route_has_one_unique_security_id():
    expected = 0
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith("/api/") or _uses_separate_route_authorization(route.path):
            continue
        expected += len(set(route.methods or ()) - {"HEAD", "OPTIONS"})

    security_ids = [item.security_id for item in HTTP_API_CAPABILITIES]
    assert len(HTTP_API_CAPABILITIES) == expected
    assert len(_HTTP_ROUTE_CAPABILITIES) == expected
    assert len(set(security_ids)) == expected
    assert all(item.source == "http_api" for item in HTTP_API_CAPABILITIES)
    assert "http.get.api.preferences" in security_ids
    assert "http.patch.api.preferences" in security_ids
    assert "http.post.api.chat" in security_ids


def test_all_admin_technical_read_routes_have_owner_admin_hard_floor():
    definitions = {item.security_id: item for item in HTTP_API_CAPABILITIES}
    covered_names: set[str] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute) or route.name not in _HTTP_ADMIN_READ_ENDPOINTS:
            continue
        covered_names.add(route.name)
        for method in set(route.methods or ()) - {"HEAD", "OPTIONS"}:
            capability = definitions[_route_security_id(method, route.path)]
            assert capability.default_presets == ("admin",)
            assert capability.required_presets == ("owner", "admin")

    assert covered_names == set(_HTTP_ADMIN_READ_ENDPOINTS)


def test_parameterized_route_matching_is_method_aware_and_does_not_shadow_static_paths():
    static = _resolve_http_route_capability(_request("GET", "/api/files/search"))
    parameterized = _resolve_http_route_capability(
        _request("GET", "/api/files/file-123/download")
    )

    assert static is not None
    assert static.security_id == "http.get.api.files.search"
    assert parameterized is not None
    assert parameterized.security_id == "http.get.api.files.by_file_id.download"
    assert _resolve_http_route_capability(_request("PATCH", "/api/files/search")) is None
    assert _resolve_http_route_capability(_request("GET", "/api/not-registered")) is None


def test_builtin_route_grants_are_least_privilege(client: TestClient):
    service = app.state.authorization
    actors: dict[str, str] = {}
    for index, preset in enumerate(("guest", "user", "moderator", "admin"), start=1):
        identity = service.upsert_external_identity(
            provider="test",
            realm_id="http-pep",
            provider_subject_id=index,
            bootstrap_preset=preset,
        )
        actors[preset] = str(identity["user_id"])

    def allowed(preset: str, security_id: str) -> bool:
        return service.authorize(actors[preset], security_id, record=False).allowed

    assert allowed("guest", "http.post.api.chat") is True
    assert allowed("guest", "http.get.api.conversations") is True
    assert allowed("guest", "http.get.api.voice.status") is True
    assert allowed("guest", "http.post.api.voice.speak") is True
    assert allowed("guest", "http.get.api.preferences") is True
    assert allowed("guest", "http.patch.api.preferences") is True
    assert allowed("guest", "preferences.write.own") is True
    assert allowed("guest", "http.get.api.memory") is False
    assert allowed("user", "http.get.api.memory") is True
    assert allowed("moderator", "http.post.api.files.upload") is True
    assert allowed("admin", "http.get.api.status") is True
    assert allowed("admin", "http.post.api.runtime.backup") is False
    assert allowed("admin", "http.post.api.diagnostics") is False
    assert service.authorize(
        LEGACY_OWNER_USER_ID,
        "http.post.api.runtime.backup",
        record=False,
    ).allowed


def test_admin_technical_read_floor_blocks_direct_and_custom_preset_grants(
    client: TestClient,
):
    service = app.state.authorization
    protected_routes = {
        "http.get.api.agent.trace.by_conversation_id": "/api/agent/trace/conv_missing",
        "http.get.api.agent.trace.message.by_message_id": (
            "/api/agent/trace/message/msg_missing"
        ),
        "http.get.api.status": "/api/status",
        "http.get.api.models": "/api/models",
        "http.get.api.environment.profile": "/api/environment/profile",
    }

    direct_identity = service.upsert_external_identity(
        provider="test",
        realm_id="http-admin-read-floor",
        provider_subject_id="direct-user",
        bootstrap_preset="user",
    )
    direct_user_id = str(direct_identity["user_id"])
    for security_id in protected_routes:
        result = service.set_user_permission(
            user_id=direct_user_id,
            security_id=security_id,
            effect="grant",
            can_delegate=False,
            granted_by=LEGACY_OWNER_USER_ID,
            reason="direct grant must not cross the technical-read role floor",
        )
        assert result["effect"] == "deny"
        assert result["reason_code"] == "preset_not_eligible"
    direct_session = service.create_user_session(
        user_id=direct_user_id,
        identity_id=str(direct_identity["identity_id"]),
        auth_method="test",
    )
    direct_headers = {"X-Jarvis-User-Session": str(direct_session["session_token"])}
    for security_id, path in protected_routes.items():
        denied = client.get(path, headers=direct_headers)
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"]["security_id"] == security_id
        assert denied.json()["detail"]["reason"] == "preset_not_eligible"

    custom_key = "http_technical_custom"
    _install_custom_preset(
        preset_key=custom_key,
        security_ids=tuple(protected_routes),
    )
    custom_identity = service.upsert_external_identity(
        provider="test",
        realm_id="http-admin-read-floor",
        provider_subject_id="custom-user",
        bootstrap_preset="user",
    )
    custom_user_id = str(custom_identity["user_id"])
    service.assign_preset(
        user_id=custom_user_id,
        preset_key=custom_key,
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="custom grants must not turn into a privileged built-in role",
    )
    custom_session = service.create_user_session(
        user_id=custom_user_id,
        identity_id=str(custom_identity["identity_id"]),
        auth_method="test",
    )
    custom_headers = {"X-Jarvis-User-Session": str(custom_session["session_token"])}
    for security_id, path in protected_routes.items():
        denied = client.get(path, headers=custom_headers)
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"]["security_id"] == security_id
        assert denied.json()["detail"]["reason"] == "preset_not_eligible"


def test_admin_technical_read_access_is_revoked_on_demotion(client: TestClient):
    service = app.state.authorization
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="http-admin-read-floor",
        provider_subject_id="demoted-admin",
        bootstrap_preset="admin",
    )
    user_id = str(identity["user_id"])
    admin_session = service.create_user_session(
        user_id=user_id,
        identity_id=str(identity["identity_id"]),
        auth_method="test",
    )
    admin_headers = {"X-Jarvis-User-Session": str(admin_session["session_token"])}
    assert client.get("/api/status", headers=admin_headers).status_code == 200

    service.assign_preset(
        user_id=user_id,
        preset_key="user",
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="technical read access must end with admin role",
    )
    assert client.get("/api/status", headers=admin_headers).status_code == 401

    demoted_session = service.create_user_session(
        user_id=user_id,
        identity_id=str(identity["identity_id"]),
        auth_method="test",
    )
    denied = client.get(
        "/api/status",
        headers={"X-Jarvis-User-Session": str(demoted_session["session_token"])},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["security_id"] == "http.get.api.status"
    assert denied.json()["detail"]["reason"] == "preset_not_eligible"


def test_nonprivileged_live_and_history_responses_strip_runtime_metadata(
    client: TestClient,
    monkeypatch,
):
    service = app.state.authorization
    storage = app.state.storage
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="tenant-metadata-redaction",
        provider_subject_id="ordinary-user",
        bootstrap_preset="user",
    )
    user_id = str(identity["user_id"])
    session = service.create_user_session(
        user_id=user_id,
        identity_id=str(identity["identity_id"]),
        auth_method="test",
    )
    headers = {"X-Jarvis-User-Session": str(session["session_token"])}
    actor = service.actor_for_user(user_id, source="metadata-redaction-test")
    assert actor is not None
    settings = app.state.settings
    technical_events = [
        ChatEvent(
            type="tool_call",
            title="LLM router",
            content=f"{settings.llm_model} via {settings.llm_base_url}",
            payload={
                "model": settings.llm_model,
                "base_url": settings.llm_base_url,
                "profile": settings.profile.name,
            },
        ),
        ChatEvent(
            type="task_kernel",
            title="Task kernel",
            content="internal planning metadata",
            payload={
                "model": settings.llm_model,
                "llm_base_url": settings.llm_base_url,
                "security_id": "internal.capability",
            },
        ),
    ]

    async def fake_chat(*_args, **_kwargs):
        return ChatResponse(
            conversation_id="conv_live_metadata",
            message_id="msg_live_metadata",
            answer="sanitized answer",
            events=technical_events,
        )

    monkeypatch.setattr(app.state.agent, "chat", fake_chat)
    live = client.post(
        "/api/chat",
        headers=headers,
        json={"message": "hello"},
    )
    assert live.status_code == 200, live.text
    live_payload = live.json()
    assert all(event["content"] is None for event in live_payload["events"])
    assert all(event["payload"] == {} for event in live_payload["events"])

    with bind_actor(actor):
        conversation_id = storage.create_conversation("Technical metadata history")
        storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content="ordinary answer",
            metadata={
                "model": settings.llm_model,
                "base_url": settings.llm_base_url,
                "task_kernel": {
                    "profile": settings.profile.name,
                    "security_id": "internal.capability",
                },
                "events": [event.model_dump(mode="python") for event in technical_events],
            },
        )

    history = client.get(
        f"/api/conversations/{conversation_id}/messages",
        headers=headers,
    )
    assert history.status_code == 200, history.text
    metadata = history.json()[0]["metadata"]
    serialized = json.dumps(metadata, ensure_ascii=False)
    assert "task_kernel" not in metadata
    for forbidden_key in (
        "base_url",
        "llm_base_url",
        "model",
        "profile",
        "security_id",
    ):
        assert f'"{forbidden_key}"' not in serialized
    assert settings.llm_base_url not in serialized
    assert settings.llm_model not in serialized


def test_privileged_interrupted_stream_plaintext_is_never_checkpointed(
    client: TestClient,
):
    service = app.state.authorization
    storage = app.state.storage
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="interrupted-stream-confidentiality",
        provider_subject_id="admin",
        bootstrap_preset="admin",
    )
    actor = service.actor_for_user(
        str(identity["user_id"]),
        source="interrupted-stream-test",
    )
    assert actor is not None
    sentinel = "PRIVILEGED_INTERRUPTED_STREAM_SENTINEL"

    with bind_actor(actor):
        conversation_id = storage.create_conversation("Interrupted privileged answer")
        ordinary = _persist_interrupted_stream(
            storage,
            conversation_id=conversation_id,
            partial=["ordinary partial"],
            events=[],
            request_id="ordinary-request",
        )
        assert ordinary is not None
        assert ordinary["required_presets"] == []
        assert _persist_interrupted_stream(
            storage,
            conversation_id=conversation_id,
            partial=[sentinel],
            events=[{"type": "thought", "content": sentinel}],
            request_id="privileged-request",
            privileged_derived=True,
        ) is None

    assert storage.get_runtime_value(
        f"{INTERRUPTED_STREAM_KEY_PREFIX}{conversation_id}",
        None,
    ) is None
    runtime_state = storage.list_runtime_values(prefix=INTERRUPTED_STREAM_KEY_PREFIX)
    assert sentinel not in json.dumps(runtime_state, ensure_ascii=False)


def test_demoted_admin_cannot_recover_cross_user_material_from_personal_history(
    client: TestClient,
):
    service = app.state.authorization
    storage = app.state.storage
    sentinel = "CROSS_TENANT_SENTINEL_DO_NOT_PERSIST_74291"
    for tool_name in (
        "materials.search",
        "materials.read",
        "materials.summarize",
    ):
        spec = app.state.agent.tools.get(tool_name)
        assert spec is not None
        assert spec.history_persistence == "metadata_only"
    writer = service.upsert_external_identity(
        provider="test",
        realm_id="material-history-redaction",
        provider_subject_id="writer",
        bootstrap_preset="user",
    )
    writer_user_id = str(writer["user_id"])
    writer_actor = service.actor_for_user(writer_user_id, source="history-redaction-test")
    assert writer_actor is not None
    with bind_actor(writer_actor):
        conversation_id = storage.create_conversation("Private writer material")
        message_id = storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=f"Private material: {sentinel}",
        )

    admin = service.upsert_external_identity(
        provider="test",
        realm_id="material-history-redaction",
        provider_subject_id="admin-reader",
        bootstrap_preset="admin",
    )
    admin_user_id = str(admin["user_id"])
    admin_session = service.create_user_session(
        user_id=admin_user_id,
        identity_id=str(admin["identity_id"]),
        auth_method="test",
    )
    admin_headers = {
        "X-Jarvis-User-Session": str(admin_session["session_token"])
    }
    read = client.post(
        "/api/tools/materials.read/run",
        headers=admin_headers,
        json={
            "arguments": {
                "source_type": "message",
                "source_id": message_id,
                "user_id": writer_user_id,
            }
        },
    )
    assert read.status_code == 200, read.text
    assert read.json()["ok"] is True
    assert sentinel in read.json()["data"]["content"]

    service.assign_preset(
        user_id=admin_user_id,
        preset_key="user",
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="verify privileged results never become personal history",
    )
    assert client.get("/api/tool-runs", headers=admin_headers).status_code == 401
    user_session = service.create_user_session(
        user_id=admin_user_id,
        identity_id=str(admin["identity_id"]),
        auth_method="test",
    )
    user_headers = {
        "X-Jarvis-User-Session": str(user_session["session_token"])
    }
    tool_runs = client.get("/api/tool-runs", headers=user_headers)
    audit = client.get("/api/audit", headers=user_headers)
    assert tool_runs.status_code == 200, tool_runs.text
    assert audit.status_code == 200, audit.text
    exposed_history = json.dumps(
        {"tool_runs": tool_runs.json(), "audit": audit.json()},
        ensure_ascii=False,
    )
    assert sentinel not in exposed_history
    assert "jarvis.tool-history.metadata-only.v1" in exposed_history

    with storage.locked_connection() as conn:
        learning_rows = conn.execute(
            """
            SELECT content, summary, payload
            FROM learning_observations
            WHERE user_id = ? AND kind = 'tool.materials.read'
            """,
            (admin_user_id,),
        ).fetchall()
        material_audit = conn.execute(
            """
            SELECT result_count, details_json
            FROM material_access_audit
            WHERE requester_user_id = ? AND action = 'materials.read'
            """,
            (admin_user_id,),
        ).fetchone()
    assert learning_rows
    assert sentinel not in json.dumps(
        [dict(row) for row in learning_rows],
        ensure_ascii=False,
    )
    assert material_audit is not None
    assert material_audit["result_count"] == 1
    assert sentinel not in str(material_audit["details_json"])


def test_http_pep_denies_unknown_route_and_blocks_handler_before_execution(
    client: TestClient,
    monkeypatch,
):
    unknown = client.get("/api/not-registered")
    assert unknown.status_code == 403
    assert unknown.json()["detail"]["security_id"] == "http.unmapped"

    identity = app.state.authorization.upsert_external_identity(
        provider="test",
        realm_id="http-pep",
        provider_subject_id="guest-handler-check",
        bootstrap_preset="guest",
    )
    session = app.state.authorization.create_user_session(
        user_id=str(identity["user_id"]),
        identity_id=str(identity["identity_id"]),
        auth_method="test",
    )

    def must_not_run():
        raise AssertionError("route handler ran before authorization")

    monkeypatch.setattr(app.state.storage, "backup_database", must_not_run)
    denied = client.post(
        "/api/runtime/backup",
        headers={"X-Jarvis-User-Session": session["session_token"]},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["security_id"] == "http.post.api.runtime.backup"


def test_mission_task_update_requires_route_and_core_write_capabilities(
    client: TestClient,
    monkeypatch,
):
    service = app.state.authorization
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="http-pep",
        provider_subject_id="mission-task-core-deny",
        bootstrap_preset="user",
    )
    user_id = str(identity["user_id"])
    route_security_id = (
        "http.patch.api.missions.by_mission_id.tasks.by_task_id"
    )
    assert service.authorize(user_id, route_security_id, record=False).allowed is True
    service.set_user_permission(
        user_id=user_id,
        security_id="missions.write.own",
        effect="deny",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="conjunctive mission mutation test",
    )
    session = service.create_user_session(
        user_id=user_id,
        identity_id=str(identity["identity_id"]),
        auth_method="test",
    )

    def must_not_read_mission(_mission_id: str):
        raise AssertionError("mission was read before the core capability check")

    monkeypatch.setattr(app.state.storage, "get_mission", must_not_read_mission)
    denied = client.patch(
        "/api/missions/mis_12345678/tasks/task_12345678",
        json={"notes": "must not persist"},
        headers={"X-Jarvis-User-Session": session["session_token"]},
    )

    assert denied.status_code == 403
    assert denied.json()["detail"]["security_id"] == "missions.write.own"


def test_high_risk_http_route_requires_request_bound_one_use_approval(client: TestClient):
    pending = client.post("/api/runtime/backup")
    assert pending.status_code == 428
    detail = pending.json()["detail"]
    assert detail["security_id"] == "http.post.api.runtime.backup"

    approval_id = detail["approval_id"]
    approved = client.patch(
        f"/api/approvals/{approval_id}",
        json={"status": "approved", "result": {"operator": "test"}},
    )
    assert approved.status_code == 200

    executed = client.post(
        "/api/runtime/backup",
        headers={"X-Jarvis-Approval-Id": approval_id},
    )
    assert executed.status_code == 200, executed.text

    replay = client.post(
        "/api/runtime/backup",
        headers={"X-Jarvis-Approval-Id": approval_id},
    )
    assert replay.status_code == 409


def test_high_risk_http_route_rejects_expired_approval(client: TestClient):
    pending = client.post("/api/runtime/backup")
    approval_id = pending.json()["detail"]["approval_id"]
    approved = client.patch(
        f"/api/approvals/{approval_id}",
        json={"status": "approved", "result": {"operator": "test"}},
    )
    assert approved.status_code == 200

    with app.state.storage.transaction(immediate=True) as conn:
        row = conn.execute(
            "SELECT payload FROM approvals WHERE id = ?",
            (approval_id,),
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["expires_at"] = "2000-01-01T00:00:00+00:00"
        conn.execute(
            "UPDATE approvals SET payload = ? WHERE id = ?",
            (json.dumps(payload), approval_id),
        )

    expired = client.post(
        "/api/runtime/backup",
        headers={"X-Jarvis-Approval-Id": approval_id},
    )
    assert expired.status_code == 409
