from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .authorization import AuthorizationService, current_actor
from .storage import JarvisStorage, new_id, utc_now

_TELEGRAM_REALM_RE = re.compile(r"^telegram:([1-9][0-9]{0,18})$")
_DELIVERY_CLAIM_STALE_SECONDS = 120


class TelegramOperatorError(RuntimeError):
    """Base error for the owner-only Telegram console."""


class TelegramOperatorNotFoundError(TelegramOperatorError):
    """The requested private Telegram binding does not exist."""


class TelegramOperatorConflictError(TelegramOperatorError):
    """An idempotency key was reused for different message content or target."""


@dataclass(frozen=True)
class TelegramDeliveryError(TelegramOperatorError):
    code: str
    uncertain: bool = False

    def __str__(self) -> str:
        return self.code


def _decode_json(value: object, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _realm_bot_id(realm_id: str) -> int:
    match = _TELEGRAM_REALM_RE.fullmatch(str(realm_id or "").strip())
    if match is None:
        raise TelegramOperatorNotFoundError("Invalid Telegram bot realm")
    return int(match.group(1))


def _encode_history_cursor(
    created_at: str,
    sort_sequence: int,
    sort_rank: int,
    message_id: str,
) -> str:
    payload = json.dumps(
        [created_at, sort_sequence, sort_rank, message_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_history_cursor(value: str) -> tuple[str, int, int, str] | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        padded = raw + ("=" * (-len(raw) % 4))
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
        payload = json.loads(decoded)
        if not isinstance(payload, list) or len(payload) != 4:
            raise ValueError
        created_at = str(payload[0])
        sort_sequence = int(payload[1])
        sort_rank = int(payload[2])
        message_id = str(payload[3])
        parsed = datetime.fromisoformat(created_at)
        if (
            parsed.tzinfo is None
            or sort_sequence < 0
            or sort_rank not in {0, 1}
            or not message_id
            or len(message_id) > 200
        ):
            raise ValueError
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid Telegram history cursor") from exc
    return created_at, sort_sequence, sort_rank, message_id


def _telegram_result(response: httpx.Response, *, stage: str) -> dict[str, Any]:
    if response.status_code >= 500:
        raise TelegramDeliveryError(
            f"telegram_{stage}_{response.status_code}",
            uncertain=stage == "send",
        )
    if response.status_code >= 400:
        raise TelegramDeliveryError(f"telegram_{stage}_{response.status_code}")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise TelegramDeliveryError(
            f"telegram_{stage}_invalid_response",
            uncertain=stage == "send",
        ) from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TelegramDeliveryError(
            f"telegram_{stage}_rejected",
            uncertain=stage == "send",
        )
    result = payload.get("result")
    if not isinstance(result, dict):
        raise TelegramDeliveryError(
            f"telegram_{stage}_missing_result",
            uncertain=stage == "send",
        )
    return result


async def send_telegram_text(
    *,
    bot_token: str,
    realm_id: str,
    chat_id: int,
    content: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, int]:
    """Send one literal Bot API text message after pinning the immutable bot realm."""

    token = str(bot_token or "").strip()
    if not token:
        raise TelegramDeliveryError("telegram_bot_not_configured")
    expected_bot_id = _realm_bot_id(realm_id)
    timeout = httpx.Timeout(20.0, connect=5.0)
    try:
        async with httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}",
            timeout=timeout,
            trust_env=False,
            transport=transport,
        ) as client:
            try:
                identity_response = await client.post("/getMe")
            except httpx.HTTPError as exc:
                raise TelegramDeliveryError("telegram_identity_unavailable") from exc
            identity = _telegram_result(identity_response, stage="identity")
            actual_bot_id = identity.get("id")
            if (
                isinstance(actual_bot_id, bool)
                or not isinstance(actual_bot_id, int)
                or actual_bot_id != expected_bot_id
            ):
                raise TelegramDeliveryError("telegram_bot_realm_mismatch")
            try:
                send_response = await client.post(
                    "/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": content,
                        "disable_web_page_preview": True,
                    },
                )
            except httpx.HTTPError as exc:
                raise TelegramDeliveryError(
                    "telegram_send_transport_unknown",
                    uncertain=True,
                ) from exc
    except TelegramDeliveryError:
        raise

    sent = _telegram_result(send_response, stage="send")
    message_id = sent.get("message_id")
    sent_at = sent.get("date")
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise TelegramDeliveryError("telegram_send_missing_message_id", uncertain=True)
    if isinstance(sent_at, bool) or not isinstance(sent_at, int) or sent_at <= 0:
        sent_at = int(datetime.now(UTC).timestamp())
    sender = sent.get("from")
    if isinstance(sender, dict) and sender.get("id") != expected_bot_id:
        raise TelegramDeliveryError("telegram_send_realm_mismatch", uncertain=True)
    return {
        "bot_id": expected_bot_id,
        "message_id": message_id,
        "date": sent_at,
    }


