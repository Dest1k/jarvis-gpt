from __future__ import annotations

import asyncio

from jarvis_gpt.config import load_settings
from jarvis_gpt.llm import LLMRouter, _stream_chunk_from_line


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
