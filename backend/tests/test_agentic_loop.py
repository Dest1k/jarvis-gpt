from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from jarvis_gpt.agent import AgentContext, AgentRuntime, ChatUnavailableError
from jarvis_gpt.approval_executor import ApprovalExecutor
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.dispatcher import DispatcherManager
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.executive_runtime import ExecutiveCoordinator
from jarvis_gpt.experience import DEFAULT_AUTONOMY_POLICY
from jarvis_gpt.ingest import FileIngestor
from jarvis_gpt.llm import LLMStreamChunk
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import _CHAT_REQUEST_METADATA, JarvisStorage


def _result(content: str, ok: bool = True, finish_reason: str | None = None):
    raw = {"choices": [{"finish_reason": finish_reason}]} if finish_reason else None
    return type("Result", (), {"ok": ok, "content": content, "error": None, "raw": raw})()


def _execution_write_call(path, *, action_id: str, content: bytes = b"approved") -> str:
    return json.dumps(
        {
            "tool": "execution.apply",
            "arguments": {
                "payload": {
                    "protocol": "jarvis.execution.v1",
                    "action": {
                        "kind": "fs.write",
                        "action_id": action_id,
                        "path": str(path),
                        "content_base64": base64.b64encode(content).decode("ascii"),
                    },
                }
            },
        }
    )


def _agent(monkeypatch, tmp_path, llm):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, bus=EventBus())
    return agent, storage


def test_agentic_loop_runs_safe_tool_then_answers(monkeypatch, tmp_path):
    class ToolThenAnswerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result('{"tool": "web.search", "arguments": {"query": "kazan weather"}}')
            return _result("По собранным данным: в Казани ясно.")

    llm = ToolThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    # Loop mechanics test: keep the answer self-check out of the call count.
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        captured["tool"] = name
        captured["arguments"] = arguments
        return type(
            "R",
            (),
            {
                "tool": name,
                "ok": True,
                "summary": "Web search returned 1 result(s).",
                "data": {"results": [{"title": "t", "url": "u", "snippet": "clear sky"}]},
            },
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("подскажи по погоде, используй что нужно"))

    assert llm.calls == 2
    assert captured["tool"] == "web.search"
    assert captured["arguments"]["query"] == "kazan weather"
    assert response.answer == "По собранным данным: в Казани ясно."
    assert any(
        event.type == "tool_call" and event.payload.get("autonomous")
        for event in response.events
    )
    storage.close()


def test_owner_service_outage_retries_same_user_turn_without_fake_fallback(
    monkeypatch,
    tmp_path,
):
    class RecoveringLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    ok=False,
                    content="",
                    error="Qwen unavailable",
                    raw={},
                    failure_scope="service",
                )
            return _result("Готов.")

    llm = RecoveringLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    conversation_id = storage.create_conversation("Owner retry")
    kwargs = {
        "conversation_id": conversation_id,
        "transport_request_id": "telegram:owner:outage-1",
    }

    with pytest.raises(ChatUnavailableError) as raised:
        asyncio.run(agent.chat("Ответь: готов", **kwargs))

    assert raised.value.retry_scope == "service"
    assert [item["role"] for item in storage.list_messages(conversation_id)] == ["user"]

    response = asyncio.run(agent.chat("Ответь: готов", **kwargs))
    rows_after_success = storage.list_messages(conversation_id)
    replay = asyncio.run(agent.chat("Ответь: готов", **kwargs))

    assert response.answer == "Готов."
    assert replay.answer == response.answer
    assert replay.conversation_id == response.conversation_id
    assert any(event.title == "Idempotent response replay" for event in replay.events)
    assert storage.list_messages(conversation_id) == rows_after_success
    assert [item["role"] for item in rows_after_success] == ["user", "assistant"]
    assert llm.calls == 2
    storage.close()


def test_pending_request_recovers_persisted_assistant_before_active_lease_expires(
    monkeypatch,
    tmp_path,
):
    class OneShotLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            return _result("Ответ сохранён.")

    llm = OneShotLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    original_complete = agent._complete_chat_request
    original_release = agent._release_chat_request

    def crash_after_assistant(*_args, **_kwargs):
        raise SystemExit("simulated process loss")

    monkeypatch.setattr(agent, "_complete_chat_request", crash_after_assistant)
    monkeypatch.setattr(agent, "_release_chat_request", lambda _handle: None)

    with pytest.raises(SystemExit):
        asyncio.run(
            agent.chat(
                "Сохрани ответ",
                transport_request_id="telegram:owner:crash-window-1",
            )
        )

    rows_after_crash = storage.list_conversations(limit=1)
    conversation_id = rows_after_crash[0]["id"]
    history_after_crash = storage.list_messages(conversation_id)
    assert [item["role"] for item in history_after_crash] == ["user", "assistant"]

    monkeypatch.setattr(agent, "_complete_chat_request", original_complete)
    monkeypatch.setattr(agent, "_release_chat_request", original_release)
    replay = asyncio.run(
        agent.chat(
            "Сохрани ответ",
            transport_request_id="telegram:owner:crash-window-1",
        )
    )

    assert replay.answer == "Ответ сохранён."
    assert storage.list_messages(conversation_id) == history_after_crash
    assert llm.calls == 1
    storage.close()


def test_agentic_loop_reports_completed_tool_when_final_synthesis_is_down(
    monkeypatch,
    tmp_path,
):
    class ToolThenOutageLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result('{"tool":"runtime.status","arguments":{}}')
            return type(
                "Result",
                (),
                {
                    "ok": False,
                    "content": "",
                    "error": "LLM temporarily unavailable",
                    "raw": {},
                },
            )()

    llm = ToolThenOutageLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    executions: list[str] = []

    async def completed_tool(name, arguments=None, **kwargs):
        executions.append(name)
        return ToolRunResponse(
            tool=name,
            ok=True,
            summary="Runtime status was captured.",
            data={"ready": True},
        )

    monkeypatch.setattr(agent.tools, "run", completed_tool)

    response = asyncio.run(agent.chat("собери данные и ответь"))

    assert executions == ["runtime.status"]
    assert "runtime.status [effect=" in response.answer
    assert "— успешно" in response.answer
    assert "Runtime status was captured" in response.answer
    assert "автоматически не повторяю" in response.answer
    assert "offline" not in response.answer.casefold()
    assert response.events[-1].payload["source"] == "tool_fallback"
    storage.close()


def test_agentic_stream_reports_completed_tool_when_synthesis_stream_dies(
    monkeypatch,
    tmp_path,
):
    class ToolThenStreamOutageLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield LLMStreamChunk(
                    kind="delta",
                    content='{"tool":"runtime.status","arguments":{}}',
                )
                yield LLMStreamChunk(kind="done", finish_reason="stop")
                return
            yield LLMStreamChunk(kind="error", error="LLM stream unavailable")

    agent, storage = _agent(monkeypatch, tmp_path, ToolThenStreamOutageLLM())
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    executions: list[str] = []

    async def completed_tool(name, arguments=None, **kwargs):
        executions.append(name)
        return ToolRunResponse(
            tool=name,
            ok=True,
            summary="Runtime status was captured.",
            data={"ready": True},
        )

    monkeypatch.setattr(agent.tools, "run", completed_tool)

    async def collect():
        deltas = []
        done = None
        async for item in agent.stream_chat("собери данные и ответь"):
            if item["type"] == "delta":
                deltas.append(item["content"])
            elif item["type"] == "done":
                done = item
        return "".join(deltas), done

    visible, done = asyncio.run(collect())

    assert executions == ["runtime.status"]
    assert "runtime.status [effect=" in visible
    assert "автоматически не повторяю" in visible
    assert "offline" not in visible.casefold()
    assert done is not None
    assert done["events"][-1]["payload"]["source"] == "tool_fallback"
    assert done["events"][-1]["payload"]["finish_reason"] == "synthesis_error"
    storage.close()


