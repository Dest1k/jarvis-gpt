from __future__ import annotations

from jarvis_gpt.llm import _stream_chunk_from_line


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
