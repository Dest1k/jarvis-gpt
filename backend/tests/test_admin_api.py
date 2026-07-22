from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
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
from jarvis_gpt.telegram_operator import TelegramDeliveryError
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
    owner_invite_token: str | None = None,
    username: str | None = None,
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
                "username": username
                or (
                    "secure_user"
                    if telegram_user_id == 424242
                    else f"secure_user_{telegram_user_id}"
                ),
                "first_name": "Secure",
                "language_code": "en",
            },
            "chat": {"id": telegram_user_id, "type": "private"},
            **(
                {
                    "owner_invite_proof": hashlib.sha256(
                        owner_invite_token.encode("utf-8")
                    ).hexdigest()
                }
                if owner_invite_token
                else {}
            ),
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
    assert store.get_or_create(chat_id, "guest", user_id=user_id) == conversation_id
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
    assert restarted_store.get_or_create(chat_id, "guest", user_id=user_id) == conversation_id
    registered_after_restart = _register_telegram_user(
        client,
        update_id=1_700_000 + chat_id,
        telegram_user_id=chat_id,
    )
    second_turn = client.post(
        "/api/chat",
        headers={"X-Jarvis-User-Session": registered_after_restart["session_token"]},
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
                + ("id = ?" if table == "conversations" else "conversation_id = ?"),
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
    assert {table: {str(row["user_id"]) for row in rows} for table, rows in owners.items()} == {
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
        assert (
            conn.execute("SELECT 1 FROM external_identities WHERE provider = 'telegram'").fetchone()
            is None
        )


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
    first = _register_telegram_user(client, update_id=10_001, telegram_user_id=101_001)
    second = _register_telegram_user(client, update_id=10_002, telegram_user_id=202_002)
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
    first = _register_telegram_user(client, update_id=10_003, telegram_user_id=101_001)
    second = _register_telegram_user(client, update_id=10_004, telegram_user_id=202_002)
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
    assert (
        client.get(
            f"/api/conversations/{conversation_id}/messages", headers=second_headers
        ).status_code
        == 404
    )
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
    assert client.get("/api/memory?q=alpha-tenant-secret", headers=second_headers).json() == []

    updated_preferences = client.patch(
        "/api/preferences",
        headers=first_headers,
        json={"operator_name": "Tenant Alpha"},
    )
    assert updated_preferences.status_code == 200, updated_preferences.text
    assert (
        client.get("/api/preferences", headers=second_headers).json()["operator_name"]
        != "Tenant Alpha"
    )

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
    assert dict(decision) == {
        "effect": "deny",
        "reason_code": "preset_not_eligible",
    }


def test_account_catalog_routes_require_owner_or_admin_after_direct_grants_and_demotion(
    client,
):
    service = app.state.authorization
    routes = {
        "admin.users.list": "/api/admin/users",
        "admin.security_ids.list": "/api/admin/security-ids",
        "admin.presets.list": "/api/admin/presets",
    }
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="account-catalog-floor",
        provider_subject_id="moderator-with-direct-grants",
        bootstrap_preset="moderator",
    )
    user_id = str(identity["user_id"])
    for security_id in routes:
        result = service.set_user_permission(
            user_id=user_id,
            security_id=security_id,
            effect="grant",
            can_delegate=False,
            granted_by=LEGACY_OWNER_USER_ID,
            reason="direct grant must remain below the hard role floor",
        )
        assert result["effect"] == "deny"
        assert result["reason_code"] == "preset_not_eligible"

    catalog = {item["security_id"]: item for item in service.list_security_ids()}
    for security_id in routes:
        assert catalog[security_id]["required_presets"] == ["admin", "owner"]

    moderator_session = service.create_user_session(
        user_id=user_id,
        identity_id=str(identity["identity_id"]),
        auth_method="test",
    )
    moderator_headers = {"X-Jarvis-User-Session": str(moderator_session["session_token"])}
    for security_id, path in routes.items():
        denied = client.get(path, headers=moderator_headers)
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"] == {
            "error": "permission_denied",
            "security_id": security_id,
            "reason": "preset_not_eligible",
            "decision_id": denied.json()["detail"]["decision_id"],
        }

    service.assign_preset(
        user_id=user_id,
        preset_key="admin",
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="verify the eligible role can use the account catalog",
    )
    admin_session = service.create_user_session(
        user_id=user_id,
        identity_id=str(identity["identity_id"]),
        auth_method="test",
    )
    admin_headers = {"X-Jarvis-User-Session": str(admin_session["session_token"])}
    for path in routes.values():
        allowed = client.get(path, headers=admin_headers)
        assert allowed.status_code == 200, allowed.text

    service.assign_preset(
        user_id=user_id,
        preset_key="user",
        assigned_by=LEGACY_OWNER_USER_ID,
        reason="demotion must revoke catalog access immediately",
    )
    assert client.get("/api/admin/users", headers=admin_headers).status_code == 401
    demoted_session = service.create_user_session(
        user_id=user_id,
        identity_id=str(identity["identity_id"]),
        auth_method="test",
    )
    demoted_headers = {"X-Jarvis-User-Session": str(demoted_session["session_token"])}
    for security_id, path in routes.items():
        denied = client.get(path, headers=demoted_headers)
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"]["security_id"] == security_id
        assert denied.json()["detail"]["reason"] == "preset_not_eligible"


