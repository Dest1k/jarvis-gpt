from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .memory_vault import MemoryVault
from .redaction import redact_value

_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "with",
    "а",
    "без",
    "бы",
    "в",
    "во",
    "вот",
    "где",
    "да",
    "для",
    "до",
    "же",
    "за",
    "и",
    "из",
    "или",
    "как",
    "к",
    "ко",
    "ли",
    "мне",
    "мой",
    "моя",
    "мы",
    "на",
    "над",
    "не",
    "но",
    "о",
    "об",
    "он",
    "она",
    "они",
    "от",
    "по",
    "под",
    "про",
    "с",
    "со",
    "так",
    "там",
    "то",
    "у",
    "что",
    "это",
    "я",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _loads(data: str | None, default: Any) -> Any:
    if not data:
        return default
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return default


def _approval_record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **dict(row),
        "payload": _loads(row["payload"], {}),
        "result": _loads(row["result"], {}),
    }


_SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "api_key",
    "apikey",
    "bearer",
    "cookie",
    "password",
    "secret",
    "token",
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|cookie|password|secret|token)\b\s*[:=]\s*[^,\s;]+"
)
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_URL_CREDENTIAL_RE = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+:[^/@\s]+@")
_SENSITIVE_FLAG_RE = re.compile(
    r"(?i)^--?(?:password|passwd|secret|token|credential|api[-_]?key|private[-_]?key)$"
)


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _sensitive_key(text_key):
                redacted[text_key] = "[redacted]"
            elif text_key.casefold() in {"content_base64", "value_base64"}:
                redacted[text_key] = f"[redacted:{len(str(item))} chars]"
            elif text_key.casefold() == "arguments" and isinstance(item, list | tuple):
                redacted[text_key] = _redact_argument_list(item)
            elif text_key.casefold() == "environment" and isinstance(item, dict):
                redacted[text_key] = {str(name): "[redacted]" for name in item}
            else:
                redacted[text_key] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    return value


def _redact_sensitive_text(text: str) -> str:
    text = _BEARER_RE.sub(r"\1[redacted]", text)
    text = _OPENAI_KEY_RE.sub("sk-[redacted]", text)
    text = _URL_CREDENTIAL_RE.sub(r"\1[redacted]@", text)
    return _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)


def _redact_argument_list(value: list[Any] | tuple[Any, ...]) -> list[Any]:
    result: list[Any] = []
    redact_next = False
    for item in value:
        if not isinstance(item, str):
            result.append(_redact_sensitive(item))
            redact_next = False
            continue
        if redact_next:
            result.append("[redacted]")
            redact_next = False
            continue
        redacted = _redact_sensitive_text(item)
        prefix, separator, _secret = redacted.partition("=")
        if separator and _SENSITIVE_FLAG_RE.fullmatch(prefix):
            result.append(f"{prefix}=[redacted]")
            continue
        result.append(redacted)
        redact_next = bool(_SENSITIVE_FLAG_RE.fullmatch(item))
    return result


def _sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _query_terms(query: str | None, *, limit: int = 12) -> list[str]:
    if not query:
        return []
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[\w-]+", query, flags=re.UNICODE):
        clean = token.replace('"', "").replace("'", "").strip()
        normalized = clean.casefold()
        if not clean or normalized in seen or normalized in _QUERY_STOPWORDS:
            continue
        terms.append(clean)
        seen.add(normalized)
        if len(terms) >= limit:
            break
    return terms


def _fts_query(query: str) -> str:
    return " OR ".join(f'"{term}"' for term in _query_terms(query, limit=8))


def _recoverable_fts_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "no such module: fts5",
            "no such table: memories_fts",
            "no such table: file_chunks_fts",
            "fts5: syntax error",
            "malformed match expression",
            "unterminated string",
        )
    )


def _decorate_memory_hit(row: dict[str, Any], query: str | None) -> dict[str, Any]:
    tags = _loads(row.get("tags"), [])
    terms = _query_terms(query)
    searchable = " ".join(
        [
            str(row.get("namespace") or ""),
            str(row.get("content") or ""),
            " ".join(str(tag) for tag in tags),
        ]
    )
    matched = _matched_terms(searchable, terms)
    row["tags"] = tags
    row["matched_terms"] = matched
    row["snippet"] = _snippet(str(row.get("content") or ""), matched or terms)
    row["relevance"] = _relevance(
        row.get("rank"),
        matched_terms=matched,
        query_terms=terms,
        importance=float(row.get("importance") or 0),
    )
    return row


def _decorate_file_hit(row: dict[str, Any], query: str | None) -> dict[str, Any]:
    terms = _query_terms(query)
    searchable = " ".join([str(row.get("file_name") or ""), str(row.get("content") or "")])
    matched = _matched_terms(searchable, terms)
    row["matched_terms"] = matched
    row["exact_matched_terms"] = _matched_terms_exact(searchable, terms)
    row["snippet"] = _snippet(str(row.get("content") or ""), matched or terms)
    row["relevance"] = _relevance(
        row.get("rank"),
        matched_terms=matched,
        query_terms=terms,
        importance=0,
    )
    return row


def _matched_terms(text: str, terms: list[str]) -> list[str]:
    lowered = text.casefold()
    return [term for term in terms if term.casefold() in lowered]


def _matched_terms_exact(text: str, terms: list[str]) -> list[str]:
    lowered = text.casefold()
    return [
        term
        for term in terms
        if re.search(
            rf"(?<!\w){re.escape(term.casefold())}(?!\w)",
            lowered,
            flags=re.UNICODE,
        )
    ]


