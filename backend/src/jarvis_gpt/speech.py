"""Local voice for Jarvis: speech-to-text and text-to-speech.

Two capabilities, both **local-first** and **optional**:

* **STT** — transcribe an audio/video file to text. Default engine is
  ``faster-whisper`` (CTranslate2, CPU int8) which needs no external binary; the
  legacy ``whisper`` CLI stays as a fallback. The engine is pluggable via
  ``JARVIS_STT_BACKEND`` so an OpenVINO/NPU backend can slot in later without
  touching callers.
* **TTS** — synthesize speech to a WAV file. The default ``auto`` route prefers
  local Silero ``v5_5_ru`` with the ``aidar`` speaker and keeps Windows SAPI5 as
  an availability fallback.

Every heavy dependency is imported lazily inside a ``try``. When a backend is
missing the ``*_status`` helpers report it and callers degrade to text-only —
exactly like the pre-existing ``_media_transcription_status`` hook did for the
whisper CLI. Nothing here is a hard dependency of the runtime.
"""

from __future__ import annotations

import html
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Audio/video containers we accept for transcription. Mirrors the media set the
# legacy whisper hook advertised; kept here so speech.py is self-contained.
AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wma"}
)
VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"})
TRANSCRIBE_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

_DEFAULT_STT_MODEL = "small"
_DEFAULT_STT_DEVICE = "cpu"
_DEFAULT_STT_COMPUTE = "int8"
_MAX_TRANSCRIPT_CHARS = 20000
_DEFAULT_TTS_TEMPO = 1.08
_MIN_TTS_TEMPO = 0.5
_MAX_TTS_TEMPO = 2.0

# Loaded faster-whisper models are expensive to build; cache per (model, device,
# compute) so repeated turns reuse the warm model.
_WHISPER_MODELS: dict[tuple[str, str, str], Any] = {}
_MAX_CACHED_STT_MODELS = 2


def _lru_evict(cache: dict, max_size: int) -> None:
    while len(cache) > max_size:
        cache.pop(next(iter(cache)), None)


@dataclass
class TranscriptResult:
    """Outcome of a speech-to-text run."""

    ok: bool
    text: str = ""
    language: str | None = None
    duration_sec: float | None = None
    engine: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "language": self.language,
            "duration_sec": self.duration_sec,
            "engine": self.engine,
            "error": self.error,
        }


@dataclass
class SpeechResult:
    """Outcome of a text-to-speech run."""

    ok: bool
    path: Path | None = None
    engine: str | None = None
    voice: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": str(self.path) if self.path else None,
            "engine": self.engine,
            "voice": self.voice,
            "error": self.error,
            **self.extra,
        }


# --------------------------------------------------------------------------- #
# Config resolution (env with sane defaults; callers may override per call).
# --------------------------------------------------------------------------- #


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def stt_model_name() -> str:
    return _env("JARVIS_STT_MODEL", _DEFAULT_STT_MODEL)


def stt_device() -> str:
    return _env("JARVIS_STT_DEVICE", _DEFAULT_STT_DEVICE)


def stt_compute_type() -> str:
    return _env("JARVIS_STT_COMPUTE", _DEFAULT_STT_COMPUTE)


def stt_backend() -> str:
    """Selected STT engine: ``faster-whisper`` (default), ``whisper-cli`` or ``auto``."""

    return _env("JARVIS_STT_BACKEND", "auto")


def tts_voice_hint() -> str:
    """Substring used to pick a SAPI voice; defaults to the Russian 'Irina'."""

    return _env("JARVIS_TTS_VOICE", "irina")


def tts_engine() -> str:
    """Preferred TTS engine: ``silero`` (neural, default), ``sapi`` or ``auto``."""

    return _env("JARVIS_TTS_ENGINE", "auto").lower()


def tts_style() -> str:
    """Post-processing style: ``clean`` (default), ``jarvis`` or ``radio``."""

    return _env("JARVIS_TTS_STYLE", "clean").lower()