def test_stream_service_outage_without_output_is_structured_and_saves_no_assistant(
    monkeypatch,
    tmp_path,
):
    class OutageStreamLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            return _result("unused")

        async def stream_complete(
            self, messages, *, temperature=None, max_tokens=None, **kwargs
        ):
            yield LLMStreamChunk(
                kind="error",
                error="Qwen stream unavailable",
                failure_scope="service",
            )

    agent, storage = _agent(monkeypatch, tmp_path, OutageStreamLLM())
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    conversation_id = storage.create_conversation("Stream outage")

    async def collect():
        return [
            item
            async for item in agent.stream_chat(
                "Ответь кратко",
                conversation_id=conversation_id,
                transport_request_id="stream:owner:outage-1",
            )
        ]

    items = asyncio.run(collect())

    assert items[-1] == {
        "type": "error",
        "error": "Qwen stream unavailable",
        "failure_scope": "service",
        "retry_class": "llm-outage",
    }
    assert not any(item.get("type") == "done" for item in items)
    assert [item["role"] for item in storage.list_messages(conversation_id)] == ["user"]
    storage.close()


def test_cancelled_partial_stream_is_not_replayed_as_terminal_answer(
    monkeypatch,
    tmp_path,
):
    class RecoveringQwenStream:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_complete(
            self, messages, *, temperature=None, max_tokens=None, **kwargs
        ):
            self.calls += 1
            content = "cut partial" if self.calls == 1 else "complete terminal answer"
            yield LLMStreamChunk(kind="delta", content=content)
            yield LLMStreamChunk(kind="done", finish_reason="stop")

    llm = RecoveringQwenStream()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    request_id = "stream:owner:cancelled-1"

    async def scenario():
        first_stream = agent.stream_chat(
            "answer once",
            transport_request_id=request_id,
        )
        conversation_id = ""
        partial = ""
        while True:
            item = await anext(first_stream)
            if item.get("type") == "meta":
                conversation_id = str(item.get("conversation_id") or "")
            if item.get("type") == "delta":
                partial += str(item.get("content") or "")
                break
        assert _CHAT_REQUEST_METADATA.get() is None
        await first_stream.aclose()
        retried = [
            item
            async for item in agent.stream_chat(
                "answer once",
                transport_request_id=request_id,
            )
        ]
        return conversation_id, partial, retried

    conversation_id, partial, retried = asyncio.run(scenario())
    done = next(item for item in retried if item.get("type") == "done")
    history = storage.list_messages(conversation_id)

    assert partial == "cut partial"
    assert done["answer"] == "complete terminal answer"
    assert done.get("source") != "idempotent_response_replay"
    assert llm.calls == 2
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[-1]["metadata"]["chat_request_terminal"] is True
    storage.close()


def test_partial_qwen_stream_error_retries_instead_of_completing_partial(
    monkeypatch,
    tmp_path,
):
    class RecoveringQwenStream:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_complete(
            self, messages, *, temperature=None, max_tokens=None, **kwargs
        ):
            self.calls += 1
            if self.calls == 1:
                yield LLMStreamChunk(kind="delta", content="cut partial")
                yield LLMStreamChunk(
                    kind="error",
                    error="Qwen stream unavailable",
                    failure_scope="service",
                )
                return
            yield LLMStreamChunk(kind="delta", content="complete terminal answer")
            yield LLMStreamChunk(kind="done", finish_reason="stop")

    llm = RecoveringQwenStream()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    request_id = "stream:owner:partial-error-1"

    async def collect():
        first = [
            item
            async for item in agent.stream_chat(
                "answer once",
                transport_request_id=request_id,
            )
        ]
        second = [
            item
            async for item in agent.stream_chat(
                "answer once",
                transport_request_id=request_id,
            )
        ]
        return first, second

    first, second = asyncio.run(collect())
    conversation_id = next(
        item["conversation_id"] for item in first if item.get("type") == "meta"
    )
    history = storage.list_messages(conversation_id)

    assert first[-1]["type"] == "error"
    assert first[-1]["retry_class"] == "llm-outage"
    assert not any(item.get("type") == "done" for item in first)
    assert next(item for item in second if item.get("type") == "done")["answer"] == (
        "complete terminal answer"
    )
    assert llm.calls == 2
    assert [item["role"] for item in history] == ["user", "assistant"]
    storage.close()


def test_agentic_recovery_does_not_leak_failed_tool_error(monkeypatch, tmp_path):
    """A read-only tool rejected by the runtime must never surface its raw error string
    or internal effect id in the recovery answer. The weak model can fumble a system.inspect
    call (invalid WMI class) repeatedly; the honest-fallback answer must report a failure
    without echoing 'WMI class name contains unsupported characters.' + a hash to chat."""

    class ToolThenOutageLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(
                    '{"tool":"system.inspect","arguments":{"action":"hardware.memory"}}'
                )
            return type(
                "Result",
                (),
                {"ok": False, "content": "", "error": "LLM temporarily unavailable", "raw": {}},
            )()

    agent, storage = _agent(monkeypatch, tmp_path, ToolThenOutageLLM())
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})

    async def rejected_tool(name, arguments=None, **kwargs):
        return ToolRunResponse(
            tool=name,
            ok=False,
            summary="WMI class name contains unsupported characters.",
            data={},
        )

    monkeypatch.setattr(agent.tools, "run", rejected_tool)

    response = asyncio.run(agent.chat("собери данные и ответь"))

    assert "effect=" not in response.answer
    assert "unsupported characters" not in response.answer
    assert "WMI class name" not in response.answer
    # still honest about the failure / non-completion
    assert "завершилась ошибкой" in response.answer
    assert "автоматически не повторяю" in response.answer
    assert "offline" not in response.answer.casefold()
    storage.close()


def test_fuzzy_resolve_tool_name(monkeypatch, tmp_path):
    class _IdleLLM:
        async def complete(self, *a, **k):
            return _result("ok")

    agent, storage = _agent(monkeypatch, tmp_path, _IdleLLM())
    resolve = agent._fuzzy_resolve_tool_name
    allowed = {
        "filesystem.find",
        "filesystem.write_text",
        "web.search",
        "documents.generate",
    }
    # Exact separator/case fold → the identically-named tool (accepted at any danger
    # level; the normal authorization/approval gates still apply downstream).
    assert resolve("filesystem_find", allowed) == "filesystem.find"
    assert resolve("filesystem_write_text", allowed) == "filesystem.write_text"
    # High-confidence fuzzy match onto a SAFE (read-only) tool.
    assert resolve("filesystm.find", allowed) == "filesystem.find"
    # A fuzzy GUESS must never resolve onto a mutating (non-safe) tool.
    assert resolve("filesystem.write_txt", allowed) is None
    # Genuinely unknown / too short → keep today's fail-closed reject.
    assert resolve("teleport", allowed) is None
    assert resolve("ab", allowed) is None
    storage.close()


def test_streaming_gate_resynthesizes_raw_tool_dump(monkeypatch, tmp_path):
    # The live streaming loop must not emit a pasted raw tool result verbatim: it buffers
    # the final answer turn and forces one clean synthesis (parity with the non-stream gate).
    raw_dump = (
        '{"root":"D:/x","matches":[{"path":"a.py","line":3}],'
        '"truncated":false,"files_scanned":12}'
    )

    class ToolThenRawDumpLLM:
        def __init__(self) -> None:
            self.stream_calls = 0
            self.complete_calls = 0

        async def stream_complete(
            self, messages, *, temperature=None, max_tokens=None, **kwargs
        ):
            self.stream_calls += 1
            if self.stream_calls == 1:
                yield LLMStreamChunk(
                    kind="delta",
                    content='{"tool":"filesystem.find","arguments":{"query":"TODO"}}',
                )
                yield LLMStreamChunk(kind="done", finish_reason="stop")
                return
            # Round 2: the weak model pastes the raw tool result JSON as its "answer".
            yield LLMStreamChunk(kind="delta", content=raw_dump)
            yield LLMStreamChunk(kind="done", finish_reason="stop")

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            # The synthesis gate re-asks the model for a clean natural-language answer.
            self.complete_calls += 1
            return _result("Нашёл 1 совпадение: a.py, строка 3.")

    llm = ToolThenRawDumpLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})

    async def fake_run(name, arguments=None, **kwargs):
        return ToolRunResponse(
            tool=name,
            ok=True,
            summary="Found 1 match(es) across 12 file(s).",
            data={
                "root": "D:/x",
                "matches": [{"path": "a.py", "line": 3}],
                "files_scanned": 12,
            },
        )

    monkeypatch.setattr(agent.tools, "run", fake_run)

    async def collect():
        deltas = []
        async for item in agent.stream_chat("найди TODO в папке D:\\x"):
            if item["type"] == "delta":
                deltas.append(item["content"])
        return "".join(deltas)

    visible = asyncio.run(collect())

    assert llm.complete_calls >= 1  # the synthesis gate fired
    assert "Нашёл 1 совпадение" in visible
    assert raw_dump not in visible
    storage.close()


