from __future__ import annotations

import json
import re
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


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[\w-]+", query, flags=re.UNICODE)
    clean = [token.replace('"', "").replace("'", "") for token in tokens[:8]]
    clean = [token for token in clean if token]
    return " OR ".join(f'"{token}"' for token in clean)


class JarvisStorage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._memory_fts_available = False
        self._file_fts_available = False

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
            self._memory_fts_available = self._ensure_memory_fts(conn)
            self._file_fts_available = self._ensure_file_chunks_fts(conn)
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
        except sqlite3.OperationalError:
            return False

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
        except sqlite3.OperationalError:
            return False

    def ping(self) -> bool:
        with self._lock:
            self.connect().execute("SELECT 1").fetchone()
        return True

    def counters(self) -> dict[str, int]:
        tables = [
            "conversations",
            "messages",
            "memories",
            "missions",
            "mission_tasks",
            "files",
            "file_chunks",
            "tool_runs",
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
            conn = self.connect()
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
            if self._memory_fts_available:
                conn.execute(
                    """
                    INSERT INTO memories_fts(id, namespace, content, tags)
                    VALUES (?, ?, ?, ?)
                    """,
                    (row["id"], row["namespace"], row["content"], _json(row["tags"])),
                )
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

    def search_memory(self, query: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        if query and self._memory_fts_available:
            match = _fts_query(query)
            if match:
                try:
                    with self._lock:
                        rows = self.connect().execute(
                            """
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
                            ORDER BY rank ASC, m.importance DESC, m.updated_at DESC
                            LIMIT ?
                            """,
                            (match, limit),
                        ).fetchall()
                    return [{**dict(row), "tags": _loads(row["tags"], [])} for row in rows]
                except sqlite3.OperationalError:
                    pass

        params: tuple[Any, ...]
        where = ""
        if query:
            where = "WHERE content LIKE ? OR tags LIKE ? OR namespace LIKE ?"
            like = f"%{query}%"
            params = (like, like, like, limit)
        else:
            params = (limit,)
        sql = f"""
            SELECT id, namespace, content, tags, importance, created_at, updated_at, NULL AS rank
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
            self._refresh_mission_progress(conn, mission_id, now=now)
            conn.commit()
        mission = self.get_mission(mission_id)
        if mission is None:
            raise RuntimeError("Mission was not persisted")
        self.record_audit(
            actor="system",
            action="mission.create",
            target_type="mission",
            target_id=mission_id,
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

    def update_mission_task(
        self,
        task_id: str,
        *,
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
                WHERE id = ?
                """,
                (task_id,),
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
                WHERE id = ?
                """,
                (next_title, next_status, next_notes, now, task_id),
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
            "summary": summary,
            "arguments": arguments or {},
            "data": data or {},
            "mission_id": mission_id,
            "task_id": task_id,
        }
        with self._lock:
            self.connect().execute(
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
            self.connect().commit()
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

    def add_file_chunks(self, file_id: str, chunks: list[str]) -> None:
        now = utc_now()
        with self._lock:
            conn = self.connect()
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
            conn.execute(
                """
                UPDATE files
                SET chunk_count = ?, updated_at = ?
                WHERE id = ?
                """,
                (len(chunks), now, file_id),
            )
            conn.commit()

    def list_files(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT
                    id, name, source_path, stored_path, mime_type, size, sha256,
                    status, error, chunk_count, created_at, updated_at
                FROM files
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

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
                    return [dict(row) for row in rows]
                except sqlite3.OperationalError:
                    pass

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
        return [dict(row) for row in rows]

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

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace, importance);
CREATE INDEX IF NOT EXISTS idx_mission_tasks_mission ON mission_tasks(mission_id, position);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_updated ON files(updated_at);
CREATE INDEX IF NOT EXISTS idx_file_chunks_file ON file_chunks(file_id, position);
CREATE INDEX IF NOT EXISTS idx_health_component ON health_snapshots(component, ts);
CREATE INDEX IF NOT EXISTS idx_tool_runs_ts ON tool_runs(ts);
CREATE INDEX IF NOT EXISTS idx_tool_runs_mission ON tool_runs(mission_id, task_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_target ON audit_log(target_type, target_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts ON audit_log(ts);
"""