def silero_speaker() -> str:
    """Silero speaker; defaults to the natural male ``aidar`` voice."""

    return _env("JARVIS_TTS_SILERO_SPEAKER", "aidar")


def silero_package() -> str:
    """Silero Russian model package used for synthesis."""

    return _env("JARVIS_TTS_SILERO_PACKAGE", "v5_5_ru")


def silero_sample_rate() -> int:
    try:
        return int(_env("JARVIS_TTS_SILERO_RATE", "48000"))
    except ValueError:
        return 48000


def tts_tempo() -> float:
    """Pitch-preserving playback tempo requested for synthesized speech."""

    try:
        value = float(_env("JARVIS_TTS_TEMPO", str(_DEFAULT_TTS_TEMPO)))
    except ValueError:
        return _DEFAULT_TTS_TEMPO
    if not math.isfinite(value) or not (_MIN_TTS_TEMPO <= value <= _MAX_TTS_TEMPO):
        return _DEFAULT_TTS_TEMPO
    return value


# --------------------------------------------------------------------------- #
# Capability probing.
# --------------------------------------------------------------------------- #


def _faster_whisper_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("faster_whisper") is not None


def _whisper_cli_path() -> str | None:
    return shutil.which("whisper")


def _sapi_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("comtypes") is not None


def _silero_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("torch") is not None


def stt_status() -> dict[str, Any]:
    """Report which STT engine (if any) is usable, without loading a model."""

    faster = _faster_whisper_available()
    cli = _whisper_cli_path()
    backend = stt_backend()
    if backend == "faster-whisper":
        engine = "faster-whisper" if faster else None
    elif backend == "whisper-cli":
        engine = "whisper-cli" if cli else None
    else:  # auto — prefer the in-process lib
        engine = "faster-whisper" if faster else ("whisper-cli" if cli else None)
    return {
        "available": engine is not None,
        "engine": engine,
        "faster_whisper": faster,
        "whisper_cli": bool(cli),
        "model": stt_model_name() if faster else None,
        "device": stt_device(),
        "compute_type": stt_compute_type(),
        "supported_extensions": sorted(TRANSCRIBE_EXTENSIONS),
    }


def tts_status() -> dict[str, Any]:
    """Report whether TTS is usable and which engine/voice/style would be picked."""

    sapi = _sapi_available()
    silero = _silero_available()
    voices: list[str] = []
    picked: str | None = None
    if sapi:
        try:
            voices = _sapi_voice_names()
            picked = _pick_sapi_voice_name(voices, tts_voice_hint())
        except Exception:  # noqa: BLE001 — probing must never raise
            voices = []
    preference = tts_engine()
    if preference == "silero":
        engine = "silero" if silero else None
    elif preference == "sapi":
        engine = "sapi" if (sapi and voices) else None
    else:  # auto — prefer the neural engine, fall back to SAPI
        engine = "silero" if silero else ("sapi" if (sapi and voices) else None)
    return {
        "available": engine is not None,
        "engine": engine,
        "engine_preference": preference,
        "style": tts_style(),
        "tempo": tts_tempo(),
        "silero": silero,
        "silero_package": silero_package() if silero else None,
        "silero_speaker": silero_speaker() if silero else None,
        "sapi": bool(sapi and voices),
        "voices": voices,
        "voice": picked,
        "voice_hint": tts_voice_hint(),
    }


def voice_status() -> dict[str, Any]:
    return {"stt": stt_status(), "tts": tts_status()}


# --------------------------------------------------------------------------- #
# Speech-to-text.
# --------------------------------------------------------------------------- #


