from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from jarvis_gpt.agent import (
    CHAT_REQUEST_KEY_PREFIX,
    LEGACY_GUEST_REQUEST_KEY_PREFIX,
    AgentRuntime,
    GuestChatUnavailableError,
)
from jarvis_gpt.authorization import ActorContext, bind_actor
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.storage import JarvisStorage


class _GuestLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict]] = []
        self.results: list[SimpleNamespace] = []

    async def complete(self, messages, **_kwargs):
        self.calls.append(messages)
        if self.results:
            return self.results.pop(0)
        return SimpleNamespace(ok=True, content="Гостевой ответ")


def _runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = _GuestLLM()
    return storage, llm, AgentRuntime(settings=settings, storage=storage, llm=llm)


def test_guest_chat_never_enters_local_agent_routes(monkeypatch, tmp_path):
    storage, llm, agent = _runtime(monkeypatch, tmp_path)

    response = asyncio.run(
        agent.chat(
            "Следи за экраном и скажи, когда появится пароль",
            access_mode="guest",
            notification_chat_id=99,
        )
    )

    assert response.answer == "Гостевой ответ"
    assert response.events == []
    assert storage.list_reminders(status="all") == []
    assert "нет доступа к экрану" in llm.calls[0][0]["content"]
    assert all("notification_chat_id" not in str(message) for message in llm.calls[0])
    storage.close()


def test_legacy_guest_cannot_attach_to_or_read_owner_conversation(monkeypatch, tmp_path):
    storage, llm, agent = _runtime(monkeypatch, tmp_path)
    owner_conversation = storage.create_conversation("Владелец")
    storage.add_message(
        conversation_id=owner_conversation,
        role="assistant",
        content="OWNER_SECRET_SENTINEL",
        metadata={"access_mode": "owner"},
    )

    response = asyncio.run(
        agent.chat(
            "Повтори предыдущий ответ",
            conversation_id=owner_conversation,
            access_mode="guest",
        )
    )

    assert response.conversation_id != owner_conversation
    assert "OWNER_SECRET_SENTINEL" not in str(llm.calls[0])
    assert all(
        item["metadata"].get("access_mode") == "guest"
        for item in storage.list_messages(response.conversation_id)
    )
    storage.close()


def test_legacy_owner_cannot_promote_guest_transcript_into_privileged_context(
    monkeypatch, tmp_path
):
    storage, llm, agent = _runtime(monkeypatch, tmp_path)
    guest = asyncio.run(
        agent.chat(
            "GUEST_ADVERSARIAL_SENTINEL",
            access_mode="guest",
        )
    )

    owner_context = agent._prepare_context("owner request", guest.conversation_id)

    assert owner_context.conversation_id != guest.conversation_id
    assert storage.recent_messages(owner_context.conversation_id, limit=10) == []
    assert "GUEST_ADVERSARIAL_SENTINEL" not in str(owner_context)
    storage.close()


def test_authenticated_role_downgrade_preserves_same_tenant_conversation(
    monkeypatch, tmp_path
):
    storage, llm, agent = _runtime(monkeypatch, tmp_path)
    identity = agent.permissions.upsert_external_identity(
        provider="test",
        realm_id="role-change",
        provider_subject_id="downgrade",
        bootstrap_preset="guest",
    )
    user_id = str(identity["user_id"])
    guest_actor = agent.permissions.actor_for_user(user_id, source="session")
    assert guest_actor is not None and guest_actor.preset_key == "guest"
    owner_actor = ActorContext(
        user_id=user_id,
        preset_key="owner",
        source="session",
        identity_id=guest_actor.identity_id,
        policy_epoch=guest_actor.policy_epoch,
    )
    with bind_actor(owner_actor):
        conversation_id = storage.create_conversation("Владелец")
        storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content="SAME_TENANT_OWNER_HISTORY",
            metadata={"access_mode": "owner"},
        )
    with bind_actor(guest_actor):
        response = asyncio.run(
            agent.chat("Повтори предыдущий ответ", conversation_id=conversation_id)
        )
        messages = storage.list_messages(conversation_id)

    assert response.conversation_id == conversation_id
    assert "SAME_TENANT_OWNER_HISTORY" in str(llm.calls[0])
    assert messages[-1]["metadata"]["access_mode"] == "guest"
    storage.close()


def test_authenticated_role_promotion_preserves_same_tenant_conversation(
    monkeypatch, tmp_path
):
    storage, _llm, agent = _runtime(monkeypatch, tmp_path)
    identity = agent.permissions.upsert_external_identity(
        provider="test",
        realm_id="role-change",
        provider_subject_id="promotion",
        bootstrap_preset="guest",
    )
    user_id = str(identity["user_id"])
    guest_actor = agent.permissions.actor_for_user(user_id, source="session")
    assert guest_actor is not None and guest_actor.preset_key == "guest"
    with bind_actor(guest_actor):
        guest = asyncio.run(agent.chat("SAME_TENANT_GUEST_HISTORY"))
    owner_actor = ActorContext(
        user_id=user_id,
        preset_key="owner",
        source="session",
        identity_id=guest_actor.identity_id,
        policy_epoch=guest_actor.policy_epoch,
    )

    with bind_actor(owner_actor):
        owner_context = agent._prepare_context("owner request", guest.conversation_id)
        history = storage.recent_messages(owner_context.conversation_id, limit=10)

    assert owner_context.conversation_id == guest.conversation_id
    assert "SAME_TENANT_GUEST_HISTORY" in str(history)
    storage.close()


