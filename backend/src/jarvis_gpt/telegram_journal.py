from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


def telegram_message_log_id(realm_id: str, source_key: str) -> str:
    digest = hashlib.sha256(f"{realm_id}:{source_key}".encode()).hexdigest()
    return f"tglog_{digest[:32]}"


def telegram_message_created_at(message: Mapping[str, Any]) -> str:
    timestamp = message.get("date")
    if isinstance(timestamp, int) and not isinstance(timestamp, bool) and timestamp > 0:
        return datetime.fromtimestamp(timestamp, UTC).isoformat()
    return datetime.now(UTC).isoformat()


def record_telegram_outbound(
    conn: sqlite3.Connection,
    *,
    realm_id: str,
    chat_id: int,
    text: str,
    telegram_message: Mapping[str, Any],
    sender_kind: str = "bot",
    content_type: str = "text",
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Append a successfully delivered Bot API message to the shared transport log."""

    telegram_message_id = telegram_message.get("message_id")
    if isinstance(telegram_message_id, bool) or not isinstance(
        telegram_message_id, int
    ):
        return False
    if sender_kind not in {"bot", "operator"}:
        raise ValueError("Invalid outbound Telegram sender kind")
    has_bindings = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'telegram_conversations'"
    ).fetchone()
    binding = (
        conn.execute(
            """
            SELECT conversation_id, user_id FROM telegram_conversations
            WHERE realm_id = ? AND chat_id = ?
            """,
            (realm_id, chat_id),
        ).fetchone()
        if has_bindings is not None
        else None
    )
    source_key = f"out:{chat_id}:{telegram_message_id}"
    cursor = conn.execute(
        """
        INSERT INTO telegram_message_log(
            id, realm_id, chat_id, direction, sender_kind, source_key,
            telegram_message_id, conversation_id, user_id, content,
            content_type, metadata, created_at
        ) VALUES (?, ?, ?, 'outbound', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(realm_id, source_key) DO NOTHING
        """,
        (
            telegram_message_log_id(realm_id, source_key),
            realm_id,
            chat_id,
            sender_kind,
            source_key,
            telegram_message_id,
            str(binding[0]) if binding is not None else None,
            str(binding[1])
            if binding is not None and binding[1] is not None
            else None,
            str(text or " "),
            content_type[:40] or "text",
            json.dumps(
                dict(metadata or {}),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            telegram_message_created_at(telegram_message),
        ),
    )
    return cursor.rowcount == 1
