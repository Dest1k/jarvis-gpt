#!/usr/bin/env python3
"""
Email Integration - Improved version
"""

from dataclasses import dataclass
from typing import List, Optional


class EmailSummary:
    def __init__(self, id, from_addr, subject, date, snippet):
        self.id = id
        self.from_addr = from_addr
        self.subject = subject
        self.date = date
        self.snippet = snippet


@dataclass
class EmailConfig:
    require_approval_for_send: bool = True


class EmailIntegration:
    def __init__(self, config=None):
        self.config = config or EmailConfig()

    def get_unread_summary(self, limit: int = 10) -> List[EmailSummary]:
        return [EmailSummary("eml1", "test@example.com", "[Placeholder] Subject", "2026-07-12", "Summary...")]

    def send_email(self, to, subject, body, attachments=None):
        return "email_pending_approval"


def get_email_tools():
    e = EmailIntegration()
    return {
        "email.unread_summary": e.get_unread_summary,
        "email.send": e.send_email,
    }

print("[email_integration.py] Improved.")