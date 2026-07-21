from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from jarvis_gpt.api import app
from jarvis_gpt.authorization import (
    LEGACY_OWNER_USER_ID,
    AuthorizationError,
    bind_actor,
    current_actor,
)
from jarvis_gpt.telegram_bridge import TelegramConversationStore
from starlette.testclient import TestClient

BRIDGE_SECRET = "bridge-test-secret-with-at-least-32-chars"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_AUTONOMY_ENABLED", "0")
    monkeypatch.setenv("JARVIS_TELEGRAM_BRIDGE_SECRET", BRIDGE_SECRET)
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
    bot_id: int = 700001,
    realm_id: str | None = None,
) -> dict:
    realm_id = realm_id or f"telegram:{bot_id}"
    response = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "realm_id": realm_id,
            "bot_id": bot_id,
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


def _seed_legacy_telegram_history(
    *,
    chat_id: int,
    conversation_id: str,
    access_mode: str,
) -> TelegramConversationStore:
    store = TelegramConversationStore(
        app.state.storage.database_path,
        realm_id="telegram:700001",
    )
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO conversations(id, title, created_at, updated_at, user_id)
            VALUES (?, 'Legacy Telegram', ?, ?, ?)
            """,
            (conversation_id, now, now, LEGACY_OWNER_USER_ID),
        )
        conn.execute(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, metadata, created_at, user_id
            ) VALUES (?, ?, 'user', 'legacy private history', '{}', ?, ?)
            """,
            (
                f"msg_{chat_id}",
                conversation_id,
                now,
                LEGACY_OWNER_USER_ID,
            ),
        )
        conn.execute(
            """
            INSERT INTO reminders(
                id, created_at, updated_at, text, due_at, status,
                conversation_id, source_text, payload, user_id
            ) VALUES (?, ?, ?, 'legacy reminder', ?, 'pending', ?, '', '{}', ?)
            """,
            (
                f"rem_{chat_id}",
                now,
                now,
                now,
                conversation_id,
                LEGACY_OWNER_USER_ID,
            ),
        )
        conn.execute(
            """
            INSERT INTO learning_observations(
                id, ts, kind, conversation_id, content, payload, user_id
            ) VALUES (?, ?, 'conversation', ?, 'legacy observation', '{}', ?)
            """,
            (
                f"learn_{chat_id}",
                now,
                conversation_id,
                LEGACY_OWNER_USER_ID,
            ),
        )
        conn.execute(
            """
            INSERT INTO telegram_conversations(
                realm_id, chat_id, conversation_id, access_mode, user_id
            ) VALUES ('telegram:700001', ?, ?, ?, NULL)
            """,
            (chat_id, conversation_id, access_mode),
        )
    return store


