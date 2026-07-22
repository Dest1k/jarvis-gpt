from __future__ import annotations

import asyncio

import pytest
from jarvis_gpt.agent import AgentRuntime, ChatRequestConflictError
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import ChatResponse
from jarvis_gpt.storage import JarvisStorage


def _runtime(monkeypatch, tmp_path) -> tuple[AgentRuntime, JarvisStorage]:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return (
        AgentRuntime(
            settings=settings,
            storage=storage,
            llm=LLMRouter(settings),
            bus=EventBus(),
        ),
        storage,
    )


def test_full_chat_persists_ingress_before_context_failure(monkeypatch, tmp_path):
    agent, storage = _runtime(monkeypatch, tmp_path)

    def fail_context(*_args, **_kwargs):
        raise RuntimeError("context failed after durable ingress")

    monkeypatch.setattr(agent, "_prepare_context", fail_context)
    with pytest.raises(RuntimeError, match="context failed"):
        asyncio.run(agent.chat("Exact message that must survive"))

    conversations = storage.list_conversations(limit=10)
    assert len(conversations) == 1
    messages = storage.list_messages(conversations[0]["id"])
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Exact message that must survive")
    ]
    assert messages[0]["metadata"]["ingress_status"] == "accepted"
    storage.close()


def test_stream_persists_ingress_before_first_possible_event(monkeypatch, tmp_path):
    agent, storage = _runtime(monkeypatch, tmp_path)

    def fail_context(*_args, **_kwargs):
        raise RuntimeError("stream context failed")

    monkeypatch.setattr(agent, "_prepare_context", fail_context)

    async def consume() -> None:
        async for _item in agent.stream_chat("Durable streamed message"):
            raise AssertionError("nothing may be yielded before context setup")

    with pytest.raises(RuntimeError, match="stream context failed"):
        asyncio.run(consume())

    conversations = storage.list_conversations(limit=10)
    messages = storage.list_messages(conversations[0]["id"])
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Durable streamed message")
    ]
    assert messages[0]["metadata"]["ingress_status"] == "accepted"
    storage.close()


def test_successful_stream_matches_chat_raw_memory_capture(monkeypatch, tmp_path):
    agent, storage = _runtime(monkeypatch, tmp_path)
    message = "Durable streamed memory sentinel for restart recall"

    async def consume() -> None:
        async for _item in agent.stream_chat(message, mode="chat"):
            pass

    asyncio.run(consume())

    assert any(
        message in item["content"]
        for item in storage.search_memory("streamed memory sentinel", limit=10)
    )
    storage.close()


def test_failure_after_planning_keeps_ingress_accepted_without_terminal_row(
    monkeypatch, tmp_path
):
    agent, storage = _runtime(monkeypatch, tmp_path)

    async def fail_compaction(*_args, **_kwargs):
        raise RuntimeError("compaction failed after planning")

    monkeypatch.setattr(agent, "_compact_conversation_memory", fail_compaction)
    with pytest.raises(RuntimeError, match="compaction failed after planning"):
        asyncio.run(agent.chat("Message accepted before late failure"))

    conversation = storage.list_conversations(limit=1)[0]
    messages = storage.list_messages(conversation["id"])
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Message accepted before late failure")
    ]
    assert messages[0]["metadata"]["ingress_status"] == "accepted"
    assert "ingress_terminal_message_id" not in messages[0]["metadata"]
    storage.close()


def test_service_notice_is_persisted_and_idempotent(monkeypatch, tmp_path):
    agent, storage = _runtime(monkeypatch, tmp_path)
    kwargs = {
        "answer": "Service is temporarily busy",
        "source": "model_overload",
        "transport_request_id": "api:notice:42",
    }

    first = asyncio.run(agent.record_notice_turn("Remember this request", **kwargs))
    replay = asyncio.run(agent.record_notice_turn("Remember this request", **kwargs))
    messages = storage.list_messages(first.conversation_id)

    assert replay.message_id == first.message_id
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Remember this request"),
        ("assistant", "Service is temporarily busy"),
    ]
    assert messages[0]["metadata"]["ingress_status"] == "processed"
    assert messages[1]["metadata"]["source"] == "model_overload"
    storage.close()


