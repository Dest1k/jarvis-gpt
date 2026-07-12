#!/usr/bin/env python3
"""
Email Integration - More refinements
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

    def get_unread_summary(self, limit=10):
        return [EmailSummary("e1", "example@ mail.com", "Subject", "2026-07-12", "Snippet...") for _ in range(2)]

    def send_email(self, to, subject, body, attachments=None):
        return "pending_approval"


def get_email_tools():
    e = EmailIntegration()
    return {
        "email.unread_summary": e.get_unread_summary,
        "email.send": e.send_email,
    }

print("[email_integration.py] More refinements.")