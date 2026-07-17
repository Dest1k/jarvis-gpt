from __future__ import annotations

import asyncio
import re

import httpx
import pytest
from jarvis_gpt.config import load_settings
from jarvis_gpt.llm import LLMRouter, LLMStreamChunk, _stream_chunk_from_line

# Shared marker scan for SPARK-0006 / FUNC-FIND-006 (mirrors qa response_integrity).
_TOOL_ENVELOPE_MARKERS = (
    re.compile(r"(?i)(?:^|\s)call\s*:\s*\S+"),
    re.compile(
        r"(?is)[{[][^}\]]*(?:"
        r"\"(?:tool|function|tool_calls|function_call)\"\s*:|"
        r"\"name\"\s*:\s*\"[^\"]+\"[^}\]]*\"arguments\"\s*:"
        r")"
    ),
)


def _has_tool_envelope_marker(text: str) -> bool:
    return any(pattern.search(text) for pattern in _TOOL_ENVELOPE_MARKERS)


def test_tool_shaped_output_fixtures_are_detected_by_marker_scan():
    """SPARK-0006: known bad finals must fail the envelope marker scan."""
    bad_finals = [
        "call:documents.read",
        "call:llm.health",
        "call:dispatcher.status",
        'call:documents.read\n{"tool":"documents.read","arguments":{}}',
        '{"tool":"web.search","arguments":{"query":"x"}}',
        '{"tool_calls":[{"id":"1","function":{"name":"x","arguments":"{}"}}]}',
    ]
    for final in bad_finals:
        assert _has_tool_envelope_marker(final) is True, final

    assert _has_tool_envelope_marker("Документ сохранён, замечаний нет.") is False
    assert _has_tool_envelope_marker("Use the tools available in the UI.") is False


def test_stream_chunk_parser_reads_openai_sse_delta():
    chunk = _stream_chunk_from_line(
        'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}'
    )

    assert chunk is not None
    assert chunk.kind == "delta"
    assert chunk.content == "Hello"


def test_stream_chunk_parser_reads_done_marker():
    chunk = _stream_chunk_from_line("data: [DONE]")

    assert chunk is not None
    assert chunk.kind == "done"


def test_stream_chunk_parser_preserves_finish_reason():
    chunk = _stream_chunk_from_line(
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}'
    )

    assert chunk is not None
    assert chunk.kind == "done"
    assert chunk.finish_reason == "length"


def test_stream_chunk_parser_keeps_finish_reason_on_contentful_delta():
    chunk = _stream_chunk_from_line(
        'data: {"choices":[{"delta":{"content":"cut"},"finish_reason":"length"}]}'
    )

    assert chunk is not None
    assert chunk.kind == "delta"
    assert chunk.content == "cut"
    assert chunk.finish_reason == "length"


def test_stream_chunk_parser_preserves_usage_without_choices():
    chunk = _stream_chunk_from_line(
        'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":8}}'
    )

    assert chunk is not None
    assert chunk.kind == "usage"
    assert chunk.raw["usage"]["completion_tokens"] == 8