def test_agentic_recovery_labels_ambiguous_tool_outcome(monkeypatch, tmp_path):
    class ToolThenOutageLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result('{"tool":"runtime.status","arguments":{}}')
            return _result("", ok=False)

    agent, storage = _agent(monkeypatch, tmp_path, ToolThenOutageLLM())
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})

    async def uncertain_tool(name, arguments=None, **kwargs):
        return ToolRunResponse(
            tool=name,
            ok=False,
            summary="Connection disappeared after dispatch.",
            data={
                "failure": {
                    "protocol": "jarvis.tool-failure.v1",
                    "outcome": "unknown",
                    "outcome_known": False,
                    "retryable": False,
                }
            },
        )

    monkeypatch.setattr(agent.tools, "run", uncertain_tool)

    response = asyncio.run(agent.chat("собери данные и ответь"))

    assert "исход неизвестен" in response.answer
    assert "сверка состояния" in response.answer
    assert "автоматически не повторяю" in response.answer
    storage.close()


def test_agentic_loop_corrects_mixed_tool_payload_before_execution(monkeypatch, tmp_path):
    class MixedThenCorrectToolLLM:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return _result(
                    'Сейчас проверю.\n{"tool":"runtime.status","arguments":{}}'
                )
            if len(self.calls) == 2:
                return _result('{"tool":"runtime.status","arguments":{}}')
            return _result("Рантайм проверен и работает.")

    llm = MixedThenCorrectToolLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    runs = []

    async def fake_run(name, arguments=None, **kwargs):
        runs.append((name, arguments))
        return type(
            "R",
            (),
            {"tool": name, "ok": True, "summary": "runtime ok", "data": {"ready": True}},
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("собери данные и ответь"))

    assert response.answer == "Рантайм проверен и работает."
    assert runs == [("runtime.status", {})]
    correction_system = "\n".join(
        item["content"] for item in llm.calls[1] if item["role"] == "system"
    )
    assert "Внутренняя ошибка протокола" in correction_system
    assert '"tool"' not in response.answer
    storage.close()


def test_agentic_loop_never_returns_repeated_malformed_tool_payload(monkeypatch, tmp_path):
    class MalformedToolLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            return _result('```json\n{"tool":"runtime.status","arguments":\n```')

    llm = MalformedToolLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})

    async def forbidden_run(name, arguments=None, **kwargs):
        raise AssertionError(f"malformed tool payload reached {name}")

    monkeypatch.setattr(agent.tools, "run", forbidden_run)

    response = asyncio.run(agent.chat("собери данные и ответь"))

    assert llm.calls == 2
    assert "Не удалось безопасно завершить запрос" in response.answer
    assert '"tool"' not in response.answer
    assert "runtime.status" not in response.answer
    storage.close()


def test_agentic_loop_recalls_persisted_document_then_summarizes(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    monkeypatch.setenv("JARVIS_EMBEDDINGS_ENABLED", "0")

    class RecallThenAnswerLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.observation = ""

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            rendered = "\n".join(item["content"] for item in messages)
            assert "documents.recall(query?" in rendered
            self.observation = rendered
            return _result(
                "Phoenix готов к выпуску; перед релизом нужно проверить резервную копию."
            )

    llm = RecallThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    source = tmp_path / "phoenix-report.txt"
    source.write_text(
        "Phoenix release is ready. Required before launch: validate the backup.",
        encoding="utf-8",
    )
    ingested = FileIngestor(agent.settings, storage).ingest_path(source)

    response = asyncio.run(
        agent.chat("Дай резюме сохраненного документа Phoenix")
    )

    assert llm.calls == 1
    assert "validate the backup" in llm.observation
    assert "untrusted document/file evidence" in llm.observation
    assert "резервную копию" in response.answer
    assert any(
        event.type == "tool_call"
        and event.payload.get("tool") == "documents.recall"
        and event.payload.get("ok") is True
        and event.payload.get("prefetch") is True
        for event in response.events
    )
    assert storage.get_file(ingested["file"]["id"])["status"] == "indexed"
    storage.close()


def test_agentic_loop_learns_persona_insight_from_dialogue(monkeypatch, tmp_path):
    # The operator reveals a durable fact in passing; the model saves it through
    # the real persona.insight tool (no monkeypatched registry) so future turns
    # see it in the persona block. This is the reasoning-first replacement for
    # regex persona extraction.
    from jarvis_gpt.persona import load_persona

    class InsightThenAnswerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(
                    '{"tool": "persona.insight", '
                    '"arguments": {"field": "tech_stack", "value": "Proxmox"}}'
                )
            return _result("Запомнил: Proxmox теперь часть твоего стека.")

    llm = InsightThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    # Persona-learning test: keep the answer self-check out of the call count.
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})

    response = asyncio.run(agent.chat("кстати, я перевёл домашний кластер на Proxmox"))

    assert llm.calls == 2
    assert "Proxmox" in response.answer
    persona = load_persona(storage)
    assert "Proxmox" in persona["tech_stack"]
    assert any(
        event.type == "tool_call" and event.payload.get("tool") == "persona.insight"
        for event in response.events
    )
    audit_actions = {item["action"] for item in storage.list_audit(limit=20)}
    assert "persona.insight" in audit_actions
    storage.close()


def test_agentic_loop_inspects_system_without_the_word_wmi(monkeypatch, tmp_path):
    # An everyday phrasing with no "wmi"/"cim" keyword: the deterministic native
    # heuristics do not fire, so the model itself reaches for the safe
    # system.inspect tool and picks the WMI class from its own understanding.
    class InspectThenAnswerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(
                    '{"tool": "system.inspect", "arguments": {"action": "wmi.query", '
                    '"payload": {"class_name": "Win32_Battery", '
                    '"properties": ["EstimatedChargeRemaining"]}}}'
                )
            return _result("Заряд батареи: 87%.")

    llm = InspectThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        captured["tool"] = name
        captured["arguments"] = arguments
        return type(
            "R",
            (),
            {
                "tool": name,
                "ok": True,
                "summary": "Battery 87%",
                "data": {"action": "wmi.query"},
            },
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("сколько заряда осталось на ноуте?"))

    assert llm.calls == 2
    assert captured["tool"] == "system.inspect"
    assert captured["arguments"]["payload"]["class_name"] == "Win32_Battery"
    assert "87%" in response.answer
    storage.close()


def test_agentic_answer_auto_continues_after_length_finish(monkeypatch, tmp_path):
    class LengthThenDoneLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result("Первая часть", finish_reason="length")
            return _result("и нормальный финал.", finish_reason="stop")

    llm = LengthThenDoneLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)

    response = asyncio.run(agent.chat("Объясни устройство локального runtime", mode="chat"))

    assert llm.calls == 2
    assert "Первая часть" in response.answer
    assert "нормальный финал" in response.answer
    assert "лимиту" not in response.answer
    done = [event for event in response.events if event.type == "assistant_done"][-1]
    assert done.payload["continuations"] == 1
    storage.close()


