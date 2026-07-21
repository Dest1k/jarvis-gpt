from __future__ import annotations

import json

import pytest
from fastapi.routing import APIRoute
from jarvis_gpt.api import (
    _HTTP_ROUTE_CAPABILITIES,
    HTTP_API_CAPABILITIES,
    _resolve_http_route_capability,
    _uses_separate_route_authorization,
    app,
)
from jarvis_gpt.authorization import LEGACY_OWNER_USER_ID
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
