"""Tests for the local voice module (speech-to-text + text-to-speech).

Heavy engines (faster-whisper, torch/Silero, comtypes/SAPI, numpy) are optional and
imported lazily, so these tests monkeypatch the availability probes and engine
callables to exercise selection/degradation logic without a GPU or any model.
"""

from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from jarvis_gpt import speech

# --------------------------------------------------------------------------- #
# Explicit read-aloud commands.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "Озвучь этот текст: Сохрани: знаки!\nИ перенос строки.",
            "Сохрани: знаки!\nИ перенос строки.",
        ),
        ("Пожалуйста, зачитай — Текст «как есть».", "Текст «как есть»."),
        ("прочитай вслух:\nСтрока 1\nСтрока 2", "Строка 1\nСтрока 2"),
        ("Произнесите следующий текст:  один пробел", " один пробел"),
        ("Озвучьте текст, пожалуйста:\nТочно.", "Точно."),
        (
            "Прочти мне, пожалуйста, этот текст ниже: Дословно.",
            "Дословно.",
        ),
    ],
)
def test_extract_explicit_read_aloud_text_preserves_literal_body(command, expected):
    assert speech.extract_explicit_read_aloud_text(command) == expected
    assert speech.requests_explicit_read_aloud(command) is True


@pytest.mark.parametrize(
    "text",
    [
        "Не озвучивай этот текст: секрет",
        "Почему ты озвучил этот текст?",
        "Можешь озвучить этот текст?",
        "Озвучь этот текст?",
        "Я прошу: озвучь этот текст: пример",
        "Прочитай документ и перескажи его",
        "Зачитай:",
    ],
)
def test_explicit_read_aloud_rejects_negations_questions_and_missing_body(text):
    assert speech.extract_explicit_read_aloud_text(text) is None
    assert speech.requests_explicit_read_aloud(text) is False


def test_extract_explicit_read_aloud_text_has_no_length_based_truncation():
    body = ("Абзац со знаками: один, два, три.\n" * 700).rstrip()

    assert speech.extract_explicit_read_aloud_text(f"Озвучь этот текст:\n{body}") == body


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
    assert status["silero_package"] == "v5_5_ru"
    assert status["silero_speaker"] == "aidar"
    assert status["style"] == "clean"


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
    speakers: list[str] = []

    def _silero(text, destination, **k):
        speakers.append(k["speaker"])
        _write_fake_wav(Path(destination))
        return f"silero:{k['speaker']}"

    monkeypatch.setattr(speech, "_silero_available", lambda: True)
    monkeypatch.setattr(speech, "_silero_synthesize_to_wav", _silero)
    monkeypatch.setattr(speech, "_sapi_available", lambda: True)
    monkeypatch.setenv("JARVIS_TTS_ENGINE", "auto")
    result = speech.synthesize("Привет, сэр.", out, style="off")
    assert result.ok is True
    assert result.engine == "silero"
    assert result.voice == "silero:aidar"
    assert speakers == ["aidar"]


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


# --------------------------------------------------------------------------- #
# Silero input normalization and pitch-preserving tempo.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["invalid", "nan", "inf", "0.49", "2.01"])
def test_tts_tempo_rejects_invalid_or_unsafe_values(monkeypatch, value):
    monkeypatch.setenv("JARVIS_TTS_TEMPO", value)
    assert speech.tts_tempo() == pytest.approx(1.08)


@pytest.mark.parametrize("value", ["0.5", "1", "1.12", "2.0"])
def test_tts_tempo_accepts_bounded_values(monkeypatch, value):
    monkeypatch.setenv("JARVIS_TTS_TEMPO", value)
    assert speech.tts_tempo() == pytest.approx(float(value))


def test_tts_status_reports_default_tempo(monkeypatch):
    monkeypatch.setattr(speech, "_silero_available", lambda: True)
    monkeypatch.setattr(speech, "_sapi_available", lambda: False)
    monkeypatch.delenv("JARVIS_TTS_TEMPO", raising=False)
    assert speech.tts_status()["tempo"] == pytest.approx(1.08)


_FORMATTED_VOICE_LIST_REPLY = (
    "Принято. Прошу прощения за задержку. Сегодня на компьютер поступило три "
    "голосовых сообщения: 1. **voice.ogg** — 23 июля, 10:03 (МСК) 2. "
    "**voice.ogg** — 23 июля, 15:22 (МСК) 3. **voice.ogg** — 23 июля, 18:05 "
    "(МСК) Также сегодня утром (в 10:03) было зафиксировано поступление еще "
    "одного голосового сообщения (около 09:03 по Москве)."
)