def test_one_time_owner_invitation_claims_immutable_telegram_identity(client):
    username_only = _register_telegram_user(
        client,
        update_id=60,
        telegram_user_id=515_151,
        username="JBL61R",
    )
    assert username_only["user"]["preset_key"] == "guest"

    issued = _approved_request(
        client,
        "POST",
        "/api/admin/telegram-owner-invitations",
        json={"expires_in_seconds": 1800, "reason": "Invite JBL61R as owner"},
    )
    assert issued.status_code == 201, issued.text
    invitation = issued.json()
    prefix = "/start owner_"
    assert invitation["command"].startswith(prefix)
    raw_token = invitation["command"].removeprefix(prefix)
    assert len(raw_token) == 43
    proof = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    verifier = hashlib.sha256(proof.encode("ascii")).hexdigest()

    with app.state.storage.locked_connection() as conn:
        stored = conn.execute(
            """
            SELECT token_verifier_sha256, consumed_at, claimed_user_id,
                   claimed_identity_id, reason
            FROM telegram_owner_invitations WHERE id = ?
            """,
            (invitation["id"],),
        ).fetchone()
    assert stored is not None
    assert stored["token_verifier_sha256"] == verifier
    assert raw_token not in json.dumps(dict(stored), ensure_ascii=False)
    assert stored["consumed_at"] is None

    claimed = _register_telegram_user(
        client,
        update_id=61,
        telegram_user_id=616_161,
        username="JBL61R",
        owner_invite_token=raw_token,
    )
    assert claimed["user"]["created"] is True
    assert claimed["user"]["preset_key"] == "owner"
    assert claimed["user"]["owner_invite_claimed"] is True
    assert (
        client.get(
            "/api/admin/users",
            headers={"X-Jarvis-User-Session": claimed["session_token"]},
        ).status_code
        == 200
    )

    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            "UPDATE telegram_owner_invitations SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", invitation["id"]),
        )

    # Once consumed, expiry must not break an exact lost-response replay by the
    # immutable winning identity. It still cannot grant anyone else.
    replay = _register_telegram_user(
        client,
        update_id=61,
        telegram_user_id=616_161,
        username="JBL61R",
        owner_invite_token=raw_token,
    )
    assert replay["user"]["id"] == claimed["user"]["id"]
    assert replay["user"]["preset_key"] == "owner"
    assert replay["user"]["owner_invite_claimed"] is True

    rejected = client.post(
        "/api/integrations/telegram/session",
        headers={"X-Jarvis-Bridge-Secret": BRIDGE_SECRET},
        json={
            "realm_id": "telegram:700001",
            "bot_id": 700001,
            "update_id": 62,
            "telegram_user": {"id": 626_262, "is_bot": False, "username": "attacker"},
            "chat": {"id": 626_262, "type": "private"},
            "owner_invite_proof": proof,
        },
    )
    assert rejected.status_code == 403, rejected.text
    assert rejected.json()["detail"] == "Owner invitation is invalid or expired"

    with app.state.storage.locked_connection() as conn:
        consumed = conn.execute(
            """
            SELECT consumed_at, claimed_user_id, claimed_identity_id
            FROM telegram_owner_invitations WHERE id = ?
            """,
            (invitation["id"],),
        ).fetchone()
        attacker = conn.execute(
            """
            SELECT 1 FROM external_identities
            WHERE provider = 'telegram' AND provider_subject_id = '626262'
            """
        ).fetchone()
        claims = conn.execute(
            """
            SELECT COUNT(*) AS c FROM security_audit_log
            WHERE action = 'telegram.owner_invitation.claim'
              AND target_user_id = ?
            """,
            (claimed["user"]["id"],),
        ).fetchone()
    assert consumed["consumed_at"] is not None
    assert consumed["claimed_user_id"] == claimed["user"]["id"]
    assert consumed["claimed_identity_id"] == claimed["user"]["identity_id"]
    assert attacker is None
    assert claims["c"] == 1

    pre_provisioned = json.loads(
        (app.state.settings.state_dir / "telegram_pre_provisioned.json").read_text(encoding="utf-8")
    )
    assert 616_161 in pre_provisioned["chat_ids"]


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
    assert (
        client.get(
            "/api/conversations",
            headers={"X-Jarvis-User-Session": old_token},
        ).status_code
        == 401
    )

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
    assert (
        client.get(
            "/api/admin/users",
            headers={"X-Jarvis-User-Session": refreshed_body["session_token"]},
        ).status_code
        == 200
    )


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
        assert (
            conn.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()["status"]
            == "deleted"
        )
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
        user_files_dir = app.state.settings.data_dir / "files" / "users" / user_id
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

    owner_conversation_id = storage.create_conversation("operator attribution survivor")
    owner_message_id = storage.add_message(
        conversation_id=owner_conversation_id,
        role="assistant",
        content="message sent by the account being deleted",
        metadata={"operator_user_id": user_id, "operator_authored": True},
    )

    TelegramConversationStore(
        storage.database_path,
        realm_id="telegram:700001",
    )
    with storage.transaction(immediate=True) as conn:
        authored_content = "authored send privacy row"
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO telegram_operator_sends(
                id, operator_user_id, client_request_id, realm_id, chat_id,
                conversation_id, user_id, content, content_sha256, status,
                telegram_message_id, message_id, created_at, updated_at, delivered_at
            ) VALUES (
                'delete_authored_send', ?, 'delete-authored-request',
                'telegram:700001', 101010, ?, ?, ?, ?, 'delivered',
                700, ?, ?, ?, ?
            )
            """,
            (
                user_id,
                owner_conversation_id,
                LEGACY_OWNER_USER_ID,
                authored_content,
                hashlib.sha256(authored_content.encode()).hexdigest(),
                owner_message_id,
                now,
                now,
                now,
            ),
        )
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
        conn.executemany(
            """
            INSERT INTO telegram_attachment_relay(
                realm_id, update_id, file_key, status, record_json, updated_at
            ) VALUES ('telegram:700001', ?, ?, 'success', '{"private":true}', 1.0)
            """,
            [(404040, "inbox-file"), (404041, "journal-file")],
        )
        conn.execute(
            """
            INSERT INTO telegram_message_log(
                id, realm_id, chat_id, direction, sender_kind, source_key,
                update_id, conversation_id, user_id, content, created_at
            ) VALUES (
                'tglog_delete_regression', 'telegram:700001', 404040,
                'inbound', 'user', 'in:404041:1', 404041, ?, ?,
                'private journal message', '2026-07-22T10:00:00+00:00'
            )
            """,
            (conversation_id, user_id),
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
    assert body["deleted_counts"]["telegram_attachment_relay"] == 2
    assert body["deleted_counts"]["telegram_operator_sends_authored"] == 1

    with storage.locked_connection() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None
        assert (
            conn.execute(
                "SELECT 1 FROM external_identities WHERE user_id = ?", (user_id,)
            ).fetchone()
            is None
        )
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
            assert (
                conn.execute(f'SELECT 1 FROM "{table}" WHERE user_id = ?', (user_id,)).fetchone()
                is None
            )
        assert (
            conn.execute(
                "SELECT 1 FROM runtime_kv WHERE key LIKE ?",
                (f"user.{user_id}.%",),
            ).fetchone()
            is None
        )
        assert (
            conn.execute("SELECT 1 FROM telegram_conversations WHERE chat_id = 404040").fetchone()
            is None
        )
        assert (
            conn.execute("SELECT 1 FROM telegram_update_inbox WHERE chat_id = 404040").fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM telegram_attachment_relay "
                "WHERE realm_id = 'telegram:700001' AND update_id IN (404040, 404041)"
            ).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM telegram_operator_sends WHERE id = 'delete_authored_send'"
            ).fetchone()
            is None
        )
        survivor = conn.execute(
            "SELECT metadata FROM messages WHERE id = ?",
            (owner_message_id,),
        ).fetchone()
        assert survivor is not None
        assert "operator_user_id" not in json.loads(survivor["metadata"])
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
        (app.state.settings.state_dir / "telegram_pre_provisioned.json").read_text(encoding="utf-8")
    )
    assert 404_040 in pre_provisioned["chat_ids"]
    assert "404040" not in pre_provisioned["users"]

    assert (
        client.get(
            "/api/conversations",
            headers={"X-Jarvis-User-Session": registered["session_token"]},
        ).status_code
        == 401
    )
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


def test_delete_user_purges_requester_material_access_audit(client):
    service = app.state.authorization
    identity = service.upsert_external_identity(
        provider="test",
        realm_id="material-audit-deletion",
        provider_subject_id="admin-requester",
        bootstrap_preset="admin",
    )
    user_id = str(identity["user_id"])
    actor = service.actor_for_user(user_id, source="material-audit-deletion")
    assert actor is not None

    app.state.agent.tools.material_access.accounts(actor, limit=5)
    with app.state.storage.locked_connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM material_access_audit WHERE requester_user_id = ?",
                (user_id,),
            ).fetchone()[0]
            == 1
        )

    deleted = service.delete_user(
        user_id=user_id,
        reason="privacy deletion must include privileged material access history",
    )

    assert deleted["ok"] is True
    assert deleted["deleted_counts"]["material_access_audit"] == 1
    with app.state.storage.locked_connection() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None
        assert (
            conn.execute(
                "SELECT 1 FROM material_access_audit WHERE requester_user_id = ?",
                (user_id,),
            ).fetchone()
            is None
        )


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
        assert conn.execute("SELECT 1 FROM user_sessions WHERE user_id = ?", (user_id,)).fetchone()
        assert conn.execute(
            "SELECT 1 FROM user_preset_assignments WHERE user_id = ?", (user_id,)
        ).fetchone()
        conn.execute("DROP TRIGGER block_test_user_delete")

    assert (
        client.get(
            "/api/conversations",
            headers={"X-Jarvis-User-Session": registered["session_token"]},
        ).status_code
        == 200
    )


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
        status_value = conn.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()[
            "status"
        ]
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


def test_stale_telegram_attempt_cannot_finalize_after_lease_is_reclaimed(client, monkeypatch):
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

    response = client.get("/api/admin/users?limit=200&offset=400&search=bulk_")
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 505
    assert page["limit"] == 200
    assert page["offset"] == 400
    assert len(page["users"]) == 105
    assert len({item["id"] for item in page["users"]}) == 105
    extra = next(item for item in page["users"] if item["id"] == "bulk_0450")
    assert len(extra["identities"]) == 2


def test_owner_telegram_console_lists_cross_tenant_history_and_sends_idempotently(
    client,
    monkeypatch,
):
    chat_id = 919_191
    realm_id = "telegram:700001"
    registered = _register_telegram_user(
        client,
        update_id=91,
        telegram_user_id=chat_id,
        username="operator_console_guest",
    )
    user_id = str(registered["user"]["id"])
    conversation_id = "tg_operator_console"
    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO conversations(
                id, title, created_at, updated_at, last_message,
                last_message_at, user_id
            ) VALUES (?, 'Telegram console', ?, ?, 'Ответ Джарвиса', ?, ?)
            """,
            (
                conversation_id,
                "2026-07-22T10:00:00+00:00",
                "2026-07-22T10:00:01+00:00",
                "2026-07-22T10:00:01+00:00",
                user_id,
            ),
        )
        conn.executemany(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, metadata, created_at, user_id
            ) VALUES (?, ?, ?, ?, '{}', ?, ?)
            """,
            [
                (
                    "msg_console_user",
                    conversation_id,
                    "user",
                    "Привет из Telegram",
                    "2026-07-22T10:00:00+00:00",
                    user_id,
                ),
                (
                    "msg_console_assistant",
                    conversation_id,
                    "assistant",
                    "Ответ Джарвиса",
                    "2026-07-22T10:00:01+00:00",
                    user_id,
                ),
            ],
        )
    TelegramConversationStore(
        app.state.storage.database_path,
        realm_id=realm_id,
    ).bind(chat_id, conversation_id, "guest", user_id=user_id)

    guest_headers = {"X-Jarvis-User-Session": registered["session_token"]}
    denied = client.get("/api/admin/telegram/chats", headers=guest_headers)
    assert denied.status_code == 403, denied.text
    assert denied.json()["detail"]["security_id"] == "admin.telegram.messages.read"

    chats = client.get("/api/admin/telegram/chats?search=operator_console")
    assert chats.status_code == 200, chats.text
    payload = chats.json()
    assert payload["total"] == 1
    assert payload["chats"][0]["chat_id"] == chat_id
    assert payload["chats"][0]["username"] == "operator_console_guest"
    assert payload["chats"][0]["message_count"] == 2

    history = client.get(f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages")
    assert history.status_code == 200, history.text
    assert [item["content"] for item in history.json()["messages"]] == [
        "Привет из Telegram",
        "Ответ Джарвиса",
    ]

    deliveries: list[dict] = []

    async def fake_send_telegram_text(**kwargs):
        deliveries.append(kwargs)
        return {
            "bot_id": 700001,
            "message_id": 777,
            "date": int(datetime(2026, 7, 22, 10, 0, 2, tzinfo=UTC).timestamp()),
        }

    monkeypatch.setattr(
        "jarvis_gpt.telegram_operator.send_telegram_text",
        fake_send_telegram_text,
    )
    request = {
        "content": "Ручной ответ владельца",
        "client_request_id": "console-request-0001",
    }
    sent = client.post(
        f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages",
        json=request,
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["send"]["status"] == "delivered"
    assert sent.json()["message"]["operator_authored"] is True
    assert sent.json()["message"]["content"] == request["content"]
    assert sent.json()["message"]["metadata"]["client_request_id"] == request[
        "client_request_id"
    ]
    assert sent.json()["message"]["sort_sequence"] == 777
    assert len(deliveries) == 1
    assert deliveries[0]["realm_id"] == realm_id
    assert deliveries[0]["chat_id"] == chat_id
    assert deliveries[0]["content"] == request["content"]

    replay = client.post(
        f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages",
        json=request,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["send"]["telegram_message_id"] == 777
    assert len(deliveries) == 1

    conflict = client.post(
        f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages",
        json={**request, "content": "Другой текст"},
    )
    assert conflict.status_code == 409, conflict.text

    updated_history = client.get(f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages").json()[
        "messages"
    ]
    operator_message = updated_history[-1]
    assert operator_message["content"] == request["content"]
    assert operator_message["delivery_status"] == "delivered"
    assert operator_message["metadata"]["client_request_id"] == request[
        "client_request_id"
    ]
    with app.state.storage.locked_connection() as conn:
        audit = conn.execute(
            """
            SELECT action, after_json FROM security_audit_log
            WHERE action = 'telegram.operator.send'
            ORDER BY ts DESC LIMIT 1
            """
        ).fetchone()
    assert audit is not None
    assert request["content"] not in str(audit["after_json"])


def test_owner_telegram_console_fences_uncertain_delivery_retries(client, monkeypatch):
    chat_id = 929_292
    realm_id = "telegram:700001"
    registered = _register_telegram_user(
        client,
        update_id=92,
        telegram_user_id=chat_id,
    )
    user_id = str(registered["user"]["id"])
    conversation_id = "tg_operator_uncertain"
    now = datetime.now(UTC).isoformat()
    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO conversations(id, title, created_at, updated_at, user_id)
            VALUES (?, 'Uncertain send', ?, ?, ?)
            """,
            (conversation_id, now, now, user_id),
        )
    TelegramConversationStore(
        app.state.storage.database_path,
        realm_id=realm_id,
    ).bind(chat_id, conversation_id, "guest", user_id=user_id)
    calls = 0

    async def ambiguous_send(**_kwargs):
        nonlocal calls
        calls += 1
        raise TelegramDeliveryError("telegram_send_transport_unknown", uncertain=True)

    monkeypatch.setattr(
        "jarvis_gpt.telegram_operator.send_telegram_text",
        ambiguous_send,
    )
    request = {
        "content": "Не дублировать при неизвестном результате",
        "client_request_id": "console-request-uncertain-0001",
    }
    first = client.post(
        f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages",
        json=request,
    )
    assert first.status_code == 504, first.text
    assert first.json()["send"]["status"] == "uncertain"

    replay = client.post(
        f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages",
        json=request,
    )
    assert replay.status_code == 504, replay.text
    assert replay.json()["send"]["status"] == "uncertain"
    assert calls == 1