def transcribe(
    path: str | Path,
    *,
    language: str = "auto",
    model_size: str | None = None,
    device: str | None = None,
    compute_type: str | None = None,
    max_chars: int = _MAX_TRANSCRIPT_CHARS,
    backend: str | None = None,
) -> TranscriptResult:
    """Transcribe an audio/video file to text.

    Chooses the engine from ``backend`` (or ``JARVIS_STT_BACKEND``): the in-process
    ``faster-whisper`` by default, falling back to the ``whisper`` CLI. Never
    raises — a failure is returned as ``TranscriptResult(ok=False, error=...)``.
    """

    media = Path(path)
    if not media.exists() or not media.is_file():
        return TranscriptResult(ok=False, error="Audio file does not exist.")
    if media.suffix.lower() not in TRANSCRIBE_EXTENSIONS:
        return TranscriptResult(
            ok=False, error="Unsupported media extension for transcription."
        )

    chosen = (backend or stt_backend()).strip().lower()
    order: list[str]
    if chosen == "faster-whisper":
        order = ["faster-whisper"]
    elif chosen == "whisper-cli":
        order = ["whisper-cli"]
    else:  # auto
        order = ["faster-whisper", "whisper-cli"]

    last_error = "No speech-to-text engine is available."
    for engine in order:
        if engine == "faster-whisper" and _faster_whisper_available():
            try:
                return _transcribe_faster_whisper(
                    media,
                    language=language,
                    model_size=model_size or stt_model_name(),
                    device=device or stt_device(),
                    compute_type=compute_type or stt_compute_type(),
                    max_chars=max_chars,
                )
            except Exception as exc:  # noqa: BLE001 — fall through to the next engine
                last_error = f"faster-whisper failed: {exc}"
        elif engine == "whisper-cli" and _whisper_cli_path():
            try:
                text = _transcribe_whisper_cli(
                    media, language=language, max_chars=max_chars
                )
                return TranscriptResult(
                    ok=bool(text.strip()),
                    text=text,
                    language=None if language == "auto" else language,
                    engine="whisper-cli",
                )
            except Exception as exc:  # noqa: BLE001
                last_error = f"whisper CLI failed: {exc}"
    return TranscriptResult(ok=False, error=last_error)


def _load_whisper_model(model_size: str, device: str, compute_type: str) -> Any:
    key = (model_size, device, compute_type)
    model = _WHISPER_MODELS.get(key)
    if model is None:
        from faster_whisper import WhisperModel  # lazy — optional dependency

        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        _lru_evict(_WHISPER_MODELS, _MAX_CACHED_STT_MODELS)
        _WHISPER_MODELS[key] = model
    return model


def _transcribe_faster_whisper(
    media: Path,
    *,
    language: str,
    model_size: str,
    device: str,
    compute_type: str,
    max_chars: int,
) -> TranscriptResult:
    model = _load_whisper_model(model_size, device, compute_type)
    lang = None if language in ("", "auto") else language
    segments, info = model.transcribe(str(media), language=lang, vad_filter=True)
    parts: list[str] = []
    total = 0
    for segment in segments:
        chunk = (segment.text or "").strip()
        if not chunk:
            continue
        parts.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            break
    text = " ".join(parts).strip()[:max_chars]
    detected = getattr(info, "language", None) or lang
    duration = getattr(info, "duration", None)
    return TranscriptResult(
        ok=bool(text),
        text=text,
        language=detected,
        duration_sec=float(duration) if duration is not None else None,
        engine=f"faster-whisper:{model_size}",
    )


def _transcribe_whisper_cli(media: Path, *, language: str, max_chars: int) -> str:
    whisper = shutil.which("whisper")
    if not whisper:
        raise ValueError("whisper CLI not found")
    with tempfile.TemporaryDirectory(prefix="jarvis-whisper-") as tmp_dir:
        command = [
            whisper,
            str(media),
            "--output_format",
            "txt",
            "--output_dir",
            tmp_dir,
            "--fp16",
            "False",
        ]
        if language and language != "auto":
            command.extend(["--language", language])
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        if result.returncode != 0:
            raise subprocess.SubprocessError(
                (result.stderr or result.stdout or "whisper failed")[:500]
            )
        output_path = Path(tmp_dir) / f"{media.stem}.txt"
        if not output_path.exists():
            candidates = sorted(Path(tmp_dir).glob("*.txt"))
            output_path = candidates[0] if candidates else output_path
        text = (
            output_path.read_text(encoding="utf-8", errors="replace")
            if output_path.exists()
            else ""
        )
    return text.strip()[:max_chars]


