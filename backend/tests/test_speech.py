"""Tests for the local voice module (speech-to-text + text-to-speech).

Heavy engines (faster-whisper, torch/Silero, comtypes/SAPI, numpy) are optional and
imported lazily, so these tests monkeypatch the availability probes and engine
callables to exercise selection/degradation logic without a GPU or any model.
"""

from __future__ import annotations

import wave
from pathlib import Path

import pytest
from jarvis_gpt import speech

# --------------------------------------------------------------------------- #
# _split_for_tts
# --------------------------------------------------------------------------- #


def test_split_short_text_single_chunk():
    assert speech._split_for_tts("Короткая фраза.", 900) == ["Короткая фраза."]


def test_split_empty_text():
    assert speech._split_for_tts("   ", 900) == []


def test_split_packs_sentences_under_limit():
    text = "Раз. Два. Три. Четыре."
    chunks = speech._split_for_tts(text, 12)
    assert all(len(c) <= 12 for c in chunks)
    # Every sentence survives across the chunk boundaries.
    joined = " ".join(chunks)
    for word in ("Раз", "Два", "Три", "Четыре"):
        assert word in joined


def test_split_hard_splits_a_very_long_sentence():
    text = "а" * 50  # no sentence breaks
    chunks = speech._split_for_tts(text, 10)
    assert len(chunks) == 5
    assert all(len(c) <= 10 for c in chunks)


# --------------------------------------------------------------------------- #
# Status reporting (no engine loaded).
# --------------------------------------------------------------------------- #


def test_stt_status_prefers_faster_whisper(monkeypatch):
    monkeypatch.setattr(speech, "_faster_whisper_available", lambda: True)
    monkeypatch.setattr(speech, "_whisper_cli_path", lambda: None)
    monkeypatch.delenv("JARVIS_STT_BACKEND", raising=False)
    status = speech.stt_status()
    assert status["available"] is True
    assert status["engine"] == "faster-whisper"


def test_stt_status_unavailable_when_no_engine(monkeypatch):
    monkeypatch.setattr(speech, "_faster_whisper_available", lambda: False)
    monkeypatch.setattr(speech, "_whisper_cli_path", lambda: None)
    monkeypatch.delenv("JARVIS_STT_BACKEND", raising=False)
    status = speech.stt_status()
    assert status["available"] is False
    assert status["engine"] is None


def test_tts_status_prefers_silero(monkeypatch):
    monkeypatch.setattr(speech, "_silero_available", lambda: True)
    monkeypatch.setattr(speech, "_sapi_available", lambda: True)
    monkeypatch.setattr(speech, "_sapi_voice_names", lambda: ["Microsoft Irina"])
    monkeypatch.delenv("JARVIS_TTS_ENGINE", raising=False)
    status = speech.tts_status()
    assert status["available"] is True
    assert status["engine"] == "silero"
    assert status["silero_speaker"] == "eugene"


def test_tts_status_forced_sapi(monkeypatch):
    monkeypatch.setattr(speech, "_silero_available", lambda: True)
    monkeypatch.setattr(speech, "_sapi_available", lambda: True)
    monkeypatch.setattr(speech, "_sapi_voice_names", lambda: ["Microsoft Irina"])
    monkeypatch.setenv("JARVIS_TTS_ENGINE", "sapi")
    assert speech.tts_status()["engine"] == "sapi"


# --------------------------------------------------------------------------- #
# transcribe() engine selection / degradation.
# --------------------------------------------------------------------------- #


def test_transcribe_missing_file(tmp_path):
    result = speech.transcribe(tmp_path / "nope.wav")
    assert result.ok is False
    assert "does not exist" in (result.error or "")


def test_transcribe_unsupported_extension(tmp_path):
    bogus = tmp_path / "note.txt"
    bogus.write_text("hi", encoding="utf-8")
    result = speech.transcribe(bogus)
    assert result.ok is False
    assert "Unsupported" in (result.error or "")


def test_transcribe_uses_faster_whisper(monkeypatch, tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF0000WAVEfmt ")
    monkeypatch.setattr(speech, "_faster_whisper_available", lambda: True)
    monkeypatch.setattr(
        speech,
        "_transcribe_faster_whisper",
        lambda *a, **k: speech.TranscriptResult(
            ok=True, text="привет", engine="faster-whisper:small"
        ),
    )
    monkeypatch.setenv("JARVIS_STT_BACKEND", "auto")
    result = speech.transcribe(audio, language="ru")
    assert result.ok is True
    assert result.text == "привет"


def test_transcribe_falls_back_to_cli(monkeypatch, tmp_path):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF0000WAVEfmt ")

    def _boom(*a, **k):
        raise RuntimeError("no model")

    monkeypatch.setattr(speech, "_faster_whisper_available", lambda: True)
    monkeypatch.setattr(speech, "_transcribe_faster_whisper", _boom)
    monkeypatch.setattr(speech, "_whisper_cli_path", lambda: "whisper")
    monkeypatch.setattr(speech, "_transcribe_whisper_cli", lambda *a, **k: "cli text")
    monkeypatch.setenv("JARVIS_STT_BACKEND", "auto")
    result = speech.transcribe(audio)
    assert result.ok is True
    assert result.text == "cli text"
    assert result.engine == "whisper-cli"


# --------------------------------------------------------------------------- #
# synthesize() engine selection / degradation.
# --------------------------------------------------------------------------- #


def _write_fake_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 1_600)