def test_agentic_loop_gates_dangerous_tool_with_approval(monkeypatch, tmp_path):
    target = tmp_path / "agentic-approved.txt"
    tool_call = _execution_write_call(target, action_id="agentic-approved-write")

    class DangerThenAnswerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(tool_call)
            return _result("Нужно ваше подтверждение, чтобы выполнить команду на хосте.")

    llm = DangerThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value(
        "experience.autonomy_policy",
        {
            "allow_danger_tools": True,
            "approval_required_for": [],
            "verify_answers": False,
        },
    )

    async def fail_run(name, arguments=None, **kwargs):
        raise AssertionError(f"dangerous tool {name} must not run autonomously")

    monkeypatch.setattr(agent.tools, "run", fail_run)

    response = asyncio.run(agent.chat("посмотри дату на хосте"))

    assert llm.calls == 2
    assert response.answer.startswith("Нужно ваше подтверждение")
    pending = storage.list_approvals(limit=10, status="pending")
    assert len(pending) == 1
    assert pending[0]["requested_action"] == "tool.run"
    assert pending[0]["risk"] == "danger"
    assert pending[0]["payload"]["tool"] == "execution.apply"
    assert pending[0]["payload"]["arguments"]["payload"]["protocol"] == "jarvis.execution.v1"
    assert not target.exists()
    assert any(event.type == "approval" for event in response.events)

    # Losing the first HTTP response cannot mint a sibling approval: the
    # normally synthesized awaiting-approval answer is durably replayed.
    retry = asyncio.run(
        agent.chat("посмотри дату на хосте", conversation_id=response.conversation_id)
    )
    assert retry.answer == response.answer
    assert len(storage.list_approvals(limit=10, status="pending")) == 1
    assert any(event.title == "Idempotent response replay" for event in retry.events)
    ledger = storage.list_runtime_values(prefix="agent.operator_effect.")[0]["value"]
    assert next(iter(ledger["requests"].values()))["status"] == "completed"
    storage.close()


def test_autonomy_policy_controls_proposals_without_granting_execution(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path, object())

    storage.set_runtime_value(
        "experience.autonomy_policy",
        {
            "allow_safe_tools": True,
            "allow_review_tools": False,
            "allow_danger_tools": False,
        },
    )
    safe_only = {tool.name for tool in agent._autonomous_tools()}
    assert "runtime.status" in safe_only
    assert "browser.open" not in safe_only
    assert "browser.open_many" not in safe_only
    assert "execution.apply" not in safe_only

    storage.set_runtime_value(
        "experience.autonomy_policy",
        {
            "allow_safe_tools": False,
            "allow_review_tools": True,
            "allow_danger_tools": True,
        },
    )
    gated_proposals = {tool.name for tool in agent._autonomous_tools()}
    assert "runtime.status" not in gated_proposals
    assert "browser.open" in gated_proposals
    assert "browser.open_many" in gated_proposals
    assert "execution.apply" in gated_proposals
    assert agent.tools.get("browser.open").danger_level == "review"
    assert agent.tools.get("browser.open_many").danger_level == "review"
    assert agent.tools.get("execution.apply").danger_level == "danger"
    storage.close()


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "documents.archive.create",
            {
                "paths": ["/tmp/source.txt"],
                "archive_format": "zip",
                "output_name": "exact.zip",
            },
        ),
        (
            "documents.archive.extract",
            {"path": "/tmp/exact.zip", "output_name": "exact-extracted"},
        ),
    ],
)
def test_safe_document_mutator_is_still_claimed_and_approval_gated(
    monkeypatch, tmp_path, tool_name, arguments
):
    agent, storage = _agent(monkeypatch, tmp_path, object())
    conversation_id = storage.create_conversation("durable mutator gate")
    prompt = "Create archive exact.zip from /tmp/source.txt"
    context = AgentContext(conversation_id=conversation_id, memory_hits=[], file_hits=[])
    context.operator_message = prompt
    context.side_effects_admitted = True
    agent._bind_operator_request_identity(
        context,
        message=prompt,
        mode="auto",
        attachments=[],
    )
    context.operator_message_id = storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content=prompt,
    )

    async def must_not_run(name, arguments=None, **kwargs):
        raise AssertionError(f"safe durable mutator {name} ran without a claim/gate")

    monkeypatch.setattr(agent.tools, "run", must_not_run)
    _observation, event, executed = asyncio.run(
        agent._run_agentic_tool(
            tool_name,
            arguments,
            {tool_name},
            context,
        )
    )

    assert executed is None
    assert event.type == "approval"
    pending = storage.list_approvals(limit=10, status="pending")
    assert len(pending) == 1
    assert pending[0]["payload"]["operator_effect_key"]
    ledger = storage.list_runtime_values(prefix="agent.operator_effect.")[0]["value"]
    request = next(iter(ledger["requests"].values()))
    assert next(iter(request["effects"].values()))["tool"] == "approval.create"
    storage.close()


def test_agentic_policy_required_tool_never_uses_current_turn_authority(
    monkeypatch,
    tmp_path,
):
    target = tmp_path / "policy-gated.txt"
    tool_call = _execution_write_call(
        target,
        action_id="policy-gated-write",
        content=b"policy gated",
    )

    class ExplicitWriteLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            if any("Ты intent-router" in item["content"] for item in messages):
                return _result(
                    '{"route":"local_action","confidence":0.99,'
                    '"rationale":"explicit file write"}'
                )
            self.calls += 1
            if self.calls == 1:
                return _result(tool_call)
            return _result("Операция подготовлена и ждёт подтверждения.")

    agent, storage = _agent(monkeypatch, tmp_path, ExplicitWriteLLM())
    storage.set_runtime_value(
        "experience.autonomy_policy",
        {
            "approval_required_for": ["execution.apply"],
            "verify_answers": False,
        },
    )

    response = asyncio.run(agent.chat(f"Создай файл {target} и запиши policy gated"))

    pending = storage.list_approvals(limit=10, status="pending")
    assert len(pending) == 1
    assert pending[0]["payload"]["tool"] == "execution.apply"
    assert not target.exists()
    approval_event = next(event for event in response.events if event.type == "approval")
    assert approval_event.payload["policy_approval_required"] is True
    storage.close()


def test_explicit_current_turn_write_executes_without_approval(monkeypatch, tmp_path):
    target = tmp_path / "operator-created.txt"
    content = b"approved current turn"
    tool_call = _execution_write_call(
        target,
        action_id="operator-write",
        content=content,
    )

    class ExplicitWriteLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            if any("Ты intent-router" in item["content"] for item in messages):
                return _result(
                    '{"route":"local_action","confidence":0.99,'
                    '"rationale":"explicit file write"}'
                )
            self.calls += 1
            if self.calls == 1:
                return _result(tool_call)
            return _result("Готово: файл записан.")

    llm = ExplicitWriteLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value(
        "experience.autonomy_policy",
        {"approval_required_for": [], "verify_answers": False},
    )

    response = asyncio.run(
        agent.chat(f"Создай файл {target} и запиши approved current turn")
    )

    assert target.read_bytes() == content
    assert storage.list_approvals(limit=10, status="pending") == []
    run = next(run for run in storage.list_tool_runs() if run["tool"] == "execution.apply")
    assert run["ok"] is True
    event = next(event for event in response.events if event.payload.get("operator_requested"))
    assert event.payload["authority"] == "operator_turn"
    storage.close()


def test_equivalent_operator_effect_runs_only_once_per_turn(monkeypatch, tmp_path):
    class DuplicateBrowserEffectLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            if any("Ты intent-router" in item["content"] for item in messages):
                return _result(
                    '{"route":"local_action","confidence":0.99,'
                    '"rationale":"explicit browser click"}'
                )
            self.calls += 1
            if self.calls == 1:
                return _result(
                    '{"tool":"browser.click","arguments":'
                    '{"url":"https://example.com","target":"Search"}}'
                )
            if self.calls == 2:
                return _result(
                    '{"tool":"browser.click","arguments":'
                    '{"url":"https://www.EXAMPLE.com/","target":"Search",'
                    '"wait_ms":5000,"debug_url":"http://127.0.0.1:9222"}}'
                )
            return _result("Клик выполнен один раз.")

    agent, storage = _agent(monkeypatch, tmp_path, DuplicateBrowserEffectLLM())
    storage.set_runtime_value(
        "experience.autonomy_policy",
        {
            "allow_review_tools": True,
            "approval_required_for": [],
            "verify_answers": False,
        },
    )
    runs: list[tuple[str, dict]] = []

    async def fake_run(name, arguments=None, **kwargs):
        runs.append((name, dict(arguments or {})))
        return ToolRunResponse(tool=name, ok=True, summary="clicked", data={})

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("Click Search at https://example.com"))

    assert [name for name, _args in runs] == ["browser.click"]
    assert response.answer == "Клик выполнен один раз."
    assert storage.list_approvals(limit=10, status="pending") == []
    assert any(event.title == "Duplicate effect skipped" for event in response.events)
    storage.close()