def test_notice_then_normal_retry_replays_same_terminal_request(monkeypatch, tmp_path):
    agent, storage = _runtime(monkeypatch, tmp_path)
    request_id = "api:notice-to-normal:42"
    first = asyncio.run(
        agent.record_notice_turn(
            "Remember this request",
            answer="Service is temporarily busy",
            source="model_overload",
            transport_request_id=request_id,
        )
    )

    async def must_not_execute(*_args, **_kwargs):
        pytest.fail("a completed service notice must fence a later normal retry")

    monkeypatch.setattr(agent, "_chat_impl", must_not_execute)
    replay = asyncio.run(
        agent.chat("Remember this request", transport_request_id=request_id)
    )

    assert replay.message_id == first.message_id
    assert replay.answer == first.answer
    assert any(event.title == "Idempotent response replay" for event in replay.events)
    assert len(storage.list_messages(first.conversation_id)) == 2
    storage.close()


def test_normal_then_notice_retry_replays_normal_terminal_request(monkeypatch, tmp_path):
    agent, storage = _runtime(monkeypatch, tmp_path)
    request_id = "api:normal-to-notice:42"

    async def fake_chat_impl(message, **kwargs):
        conversation_id, user_message_id = agent._accept_chat_ingress(
            message,
            conversation_id=kwargs["conversation_id"],
            attachments=kwargs["attachments"] or [],
            mode=kwargs["mode"],
            temperature=kwargs["temperature"],
            max_tokens=kwargs["max_tokens"],
            thinking_enabled=kwargs["thinking_enabled"],
            response_modality=kwargs["response_modality"],
            request_handle=kwargs["_request_handle"],
        )
        answer = "Normal model answer"
        message_id = storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
            metadata={"source": "test-normal", "events": [], "duration_ms": 0},
        )
        agent._finalize_accepted_user_message(user_message_id, metadata={})
        return ChatResponse(
            conversation_id=conversation_id,
            message_id=message_id,
            answer=answer,
            events=[],
            duration_ms=0,
        )

    monkeypatch.setattr(agent, "_chat_impl", fake_chat_impl)
    first = asyncio.run(agent.chat("Remember this request", transport_request_id=request_id))
    replay = asyncio.run(
        agent.record_notice_turn(
            "Remember this request",
            answer="Service is temporarily busy",
            source="service_mode",
            transport_request_id=request_id,
        )
    )

    assert replay.message_id == first.message_id
    assert replay.answer == "Normal model answer"
    assert len(storage.list_messages(first.conversation_id)) == 2
    storage.close()


def test_request_id_reuse_with_indentation_change_conflicts(monkeypatch, tmp_path):
    agent, storage = _runtime(monkeypatch, tmp_path)
    request_id = "api:indentation-sensitive:42"
    asyncio.run(
        agent.record_notice_turn(
            "if ready:\n    deploy()",
            answer="Service is temporarily busy",
            source="service_mode",
            transport_request_id=request_id,
        )
    )

    with pytest.raises(ChatRequestConflictError):
        asyncio.run(
            agent.record_notice_turn(
                "if ready:\ndeploy()",
                answer="Service is temporarily busy",
                source="service_mode",
                transport_request_id=request_id,
            )
        )
    storage.close()


def test_complete_history_recall_excludes_the_current_persisted_turn(monkeypatch, tmp_path):
    agent, storage = _runtime(monkeypatch, tmp_path)
    old_conversation = storage.create_conversation("Old discussion")
    storage.add_message(
        conversation_id=old_conversation,
        role="user",
        content="Docker migration sentinel from the old discussion",
    )
    current_text = "Find my Docker migration discussion"
    current_conversation, current_message_id = agent._accept_chat_ingress(
        current_text,
        conversation_id=None,
        attachments=[],
        mode="chat",
        temperature=None,
        max_tokens=None,
        thinking_enabled=True,
        response_modality="text",
        request_handle=None,
    )
    context = agent._prepare_context(
        current_text,
        current_conversation,
        accepted_message_id=current_message_id,
    )

    asyncio.run(agent._augment_semantic_memory(context, current_text))

    assert "old discussion" in str(context.chat_history_hint)
    assert current_text not in str(context.chat_history_hint)
    storage.close()