def _seed_telegram_console_chat(
    client: TestClient,
    *,
    chat_id: int,
    conversation_id: str,
    username: str,
) -> tuple[str, str]:
    realm_id = "telegram:700001"
    registered = _register_telegram_user(
        client,
        update_id=chat_id,
        telegram_user_id=chat_id,
        username=username,
    )
    user_id = str(registered["user"]["id"])
    now = datetime.now(UTC).isoformat()
    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO conversations(id, title, created_at, updated_at, user_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conversation_id, username, now, now, user_id),
        )
    TelegramConversationStore(
        app.state.storage.database_path,
        realm_id=realm_id,
    ).bind(chat_id, conversation_id, "guest", user_id=user_id)
    return realm_id, user_id


def test_telegram_console_history_cursor_retrieves_all_merged_messages(client):
    chat_id = 939_391
    conversation_id = "tg_console_cursor"
    realm_id, user_id = _seed_telegram_console_chat(
        client,
        chat_id=chat_id,
        conversation_id=conversation_id,
        username="cursor_regression",
    )
    base = datetime(2026, 7, 1, tzinfo=UTC)
    backend_count = 311
    journal_count = 313
    backend_ids = {f"cursor_backend_{index:04d}" for index in range(backend_count)}
    journal_ids = {f"cursor_journal_{index:04d}" for index in range(journal_count)}
    with app.state.storage.transaction(immediate=True) as conn:
        conn.executemany(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, metadata, created_at, user_id
            ) VALUES (?, ?, 'assistant', ?, '{}', ?, ?)
            """,
            [
                (
                    message_id,
                    conversation_id,
                    f"backend-{index}",
                    (base + timedelta(seconds=index * 2)).isoformat(),
                    user_id,
                )
                for index, message_id in enumerate(sorted(backend_ids))
            ],
        )
        conn.executemany(
            """
            INSERT INTO telegram_message_log(
                id, realm_id, chat_id, direction, sender_kind, source_key,
                conversation_id, user_id, content, content_type, metadata,
                created_at
            ) VALUES (?, ?, ?, 'inbound', 'user', ?, ?, ?, ?, 'text', '{}', ?)
            """,
            [
                (
                    message_id,
                    realm_id,
                    chat_id,
                    f"cursor-source-{index:04d}",
                    conversation_id,
                    user_id,
                    f"journal-{index}",
                    (base + timedelta(seconds=index * 2 + 1)).isoformat(),
                )
                for index, message_id in enumerate(sorted(journal_ids))
            ],
        )

    path = f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages"
    seen_ids: list[str] = []
    before = ""
    for _page_number in range(10):
        params = {"limit": 113}
        if before:
            params["before"] = before
        response = client.get(path, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        page_ids = [item["id"] for item in page["messages"]]
        assert len(page_ids) <= 113
        assert not set(page_ids).intersection(seen_ids)
        seen_ids.extend(page_ids)
        if not page["has_more"]:
            assert page["next_before"] is None
            break
        assert page["next_before"]
        assert page["next_before"] != before
        before = page["next_before"]
    else:
        pytest.fail("Telegram history pagination did not terminate")

    expected_ids = backend_ids | journal_ids
    assert len(expected_ids) > 500
    assert len(seen_ids) == len(expected_ids)
    assert set(seen_ids) == expected_ids


def test_telegram_console_keeps_legacy_user_rows_and_deduplicates_exact_hash(client):
    chat_id = 939_392
    conversation_id = "tg_console_dedup"
    realm_id, user_id = _seed_telegram_console_chat(
        client,
        chat_id=chat_id,
        conversation_id=conversation_id,
        username="dedup_regression",
    )
    exact_hash = "request-hash-exact"
    with app.state.storage.transaction(immediate=True) as conn:
        conn.executemany(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, metadata, created_at, user_id
            ) VALUES (?, ?, 'user', ?, ?, ?, ?)
            """,
            [
                (
                    "dedup_legacy_backend",
                    conversation_id,
                    "legacy-without-hash",
                    "{}",
                    "2026-07-22T09:00:00+00:00",
                    user_id,
                ),
                (
                    "dedup_exact_backend",
                    conversation_id,
                    "exact-duplicate",
                    json.dumps({"chat_request_hash": exact_hash}),
                    "2026-07-22T09:01:00+00:00",
                    user_id,
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO telegram_message_log(
                id, realm_id, chat_id, direction, sender_kind, source_key,
                conversation_id, user_id, content, content_type, metadata,
                created_at
            ) VALUES (?, ?, ?, 'inbound', 'user', ?, ?, ?, ?, 'text', ?, ?)
            """,
            [
                (
                    "dedup_exact_journal",
                    realm_id,
                    chat_id,
                    "dedup-source-exact",
                    conversation_id,
                    user_id,
                    "exact-duplicate",
                    json.dumps({"chat_request_hash": exact_hash}),
                    "2026-07-22T09:01:01+00:00",
                ),
                (
                    "dedup_newer_journal",
                    realm_id,
                    chat_id,
                    "dedup-source-newer",
                    conversation_id,
                    user_id,
                    "newer-journal-only",
                    json.dumps({"chat_request_hash": "request-hash-newer"}),
                    "2026-07-22T09:02:00+00:00",
                ),
            ],
        )

    response = client.get(
        f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages",
        params={"limit": 50},
    )
    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    ids = {item["id"] for item in messages}
    assert "dedup_legacy_backend" in ids
    assert "dedup_exact_journal" in ids
    assert "dedup_newer_journal" in ids
    assert "dedup_exact_backend" not in ids
    assert [item["content"] for item in messages].count("exact-duplicate") == 1


def test_telegram_console_uses_telegram_message_order_for_equal_timestamps(client):
    chat_id = 939_397
    conversation_id = "tg_console_equal_second"
    realm_id, user_id = _seed_telegram_console_chat(
        client,
        chat_id=chat_id,
        conversation_id=conversation_id,
        username="equal_second_order",
    )
    created_at = "2026-07-22T09:30:00+00:00"
    with app.state.storage.transaction(immediate=True) as conn:
        conn.executemany(
            """
            INSERT INTO telegram_message_log(
                id, realm_id, chat_id, direction, sender_kind, source_key,
                telegram_message_id, conversation_id, user_id, content,
                content_type, metadata, created_at
            ) VALUES (?, ?, ?, 'inbound', 'user', ?, ?, ?, ?, ?, 'text', '{}', ?)
            """,
            [
                (
                    "tglog_hash_sorts_first_but_message_is_second",
                    realm_id,
                    chat_id,
                    "equal-second-101",
                    101,
                    conversation_id,
                    user_id,
                    "second",
                    created_at,
                ),
                (
                    "zz_tglog_hash_sorts_last_but_message_is_first",
                    realm_id,
                    chat_id,
                    "equal-second-100",
                    100,
                    conversation_id,
                    user_id,
                    "first",
                    created_at,
                ),
            ],
        )

    response = client.get(
        f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages",
        params={"limit": 1},
    )
    assert response.status_code == 200, response.text
    newest = response.json()
    assert [item["content"] for item in newest["messages"]] == ["second"]
    assert newest["messages"][0]["sort_sequence"] == 101
    assert newest["messages"][0]["sort_rank"] == 0
    assert newest["has_more"] is True

    older = client.get(
        f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages",
        params={"limit": 1, "before": newest["next_before"]},
    )
    assert older.status_code == 200, older.text
    assert [item["content"] for item in older.json()["messages"]] == ["first"]
    assert older.json()["messages"][0]["sort_sequence"] == 100
    assert older.json()["messages"][0]["sort_rank"] == 0

    chats = client.get(
        "/api/admin/telegram/chats",
        params={"search": "equal_second_order"},
    )
    assert chats.status_code == 200, chats.text
    assert chats.json()["chats"][0]["last_message"] == "second"


def test_telegram_console_prefers_delivered_transport_over_backend_turn_rows(client):
    chat_id = 939_398
    conversation_id = "tg_console_transport_truth"
    realm_id, user_id = _seed_telegram_console_chat(
        client,
        chat_id=chat_id,
        conversation_id=conversation_id,
        username="transport_truth",
    )
    request_hash = "transport-truth-hash"
    metadata = json.dumps({"chat_request_hash": request_hash})
    with app.state.storage.transaction(immediate=True) as conn:
        conn.executemany(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, metadata, created_at, user_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "transport_backend_user",
                    conversation_id,
                    "user",
                    "synthetic callback prompt",
                    metadata,
                    "2026-07-22T09:40:00+00:00",
                    user_id,
                ),
                (
                    "transport_backend_assistant",
                    conversation_id,
                    "assistant",
                    "one large backend answer",
                    metadata,
                    "2026-07-22T09:40:01+00:00",
                    user_id,
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO telegram_message_log(
                id, realm_id, chat_id, direction, sender_kind, source_key,
                telegram_message_id, conversation_id, user_id, content,
                content_type, metadata, created_at
            ) VALUES (?, ?, ?, 'outbound', 'bot', ?, ?, ?, ?, ?, 'text', ?, ?)
            """,
            [
                (
                    "transport_chunk_1",
                    realm_id,
                    chat_id,
                    "transport-chunk-1",
                    200,
                    conversation_id,
                    user_id,
                    "chunk one",
                    metadata,
                    "2026-07-22T09:40:01+00:00",
                ),
                (
                    "transport_chunk_2",
                    realm_id,
                    chat_id,
                    "transport-chunk-2",
                    201,
                    conversation_id,
                    user_id,
                    "chunk two",
                    metadata,
                    "2026-07-22T09:40:01+00:00",
                ),
            ],
        )

    response = client.get(
        f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages",
        params={"limit": 50},
    )
    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    assert [item["content"] for item in messages] == ["chunk one", "chunk two"]


def test_telegram_console_chat_order_prefers_newer_journal_activity(client):
    older_chat_id = 939_393
    journal_chat_id = 939_394
    realm_id, _older_user_id = _seed_telegram_console_chat(
        client,
        chat_id=older_chat_id,
        conversation_id="tg_console_order_older",
        username="ordering_case_older",
    )
    _realm_id, journal_user_id = _seed_telegram_console_chat(
        client,
        chat_id=journal_chat_id,
        conversation_id="tg_console_order_journal",
        username="ordering_case_journal",
    )
    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            """
            UPDATE conversations
            SET last_message = 'newer conversation',
                last_message_at = '2026-07-22T12:00:00+00:00'
            WHERE id = 'tg_console_order_older'
            """
        )
        conn.execute(
            """
            UPDATE conversations
            SET last_message = 'older conversation',
                last_message_at = '2026-07-22T10:00:00+00:00'
            WHERE id = 'tg_console_order_journal'
            """
        )
        conn.execute(
            """
            INSERT INTO telegram_message_log(
                id, realm_id, chat_id, direction, sender_kind, source_key,
                conversation_id, user_id, content, content_type, metadata,
                created_at
            ) VALUES (
                'ordering_journal_message', ?, ?, 'inbound', 'user',
                'ordering-journal-source', 'tg_console_order_journal', ?,
                'journal wins', 'text', '{}', '2026-07-22T13:00:00+00:00'
            )
            """,
            (realm_id, journal_chat_id, journal_user_id),
        )

    response = client.get(
        "/api/admin/telegram/chats",
        params={"search": "ordering_case"},
    )
    assert response.status_code == 200, response.text
    chats = response.json()["chats"]
    assert [item["chat_id"] for item in chats] == [journal_chat_id, older_chat_id]
    assert chats[0]["last_message"] == "journal wins"
    assert chats[0]["last_message_at"] == "2026-07-22T13:00:00+00:00"