class TelegramOperatorService:
    """Owner-only cross-tenant view and audited manual Bot API delivery."""

    def __init__(
        self,
        *,
        storage: JarvisStorage,
        authorization: AuthorizationService,
    ) -> None:
        self.storage = storage
        self.authorization = authorization

    @staticmethod
    def _chat_payload(row: Any) -> dict[str, Any]:
        item = dict(row)
        first_name = str(item.get("first_name") or "").strip()
        last_name = str(item.get("last_name") or "").strip()
        full_name = " ".join(part for part in (first_name, last_name) if part)
        display_name = str(item.get("display_name") or "").strip()
        username = str(item.get("username") or "").strip()
        chat_id = int(item["chat_id"])
        item["display_name"] = (
            full_name
            or display_name
            or (f"@{username}" if username else str(chat_id))
        )
        item["key"] = f"{item['realm_id']}:{chat_id}"
        transport_count = int(item.pop("telegram_event_count", 0) or 0)
        backend_count = int(item.pop("backend_message_count", 0) or 0)
        assistant_count = int(item.pop("assistant_message_count", 0) or 0)
        legacy_user_count = int(item.pop("legacy_user_message_count", 0) or 0)
        item["message_count"] = (
            transport_count + assistant_count + legacy_user_count
            if transport_count
            else backend_count
        )
        journal_at = str(item.pop("telegram_last_at", "") or "")
        if journal_at and journal_at >= str(item.get("last_message_at") or ""):
            item["last_message"] = str(item.pop("telegram_last_content", "") or "")
            item["last_message_at"] = journal_at
            direction = str(item.pop("telegram_last_direction", "") or "")
            item["last_role"] = "user" if direction == "inbound" else "assistant"
        else:
            item.pop("telegram_last_content", None)
            item.pop("telegram_last_direction", None)
        return item

    @staticmethod
    def _chat_select(where: str = "") -> str:
        return f"""
            SELECT
                tc.realm_id,
                tc.chat_id,
                tc.conversation_id,
                tc.user_id,
                tc.access_mode,
                tc.updated_at,
                u.status,
                u.display_name,
                p.preset_key,
                ei.username,
                ei.first_name,
                ei.last_name,
                c.title,
                c.last_message,
                c.last_message_at,
                (
                    SELECT m.role
                    FROM messages m
                    WHERE m.conversation_id = tc.conversation_id
                      AND m.user_id = tc.user_id
                      AND m.is_deleted = 0
                    ORDER BY m.created_at DESC, m.rowid DESC
                    LIMIT 1
                ) AS last_role,
                (
                    SELECT COUNT(1)
                    FROM messages m
                    WHERE m.conversation_id = tc.conversation_id
                      AND m.user_id = tc.user_id
                      AND m.is_deleted = 0
                ) AS backend_message_count,
                (
                    SELECT COUNT(1)
                    FROM messages m
                    WHERE m.user_id = tc.user_id
                      AND m.role = 'assistant'
                      AND m.is_deleted = 0
                      AND COALESCE(
                          json_extract(m.metadata, '$.telegram_transport_visible'),
                          1
                      ) != 0
                      AND (
                          COALESCE(
                              json_extract(m.metadata, '$.chat_request_hash'), ''
                          ) = ''
                          OR NOT EXISTS (
                              SELECT 1 FROM telegram_message_log tmlo
                              WHERE tmlo.realm_id = tc.realm_id
                                AND tmlo.chat_id = tc.chat_id
                                AND tmlo.direction = 'outbound'
                                AND json_extract(
                                    tmlo.metadata, '$.chat_request_hash'
                                ) = json_extract(
                                    m.metadata, '$.chat_request_hash'
                                )
                          )
                      )
                      AND m.conversation_id IN (
                          SELECT conversation_id
                          FROM telegram_message_log tmlc
                          WHERE tmlc.realm_id = tc.realm_id
                            AND tmlc.chat_id = tc.chat_id
                            AND tmlc.conversation_id IS NOT NULL
                          UNION SELECT tc.conversation_id
                      )
                ) AS assistant_message_count,
                (
                    SELECT COUNT(1)
                    FROM messages m
                    WHERE m.user_id = tc.user_id
                      AND m.role = 'user'
                      AND m.is_deleted = 0
                      AND m.conversation_id IN (
                          SELECT conversation_id
                          FROM telegram_message_log tmlc
                          WHERE tmlc.realm_id = tc.realm_id
                            AND tmlc.chat_id = tc.chat_id
                            AND tmlc.conversation_id IS NOT NULL
                          UNION SELECT tc.conversation_id
                      )
                      AND (
                          COALESCE(json_extract(m.metadata, '$.chat_request_hash'), '') = ''
                          OR NOT EXISTS (
                              SELECT 1 FROM telegram_message_log tmld
                              WHERE tmld.realm_id = tc.realm_id
                                AND tmld.chat_id = tc.chat_id
                                AND json_extract(
                                    tmld.metadata, '$.chat_request_hash'
                                ) = json_extract(
                                    m.metadata, '$.chat_request_hash'
                                )
                          )
                      )
                ) AS legacy_user_message_count,
                (
                    SELECT COUNT(1)
                    FROM telegram_message_log tml
                    WHERE tml.realm_id = tc.realm_id AND tml.chat_id = tc.chat_id
                ) AS telegram_event_count,
                (
                    SELECT content FROM telegram_message_log tml
                    WHERE tml.realm_id = tc.realm_id AND tml.chat_id = tc.chat_id
                    ORDER BY tml.created_at DESC,
                             COALESCE(tml.telegram_message_id, 0) DESC,
                             tml.id DESC LIMIT 1
                ) AS telegram_last_content,
                (
                    SELECT direction FROM telegram_message_log tml
                    WHERE tml.realm_id = tc.realm_id AND tml.chat_id = tc.chat_id
                    ORDER BY tml.created_at DESC,
                             COALESCE(tml.telegram_message_id, 0) DESC,
                             tml.id DESC LIMIT 1
                ) AS telegram_last_direction,
                (
                    SELECT created_at FROM telegram_message_log tml
                    WHERE tml.realm_id = tc.realm_id AND tml.chat_id = tc.chat_id
                    ORDER BY tml.created_at DESC,
                             COALESCE(tml.telegram_message_id, 0) DESC,
                             tml.id DESC LIMIT 1
                ) AS telegram_last_at
            FROM telegram_conversations tc
            JOIN users u ON u.id = tc.user_id
            LEFT JOIN user_preset_assignments upa
              ON upa.user_id = u.id AND upa.revoked_at IS NULL
            LEFT JOIN permission_presets p ON p.id = upa.preset_id
            LEFT JOIN external_identities ei
              ON ei.user_id = tc.user_id
             AND ei.provider = 'telegram'
             AND ei.realm_id = tc.realm_id
             AND ei.provider_subject_id = CAST(tc.chat_id AS TEXT)
            LEFT JOIN conversations c
              ON c.id = tc.conversation_id AND c.user_id = tc.user_id
            {where}
        """

    def list_chats(
        self,
        *,
        limit: int,
        offset: int,
        search: str = "",
    ) -> dict[str, Any]:
        normalized_search = " ".join(str(search or "").split()).strip()[:160]
        where = "WHERE tc.user_id IS NOT NULL AND tc.chat_id > 0"
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if normalized_search:
            where += """
                AND (
                    lower(CAST(tc.chat_id AS TEXT)) LIKE lower(:pattern) ESCAPE '\\'
                    OR lower(u.display_name) LIKE lower(:pattern) ESCAPE '\\'
                    OR lower(COALESCE(ei.username, '')) LIKE lower(:pattern) ESCAPE '\\'
                    OR lower(COALESCE(ei.first_name, '')) LIKE lower(:pattern) ESCAPE '\\'
                    OR lower(COALESCE(ei.last_name, '')) LIKE lower(:pattern) ESCAPE '\\'
                )
            """
            escaped = (
                normalized_search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            params["pattern"] = f"%{escaped}%"
        with self.storage.locked_connection() as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'telegram_conversations'"
            ).fetchone() is None:
                return {"chats": [], "total": 0, "limit": limit, "offset": offset}
            total = int(
                conn.execute(
                    f"SELECT COUNT(1) FROM ({self._chat_select(where)})",
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                self._chat_select(where)
                + " ORDER BY CASE WHEN COALESCE(NULLIF(telegram_last_at, ''), '') "
                ">= COALESCE(NULLIF(c.last_message_at, ''), tc.updated_at) "
                "THEN telegram_last_at "
                "ELSE COALESCE(NULLIF(c.last_message_at, ''), tc.updated_at) END DESC, "
                "tc.realm_id, tc.chat_id LIMIT :limit OFFSET :offset",
                params,
            ).fetchall()
        return {
            "chats": [self._chat_payload(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_chat(self, *, realm_id: str, chat_id: int) -> dict[str, Any]:
        _realm_bot_id(realm_id)
        with self.storage.locked_connection() as conn:
            row = conn.execute(
                self._chat_select(
                    "WHERE tc.realm_id = :realm_id AND tc.chat_id = :chat_id "
                    "AND tc.user_id IS NOT NULL"
                ),
                {"realm_id": realm_id, "chat_id": chat_id},
            ).fetchone()
        if row is None:
            raise TelegramOperatorNotFoundError("Telegram chat is not registered")
        return self._chat_payload(row)

    def list_messages(
        self,
        *,
        realm_id: str,
        chat_id: int,
        limit: int,
        before: str = "",
    ) -> dict[str, Any]:
        chat = self.get_chat(realm_id=realm_id, chat_id=chat_id)
        self._fence_stale_claims(realm_id=realm_id, chat_id=chat_id)
        cursor = _decode_history_cursor(before)

        def cursor_sql(
            alias: str,
            sequence_expression: str,
            rank_expression: str,
        ) -> tuple[str, tuple[str | int, ...]]:
            if cursor is None:
                return "", ()
            created_at, sort_sequence, sort_rank, message_id = cursor
            return (
                f" AND ({alias}.created_at < ? OR "
                f"({alias}.created_at = ? AND ("
                f"{sequence_expression} < ? OR "
                f"({sequence_expression} = ? AND ("
                f"{rank_expression} < ? OR "
                f"({rank_expression} = ? AND {alias}.id < ?))))))",
                (
                    created_at,
                    created_at,
                    sort_sequence,
                    sort_sequence,
                    sort_rank,
                    sort_rank,
                    message_id,
                ),
            )

        transport_rank = "CASE WHEN tml.direction = 'inbound' THEN 0 ELSE 1 END"
        backend_rank = "CASE WHEN m.role = 'user' THEN 0 ELSE 1 END"
        transport_sequence = "COALESCE(tml.telegram_message_id, 0)"
        backend_sequence = (
            "COALESCE(CAST(json_extract(m.metadata, '$.telegram.message_id') AS INTEGER), 0)"
        )
        transport_cursor_sql, transport_cursor_params = cursor_sql(
            "tml", transport_sequence, transport_rank
        )
        backend_cursor_sql, backend_cursor_params = cursor_sql(
            "m", backend_sequence, backend_rank
        )
        send_cursor_sql, send_cursor_params = cursor_sql("tos", "0", "1")
        with self.storage.locked_connection() as conn:
            transport_rows = conn.execute(
                f"""
                SELECT id, direction, sender_kind, content, content_type, metadata,
                       created_at, edited_at, telegram_message_id, conversation_id
                FROM telegram_message_log tml
                WHERE tml.realm_id = ? AND tml.chat_id = ?
                {transport_cursor_sql}
                ORDER BY tml.created_at DESC, {transport_sequence} DESC,
                         {transport_rank} DESC, tml.id DESC
                LIMIT ?
                """,
                (realm_id, chat_id, *transport_cursor_params, limit + 1),
            ).fetchall()
            conversation_ids = {
                str(row["conversation_id"])
                for row in conn.execute(
                    """
                    SELECT DISTINCT conversation_id
                    FROM telegram_message_log
                    WHERE realm_id = ? AND chat_id = ?
                      AND conversation_id IS NOT NULL
                    """,
                    (realm_id, chat_id),
                ).fetchall()
            }
            conversation_ids.add(str(chat["conversation_id"]))
            placeholders = ",".join("?" for _ in conversation_ids)
            rows = conn.execute(
                f"""
                SELECT id, role, content, metadata, created_at, edited_at,
                       reply_to_message_id
                FROM messages m
                WHERE m.conversation_id IN ({placeholders})
                  AND m.user_id = ? AND m.is_deleted = 0
                  AND m.role IN ('user', 'assistant')
                  AND COALESCE(
                      json_extract(m.metadata, '$.telegram_transport_visible'),
                      1
                  ) != 0
                  AND (
                      m.role != 'assistant'
                      OR COALESCE(
                          json_extract(m.metadata, '$.chat_request_hash'), ''
                      ) = ''
                      OR NOT EXISTS (
                          SELECT 1 FROM telegram_message_log tmlo
                          WHERE tmlo.realm_id = ? AND tmlo.chat_id = ?
                            AND tmlo.direction = 'outbound'
                            AND json_extract(
                                tmlo.metadata, '$.chat_request_hash'
                            ) = json_extract(
                                m.metadata, '$.chat_request_hash'
                            )
                      )
                  )
                  AND (
                      m.role != 'user'
                      OR COALESCE(json_extract(m.metadata, '$.chat_request_hash'), '') = ''
                      OR NOT EXISTS (
                          SELECT 1 FROM telegram_message_log tmld
                          WHERE tmld.realm_id = ? AND tmld.chat_id = ?
                            AND json_extract(
                                tmld.metadata, '$.chat_request_hash'
                            ) = json_extract(
                                m.metadata, '$.chat_request_hash'
                            )
                      )
                  )
                  {backend_cursor_sql}
                ORDER BY m.created_at DESC, {backend_sequence} DESC,
                         {backend_rank} DESC, m.id DESC
                LIMIT ?
                """,
                (
                    *sorted(conversation_ids),
                    chat["user_id"],
                    realm_id,
                    chat_id,
                    realm_id,
                    chat_id,
                    *backend_cursor_params,
                    limit + 1,
                ),
            ).fetchall()
            unsettled = conn.execute(
                f"""
                SELECT id, client_request_id, content, status, error_code,
                       created_at, updated_at
                FROM telegram_operator_sends tos
                WHERE tos.realm_id = ? AND tos.chat_id = ?
                  AND (tos.status != 'delivered' OR tos.message_id IS NULL)
                  {send_cursor_sql}
                ORDER BY tos.created_at DESC, tos.rowid DESC
                LIMIT ?
                """,
                (realm_id, chat_id, *send_cursor_params, limit + 1),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            metadata = _decode_json(row["metadata"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            role = str(row["role"])
            operator_authored = bool(metadata.get("operator_authored"))
            telegram_metadata = metadata.get("telegram")
            backend_sequence = (
                int(telegram_metadata.get("message_id"))
                if isinstance(telegram_metadata, dict)
                and isinstance(telegram_metadata.get("message_id"), int)
                and not isinstance(telegram_metadata.get("message_id"), bool)
                else 0
            )
            messages.append(
                {
                    "id": str(row["id"]),
                    "role": role,
                    "direction": "inbound" if role == "user" else "outbound",
                    "content": str(row["content"]),
                    "created_at": str(row["created_at"]),
                    "edited_at": row["edited_at"],
                    "reply_to_message_id": row["reply_to_message_id"],
                    "metadata": metadata,
                    "operator_authored": operator_authored,
                    "delivery_status": "delivered" if operator_authored else None,
                    "_sort_sequence": backend_sequence,
                    "_sort_rank": 0 if role == "user" else 1,
                }
            )
        for row in transport_rows:
            metadata = _decode_json(row["metadata"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            direction = str(row["direction"])
            messages.append(
                {
                    "id": str(row["id"]),
                    "role": "user" if direction == "inbound" else "assistant",
                    "direction": direction,
                    "content": str(row["content"]),
                    "created_at": str(row["created_at"]),
                    "edited_at": row["edited_at"],
                    "reply_to_message_id": None,
                    "metadata": {
                        **metadata,
                        "content_type": str(row["content_type"]),
                        "telegram_message_id": row["telegram_message_id"],
                    },
                    "operator_authored": str(row["sender_kind"]) == "operator",
                    "delivery_status": "delivered" if direction == "outbound" else None,
                    "_sort_sequence": int(row["telegram_message_id"] or 0),
                    "_sort_rank": 0 if direction == "inbound" else 1,
                }
            )
        for row in unsettled:
            messages.append(
                {
                    "id": str(row["id"]),
                    "role": "assistant",
                    "direction": "outbound",
                    "content": str(row["content"]),
                    "created_at": str(row["created_at"]),
                    "edited_at": None,
                    "reply_to_message_id": None,
                    "metadata": {
                        "client_request_id": str(row["client_request_id"]),
                        "error_code": row["error_code"],
                        "operator_send_id": str(row["id"]),
                    },
                    "operator_authored": True,
                    "delivery_status": str(row["status"]),
                    "_sort_sequence": 0,
                    "_sort_rank": 1,
                }
            )
        messages.sort(
            key=lambda item: (
                str(item["created_at"]),
                int(item["_sort_sequence"]),
                int(item["_sort_rank"]),
                str(item["id"]),
            ),
            reverse=True,
        )
        has_more = len(messages) > limit
        page = messages[:limit]
        next_before = (
            _encode_history_cursor(
                str(page[-1]["created_at"]),
                int(page[-1]["_sort_sequence"]),
                int(page[-1]["_sort_rank"]),
                str(page[-1]["id"]),
            )
            if has_more and page
            else None
        )
        page.reverse()
        for item in page:
            item["sort_sequence"] = int(item.pop("_sort_sequence", 0))
            item["sort_rank"] = int(item.pop("_sort_rank", 0))
        return {
            "chat": chat,
            "messages": page,
            "has_more": has_more,
            "next_before": next_before,
        }

    @staticmethod
    def _public_send(row: Any) -> dict[str, Any]:
        item = dict(row)
        item.pop("content_sha256", None)
        return item

    def _send_by_id(self, send_id: str) -> dict[str, Any]:
        with self.storage.locked_connection() as conn:
            row = conn.execute(
                """
                SELECT id, operator_user_id, client_request_id, realm_id, chat_id,
                       conversation_id, user_id, content, content_sha256, status,
                       telegram_message_id, message_id, error_code,
                       delivery_claimed_at, delivery_attempt_count, created_at,
                       updated_at, delivered_at
                FROM telegram_operator_sends WHERE id = ?
                """,
                (send_id,),
            ).fetchone()
        if row is None:
            raise TelegramOperatorNotFoundError("Telegram operator send does not exist")
        return self._public_send(row)

    def prepare_send(
        self,
        *,
        realm_id: str,
        chat_id: int,
        content: str,
        client_request_id: str,
    ) -> tuple[dict[str, Any], bool]:
        _realm_bot_id(realm_id)
        normalized_content = str(content or "").strip()
        if not normalized_content or len(normalized_content) > 4096:
            raise ValueError("Telegram message must contain 1 to 4096 characters")
        actor = current_actor()
        digest = _content_sha256(normalized_content)
        now = utc_now()
        with self.storage.transaction(immediate=True) as conn:
            existing = conn.execute(
                """
                SELECT id, operator_user_id, client_request_id, realm_id, chat_id,
                       conversation_id, user_id, content, content_sha256, status,
                       telegram_message_id, message_id, error_code,
                       delivery_claimed_at, delivery_attempt_count, created_at,
                       updated_at, delivered_at
                FROM telegram_operator_sends
                WHERE operator_user_id = ? AND client_request_id = ?
                """,
                (actor.user_id, client_request_id),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["realm_id"]) != realm_id
                    or int(existing["chat_id"]) != chat_id
                    or str(existing["content_sha256"]) != digest
                ):
                    raise TelegramOperatorConflictError(
                        "Telegram client_request_id was reused for a different message"
                    )
                return self._public_send(existing), False
            chat = conn.execute(
                """
                SELECT tc.conversation_id, tc.user_id, u.status
                FROM telegram_conversations tc
                JOIN users u ON u.id = tc.user_id
                JOIN conversations c
                  ON c.id = tc.conversation_id AND c.user_id = tc.user_id
                WHERE tc.realm_id = ? AND tc.chat_id = ? AND tc.user_id IS NOT NULL
                """,
                (realm_id, chat_id),
            ).fetchone()
            if chat is None or str(chat["status"]) == "deleted":
                raise TelegramOperatorNotFoundError("Telegram chat is not registered")
            send_id = new_id("tgsend")
            conn.execute(
                """
                INSERT INTO telegram_operator_sends(
                    id, operator_user_id, client_request_id, realm_id, chat_id,
                    conversation_id, user_id, content, content_sha256, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    send_id,
                    actor.user_id,
                    client_request_id,
                    realm_id,
                    chat_id,
                    str(chat["conversation_id"]),
                    str(chat["user_id"]),
                    normalized_content,
                    digest,
                    now,
                    now,
                ),
            )
            self.authorization.append_security_audit(
                conn,
                action="telegram.operator.send",
                target_type="telegram_chat",
                target_id=f"{realm_id}:{chat_id}",
                target_user_id=str(chat["user_id"]),
                reason="Manual message from the owner Telegram console",
                after={
                    "send_id": send_id,
                    "client_request_id": client_request_id,
                    "content_sha256": digest,
                    "content_length": len(normalized_content),
                    "status": "pending",
                },
            )
        return self._send_by_id(send_id), True

    def _claim_send(self, send_id: str) -> tuple[dict[str, Any], bool]:
        """Claim exactly one external attempt; stale claims are fenced, never retried."""

        now = datetime.now(UTC)
        now_text = now.isoformat()
        claimed = False
        with self.storage.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM telegram_operator_sends WHERE id = ?",
                (send_id,),
            ).fetchone()
            if row is None:
                raise TelegramOperatorNotFoundError("Telegram operator send does not exist")
            if str(row["status"]) != "pending":
                return self._public_send(row), False
            raw_claimed_at = str(row["delivery_claimed_at"] or "")
            if raw_claimed_at:
                try:
                    claimed_at = datetime.fromisoformat(
                        raw_claimed_at.replace("Z", "+00:00")
                    )
                    if claimed_at.tzinfo is None:
                        claimed_at = claimed_at.replace(tzinfo=UTC)
                    stale = (now - claimed_at.astimezone(UTC)).total_seconds() >= (
                        _DELIVERY_CLAIM_STALE_SECONDS
                    )
                except ValueError:
                    stale = True
                if not stale:
                    return self._public_send(row), False
                conn.execute(
                    """
                    UPDATE telegram_operator_sends
                    SET status = 'uncertain', error_code = 'delivery_claim_expired',
                        updated_at = ?
                    WHERE id = ? AND status = 'pending'
                      AND delivery_claimed_at = ?
                    """,
                    (now_text, send_id, raw_claimed_at),
                )
                self.authorization.append_security_audit(
                    conn,
                    action="telegram.operator.delivery",
                    target_type="telegram_send",
                    target_id=send_id,
                    target_user_id=str(row["user_id"]),
                    reason="Stale Telegram delivery claim was fenced",
                    after={
                        "status": "uncertain",
                        "error_code": "delivery_claim_expired",
                    },
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE telegram_operator_sends
                    SET delivery_claimed_at = ?,
                        delivery_attempt_count = delivery_attempt_count + 1,
                        updated_at = ?
                    WHERE id = ? AND status = 'pending'
                      AND delivery_claimed_at IS NULL
                    """,
                    (now_text, now_text, send_id),
                )
                claimed = cursor.rowcount == 1
        return self._send_by_id(send_id), claimed

    def _fence_stale_claims(self, *, realm_id: str, chat_id: int) -> None:
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=_DELIVERY_CLAIM_STALE_SECONDS)
        ).isoformat()
        now = utc_now()
        with self.storage.transaction(immediate=True) as conn:
            rows = conn.execute(
                """
                SELECT id, user_id FROM telegram_operator_sends
                WHERE realm_id = ? AND chat_id = ? AND status = 'pending'
                  AND delivery_claimed_at IS NOT NULL
                  AND delivery_claimed_at <= ?
                """,
                (realm_id, chat_id, cutoff),
            ).fetchall()
            for row in rows:
                cursor = conn.execute(
                    """
                    UPDATE telegram_operator_sends
                    SET status = 'uncertain', error_code = 'delivery_claim_expired',
                        updated_at = ?
                    WHERE id = ? AND status = 'pending'
                      AND delivery_claimed_at IS NOT NULL
                      AND delivery_claimed_at <= ?
                    """,
                    (now, str(row["id"]), cutoff),
                )
                if cursor.rowcount != 1:
                    continue
                self.authorization.append_security_audit(
                    conn,
                    action="telegram.operator.delivery",
                    target_type="telegram_send",
                    target_id=str(row["id"]),
                    target_user_id=str(row["user_id"]),
                    reason="Stale Telegram delivery claim was fenced",
                    after={
                        "status": "uncertain",
                        "error_code": "delivery_claim_expired",
                    },
                )

    def _finalize_failure(
        self,
        *,
        send_id: str,
        error: TelegramDeliveryError,
    ) -> dict[str, Any]:
        status = "uncertain" if error.uncertain else "failed"
        now = utc_now()
        with self.storage.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT user_id, status FROM telegram_operator_sends WHERE id = ?",
                (send_id,),
            ).fetchone()
            if row is None:
                raise TelegramOperatorNotFoundError("Telegram operator send does not exist")
            if str(row["status"]) != "pending":
                return self._send_by_id(send_id)
            conn.execute(
                """
                UPDATE telegram_operator_sends
                SET status = ?, error_code = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, error.code[:120], now, send_id),
            )
            self.authorization.append_security_audit(
                conn,
                action="telegram.operator.delivery",
                target_type="telegram_send",
                target_id=send_id,
                target_user_id=str(row["user_id"]),
                reason="Telegram operator message delivery finished",
                after={"status": status, "error_code": error.code[:120]},
            )
        return self._send_by_id(send_id)

    def _finalize_success(
        self,
        *,
        send_id: str,
        delivery: dict[str, int],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        delivered_at = datetime.fromtimestamp(delivery["date"], UTC).isoformat()
        with self.storage.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM telegram_operator_sends WHERE id = ?",
                (send_id,),
            ).fetchone()
            if row is None:
                raise TelegramOperatorNotFoundError("Telegram operator send does not exist")
            if str(row["status"]) == "delivered" and row["message_id"]:
                message = conn.execute(
                    """
                    SELECT id, role, content, metadata, created_at, edited_at,
                           reply_to_message_id
                    FROM messages WHERE id = ?
                    """,
                    (row["message_id"],),
                ).fetchone()
                if message is None:
                    raise TelegramOperatorConflictError(
                        "Delivered Telegram message history row is missing"
                    )
                return self._public_send(row), self._message_payload(message)
            if str(row["status"]) != "pending":
                raise TelegramOperatorConflictError(
                    "Telegram send is no longer eligible for delivery finalization"
                )
            conversation = conn.execute(
                "SELECT user_id FROM conversations WHERE id = ?",
                (row["conversation_id"],),
            ).fetchone()
            if conversation is None or str(conversation["user_id"]) != str(row["user_id"]):
                raise TelegramOperatorConflictError("Telegram conversation ownership changed")
            message_id = new_id("msg")
            metadata = {
                "transport": "telegram",
                "operator_authored": True,
                "operator_user_id": str(row["operator_user_id"]),
                "operator_send_id": send_id,
                "client_request_id": str(row["client_request_id"]),
                "telegram": {
                    "realm_id": str(row["realm_id"]),
                    "chat_id": int(row["chat_id"]),
                    "message_id": delivery["message_id"],
                    "bot_id": delivery["bot_id"],
                },
            }
            conn.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, metadata, created_at,
                    user_id, reply_to_message_id
                ) VALUES (?, ?, 'assistant', ?, ?, ?, ?, NULL)
                """,
                (
                    message_id,
                    str(row["conversation_id"]),
                    str(row["content"]),
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    delivered_at,
                    str(row["user_id"]),
                ),
            )
            preview = " ".join(str(row["content"]).split())[:120]
            conn.execute(
                """
                UPDATE conversations
                SET updated_at = ?, last_message = ?, last_message_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    delivered_at,
                    preview,
                    delivered_at,
                    str(row["conversation_id"]),
                    str(row["user_id"]),
                ),
            )
            conn.execute(
                """
                UPDATE telegram_operator_sends
                SET status = 'delivered', telegram_message_id = ?, message_id = ?,
                    error_code = NULL, updated_at = ?, delivered_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    delivery["message_id"],
                    message_id,
                    delivered_at,
                    delivered_at,
                    send_id,
                ),
            )
            self.authorization.append_security_audit(
                conn,
                action="telegram.operator.delivery",
                target_type="telegram_send",
                target_id=send_id,
                target_user_id=str(row["user_id"]),
                reason="Telegram operator message delivery finished",
                after={
                    "status": "delivered",
                    "telegram_message_id": delivery["message_id"],
                    "message_id": message_id,
                },
            )
            message = conn.execute(
                """
                SELECT id, role, content, metadata, created_at, edited_at,
                       reply_to_message_id
                FROM messages WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
        return self._send_by_id(send_id), self._message_payload(message)

    @staticmethod
    def _message_payload(row: Any) -> dict[str, Any]:
        metadata = _decode_json(row["metadata"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        role = str(row["role"])
        telegram_metadata = metadata.get("telegram")
        telegram_message_id = (
            telegram_metadata.get("message_id")
            if isinstance(telegram_metadata, dict)
            else None
        )
        sort_sequence = (
            int(telegram_message_id)
            if isinstance(telegram_message_id, int)
            and not isinstance(telegram_message_id, bool)
            else 0
        )
        return {
            "id": str(row["id"]),
            "role": role,
            "direction": "inbound" if role == "user" else "outbound",
            "content": str(row["content"]),
            "created_at": str(row["created_at"]),
            "edited_at": row["edited_at"],
            "reply_to_message_id": row["reply_to_message_id"],
            "metadata": metadata,
            "operator_authored": bool(metadata.get("operator_authored")),
            "delivery_status": "delivered",
            "sort_sequence": sort_sequence,
            "sort_rank": 0 if role == "user" else 1,
        }

    async def deliver(
        self,
        *,
        send: dict[str, Any],
        bot_token: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        claimed_send, claimed = self._claim_send(str(send["id"]))
        if not claimed:
            message_id = str(claimed_send.get("message_id") or "")
            if not message_id:
                return claimed_send, None
            with self.storage.locked_connection() as conn:
                row = conn.execute(
                    """
                    SELECT id, role, content, metadata, created_at, edited_at,
                           reply_to_message_id FROM messages WHERE id = ?
                    """,
                    (message_id,),
                ).fetchone()
            return claimed_send, self._message_payload(row) if row is not None else None
        try:
            delivery = await send_telegram_text(
                bot_token=bot_token,
                realm_id=str(claimed_send["realm_id"]),
                chat_id=int(claimed_send["chat_id"]),
                content=str(claimed_send["content"]),
            )
        except asyncio.CancelledError:
            self._finalize_failure(
                send_id=str(claimed_send["id"]),
                error=TelegramDeliveryError(
                    "telegram_send_cancelled_unknown",
                    uncertain=True,
                ),
            )
            raise
        except TelegramDeliveryError as error:
            return (
                self._finalize_failure(send_id=str(claimed_send["id"]), error=error),
                None,
            )
        except Exception:
            return (
                self._finalize_failure(
                    send_id=str(claimed_send["id"]),
                    error=TelegramDeliveryError(
                        "telegram_send_unexpected_unknown",
                        uncertain=True,
                    ),
                ),
                None,
            )
        return self._finalize_success(send_id=str(claimed_send["id"]), delivery=delivery)
