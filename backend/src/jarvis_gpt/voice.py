"""Experimental voice helpers (host Whisper detection only)."""

from __future__ import annotations

import shutil
from typing import Any


class VoiceManager:
    def status(self) -> dict[str, Any]:
        whisper = shutil.which("whisper")
        return {
            "whisper_available": bool(whisper),
            "whisper_path": whisper,
            "note": "Transcription requires host Whisper; not auto-enabled in core runtime.",
        }

    def transcribe(self, path: str) -> dict[str, Any]:
        status = self.status()
        if not status["whisper_available"]:
            return {
                "ok": False,
                "path": path,
                "error": "whisper binary not found in PATH",
            }
        return {
            "ok": False,
            "path": path,
            "error": "Voice transcription adapter is experimental and not wired for auto-run.",
        }


def get_voice_tools() -> dict[str, Any]:
    voice = VoiceManager()
    return {
        "voice.status": voice.status,
        "voice.transcribe": voice.transcribe,
    }