def test_telegram_console_claims_precreated_pending_send_exactly_once(
    client,
    monkeypatch,
):
    chat_id = 939_395
    conversation_id = "tg_console_pending_claim"
    realm_id, user_id = _seed_telegram_console_chat(
        client,
        chat_id=chat_id,
        conversation_id=conversation_id,
        username="pending_claim_regression",
    )
    content = "deliver precreated pending"
    request_id = "precreated-pending-0001"
    now = datetime.now(UTC).isoformat()
    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO telegram_operator_sends(
                id, operator_user_id, client_request_id, realm_id, chat_id,
                conversation_id, user_id, content, content_sha256, status,
                delivery_claimed_at, delivery_attempt_count, created_at, updated_at
            ) VALUES (
                'precreated_pending_send', ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                NULL, 0, ?, ?
            )
            """,
            (
                LEGACY_OWNER_USER_ID,
                request_id,
                realm_id,
                chat_id,
                conversation_id,
                user_id,
                content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                now,
                now,
            ),
        )
    deliveries = 0

    async def fake_delivery(**_kwargs):
        nonlocal deliveries
        deliveries += 1
        return {
            "bot_id": 700001,
            "message_id": 939395,
            "date": int(datetime(2026, 7, 22, 14, 0, tzinfo=UTC).timestamp()),
        }

    monkeypatch.setattr(
        "jarvis_gpt.telegram_operator.send_telegram_text",
        fake_delivery,
    )
    path = f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages"
    request = {"content": content, "client_request_id": request_id}
    first = client.post(path, json=request)
    assert first.status_code == 200, first.text
    assert first.json()["send"]["status"] == "delivered"
    replay = client.post(path, json=request)
    assert replay.status_code == 200, replay.text
    assert replay.json()["send"]["status"] == "delivered"
    assert deliveries == 1
    with app.state.storage.locked_connection() as conn:
        stored = conn.execute(
            """
            SELECT status, delivery_attempt_count, delivery_claimed_at,
                   telegram_message_id, message_id
            FROM telegram_operator_sends WHERE id = 'precreated_pending_send'
            """
        ).fetchone()
    assert stored is not None
    assert stored["status"] == "delivered"
    assert stored["delivery_attempt_count"] == 1
    assert stored["delivery_claimed_at"]
    assert stored["telegram_message_id"] == 939395
    assert stored["message_id"]


def test_telegram_console_fences_precreated_stale_claim_without_resend(
    client,
    monkeypatch,
):
    chat_id = 939_396
    conversation_id = "tg_console_stale_claim"
    realm_id, user_id = _seed_telegram_console_chat(
        client,
        chat_id=chat_id,
        conversation_id=conversation_id,
        username="stale_claim_regression",
    )
    content = "do not resend stale claim"
    request_id = "precreated-stale-0001"
    now = datetime.now(UTC)
    stale_claimed_at = (now - timedelta(minutes=10)).isoformat()
    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO telegram_operator_sends(
                id, operator_user_id, client_request_id, realm_id, chat_id,
                conversation_id, user_id, content, content_sha256, status,
                delivery_claimed_at, delivery_attempt_count, created_at, updated_at
            ) VALUES (
                'precreated_stale_send', ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                ?, 1, ?, ?
            )
            """,
            (
                LEGACY_OWNER_USER_ID,
                request_id,
                realm_id,
                chat_id,
                conversation_id,
                user_id,
                content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                stale_claimed_at,
                stale_claimed_at,
                now.isoformat(),
            ),
        )
    deliveries = 0

    async def unexpected_delivery(**_kwargs):
        nonlocal deliveries
        deliveries += 1
        return {
            "bot_id": 700001,
            "message_id": 1,
            "date": int(now.timestamp()),
        }

    monkeypatch.setattr(
        "jarvis_gpt.telegram_operator.send_telegram_text",
        unexpected_delivery,
    )
    path = f"/api/admin/telegram/chats/{realm_id}/{chat_id}/messages"
    request = {"content": content, "client_request_id": request_id}
    first = client.post(path, json=request)
    assert first.status_code == 504, first.text
    assert first.json()["send"]["status"] == "uncertain"
    assert first.json()["send"]["error_code"] == "delivery_claim_expired"
    replay = client.post(path, json=request)
    assert replay.status_code == 504, replay.text
    assert replay.json()["send"]["status"] == "uncertain"
    assert deliveries == 0
    with app.state.storage.locked_connection() as conn:
        stored = conn.execute(
            """
            SELECT status, delivery_attempt_count, error_code
            FROM telegram_operator_sends WHERE id = 'precreated_stale_send'
            """
        ).fetchone()
    assert stored is not None
    assert stored["status"] == "uncertain"
    assert stored["delivery_attempt_count"] == 1
    assert stored["error_code"] == "delivery_claim_expired"


