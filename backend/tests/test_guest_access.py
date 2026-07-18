from __future__ import annotations

import asyncio
from types import SimpleNamespace

from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.storage import JarvisStorage


class _GuestLLM:
    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def complete(self, messages, **_kwargs):
        self.calls.append(messages)
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


def test_guest_cannot_attach_to_or_read_an_owner_conversation(monkeypatch, tmp_path):
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


def test_owner_cannot_promote_guest_transcript_into_privileged_context(monkeypatch, tmp_path):
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