def test_synthesize_empty_text(tmp_path):
    result = speech.synthesize("   ", tmp_path / "out.wav")
    assert result.ok is False


def test_synthesize_prefers_silero(monkeypatch, tmp_path):
    out = tmp_path / "out.wav"

    def _silero(text, destination, **k):
        _write_fake_wav(Path(destination))
        return "silero:eugene"

    monkeypatch.setattr(speech, "_silero_available", lambda: True)
    monkeypatch.setattr(speech, "_silero_synthesize_to_wav", _silero)
    monkeypatch.setattr(speech, "_sapi_available", lambda: True)
    monkeypatch.setenv("JARVIS_TTS_ENGINE", "auto")
    result = speech.synthesize("Привет, сэр.", out, style="off")
    assert result.ok is True
    assert result.engine == "silero"
    assert result.voice == "silero:eugene"


def test_synthesize_falls_back_to_sapi(monkeypatch, tmp_path):
    out = tmp_path / "out.wav"

    def _silero_boom(*a, **k):
        raise RuntimeError("torch missing")

    def _sapi(text, destination, **k):
        _write_fake_wav(Path(destination))
        return "Microsoft Irina"

    monkeypatch.setattr(speech, "_silero_available", lambda: True)
    monkeypatch.setattr(speech, "_silero_synthesize_to_wav", _silero_boom)
    monkeypatch.setattr(speech, "_sapi_available", lambda: True)
    monkeypatch.setattr(speech, "_sapi_synthesize_to_wav", _sapi)
    monkeypatch.setenv("JARVIS_TTS_ENGINE", "auto")
    result = speech.synthesize("Привет, сэр.", out, style="off")
    assert result.ok is True
    assert result.engine == "sapi"


def test_synthesize_rejects_header_only_engine_success(monkeypatch, tmp_path):
    out = tmp_path / "out.wav"

    def _sapi(text, destination, **kwargs):
        with wave.open(str(destination), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16_000)
            wav.writeframes(b"\x00\x00")
        return "Microsoft Irina"

    monkeypatch.setattr(speech, "_silero_available", lambda: False)
    monkeypatch.setattr(speech, "_sapi_available", lambda: True)
    monkeypatch.setattr(speech, "_sapi_synthesize_to_wav", _sapi)
    result = speech.synthesize("Привет, сэр.", out, engine="sapi", style="off")

    assert result.ok is False
    assert result.error == "Engine produced no audio."


def test_synthesize_no_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(speech, "_silero_available", lambda: False)
    monkeypatch.setattr(speech, "_sapi_available", lambda: False)
    result = speech.synthesize("Привет.", tmp_path / "out.wav")
    assert result.ok is False


# --------------------------------------------------------------------------- #
# DSP stylizer round-trip (numpy required).
# --------------------------------------------------------------------------- #


def test_stylize_roundtrip(tmp_path):
    np = pytest.importorskip("numpy")
    rate = 22050
    t = np.linspace(0, 1.0, rate, endpoint=False)
    tone = (0.5 * np.sin(2 * np.pi * 220 * t)).astype("float32")
    src = tmp_path / "dry.wav"
    speech._write_wav_mono(src, tone, rate)

    out = tmp_path / "styled.wav"
    speech.stylize_wav(src, out, style="jarvis")
    assert out.exists() and out.stat().st_size > 44

    styled, sr = speech._read_wav_mono(out)
    assert sr == rate
    assert len(styled) > 0
    assert np.all(np.isfinite(styled))
    assert float(np.max(np.abs(styled))) <= 1.0


def test_stylize_radio_style(tmp_path):
    np = pytest.importorskip("numpy")
    rate = 16000
    tone = (0.3 * np.sin(2 * np.pi * 300 * np.arange(rate) / rate)).astype("float32")
    src = tmp_path / "dry.wav"
    speech._write_wav_mono(src, tone, rate)
    out = speech.stylize_wav(src, tmp_path / "radio.wav", style="radio")
    assert Path(out).exists()