def _snippet(text: str, terms: list[str], *, max_chars: int = 260) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    lowered = cleaned.casefold()
    positions = [lowered.find(term.casefold()) for term in terms if term]
    positions = [position for position in positions if position >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - max_chars // 3)
    end = min(len(cleaned), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(cleaned) else ""
    return f"{prefix}{cleaned[start:end]}{suffix}"


def _relevance(
    rank: Any,
    *,
    matched_terms: list[str],
    query_terms: list[str],
    importance: float,
) -> float:
    try:
        raw_rank = abs(float(rank))
    except (TypeError, ValueError):
        raw_rank = 3.0
    rank_score = 1.0 / (1.0 + max(raw_rank, 0.0))
    coverage = len(matched_terms) / len(query_terms) if query_terms else 0.0
    score = (rank_score * 0.35) + (coverage * 0.45) + (max(0.0, min(1.0, importance)) * 0.20)
    return round(max(0.0, min(1.0, score)), 4)


def _normalize_memory_content(content: str) -> str:
    cleaned = re.sub(r"\s+", " ", content).strip().casefold()
    return re.sub(r"[^\w:/\\.-]+", " ", cleaned, flags=re.UNICODE).strip()


def _normalize_tags(tags: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for tag in tags:
        clean = str(tag).strip()[:80]
        key = clean.casefold()
        if not clean or key in seen:
            continue
        normalized.append(clean)
        seen.add(key)
        if len(normalized) >= 16:
            break
    return normalized


def _merge_tags(*tag_lists: Iterable[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for tags in tag_lists:
        for tag in tags:
            clean = str(tag).strip()[:80]
            key = clean.casefold()
            if not clean or key in seen:
                continue
            merged.append(clean)
            seen.add(key)
            if len(merged) >= 16:
                return merged
    return merged


def _memory_sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
    return (
        float(item.get("relevance") or 0),
        float(item.get("importance") or 0),
        str(item.get("updated_at") or ""),
    )


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


class JarvisStorage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._read_only = False
        self._memory_fts_available = False
        self._file_fts_available = False
        self.memory_vault = MemoryVault(database_path.parent.parent / "memory-vault")

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.database_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def open_readonly(self) -> None:
        """Open an existing database without migrations, WAL changes, or vault sync."""

        with self._lock:
            if self._conn is not None:
                return
            path = self.database_path.resolve(strict=True)
            if not path.is_file():
                raise FileNotFoundError(f"Jarvis database is not a file: {path}")
            conn = sqlite3.connect(
                f"{path.as_uri()}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA foreign_keys=ON")
                self._memory_fts_available = _sqlite_table_exists(
                    conn, "memories_fts"
                )
                self._file_fts_available = _sqlite_table_exists(
                    conn, "file_chunks_fts"
                )
            except BaseException:
                conn.close()
                raise
            self._conn = conn
            self._read_only = True

    def initialize(self) -> None:
        with self._lock:
            conn = self.connect()
            conn.executescript(SCHEMA)
            self._memory_fts_available = self._ensure_memory_fts(conn)
            self._file_fts_available = self._ensure_file_chunks_fts(conn)
            self._sync_memory_vault(conn)
            conn.commit()

    def _ensure_memory_fts(self, conn: sqlite3.Connection) -> bool:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(id UNINDEXED, namespace, content, tags)
                """
            )
            conn.execute("DELETE FROM memories_fts")
            conn.execute(
                """
                INSERT INTO memories_fts(id, namespace, content, tags)
                SELECT id, namespace, content, tags FROM memories
                """
            )
            return True
        except sqlite3.OperationalError as exc:
            if _recoverable_fts_error(exc):
                return False
            raise

    def _ensure_file_chunks_fts(self, conn: sqlite3.Connection) -> bool:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks_fts
                USING fts5(file_id UNINDEXED, chunk_id UNINDEXED, content)
                """
            )
            conn.execute("DELETE FROM file_chunks_fts")
            conn.execute(
                """
                INSERT INTO file_chunks_fts(file_id, chunk_id, content)
                SELECT file_id, id, content FROM file_chunks
                """
            )
            return True
        except sqlite3.OperationalError as exc:
            if _recoverable_fts_error(exc):
                return False
            raise

    def _sync_memory_vault(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT id, namespace, content, tags, importance, created_at, updated_at
            FROM memories
            ORDER BY updated_at DESC
            """
        ).fetchall()
        memories = [
            {**dict(row), "tags": _loads(row["tags"], [])}
            for row in rows
        ]
        self.memory_vault.sync(memories)

    def ping(self) -> bool:
        with self._lock:
            self.connect().execute("SELECT 1").fetchone()
        return True

    def backup_database(self, target_dir: str | Path | None = None) -> dict[str, Any]:
        """Create a consistent SQLite backup using the SQLite backup API."""

        created_at = utc_now()
        backup_dir = (
            Path(target_dir)
            if target_dir is not None
            else self.database_path.parent / "backups"
        )
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target_path = backup_dir / f"{self.database_path.stem}-{stamp}.sqlite3"
        counter = 1
        while target_path.exists():
            target_path = backup_dir / f"{self.database_path.stem}-{stamp}-{counter}.sqlite3"
            counter += 1
        with self._lock:
            source = self.connect()
            with sqlite3.connect(target_path) as target:
                source.backup(target)
        result = {
            "ok": True,
            "path": str(target_path),
            "size": target_path.stat().st_size,
            "created_at": created_at,
        }
        self.add_event(
            kind="runtime.backup",
            title="Runtime database backup created",
            payload=result,
        )
        self.record_audit(
            actor="operator",
            action="runtime.backup",
            target_type="runtime",
            target_id="sqlite",
            summary="Runtime database backup created",
            after=result,
        )
        return result

    def counters(self) -> dict[str, int]:
        tables = [
            "runtime_kv",
            "conversations",
            "messages",
            "memories",
            "missions",
            "mission_tasks",
            "reminders",
            "files",
            "file_chunks",
            "tool_runs",
            "learning_observations",
            "approvals",
            "telemetry_snapshots",
            "audit_log",
        ]
        with self._lock:
            conn = self.connect()
            return {
                table: int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
                for table in tables
            }

    def add_event(
        self,
        *,
        kind: str,
        title: str,
        level: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "id": new_id("evt"),
            "ts": utc_now(),
            "level": level,
            "kind": kind,
            "title": title,
            "payload": payload or {},
        }
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO runtime_events(id, ts, level, kind, title, payload)
                VALUES (:id, :ts, :level, :kind, :title, :payload)
                """,
                {**row, "payload": _json(row["payload"])},
            )
            self.connect().commit()
        return row

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT id, ts, level, kind, title, payload
                FROM runtime_events
                ORDER BY ts DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [
            {**dict(row), "payload": _loads(row["payload"], {})}
            for row in rows.fetchall()
        ]

    def get_runtime_value(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.connect().execute(
                """
                SELECT value
                FROM runtime_kv
                WHERE key = ?
                """,
                (key,),
            ).fetchone()
        if row is None:
            return default
        return _loads(row["value"], default)

    def set_runtime_value(self, key: str, value: Any) -> dict[str, Any]:
        now = utc_now()
        row = {"key": key[:160], "value": value, "updated_at": now}
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO runtime_kv(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (row["key"], _json(row["value"]), row["updated_at"]),
            )
            self.connect().commit()
        return row

    def update_runtime_value_atomic(
        self,
        key: str,
        updater: Callable[[Any], Any],
        *,
        default: Any = None,
    ) -> Any:
        """Read, transform, and replace one runtime value under a SQLite write lock.

        Runtime values such as autonomy jobs are JSON aggregates. A separate
        ``get`` followed by ``set`` lets two scheduler connections both acquire
        the same logical lease. ``BEGIN IMMEDIATE`` serializes the complete
        read/modify/write sequence across connections and processes.

        The updater must be a pure in-memory transform; it must not call back
        into storage while this transaction is open.
        """

        safe_key = key[:160]
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT value FROM runtime_kv WHERE key = ?",
                    (safe_key,),
                ).fetchone()
                current = default if row is None else _loads(row["value"], default)
                updated = updater(current)
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO runtime_kv(key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (safe_key, _json(updated), now),
                )
                conn.commit()
            except Exception:  # noqa: BLE001 - atomic mutation must roll back fully
                conn.rollback()
                raise
        return updated

    def list_runtime_values(self, prefix: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if prefix:
                rows = self.connect().execute(
                    """
                    SELECT key, value, updated_at
                    FROM runtime_kv
                    WHERE key LIKE ?
                    ORDER BY updated_at DESC
                    """,
                    (f"{prefix}%",),
                ).fetchall()
            else:
                rows = self.connect().execute(
                    """
                    SELECT key, value, updated_at
                    FROM runtime_kv
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        return [
            {
                "key": row["key"],
                "value": _loads(row["value"], None),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def create_conversation(self, title: str = "Новый диалог") -> str:
        now = utc_now()
        cid = new_id("conv")
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO conversations(id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (cid, title[:200], now, now),
            )
            self.connect().commit()
        return cid

    def ensure_conversation(self, conversation_id: str, title: str = "Новый диалог") -> str:
        """Guarantee a conversation row exists for a caller-supplied id (idempotent).

        A client may drive the chat with its own ``conversation_id`` instead of
        letting the backend mint one. Without a row the first ``add_message`` fails
        the ``messages.conversation_id`` foreign key and the turn 500s. ``INSERT OR
        IGNORE`` under the storage lock creates the row on first use and is a no-op
        (title preserved) on every later turn, with no check-then-insert race.
        """

        now = utc_now()
        with self._lock:
            conn = self.connect()
            conn.execute(
                """
                INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (conversation_id, title[:200], now, now),
            )
            conn.commit()
        return conversation_id

    def add_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        mid = new_id("msg")
        now = utc_now()
        with self._lock:
            conn = self.connect()
            conn.execute(
                """
                INSERT INTO messages(id, conversation_id, role, content, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (mid, conversation_id, role, content, _json(metadata or {}), now),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            self._insert_learning_observation(
                conn,
                kind="conversation.message",
                source_id=mid,
                conversation_id=conversation_id,
                role=role,
                content=content,
                summary=f"{role} message captured for learning",
                payload={"metadata": metadata or {}},
                ts=now,
            )
            conn.commit()
        return mid

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT
                    c.id,
                    c.title,
                    c.created_at,
                    c.updated_at,
                    COUNT(m.id) AS message_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                GROUP BY c.id
                ORDER BY c.updated_at DESC, c.rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                """
                SELECT
                    c.id,
                    c.title,
                    c.created_at,
                    c.updated_at,
                    COUNT(m.id) AS message_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                WHERE c.id = ?
                GROUP BY c.id
                """,
                (conversation_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                """
                SELECT id, conversation_id, role, content, metadata, created_at
                FROM messages
                WHERE id = ?
                """,
                (message_id,),
            ).fetchone()
        if row is None:
            return None
        return {**dict(row), "metadata": _loads(row["metadata"], {})}

    def set_message_feedback(
        self,
        message_id: str,
        *,
        rating: str,
        comment: str = "",
    ) -> dict[str, Any] | None:
        """Persist operator feedback on an assistant message.

        The rating lands in the message metadata (so the UI can restore it) and
        in the append-only learning journal (so the learning tick can turn it
        into a durable lesson even after the visible chat is deleted).
        """

        message = self.get_message(message_id)
        if message is None:
            return None
        rating = "up" if str(rating).strip().lower() == "up" else "down"
        comment = " ".join(str(comment or "").split())[:600]
        feedback = {"rating": rating, "comment": comment, "ts": utc_now()}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        metadata = {**metadata, "feedback": feedback}
        with self._lock:
            conn = self.connect()
            conn.execute(
                "UPDATE messages SET metadata = ? WHERE id = ?",
                (_json(metadata), message_id),
            )
            self._insert_learning_observation(
                conn,
                kind="operator.feedback",
                source_id=message_id,
                conversation_id=message.get("conversation_id"),
                role="operator",
                content=str(message.get("content") or "")[:2000],
                summary=(
                    f"Operator rated an answer {rating}"
                    + (f": {comment}" if comment else ".")
                ),
                payload={"rating": rating, "comment": comment, "message_id": message_id},
            )
            conn.commit()
        self.record_audit(
            actor="operator",
            action="message.feedback",
            target_type="message",
            target_id=message_id,
            summary=f"Feedback {rating}" + (f": {comment[:120]}" if comment else ""),
            after=feedback,
        )
        self.add_event(
            kind="feedback",
            title=f"Оценка ответа: {'полезно' if rating == 'up' else 'мимо задачи'}",
            level="info" if rating == "up" else "warn",
            payload={
                "message_id": message_id,
                "conversation_id": message.get("conversation_id"),
                "rating": rating,
                "comment": comment,
            },
        )
        return {**message, "metadata": metadata}

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            conn = self.connect()
            existing = conn.execute(
                "SELECT id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if existing is None:
                return False
            message_count = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["c"]
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            self._insert_learning_observation(
                conn,
                kind="conversation.deleted",
                source_id=conversation_id,
                conversation_id=conversation_id,
                summary=f"Conversation deleted from UI history with {message_count} message(s).",
                payload={"message_count": int(message_count)},
            )
            conn.commit()
        return True

    def list_messages(self, conversation_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT id, conversation_id, role, content, metadata, created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [
            {**dict(row), "metadata": _loads(row["metadata"], {})}
            for row in rows
        ]

    def list_messages_slice(
        self,
        conversation_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT id, conversation_id, role, content, metadata, created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT ? OFFSET ?
                """,
                (conversation_id, max(1, limit), max(0, offset)),
            ).fetchall()
        return [
            {**dict(row), "metadata": _loads(row["metadata"], {})}
            for row in rows
        ]

    def recent_messages(self, conversation_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT role, content, metadata, created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
        return [
            {**dict(row), "metadata": _loads(row["metadata"], {})}
            for row in reversed(rows)
        ]

    def add_memory(
        self,
        *,
        content: str,
        namespace: str = "core",
        tags: Iterable[str] = (),
        importance: float = 0.5,
    ) -> dict[str, Any]:
        now = utc_now()
        content = " ".join(str(content).split()).strip()[:20000]
        namespace = (str(namespace).strip() or "core")[:80]
        tags = _normalize_tags(tags)
        importance = max(0.0, min(1.0, float(importance)))
        content_key = _normalize_memory_content(content)
        row = {
            "id": new_id("mem"),
            "namespace": namespace,
            "content": content,
            "tags": tags,
            "importance": importance,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            conn = self.connect()
            existing_rows = conn.execute(
                """
                SELECT id, namespace, content, tags, importance, created_at, updated_at
                FROM memories
                WHERE namespace = ?
                ORDER BY updated_at DESC
                LIMIT 250
                """,
                (namespace,),
            ).fetchall()
            for existing in existing_rows:
                existing_dict = dict(existing)
                if _normalize_memory_content(str(existing_dict["content"])) != content_key:
                    continue
                merged_tags = _merge_tags(_loads(existing_dict["tags"], []), tags)
                merged_importance = max(float(existing_dict["importance"] or 0), importance)
                row = {
                    **existing_dict,
                    "tags": merged_tags,
                    "importance": merged_importance,
                    "updated_at": now,
                }
                conn.execute(
                    """
                    UPDATE memories
                    SET tags = ?, importance = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_json(merged_tags), merged_importance, now, row["id"]),
                )
                self._replace_memory_fts(conn, row)
                self.memory_vault.upsert_memory(row)
                conn.commit()
                self.record_audit(
                    actor="system",
                    action="memory.merge",
                    target_type="memory",
                    target_id=row["id"],
                    summary=f"Memory refreshed in namespace {row['namespace']}.",
                    after=row,
                )
                return row
            conn.execute(
                """
                INSERT INTO memories(
                    id, namespace, content, tags, importance, created_at, updated_at
                )
                VALUES (
                    :id, :namespace, :content, :tags, :importance, :created_at, :updated_at
                )
                """,
                {**row, "tags": _json(row["tags"])},
            )
            self._replace_memory_fts(conn, row)
            self.memory_vault.upsert_memory(row)
            conn.commit()
        self.record_audit(
            actor="system",
            action="memory.create",
            target_type="memory",
            target_id=row["id"],
            summary=f"Memory saved in namespace {row['namespace']}.",
            after=row,
        )
        return row

    def _replace_memory_fts(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        if not self._memory_fts_available:
            return
        conn.execute("DELETE FROM memories_fts WHERE id = ?", (row["id"],))
        conn.execute(
            """
            INSERT INTO memories_fts(id, namespace, content, tags)
            VALUES (?, ?, ?, ?)
            """,
            (row["id"], row["namespace"], row["content"], _json(row["tags"])),
        )

    def search_memory(
        self,
        query: str | None = None,
        limit: int = 25,
        *,
        namespaces: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        namespace_filter = [str(item) for item in (namespaces or []) if str(item).strip()]
        seen_ids: set[str] = set()
        decorated: list[dict[str, Any]] = []

        def add_rows(rows: Iterable[sqlite3.Row]) -> None:
            for row in rows:
                item = dict(row)
                if item["id"] in seen_ids:
                    continue
                seen_ids.add(item["id"])
                decorated.append(_decorate_memory_hit(item, query))

        if query and self._memory_fts_available:
            match = _fts_query(query)
            if match:
                try:
                    namespace_sql = ""
                    params: list[Any] = [match]
                    if namespace_filter:
                        placeholders = ",".join("?" for _ in namespace_filter)
                        namespace_sql = f" AND m.namespace IN ({placeholders})"
                        params.extend(namespace_filter)
                    oversample = max(limit * 4, limit)
                    params.append(min(200, oversample))
                    with self._lock:
                        rows = self.connect().execute(
                            f"""
                            SELECT
                                m.id,
                                m.namespace,
                                m.content,
                                m.tags,
                                m.importance,
                                m.created_at,
                                m.updated_at,
                                bm25(memories_fts) AS rank
                            FROM memories_fts
                            JOIN memories m ON m.id = memories_fts.id
                            WHERE memories_fts MATCH ?
                            {namespace_sql}
                            ORDER BY rank ASC, m.importance DESC, m.updated_at DESC
                            LIMIT ?
                            """,
                            tuple(params),
                        ).fetchall()
                    add_rows(rows)
                except sqlite3.OperationalError as exc:
                    if not _recoverable_fts_error(exc):
                        raise

        if query and len(decorated) < limit:
            terms = _query_terms(query, limit=8)
            clauses: list[str] = []
            params: list[Any] = []
            for term in terms:
                like = f"%{term}%"
                clauses.append("(content LIKE ? OR tags LIKE ? OR namespace LIKE ?)")
                params.extend([like, like, like])
            namespace_sql = ""
            if namespace_filter:
                placeholders = ",".join("?" for _ in namespace_filter)
                namespace_sql = f" AND namespace IN ({placeholders})"
                params.extend(namespace_filter)
            if clauses:
                sql = f"""
                    SELECT
                        id, namespace, content, tags, importance, created_at, updated_at,
                        NULL AS rank
                    FROM memories
                    WHERE ({" OR ".join(clauses)}){namespace_sql}
                    ORDER BY importance DESC, updated_at DESC
                    LIMIT ?
                """
                params.append(min(200, max(limit * 4, limit)))
                with self._lock:
                    rows = self.connect().execute(sql, tuple(params)).fetchall()
                add_rows(rows)

        if decorated:
            decorated.sort(key=_memory_sort_key, reverse=True)
            return decorated[:limit]

        params: list[Any]
        where = ""
        namespace_sql = ""
        if namespace_filter:
            placeholders = ",".join("?" for _ in namespace_filter)
            namespace_sql = f"namespace IN ({placeholders})"
        if query:
            where = "content LIKE ? OR tags LIKE ? OR namespace LIKE ?"
            like = f"%{query}%"
            params = [like, like, like]
        else:
            params = []
        if namespace_sql:
            where = f"({where}) AND {namespace_sql}" if where else namespace_sql
            params.extend(namespace_filter)
        params.append(limit)
        sql = f"""
            SELECT id, namespace, content, tags, importance, created_at, updated_at, NULL AS rank
            FROM memories
            {"WHERE " + where if where else ""}
            ORDER BY importance DESC, updated_at DESC
            LIMIT ?
        """
        with self._lock:
            rows = self.connect().execute(sql, tuple(params)).fetchall()
        return [_decorate_memory_hit(dict(row), query) for row in rows]

    def consolidate_memories(self, limit: int = 1000) -> dict[str, int]:
        with self._lock:
            conn = self.connect()
            rows = conn.execute(
                """
                SELECT id, namespace, content, tags, importance, created_at, updated_at
                FROM memories
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (max(1, min(5000, limit)),),
            ).fetchall()
            groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for row in rows:
                item = dict(row)
                key = (str(item["namespace"]), _normalize_memory_content(str(item["content"])))
                if not key[1]:
                    continue
                groups.setdefault(key, []).append(item)

            removed = 0
            merged = 0
            for items in groups.values():
                if len(items) < 2:
                    continue
                keep = max(
                    items,
                    key=lambda item: (
                        float(item.get("importance") or 0),
                        str(item.get("updated_at") or ""),
                    ),
                )
                duplicate_ids = [item["id"] for item in items if item["id"] != keep["id"]]
                merged_tags = _merge_tags(*(_loads(item.get("tags"), []) for item in items))
                merged_importance = max(float(item.get("importance") or 0) for item in items)
                now = utc_now()
                conn.execute(
                    """
                    UPDATE memories
                    SET tags = ?, importance = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_json(merged_tags), merged_importance, now, keep["id"]),
                )
                keep = {
                    **keep,
                    "tags": merged_tags,
                    "importance": merged_importance,
                    "updated_at": now,
                }
                self._replace_memory_fts(conn, keep)
                for duplicate_id in duplicate_ids:
                    if self._memory_fts_available:
                        conn.execute("DELETE FROM memories_fts WHERE id = ?", (duplicate_id,))
                    conn.execute("DELETE FROM memories WHERE id = ?", (duplicate_id,))
                    self.memory_vault.remove_memory(str(duplicate_id))
                removed += len(duplicate_ids)
                merged += 1
            conn.commit()
        if removed:
            self.add_event(
                kind="memory.consolidate",
                title=(
                    f"Memory consolidation merged {merged} group(s), "
                    f"removed {removed} duplicate(s)"
                ),
                payload={"examined": len(rows), "merged": merged, "removed": removed},
            )
        return {"examined": len(rows), "merged": merged, "removed": removed}

    def memory_graph(self) -> dict[str, Any]:
        with self._lock:
            conn = self.connect()
            self._sync_memory_vault(conn)
        return self.memory_vault.graph()

    def rebuild_memory_vault(self) -> dict[str, Any]:
        with self._lock:
            conn = self.connect()
            self._sync_memory_vault(conn)
        return self.memory_vault.graph()

    def create_mission(
        self,
        *,
        title: str,
        goal: str,
        tasks: list[str],
        mission_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a mission once, optionally under a caller-reserved durable id."""

        now = utc_now()
        selected_id = str(mission_id or new_id("mis"))
        if not re.fullmatch(r"mis_[A-Za-z0-9_-]{8,80}", selected_id):
            raise ValueError("mission_id must be a bounded Jarvis mission identifier")
        created = False
        with self._lock:
            conn = self.connect()
            if conn.in_transaction:
                raise RuntimeError("mission creation requires a clean storage transaction")
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO missions(
                        id, title, goal, status, progress, created_at, updated_at
                    )
                    VALUES (?, ?, ?, 'planned', 0, ?, ?)
                    """,
                    (selected_id, title[:240], goal, now, now),
                )
                created = cursor.rowcount == 1
                if created:
                    for position, task_title in enumerate(tasks, start=1):
                        conn.execute(
                            """
                            INSERT INTO mission_tasks(
                                id, mission_id, title, status, notes, position,
                                created_at, updated_at
                            )
                            VALUES (?, ?, ?, 'pending', NULL, ?, ?, ?)
                            """,
                            (
                                new_id("task"),
                                selected_id,
                                task_title,
                                position,
                                now,
                                now,
                            ),
                        )
                    self._refresh_mission_progress(conn, selected_id, now=now)
                else:
                    existing = conn.execute(
                        "SELECT goal FROM missions WHERE id = ?",
                        (selected_id,),
                    ).fetchone()
                    if existing is None or str(existing["goal"]) != goal:
                        raise ValueError(
                            "mission_id is already bound to a different goal"
                        )
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
        mission = self.get_mission(selected_id)
        if mission is None:
            raise RuntimeError("Mission was not persisted")
        if created:
            self.record_audit(
                actor="system",
                action="mission.create",
                target_type="mission",
                target_id=selected_id,
                summary=f"Mission created: {mission['title']}",
                after=mission,
            )
        return mission

    def list_missions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT id, title, goal, status, progress, created_at, updated_at
                FROM missions
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        missions = [dict(row) for row in rows]
        for mission in missions:
            mission["tasks"] = self.list_mission_tasks(mission["id"])
        return missions

    def get_mission(self, mission_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                """
                SELECT id, title, goal, status, progress, created_at, updated_at
                FROM missions
                WHERE id = ?
                """,
                (mission_id,),
            ).fetchone()
        if row is None:
            return None
        mission = dict(row)
        mission["tasks"] = self.list_mission_tasks(mission_id)
        return mission

    def next_mission_task(self, mission_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                """
                SELECT id, mission_id, title, status, notes, position, created_at, updated_at
                FROM mission_tasks
                WHERE mission_id = ? AND status = 'pending'
                ORDER BY position ASC
                LIMIT 1
                """,
                (mission_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_mission_task(
        self,
        mission_id: str,
        *,
        title: str,
        position: int | None = None,
    ) -> dict[str, Any]:
        """Append or insert one planner-owned task into an existing mission."""

        clean_title = str(title).strip()
        if not clean_title:
            raise ValueError("mission task title is required")
        now = utc_now()
        task_id = new_id("task")
        with self._lock:
            conn = self.connect()
            mission = conn.execute("SELECT id FROM missions WHERE id = ?", (mission_id,)).fetchone()
            if mission is None:
                raise KeyError(f"unknown mission: {mission_id}")
            last_position = int(
                conn.execute(
                    "SELECT COALESCE(MAX(position), 0) AS value FROM mission_tasks "
                    "WHERE mission_id = ?",
                    (mission_id,),
                ).fetchone()["value"]
            )
            selected = last_position + 1 if position is None else int(position)
            if selected < 1 or selected > last_position + 1:
                raise ValueError("mission task position is outside the insert range")
            if selected <= last_position:
                conn.execute(
                    "UPDATE mission_tasks SET position = position + 1, updated_at = ? "
                    "WHERE mission_id = ? AND position >= ?",
                    (now, mission_id, selected),
                )
            conn.execute(
                """
                INSERT INTO mission_tasks(
                    id, mission_id, title, status, notes, position, created_at, updated_at
                )
                VALUES (?, ?, ?, 'pending', NULL, ?, ?, ?)
                """,
                (task_id, mission_id, clean_title[:500], selected, now, now),
            )
            self._refresh_mission_progress(conn, mission_id, now=now)
            conn.commit()
            row = conn.execute(
                "SELECT id, mission_id, title, status, notes, position, created_at, updated_at "
                "FROM mission_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("mission task was not persisted")
        task = dict(row)
        self.record_audit(
            actor="system",
            action="mission.task.add",
            target_type="mission_task",
            target_id=task_id,
            summary=f"Mission task added: {task['title']}",
            after=task,
        )
        return task

    def claim_mission_task(self, mission_id: str, task_id: str) -> dict[str, Any] | None:
        """Atomically claim a specific DAG-ready task when no sibling is running."""

        now = utc_now()
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                """
                UPDATE mission_tasks
                SET status = 'running', updated_at = ?
                WHERE id = ? AND mission_id = ? AND status = 'pending'
                  AND NOT EXISTS (
                      SELECT 1 FROM mission_tasks AS active
                      WHERE active.mission_id = ? AND active.status = 'running'
                  )
                RETURNING id, mission_id, title, status, notes, position, created_at, updated_at
                """,
                (now, task_id, mission_id, mission_id),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            claimed = dict(row)
            self._refresh_mission_progress(conn, mission_id, now=now)
            conn.commit()
        self.record_audit(
            actor="system",
            action="mission.task.claim",
            target_type="mission_task",
            target_id=task_id,
            summary=f"DAG mission task claimed: {claimed['title']}",
            after=claimed,
        )
        return claimed

    def claim_next_mission_task(self, mission_id: str) -> dict[str, Any] | None:
        """Atomically claim the next task when no sibling task is already running."""

        now = utc_now()
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                """
                UPDATE mission_tasks
                SET status = 'running', updated_at = ?
                WHERE id = (
                    SELECT pending.id
                    FROM mission_tasks AS pending
                    WHERE pending.mission_id = ?
                      AND pending.status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM mission_tasks AS active
                          WHERE active.mission_id = pending.mission_id
                            AND active.status IN ('running', 'blocked')
                      )
                    ORDER BY pending.position ASC
                    LIMIT 1
                )
                  AND status = 'pending'
                RETURNING
                    id, mission_id, title, status, notes, position, created_at, updated_at
                """,
                (now, mission_id),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            claimed = dict(row)
            self._refresh_mission_progress(conn, mission_id, now=now)
            conn.commit()
        self.record_audit(
            actor="system",
            action="mission.task.claim",
            target_type="mission_task",
            target_id=str(claimed["id"]),
            summary=f"Mission task claimed: {claimed['title']}",
            after=claimed,
        )
        return claimed

    # ---- Reminders -----------------------------------------------------------

    @staticmethod
    def _decode_reminder(row: Any) -> dict[str, Any]:
        reminder = dict(row)
        reminder["recurrence"] = _loads(reminder.get("recurrence"), None)
        reminder["payload"] = _loads(reminder.get("payload"), {})
        return reminder

    def create_reminder(
        self,
        *,
        text: str,
        due_at: str,
        recurrence: dict[str, Any] | None = None,
        conversation_id: str | None = None,
        source_text: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a pending reminder. ``due_at`` is a UTC ISO string (see reminders.to_utc_iso)."""

        now = utc_now()
        row = {
            "id": new_id("rem"),
            "created_at": now,
            "updated_at": now,
            "text": text.strip()[:500],
            "due_at": due_at,
            "recurrence": recurrence or None,
            "status": "pending",
            "conversation_id": conversation_id,
            "source_text": (source_text or "")[:1000],
            "fired_at": None,
            "fire_count": 0,
            "payload": payload or {},
        }
        with self._lock:
            conn = self.connect()
            conn.execute(
                """
                INSERT INTO reminders(
                    id, created_at, updated_at, text, due_at, recurrence, status,
                    conversation_id, source_text, fired_at, fire_count, payload
                )
                VALUES (
                    :id, :created_at, :updated_at, :text, :due_at, :recurrence, :status,
                    :conversation_id, :source_text, :fired_at, :fire_count, :payload
                )
                """,
                {
                    **row,
                    "recurrence": _json(row["recurrence"]) if row["recurrence"] else None,
                    "payload": _json(row["payload"]),
                },
            )
            conn.commit()
        self.record_audit(
            actor="operator",
            action="reminder.create",
            target_type="reminder",
            target_id=row["id"],
            summary=f"Reminder: {row['text']} @ {due_at}",
            after=row,
        )
        return row

    def list_reminders(
        self,
        *,
        status: str | None = "pending",
        before: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT id, created_at, updated_at, text, due_at, recurrence, status, "
            "conversation_id, source_text, fired_at, fire_count, payload FROM reminders"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if before:
            clauses.append("due_at <= ?")
            params.append(before)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY due_at ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self.connect().execute(query, tuple(params)).fetchall()
        return [self._decode_reminder(row) for row in rows]

    def get_reminder(self, reminder_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                """
                SELECT id, created_at, updated_at, text, due_at, recurrence, status,
                       conversation_id, source_text, fired_at, fire_count, payload
                FROM reminders
                WHERE id = ?
                """,
                (reminder_id,),
            ).fetchone()
        return self._decode_reminder(row) if row is not None else None

    def cancel_reminder(self, reminder_id: str) -> dict[str, Any] | None:
        """Cancel a still-pending reminder, guarding against a concurrent fire."""

        now = utc_now()
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                """
                UPDATE reminders
                SET status = 'cancelled', updated_at = ?
                WHERE id = ? AND status = 'pending'
                RETURNING id, created_at, updated_at, text, due_at, recurrence, status,
                          conversation_id, source_text, fired_at, fire_count, payload
                """,
                (now, reminder_id),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        cancelled = self._decode_reminder(row)
        self.record_audit(
            actor="operator",
            action="reminder.cancel",
            target_type="reminder",
            target_id=reminder_id,
            summary=f"Reminder cancelled: {cancelled['text']}",
            after=cancelled,
        )
        return cancelled

    def claim_due_reminders(
        self,
        now_iso: str | None = None,
        *,
        tz_name: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Atomically fire every due reminder in a single BEGIN IMMEDIATE transaction.

        One-shot rows flip to ``fired``; recurring rows advance ``due_at`` to the first
        occurrence strictly after ``now`` (rolled past downtime in one shot — no
        catch-up burst) and stay ``pending``. Mirrors ``create_mission`` /
        ``claim_next_mission_task``: ``BEGIN IMMEDIATE`` plus a ``status='pending'``
        guard on every UPDATE prevents a double fire even if a manual run races the loop.
        Returns one snapshot per fired reminder (its state *before* advancement).
        """

        from .reminders import compute_next_due, reminder_zone, to_utc_iso

        now = now_iso or utc_now()
        tz = reminder_zone(tz_name) if tz_name else reminder_zone()
        now_local = datetime.fromisoformat(now).astimezone(tz)
        fired: list[dict[str, Any]] = []
        with self._lock:
            conn = self.connect()
            if conn.in_transaction:
                raise RuntimeError("reminder claim requires a clean storage transaction")
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    SELECT id, created_at, updated_at, text, due_at, recurrence, status,
                           conversation_id, source_text, fired_at, fire_count, payload
                    FROM reminders
                    WHERE status = 'pending' AND due_at <= ?
                    ORDER BY due_at ASC
                    LIMIT ?
                    """,
                    (now, limit),
                ).fetchall()
                for row in rows:
                    snapshot = self._decode_reminder(row)
                    recurrence = snapshot["recurrence"]
                    next_due = (
                        compute_next_due(recurrence, after=now_local, tz=tz)
                        if isinstance(recurrence, dict)
                        else None
                    )
                    if next_due is not None:
                        conn.execute(
                            """
                            UPDATE reminders
                            SET due_at = ?, fired_at = ?, fire_count = fire_count + 1,
                                updated_at = ?
                            WHERE id = ? AND status = 'pending'
                            """,
                            (to_utc_iso(next_due), now, now, snapshot["id"]),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE reminders
                            SET status = 'fired', fired_at = ?, fire_count = fire_count + 1,
                                updated_at = ?
                            WHERE id = ? AND status = 'pending'
                            """,
                            (now, now, snapshot["id"]),
                        )
                    fired.append(snapshot)
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
        return fired

    def update_mission_task(
        self,
        task_id: str,
        *,
        mission_id: str | None = None,
        title: str | None = None,
        status: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._lock:
            conn = self.connect()
            existing = conn.execute(
                """
                SELECT id, mission_id, title, status, notes, position, created_at, updated_at
                FROM mission_tasks
                WHERE id = ? AND (? IS NULL OR mission_id = ?)
                """,
                (task_id, mission_id, mission_id),
            ).fetchone()
            if existing is None:
                return None
            before = dict(existing)
            next_title = title if title is not None else existing["title"]
            next_status = status if status is not None else existing["status"]
            next_notes = notes if notes is not None else existing["notes"]
            conn.execute(
                """
                UPDATE mission_tasks
                SET title = ?, status = ?, notes = ?, updated_at = ?
                WHERE id = ? AND (? IS NULL OR mission_id = ?)
                """,
                (next_title, next_status, next_notes, now, task_id, mission_id, mission_id),
            )
            self._refresh_mission_progress(conn, existing["mission_id"], now=now)
            conn.commit()
            row = conn.execute(
                """
                SELECT id, mission_id, title, status, notes, position, created_at, updated_at
                FROM mission_tasks
                WHERE id = ?
                """,
                (task_id,),
            ).fetchone()
        updated = dict(row) if row else None
        if updated:
            self.record_audit(
                actor="system",
                action="mission.task.update",
                target_type="mission_task",
                target_id=task_id,
                summary=f"Task status is {updated['status']}: {updated['title']}",
                before=before,
                after=updated,
            )
        return updated

    def list_mission_tasks(self, mission_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT id, mission_id, title, status, notes, position, created_at, updated_at
                FROM mission_tasks
                WHERE mission_id = ?
                ORDER BY position ASC
                """,
                (mission_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_tool_run(
        self,
        *,
        tool: str,
        ok: bool,
        summary: str,
        arguments: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        mission_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        row = {
            "id": new_id("run"),
            "ts": utc_now(),
            "tool": tool,
            "ok": 1 if ok else 0,
            "summary": _redact_sensitive_text(summary),
            "arguments": _redact_sensitive(arguments or {}),
            "data": _redact_sensitive(data or {}),
            "mission_id": mission_id,
            "task_id": task_id,
        }
        with self._lock:
            conn = self.connect()
            conn.execute(
                """
                INSERT INTO tool_runs(
                    id, ts, tool, ok, summary, arguments, data, mission_id, task_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["ts"],
                    row["tool"],
                    row["ok"],
                    row["summary"],
                    _json(row["arguments"]),
                    _json(row["data"]),
                    row["mission_id"],
                    row["task_id"],
                ),
            )
            self._insert_learning_observation(
                conn,
                kind=f"tool.{row['tool']}",
                source_id=row["id"],
                content=row["summary"],
                summary=row["summary"],
                payload={
                    "tool": row["tool"],
                    "ok": bool(row["ok"]),
                    "arguments": row["arguments"],
                    "data": row["data"],
                    "mission_id": row["mission_id"],
                    "task_id": row["task_id"],
                },
                ts=row["ts"],
            )
            conn.commit()
        result = {**row, "ok": bool(row["ok"])}
        self.record_audit(
            actor="agent",
            action="tool.run",
            target_type="tool_run",
            target_id=row["id"],
            summary=row["summary"],
            after=result,
        )
        return result

    def list_tool_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT id, ts, tool, ok, summary, arguments, data, mission_id, task_id
                FROM tool_runs
                ORDER BY ts DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                **dict(row),
                "ok": bool(row["ok"]),
                "arguments": _loads(row["arguments"], {}),
                "data": _loads(row["data"], {}),
            }
            for row in rows
        ]

    def record_learning_observation(
        self,
        *,
        kind: str,
        source_id: str | None = None,
        conversation_id: str | None = None,
        role: str | None = None,
        content: str = "",
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self._learning_observation_row(
            kind=kind,
            source_id=source_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            summary=summary,
            payload=payload,
        )
        with self._lock:
            conn = self.connect()
            self._insert_learning_observation(conn, **row)
            conn.commit()
        return row

    def list_learning_observations(
        self,
        *,
        limit: int = 50,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if kind:
            where = "WHERE kind = ?"
            params.append(kind)
        params.append(limit)
        with self._lock:
            rows = self.connect().execute(
                f"""
                SELECT
                    id, ts, kind, source_id, conversation_id, role,
                    content, summary, payload
                FROM learning_observations
                {where}
                ORDER BY ts DESC, rowid DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                **dict(row),
                "payload": _loads(row["payload"], {}),
            }
            for row in rows
        ]

    def _learning_observation_row(
        self,
        *,
        kind: str,
        source_id: str | None = None,
        conversation_id: str | None = None,
        role: str | None = None,
        content: str = "",
        summary: str = "",
        payload: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": new_id("learn"),
            "ts": ts or utc_now(),
            "kind": kind[:120],
            "source_id": source_id,
            "conversation_id": conversation_id,
            "role": role[:40] if role else None,
            "content": str(content or "")[:20000],
            "summary": str(summary or "")[:1000],
            "payload": payload or {},
        }

    def _insert_learning_observation(
        self,
        conn: sqlite3.Connection,
        **kwargs: Any,
    ) -> dict[str, Any]:
        row = (
            kwargs
            if "id" in kwargs
            else self._learning_observation_row(**kwargs)
        )
        conn.execute(
            """
            INSERT INTO learning_observations(
                id, ts, kind, source_id, conversation_id, role, content, summary, payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["ts"],
                row["kind"],
                row["source_id"],
                row["conversation_id"],
                row["role"],
                row["content"],
                row["summary"],
                _json(row["payload"]),
            ),
        )
        return row

    def record_audit(
        self,
        *,
        actor: str,
        action: str,
        target_type: str,
        summary: str,
        target_id: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "id": new_id("aud"),
            "ts": utc_now(),
            "actor": actor[:80],
            "action": action[:120],
            "target_type": target_type[:80],
            "target_id": target_id,
            "summary": summary[:500],
            "before": before or {},
            "after": after or {},
        }
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO audit_log(
                    id, ts, actor, action, target_type, target_id, summary, before_json, after_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["ts"],
                    row["actor"],
                    row["action"],
                    row["target_type"],
                    row["target_id"],
                    row["summary"],
                    _json(row["before"]),
                    _json(row["after"]),
                ),
            )
            self.connect().commit()
        return row

    def list_audit(
        self,
        *,
        limit: int = 50,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if target_type:
            conditions.append("target_type = ?")
            params.append(target_type)
        if target_id:
            conditions.append("target_id = ?")
            params.append(target_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        with self._lock:
            rows = self.connect().execute(
                f"""
                SELECT
                    id, ts, actor, action, target_type, target_id, summary,
                    before_json, after_json
                FROM audit_log
                {where}
                ORDER BY ts DESC, rowid DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "actor": row["actor"],
                "action": row["action"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "summary": row["summary"],
                "before": _loads(row["before_json"], {}),
                "after": _loads(row["after_json"], {}),
            }
            for row in rows
        ]

    def create_file_record(
        self,
        *,
        name: str,
        stored_path: Path,
        sha256: str,
        size: int,
        mime_type: str,
        status: str,
        source_path: Path | None = None,
        error: str | None = None,
        chunk_count: int = 0,
    ) -> dict[str, Any]:
        now = utc_now()
        row = {
            "id": new_id("file"),
            "name": name[:260],
            "source_path": str(source_path) if source_path else None,
            "stored_path": str(stored_path),
            "mime_type": mime_type[:120],
            "size": int(size),
            "sha256": sha256,
            "status": status[:40],
            "error": error,
            "chunk_count": int(chunk_count),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO files(
                    id, name, source_path, stored_path, mime_type, size, sha256,
                    status, error, chunk_count, created_at, updated_at
                )
                VALUES (
                    :id, :name, :source_path, :stored_path, :mime_type, :size, :sha256,
                    :status, :error, :chunk_count, :created_at, :updated_at
                )
                """,
                row,
            )
            self.connect().commit()
        return row

    def add_file_chunks(
        self,
        file_id: str,
        chunks: list[str],
        *,
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        with self._lock:
            conn = self.connect()
            self._replace_file_chunks(conn, file_id, chunks, now=now)
            if status is None:
                conn.execute(
                    """
                    UPDATE files
                    SET chunk_count = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (len(chunks), now, file_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE files
                    SET chunk_count = ?, status = ?, error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (len(chunks), status[:40], error, now, file_id),
                )
            conn.commit()

    def reindex_file(
        self,
        file_id: str,
        chunks: list[str],
        *,
        name: str,
        stored_path: Path,
        size: int,
        mime_type: str,
        status: str,
        error: str | None,
        source_path: Path | None = None,
    ) -> dict[str, Any] | None:
        """Atomically replace a legacy file's metadata and searchable index."""

        now = utc_now()
        with self._lock:
            conn = self.connect()
            try:
                self._replace_file_chunks(conn, file_id, chunks, now=now)
                conn.execute(
                    """
                    UPDATE files
                    SET name = ?, source_path = ?, stored_path = ?, mime_type = ?, size = ?,
                        chunk_count = ?, status = ?, error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name[:260],
                        str(source_path) if source_path else None,
                        str(stored_path),
                        mime_type[:120],
                        int(size),
                        len(chunks),
                        status[:40],
                        error,
                        now,
                        file_id,
                    ),
                )
                conn.commit()
            except Exception:  # noqa: BLE001 - transaction must roll back on any failure
                conn.rollback()
                raise
        return self.get_file(file_id)

    def _replace_file_chunks(
        self,
        conn: sqlite3.Connection,
        file_id: str,
        chunks: list[str],
        *,
        now: str,
    ) -> None:
        conn.execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))
        if self._file_fts_available:
            conn.execute("DELETE FROM file_chunks_fts WHERE file_id = ?", (file_id,))
        for position, content in enumerate(chunks, start=1):
            chunk_id = new_id("chunk")
            conn.execute(
                """
                INSERT INTO file_chunks(id, file_id, position, content, char_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chunk_id, file_id, position, content, len(content), now),
            )
            if self._file_fts_available:
                conn.execute(
                    """
                    INSERT INTO file_chunks_fts(file_id, chunk_id, content)
                    VALUES (?, ?, ?)
                    """,
                    (file_id, chunk_id, content),
                )

    def list_files(self, limit: int = 50, *, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT
                    id, name, source_path, stored_path, mime_type, size, sha256,
                    status, error, chunk_count, created_at, updated_at
                FROM files
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ? OFFSET ?
                """,
                (limit, max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_files_in_range(
        self, start: str, end: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """List files uploaded in the [start, end) window (ISO created_at), oldest first.

        ``created_at`` is an ISO-8601 UTC string (``utc_now``), so a lexicographic
        comparison against ISO boundaries in the same ``+00:00`` form is a correct time
        range. Used by date-scoped document recall ("какие документы были 15 июля").
        """

        with self._lock:
            rows = self.connect().execute(
                """
                SELECT
                    id, name, source_path, stored_path, mime_type, size, sha256,
                    status, error, chunk_count, created_at, updated_at
                FROM files
                WHERE created_at >= ? AND created_at < ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
                """,
                (start, end, max(1, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_files(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Resolve persisted files by filename and indexed content.

        ``file_chunks_fts`` stores only chunk text, so an FTS-only lookup cannot
        find a remembered filename or a stored document without extractable text.
        Fuse bounded chunk hits with filename matches and return stable file ids
        for follow-on document tools.
        """

        query = " ".join(str(query or "").split()).strip()
        if not query:
            return []
        limit = max(1, min(50, int(limit)))
        terms = _query_terms(query, limit=12)
        if not terms:
            return []

        chunk_hits = self.search_file_chunks(query, limit=max(40, limit * 8))
        chunks_by_file: dict[str, list[dict[str, Any]]] = {}
        chunk_scores: dict[str, float] = {}
        for hit in chunk_hits:
            file_id = str(hit.get("file_id") or "")
            if not file_id:
                continue
            snippets = chunks_by_file.setdefault(file_id, [])
            if len(snippets) < 4:
                snippets.append(
                    {
                        "chunk_id": hit.get("chunk_id"),
                        "position": hit.get("position"),
                        "snippet": hit.get("snippet")
                        or _snippet(str(hit.get("content") or ""), terms),
                        "matched_terms": list(hit.get("matched_terms") or []),
                        "exact_matched_terms": list(
                            hit.get("exact_matched_terms") or []
                        ),
                        "relevance": float(hit.get("relevance") or 0.0),
                    }
                )
            chunk_scores[file_id] = max(
                chunk_scores.get(file_id, 0.0),
                2.0 + float(hit.get("relevance") or 0.0),
            )

        records_by_id: dict[str, dict[str, Any]] = {}
        for file_id in chunks_by_file:
            record = self.get_file(file_id)
            if record is not None:
                records_by_id[file_id] = record
        name_clauses = " OR ".join("name LIKE ?" for _term in terms)
        name_params = [f"%{term}%" for term in terms]
        with self._lock:
            name_rows = self.connect().execute(
                f"""
                SELECT
                    id, name, source_path, stored_path, mime_type, size, sha256,
                    status, error, chunk_count, created_at, updated_at
                FROM files
                WHERE {name_clauses}
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
                """,
                (*name_params, max(100, limit * 20)),
            ).fetchall()
        for row in name_rows:
            record = dict(row)
            records_by_id.setdefault(str(record["id"]), record)
        if any(not term.isascii() for term in terms):
            offset = 0
            while True:
                batch = self.list_files(limit=500, offset=offset)
                if not batch:
                    break
                for record in batch:
                    folded_name = str(record.get("name") or "").casefold()
                    if any(term.casefold() in folded_name for term in terms):
                        records_by_id.setdefault(str(record["id"]), record)
                offset += len(batch)
                if len(batch) < 500:
                    break
        records = list(records_by_id.values())
        normalized_query = query.casefold()
        ranked: list[tuple[float, int, dict[str, Any]]] = []
        for recency, record in enumerate(records):
            file_id = str(record.get("id") or "")
            name = str(record.get("name") or "")
            matched_name_terms = _matched_terms(name, terms)
            exact_name_terms = _matched_terms_exact(name, terms)
            sources: list[str] = []
            score = chunk_scores.get(file_id, 0.0)
            if file_id in chunks_by_file:
                sources.append("content")
            if matched_name_terms:
                sources.append("name")
                score += 3.0 + (2.0 * len(matched_name_terms) / max(1, len(terms)))
            if normalized_query and normalized_query in name.casefold():
                if "name" not in sources:
                    sources.append("name")
                score += 4.0
            if not sources:
                continue
            matched_content_terms = {
                term
                for chunk in chunks_by_file.get(file_id, [])
                for term in chunk.get("exact_matched_terms") or []
            }
            ranked.append(
                (
                    score,
                    recency,
                    {
                        **record,
                        "match_sources": sources,
                        "matched_terms": sorted(
                            {*exact_name_terms, *matched_content_terms},
                            key=str.casefold,
                        ),
                        "match_score": round(score, 4),
                        "matched_chunks": chunks_by_file.get(file_id, []),
                    },
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in ranked[:limit]]

    def get_file(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                """
                SELECT
                    id, name, source_path, stored_path, mime_type, size, sha256,
                    status, error, chunk_count, created_at, updated_at
                FROM files
                WHERE id = ?
                """,
                (file_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_file_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                """
                SELECT
                    id, name, source_path, stored_path, mime_type, size, sha256,
                    status, error, chunk_count, created_at, updated_at
                FROM files
                WHERE sha256 = ?
                ORDER BY
                    CASE WHEN status = 'indexed' THEN 0 ELSE 1 END,
                    CASE WHEN mime_type = 'application/octet-stream' THEN 1 ELSE 0 END,
                    created_at ASC,
                    rowid ASC
                LIMIT 1
                """,
                (sha256,),
            ).fetchone()
        return dict(row) if row else None

    def list_file_chunks(self, file_id: str, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT
                    f.id AS file_id,
                    f.name AS file_name,
                    c.id AS chunk_id,
                    c.position,
                    c.content,
                    c.created_at,
                    NULL AS rank
                FROM file_chunks c
                JOIN files f ON f.id = c.file_id
                WHERE c.file_id = ?
                ORDER BY c.position ASC
                LIMIT ?
                """,
                (file_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_file_chunks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        if query and self._file_fts_available:
            match = _fts_query(query)
            if match:
                try:
                    with self._lock:
                        rows = self.connect().execute(
                            """
                            SELECT
                                f.id AS file_id,
                                f.name AS file_name,
                                c.id AS chunk_id,
                                c.position,
                                c.content,
                                c.created_at,
                                bm25(file_chunks_fts) AS rank
                            FROM file_chunks_fts
                            JOIN file_chunks c ON c.id = file_chunks_fts.chunk_id
                            JOIN files f ON f.id = c.file_id
                            WHERE file_chunks_fts MATCH ?
                            ORDER BY rank ASC, c.position ASC
                            LIMIT ?
                            """,
                            (match, limit),
                        ).fetchall()
                    return [_decorate_file_hit(dict(row), query) for row in rows]
                except sqlite3.OperationalError as exc:
                    if not _recoverable_fts_error(exc):
                        raise

        like = f"%{query}%"
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT
                    f.id AS file_id,
                    f.name AS file_name,
                    c.id AS chunk_id,
                    c.position,
                    c.content,
                    c.created_at,
                    NULL AS rank
                FROM file_chunks c
                JOIN files f ON f.id = c.file_id
                WHERE c.content LIKE ? OR f.name LIKE ?
                ORDER BY f.updated_at DESC, c.position ASC
                LIMIT ?
                """,
                (like, like, limit),
            ).fetchall()
        return [_decorate_file_hit(dict(row), query) for row in rows]

    def recent_file_chunks(self, limit: int = 20) -> list[dict[str, Any]]:
        """Chunks of the most recently updated files, as a semantic fallback pool.

        Used when lexical file search finds nothing (zero token overlap with the
        query) so hybrid retrieval still has candidates to re-rank — the file
        analog of the recent/important memory pool.
        """

        with self._lock:
            rows = self.connect().execute(
                """
                SELECT
                    f.id AS file_id,
                    f.name AS file_name,
                    c.id AS chunk_id,
                    c.position,
                    c.content,
                    c.created_at,
                    NULL AS rank
                FROM file_chunks c
                JOIN files f ON f.id = c.file_id
                ORDER BY f.updated_at DESC, f.rowid DESC, c.position ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_approval(
        self,
        *,
        title: str,
        description: str,
        requested_action: str,
        risk: str = "review",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        row = {
            "id": new_id("apr"),
            "created_at": now,
            "updated_at": now,
            "status": "pending",
            "risk": risk[:40],
            "title": title[:240],
            "description": description,
            "requested_action": requested_action[:120],
            "payload": payload or {},
            "result": {},
        }
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO approvals(
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["created_at"],
                    row["updated_at"],
                    row["status"],
                    row["risk"],
                    row["title"],
                    row["description"],
                    row["requested_action"],
                    _json(row["payload"]),
                    _json(row["result"]),
                ),
            )
            self.connect().commit()
        self.record_audit(
            actor="agent",
            action="approval.request",
            target_type="approval",
            target_id=row["id"],
            summary=f"Approval requested: {row['title']}",
            after=row,
        )
        return row

    def list_approvals(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        params: tuple[Any, ...]
        where = ""
        if status:
            where = "WHERE status = ?"
            params = (status, limit)
        else:
            params = (limit,)
        with self._lock:
            rows = self.connect().execute(
                f"""
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                {where}
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                **dict(row),
                "payload": _loads(row["payload"], {}),
                "result": _loads(row["result"], {}),
            }
            for row in rows
        ]

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE id = ?
                """,
                (approval_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            **dict(row),
            "payload": _loads(row["payload"], {}),
            "result": _loads(row["result"], {}),
        }

    def update_approval(
        self,
        approval_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._lock:
            conn = self.connect()
            existing = conn.execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE id = ?
                """,
                (approval_id,),
            ).fetchone()
            if existing is None:
                return None
            before = _approval_record(existing)
            allowed_transitions = {
                "pending": {"approved", "rejected", "cancelled"},
                "approved": {"rejected", "cancelled"},
            }
            current_status = str(existing["status"])
            if status not in allowed_transitions.get(current_status, set()):
                raise ValueError(
                    f"Approval cannot transition from {current_status!r} to {status!r}."
                )
            sanitized_result = redact_value(result or {})
            stored_result = (
                sanitized_result if isinstance(sanitized_result, dict) else {}
            )
            payload = before.get("payload")
            mission_bound = bool(
                isinstance(payload, dict)
                and str(payload.get("mission_id") or "").strip()
                and str(payload.get("task_id") or "").strip()
            )
            if status in {"rejected", "cancelled"} and mission_bound:
                # The approval transition commits before the async executive
                # callback.  This outbox marker makes that second half durable
                # across client disconnects and process/power loss.
                stored_result["reconciliation"] = {
                    "protocol": "jarvis.approval-reconciliation.v1",
                    "status": "pending",
                    "attempts": 0,
                    "mode": f"operator_{status}",
                    "created_at": now,
                }
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, result = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (status, _json(stored_result), now, approval_id, current_status),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise ValueError("Approval state changed concurrently; reload before updating.")
            conn.commit()
            row = conn.execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE id = ?
                """,
                (approval_id,),
            ).fetchone()
        updated = _approval_record(row) if row else None
        if updated:
            self.record_audit(
                actor="operator",
                action="approval.update",
                target_type="approval",
                target_id=approval_id,
                summary=f"Approval {status}: {updated['title']}",
                before=before,
                after=updated,
            )
        return updated

    def invalidate_mission_approval(
        self,
        approval_id: str,
        *,
        reason: str,
    ) -> dict[str, Any] | None:
        """Cancel a stale executive capability with reconciliation already complete.

        Environment/plan recovery performs the branch adaptation itself.  Routing
        this transition through ``update_approval`` would create an operator
        cancellation outbox entry and later abort the newly adapted branch a
        second time.  This dedicated transition records, atomically, that the
        coordinator has already reconciled the invalidated capability.
        """

        now = utc_now()
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE id = ?
                """,
                (approval_id,),
            ).fetchone()
            if row is None:
                return None
            before = _approval_record(row)
            payload = before.get("payload")
            claim = payload.get("executive_claim") if isinstance(payload, dict) else None
            if not (
                isinstance(payload, dict)
                and str(payload.get("mission_id") or "").strip()
                and str(payload.get("task_id") or "").strip()
                and isinstance(claim, dict)
                and claim.get("protocol") == "jarvis.executive-approval.v1"
            ):
                raise ValueError("Only a mission-bound executive approval can be invalidated.")
            current_status = str(before.get("status") or "")
            if current_status not in {"pending", "approved"}:
                return None
            safe_reason = redact_value(str(reason)[:2000])
            if not isinstance(safe_reason, str):
                safe_reason = "executive approval was invalidated"
            result = {
                "ok": False,
                "reason": safe_reason,
                "reconciliation": {
                    "protocol": "jarvis.approval-reconciliation.v1",
                    "status": "completed",
                    "attempts": 1,
                    "mode": "environment_invalidated",
                    "created_at": now,
                    "completed_at": now,
                    "detail": "executive recovery adapted the invalidated branch",
                },
            }
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = 'cancelled', result = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (_json(result), now, approval_id, current_status),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            updated_row = conn.execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE id = ?
                """,
                (approval_id,),
            ).fetchone()
        updated = _approval_record(updated_row) if updated_row is not None else None
        if updated is not None:
            self.record_audit(
                actor="system",
                action="approval.invalidate",
                target_type="approval",
                target_id=approval_id,
                summary=f"Executive approval invalidated: {updated['title']}",
                before=before,
                after=updated,
            )
        return updated

    def claim_approval_execution(self, approval_id: str) -> dict[str, Any] | None:
        """Atomically move one approved action into the executing state.

        The conditional UPDATE is the concurrency boundary.  A competing
        process or request can observe the approval, but only one caller can
        change ``approved`` to ``executing`` and therefore acquire permission
        to perform the side effect.
        """

        now = utc_now()
        with self._lock:
            conn = self.connect()
            before_row = conn.execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE id = ?
                """,
                (approval_id,),
            ).fetchone()
            if before_row is None:
                return None
            before = _approval_record(before_row)
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = 'executing', updated_at = ?
                WHERE id = ? AND status = 'approved'
                """,
                (now, approval_id),
            )
            conn.commit()
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE id = ?
                """,
                (approval_id,),
            ).fetchone()
        claimed = _approval_record(row) if row is not None else None
        if claimed is not None:
            self.record_audit(
                actor="agent",
                action="approval.execute.claim",
                target_type="approval",
                target_id=approval_id,
                summary=f"Approval execution claimed: {claimed['title']}",
                before=before,
                after=claimed,
            )
        return claimed

    def finalize_approval_execution(
        self,
        approval_id: str,
        *,
        status: str,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Finalize a claimed approval without overwriting another state change."""

        if status not in {"executed", "failed"}:
            raise ValueError("Approval execution status must be 'executed' or 'failed'.")
        sanitized_result = redact_value(result)
        if not isinstance(sanitized_result, dict):
            raise TypeError("Approval execution result must be a JSON object.")
        now = utc_now()
        with self._lock:
            conn = self.connect()
            before_row = conn.execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE id = ?
                """,
                (approval_id,),
            ).fetchone()
            if before_row is None:
                return None
            before = _approval_record(before_row)
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, result = ?, updated_at = ?
                WHERE id = ? AND status = 'executing'
                """,
                (status, _json(sanitized_result), now, approval_id),
            )
            conn.commit()
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE id = ?
                """,
                (approval_id,),
            ).fetchone()
        updated = _approval_record(row) if row is not None else None
        if updated is not None:
            self.record_audit(
                actor="agent",
                action="approval.execute.finalize",
                target_type="approval",
                target_id=approval_id,
                summary=f"Approval execution {status}: {updated['title']}",
                before=before,
                after=updated,
            )
        return updated

    def recover_interrupted_approval_executions(self) -> list[dict[str, Any]]:
        """Fail closed approval executions left in-flight by a process exit.

        An ``executing`` approval is an acquired, one-use capability: replaying it
        after a restart could duplicate an already completed side effect.  Cold
        start therefore makes the approval terminal and, for mission-bound
        actions, persists a separate reconciliation obligation.  The obligation
        remains discoverable until the executive branch has been reconciled.
        """

        now = utc_now()
        recovered: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with self._lock:
            conn = self.connect()
            rows = conn.execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE status = 'executing'
                ORDER BY updated_at ASC, rowid ASC
                """
            ).fetchall()
            for raw in rows:
                before = _approval_record(raw)
                payload = before.get("payload")
                mission_bound = bool(
                    isinstance(payload, dict)
                    and str(payload.get("mission_id") or "").strip()
                    and str(payload.get("task_id") or "").strip()
                )
                result = {
                    "ok": False,
                    "summary": (
                        "Approval execution was interrupted by a runtime restart; "
                        "the action will not be replayed automatically."
                    ),
                    "data": {
                        "error": "InterruptedApprovalExecution",
                        "reconcile_only": True,
                    },
                    "recovered_at": now,
                    "reconciliation": {
                        "protocol": "jarvis.approval-reconciliation.v1",
                        "status": "pending" if mission_bound else "not_required",
                        "attempts": 0,
                    },
                }
                cursor = conn.execute(
                    """
                    UPDATE approvals
                    SET status = 'failed', result = ?, updated_at = ?
                    WHERE id = ? AND status = 'executing'
                    """,
                    (_json(result), now, before["id"]),
                )
                if cursor.rowcount != 1:
                    continue
                after = {**before, "status": "failed", "updated_at": now, "result": result}
                recovered.append((before, after))
            conn.commit()

        for before, after in recovered:
            self.record_audit(
                actor="system",
                action="approval.execute.recover",
                target_type="approval",
                target_id=after["id"],
                summary=f"Interrupted approval execution failed closed: {after['title']}",
                before=before,
                after=after,
            )
        return [after for _before, after in recovered]

    def pending_approval_reconciliations(
        self,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return terminal approvals whose mission branch still needs reconciliation."""

        with self._lock:
            rows = self.connect().execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE status IN ('failed', 'rejected', 'cancelled')
                ORDER BY updated_at ASC, rowid ASC
                """,
            ).fetchall()
        approvals = [_approval_record(row) for row in rows]
        pending = [
            approval
            for approval in approvals
            if isinstance(approval.get("result"), dict)
            and isinstance(approval["result"].get("reconciliation"), dict)
            and approval["result"]["reconciliation"].get("protocol")
            == "jarvis.approval-reconciliation.v1"
            and approval["result"]["reconciliation"].get("status") == "pending"
        ]
        return pending[: max(1, min(int(limit), 1000))]

    def complete_approval_reconciliation(
        self,
        approval_id: str,
        *,
        detail: str,
    ) -> dict[str, Any] | None:
        """Durably acknowledge one completed reconcile-only mission update."""

        now = utc_now()
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result
                FROM approvals
                WHERE id = ? AND status IN ('failed', 'rejected', 'cancelled')
                """,
                (approval_id,),
            ).fetchone()
            if row is None:
                return None
            raw_result = str(row["result"])
            before = _approval_record(row)
            result = before.get("result")
            reconciliation = result.get("reconciliation") if isinstance(result, dict) else None
            if (
                not isinstance(reconciliation, dict)
                or reconciliation.get("protocol") != "jarvis.approval-reconciliation.v1"
                or reconciliation.get("status") != "pending"
            ):
                return None
            raw_attempts = reconciliation.get("attempts")
            attempts = (
                raw_attempts
                if isinstance(raw_attempts, int) and raw_attempts >= 0
                else 0
            )
            updated_result = {
                **result,
                "reconciliation": {
                    **reconciliation,
                    "status": "completed",
                    "attempts": attempts + 1,
                    "completed_at": now,
                    "detail": str(detail)[:1000],
                },
            }
            cursor = conn.execute(
                """
                UPDATE approvals
                SET result = ?, updated_at = ?
                WHERE id = ?
                  AND status IN ('failed', 'rejected', 'cancelled')
                  AND result = ?
                """,
                (_json(updated_result), now, approval_id, raw_result),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return None
            conn.commit()
            after = {
                **before,
                "updated_at": now,
                "result": updated_result,
            }
        self.record_audit(
            actor="system",
            action="approval.reconcile.complete",
            target_type="approval",
            target_id=approval_id,
            summary=f"Approval mission reconciliation completed: {after['title']}",
            before=before,
            after=after,
        )
        return after

    def _refresh_mission_progress(
        self,
        conn: sqlite3.Connection,
        mission_id: str,
        *,
        now: str | None = None,
    ) -> None:
        rows = conn.execute(
            "SELECT status FROM mission_tasks WHERE mission_id = ?",
            (mission_id,),
        ).fetchall()
        total = len(rows)
        done = sum(1 for row in rows if row["status"] in {"done", "skipped"})
        running = any(row["status"] == "running" for row in rows)
        blocked = any(row["status"] == "blocked" for row in rows)
        progress = 1.0 if total == 0 else done / total
        if total > 0 and done == total:
            mission_status = "done"
        elif blocked:
            mission_status = "blocked"
        elif running or done > 0:
            mission_status = "running"
        else:
            mission_status = "planned"
        conn.execute(
            """
            UPDATE missions
            SET status = ?, progress = ?, updated_at = ?
            WHERE id = ?
            """,
            (mission_status, progress, now or utc_now(), mission_id),
        )

    def record_health(
        self,
        *,
        component: str,
        status: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO health_snapshots(id, ts, component, status, message, details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (new_id("health"), utc_now(), component, status, message, _json(details or {})),
            )
            self.connect().commit()

    def record_health_snapshot(self, checks: list[dict[str, Any]]) -> dict[str, Any]:
        """Atomically publish one complete diagnostics generation.

        Component-at-a-time commits let readers combine a partially written
        current probe with successful rows from an older probe. The runtime KV
        marker and every referenced row are therefore committed together.
        """

        normalized: list[dict[str, Any]] = []
        seen_components: set[str] = set()
        for raw in checks:
            component = str(raw.get("component") or "").strip()
            status = str(raw.get("status") or "").strip()
            message = str(raw.get("message") or "")
            details = raw.get("details")
            if not component or not status:
                raise ValueError("health snapshot checks require component and status")
            if component in seen_components:
                raise ValueError(f"duplicate health component: {component}")
            if details is not None and not isinstance(details, dict):
                raise TypeError("health snapshot details must be an object")
            seen_components.add(component)
            normalized.append(
                {
                    "id": new_id("health"),
                    "component": component,
                    "status": status,
                    "message": message,
                    "details": details or {},
                }
            )
        if not normalized:
            raise ValueError("health snapshot must contain at least one check")

        snapshot_id = new_id("healthrun")
        timestamp = utc_now()
        marker = {
            "protocol": "jarvis.health-snapshot.v1",
            "snapshot_id": snapshot_id,
            "ts": timestamp,
            "row_ids": [row["id"] for row in normalized],
            "components": [row["component"] for row in normalized],
        }
        with self._lock:
            conn = self.connect()
            if conn.in_transaction:
                raise RuntimeError("health snapshot requires a clean storage transaction")
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    """
                    INSERT INTO health_snapshots(
                        id, ts, component, status, message, details
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            row["id"],
                            timestamp,
                            row["component"],
                            row["status"],
                            row["message"],
                            _json(row["details"]),
                        )
                        for row in normalized
                    ],
                )
                conn.execute(
                    """
                    INSERT INTO runtime_kv(key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    ("health.latest_complete", _json(marker), timestamp),
                )
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
        return marker

    def latest_complete_health(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return only rows from the last atomically committed diagnostics run."""

        bounded_limit = max(1, min(100, int(limit)))
        with self._lock:
            conn = self.connect()
            marker_row = conn.execute(
                "SELECT value FROM runtime_kv WHERE key = ?",
                ("health.latest_complete",),
            ).fetchone()
            marker = _loads(marker_row["value"], {}) if marker_row is not None else {}
            row_ids = marker.get("row_ids") if isinstance(marker, dict) else None
            components = marker.get("components") if isinstance(marker, dict) else None
            if (
                marker.get("protocol") != "jarvis.health-snapshot.v1"
                or not isinstance(row_ids, list)
                or not row_ids
                or len(row_ids) > 100
                or len(set(row_ids)) != len(row_ids)
                or not all(isinstance(item, str) and item for item in row_ids)
                or not isinstance(components, list)
                or len(components) != len(row_ids)
            ):
                return []
            selected_ids = row_ids[:bounded_limit]
            placeholders = ",".join("?" for _item in selected_ids)
            rows = conn.execute(
                f"""
                SELECT id, ts, component, status, message, details
                FROM health_snapshots
                WHERE id IN ({placeholders})
                """,  # noqa: S608 - placeholders are generated, values stay bound
                selected_ids,
            ).fetchall()
        by_id = {str(row["id"]): row for row in rows}
        if len(by_id) != len(selected_ids):
            return []
        return [
            {**dict(by_id[row_id]), "details": _loads(by_id[row_id]["details"], {})}
            for row_id in selected_ids
        ]

    def latest_health(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT h.id, h.ts, h.component, h.status, h.message, h.details
                FROM health_snapshots h
                WHERE h.rowid = (
                    SELECT latest.rowid
                    FROM health_snapshots latest
                    WHERE latest.component = h.component
                    ORDER BY latest.ts DESC, latest.rowid DESC
                    LIMIT 1
                )
                ORDER BY h.ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{**dict(row), "details": _loads(row["details"], {})} for row in rows]

    def record_telemetry(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        row = {
            "id": new_id("tel"),
            "ts": str(snapshot.get("ts") or utc_now()),
            "snapshot": snapshot,
        }
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO telemetry_snapshots(id, ts, snapshot)
                VALUES (?, ?, ?)
                """,
                (row["id"], row["ts"], _json(row["snapshot"])),
            )
            self.connect().commit()
        return row

    def list_telemetry(self, limit: int = 24) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT id, ts, snapshot
                FROM telemetry_snapshots
                ORDER BY ts DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {"id": row["id"], "ts": row["ts"], "snapshot": _loads(row["snapshot"], {})}
            for row in rows
        ]


SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_events (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    importance REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_tasks (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    text TEXT NOT NULL,
    due_at TEXT NOT NULL,
    recurrence TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    conversation_id TEXT,
    source_text TEXT NOT NULL DEFAULT '',
    fired_at TEXT,
    fire_count INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_path TEXT,
    stored_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_chunks (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_snapshots (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    component TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS tool_runs (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    tool TEXT NOT NULL,
    ok INTEGER NOT NULL,
    summary TEXT NOT NULL,
    arguments TEXT NOT NULL DEFAULT '{}',
    data TEXT NOT NULL DEFAULT '{}',
    mission_id TEXT REFERENCES missions(id) ON DELETE SET NULL,
    task_id TEXT REFERENCES mission_tasks(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS learning_observations (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    source_id TEXT,
    conversation_id TEXT,
    role TEXT,
    content TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    risk TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    requested_action TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS telemetry_snapshots (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    snapshot TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    summary TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runtime_kv_updated ON runtime_kv(updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace, importance);
CREATE INDEX IF NOT EXISTS idx_mission_tasks_mission ON mission_tasks(mission_id, position);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at);
CREATE INDEX IF NOT EXISTS idx_reminders_conversation ON reminders(conversation_id, due_at);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_updated ON files(updated_at);
CREATE INDEX IF NOT EXISTS idx_file_chunks_file ON file_chunks(file_id, position);
CREATE INDEX IF NOT EXISTS idx_health_component ON health_snapshots(component, ts);
CREATE INDEX IF NOT EXISTS idx_tool_runs_ts ON tool_runs(ts);
CREATE INDEX IF NOT EXISTS idx_tool_runs_mission ON tool_runs(mission_id, task_id);
CREATE INDEX IF NOT EXISTS idx_learning_observations_ts ON learning_observations(ts);
CREATE INDEX IF NOT EXISTS idx_learning_observations_kind ON learning_observations(kind, ts);
CREATE INDEX IF NOT EXISTS idx_learning_observations_conversation
ON learning_observations(conversation_id, ts);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_audit_log_target ON audit_log(target_type, target_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);
"""
