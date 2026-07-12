from __future__ import annotations

import asyncio

from jarvis_gpt.config import load_settings
from jarvis_gpt.llm import LLMRouter, LLMStreamChunk, _stream_chunk_from_line


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