# --------------------------------------------------------------------------- #
# Text-to-speech (Windows SAPI5 via comtypes).
# --------------------------------------------------------------------------- #

_SSFM_CREATE_FOR_WRITE = 3  # SpeechLib SpeechStreamFileMode


def _sapi_voice_tokens() -> list[Any]:
    import comtypes.client  # lazy — optional dependency

    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    tokens = voice.GetVoices()
    return [tokens.Item(i) for i in range(tokens.Count)]


def _sapi_voice_names() -> list[str]:
    names: list[str] = []
    for token in _sapi_voice_tokens():
        try:
            names.append(token.GetDescription())
        except Exception:  # noqa: BLE001
            continue
    return names


def _pick_sapi_voice_name(names: list[str], hint: str) -> str | None:
    if not names:
        return None
    hint = (hint or "").strip().lower()
    if hint:
        for name in names:
            if hint in name.lower():
                return name
    return names[0]


def _wav_has_playable_duration(path: Path, *, minimum_seconds: float = 0.05) -> bool:
    """Reject header-only/truncated renders that audio transports cannot play."""

    import wave

    try:
        with wave.open(str(path), "rb") as wav:
            rate = wav.getframerate()
            frames = wav.getnframes()
            return bool(
                rate > 0
                and frames >= max(1, int(rate * minimum_seconds))
                and wav.getnchannels() > 0
                and wav.getsampwidth() > 0
            )
    except (EOFError, OSError, wave.Error):
        return False


def synthesize(
    text: str,
    out_path: str | Path,
    *,
    voice: str | None = None,
    rate: int | None = None,
    engine: str | None = None,
    style: str | None = None,
) -> SpeechResult:
    """Render ``text`` to a WAV at ``out_path`` and apply the requested style.

    Engine order comes from ``engine`` (or ``JARVIS_TTS_ENGINE``): the neural
    ``silero`` (male Russian by default) preferred, falling back to Windows
    ``sapi``. The optional ``style`` post-processor (``JARVIS_TTS_STYLE``) can
    shape the dry render; the default ``clean`` mode preserves a human timbre.
    Never raises.
    """

    text = (text or "").strip()
    if not text:
        return SpeechResult(ok=False, error="No text to synthesize.")
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    preference = (engine or tts_engine()).strip().lower()
    if preference == "silero":
        order = ["silero"]
    elif preference == "sapi":
        order = ["sapi"]
    else:  # auto — neural first, SAPI as the always-present safety net
        order = ["silero", "sapi"]

    last_error = "No text-to-speech engine is available."
    used_engine: str | None = None
    used_voice: str | None = None
    for eng in order:
        if eng == "silero" and _silero_available():
            try:
                used_voice = _silero_synthesize_to_wav(
                    text,
                    destination,
                    speaker=voice or silero_speaker(),
                    sample_rate=silero_sample_rate(),
                )
                used_engine = "silero"
                break
            except Exception as exc:  # noqa: BLE001 — fall back to the next engine
                last_error = f"Silero synthesis failed: {exc}"
        elif eng == "sapi" and _sapi_available():
            try:
                used_voice = _sapi_synthesize_to_wav(
                    text,
                    destination,
                    voice_hint=voice if voice is not None else tts_voice_hint(),
                    rate=rate,
                )
                used_engine = "sapi"
                break
            except Exception as exc:  # noqa: BLE001
                last_error = f"SAPI synthesis failed: {exc}"
    if used_engine is None:
        return SpeechResult(ok=False, error=last_error)
    if not destination.exists() or not _wav_has_playable_duration(destination):
        return SpeechResult(
            ok=False, engine=used_engine, voice=used_voice, error="Engine produced no audio."
        )

    style_name = (style if style is not None else tts_style()).strip().lower()
    styled = False
    if style_name not in ("", "off", "none", "clean", "dry"):
        try:
            stylize_wav(destination, destination, style=style_name)
            styled = True
        except Exception:  # noqa: BLE001 — styling is a nicety; keep the dry render
            styled = False
    requested_tempo = tts_tempo()
    tempo_applied = False
    if not math.isclose(requested_tempo, 1.0, abs_tol=1e-6):
        try:
            tempo_applied = _apply_wav_tempo(destination, requested_tempo)
        except Exception:  # noqa: BLE001 - preserve the verified original WAV
            tempo_applied = False
    if not _wav_has_playable_duration(destination):
        return SpeechResult(
            ok=False,
            engine=used_engine,
            voice=used_voice,
            error="Audio post-processing produced no playable output.",
        )
    return SpeechResult(
        ok=True,
        path=destination,
        engine=used_engine,
        voice=used_voice,
        extra={
            "bytes": destination.stat().st_size,
            "style": style_name if styled else "clean",
            "styled": styled,
            "tempo": requested_tempo if tempo_applied else 1.0,
            "tempo_requested": requested_tempo,
            "tempo_applied": tempo_applied,
        },
    )