def test_formatted_voice_list_stays_on_silero_aidar(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")
    calls: list[str] = []

    class FakeTensor:
        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return np.full(4_800, 0.1, dtype="float32")

    class FakeModel:
        speakers = ["aidar"]

        def apply_tts(self, *, text, **kwargs):
            calls.append(text)
            if "**" in text:
                raise KeyError("v")
            return FakeTensor()

    monkeypatch.setattr(speech, "_load_silero_model", lambda: FakeModel())
    monkeypatch.setattr(speech, "_silero_available", lambda: True)
    monkeypatch.setattr(
        speech,
        "_sapi_synthesize_to_wav",
        lambda *args, **kwargs: pytest.fail("recoverable markup must not switch to SAPI"),
    )
    monkeypatch.setenv("JARVIS_TTS_ENGINE", "auto")
    monkeypatch.setenv("JARVIS_TTS_TEMPO", "1.0")

    output = tmp_path / "formatted.wav"
    result = speech.synthesize(_FORMATTED_VOICE_LIST_REPLY, output, style="off")

    assert len(_FORMATTED_VOICE_LIST_REPLY) == 330
    assert result.ok is True
    assert result.engine == "silero"
    assert result.voice == "silero:aidar"
    assert calls[0] == _FORMATTED_VOICE_LIST_REPLY
    assert "**" not in calls[1]
    assert calls[1].count("voice.ogg") == 3
    with wave.open(str(output), "rb") as rendered:
        assert rendered.getframerate() == 48_000
        assert rendered.getnframes() > 0


def test_silero_does_not_silently_drop_a_failed_chunk(monkeypatch, tmp_path):
    np = pytest.importorskip("numpy")

    class FakeTensor:
        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return np.ones(1_000, dtype="float32")

    class FakeModel:
        speakers = ["aidar"]

        def apply_tts(self, *, text, **kwargs):
            if text == "bad":
                raise RuntimeError("render failed")
            return FakeTensor()

    monkeypatch.setattr(speech, "_load_silero_model", lambda: FakeModel())
    monkeypatch.setattr(speech, "_split_for_tts", lambda text, limit: ["good", "bad"])
    output = tmp_path / "partial.wav"

    with pytest.raises(RuntimeError, match="render failed"):
        speech._silero_synthesize_to_wav(
            "good bad",
            output,
            speaker="aidar",
            sample_rate=48_000,
        )
    assert not output.exists()


def test_apply_wav_tempo_failure_preserves_original(monkeypatch, tmp_path):
    source = tmp_path / "source.wav"
    _write_fake_wav(source)
    original = source.read_bytes()
    monkeypatch.setattr(speech.shutil, "which", lambda name: "ffmpeg")
    monkeypatch.setattr(
        speech.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    assert speech._apply_wav_tempo(source, 1.08) is False
    assert source.read_bytes() == original


def test_apply_wav_tempo_uses_pitch_preserving_filter(monkeypatch, tmp_path):
    source = tmp_path / "source.wav"
    _write_fake_wav(source)
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        _write_fake_wav(Path(command[-1]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(speech.shutil, "which", lambda name: "ffmpeg")
    monkeypatch.setattr(speech.subprocess, "run", fake_run)

    assert speech._apply_wav_tempo(source, 1.08) is True
    assert "-filter:a" in commands[0]
    assert "atempo=1.0800" in commands[0]
    assert speech._wav_has_playable_duration(source)


def test_real_atempo_shortens_wav_without_pitch_shift(tmp_path):
    if speech.shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    np = pytest.importorskip("numpy")
    rate = 48_000
    seconds = 5
    timeline = np.arange(rate * seconds, dtype="float32") / rate
    tone = (0.25 * np.sin(2 * np.pi * 440 * timeline)).astype("float32")
    source = tmp_path / "tone.wav"
    speech._write_wav_mono(source, tone, rate)

    assert speech._apply_wav_tempo(source, 1.08) is True
    rendered, rendered_rate = speech._read_wav_mono(source)
    duration = len(rendered) / rendered_rate
    zero_crossings = np.count_nonzero(np.diff(np.signbit(rendered)))
    estimated_frequency = zero_crossings / (2 * duration)

    assert rendered_rate == rate
    assert duration == pytest.approx(seconds / 1.08, rel=0.02)
    assert estimated_frequency == pytest.approx(440, rel=0.02)