def test_unfinished_operator_effect_is_not_replayed_by_new_message_id(
    monkeypatch,
    tmp_path,
):
    class ToolThenOutageLLM:
        def __init__(self) -> None:
            self.round_number = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            if any("Ты intent-router" in item["content"] for item in messages):
                return _result(
                    '{"route":"local_action","confidence":0.99,'
                    '"rationale":"explicit browser click"}'
                )
            self.round_number += 1
            if self.round_number == 1:
                return _result(
                    '{"tool":"browser.click","arguments":'
                    '{"url":"https://example.com","target":"Search"}}'
                )
            return type(
                "Result",
                (),
                {
                    "ok": False,
                    "content": "",
                    "error": "synthesis unavailable",
                    "raw": {},
                },
            )()

    class ToolThenAnswerLLM:
        def __init__(self) -> None:
            self.round_number = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            if any("Ты intent-router" in item["content"] for item in messages):
                return _result(
                    '{"route":"local_action","confidence":0.99,'
                    '"rationale":"explicit browser click"}'
                )
            self.round_number += 1
            if self.round_number == 1:
                return _result(
                    '{"tool":"browser.click","arguments":'
                    '{"url":"https://example.com","target":"Search"}}'
                )
            return _result("Запрос обработан без повторного клика.")

    llm = ToolThenOutageLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value(
        "experience.autonomy_policy",
        {
            "allow_review_tools": True,
            "approval_required_for": [],
            "verify_answers": False,
        },
    )
    runs: list[tuple[str, dict]] = []

    async def fake_run(name, arguments=None, **kwargs):
        runs.append((name, dict(arguments or {})))
        return ToolRunResponse(tool=name, ok=True, summary="clicked", data={})

    monkeypatch.setattr(agent.tools, "run", fake_run)
    message = "Click Search at https://example.com"

    first = asyncio.run(agent.chat(message))
    agent.llm = ToolThenAnswerLLM()
    second = asyncio.run(agent.chat(message, conversation_id=first.conversation_id))

    assert [name for name, _args in runs] == ["browser.click"]
    assert first.events[-1].payload["source"] == "tool_fallback"
    assert second.answer == first.answer
    ledger = storage.list_runtime_values(prefix="agent.operator_effect.")[0]["value"]
    assert next(iter(ledger["requests"].values()))["status"] == "completed"

    # A restated command is a deliberate new operator request and gets a fresh
    # effect claim even though the canonical tool effect is identical.
    agent.llm = ToolThenAnswerLLM()
    third = asyncio.run(
        agent.chat(
            "Again, click Search at https://example.com",
            conversation_id=first.conversation_id,
        )
    )
    assert [name for name, _args in runs] == ["browser.click", "browser.click"]
    assert not any(
        event.title == "Durable duplicate effect skipped" for event in third.events
    )

    # Even after successful synthesis, an exact immediate retry may mean the
    # HTTP response was lost.  The bounded completed-request fence still wins.
    agent.llm = ToolThenAnswerLLM()
    fourth = asyncio.run(
        agent.chat(
            "Again, click Search at https://example.com",
            conversation_id=first.conversation_id,
        )
    )
    assert [name for name, _args in runs] == ["browser.click", "browser.click"]
    assert any(
        event.title == "Idempotent response replay" for event in fourth.events
    )
    assert fourth.answer == third.answer
    storage.close()


def test_operator_effect_claim_survives_process_crash_before_tool_return(
    monkeypatch,
    tmp_path,
):
    class ToolCallLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            if any("Ты intent-router" in item["content"] for item in messages):
                return _result(
                    '{"route":"local_action","confidence":0.99,'
                    '"rationale":"explicit browser click"}'
                )
            if any("skipped" in item["content"] for item in messages if item["role"] == "user"):
                return _result("Предыдущий эффект не отправлен повторно; нужна сверка.")
            return _result(
                '{"tool":"browser.click","arguments":'
                '{"url":"https://example.com","target":"Search"}}'
            )

    agent, storage = _agent(monkeypatch, tmp_path, ToolCallLLM())
    storage.set_runtime_value(
        "experience.autonomy_policy",
        {
            "allow_review_tools": True,
            "approval_required_for": [],
            "verify_answers": False,
        },
    )
    attempts: list[str] = []

    async def crash_after_dispatch(name, arguments=None, **kwargs):
        attempts.append(name)
        raise RuntimeError("synthetic process crash after dispatch")

    monkeypatch.setattr(agent.tools, "run", crash_after_dispatch)
    message = "Click Search at https://example.com"

    with pytest.raises(RuntimeError, match="synthetic process crash"):
        asyncio.run(agent.chat(message))

    conversation_id = storage.list_conversations(limit=1)[0]["id"]
    restarted = AgentRuntime(
        settings=agent.settings,
        storage=storage,
        llm=ToolCallLLM(),
        bus=EventBus(),
    )

    async def must_not_run(name, arguments=None, **kwargs):
        attempts.append(name)
        return ToolRunResponse(tool=name, ok=True, summary="unexpected", data={})

    monkeypatch.setattr(restarted.tools, "run", must_not_run)
    response = asyncio.run(restarted.chat(message, conversation_id=conversation_id))

    assert attempts == ["browser.click"]
    assert any(
        event.title == "Durable duplicate effect skipped" for event in response.events
    )
    persisted = storage.list_runtime_values(prefix="agent.operator_effect.")
    assert len(persisted) == 1
    requests = persisted[0]["value"]["requests"]
    assert len(requests) == 1
    assert next(iter(requests.values()))["status"] == "incomplete"
    storage.close()


def test_completed_operator_turn_fences_exact_http_response_retry(
    monkeypatch,
    tmp_path,
):
    class ToolThenAnswerLLM:
        def __init__(self) -> None:
            self.round_number = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            if any("Ты intent-router" in item["content"] for item in messages):
                return _result(
                    '{"route":"local_action","confidence":0.99,'
                    '"rationale":"explicit browser click"}'
                )
            self.round_number += 1
            if self.round_number == 1:
                return _result(
                    '{"tool":"browser.click","arguments":'
                    '{"url":"https://example.com","target":"Search"}}'
                )
            return _result("Клик обработан.")

    agent, storage = _agent(monkeypatch, tmp_path, ToolThenAnswerLLM())
    storage.set_runtime_value(
        "experience.autonomy_policy",
        {
            "allow_review_tools": True,
            "approval_required_for": [],
            "verify_answers": False,
        },
    )
    runs: list[str] = []

    async def fake_run(name, arguments=None, **kwargs):
        runs.append(name)
        return ToolRunResponse(tool=name, ok=True, summary="clicked", data={})

    monkeypatch.setattr(agent.tools, "run", fake_run)
    message = "Click Search at https://example.com"

    # Treat the first successful response as lost by the HTTP client.
    first = asyncio.run(agent.chat(message, transport_request_id="telegram:default:1"))
    ledger_item = storage.list_runtime_values(prefix="agent.operator_effect.")[0]
    ledger = ledger_item["value"]
    request_state = next(iter(ledger["requests"].values()))
    # Telegram may retain a machine-classified outage for 24 hours. A response
    # lost before that outage must remain fenced well beyond the former 20-minute TTL.
    request_state["completed_at"] = (
        datetime.now(UTC) - timedelta(hours=23)
    ).isoformat()
    request_state["updated_at"] = request_state["completed_at"]
    storage.set_runtime_value(ledger_item["key"], ledger)
    agent.llm = ToolThenAnswerLLM()
    retry = asyncio.run(
        agent.chat(
            message,
            conversation_id=first.conversation_id,
            transport_request_id="telegram:default:1",
        )
    )

    assert runs == ["browser.click"]
    # Transport replay keeps the original answer and does not re-run tools; it
    # surfaces an explicit idempotent marker event (may prepend to the original
    # event list, so full ChatResponse equality is not required).
    assert retry.answer == first.answer
    assert retry.conversation_id == first.conversation_id
    assert any(event.title == "Idempotent response replay" for event in retry.events)

    # The same words in a different Telegram update are a deliberate new turn.
    agent.llm = ToolThenAnswerLLM()
    new_update = asyncio.run(
        agent.chat(
            message,
            conversation_id=first.conversation_id,
            transport_request_id="telegram:default:2",
        )
    )
    assert runs == ["browser.click", "browser.click"]
    assert not any(event.title == "Idempotent response replay" for event in new_update.events)

    # Explicitly restating the intent creates a new digest and is not suppressed.
    agent.llm = ToolThenAnswerLLM()
    repeated = asyncio.run(
        agent.chat(
            "Again, click Search at https://example.com",
            conversation_id=first.conversation_id,
        )
    )
    assert runs == ["browser.click", "browser.click", "browser.click"]
    assert not any(
        event.title == "Durable duplicate effect skipped" for event in repeated.events
    )
    storage.close()