def _sapi_synthesize_to_wav(
    text: str, destination: Path, *, voice_hint: str, rate: int | None
) -> str | None:
    import comtypes.client  # lazy

    speaker = comtypes.client.CreateObject("SAPI.SpVoice")
    picked: str | None = None
    if voice_hint:
        tokens = speaker.GetVoices()
        for i in range(tokens.Count):
            token = tokens.Item(i)
            try:
                description = token.GetDescription()
            except Exception:  # noqa: BLE001
                continue
            if voice_hint.lower() in description.lower():
                speaker.Voice = token
                picked = description
                break
    if picked is None:
        try:
            picked = speaker.Voice.GetDescription()
        except Exception:  # noqa: BLE001
            picked = None
    if rate is not None:
        # SAPI rate is -10..10; clamp defensively.
        speaker.Rate = max(-10, min(10, int(rate)))
    stream = comtypes.client.CreateObject("SAPI.SpFileStream")
    stream.Open(str(destination), _SSFM_CREATE_FOR_WRITE, False)
    try:
        speaker.AudioOutputStream = stream
        speaker.Speak(text)
    finally:
        stream.Close()
    return picked


# --------------------------------------------------------------------------- #
# Neural TTS (Silero, CPU) — a warmer male Russian voice than SAPI.
# --------------------------------------------------------------------------- #

_SILERO_MODELS: dict[tuple[str, str], Any] = {}
_MAX_CACHED_TTS_MODELS = 2
_SILERO_CHAR_LIMIT = 900
_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _load_silero_model(language: str = "ru", package: str | None = None) -> Any:
    package = package or silero_package()
    key = (language, package)
    model = _SILERO_MODELS.get(key)
    if model is None:
        import torch  # lazy — optional dependency

        model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-models",
            model="silero_tts",
            language=language,
            speaker=package,
            trust_repo=True,
        )
        model.to("cpu")
        _lru_evict(_SILERO_MODELS, _MAX_CACHED_TTS_MODELS)
        _SILERO_MODELS[key] = model
    return model


def _split_for_tts(text: str, limit: int) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(current) + len(sentence) + 1 <= limit:
            current = f"{current} {sentence}".strip()
            continue
        if current:
            chunks.append(current)
        if len(sentence) <= limit:
            current = sentence
        else:  # a single very long sentence — hard split
            for i in range(0, len(sentence), limit):
                chunks.append(sentence[i : i + limit])
            current = ""
    if current:
        chunks.append(current)
    return chunks