@pytest.mark.parametrize("legacy_access_mode", ["owner", "guest"])
def test_first_telegram_registration_claims_history_without_trusting_legacy_mode(
    client,
    monkeypatch,
    legacy_access_mode,
):
    async def guest_completion(*_args, **_kwargs):
        return SimpleNamespace(ok=True, content="guest migration reply", error=None)

    monkeypatch.setattr(app.state.agent.llm, "complete", guest_completion)
    chat_id = 810001 if legacy_access_mode == "owner" else 810002
    conversation_id = f"legacy_{legacy_access_mode}_conversation"
    store = _seed_legacy_telegram_history(
        chat_id=chat_id,
        conversation_id=conversation_id,
        access_mode=legacy_access_mode,
    )

    registered = _register_telegram_user(
        client,
        update_id=810000 + chat_id,
        telegram_user_id=chat_id,
    )
    user_id = str(registered["user"]["id"])

    assert user_id != LEGACY_OWNER_USER_ID
    assert registered["user"]["preset_key"] == "guest"
    assert (
        store.get_or_create(chat_id, "guest", user_id=user_id)
        == conversation_id
    )
    history = client.get(
        f"/api/conversations/{conversation_id}/messages",
        headers={"X-Jarvis-User-Session": registered["session_token"]},
    )
    assert history.status_code == 200, history.text
    assert [item["content"] for item in history.json()] == ["legacy private history"]

    first_turn = client.post(
        "/api/chat",
        headers={"X-Jarvis-User-Session": registered["session_token"]},
        json={
            "message": "first turn after legacy migration",
            "conversation_id": conversation_id,
        },
    )
    assert first_turn.status_code == 200, first_turn.text
    assert first_turn.json()["conversation_id"] == conversation_id

    # A bridge restart reconstructs its hot cache from the durable binding.  The next
    # authenticated update must still use the claimed conversation, not mint a guest one.
    restarted_store = TelegramConversationStore(
        app.state.storage.database_path,
        realm_id="telegram:700001",
    )
    assert (
        restarted_store.get_or_create(chat_id, "guest", user_id=user_id)
        == conversation_id
    )
    registered_after_restart = _register_telegram_user(
        client,
        update_id=1_700_000 + chat_id,
        telegram_user_id=chat_id,
    )
    second_turn = client.post(
        "/api/chat",
        headers={
            "X-Jarvis-User-Session": registered_after_restart["session_token"]
        },
        json={
            "message": "turn after bridge restart",
            "conversation_id": restarted_store.get_or_create(
                chat_id,
                "guest",
                user_id=user_id,
            ),
        },
    )
    assert second_turn.status_code == 200, second_turn.text
    assert second_turn.json()["conversation_id"] == conversation_id

    with app.state.storage.locked_connection() as conn:
        owners = {
            table: conn.execute(
                f'SELECT DISTINCT user_id FROM "{table}" WHERE '
                + (
                    "id = ?"
                    if table == "conversations"
                    else "conversation_id = ?"
                ),
                (conversation_id,),
            ).fetchall()
            for table in (
                "conversations",
                "messages",
                "reminders",
                "learning_observations",
            )
        }
        binding = conn.execute(
            """
            SELECT user_id, access_mode FROM telegram_conversations
            WHERE realm_id = 'telegram:700001' AND chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
        user_conversation_ids = {
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM conversations WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        }
    assert {
        table: {str(row["user_id"]) for row in rows}
        for table, rows in owners.items()
    } == {
        "conversations": {user_id},
        "messages": {user_id},
        "reminders": {user_id},
        "learning_observations": {user_id},
    }
    assert dict(binding) == {"user_id": user_id, "access_mode": "guest"}
    assert user_conversation_ids == {conversation_id}


def test_same_telegram_user_id_is_isolated_across_canonical_bot_realms(client):
    first = _register_telegram_user(
        client,
        update_id=900001,
        telegram_user_id=919191,
        bot_id=700001,
    )
    second = _register_telegram_user(
        client,
        update_id=900001,
        telegram_user_id=919191,
        bot_id=700002,
    )
    first_again = _register_telegram_user(
        client,
        update_id=900002,
        telegram_user_id=919191,
        bot_id=700001,
    )

    assert first["user"]["id"] != second["user"]["id"]
    assert first_again["user"]["id"] == first["user"]["id"]
    with app.state.storage.locked_connection() as conn:
        realms = conn.execute(
            "SELECT realm_id, bot_id FROM telegram_realms ORDER BY bot_id"
        ).fetchall()
        identities = conn.execute(
            """
            SELECT realm_id, user_id FROM external_identities
            WHERE provider = 'telegram' AND provider_subject_id = '919191'
            ORDER BY realm_id
            """
        ).fetchall()
    assert [dict(row) for row in realms] == [
        {"realm_id": "telegram:700001", "bot_id": 700001},
        {"realm_id": "telegram:700002", "bot_id": 700002},
    ]
    assert len({str(row["user_id"]) for row in identities}) == 2


def test_backend_rejects_noncanonical_bridge_realm_before_persisting_identity(client):
    response = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "realm_id": "telegram:700002",
            "bot_id": 700001,
            "update_id": 700001,
            "telegram_user": {"id": 717171, "is_bot": False},
            "chat": {"id": 717171, "type": "private"},
        },
    )

    assert response.status_code == 409, response.text
    with app.state.storage.locked_connection() as conn:
        assert conn.execute("SELECT 1 FROM telegram_realms").fetchone() is None
        assert conn.execute("SELECT 1 FROM telegram_updates").fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM external_identities WHERE provider = 'telegram'"
        ).fetchone() is None


def test_legacy_history_claim_rolls_back_on_foreign_tenant_owner(client):
    foreign = app.state.authorization.upsert_external_identity(
        provider="test",
        realm_id="foreign-owner",
        provider_subject_id="foreign-owner",
        bootstrap_preset="guest",
    )
    foreign_user_id = str(foreign["user_id"])
    store = TelegramConversationStore(
        app.state.storage.database_path,
        realm_id="telegram:700001",
    )
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO conversations(id, title, created_at, updated_at, user_id)
            VALUES ('foreign-history', 'Foreign', ?, ?, ?)
            """,
            (now, now, foreign_user_id),
        )
        conn.execute(
            """
            INSERT INTO telegram_conversations(
                realm_id, chat_id, conversation_id, access_mode, user_id
            ) VALUES ('telegram:700001', 818181, 'foreign-history', 'guest', NULL)
            """
        )

    response = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "realm_id": "telegram:700001",
            "bot_id": 700001,
            "update_id": 818181,
            "telegram_user": {"id": 818181, "is_bot": False},
            "chat": {"id": 818181, "type": "private"},
        },
    )

    assert response.status_code == 403, response.text
    with app.state.storage.locked_connection() as conn:
        identity = conn.execute(
            """
            SELECT 1 FROM external_identities
            WHERE provider = 'telegram' AND realm_id = 'telegram:700001'
              AND provider_subject_id = '818181'
            """
        ).fetchone()
        binding = conn.execute(
            """
            SELECT user_id FROM telegram_conversations
            WHERE realm_id = 'telegram:700001' AND chat_id = 818181
            """
        ).fetchone()
        owner = conn.execute(
            "SELECT user_id FROM conversations WHERE id = 'foreign-history'"
        ).fetchone()
    assert identity is None
    assert binding["user_id"] is None
    assert owner["user_id"] == foreign_user_id
    assert store.load_all()[818181] == "foreign-history"


