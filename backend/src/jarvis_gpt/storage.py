from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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


class JarvisStorage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

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

    def initialize(self) -> None:
        with self._lock:
            conn = self.connect()
            conn.executescript(SCHEMA)
            conn.commit()

    def ping(self) -> bool:
        with self._lock:
            self.connect().execute("SELECT 1").fetchone()
        return True

    def counters(self) -> dict[str, int]:
        tables = ["conversations", "messages", "memories", "missions", "mission_tasks"]
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
            conn.commit()
        return mid

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
        row = {
            "id": new_id("mem"),
            "namespace": namespace,
            "content": content,
            "tags": list(tags),
            "importance": float(importance),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self.connect().execute(
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
            self.connect().commit()
        return row

    def search_memory(self, query: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        params: tuple[Any, ...]
        where = ""
        if query:
            where = "WHERE content LIKE ? OR tags LIKE ? OR namespace LIKE ?"
            like = f"%{query}%"
            params = (like, like, like, limit)
        else:
            params = (limit,)
        sql = f"""
            SELECT id, namespace, content, tags, importance, created_at, updated_at
            FROM memories
            {where}
            ORDER BY importance DESC, updated_at DESC
            LIMIT ?
        """
        with self._lock:
            rows = self.connect().execute(sql, params).fetchall()
        return [{**dict(row), "tags": _loads(row["tags"], [])} for row in rows]

    def create_mission(self, *, title: str, goal: str, tasks: list[str]) -> dict[str, Any]:
        now = utc_now()
        mission_id = new_id("mis")
        with self._lock:
            conn = self.connect()
            conn.execute(
                """
                INSERT INTO missions(id, title, goal, status, progress, created_at, updated_at)
                VALUES (?, ?, ?, 'planned', 0, ?, ?)
                """,
                (mission_id, title[:240], goal, now, now),
            )
            for position, task_title in enumerate(tasks, start=1):
                conn.execute(
                    """
                    INSERT INTO mission_tasks(
                        id, mission_id, title, status, notes, position, created_at, updated_at
                    )
                    VALUES (?, ?, ?, 'pending', NULL, ?, ?, ?)
                    """,
                    (new_id("task"), mission_id, task_title, position, now, now),
                )
            conn.commit()
        mission = self.get_mission(mission_id)
        if mission is None:
            raise RuntimeError("Mission was not persisted")
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

    def latest_health(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT h.id, h.ts, h.component, h.status, h.message, h.details
                FROM health_snapshots h
                JOIN (
                    SELECT component, MAX(ts) AS ts
                    FROM health_snapshots
                    GROUP BY component
                ) latest ON latest.component = h.component AND latest.ts = h.ts
                ORDER BY h.ts DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{**dict(row), "details": _loads(row["details"], {})} for row in rows]


SCHEMA = """
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

CREATE TABLE IF NOT EXISTS health_snapshots (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    component TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace, importance);
CREATE INDEX IF NOT EXISTS idx_mission_tasks_mission ON mission_tasks(mission_id, position);
CREATE INDEX IF NOT EXISTS idx_health_component ON health_snapshots(component, ts);
"""