def test_llm_health_ignores_proxy_environment(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[dict[str, str]]]:
            return {"data": [{"id": "dispatcher"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr("jarvis_gpt.llm.httpx.AsyncClient", FakeClient)

    result = asyncio.run(LLMRouter(load_settings()).health())

    assert result["ok"] is True
    assert captured["trust_env"] is False


def test_foreground_preempts_and_defers_background_generation(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    router = LLMRouter(load_settings())

    async def scenario():
        background_started = asyncio.Event()
        background_cancelled = asyncio.Event()
        calls = {"background": 0, "foreground": 0}

        class FakeResponse:
            def __init__(self, content: str) -> None:
                self._content = content

            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {"choices": [{"message": {"content": self._content}}]}

        class FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args) -> None:
                return None

            async def post(self, _url: str, *, json):
                label = json["messages"][-1]["content"]
                calls[label] += 1
                if label == "background" and calls[label] == 1:
                    background_started.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        background_cancelled.set()
                        raise
                return FakeResponse(label)

        monkeypatch.setattr("jarvis_gpt.llm.httpx.AsyncClient", FakeClient)
        with router.background_priority():
            background = asyncio.create_task(
                router.complete([{"role": "user", "content": "background"}])
            )
        await asyncio.wait_for(background_started.wait(), timeout=1)

        foreground = asyncio.create_task(
            router.complete([{"role": "user", "content": "foreground"}])
        )
        foreground_result = await asyncio.wait_for(foreground, timeout=1)
        background_result = await asyncio.wait_for(background, timeout=1)

        assert foreground_result.ok is True
        assert foreground_result.content == "foreground"
        assert background_result.ok is True
        assert background_result.content == "background"
        assert background_cancelled.is_set()
        assert calls == {"background": 2, "foreground": 1}
        status = router.admission_status()
        assert status["background_preemptions"] == 1
        assert status["background_deferred"] >= 1
        assert status["foreground_active"] == 0
        assert status["background_active"] == 0

    asyncio.run(scenario())


def test_router_admission_is_safe_across_event_loops(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, _url: str, *, json):
            return FakeResponse()

    monkeypatch.setattr("jarvis_gpt.llm.httpx.AsyncClient", FakeClient)
    router = LLMRouter(load_settings())

    first = asyncio.run(router.complete([{"role": "user", "content": "one"}]))
    second = asyncio.run(router.complete([{"role": "user", "content": "two"}]))

    assert first.ok is True
    assert second.ok is True


def test_admission_release_survives_repeated_cancellation(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    router = LLMRouter(load_settings())

    async def scenario():
        background = await router._acquire_admission("background")
        state = background.state

        async with state.condition:
            release = asyncio.create_task(router._release_admission(background))
            await asyncio.sleep(0)
            release.cancel()
            await asyncio.sleep(0)
            release.cancel()
            await asyncio.sleep(0)

        try:
            await release
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("repeated cancellation must remain visible to the caller")

        assert background.released is True
        assert router.admission_status()["background_active"] == 0
        foreground = await asyncio.wait_for(
            router._acquire_admission("foreground"),
            timeout=1,
        )
        await router._release_admission(foreground)
        assert router.admission_status()["foreground_active"] == 0

    asyncio.run(scenario())


def test_inference_benchmark_uses_stream_usage_and_bounded_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    router = LLMRouter(load_settings())
    calls: list[dict[str, object]] = []

    async def fake_stream(_messages, **kwargs):
        calls.append(kwargs)
        yield LLMStreamChunk(kind="delta", content="1. красный\n")
        yield LLMStreamChunk(
            kind="usage",
            raw={"usage": {"prompt_tokens": 20, "completion_tokens": 12}},
        )
        yield LLMStreamChunk(kind="done")

    monkeypatch.setattr(router, "stream_complete", fake_stream)
    result = asyncio.run(router.benchmark_inference(runs=9, max_tokens=999))

    assert result["ok"] is True
    assert result["requested_runs"] == 3
    assert result["max_tokens"] == 64
    assert len(calls) == 3
    assert all(item["include_usage"] is True for item in calls)
    assert all(item["max_tokens"] == 64 for item in calls)
    assert result["aggregate"]["completion_tokens_p50"] == 12.0


def test_stream_benchmark_reads_usage_after_finish_reason(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    router = LLMRouter(load_settings())

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self):
            lines = [
                'data: {"choices":[{"delta":{"content":"красный"}}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                'data: {"choices":[],"usage":{"completion_tokens":8}}',
                "data: [DONE]",
            ]
            for line in lines:
                await asyncio.sleep(0)
                yield line

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *_args) -> None:
            return None

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        def stream(self, *_args, **_kwargs):
            return FakeStream()

    monkeypatch.setattr("jarvis_gpt.llm.httpx.AsyncClient", FakeClient)

    result = asyncio.run(router._benchmark_inference_run(max_tokens=8))

    assert result["ok"] is True
    assert result["completion_tokens"] == 8
    assert result["decode_tokens_per_sec"] is not None

    async def collect_kinds() -> list[str]:
        return [
            chunk.kind
            async for chunk in router.stream_complete(
                [{"role": "user", "content": "benchmark"}],
                include_usage=True,
            )
        ]

    assert asyncio.run(collect_kinds()) == ["delta", "done", "usage"]


def test_background_stream_releases_lease_before_buffer_delivery(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    router = LLMRouter(load_settings())

    async def scenario():
        first_delivered = asyncio.Event()
        finish_consumer = asyncio.Event()

        async def fake_stream(_body, _lease):
            yield LLMStreamChunk(kind="delta", content="background")
            yield LLMStreamChunk(kind="done")

        async def fake_post(body, _lease):
            return {
                "choices": [
                    {"message": {"content": body["messages"][-1]["content"]}}
                ]
            }

        monkeypatch.setattr(router, "_stream_completion", fake_stream)
        monkeypatch.setattr(router, "_post_completion", fake_post)

        async def consume_background() -> None:
            with router.background_priority():
                stream = router.stream_complete(
                    [{"role": "user", "content": "background"}]
                )
                first = await anext(stream)
                assert first.content == "background"
                first_delivered.set()
                await finish_consumer.wait()
                assert [chunk.kind async for chunk in stream] == ["done"]

        consumer = asyncio.create_task(consume_background())
        await asyncio.wait_for(first_delivered.wait(), timeout=1)
        assert router.admission_status()["background_active"] == 0

        foreground = await asyncio.wait_for(
            router.complete([{"role": "user", "content": "foreground"}]),
            timeout=1,
        )
        assert foreground.ok is True
        assert foreground.content == "foreground"

        finish_consumer.set()
        await asyncio.wait_for(consumer, timeout=1)

    asyncio.run(scenario())


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
def test_complete_retries_transient_http_status(monkeypatch, tmp_path, status_code):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr("jarvis_gpt.llm._LLM_RETRY_BASE_DELAY_SEC", 0.0)
    router = LLMRouter(load_settings())
    calls = 0

    async def flaky_post(_body, _lease):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _http_status_error(status_code)
        return {"choices": [{"message": {"content": "recovered"}}]}

    monkeypatch.setattr(router, "_post_completion", flaky_post)

    result = asyncio.run(router.complete([{"role": "user", "content": "retry"}]))

    assert result.ok is True
    assert result.content == "recovered"
    assert calls == 2


def test_complete_does_not_retry_non_transient_http_status(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    router = LLMRouter(load_settings())
    calls = 0

    async def rejected_post(_body, _lease):
        nonlocal calls
        calls += 1
        raise _http_status_error(400)

    monkeypatch.setattr(router, "_post_completion", rejected_post)

    result = asyncio.run(router.complete([{"role": "user", "content": "bad"}]))

    assert result.ok is False
    assert calls == 1


def test_complete_bounds_timeout_retries(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr("jarvis_gpt.llm._LLM_RETRY_BASE_DELAY_SEC", 0.0)
    router = LLMRouter(load_settings())
    calls = 0

    async def timed_out_post(_body, _lease):
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "http://llm.test/chat/completions")
        raise httpx.ReadTimeout("temporary timeout", request=request)

    monkeypatch.setattr(router, "_post_completion", timed_out_post)

    result = asyncio.run(router.complete([{"role": "user", "content": "retry"}]))

    assert result.ok is False
    assert result.error == "temporary timeout"
    assert calls == 3


def test_complete_retries_connection_reset(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr("jarvis_gpt.llm._LLM_RETRY_BASE_DELAY_SEC", 0.0)
    router = LLMRouter(load_settings())
    calls = 0

    async def reset_post(_body, _lease):
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("POST", "http://llm.test/chat/completions")
            raise httpx.RemoteProtocolError("connection reset", request=request)
        return {"choices": [{"message": {"content": "recovered"}}]}

    monkeypatch.setattr(router, "_post_completion", reset_post)

    result = asyncio.run(router.complete([{"role": "user", "content": "retry"}]))

    assert result.ok is True
    assert result.content == "recovered"
    assert calls == 2


def test_foreground_stream_retries_before_first_exposed_chunk(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr("jarvis_gpt.llm._LLM_RETRY_BASE_DELAY_SEC", 0.0)
    router = LLMRouter(load_settings())
    calls = 0

    async def flaky_stream(_body, _lease):
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("POST", "http://llm.test/chat/completions")
            raise httpx.ConnectError("not ready", request=request)
        yield LLMStreamChunk(kind="delta", content="once")
        yield LLMStreamChunk(kind="done", finish_reason="stop")

    monkeypatch.setattr(router, "_stream_completion", flaky_stream)

    async def collect():
        return [
            chunk
            async for chunk in router.stream_complete(
                [{"role": "user", "content": "stream"}]
            )
        ]

    chunks = asyncio.run(collect())

    assert [(chunk.kind, chunk.content) for chunk in chunks] == [
        ("delta", "once"),
        ("done", ""),
    ]
    assert calls == 2


def test_foreground_stream_never_retries_after_metadata_is_visible(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr("jarvis_gpt.llm._LLM_RETRY_BASE_DELAY_SEC", 0.0)
    router = LLMRouter(load_settings())
    calls = 0

    async def reset_after_metadata(_body, _lease):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield LLMStreamChunk(kind="usage", raw={"usage": {"prompt_tokens": 1}})
            request = httpx.Request("POST", "http://llm.test/chat/completions")
            raise httpx.RemoteProtocolError("connection reset", request=request)
        yield LLMStreamChunk(kind="delta", content="once")
        yield LLMStreamChunk(kind="done")

    monkeypatch.setattr(router, "_stream_completion", reset_after_metadata)

    async def collect():
        return [
            chunk
            async for chunk in router.stream_complete(
                [{"role": "user", "content": "stream"}]
            )
        ]

    chunks = asyncio.run(collect())

    assert [(chunk.kind, chunk.content) for chunk in chunks] == [
        ("usage", ""),
        ("error", ""),
    ]
    assert chunks[-1].error == "connection reset"
    assert calls == 1


def test_foreground_stream_never_retries_after_partial_output(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr("jarvis_gpt.llm._LLM_RETRY_BASE_DELAY_SEC", 0.0)
    router = LLMRouter(load_settings())
    calls = 0

    async def interrupted_stream(_body, _lease):
        nonlocal calls
        calls += 1
        yield LLMStreamChunk(kind="delta", content="partial")
        request = httpx.Request("POST", "http://llm.test/chat/completions")
        raise httpx.ReadTimeout("stream interrupted", request=request)

    monkeypatch.setattr(router, "_stream_completion", interrupted_stream)

    async def collect():
        return [
            chunk
            async for chunk in router.stream_complete(
                [{"role": "user", "content": "stream"}]
            )
        ]

    chunks = asyncio.run(collect())

    assert [(chunk.kind, chunk.content) for chunk in chunks] == [
        ("delta", "partial"),
        ("error", ""),
    ]
    assert chunks[-1].error == "stream interrupted"
    assert calls == 1


def test_background_stream_discards_buffer_before_transient_retry(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr("jarvis_gpt.llm._LLM_RETRY_BASE_DELAY_SEC", 0.0)
    router = LLMRouter(load_settings())
    calls = 0

    async def flaky_stream(_body, _lease):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield LLMStreamChunk(kind="delta", content="discarded")
            request = httpx.Request("POST", "http://llm.test/chat/completions")
            raise httpx.ReadTimeout("stream interrupted", request=request)
        yield LLMStreamChunk(kind="delta", content="kept")
        yield LLMStreamChunk(kind="done")

    monkeypatch.setattr(router, "_stream_completion", flaky_stream)

    async def collect():
        with router.background_priority():
            return [
                chunk
                async for chunk in router.stream_complete(
                    [{"role": "user", "content": "stream"}]
                )
            ]

    chunks = asyncio.run(collect())

    assert [(chunk.kind, chunk.content) for chunk in chunks] == [
        ("delta", "kept"),
        ("done", ""),
    ]
    assert calls == 2


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://llm.test/chat/completions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"status {status_code}",
        request=request,
        response=response,
    )


def test_system_first_merges_all_system_into_one_leading_block():
    from jarvis_gpt.llm import _system_first

    msgs = [
        {"role": "system", "content": "A"},
        {"role": "user", "content": "lessons"},
        {"role": "system", "content": "B"},  # Qwen forbids a 2nd system message
        {"role": "user", "content": "hi"},
    ]
    out = _system_first(msgs)
    # Exactly one system message, at the front, content merged in order.
    assert [m["role"] for m in out] == ["system", "user", "user"]
    assert out[0]["content"] == "A\n\nB"
    assert [m["content"] for m in out[1:]] == ["lessons", "hi"]


def test_system_first_is_noop_when_single_leading_system():
    from jarvis_gpt.llm import _system_first

    msgs = [
        {"role": "system", "content": "A"},
        {"role": "user", "content": "hi"},
    ]
    assert _system_first(msgs) is msgs


def test_system_first_handles_no_system_messages():
    from jarvis_gpt.llm import _system_first

    msgs = [{"role": "user", "content": "hi"}]
    assert _system_first(msgs) is msgs


def test_suppress_model_thinking_profile_forces_enable_thinking_off(monkeypatch, tmp_path):
    # Qwen dumps an unparseable thinking trace into the answer; its profile sets
    # suppress_model_thinking, so the router must send enable_thinking=False even when the
    # caller asked for thinking.
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_PROFILE", "qwen36-vl")
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    router = LLMRouter(load_settings())
    captured: dict[str, object] = {}

    async def fake_post(body, _lease):
        captured["body"] = body
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router, "_post_completion", fake_post)
    result = asyncio.run(
        router.complete([{"role": "user", "content": "hi"}], thinking_enabled=True)
    )
    assert result.ok is True
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_non_suppressing_profile_keeps_thinking_when_enabled(monkeypatch, tmp_path):
    # A profile without the flag must NOT inject enable_thinking=False when the caller
    # left thinking on.
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_PROFILE", "gemma4-turbo")
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    router = LLMRouter(load_settings())
    captured: dict[str, object] = {}

    async def fake_post(body, _lease):
        captured["body"] = body
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(router, "_post_completion", fake_post)
    asyncio.run(router.complete([{"role": "user", "content": "hi"}], thinking_enabled=True))
    assert "chat_template_kwargs" not in captured["body"]
