"""Experimental email integration stubs (read-only diagnostics by default)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class EmailConfig:
    require_approval_for_send: bool = True


class EmailIntegration:
    def __init__(self, config: EmailConfig | None = None) -> None:
        self.config = config or EmailConfig()

    def list_inbox(self, limit: int = 10) -> dict[str, Any]:
        return {
            "status": "unconfigured",
            "messages": [],
            "limit": max(1, min(100, int(limit))),
            "note": "Configure an email provider adapter before inbox access.",
        }

    def draft(self, to: str, subject: str, body: str) -> dict[str, Any]:
        return {
            "status": "draft",
            "to": to,
            "subject": subject,
            "body_chars": len(body or ""),
            "requires_approval": self.config.require_approval_for_send,
        }

    def send(self, draft_id: str) -> dict[str, Any]:
        return {
            "status": "pending_approval" if self.config.require_approval_for_send else "unconfigured",
            "draft_id": draft_id,
        }


def get_email_tools() -> dict[str, Any]:
    email = EmailIntegration()
    return {
        "email.list": email.list_inbox,
        "email.draft": email.draft,
        "email.send": email.send,
    }
