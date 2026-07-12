#!/usr/bin/env python3
"""
Proactive Briefing - Improved version
"""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Briefing:
    title: str
    content: str
    priority_items: List[str]
    sources: List[str]


class ProactiveBriefing:
    def generate_daily_briefing(self, persona_context: Optional[str] = None) -> Briefing:
        content = "Daily Briefing\n"
        content += "- Upcoming events from calendar\n"
        content += "- Important emails\n"
        content += "- Web watch triggers\n"
        content += "- Key lessons from memory\n"
        content += "[Real synthesis would combine all sources here]"

        return Briefing(
            title="Daily Briefing",
            content=content,
            priority_items=["Review important items", "Focus on key tasks"],
            sources=["calendar", "email", "web", "memory"]
        )


def get_briefing_tools():
    b = ProactiveBriefing()
    return {
        "briefing.daily": b.generate_daily_briefing,
    }

print("[proactive_briefing.py] Improved.")