def test_safe_reminder_mutation_fences_exact_http_response_retry(
    monkeypatch,
    tmp_path,
):
    class ReminderThenAnswerLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            if any("Ты intent-router" in item["content"] for item in messages):
                return _result(
                    '{"route":"local_action","confidence":0.99,'
                    '"rationale":"explicit reminder"}'
                )
            if any(
                "observation[reminders.create" in item["content"]
                for item in messages
                if item["role"] == "user"
            ):
                return _result("Напоминание создано.")
            return _result(
                '{"tool":"reminders.create","arguments":'
                '{"text":"проверить бэкап","when":"завтра в 10"}}'
            )

    agent, storage = _agent(monkeypatch, tmp_path, ReminderThenAnswerLLM())
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    original_run = agent.tools.run
    runs: list[str] = []

    async def counted_run(name, arguments=None, **kwargs):
        runs.append(name)
        return await original_run(name, arguments, **kwargs)

    monkeypatch.setattr(agent.tools, "run", counted_run)
    message = "Напомни завтра в 10 проверить бэкап"
    first = asyncio.run(
        agent.chat(message, transport_request_id="telegram:700001:reminder-1")
    )

    # Simulate loss of the successful HTTP response and an exact Telegram retry.
    retry = asyncio.run(
        agent.chat(
            message,
            conversation_id=first.conversation_id,
            transport_request_id="telegram:700001:reminder-1",
        )
    )

    assert runs == ["reminders.create"]
    assert len(storage.list_reminders(status="pending", limit=1000)) == 1
    assert retry.answer == first.answer
    assert retry.conversation_id == first.conversation_id
    assert any(event.title == "Idempotent response replay" for event in retry.events)

    asyncio.run(
        agent.chat(
            message,
            conversation_id=first.conversation_id,
            transport_request_id="telegram:700001:reminder-2",
        )
    )
    assert runs == ["reminders.create", "reminders.create"]
    assert len(storage.list_reminders(status="pending", limit=1000)) == 2
    storage.close()


def test_stream_completed_operator_turn_replays_exact_cached_answer(
    monkeypatch,
    tmp_path,
):
    class StreamToolThenAnswerLLM:
        def __init__(self) -> None:
            self.round_number = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            return _result(
                '{"route":"local_action","confidence":0.99,'
                '"rationale":"explicit browser click"}'
            )

        async def stream_complete(
            self,
            messages,
            *,
            temperature=None,
            max_tokens=None,
            **kwargs,
        ):
            self.round_number += 1
            if self.round_number == 1:
                yield LLMStreamChunk(
                    kind="delta",
                    content=(
                        '{"tool":"browser.click","arguments":'
                        '{"url":"https://example.com","target":"Search"}}'
                    ),
                )
            else:
                yield LLMStreamChunk(kind="delta", content="Точный потоковый итог.")
            yield LLMStreamChunk(kind="done", finish_reason="stop")

    agent, storage = _agent(monkeypatch, tmp_path, StreamToolThenAnswerLLM())
    storage.set_runtime_value(
        "experience.autonomy_policy",
        {
            "allow_review_tools": True,
            "approval_required_for": [],
            "verify_answers": False,
        },
    )
    runs: list[str] = []

    async def fake_run(name, arguments=None, **kwargs):
        runs.append(name)
        return ToolRunResponse(tool=name, ok=True, summary="clicked", data={})

    monkeypatch.setattr(agent.tools, "run", fake_run)
    message = "Click Search at https://example.com"

    async def collect(conversation_id=None):
        return [
            item
            async for item in agent.stream_chat(message, conversation_id=conversation_id)
        ]

    first = asyncio.run(collect())
    first_done = next(item for item in first if item["type"] == "done")
    agent.llm = StreamToolThenAnswerLLM()
    retry = asyncio.run(collect(first_done["conversation_id"]))
    retry_done = next(item for item in retry if item["type"] == "done")

    assert runs == ["browser.click"]
    assert retry_done["answer"] == first_done["answer"] == "Точный потоковый итог."
    assert any(
        item.get("event", {}).get("title") == "Idempotent response replay"
        for item in retry
        if item["type"] == "event"
    )
    storage.close()


def test_agentic_tool_step_budget_honors_policy_up_to_twenty_four(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path, object())

    storage.set_runtime_value(
        "experience.autonomy_policy",
        {"max_autonomous_steps": 24},
    )
    assert agent._max_tool_steps() == 24

    storage.set_runtime_value(
        "experience.autonomy_policy",
        {"max_autonomous_steps": 200},
    )
    assert agent._max_tool_steps() == 24
    storage.close()


def test_agentic_policy_fails_closed_on_malformed_persisted_values(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path, object())
    storage.set_runtime_value(
        "experience.autonomy_policy",
        {
            "allow_safe_tools": "false",
            "allow_review_tools": "false",
            "allow_danger_tools": 1,
            "approval_required_for": "execution.apply",
            "verify_answers": "false",
        },
    )

    policy = agent._autonomy_policy()

    assert policy["allow_safe_tools"] is True
    assert policy["allow_review_tools"] is False
    assert policy["allow_danger_tools"] is False
    assert policy["approval_required_for"] == list(
        DEFAULT_AUTONOMY_POLICY["approval_required_for"]
    )
    assert policy["verify_answers"] is True
    storage.close()


def test_agentic_loop_stops_at_step_budget(monkeypatch, tmp_path):
    # Model keeps asking for a tool; loop must force a final answer at the budget.
    class AlwaysToolLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            if "Лимит шагов" in system:
                return _result("Финальный ответ после лимита.")
            return _result('{"tool": "web.search", "arguments": {"query": "x"}}')

    llm = AlwaysToolLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    agent.storage.set_runtime_value(
        "experience.autonomy_policy",
        {"max_autonomous_steps": 2, "verify_answers": False},
    )

    async def fake_run(name, arguments=None, **kwargs):
        return type(
            "R",
            (),
            {"tool": name, "ok": True, "summary": "ok", "data": {"results": []}},
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("собери данные и ответь"))

    assert response.answer == "Финальный ответ после лимита."
    # Two tool rounds then a forced final answer = 3 completions.
    assert llm.calls == 3
    storage.close()