def test_user_delete_blocks_active_telegram_operator_delivery(client):
    chat_id = 939_399
    conversation_id = "tg_console_delete_inflight"
    realm_id, user_id = _seed_telegram_console_chat(
        client,
        chat_id=chat_id,
        conversation_id=conversation_id,
        username="delete_inflight",
    )
    content = "in flight delete fence"
    now = datetime.now(UTC)
    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO telegram_operator_sends(
                id, operator_user_id, client_request_id, realm_id, chat_id,
                conversation_id, user_id, content, content_sha256, status,
                delivery_claimed_at, delivery_attempt_count, created_at, updated_at
            ) VALUES (
                'delete_inflight_send', ?, 'delete-inflight-request', ?, ?, ?, ?,
                ?, ?, 'pending', ?, 1, ?, ?
            )
            """,
            (
                LEGACY_OWNER_USER_ID,
                realm_id,
                chat_id,
                conversation_id,
                user_id,
                content,
                hashlib.sha256(content.encode()).hexdigest(),
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )

    blocked = _approved_request(
        client,
        "DELETE",
        f"/api/admin/users/{user_id}",
        json={"reason": "must wait for active delivery"},
    )
    assert blocked.status_code == 409, blocked.text
    assert "delivery is in flight" in blocked.json()["detail"]

    with app.state.storage.transaction(immediate=True) as conn:
        conn.execute(
            """
            UPDATE telegram_operator_sends
            SET delivery_claimed_at = ?
            WHERE id = 'delete_inflight_send'
            """,
            ((now - timedelta(minutes=10)).isoformat(),),
        )
    deleted = _approved_request(
        client,
        "DELETE",
        f"/api/admin/users/{user_id}",
        json={"reason": "stale delivery is safely fenced"},
    )
    assert deleted.status_code == 200, deleted.text