def test_telegram_sessions_isolate_memory_preferences_persona_and_files(client, monkeypatch):
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

    # LLM is disabled in the client fixture; stub completions so isolation
    # assertions exercise real chat routes without a live model.
    from jarvis_gpt.llm import LLMResult

    async def _stub_complete(*_args, **_kwargs):
        return LLMResult(ok=True, content="tenant isolation reply")

    monkeypatch.setattr(app.state.agent.llm, "complete", _stub_complete)

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
            "realm_id": "telegram:700001",
            "bot_id": 700001,
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


def test_preset_assignment_refreshes_next_telegram_session_permissions(client):
    registered = _register_telegram_user(client, update_id=30)
    user_id = registered["user"]["id"]
    old_token = registered["session_token"]

    assigned = _approved_request(
        client,
        "PUT",
        f"/api/admin/users/{user_id}/preset",
        json={"preset_key": "admin", "reason": "verify Telegram role refresh"},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["preset_key"] == "admin"
    assert client.get(
        "/api/conversations",
        headers={"X-Jarvis-User-Session": old_token},
    ).status_code == 401

    refreshed = client.post(
        "/api/integrations/telegram/session",
        headers={
            "X-Jarvis-Bridge-Secret": BRIDGE_SECRET,
            "X-Jarvis-User-Session": old_token,
        },
        json={
            "realm_id": "telegram:700001",
            "bot_id": 700001,
            "update_id": 31,
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
    assert refreshed.status_code == 200, refreshed.text
    refreshed_body = refreshed.json()
    assert refreshed_body["session_token"] != old_token
    assert refreshed_body["user"]["preset_key"] == "admin"
    assert client.get(
        "/api/admin/users",
        headers={"X-Jarvis-User-Session": refreshed_body["session_token"]},
    ).status_code == 200


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


def test_soft_deleted_telegram_user_stays_blocked_until_reactivated(client):
    registered = _register_telegram_user(client, update_id=38, telegram_user_id=383_838)
    user_id = registered["user"]["id"]
    blocked = _approved_request(
        client,
        "PATCH",
        f"/api/admin/users/{user_id}/status",
        json={"status": "deleted", "reason": "retain identity as a block"},
    )
    assert blocked.status_code == 200, blocked.text

    retry = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "realm_id": "telegram:700001",
            "bot_id": 700001,
            "update_id": 39,
            "telegram_user": {"id": 383_838, "is_bot": False},
            "chat": {"id": 383_838, "type": "private"},
        },
    )
    assert retry.status_code == 403, retry.text
    assert retry.json()["detail"] == "Telegram user is inactive"
    with app.state.storage.locked_connection() as conn:
        assert conn.execute(
            "SELECT status FROM users WHERE id = ?", (user_id,)
        ).fetchone()["status"] == "deleted"
        assert conn.execute(
            "SELECT 1 FROM external_identities WHERE user_id = ?", (user_id,)
        ).fetchone()

    reactivated = _approved_request(
        client,
        "PATCH",
        f"/api/admin/users/{user_id}/status",
        json={"status": "active", "reason": "explicit reactivation"},
    )
    assert reactivated.status_code == 200, reactivated.text
    refreshed = _register_telegram_user(
        client,
        update_id=40,
        telegram_user_id=383_838,
    )
    assert refreshed["user"]["id"] == user_id
    assert refreshed["user"]["created"] is False


def test_admin_delete_permanently_purges_user_and_allows_clean_registration(client):
    registered = _register_telegram_user(client, update_id=50, telegram_user_id=404_040)
    user_id = registered["user"]["id"]
    service = app.state.authorization
    storage = app.state.storage
    actor = service.actor_for_user(user_id, source="deletion-test")
    assert actor is not None

    with bind_actor(actor):
        conversation_id = storage.create_conversation("private deletion test")
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content="private message that must be deleted",
        )
        memory = storage.add_memory(
            content="private memory that must be deleted",
            namespace="private",
        )
        storage.set_runtime_value("preferences", {"private": True})
        user_files_dir = (
            app.state.settings.data_dir / "files" / "users" / user_id
        )
        user_files_dir.mkdir(parents=True, exist_ok=True)
        stored_path = user_files_dir / "private.txt"
        stored_path.write_text("private file", encoding="utf-8")
        file_record = storage.create_file_record(
            name=stored_path.name,
            stored_path=stored_path,
            sha256="a" * 64,
            size=stored_path.stat().st_size,
            mime_type="text/plain",
            status="indexed",
            chunk_count=1,
        )
        storage.add_file_chunks(file_record["id"], ["private indexed content"])
        app.state.playbooks.record(
            symptom="private deletion symptom",
            solution="private deletion solution",
            verification="private deletion verification",
            outcome="success",
        )

    TelegramConversationStore(
        storage.database_path,
        realm_id="telegram:700001",
    )
    with storage.transaction(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO telegram_conversations(
                realm_id, chat_id, conversation_id, access_mode, user_id
            ) VALUES ('telegram:700001', 404040, ?, 'guest', ?)
            """,
            (conversation_id, user_id),
        )
        conn.execute(
            """
            INSERT INTO telegram_update_inbox(
                realm_id, update_id, chat_id, payload_json, status,
                attempt_count, received_at, updated_at
            ) VALUES ('telegram:700001', 404040, 404040, '{"private":true}',
                      'completed', 1, 1.0, 1.0)
            """
        )

    provisioned = _approved_request(
        client,
        "POST",
        "/api/admin/users",
        json={
            "kind": "telegram",
            "telegram_user_id": 404_040,
            "realm_id": "telegram:700001",
            "preset_key": "guest",
            "reason": "retain eligibility while deleting the old account",
        },
    )
    assert provisioned.status_code == 201, provisioned.text

    deleted = _approved_request(
        client,
        "DELETE",
        f"/api/admin/users/{user_id}",
        json={"reason": "permanent deletion regression"},
    )
    assert deleted.status_code == 200, deleted.text
    body = deleted.json()
    assert body["permanently_deleted"] is True
    assert body["cleanup_complete"] is True
    assert body["deleted_counts"]["messages"] >= 1
    assert body["deleted_counts"]["memories"] == 1
    assert body["deleted_counts"]["execution_playbooks"] == 1

    with storage.locked_connection() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM external_identities WHERE user_id = ?", (user_id,)
        ).fetchone() is None
        for table in (
            "runtime_events",
            "conversations",
            "messages",
            "memories",
            "files",
            "file_chunks",
            "learning_observations",
            "approvals",
            "audit_log",
        ):
            assert conn.execute(
                f'SELECT 1 FROM "{table}" WHERE user_id = ?', (user_id,)
            ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM runtime_kv WHERE key LIKE ?",
            (f"user.{user_id}.%",),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM telegram_conversations WHERE chat_id = 404040"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM telegram_update_inbox WHERE chat_id = 404040"
        ).fetchone() is None
        audit = conn.execute(
            """
            SELECT target_id, target_user_id, after_json
            FROM security_audit_log
            WHERE action = 'user.delete' AND target_id = ?
            ORDER BY ts DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    assert audit is not None
    assert audit["target_user_id"] is None
    assert json.loads(audit["after_json"])["permanently_deleted"] is True

    with bind_actor(actor):
        assert app.state.playbooks.stats()["entries"] == 0
    assert not stored_path.exists()
    vault_note = (
        app.state.settings.data_dir
        / "memory-vault"
        / "users"
        / user_id
        / "private"
        / f"{memory['id']}.md"
    )
    assert not vault_note.exists()

    pre_provisioned = json.loads(
        (app.state.settings.state_dir / "telegram_pre_provisioned.json").read_text(
            encoding="utf-8"
        )
    )
    assert 404_040 in pre_provisioned["chat_ids"]
    assert "404040" not in pre_provisioned["users"]

    assert client.get(
        "/api/conversations",
        headers={"X-Jarvis-User-Session": registered["session_token"]},
    ).status_code == 401
    replacement = _register_telegram_user(
        client,
        update_id=51,
        telegram_user_id=404_040,
    )
    assert replacement["user"]["created"] is True
    assert replacement["user"]["id"] != user_id
    assert replacement["user"]["preset_key"] == "guest"
    replacement_headers = {"X-Jarvis-User-Session": replacement["session_token"]}
    assert client.get("/api/conversations", headers=replacement_headers).json() == []


def test_admin_delete_rejects_owner_accounts(client):
    response = _approved_request(
        client,
        "DELETE",
        f"/api/admin/users/{LEGACY_OWNER_USER_ID}",
        json={"reason": "owner must remain protected"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "Owner accounts cannot be permanently deleted"
    assert app.state.authorization.get_user(LEGACY_OWNER_USER_ID)["status"] == "active"


def test_user_delete_rolls_back_all_rows_when_final_account_delete_fails(client):
    registered = _register_telegram_user(client, update_id=52, telegram_user_id=424_242)
    user_id = registered["user"]["id"]
    storage = app.state.storage
    service = app.state.authorization
    with storage.transaction(immediate=True) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER block_test_user_delete
            BEFORE DELETE ON users
            WHEN OLD.id = '{user_id}'
            BEGIN
                SELECT RAISE(ABORT, 'test deletion failure');
            END
            """
        )

    with pytest.raises(AuthorizationError, match="rolled back"):
        service.delete_user(user_id=user_id, reason="rollback regression")

    with storage.transaction(immediate=True) as conn:
        assert conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
        assert conn.execute(
            "SELECT 1 FROM external_identities WHERE user_id = ?", (user_id,)
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM user_sessions WHERE user_id = ?", (user_id,)
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM user_preset_assignments WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.execute("DROP TRIGGER block_test_user_delete")

    assert client.get(
        "/api/conversations",
        headers={"X-Jarvis-User-Session": registered["session_token"]},
    ).status_code == 200


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
    registered = _register_telegram_user(client, update_id=10)
    identical_replay = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "realm_id": "telegram:700001",
            "bot_id": 700001,
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

    # Session registration is not the agent turn. Exact completed replays remain
    # available while a model/container outage is retried by the durable bridge.
    for _ in range(4):
        identical_replay = client.post(
            "/api/integrations/telegram/session",
            headers={
                "X-Jarvis-Bridge-Secret": BRIDGE_SECRET,
                "X-Jarvis-User-Session": registered["session_token"],
            },
            json={
                "realm_id": "telegram:700001",
                "bot_id": 700001,
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
        assert identical_replay.json()["session_token"] == registered["session_token"]

    replay = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "realm_id": "telegram:700001",
            "bot_id": 700001,
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
                WHERE realm_id = 'telegram:700001' AND update_id = 11
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
            "realm_id": "telegram:700001",
            "bot_id": 700001,
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
            FROM telegram_updates WHERE realm_id = 'telegram:700001' AND update_id = 11
            """
        ).fetchone()
        active_sessions = conn.execute(
            """
            SELECT COUNT(*) AS count FROM user_sessions s
            JOIN external_identities ei ON ei.user_id = s.user_id
            WHERE ei.provider = 'telegram' AND ei.realm_id = 'telegram:700001'
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
                WHERE realm_id = 'telegram:700001' AND update_id = 12
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
            "realm_id": "telegram:700001",
            "bot_id": 700001,
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
            FROM telegram_updates WHERE realm_id = 'telegram:700001' AND update_id = 12
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
            "realm_id": "telegram:700001",
            "bot_id": 700001,
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
            "realm_id": "telegram:700001",
            "bot_id": 700001,
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
