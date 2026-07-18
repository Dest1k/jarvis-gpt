"""Voice chat pipeline: audio attachments are transcribed and folded into the turn.

A voice note (web upload or a Telegram voice message relayed as an attachment) is the
user's message — these tests cover the seam that runs STT on audio/video attachments and
folds the transcript into the text the agent reasons over, degrading to a no-op when STT
is unavailable.
"""

from __future__ import annotations

import asyncio
import io

from jarvis_gpt import speech
from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.ingest import FileIngestor
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage


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
    result = FileIngestor(settings=settings, storage=storage).ingest_upload(name, io.BytesIO(data))
    return str(result["file"]["id"])


def _fake_transcript(text: str):
    def _fn(path, **kwargs):
        return speech.TranscriptResult(ok=True, text=text, language="ru", engine="fake")

    return _fn


def test_audio_only_message_becomes_the_transcript(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    file_id = _upload(settings, storage, "voice.ogg", b"OggS" + b"\x00" * 40)
    monkeypatch.setattr("jarvis_gpt.speech.transcribe", _fake_transcript("включи свет на кухне"))
    attachments = [{"id": file_id, "name": "voice.ogg", "mime_type": "audio/ogg"}]
    folded = asyncio.run(agent._fold_audio_attachment_transcripts("", attachments))
    assert folded == "включи свет на кухне"


def test_audio_transcript_is_appended_to_typed_text(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    file_id = _upload(settings, storage, "note.wav", b"RIFF" + b"\x00" * 40)
    monkeypatch.setattr("jarvis_gpt.speech.transcribe", _fake_transcript("какая погода"))
    attachments = [{"id": file_id, "name": "note.wav", "mime_type": "audio/wav"}]
    folded = asyncio.run(agent._fold_audio_attachment_transcripts("контекст:", attachments))
    assert folded.startswith("контекст:")
    assert "какая погода" in folded
    assert "Расшифровка" in folded


def test_non_audio_attachment_is_left_alone(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    file_id = _upload(settings, storage, "notes.txt", b"hello world")

    def _boom(path, **kwargs):  # transcription must not even be attempted
        raise AssertionError("transcribe called on a non-audio attachment")

    monkeypatch.setattr("jarvis_gpt.speech.transcribe", _boom)
    attachments = [{"id": file_id, "name": "notes.txt", "mime_type": "text/plain"}]
    folded = asyncio.run(agent._fold_audio_attachment_transcripts("привет", attachments))
    assert folded == "привет"


def test_unavailable_stt_is_a_noop(monkeypatch, tmp_path):
    agent, storage, settings = _agent(monkeypatch, tmp_path)
    file_id = _upload(settings, storage, "voice.ogg", b"OggS" + b"\x00" * 40)

    def _fail(path, **kwargs):
        return speech.TranscriptResult(ok=False, error="no engine")

    monkeypatch.setattr("jarvis_gpt.speech.transcribe", _fail)
    attachments = [{"id": file_id, "name": "voice.ogg", "mime_type": "audio/ogg"}]
    folded = asyncio.run(agent._fold_audio_attachment_transcripts("привет", attachments))
    assert folded == "привет"


def test_no_attachments_returns_message_unchanged(monkeypatch, tmp_path):
    agent, _storage, _ = _agent(monkeypatch, tmp_path)
    folded = asyncio.run(agent._fold_audio_attachment_transcripts("привет", []))
    assert folded == "привет"