def _normalize_silero_text(text: str) -> str:
    """Flatten presentation markup that Silero's Russian normalizer cannot tokenize."""

    value = unicodedata.normalize("NFKC", html.unescape(str(text or "")))
    value = _MARKDOWN_IMAGE_RE.sub(lambda match: match.group(1) or "изображение", value)
    value = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1), value)
    value = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", value)
    value = re.sub(r"(?m)^\s{0,3}>\s?", "", value)
    value = re.sub(r"(?m)^\s{0,3}[-+*]\s+", "", value)
    for marker in ("```", "**", "__", "~~", "`"):
        value = value.replace(marker, "")
    value = value.replace("*", " ")
    value = (
        value.replace("\u00a0", " ")
        .replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("\u2212", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2026", "...")
    )
    value = "".join(
        character
        for character in value
        if character in "\n\t" or not unicodedata.category(character).startswith("C")
    )
    return " ".join(value.split())


def _silero_apply_tts(
    model: Any,
    text: str,
    *,
    speaker: str,
    sample_rate: int,
) -> Any:
    kwargs = {
        "speaker": speaker,
        "sample_rate": sample_rate,
        "put_accent": True,
        "put_yo": True,
    }
    try:
        return model.apply_tts(text=text, **kwargs)
    except (KeyError, ValueError) as original_error:
        normalized = _normalize_silero_text(text)
        if not normalized or normalized == text:
            raise
        try:
            return model.apply_tts(text=normalized, **kwargs)
        except Exception as retry_error:
            raise retry_error from original_error


def _silero_synthesize_to_wav(
    text: str, destination: Path, *, speaker: str, sample_rate: int
) -> str:
    import numpy as np

    model = _load_silero_model()
    valid = set(getattr(model, "speakers", []) or [])
    chosen = speaker if speaker in valid else silero_speaker()
    if chosen not in valid:
        chosen = "aidar" if "aidar" in valid else (sorted(valid)[0] if valid else "aidar")

    parts: list[Any] = []
    for chunk in _split_for_tts(text, _SILERO_CHAR_LIMIT):
        wav = _silero_apply_tts(
            model,
            chunk,
            speaker=chosen,
            sample_rate=sample_rate,
        )
        arr = wav.detach().cpu().numpy().astype("float32")
        if not arr.size:
            raise ValueError("Silero produced no audio for a text fragment.")
        parts.append(arr)
    if not parts:
        raise ValueError("Silero produced no audio for the given text.")
    audio = np.concatenate(parts)
    _write_wav_mono(destination, audio, sample_rate)
    return f"silero:{chosen}"


# --------------------------------------------------------------------------- #
# WAV I/O + DSP stylizer (numpy only, no external audio deps).
# --------------------------------------------------------------------------- #


def _apply_wav_tempo(path: str | Path, tempo: float) -> bool:
    """Apply FFmpeg ``atempo`` atomically; keep the original WAV on any failure."""

    source = Path(path)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or math.isclose(tempo, 1.0, abs_tol=1e-6):
        return False
    with tempfile.TemporaryDirectory(prefix="jarvis-tempo-", dir=str(source.parent)) as tmp:
        rendered = Path(tmp) / "tempo.wav"
        try:
            process = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-filter:a",
                    f"atempo={tempo:.4f}",
                    "-c:a",
                    "pcm_s16le",
                    str(rendered),
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if process.returncode != 0 or not _wav_has_playable_duration(rendered):
            return False
        os.replace(rendered, source)
    return True


def _read_wav_mono(path: str | Path) -> tuple[Any, int]:
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {width}")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def _write_wav_mono(path: str | Path, samples: Any, rate: int) -> None:
    import wave

    import numpy as np

    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(rate))
        wf.writeframes(pcm.tobytes())


