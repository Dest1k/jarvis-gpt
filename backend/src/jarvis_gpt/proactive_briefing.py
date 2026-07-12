"""Experimental proactive briefing builder using local document/memory signals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class ProactiveBriefing:
    def build(self, *, focus: str | None = None, notes: list[str] | None = None) -> dict[str, Any]:
        items = list(notes or [])
        if focus:
            items.insert(0, f"Focus: {focus}")
        if not items:
            items = ["No briefing inputs provided; attach memory/docs to enrich."]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "focus": focus,
            "items": items[:20],
            "status": "draft",
        }


def get_briefing_tools() -> dict[str, Any]:
    briefing = ProactiveBriefing()
    return {
        "briefing.build": briefing.build,
    }
