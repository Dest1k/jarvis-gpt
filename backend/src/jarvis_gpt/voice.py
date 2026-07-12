#!/usr/bin/env python3
"""
Voice Module - Improved version
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class VoiceConfig:
    stt_engine: str = "whisper"
    tts_engine: str = "piper"
    wake_word: str = "jarvis"


class VoiceManager:
    def __init__(self, config=None):
        self.config = config or VoiceConfig()

    def listen(self, duration: float = 5.0) -> str:
        return "[Transcribed voice input placeholder]"

    def speak(self, text: str):
        print(f"[JARVIS] {text[:80]}...")

    def start_full_duplex(self):
        print("Full-duplex voice mode activated")


def get_voice_tools():
    v = VoiceManager()
    return {
        "voice.listen": v.listen,
        "voice.speak": v.speak,
        "voice.start_full_duplex": v.start_full_duplex,
    }

print("[voice.py] Improved.")