def _fft_convolve(signal: Any, impulse: Any) -> Any:
    import numpy as np

    n = len(signal) + len(impulse) - 1
    nfft = 1 << (n - 1).bit_length()
    spectrum = np.fft.rfft(signal, nfft) * np.fft.rfft(impulse, nfft)
    return np.fft.irfft(spectrum, nfft)[:n]


def _apply_eq(x: Any, rate: int, points: list[tuple[float, float]]) -> Any:
    import numpy as np

    n = len(x)
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1.0 / rate)
    fpts = np.log10(np.array([max(p[0], 1.0) for p in points], dtype=np.float64))
    gpts = np.array([p[1] for p in points], dtype=np.float64)
    gain_db = np.interp(np.log10(np.maximum(freqs, 1.0)), fpts, gpts, left=gpts[0], right=gpts[-1])
    return np.fft.irfft(spectrum * (10.0 ** (gain_db / 20.0)), n=n)


def _reverb(
    x: Any, rate: int, *, decay: float = 0.4, predelay_ms: float = 28.0, wet: float = 0.2
) -> Any:
    import numpy as np

    length = int(decay * rate)
    if length < 8 or wet <= 0:
        return x
    rng = np.random.default_rng(1234)  # deterministic IR → reproducible output
    envelope = np.exp(-np.linspace(0.0, 6.0, length))
    impulse = rng.standard_normal(length) * envelope
    predelay = int(predelay_ms * 0.001 * rate)
    if predelay > 0:
        impulse = np.concatenate([np.zeros(predelay, dtype=np.float64), impulse])
    impulse /= np.sqrt(np.sum(impulse**2)) + 1e-9
    wet_signal = _fft_convolve(x, impulse)[: len(x)]
    rms_x = np.sqrt(np.mean(x**2)) + 1e-9
    rms_w = np.sqrt(np.mean(wet_signal**2)) + 1e-9
    wet_signal *= rms_x / rms_w
    return (1.0 - wet) * x + wet * wet_signal


def _comb(x: Any, rate: int, *, delay_ms: float = 6.5, gain: float = 0.18) -> Any:
    import numpy as np

    delay = int(delay_ms * 0.001 * rate)
    if delay <= 0:
        return x
    y = np.array(x, dtype=np.float64, copy=True)
    y[delay:] += gain * x[:-delay]
    return y


def _saturate(x: Any, drive: float = 1.5) -> Any:
    import numpy as np

    return np.tanh(x * drive) / np.tanh(drive)


def stylize_wav(in_path: str | Path, out_path: str | Path, *, style: str = "jarvis") -> Path:
    """Shape a dry TTS WAV into a stylized voice. Pure numpy; writes 16-bit mono."""

    import numpy as np

    x, rate = _read_wav_mono(in_path)
    if len(x) == 0:
        return Path(out_path)
    x = x - np.mean(x)
    x = x / (np.max(np.abs(x)) + 1e-9) * 0.9

    style = (style or "jarvis").lower()
    if style == "radio":  # narrow-band comms voice
        x = _apply_eq(x, rate, [(80, -24), (300, 2), (1000, 3), (2500, 4), (3800, 2), (5200, -20)])
        x = _saturate(x, 2.4)
        x = _comb(x, rate, delay_ms=3.0, gain=0.15)
        x = _reverb(x, rate, decay=0.18, predelay_ms=12.0, wet=0.10)
    else:  # "jarvis" — warm, present, faint hall + metallic sheen
        x = _apply_eq(
            x, rate, [(60, -8), (120, 2), (300, 0), (1500, 1.5), (3000, 3), (6000, 2), (11000, 1)]
        )
        x = _comb(x, rate, delay_ms=6.5, gain=0.18)
        x = _reverb(x, rate, decay=0.40, predelay_ms=28.0, wet=0.22)
        x = _saturate(x, 1.5)

    x = x - np.mean(x)
    x = x / (np.max(np.abs(x)) + 1e-9) * 0.95
    _write_wav_mono(out_path, x, rate)
    return Path(out_path)
