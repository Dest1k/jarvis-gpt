"""Experimental calendar integration stubs (approval-gated writes)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class CalendarConfig:
    require_approval_for_write: bool = True


class CalendarEvent:
    def __init__(
        self,
        id: str,
        title: str,
        start: datetime,
        end: datetime | None = None,
        location: str | None = None,
    ) -> None:
        self.id = id
        self.title = title
        self.start = start
        self.end = end
        self.location = location

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat() if self.end else None,
            "location": self.location,
        }


class CalendarIntegration:
    def __init__(self, config: CalendarConfig | None = None) -> None:
        self.config = config or CalendarConfig()
        self._events: list[CalendarEvent] = []

    def get_upcoming(self, days: int = 7) -> list[dict[str, Any]]:
        _ = days
        return [event.to_dict() for event in self._events]

    def add_event(self, event: CalendarEvent | dict[str, Any]) -> dict[str, Any]:
        if isinstance(event, dict):
            item = CalendarEvent(
                id=str(event.get("id") or "event"),
                title=str(event.get("title") or "Event"),
                start=datetime.fromisoformat(str(event.get("start")))
                if event.get("start")
                else datetime.now(),
                end=datetime.fromisoformat(str(event["end"])) if event.get("end") else None,
                location=event.get("location"),
            )
        else:
            item = event
        if self.config.require_approval_for_write:
            return {
                "status": "pending_approval",
                "event_id": item.id,
                "title": item.title,
            }
        self._events.append(item)
        return {"status": "added", "event": item.to_dict()}

    def check_conflicts(self, time: Any, duration: int = 60) -> list[dict[str, Any]]:
        _ = time, duration
        return []


def get_calendar_tools() -> dict[str, Any]:
    calendar = CalendarIntegration()
    return {
        "calendar.upcoming": calendar.get_upcoming,
        "calendar.add": calendar.add_event,
        "calendar.check_conflicts": calendar.check_conflicts,
    }