def test_guest_transient_failure_retries_without_duplicate_messages(
    monkeypatch, tmp_path
):
    storage, llm, agent = _runtime(monkeypatch, tmp_path)
    llm.results = [
        SimpleNamespace(ok=False, content="", error="temporarily unavailable"),
        SimpleNamespace(ok=True, content="Ответ после повтора", error=None),
    ]
    conversation_id = storage.create_conversation("Гостевой Telegram-диалог")
    kwargs = {
        "conversation_id": conversation_id,
        "access_mode": "guest",
        "transport_request_id": "telegram:700001:991",
    }

    with pytest.raises(GuestChatUnavailableError):
        asyncio.run(agent.chat("Повтори безопасно", **kwargs))
    assert storage.list_messages(conversation_id) == []

    response = asyncio.run(agent.chat("Повтори безопасно", **kwargs))
    replay = asyncio.run(agent.chat("Повтори безопасно", **kwargs))
    messages = storage.list_messages(conversation_id)

    assert replay == response
    assert len(llm.calls) == 2
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Повтори безопасно"),
        ("assistant", "Ответ после повтора"),
    ]
    storage.close()


def test_guest_request_idempotency_is_scoped_to_authenticated_user(
    monkeypatch, tmp_path
):
    storage, llm, agent = _runtime(monkeypatch, tmp_path)
    llm.results = [
        SimpleNamespace(ok=True, content="Ответ пользователя A", error=None),
        SimpleNamespace(ok=True, content="Ответ пользователя B", error=None),
    ]
    actors = []
    for subject in ("user-a", "user-b"):
        identity = agent.permissions.upsert_external_identity(
            provider="test",
            realm_id="idempotency-scope",
            provider_subject_id=subject,
            bootstrap_preset="guest",
        )
        actor = agent.permissions.actor_for_user(
            str(identity["user_id"]), source="session"
        )
        assert actor is not None
        actors.append(actor)

    responses = []
    for actor in actors:
        with bind_actor(actor):
            responses.append(
                asyncio.run(
                    agent.chat(
                        "Одинаковый запрос",
                        transport_request_id="shared-transport-request",
                    )
                )
            )

    assert [response.answer for response in responses] == [
        "Ответ пользователя A",
        "Ответ пользователя B",
    ]
    assert responses[0].conversation_id != responses[1].conversation_id
    assert len(llm.calls) == 2
    storage.close()


def test_guest_request_ledger_prunes_expired_rows_without_small_volume_cap(
    monkeypatch,
    tmp_path,
):
    storage, _llm, agent = _runtime(monkeypatch, tmp_path)
    identity = agent.permissions.upsert_external_identity(
        provider="test",
        realm_id="request-ledger-prune",
        provider_subject_id="guest",
        bootstrap_preset="guest",
    )
    actor = agent.permissions.actor_for_user(str(identity["user_id"]), source="session")
    assert actor is not None

    with bind_actor(actor):
        expired_chat = storage.set_runtime_value(
            f"{CHAT_REQUEST_KEY_PREFIX}expired",
            {"status": "completed"},
        )
        expired_legacy = storage.set_runtime_value(
            f"{LEGACY_GUEST_REQUEST_KEY_PREFIX}expired",
            {"status": "completed"},
        )
        escaped_prefix_decoy = storage.set_runtime_value(
            "agent.chatXrequest.expired",
            {"keep": True},
        )
        for index in range(300):
            storage.set_runtime_value(
                f"{CHAT_REQUEST_KEY_PREFIX}fresh-{index}",
                {"status": "completed", "index": index},
            )
        expired_at = (datetime.now(UTC) - timedelta(hours=27)).isoformat()
        storage.connect().execute(
            "UPDATE runtime_kv SET updated_at = ? WHERE key IN (?, ?, ?)",
            (
                expired_at,
                expired_chat["key"],
                expired_legacy["key"],
                escaped_prefix_decoy["key"],
            ),
        )
        storage.connect().commit()

        response = asyncio.run(
            agent.chat(
                "request after normal guest volume",
                transport_request_id="telegram:request-ledger-prune:301",
            )
        )

        chat_rows = storage.list_runtime_values(prefix=CHAT_REQUEST_KEY_PREFIX)
        legacy_rows = storage.list_runtime_values(
            prefix=LEGACY_GUEST_REQUEST_KEY_PREFIX
        )
        assert response.answer
        assert expired_chat["key"] not in {row["key"] for row in chat_rows}
        assert expired_legacy["key"] not in {row["key"] for row in legacy_rows}
        assert len(chat_rows) == 301
        assert storage.get_runtime_value("agent.chatXrequest.expired") == {"keep": True}
    storage.close()
