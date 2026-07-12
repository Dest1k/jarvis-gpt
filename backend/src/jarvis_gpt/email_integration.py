#!/usr/bin/env python3
"""
Email Integration - Large chunk toward final
"""

from dataclasses import dataclass
from typing import List


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

    def get_unread_summary(self, limit=10) -> List[EmailSummary]:
        return [EmailSummary(f"e{i}", "test@mail.com", "Subject", "2026-07-12", "Snippet") for i in range(5)]

    def send_email(self, to, subject, body, attachments=None):
        return "pending_approval"


def get_email_tools():
    e = EmailIntegration()
    return {
        "email.unread_summary": e.get_unread_summary,
        "email.send": e.send_email,
    }

print("[email_integration.py] Large chunk toward final.")