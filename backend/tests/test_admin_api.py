from __future__ import annotations

from datetime import UTC, datetime

import pytest
from jarvis_gpt.api import app
from jarvis_gpt.authorization import (
    LEGACY_OWNER_USER_ID,
    AuthorizationError,
    current_actor,
)
from starlette.testclient import TestClient

BRIDGE_SECRET = "bridge-test-secret-with-at-least-32-chars"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    monkeypatch.setenv("JARVIS_TELEGRAM_BRIDGE_SECRET", BRIDGE_SECRET)
    monkeypatch.setenv("JARVIS_TELEGRAM_REALM_ID", "test-bot")
    # The restricted CI sandbox cannot create the Linux abstract UNIX socket used by
    # the production single-primary lease. Lease semantics have their own test module.
    monkeypatch.setattr("jarvis_gpt.api.PrimaryRuntimeLease.acquire", lambda _self: None)
    monkeypatch.setattr("jarvis_gpt.api.PrimaryRuntimeLease.release", lambda _self: None)
    with TestClient(app) as test_client:
        yield test_client


def _register_telegram_user(
    client: TestClient,
    *,
    update_id: int = 1,
    telegram_user_id: int = 424242,
) -> dict:
    response = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "update_id": update_id,
            "telegram_user": {
                "id": telegram_user_id,
                "is_bot": False,
                "username": (
                    "secure_user"
                    if telegram_user_id == 424242
                    else f"secure_user_{telegram_user_id}"
                ),
                "first_name": "Secure",
                "language_code": "en",
            },
            "chat": {"id": telegram_user_id, "type": "private"},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_telegram_sessions_isolate_memory_preferences_persona_and_files(client):
    first = _register_telegram_user(
        client, update_id=10_001, telegram_user_id=101_001
    )
    second = _register_telegram_user(
        client, update_id=10_002, telegram_user_id=202_002
    )
    for registered in (first, second):
        assigned = _approved_request(
            client,
            "PUT",
            f"/api/admin/users/{registered['user']['id']}/preset",
            json={"preset_key": "user", "reason": "tenant isolation test"},
        )
        assert assigned.status_code == 200, assigned.text

    # Preset changes revoke old sessions; authenticated Telegram updates issue fresh,
    # short-lived sessions with the new effective policy.
    first = _register_telegram_user(
        client, update_id=10_003, telegram_user_id=101_001
    )
    second = _register_telegram_user(
        client, update_id=10_004, telegram_user_id=202_002
    )
    first_headers = {"X-Jarvis-User-Session": first["session_token"]}
    second_headers = {"X-Jarvis-User-Session": second["session_token"]}

    chat = client.post(
        "/api/chat",
        headers=first_headers,
        json={"message": "alpha private conversation", "mode": "chat"},
    )
    assert chat.status_code == 200, chat.text
    conversation_id = chat.json()["conversation_id"]
    assert all(
        item["id"] != conversation_id
        for item in client.get("/api/conversations", headers=second_headers).json()
    )
    assert client.get(
        f"/api/conversations/{conversation_id}/messages", headers=second_headers
    ).status_code == 404
    foreign_chat = client.post(
        "/api/chat",
        headers=second_headers,
        json={
            "message": "attempt foreign conversation reuse",
            "conversation_id": conversation_id,
            "mode": "chat",
        },
    )
    assert foreign_chat.status_code == 404

    saved_memory = client.post(
        "/api/memory",
        headers=first_headers,
        json={"content": "alpha-tenant-secret", "namespace": "private"},
    )
    assert saved_memory.status_code == 200, saved_memory.text
    assert client.get(
        "/api/memory?q=alpha-tenant-secret", headers=second_headers
    ).json() == []

    updated_preferences = client.patch(
        "/api/preferences",
        headers=first_headers,
        json={"operator_name": "Tenant Alpha"},
    )
    assert updated_preferences.status_code == 200, updated_preferences.text
    assert client.get("/api/preferences", headers=second_headers).json()[
        "operator_name"
    ] != "Tenant Alpha"

    updated_persona = client.patch(
        "/api/persona",
        headers=first_headers,
        json={"notes": "alpha-only persona note"},
    )
    assert updated_persona.status_code == 200, updated_persona.text
    assert client.get("/api/persona", headers=second_headers).json().get("notes") != (
        "alpha-only persona note"
    )

    uploaded = client.post(
        "/api/files/upload",
        headers=first_headers,
        files={"file": ("alpha.txt", b"alpha-private-file", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    file_id = uploaded.json()["file"]["id"]
    assert client.get(f"/api/files/{file_id}", headers=second_headers).status_code == 404


def _approved_request(client: TestClient, method: str, path: str, **kwargs):
    pending = client.request(method, path, **kwargs)
    assert pending.status_code == 428, pending.text
    approval_id = pending.json()["detail"]["approval_id"]
    approved = client.patch(
        f"/api/approvals/{approval_id}",
        json={"status": "approved", "result": {"operator": "test"}},
    )
    assert approved.status_code == 200, approved.text
    headers = {**kwargs.pop("headers", {}), "X-Jarvis-Approval-Id": approval_id}
    return client.request(method, path, headers=headers, **kwargs)


def test_telegram_registration_creates_scoped_session_and_denies_admin(client):
    denied = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": "wrong-secret"},
        json={
            "update_id": 1,
            "telegram_user": {"id": 424242, "is_bot": False},
            "chat": {"id": 424242, "type": "private"},
        },
    )
    assert denied.status_code == 401

    registered = _register_telegram_user(client)
    assert registered["user"]["preset_key"] == "guest"
    session_headers = {"X-Jarvis-User-Session": registered["session_token"]}

    own_conversations = client.get("/api/conversations", headers=session_headers)
    assert own_conversations.status_code == 200
    admin_users = client.get("/api/admin/users", headers=session_headers)
    assert admin_users.status_code == 403
    assert admin_users.json()["detail"]["security_id"] == "admin.users.list"

    with app.state.storage.locked_connection() as conn:
        decision = conn.execute(
            """
            SELECT effect, reason_code FROM authorization_decisions
            WHERE actor_user_id = ? AND security_id = 'admin.users.list'
            ORDER BY ts DESC LIMIT 1
            """,
            (registered["user"]["id"],),
        ).fetchone()
    assert dict(decision) == {"effect": "deny", "reason_code": "not_granted"}


def test_invalid_session_never_falls_back_to_local_owner(client):
    response = client.get(
        "/api/admin/users",
        headers={"X-Jarvis-User-Session": "invalid-session"},
    )
    assert response.status_code == 401


def test_session_identity_must_belong_to_session_user(client):
    service = app.state.authorization
    first = service.upsert_external_identity(
        provider="test",
        realm_id="session-binding",
        provider_subject_id="first",
        bootstrap_preset="guest",
    )
    second = service.upsert_external_identity(
        provider="test",
        realm_id="session-binding",
        provider_subject_id="second",
        bootstrap_preset="guest",
    )

    with pytest.raises(AuthorizationError, match="does not belong"):
        service.create_user_session(
            user_id=str(first["user_id"]),
            identity_id=str(second["identity_id"]),
            auth_method="test",
        )


def test_owner_mutations_preserve_effective_recovery_actor(client):
    service = app.state.authorization
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="owner-recovery",
        provider_subject_id="second-owner",
        bootstrap_preset="user",
    )
    second_owner_id = str(identity["user_id"])
    service.assign_preset(
        user_id=second_owner_id,
        preset_key="owner",
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="recovery invariant test",
    )
    service.set_user_permission(
        user_id=second_owner_id,
        security_id="admin.users.preset.assign",
        effect="deny",
        can_delegate=False,
        granted_by=LEGACY_OWNER_USER_ID,
        reason="make second owner non-recoverable",
    )

    with pytest.raises(AuthorizationError, match="retain all recovery"):
        service.assign_preset(
            user_id=LEGACY_OWNER_USER_ID,
            preset_key="admin",
            assigned_by=LEGACY_OWNER_USER_ID,
            reason="must not strand recovery",
        )
    with pytest.raises(AuthorizationError, match="retain all recovery"):
        service.set_user_status(
            user_id=LEGACY_OWNER_USER_ID,
            status="suspended",
            reason="must not strand recovery",
        )

    assert service.get_user(LEGACY_OWNER_USER_ID)["preset_key"] == "owner"
    assert service.get_user(LEGACY_OWNER_USER_ID)["status"] == "active"


def test_api_token_actor_tracks_current_iam_role(client, monkeypatch):
    service = app.state.authorization
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="api-token-role",
        provider_subject_id="replacement-owner",
        bootstrap_preset="user",
    )
    replacement_owner_id = str(identity["user_id"])
    service.assign_preset(
        user_id=replacement_owner_id,
        preset_key="owner",
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="preserve owner recovery before demotion",
    )
    service.assign_preset(
        user_id=LEGACY_OWNER_USER_ID,
        preset_key="admin",
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="verify API token role refresh",
    )
    token = "api-token-role-refresh-32-characters-long"
    monkeypatch.setenv("JARVIS_API_TOKEN", token)
    observed_presets: list[str] = []
    original_list = app.state.storage.list_conversations

    def capture_actor(*args, **kwargs):
        observed_presets.append(current_actor().preset_key)
        return original_list(*args, **kwargs)

    monkeypatch.setattr(app.state.storage, "list_conversations", capture_actor)
    headers = {"Authorization": f"Bearer {token}"}
    listed = client.get("/api/conversations", headers=headers)

    assert listed.status_code == 200
    assert observed_presets == ["admin"]
    repromote = _approved_request(
        client,
        "PUT",
        f"/api/admin/users/{LEGACY_OWNER_USER_ID}/preset",
        headers=headers,
        json={"preset_key": "owner", "reason": "stale token must not repromote"},
    )
    assert repromote.status_code == 403
    assert service.get_user(LEGACY_OWNER_USER_ID)["preset_key"] == "admin"