def test_agentic_stream_suppresses_tool_json_and_streams_answer(monkeypatch, tmp_path):
    class StreamToolThenAnswerLLM:
        def __init__(self) -> None:
            self.rounds = 0

        async def stream_complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.rounds += 1
            if self.rounds == 1:
                for piece in ['{"tool": "web.search",', ' "arguments": {"query": "x"}}']:
                    yield LLMStreamChunk(kind="delta", content=piece)
                yield LLMStreamChunk(kind="done", finish_reason="stop")
            else:
                for piece in ["Готово: ", "нашёл ответ."]:
                    yield LLMStreamChunk(kind="delta", content=piece)
                yield LLMStreamChunk(kind="done", finish_reason="stop")

    llm = StreamToolThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)

    async def fake_run(name, arguments=None, **kwargs):
        return type(
            "R",
            (),
            {"tool": name, "ok": True, "summary": "ok", "data": {"results": [{"title": "t"}]}},
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    async def collect():
        deltas = []
        events = []
        done = None
        async for message in agent.stream_chat("собери и ответь"):
            if message["type"] == "delta":
                deltas.append(message["content"])
            elif message["type"] == "event":
                events.append(message["event"])
            elif message["type"] == "done":
                done = message
        return deltas, events, done

    deltas, events, done = asyncio.run(collect())
    streamed = "".join(deltas)

    assert "tool" not in streamed  # the JSON tool call must not leak to the user
    assert "Готово: нашёл ответ." in streamed
    assert done["answer"] == "Готово: нашёл ответ."
    assert any(event.get("type") == "tool_call" for event in events)
    storage.close()


def test_agentic_stream_corrects_mixed_tool_payload_without_leaking(monkeypatch, tmp_path):
    class MixedStreamToolThenAnswerLLM:
        def __init__(self) -> None:
            self.rounds = 0
            self.messages: list[list[dict[str, str]]] = []

        async def stream_complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.rounds += 1
            self.messages.append(messages)
            if self.rounds == 1:
                for piece in [
                    "Попробую выполнить проверку.\n",
                    '{"tool":"runtime.status","arguments":{}}',
                ]:
                    yield LLMStreamChunk(kind="delta", content=piece)
            elif self.rounds == 2:
                yield LLMStreamChunk(
                    kind="delta",
                    content='{"tool":"runtime.status","arguments":{}}',
                )
            else:
                for piece in ["Проверка ", "завершена."]:
                    yield LLMStreamChunk(kind="delta", content=piece)
            yield LLMStreamChunk(kind="done", finish_reason="stop")

    llm = MixedStreamToolThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    runs = []

    async def fake_run(name, arguments=None, **kwargs):
        runs.append((name, arguments))
        return type(
            "R",
            (),
            {"tool": name, "ok": True, "summary": "runtime ok", "data": {"ready": True}},
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    async def collect():
        deltas = []
        done = None
        async for item in agent.stream_chat("собери данные и ответь"):
            if item["type"] == "delta":
                deltas.append(item["content"])
            elif item["type"] == "done":
                done = item
        return deltas, done

    deltas, done = asyncio.run(collect())
    visible = "".join(deltas)

    assert visible == "Проверка завершена."
    assert done["answer"] == visible
    assert runs == [("runtime.status", {})]
    assert "Попробую" not in visible
    assert '"tool"' not in visible
    assert "Внутренняя ошибка протокола" in "\n".join(
        item["content"] for item in llm.messages[1] if item["role"] == "system"
    )
    storage.close()


def test_agentic_stream_executes_alternative_dialect_tool_call(monkeypatch, tmp_path):
    """An operator command must run even when the model emits its tool call in an
    OpenAI dialect (tool_calls array + stringified arguments) rather than the
    canonical {"tool":...,"arguments":...} shape — no protocol dead-end."""

    class OpenAIDialectToolThenAnswerLLM:
        def __init__(self) -> None:
            self.rounds = 0

        async def stream_complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.rounds += 1
            if self.rounds == 1:
                yield LLMStreamChunk(
                    kind="delta",
                    content=(
                        '{"tool_calls":[{"type":"function","function":'
                        '{"name":"runtime.status","arguments":"{}"}}]}'
                    ),
                )
            else:
                yield LLMStreamChunk(kind="delta", content="Система в норме.")
            yield LLMStreamChunk(kind="done", finish_reason="stop")

    llm = OpenAIDialectToolThenAnswerLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    runs = []

    async def fake_run(name, arguments=None, **kwargs):
        runs.append((name, arguments))
        return type(
            "R", (), {"tool": name, "ok": True, "summary": "runtime ok", "data": {}}
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    async def collect():
        deltas = []
        done = None
        async for item in agent.stream_chat("собери данные и ответь"):
            if item["type"] == "delta":
                deltas.append(item["content"])
            elif item["type"] == "done":
                done = item
        return deltas, done

    deltas, done = asyncio.run(collect())
    visible = "".join(deltas)

    assert runs == [("runtime.status", {})]  # the requested tool actually executed
    assert visible == "Система в норме."
    assert done["answer"] == "Система в норме."
    assert "Не удалось безопасно завершить" not in visible  # no protocol dead-end
    assert "tool_calls" not in visible and '"tool"' not in visible
    storage.close()


def test_agentic_stream_forced_final_tool_payload_is_safe_error(monkeypatch, tmp_path):
    class ToolEvenWhenForcedFinalLLM:
        def __init__(self) -> None:
            self.rounds = 0

        async def stream_complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.rounds += 1
            yield LLMStreamChunk(
                kind="delta",
                content='{"tool":"runtime.status","arguments":{}}',
            )
            yield LLMStreamChunk(kind="done", finish_reason="stop")

    llm = ToolEvenWhenForcedFinalLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    storage.set_runtime_value(
        "experience.autonomy_policy",
        {"max_autonomous_steps": 1, "verify_answers": False},
    )
    runs = []

    async def fake_run(name, arguments=None, **kwargs):
        runs.append(name)
        return type(
            "R",
            (),
            {"tool": name, "ok": True, "summary": "ok", "data": {}},
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)

    async def collect():
        deltas = []
        done = None
        async for item in agent.stream_chat("собери данные и ответь"):
            if item["type"] == "delta":
                deltas.append(item["content"])
            elif item["type"] == "done":
                done = item
        return deltas, done

    deltas, done = asyncio.run(collect())
    visible = "".join(deltas)

    assert runs == ["runtime.status"]
    assert "runtime.status [effect=" in visible
    assert "автоматически не повторяю" in visible
    assert '"tool"' not in visible
    assert done["answer"] == visible
    assert done["events"][-1]["payload"]["finish_reason"] == "protocol_error"
    storage.close()


def test_mission_step_executes_with_tools_when_llm_enabled(monkeypatch, tmp_path):
    class MissionToolThenReportLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result('{"tool": "runtime.status", "arguments": {}}')
            return _result("Шаг выполнен: проверил статус рантайма. Осталось: ничего.")

    llm = MissionToolThenReportLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        captured["tool"] = name
        return type(
            "R",
            (),
            {"tool": name, "ok": True, "summary": "runtime ok", "data": {"profile": "turbo"}},
        )()

    monkeypatch.setattr(agent.tools, "run", fake_run)
    mission = agent.create_mission("Проверить рантайм и отчитаться")

    response = asyncio.run(agent.execute_next_mission_step(mission["id"]))

    assert response.result.ok is True
    assert response.task is not None
    assert response.task.status == "done"
    assert response.result.data["tool_steps"] == 1
    assert response.result.data["autonomous"] is True
    assert "Шаг выполнен" in response.result.summary
    assert captured["tool"] == "runtime.status"
    storage.close()


def test_mission_step_approval_carries_mission_id(monkeypatch, tmp_path):
    target = tmp_path / "mission-gated.txt"
    tool_call = _execution_write_call(target, action_id="mission-gated-write")

    class MissionDangerLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(tool_call)
            return _result("Шаг требует подтверждения оператора для действия на хосте.")

    agent, storage = _agent(monkeypatch, tmp_path, MissionDangerLLM())

    async def fail_run(name, arguments=None, **kwargs):
        raise AssertionError(f"dangerous tool {name} must not run autonomously")

    monkeypatch.setattr(agent.tools, "run", fail_run)
    mission = agent.create_mission("Проверить дату на хосте")

    response = asyncio.run(agent.execute_next_mission_step(mission["id"]))

    assert response.task is not None
    pending = storage.list_approvals(limit=10, status="pending")
    assert len(pending) == 1
    assert response.result.ok is False
    assert response.task.status == "blocked"
    assert response.result.data["approval_ids"] == [pending[0]["id"]]
    payload = pending[0]["payload"]
    if isinstance(payload, str):
        import json as _json

        payload = _json.loads(payload)
    assert payload.get("mission_id") == mission["id"]
    assert payload.get("tool") == "execution.apply"
    assert payload["arguments"]["payload"]["protocol"] == "jarvis.execution.v1"
    assert not target.exists()
    storage.close()


def test_approval_execution_resumes_blocked_mission_step(monkeypatch, tmp_path):
    target = tmp_path / "mission-approved.txt"
    tool_call = _execution_write_call(target, action_id="mission-approved-write")

    class MissionDangerThenResumeLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _result(tool_call)
            if self.calls == 2:
                return _result("Шаг требует допуска оператора.")
            return _result("Шаг завершён после допуска: команда на хосте выполнена.")

    llm = MissionDangerThenResumeLLM()
    agent, storage = _agent(monkeypatch, tmp_path, llm)
    profile = {
        "schema": "jarvis.host-profile.v1",
        "fingerprint_sha256": "a" * 64,
        "host": {"os": {}, "architecture": {}, "accelerators": {}, "tools": {}},
    }
    agent.executive = ExecutiveCoordinator(storage=storage, host_profile=profile)
    agent.tools.executive = agent.executive
    goal = f"Write {target}"
    mission = storage.create_mission(title=goal, goal=goal, tasks=[goal])
    agent.executive.create_for_mission(mission)

    blocked = asyncio.run(agent.execute_next_mission_step(mission["id"]))
    approval = storage.list_approvals(limit=1, status="pending")[0]
    storage.update_approval(approval["id"], status="approved", result={"operator": "test"})
    executor = ApprovalExecutor(
        storage=storage,
        llm=agent.llm,
        dispatcher=DispatcherManager(agent.settings, repo_root=tmp_path),
        tools=agent.tools,
        mission_resumer=agent.resume_mission_after_approval,
    )

    result = asyncio.run(executor.execute(approval["id"]))
    refreshed = storage.get_mission(mission["id"])
    task = refreshed["tasks"][0]
    hits = storage.search_memory("после допуска", limit=5)

    assert blocked.task is not None
    assert blocked.task.status == "blocked"
    assert result.ok is True
    assert result.approval is not None
    assert result.approval["status"] == "executed"
    assert result.data["tool_run"]["tool"] == "execution.apply"
    assert result.data["mission_resume"]["ok"] is True
    assert target.read_bytes() == b"approved"
    assert task["status"] == "done"
    assert "после допуска" in task["notes"]
    assert hits
    storage.close()


def test_approval_resume_protocol_failure_never_marks_mission_done(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path, object())
    mission = storage.create_mission(
        title="Approved action with broken synthesis",
        goal="Run one approved action",
        tasks=["Run approved action"],
    )
    task_id = mission["tasks"][0]["id"]
    storage.update_mission_task(
        task_id,
        mission_id=mission["id"],
        status="blocked",
        notes="awaiting approval",
    )
    approval = {
        "id": "apr_protocol_failure",
        "payload": {
            "mission_id": mission["id"],
            "task_id": task_id,
            "resume": {
                "messages": [{"role": "user", "content": "continue"}],
                "used_tools": 1,
            },
        },
    }

    async def protocol_failure(*_args, **_kwargs):
        return SimpleNamespace(
            answer="Не удалось безопасно завершить служебный протокол.",
            error=None,
            ok=True,
            blocked_by_approval=False,
            finish_reason="protocol_error",
            used_tools=1,
            approval_ids=(),
        )

    monkeypatch.setattr(agent, "_continue_agentic_answer", protocol_failure)
    approved_tool = ToolRunResponse(
        tool="execution.apply",
        ok=True,
        summary="Target mutation returned success.",
        data={"outcome_known": True},
    )

    result = asyncio.run(agent.resume_mission_after_approval(approval, approved_tool))
    refreshed = storage.get_mission(mission["id"])
    refreshed_task = refreshed["tasks"][0]

    assert result is not None
    assert result.ok is False
    assert result.data["finish_reason"] == "protocol_error"
    assert result.data["continuation_confirmed"] is False
    assert "Approved tool completed" in result.summary
    assert "not confirmed" in result.summary
    assert refreshed_task["status"] == "blocked"
    storage.close()


def test_approval_resume_immediate_synthesis_outage_reports_completed_tool(
    monkeypatch,
    tmp_path,
):
    class OutageLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            return SimpleNamespace(
                ok=False,
                content="",
                error="post-approval synthesis unavailable",
                raw={},
            )

    agent, storage = _agent(monkeypatch, tmp_path, OutageLLM())
    mission = storage.create_mission(
        title="Approved action with synthesis outage",
        goal="Run one approved action",
        tasks=["Run approved action"],
    )
    task_id = mission["tasks"][0]["id"]
    storage.update_mission_task(
        task_id,
        mission_id=mission["id"],
        status="blocked",
        notes="awaiting approval",
    )
    approval = {
        "id": "apr_synthesis_outage",
        "payload": {
            "mission_id": mission["id"],
            "task_id": task_id,
            "resume": {
                "messages": [{"role": "user", "content": "continue"}],
                "used_tools": 1,
            },
        },
    }
    approved_tool = ToolRunResponse(
        tool="execution.apply",
        ok=True,
        summary="Target mutation returned success.",
        data={"outcome_known": True},
    )

    result = asyncio.run(agent.resume_mission_after_approval(approval, approved_tool))
    refreshed_task = storage.get_mission(mission["id"])["tasks"][0]

    assert result is not None
    assert result.ok is False
    assert result.data["finish_reason"] == "synthesis_error"
    assert result.data["continuation_confirmed"] is False
    assert "Approved tool completed" in result.summary
    assert "not confirmed" in result.summary
    assert refreshed_task["status"] == "blocked"
    storage.close()


def test_agentic_stream_plain_answer_has_no_regression(monkeypatch, tmp_path):
    class PlainStreamLLM:
        async def stream_complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            for piece in ["Привет", ", чем помочь?"]:
                yield LLMStreamChunk(kind="delta", content=piece)
            yield LLMStreamChunk(kind="done", finish_reason="stop")

    agent, storage = _agent(monkeypatch, tmp_path, PlainStreamLLM())
    monkeypatch.setattr(agent, "_tools_for_context", lambda _context: [])

    async def collect():
        deltas = []
        async for message in agent.stream_chat("привет"):
            if message["type"] == "delta":
                deltas.append(message["content"])
        return deltas

    deltas = asyncio.run(collect())
    assert deltas == ["Привет", ", чем помочь?"]
    storage.close()


def test_looks_like_raw_tool_echo_detects_pasted_observations():
    from jarvis_gpt.agent import _looks_like_raw_tool_echo

    assert _looks_like_raw_tool_echo('observation[web.answer · error]: {"error":"x"}')
    assert _looks_like_raw_tool_echo(
        '{"ok": true, "query": "vLLM version", "answer": "", "snippets": [{"t":"x"}]}'
    )
    assert _looks_like_raw_tool_echo(
        '{"error":"web.answer failed: no provider","code":"NO_PROVIDER"}'
    )
    # The weak model echoing the injected repair context ("Факты из инструментов:") or
    # embedding a raw observation line mid-answer must be flagged for re-synthesis.
    assert _looks_like_raw_tool_echo(
        "Факты из инструментов:\n- observation[filesystem.find · ok]: Found 100 match(es)\n"
        '  data: {"truncated": true, "root": "D:\\\\jarvis-gpt"}'
    )
    assert _looks_like_raw_tool_echo(
        "Нашёл файлы:\nobservation[filesystem.find · ok]: Found 12 match(es) across 3 file(s)"
    )
    # A real natural-language answer must NOT be flagged.
    assert not _looks_like_raw_tool_echo("Последняя стабильная версия vLLM — v0.25.1.")
    assert not _looks_like_raw_tool_echo("")
    assert not _looks_like_raw_tool_echo("Вот план: 1) сделать X 2) проверить Y.")
    # A coding answer mentioning an array index must NOT trip the observation pattern
    # (it lacks the "· <state>]:" observation shape).
    assert not _looks_like_raw_tool_echo("Возьми observation[0]: это первый элемент массива.")
