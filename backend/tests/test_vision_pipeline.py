"""Vision chat pipeline: image attachments become OpenAI-style content parts for a VLM.

The plumbing (composer -> /api/files/upload -> disk -> chat attachment reference) already
existed; these tests cover the new backend seam that loads an uploaded image's bytes and
threads them into the LLM request as `image_url` content parts — gated on a vision-capable
profile so a text-only brain keeps treating images as file metadata.
"""

from __future__ import annotations

import asyncio
import base64
import io

from jarvis_gpt.agent import (
    _VISION_MAX_IMAGE_BYTES,
    _VISION_MAX_IMAGES,
    AgentContext,
    AgentRuntime,
    _native_action_from_message,
)
from jarvis_gpt.config import PROFILES, ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.ingest import FileIngestor
from jarvis_gpt.llm import LLMResult, LLMRouter
from jarvis_gpt.storage import JarvisStorage

# A 1x1 PNG — enough for a valid image/* upload.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _agent(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )
    return agent, storage, settings


def _upload(settings, storage, name: str, data: bytes) -> str:
    result = FileIngestor(settings=settings, storage=storage).ingest_upload(
        name, io.BytesIO(data)
    )
    return str(result["file"]["id"])


def test_profile_vision_capability_flags():
    assert PROFILES["qwen36-vl"].vision_capable is True
    # The certified text-only Gemma default must NOT be flagged vision-capable.
    assert PROFILES["gemma4-turbo"].vision_capable is False


def test_image_attachment_becomes_data_url_part(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    file_id = _upload(settings, storage, "shot.png", _PNG_1x1)
    parts = agent._image_parts_for_attachments(
        [{"id": file_id, "name": "shot.png", "mime_type": "image/png"}]
    )
    assert len(parts) == 1
    assert parts[0]["type"] == "image_url"
    url = parts[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # The embedded payload round-trips back to the original bytes.
    assert base64.b64decode(url.split(",", 1)[1]) == _PNG_1x1


def test_non_image_and_missing_attachments_are_skipped(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    doc_id = _upload(settings, storage, "notes.txt", b"hello world")
    parts = agent._image_parts_for_attachments(
        [
            {"id": doc_id, "name": "notes.txt", "mime_type": "text/plain"},
            {"id": "does-not-exist", "name": "ghost.png", "mime_type": "image/png"},
        ]
    )
    assert parts == []


def test_image_mime_inferred_from_name_when_absent(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    file_id = _upload(settings, storage, "photo.jpg", _PNG_1x1)
    parts = agent._image_parts_for_attachments(
        [{"id": file_id, "name": "photo.jpg", "mime_type": None}]
    )
    assert len(parts) == 1
    assert parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_oversized_image_is_skipped(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (_VISION_MAX_IMAGE_BYTES + 1)
    file_id = _upload(settings, storage, "huge.png", big)
    parts = agent._image_parts_for_attachments(
        [{"id": file_id, "name": "huge.png", "mime_type": "image/png"}]
    )
    assert parts == []


def test_image_count_is_capped(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    attachments = []
    for i in range(_VISION_MAX_IMAGES + 3):
        # Distinct bytes -> distinct sha -> distinct stored files.
        payload = _PNG_1x1 + bytes([i])
        fid = _upload(settings, storage, f"img{i}.png", payload)
        attachments.append({"id": fid, "name": f"img{i}.png", "mime_type": "image/png"})
    parts = agent._image_parts_for_attachments(attachments)
    assert len(parts) == _VISION_MAX_IMAGES


def test_build_llm_messages_emits_multipart_content_with_images(monkeypatch, tmp_path):
    agent, storage, _ = _agent(monkeypatch, tmp_path)
    ctx = AgentContext(
        conversation_id=storage.create_conversation("vis"),
        memory_hits=[],
        file_hits=[],
    )
    image_parts = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    messages = agent._build_llm_messages(ctx, "что на картинке?", image_parts=image_parts)
    last = messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    assert last["content"][0] == {"type": "text", "text": "что на картинке?"}
    assert last["content"][1]["type"] == "image_url"


def test_look_at_screen_intent_matches_common_phrasings():
    for phrase in (
        "Посмотри на экран и скажи, что открыто.",
        "Джарвис, глянь на мой экран — что там сейчас?",
        "Что у меня на экране? Есть что-то важное?",
        "look at my screen",
    ):
        from jarvis_gpt.config import load_settings

        action = _native_action_from_message(phrase, load_settings("qwen36-vl"))
        assert action is not None and action.action == "screen.capture", phrase


def test_answer_about_image_sends_screenshot_to_the_vlm(monkeypatch, tmp_path):
    agent, _storage, _ = _agent(monkeypatch, tmp_path)
    shot = tmp_path / "screen.png"
    shot.write_bytes(_PNG_1x1)
    captured: dict[str, object] = {}

    async def fake_complete(messages, **kwargs):
        captured["messages"] = messages
        captured["thinking"] = kwargs.get("thinking_enabled")
        return LLMResult(ok=True, content="На экране открыт VS Code.")

    monkeypatch.setattr(agent.llm, "complete", fake_complete)
    answer = asyncio.run(agent._answer_about_image("что на экране?", str(shot)))
    assert answer == "На экране открыт VS Code."
    # The turn carries the screenshot as an image_url part alongside the question.
    user_msg = captured["messages"][-1]
    assert user_msg["role"] == "user"
    assert isinstance(user_msg["content"], list)
    assert user_msg["content"][0]["text"] == "что на экране?"
    assert user_msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert captured["thinking"] is False


def test_answer_about_image_skips_missing_or_oversized(monkeypatch, tmp_path):
    agent, _storage, _ = _agent(monkeypatch, tmp_path)
    assert asyncio.run(agent._answer_about_image("q", str(tmp_path / "nope.png"))) is None
    big = tmp_path / "big.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (_VISION_MAX_IMAGE_BYTES + 1))
    assert asyncio.run(agent._answer_about_image("q", str(big))) is None


def test_build_llm_messages_plain_string_without_images(monkeypatch, tmp_path):
    agent, storage, _ = _agent(monkeypatch, tmp_path)
    ctx = AgentContext(
        conversation_id=storage.create_conversation("vis"),
        memory_hits=[],
        file_hits=[],
    )
    messages = agent._build_llm_messages(ctx, "просто текст", image_parts=None)
    last = messages[-1]
    assert last["role"] == "user"
    # Backward-compatible: no images -> content stays a plain string.
    assert last["content"] == "просто текст"