def test_owner_can_create_assign_and_version_custom_preset(client):
    registered = _register_telegram_user(client, update_id=2)
    user_id = registered["user"]["id"]

    created = _approved_request(
        client,
        "POST",
        "/api/admin/presets",
        json={
            "key": "researcher",
            "name": "Researcher",
            "description": "Conversation and memory access",
            "security_ids": ["chat.use", "memory.read.own"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["security_ids"] == ["chat.use", "memory.read.own"]

    assigned = _approved_request(
        client,
        "PUT",
        f"/api/admin/users/{user_id}/preset",
        json={"preset_key": "researcher", "reason": "approved test role"},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["preset_key"] == "researcher"

    updated = _approved_request(
        client,
        "PUT",
        "/api/admin/presets/researcher",
        json={
            "name": "Researcher v2",
            "description": "Conversation only",
            "security_ids": ["chat.use"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert updated.json()["security_ids"] == ["chat.use"]

    permissions = client.get(f"/api/admin/users/{user_id}/permissions")
    assert permissions.status_code == 200
    by_id = {item["security_id"]: item for item in permissions.json()}
    assert by_id["chat.use"]["allowed"] is True
    assert by_id["memory.read.own"]["allowed"] is False


def test_status_change_revokes_existing_sessions(client):
    registered = _register_telegram_user(client, update_id=3)
    user_id = registered["user"]["id"]
    suspended = _approved_request(
        client,
        "PATCH",
        f"/api/admin/users/{user_id}/status",
        json={"status": "suspended", "reason": "security review"},
    )
    assert suspended.status_code == 200, suspended.text

    denied = client.get(
        "/api/conversations",
        headers={"X-Jarvis-User-Session": registered["session_token"]},
    )
    assert denied.status_code == 401


def test_admin_approval_is_invalidated_when_target_policy_state_changes(client):
    registered = _register_telegram_user(client, update_id=4)
    user_id = registered["user"]["id"]
    path = f"/api/admin/users/{user_id}/status"
    body = {"status": "suspended", "reason": "security review"}

    pending = client.patch(path, json=body)
    assert pending.status_code == 428
    approval_id = pending.json()["detail"]["approval_id"]
    approved = client.patch(
        f"/api/approvals/{approval_id}",
        json={"status": "approved", "result": {"operator": "test"}},
    )
    assert approved.status_code == 200

    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE users SET row_version = row_version + 1 WHERE id = ?",
            (user_id,),
        )

    stale = client.patch(
        path,
        headers={"X-Jarvis-Approval-Id": approval_id},
        json=body,
    )
    assert stale.status_code == 409
    with app.state.storage.locked_connection() as conn:
        status_value = conn.execute(
            "SELECT status FROM users WHERE id = ?", (user_id,)
        ).fetchone()["status"]
    assert status_value == "active"


def test_telegram_update_retry_is_idempotent_but_cannot_change_identity(client):
    _register_telegram_user(client, update_id=10)
    identical_replay = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "update_id": 10,
            "telegram_user": {
                "id": 424242,
                "is_bot": False,
                "username": "secure_user",
                "first_name": "Secure",
                "language_code": "en",
            },
            "chat": {"id": 424242, "type": "private"},
        },
    )
    assert identical_replay.status_code == 200
    assert identical_replay.json()["user"]["id"]

    replay = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "update_id": 10,
            "telegram_user": {"id": 999999, "is_bot": False},
            "chat": {"id": 999999, "type": "private"},
        },
    )
    assert replay.status_code == 409


def test_stale_telegram_attempt_cannot_finalize_after_lease_is_reclaimed(
    client, monkeypatch
):
    service = app.state.authorization
    original = service.upsert_external_identity
    newer_lease = "tglease_newer_attempt"

    def reclaim_before_identity(**kwargs):
        with service.storage.transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_updates
                SET lease_token = ?, attempt_count = attempt_count + 1, updated_at = ?
                WHERE realm_id = 'test-bot' AND update_id = 11
                  AND status = 'processing'
                """,
                (newer_lease, datetime.now(UTC).isoformat(timespec="seconds")),
            )
            assert cursor.rowcount == 1
        return original(**kwargs)

    monkeypatch.setattr(service, "upsert_external_identity", reclaim_before_identity)
    response = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "update_id": 11,
            "telegram_user": {"id": 424243, "is_bot": False},
            "chat": {"id": 424243, "type": "private"},
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Telegram update processing lease was superseded"
    with service.storage.locked_connection() as conn:
        ledger = conn.execute(
            """
            SELECT status, attempt_count, lease_token
            FROM telegram_updates WHERE realm_id = 'test-bot' AND update_id = 11
            """
        ).fetchone()
        active_sessions = conn.execute(
            """
            SELECT COUNT(*) AS count FROM user_sessions s
            JOIN external_identities ei ON ei.user_id = s.user_id
            WHERE ei.provider = 'telegram' AND ei.realm_id = 'test-bot'
              AND ei.provider_subject_id = '424243' AND s.revoked_at IS NULL
            """
        ).fetchone()["count"]
    assert dict(ledger) == {
        "status": "processing",
        "attempt_count": 2,
        "lease_token": newer_lease,
    }
    assert active_sessions == 0


def test_losing_telegram_attempt_cannot_overwrite_newer_completion(client, monkeypatch):
    service = app.state.authorization
    original = service.upsert_external_identity
    newer_lease = "tglease_completed_attempt"

    def complete_then_fail(**kwargs):
        identity = original(**kwargs)
        with service.storage.transaction(immediate=True) as conn:
            cursor = conn.execute(
                """
                UPDATE telegram_updates
                SET lease_token = ?, attempt_count = attempt_count + 1,
                    status = 'completed', user_id = ?, updated_at = ?
                WHERE realm_id = 'test-bot' AND update_id = 12
                  AND status = 'processing'
                """,
                (
                    newer_lease,
                    identity["user_id"],
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
            assert cursor.rowcount == 1
        raise RuntimeError("superseded attempt resumed")

    monkeypatch.setattr(service, "upsert_external_identity", complete_then_fail)
    response = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "update_id": 12,
            "telegram_user": {"id": 424244, "is_bot": False},
            "chat": {"id": 424244, "type": "private"},
        },
    )

    assert response.status_code == 409
    with service.storage.locked_connection() as conn:
        ledger = conn.execute(
            """
            SELECT status, attempt_count, lease_token, last_error
            FROM telegram_updates WHERE realm_id = 'test-bot' AND update_id = 12
            """
        ).fetchone()
    assert dict(ledger) == {
        "status": "completed",
        "attempt_count": 2,
        "lease_token": newer_lease,
        "last_error": None,
    }


def test_telegram_bridge_reuses_valid_scoped_session_without_row_growth(client):
    first = _register_telegram_user(client, update_id=20)
    second = client.post(
        "/api/integrations/telegram/session",
        headers={
            "X-Jarvis-Bridge-Secret": BRIDGE_SECRET,
            "X-Jarvis-User-Session": first["session_token"],
        },
        json={
            "update_id": 21,
            "telegram_user": {
                "id": 424242,
                "is_bot": False,
                "username": "secure_user",
                "first_name": "Secure",
                "language_code": "en",
            },
            "chat": {"id": 424242, "type": "private"},
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["session_token"] == first["session_token"]
    assert second.json()["session_id"] == first["session_id"]

    with app.state.storage.locked_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM user_sessions WHERE user_id = ?",
            (first["user"]["id"],),
        ).fetchone()["count"]
    assert count == 1


def test_telegram_ingress_and_scoped_api_are_rate_limited(client, monkeypatch):
    monkeypatch.setenv("JARVIS_TELEGRAM_USER_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("JARVIS_TELEGRAM_GLOBAL_RATE_LIMIT_PER_MINUTE", "100")
    first = _register_telegram_user(client, update_id=100)
    second = _register_telegram_user(client, update_id=101)
    assert second["user"]["id"] == first["user"]["id"]

    limited = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "update_id": 102,
            "telegram_user": {"id": 424242, "is_bot": False},
            "chat": {"id": 424242, "type": "private"},
        },
    )
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1

    monkeypatch.setenv("JARVIS_API_USER_RATE_LIMIT_PER_MINUTE", "1")
    headers = {"X-Jarvis-User-Session": first["session_token"]}
    assert client.get("/api/conversations", headers=headers).status_code == 200
    api_limited = client.get("/api/conversations", headers=headers)
    assert api_limited.status_code == 429
    assert int(api_limited.headers["retry-after"]) >= 1


def test_admin_user_catalog_is_server_paginated_and_never_duplicates_users(client):
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with app.state.storage.transaction(immediate=True) as conn:
        conn.executemany(
            """
            INSERT INTO users(
                id, status, display_name, locale, policy_epoch, created_at, updated_at,
                first_seen_at, last_seen_at
            ) VALUES (?, 'active', ?, '', 1, ?, ?, ?, ?)
            """,
            [
                (f"bulk_{index:04d}", f"Bulk {index:04d}", now, now, now, now)
                for index in range(505)
            ],
        )
        conn.executemany(
            """
            INSERT INTO user_preset_assignments(
                id, user_id, preset_id, assigned_by, assigned_at, reason
            ) VALUES (?, ?, 'preset_guest', NULL, ?, 'bulk test')
            """,
            [(f"bulk_assignment_{index:04d}", f"bulk_{index:04d}", now) for index in range(505)],
        )
        conn.executemany(
            """
            INSERT INTO external_identities(
                id, user_id, provider, realm_id, provider_subject_id,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, 'telegram', 'bulk', ?, ?, ?)
            """,
            [
                (
                    f"bulk_identity_{index:04d}",
                    f"bulk_{index:04d}",
                    str(900_000 + index),
                    now,
                    now,
                )
                for index in range(505)
            ],
        )
        conn.execute(
            """
            INSERT INTO external_identities(
                id, user_id, provider, realm_id, provider_subject_id,
                first_seen_at, last_seen_at
            ) VALUES ('bulk_identity_extra', 'bulk_0450', 'test', 'bulk', 'extra', ?, ?)
            """,
            (now, now),
        )

    response = client.get(
        "/api/admin/users?limit=200&offset=400&search=bulk_"
    )
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 505
    assert page["limit"] == 200
    assert page["offset"] == 400
    assert len(page["users"]) == 105
    assert len({item["id"] for item in page["users"]}) == 105
    extra = next(item for item in page["users"] if item["id"] == "bulk_0450")
    assert len(extra["identities"]) == 2
