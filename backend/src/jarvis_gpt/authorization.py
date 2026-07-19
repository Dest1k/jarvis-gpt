from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import sqlite3

    from .storage import JarvisStorage


LEGACY_OWNER_USER_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "jarvis://legacy-owner"))
DEFAULT_PRESET_KEY = "guest"
BUILTIN_PRESET_KEYS = ("owner", "admin", "moderator", "user", "guest")

# Only tools whose data plane is already tenant-scoped (or bounded to public web
# retrieval) are granted to ordinary built-in roles.  A danger label is a HITL hint,
# not an isolation guarantee: host filesystem/runtime tools remain default-deny even
# when their ToolSpec calls them "safe".
TENANT_SAFE_TOOL_SECURITY_IDS = frozenset(
    {
        "tool.persona.get",
        "tool.persona.insight",
        "tool.memory.search",
        "tool.memory.save",
        "tool.reminders.create",
        "tool.reminders.list",
        "tool.reminders.cancel",
        "tool.files.list",
        "tool.files.search",
        "tool.documents.recall",
        "tool.web.search",
        "tool.web.shop_search",
        "tool.web.weather",
        "tool.web.research",
        "tool.web.answer",
        "tool.web.verify",
    }
)
OWNER_RECOVERY_SECURITY_IDS = frozenset(
    {
        "admin.users.list",
        "admin.users.permissions.list",
        "admin.users.status.update",
        "admin.users.preset.assign",
        "admin.users.permission.set",
        "admin.users.permission.revoke",
        "admin.security_ids.list",
        "admin.presets.list",
        "admin.audit.list",
    }
)

_SECURITY_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


class AuthorizationError(PermissionError):
    """The authenticated principal is not allowed to perform an operation."""


class ResourceIsolationError(AuthorizationError):
    """A resource exists, but belongs to another user."""


class ConcurrentPolicyUpdateError(AuthorizationError):
    """The IAM target changed after the caller reviewed/authorized its prior version."""


@dataclass(frozen=True)
class ActorContext:
    user_id: str
    preset_key: str
    source: str
    identity_id: str | None = None
    session_id: str | None = None
    policy_epoch: int = 1

    @property
    def is_owner(self) -> bool:
        return self.preset_key == "owner"


_ACTOR: ContextVar[ActorContext | None] = ContextVar("jarvis_actor", default=None)


def legacy_owner_context(*, source: str = "legacy-local") -> ActorContext:
    return ActorContext(
        user_id=LEGACY_OWNER_USER_ID,
        preset_key="owner",
        source=source,
    )


def current_actor() -> ActorContext:
    """Return the request actor, retaining a bounded legacy-owner compatibility path.

    Existing local/background code did not carry an actor. During the additive migration it
    remains attached to the deterministic legacy owner. External multi-user ingress always
    binds an explicit context before it can reach application or tool code.
    """

    return _ACTOR.get() or legacy_owner_context()


def current_user_id() -> str:
    return current_actor().user_id


@contextmanager
def bind_actor(actor: ActorContext) -> Iterator[ActorContext]:
    token = _ACTOR.set(actor)
    try:
        yield actor
    finally:
        _ACTOR.reset(token)


def scoped_runtime_key(key: str, *, actor: ActorContext | None = None) -> str:
    """Namespace runtime KV while preserving only the original user's legacy data.

    The unscoped namespace predates IAM and belongs to the deterministic legacy user,
    not to everyone who is later assigned the ``owner`` role.
    """

    principal = actor or current_actor()
    clean = str(key).strip()[:500]
    if principal.user_id == LEGACY_OWNER_USER_ID:
        return clean
    return f"user.{principal.user_id}.{clean}"


@dataclass(frozen=True)
class CapabilityDefinition:
    security_id: str
    description: str
    category: str
    risk_level: int = 0
    default_requires_hitl: bool = False
    source: str = "core"
    # Built-in grants are part of the capability catalog rather than inferred from
    # HTTP path spelling.  Owner is always granted by the bootstrap policy and must
    # therefore not be repeated here.
    default_presets: tuple[str, ...] = ()

    def validate(self) -> None:
        if not _SECURITY_ID_RE.fullmatch(self.security_id):
            raise ValueError(f"Invalid security_id: {self.security_id!r}")
        if not 0 <= int(self.risk_level) <= 4:
            raise ValueError("risk_level must be between 0 and 4")
        invalid_presets = set(self.default_presets) - set(BUILTIN_PRESET_KEYS)
        if invalid_presets or "owner" in self.default_presets:
            raise ValueError(
                "default_presets may only contain non-owner built-in preset keys"
            )


@dataclass(frozen=True)
class AuthorizationDecision:
    decision_id: str
    effect: Literal["allow", "deny"]
    security_id: str
    user_id: str
    reason_code: str
    policy_epoch: int
    preset_key: str | None = None
    preset_version: int | None = None
    source: str | None = None

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "effect": self.effect,
            "security_id": self.security_id,
            "user_id": self.user_id,
            "reason_code": self.reason_code,
            "policy_epoch": self.policy_epoch,
            "preset_key": self.preset_key,
            "preset_version": self.preset_version,
            "source": self.source,
        }


CORE_CAPABILITIES: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition("chat.use", "Use conversational Jarvis", "chat"),
    CapabilityDefinition("events.subscribe", "Subscribe to own live events", "events"),
    CapabilityDefinition("memory.read.own", "Read own memory", "memory"),
    CapabilityDefinition("memory.write.own", "Write own memory", "memory", 1),
    CapabilityDefinition("preferences.read.own", "Read own preferences", "preferences"),
    CapabilityDefinition("persona.read.own", "Read own persona and personalization", "persona"),
    CapabilityDefinition("files.read.own", "Read own files", "files"),
    CapabilityDefinition("missions.read.own", "Read own missions", "missions"),
    CapabilityDefinition("missions.write.own", "Create and update own missions", "missions", 1),
    CapabilityDefinition(
        "background.screen_watch.create",
        "Create bounded recurring screen observations",
        "background",
        3,
        True,
    ),
    CapabilityDefinition(
        "background.scheduled_task.create",
        "Create a scheduled autonomous agent task",
        "background",
        4,
        True,
    ),
    CapabilityDefinition(
        "background.scheduled_task.execute",
        "Execute a previously scheduled autonomous agent task",
        "background",
        4,
        True,
    ),
    CapabilityDefinition(
        "privacy.screen.capture",
        "Capture the local operator screen",
        "privacy",
        3,
    ),
    CapabilityDefinition(
        "privacy.clipboard.read",
        "Read the local operator clipboard",
        "privacy",
        3,
    ),
    CapabilityDefinition(
        "privacy.clipboard.write",
        "Write the local operator clipboard",
        "privacy",
        3,
        True,
    ),
    CapabilityDefinition(
        "native.capabilities.read", "Inspect native bridge capabilities", "native", 2
    ),
    CapabilityDefinition(
        "native.process.top.read", "Read the local process ranking", "native", 3
    ),
    CapabilityDefinition(
        "native.console.processes.show",
        "Show a process ranking in a local console",
        "native",
        3,
        True,
    ),
    CapabilityDefinition(
        "native.process.start", "Start a local process", "native", 4, True
    ),
    CapabilityDefinition(
        "native.app.open_and_type", "Open a local app and type text", "native", 4, True
    ),
    CapabilityDefinition(
        "native.window.focus", "Focus a local desktop window", "native", 3, True
    ),
    CapabilityDefinition(
        "native.window.list.read", "List visible local desktop windows", "native", 3
    ),
    CapabilityDefinition(
        "native.keyboard.send", "Send keys to the local desktop", "native", 4, True
    ),
    CapabilityDefinition(
        "native.wmi.query", "Query local WMI/CIM state", "native", 3
    ),
    CapabilityDefinition(
        "native.hardware.gpu.read", "Read local GPU telemetry", "native", 3
    ),
    CapabilityDefinition("approvals.read.own", "Read own approvals", "safety"),
    CapabilityDefinition(
        "background.autonomy.execute",
        "Execute the user's persisted autonomy jobs in the background",
        "background",
        4,
    ),
    CapabilityDefinition(
        "integration.telegram.session.create",
        "Register Telegram identities and create scoped sessions",
        "integration",
        2,
    ),
)


IAM_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK(status IN ('pending', 'active', 'suspended', 'deleted')),
    display_name TEXT NOT NULL DEFAULT '',
    locale TEXT NOT NULL DEFAULT '',
    policy_epoch INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    deleted_at TEXT,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS external_identities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    provider TEXT NOT NULL,
    realm_id TEXT NOT NULL,
    provider_subject_id TEXT NOT NULL,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    profile_snapshot TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(provider, realm_id, provider_subject_id)
);

CREATE TABLE IF NOT EXISTS security_ids (
    id TEXT PRIMARY KEY,
    security_id TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    category TEXT NOT NULL,
    risk_level INTEGER NOT NULL DEFAULT 0 CHECK(risk_level BETWEEN 0 AND 4),
    default_requires_hitl INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'retired', 'disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE TABLE IF NOT EXISTS permission_presets (
    id TEXT PRIMARY KEY,
    preset_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('builtin', 'custom')),
    active_version_id TEXT,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS permission_preset_versions (
    id TEXT PRIMARY KEY,
    preset_id TEXT NOT NULL REFERENCES permission_presets(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('draft', 'published', 'retired')),
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    change_reason TEXT NOT NULL,
    UNIQUE(preset_id, version)
);

CREATE TABLE IF NOT EXISTS preset_security_ids (
    preset_version_id TEXT NOT NULL
        REFERENCES permission_preset_versions(id) ON DELETE CASCADE,
    security_id_id TEXT NOT NULL REFERENCES security_ids(id) ON DELETE RESTRICT,
    effect TEXT NOT NULL DEFAULT 'grant' CHECK(effect IN ('grant', 'deny')),
    can_delegate INTEGER NOT NULL DEFAULT 0,
    constraints_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(preset_version_id, security_id_id)
);

CREATE TABLE IF NOT EXISTS user_preset_assignments (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    preset_id TEXT NOT NULL REFERENCES permission_presets(id) ON DELETE RESTRICT,
    assigned_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    assigned_at TEXT NOT NULL,
    revoked_at TEXT,
    reason TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_one_active_preset
ON user_preset_assignments(user_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS user_permissions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    security_id_id TEXT NOT NULL REFERENCES security_ids(id) ON DELETE RESTRICT,
    effect TEXT NOT NULL CHECK(effect IN ('grant', 'deny')),
    can_delegate INTEGER NOT NULL DEFAULT 0,
    constraints_json TEXT NOT NULL DEFAULT '{}',
    granted_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    valid_until TEXT,
    revoked_at TEXT,
    reason TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_one_active_permission
ON user_permissions(user_id, security_id_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS user_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    identity_id TEXT REFERENCES external_identities(id) ON DELETE SET NULL,
    token_sha256 TEXT NOT NULL UNIQUE,
    auth_method TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS authorization_decisions (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    identity_id TEXT REFERENCES external_identities(id) ON DELETE SET NULL,
    security_id TEXT NOT NULL,
    effect TEXT NOT NULL CHECK(effect IN ('allow', 'deny')),
    reason_code TEXT NOT NULL,
    policy_epoch INTEGER NOT NULL,
    preset_key TEXT,
    preset_version INTEGER,
    source TEXT,
    request_id TEXT,
    resource_type TEXT,
    resource_ref_hash TEXT,
    context_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS security_audit_log (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    target_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    reason TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS telegram_updates (
    realm_id TEXT NOT NULL,
    update_id INTEGER NOT NULL,
    user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    payload_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'failed')),
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK(attempt_count BETWEEN 1 AND 3),
    lease_token TEXT,
    last_error TEXT,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(realm_id, update_id)
);

CREATE TABLE IF NOT EXISTS ingress_rate_limits (
    scope TEXT NOT NULL,
    subject_hash TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    request_count INTEGER NOT NULL CHECK(request_count >= 1),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(scope, subject_hash, window_start)
);

CREATE TABLE IF NOT EXISTS iam_migrations (
    key TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_external_identities_user ON external_identities(user_id);
CREATE INDEX IF NOT EXISTS idx_security_ids_category ON security_ids(category, security_id);
CREATE INDEX IF NOT EXISTS idx_authorization_decisions_actor
ON authorization_decisions(actor_user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_authorization_decisions_security
ON authorization_decisions(security_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_security_audit_ts ON security_audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_security_audit_target
ON security_audit_log(target_user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id, expires_at DESC);
CREATE INDEX IF NOT EXISTS idx_external_identities_provider_user
ON external_identities(provider, user_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_telegram_updates_updated
ON telegram_updates(updated_at);
CREATE INDEX IF NOT EXISTS idx_ingress_rate_limits_updated
ON ingress_rate_limits(updated_at);
"""


PERSONAL_TABLES: tuple[str, ...] = (
    "runtime_events",
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
    "audit_log",
)


TENANT_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_runtime_events_user_ts
ON runtime_events(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
ON conversations(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_user_conversation_created
ON messages(user_id, conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_user_namespace_importance
ON memories(user_id, namespace, importance DESC, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_missions_user_updated
ON missions(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_mission_tasks_user_mission_position
ON mission_tasks(user_id, mission_id, position);
CREATE INDEX IF NOT EXISTS idx_reminders_user_status_due
ON reminders(user_id, status, due_at);
CREATE INDEX IF NOT EXISTS idx_reminders_user_conversation_due
ON reminders(user_id, conversation_id, due_at);
CREATE INDEX IF NOT EXISTS idx_files_user_updated
ON files(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_files_user_sha256
ON files(user_id, sha256);
CREATE INDEX IF NOT EXISTS idx_file_chunks_user_file_position
ON file_chunks(user_id, file_id, position);
CREATE INDEX IF NOT EXISTS idx_tool_runs_user_ts
ON tool_runs(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_tool_runs_user_mission
ON tool_runs(user_id, mission_id, task_id);
CREATE INDEX IF NOT EXISTS idx_learning_observations_user_ts
ON learning_observations(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_learning_observations_user_kind_ts
ON learning_observations(user_id, kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_approvals_user_status_updated
ON approvals(user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_user_ts
ON audit_log(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_user_target
ON audit_log(user_id, target_type, target_id, ts DESC);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def migrate_iam_schema(conn: sqlite3.Connection) -> None:
    """Install additive IAM tables and bind every legacy personal row to the owner."""

    conn.executescript(IAM_SCHEMA)
    now = _now()
    conn.execute(
        """
        INSERT INTO users(
            id, status, display_name, locale, policy_epoch, created_at, updated_at,
            first_seen_at, last_seen_at
        ) VALUES (?, 'active', 'Owner', '', 1, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        (LEGACY_OWNER_USER_ID, now, now, now, now),
    )
    for preset_key in BUILTIN_PRESET_KEYS:
        preset_id = f"preset_{preset_key}"
        version_id = f"presetv_{preset_key}_1"
        conn.execute(
            """
            INSERT INTO permission_presets(
                id, preset_key, display_name, kind, active_version_id,
                created_by, created_at, updated_at
            ) VALUES (?, ?, ?, 'builtin', NULL, ?, ?, ?)
            ON CONFLICT(preset_key) DO NOTHING
            """,
            (
                preset_id,
                preset_key,
                preset_key.capitalize(),
                LEGACY_OWNER_USER_ID,
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO permission_preset_versions(
                id, preset_id, version, state, created_by, created_at,
                published_at, change_reason
            ) VALUES (?, ?, 1, 'published', ?, ?, ?, 'Initial built-in preset')
            ON CONFLICT(preset_id, version) DO NOTHING
            """,
            (version_id, preset_id, LEGACY_OWNER_USER_ID, now, now),
        )
        conn.execute(
            """
            UPDATE permission_presets
            SET active_version_id = COALESCE(active_version_id, ?)
            WHERE id = ?
            """,
            (version_id, preset_id),
        )
    conn.execute(
        """
        INSERT INTO user_preset_assignments(
            id, user_id, preset_id, assigned_by, assigned_at, reason
        )
        SELECT ?, ?, 'preset_owner', ?, ?, 'Legacy single-user owner migration'
        WHERE NOT EXISTS (
            SELECT 1 FROM user_preset_assignments
            WHERE user_id = ? AND revoked_at IS NULL
        )
        """,
        (
            "assignment_legacy_owner",
            LEGACY_OWNER_USER_ID,
            LEGACY_OWNER_USER_ID,
            now,
            LEGACY_OWNER_USER_ID,
        ),
    )

    for table in PERSONAL_TABLES:
        if "user_id" not in _table_columns(conn, table):
            # SQLite rejects ADD COLUMN ... REFERENCES with a non-NULL default on
            # populated legacy tables.  Add it nullable, backfill in the same startup
            # transaction, and enforce ownership on every application write.  Fresh
            # databases receive the full NOT NULL/FK declaration from SCHEMA.
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN user_id TEXT')
        conn.execute(
            f'UPDATE "{table}" SET user_id = ? WHERE user_id IS NULL OR user_id = \'\'',
            (LEGACY_OWNER_USER_ID,),
        )
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS "idx_{table}_user" ON "{table}"(user_id)'
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS "trg_{table}_user_insert"
            BEFORE INSERT ON "{table}"
            WHEN NEW.user_id IS NULL OR NEW.user_id = ''
              OR NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id)
            BEGIN
                SELECT RAISE(ABORT, 'valid user_id is required');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS "trg_{table}_user_update"
            BEFORE UPDATE OF user_id ON "{table}"
            WHEN NEW.user_id IS NULL OR NEW.user_id = ''
              OR NOT EXISTS (SELECT 1 FROM users WHERE id = NEW.user_id)
            BEGIN
                SELECT RAISE(ABORT, 'valid user_id is required');
            END
            """
        )

    # These indexes must be created only after every legacy table has received its
    # additive user_id column. Keeping them in the base schema makes an existing
    # single-user database fail before the migration can run.
    for statement in TENANT_INDEXES.split(";"):
        if statement.strip():
            conn.execute(statement)

    conn.execute(
        """
        INSERT INTO iam_migrations(key, applied_at, details_json)
        VALUES ('multi_user_v1', ?, ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (now, _json({"legacy_owner_user_id": LEGACY_OWNER_USER_ID})),
    )
    telegram_columns = _table_columns(conn, "telegram_updates")
    if "attempt_count" not in telegram_columns:
        conn.execute(
            "ALTER TABLE telegram_updates ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 1"
        )
    if "lease_token" not in telegram_columns:
        conn.execute("ALTER TABLE telegram_updates ADD COLUMN lease_token TEXT")
    if "last_error" not in telegram_columns:
        conn.execute("ALTER TABLE telegram_updates ADD COLUMN last_error TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_telegram_updates_updated "
        "ON telegram_updates(updated_at)"
    )


class AuthorizationService:
    def __init__(self, storage: JarvisStorage) -> None:
        self.storage = storage
        self._retention_lock = threading.Lock()
        self._last_retention_run: datetime | None = None

    def sync_capabilities(
        self,
        definitions: Iterable[CapabilityDefinition],
        *,
        catalog_key: str,
        bootstrap_safe_presets: bool = False,
    ) -> dict[str, int]:
        entries = list(definitions)
        for entry in entries:
            entry.validate()
        inserted = 0
        reconciled_presets: set[str] = set()
        now = _now()
        with self.storage.transaction(immediate=True) as conn:
            marker = conn.execute(
                "SELECT 1 FROM iam_migrations WHERE key = ?",
                (f"catalog.{catalog_key}",),
            ).fetchone()
            first_catalog_sync = marker is None
            for entry in entries:
                row = conn.execute(
                    "SELECT id FROM security_ids WHERE security_id = ?",
                    (entry.security_id,),
                ).fetchone()
                if row is None:
                    capability_id = _new_id("sec")
                    conn.execute(
                        """
                        INSERT INTO security_ids(
                            id, security_id, description, category, risk_level,
                            default_requires_hitl, source, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                        """,
                        (
                            capability_id,
                            entry.security_id,
                            entry.description,
                            entry.category,
                            int(entry.risk_level),
                            int(entry.default_requires_hitl),
                            entry.source,
                            now,
                            now,
                        ),
                    )
                    inserted += 1
                else:
                    capability_id = str(row["id"])
                    conn.execute(
                        """
                        UPDATE security_ids
                        SET description = ?, category = ?, risk_level = ?,
                            default_requires_hitl = ?, source = ?, status = 'active',
                            updated_at = ?, retired_at = NULL
                        WHERE id = ?
                        """,
                        (
                            entry.description,
                            entry.category,
                            int(entry.risk_level),
                            int(entry.default_requires_hitl),
                            entry.source,
                            now,
                            capability_id,
                        ),
                    )
                reconciled_presets.update(
                    self._reconcile_builtin_capability_grants(
                        conn,
                        capability_id=capability_id,
                        entry=entry,
                        bootstrap_safe_presets=bootstrap_safe_presets,
                    )
                )
            sources = sorted({entry.source for entry in entries})
            for source in sources:
                source_ids = sorted(
                    entry.security_id for entry in entries if entry.source == source
                )
                placeholders = ",".join("?" for _ in source_ids)
                conn.execute(
                    f"""
                    UPDATE security_ids
                    SET status = 'retired', retired_at = ?, updated_at = ?
                    WHERE source = ? AND status = 'active'
                      AND security_id NOT IN ({placeholders})
                    """,
                    (now, now, source, *source_ids),
                )
            if reconciled_presets:
                placeholders = ",".join("?" for _ in reconciled_presets)
                affected = conn.execute(
                    f"""
                    SELECT DISTINCT upa.user_id
                    FROM user_preset_assignments upa
                    JOIN permission_presets p ON p.id = upa.preset_id
                    WHERE upa.revoked_at IS NULL AND p.preset_key IN ({placeholders})
                    """,
                    tuple(sorted(reconciled_presets)),
                ).fetchall()
                affected_ids = [str(row["user_id"]) for row in affected]
                if affected_ids:
                    affected_placeholders = ",".join("?" for _ in affected_ids)
                    conn.execute(
                        f"""
                        UPDATE users
                        SET policy_epoch = policy_epoch + 1,
                            row_version = row_version + 1,
                            updated_at = ?
                        WHERE id IN ({affected_placeholders})
                        """,
                        (now, *affected_ids),
                    )
                    conn.execute(
                        f"""
                        UPDATE user_sessions SET revoked_at = ?
                        WHERE revoked_at IS NULL AND user_id IN ({affected_placeholders})
                        """,
                        (now, *affected_ids),
                    )
                self.append_security_audit(
                    conn,
                    action="catalog.builtin_policy.reconcile",
                    target_type="capability_catalog",
                    target_id=catalog_key,
                    target_user_id=None,
                    reason="Built-in grants reconciled to the declared capability policy",
                    before={},
                    after={
                        "preset_keys": sorted(reconciled_presets),
                        "affected_user_ids": affected_ids,
                    },
                )
            if first_catalog_sync:
                conn.execute(
                    "INSERT INTO iam_migrations(key, applied_at, details_json) VALUES (?, ?, ?)",
                    (
                        f"catalog.{catalog_key}",
                        now,
                        _json({"capability_count": len(entries)}),
                    ),
                )
        return {
            "seen": len(entries),
            "inserted": inserted,
            "reconciled_presets": len(reconciled_presets),
        }

    @staticmethod
    def _reconcile_builtin_capability_grants(
        conn: sqlite3.Connection,
        *,
        capability_id: str,
        entry: CapabilityDefinition,
        bootstrap_safe_presets: bool,
    ) -> set[str]:
        presets = {"owner"}
        presets.update(entry.default_presets)
        if entry.security_id in {
            "chat.use",
            "events.subscribe",
            "preferences.read.own",
        }:
            presets.update({"guest", "user", "moderator", "admin"})
        if entry.security_id.startswith(
            ("memory.", "persona.", "files.", "missions.", "audit.read.own")
        ):
            presets.update({"user", "moderator", "admin"})
        if entry.security_id == "approvals.read.own":
            presets.update({"user", "moderator", "admin"})
        if entry.security_id.startswith("admin.") and entry.security_id != "admin.owner.transfer":
            presets.add("admin")
        if (
            bootstrap_safe_presets
            and entry.risk_level == 0
            and entry.security_id in TENANT_SAFE_TOOL_SECURITY_IDS
        ):
            presets.update({"user", "moderator", "admin"})
        desired = {
            preset_key: int(
                preset_key == "owner" or (preset_key == "admin" and entry.risk_level < 4)
            )
            for preset_key in presets
        }
        rows = conn.execute(
            """
            SELECT preset_version_id, effect, can_delegate
            FROM preset_security_ids
            WHERE security_id_id = ?
              AND preset_version_id IN (
                  'presetv_owner_1', 'presetv_admin_1', 'presetv_moderator_1',
                  'presetv_user_1', 'presetv_guest_1'
              )
            """,
            (capability_id,),
        ).fetchall()
        existing = {
            str(row["preset_version_id"]): (str(row["effect"]), int(row["can_delegate"]))
            for row in rows
        }
        changed: set[str] = set()
        for preset_key in BUILTIN_PRESET_KEYS:
            version_id = f"presetv_{preset_key}_1"
            if preset_key not in desired:
                if version_id in existing:
                    conn.execute(
                        "DELETE FROM preset_security_ids "
                        "WHERE preset_version_id = ? AND security_id_id = ?",
                        (version_id, capability_id),
                    )
                    changed.add(preset_key)
                continue
            can_delegate = desired[preset_key]
            if existing.get(version_id) == ("grant", can_delegate):
                continue
            can_delegate = int(
                preset_key == "owner" or (preset_key == "admin" and entry.risk_level < 4)
            )
            conn.execute(
                """
                INSERT INTO preset_security_ids(
                    preset_version_id, security_id_id, effect, can_delegate
                ) VALUES (?, ?, 'grant', ?)
                ON CONFLICT(preset_version_id, security_id_id) DO UPDATE SET
                    effect = excluded.effect,
                    can_delegate = excluded.can_delegate,
                    constraints_json = '{}'
                """,
                (version_id, capability_id, can_delegate),
            )
            changed.add(preset_key)
        return changed

    def authorize(
        self,
        user_id: str,
        security_id: str,
        *,
        identity_id: str | None = None,
        request_id: str | None = None,
        resource_type: str | None = None,
        resource_ref: str | None = None,
        context: dict[str, Any] | None = None,
        record: bool = True,
    ) -> AuthorizationDecision:
        decision_id = _new_id("authz")
        now = _now()
        resource_hash = (
            hashlib.sha256(resource_ref.encode("utf-8")).hexdigest()
            if resource_ref
            else None
        )
        with self.storage.locked_connection() as conn:
            user = conn.execute(
                "SELECT status, policy_epoch FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            capability = conn.execute(
                "SELECT id, status FROM security_ids WHERE security_id = ?",
                (security_id,),
            ).fetchone()
            preset = conn.execute(
                """
                SELECT p.preset_key, pv.id AS version_id, pv.version
                FROM user_preset_assignments upa
                JOIN permission_presets p ON p.id = upa.preset_id
                JOIN permission_preset_versions pv ON pv.id = p.active_version_id
                WHERE upa.user_id = ? AND upa.revoked_at IS NULL
                """,
                (user_id,),
            ).fetchone()
            epoch = int(user["policy_epoch"]) if user else 0
            preset_key = str(preset["preset_key"]) if preset else None
            preset_version = int(preset["version"]) if preset else None
            source: str | None = None
            if user is None:
                effect, reason = "deny", "unknown_user"
            elif user["status"] != "active":
                effect, reason = "deny", f"user_{user['status']}"
            elif capability is None:
                effect, reason = "deny", "unknown_security_id"
            elif capability["status"] != "active":
                effect, reason = "deny", f"security_id_{capability['status']}"
            else:
                cap_id = str(capability["id"])
                overrides = conn.execute(
                    """
                    SELECT effect FROM user_permissions
                    WHERE user_id = ? AND security_id_id = ? AND revoked_at IS NULL
                      AND (valid_until IS NULL OR valid_until > ?)
                    """,
                    (user_id, cap_id, now),
                ).fetchall()
                preset_rules = (
                    conn.execute(
                        """
                        SELECT effect FROM preset_security_ids
                        WHERE preset_version_id = ? AND security_id_id = ?
                        """,
                        (str(preset["version_id"]), cap_id),
                    ).fetchall()
                    if preset
                    else []
                )
                effects = [str(row["effect"]) for row in [*overrides, *preset_rules]]
                if "deny" in effects:
                    effect, reason, source = "deny", "explicit_deny", "permission"
                elif overrides and "grant" in effects:
                    effect, reason, source = "allow", "direct_grant", "user_permission"
                elif "grant" in effects:
                    effect, reason, source = "allow", "preset_grant", "preset"
                else:
                    effect, reason = "deny", "not_granted"

            decision = AuthorizationDecision(
                decision_id=decision_id,
                effect=effect,  # type: ignore[arg-type]
                security_id=security_id,
                user_id=user_id,
                reason_code=reason,
                policy_epoch=epoch,
                preset_key=preset_key,
                preset_version=preset_version,
                source=source,
            )
            if record:
                conn.execute(
                    """
                    INSERT INTO authorization_decisions(
                        id, ts, actor_user_id, identity_id, security_id, effect,
                        reason_code, policy_epoch, preset_key, preset_version, source,
                        request_id, resource_type, resource_ref_hash, context_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        now,
                        user_id if user else None,
                        identity_id,
                        security_id,
                        effect,
                        reason,
                        epoch,
                        preset_key,
                        preset_version,
                        source,
                        request_id,
                        resource_type,
                        resource_hash,
                        _json(context or {}),
                    ),
                )
                conn.commit()
        return decision

    def authorize_current(self, security_id: str, **kwargs: Any) -> AuthorizationDecision:
        actor = current_actor()
        return self.authorize(
            actor.user_id,
            security_id,
            identity_id=actor.identity_id,
            **kwargs,
        )

    def require_current(self, security_id: str, **kwargs: Any) -> AuthorizationDecision:
        decision = self.authorize_current(security_id, **kwargs)
        if not decision.allowed:
            raise AuthorizationError(
                f"{security_id} denied for user {decision.user_id}: {decision.reason_code}"
            )
        return decision

    def upsert_external_identity(
        self,
        *,
        provider: str,
        realm_id: str,
        provider_subject_id: int | str,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        locale: str | None = None,
        bootstrap_preset: str | None = None,
    ) -> dict[str, Any]:
        subject = str(provider_subject_id).strip()
        if not subject or len(subject) > 80:
            raise ValueError("provider_subject_id is required")
        if bootstrap_preset is not None and bootstrap_preset not in BUILTIN_PRESET_KEYS:
            raise ValueError("Unknown bootstrap preset")
        now = _now()
        with self.storage.transaction(immediate=True) as conn:
            identity = conn.execute(
                """
                SELECT ei.*, u.status, u.policy_epoch
                FROM external_identities ei
                JOIN users u ON u.id = ei.user_id
                WHERE ei.provider = ? AND ei.realm_id = ? AND ei.provider_subject_id = ?
                """,
                (provider, realm_id, subject),
            ).fetchone()
            created = identity is None
            if created:
                user_id = str(uuid.uuid4())
                identity_id = _new_id("identity")
                display_name = " ".join(
                    part for part in (first_name or "", last_name or "") if part
                ).strip()[:160]
                conn.execute(
                    """
                    INSERT INTO users(
                        id, status, display_name, locale, policy_epoch, created_at,
                        updated_at, first_seen_at, last_seen_at
                    ) VALUES (?, 'active', ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (user_id, display_name, (locale or "")[:32], now, now, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO external_identities(
                        id, user_id, provider, realm_id, provider_subject_id, username,
                        first_name, last_name, profile_snapshot, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identity_id,
                        user_id,
                        provider,
                        realm_id,
                        subject,
                        (username or "")[:160] or None,
                        (first_name or "")[:160] or None,
                        (last_name or "")[:160] or None,
                        _json({"locale": locale or ""}),
                        now,
                        now,
                    ),
                )
                preset_key = bootstrap_preset or DEFAULT_PRESET_KEY
                conn.execute(
                    """
                    INSERT INTO user_preset_assignments(
                        id, user_id, preset_id, assigned_by, assigned_at, reason
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _new_id("assignment"),
                        user_id,
                        f"preset_{preset_key}",
                        LEGACY_OWNER_USER_ID,
                        now,
                        f"Automatic {provider} registration",
                    ),
                )
            else:
                user_id = str(identity["user_id"])
                identity_id = str(identity["id"])
                conn.execute(
                    """
                    UPDATE external_identities
                    SET username = ?, first_name = ?, last_name = ?,
                        profile_snapshot = ?, last_seen_at = ?
                    WHERE id = ?
                    """,
                    (
                        (username or "")[:160] or None,
                        (first_name or "")[:160] or None,
                        (last_name or "")[:160] or None,
                        _json({"locale": locale or ""}),
                        now,
                        identity_id,
                    ),
                )
                conn.execute(
                    "UPDATE users SET last_seen_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, user_id),
                )
            row = conn.execute(
                """
                SELECT u.id AS user_id, u.status, u.policy_epoch, ei.id AS identity_id,
                       p.preset_key
                FROM users u
                JOIN external_identities ei ON ei.user_id = u.id
                JOIN user_preset_assignments upa
                  ON upa.user_id = u.id AND upa.revoked_at IS NULL
                JOIN permission_presets p ON p.id = upa.preset_id
                WHERE u.id = ? AND ei.id = ?
                """,
                (user_id, identity_id),
            ).fetchone()
        return {**dict(row), "created": created}

    def create_user_session(
        self,
        *,
        user_id: str,
        identity_id: str | None,
        auth_method: str,
        ttl_seconds: int = 86_400,
    ) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat(timespec="seconds")
        expires_at = (now_dt + timedelta(seconds=max(300, ttl_seconds))).isoformat(
            timespec="seconds"
        )
        session_id = _new_id("session")
        with self.storage.transaction(immediate=True) as conn:
            user = conn.execute("SELECT status FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None or user["status"] != "active":
                raise AuthorizationError("Cannot create a session for an inactive user")
            if identity_id is not None:
                identity = conn.execute(
                    "SELECT 1 FROM external_identities WHERE id = ? AND user_id = ?",
                    (identity_id, user_id),
                ).fetchone()
                if identity is None:
                    raise AuthorizationError("Session identity does not belong to user")
            conn.execute(
                """
                INSERT INTO user_sessions(
                    id, user_id, identity_id, token_sha256, auth_method,
                    created_at, last_seen_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    identity_id,
                    digest,
                    auth_method,
                    now,
                    now,
                    expires_at,
                ),
            )
        return {"session_token": token, "session_id": session_id, "expires_at": expires_at}

    def authenticate_session(self, token: str) -> ActorContext | None:
        raw = str(token or "").strip()
        if not raw:
            return None
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat(timespec="seconds")
        touch_before = (now_dt - timedelta(minutes=5)).isoformat(timespec="seconds")
        with self.storage.locked_connection() as conn:
            row = conn.execute(
                """
                SELECT s.id AS session_id, s.user_id, s.identity_id,
                       s.last_seen_at, u.status, u.policy_epoch, p.preset_key
                FROM user_sessions s
                JOIN users u ON u.id = s.user_id
                JOIN user_preset_assignments upa
                  ON upa.user_id = u.id AND upa.revoked_at IS NULL
                JOIN permission_presets p ON p.id = upa.preset_id
                WHERE s.token_sha256 = ? AND s.revoked_at IS NULL
                  AND s.expires_at > ? AND u.status = 'active'
                """,
                (digest, now),
            ).fetchone()
            if row is None:
                return None
            if str(row["last_seen_at"]) < touch_before:
                conn.execute(
                    "UPDATE user_sessions SET last_seen_at = ? WHERE id = ?",
                    (now, row["session_id"]),
                )
                conn.commit()
        return ActorContext(
            user_id=str(row["user_id"]),
            preset_key=str(row["preset_key"]),
            source="session",
            identity_id=str(row["identity_id"]) if row["identity_id"] else None,
            session_id=str(row["session_id"]),
            policy_epoch=int(row["policy_epoch"]),
        )

    def consume_rate_limit(
        self,
        *,
        scope: str,
        subject: str,
        limit: int,
        window_seconds: int = 60,
    ) -> dict[str, int | bool]:
        """Atomically consume one bounded ingress budget without storing raw identities."""

        safe_scope = str(scope).strip()[:80]
        if not safe_scope or not subject:
            raise ValueError("Rate-limit scope and subject are required")
        safe_limit = max(1, min(int(limit), 1_000_000))
        safe_window = max(1, min(int(window_seconds), 86_400))
        now_epoch = int(datetime.now(UTC).timestamp())
        window_start = now_epoch - (now_epoch % safe_window)
        reset_after = max(1, window_start + safe_window - now_epoch)
        subject_hash = hashlib.sha256(str(subject).encode("utf-8")).hexdigest()
        now = _now()
        with self.storage.transaction(immediate=True) as conn:
            row = conn.execute(
                """
                SELECT request_count FROM ingress_rate_limits
                WHERE scope = ? AND subject_hash = ? AND window_start = ?
                """,
                (safe_scope, subject_hash, window_start),
            ).fetchone()
            count = int(row["request_count"]) if row is not None else 0
            if count >= safe_limit:
                return {
                    "allowed": False,
                    "limit": safe_limit,
                    "remaining": 0,
                    "retry_after": reset_after,
                }
            count += 1
            conn.execute(
                """
                INSERT INTO ingress_rate_limits(
                    scope, subject_hash, window_start, request_count, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope, subject_hash, window_start) DO UPDATE SET
                    request_count = excluded.request_count,
                    updated_at = excluded.updated_at
                """,
                (safe_scope, subject_hash, window_start, count, now),
            )
        return {
            "allowed": True,
            "limit": safe_limit,
            "remaining": max(0, safe_limit - count),
            "retry_after": reset_after,
        }

    def prune_ephemeral_security_state(
        self,
        *,
        force: bool = False,
        session_retention_days: int = 7,
        telegram_update_retention_days: int = 7,
        decision_retention_days: int = 90,
    ) -> dict[str, int]:
        """Bound authentication/replay telemetry while retaining durable admin audit.

        ``security_audit_log`` is intentionally not pruned here. Authorization decisions
        are high-volume request telemetry; the security administration audit remains the
        long-lived record of policy mutations.
        """

        now_dt = datetime.now(UTC)
        with self._retention_lock:
            if (
                not force
                and self._last_retention_run is not None
                and now_dt - self._last_retention_run < timedelta(hours=1)
            ):
                return {
                    "sessions": 0,
                    "telegram_updates": 0,
                    "decisions": 0,
                    "rate_limits": 0,
                }
            self._last_retention_run = now_dt
        session_cutoff = (
            now_dt - timedelta(days=max(1, min(365, session_retention_days)))
        ).isoformat(timespec="seconds")
        update_cutoff = (
            now_dt - timedelta(days=max(1, min(90, telegram_update_retention_days)))
        ).isoformat(timespec="seconds")
        decision_cutoff = (
            now_dt - timedelta(days=max(7, min(3650, decision_retention_days)))
        ).isoformat(timespec="seconds")
        try:
            with self.storage.transaction(immediate=True) as conn:
                sessions = conn.execute(
                    """
                    DELETE FROM user_sessions
                    WHERE expires_at < ?
                      AND COALESCE(revoked_at, expires_at) < ?
                    """,
                    (now_dt.isoformat(timespec="seconds"), session_cutoff),
                ).rowcount
                updates = conn.execute(
                    "DELETE FROM telegram_updates WHERE updated_at < ?",
                    (update_cutoff,),
                ).rowcount
                decisions = conn.execute(
                    "DELETE FROM authorization_decisions WHERE ts < ?",
                    (decision_cutoff,),
                ).rowcount
                rate_limits = conn.execute(
                    "DELETE FROM ingress_rate_limits WHERE updated_at < ?",
                    ((now_dt - timedelta(days=2)).isoformat(timespec="seconds"),),
                ).rowcount
        except BaseException:
            with self._retention_lock:
                self._last_retention_run = None
            raise
        return {
            "sessions": max(0, int(sessions)),
            "telegram_updates": max(0, int(updates)),
            "decisions": max(0, int(decisions)),
            "rate_limits": max(0, int(rate_limits)),
        }

    def actor_for_user(self, user_id: str, *, source: str) -> ActorContext | None:
        """Load a fresh principal for a persisted background job owner."""

        with self.storage.locked_connection() as conn:
            row = conn.execute(
                """
                SELECT u.id AS user_id, u.status, u.policy_epoch, p.preset_key
                FROM users u
                JOIN user_preset_assignments upa
                  ON upa.user_id = u.id AND upa.revoked_at IS NULL
                JOIN permission_presets p ON p.id = upa.preset_id
                WHERE u.id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None or row["status"] != "active":
            return None
        return ActorContext(
            user_id=str(row["user_id"]),
            preset_key=str(row["preset_key"]),
            source=source,
            policy_epoch=int(row["policy_epoch"]),
        )

    def actor_for_authorized_owner(
        self,
        security_id: str,
        *,
        source: str,
    ) -> ActorContext | None:
        """Resolve a live owner for an authenticated system integration."""

        with self.storage.locked_connection() as conn:
            rows = conn.execute(
                """
                SELECT u.id
                FROM users u
                JOIN user_preset_assignments upa
                  ON upa.user_id = u.id AND upa.revoked_at IS NULL
                JOIN permission_presets p ON p.id = upa.preset_id
                WHERE u.status = 'active' AND p.preset_key = 'owner'
                ORDER BY CASE WHEN u.id = ? THEN 0 ELSE 1 END, u.created_at, u.id
                """,
                (LEGACY_OWNER_USER_ID,),
            ).fetchall()
        for row in rows:
            user_id = str(row["id"])
            if not self.authorize(user_id, security_id, record=False).allowed:
                continue
            actor = self.actor_for_user(user_id, source=source)
            if actor is not None:
                return actor
        return None

    @staticmethod
    def append_security_audit(
        conn: sqlite3.Connection,
        *,
        action: str,
        target_type: str,
        target_id: str | None,
        target_user_id: str | None,
        reason: str,
        before: Any = None,
        after: Any = None,
        actor_user_id: str | None = None,
    ) -> str:
        audit_id = _new_id("secaud")
        conn.execute(
            """
            INSERT INTO security_audit_log(
                id, ts, actor_user_id, action, target_type, target_id,
                target_user_id, reason, before_json, after_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                _now(),
                actor_user_id or current_actor().user_id,
                action[:160],
                target_type[:80],
                target_id,
                target_user_id,
                reason[:1000],
                _json(before),
                _json(after),
            ),
        )
        return audit_id

    def list_security_audit(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        with self.storage.locked_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, ts, actor_user_id, action, target_type, target_id,
                       target_user_id, reason, before_json, after_json
                FROM security_audit_log
                ORDER BY ts DESC, rowid DESC
                LIMIT ? OFFSET ?
                """,
                (max(1, min(500, limit)), max(0, offset)),
            ).fetchall()
        return [
            {
                **dict(row),
                "before": json.loads(str(row["before_json"])),
                "after": json.loads(str(row["after_json"])),
            }
            for row in rows
        ]

    def list_users_page(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        search: str = "",
    ) -> dict[str, Any]:
        bounded_limit = max(1, min(500, int(limit)))
        bounded_offset = max(0, int(offset))
        needle = " ".join(str(search or "").split())[:160]
        escaped = (
            needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        pattern = f"%{escaped}%"
        where_sql = ""
        params: dict[str, Any] = {
            "pattern": pattern,
            "limit": bounded_limit,
            "offset": bounded_offset,
        }
        if needle:
            where_sql = """
                WHERE lower(u.id) LIKE lower(:pattern) ESCAPE '\\'
                   OR lower(u.display_name) LIKE lower(:pattern) ESCAPE '\\'
                   OR lower(u.status) LIKE lower(:pattern) ESCAPE '\\'
                   OR lower(COALESCE(p.preset_key, '')) LIKE lower(:pattern) ESCAPE '\\'
                   OR EXISTS (
                       SELECT 1 FROM external_identities search_identity
                       WHERE search_identity.user_id = u.id
                         AND (
                             lower(search_identity.provider) LIKE lower(:pattern) ESCAPE '\\'
                             OR lower(search_identity.provider_subject_id)
                                LIKE lower(:pattern) ESCAPE '\\'
                             OR lower(COALESCE(search_identity.username, ''))
                                LIKE lower(:pattern) ESCAPE '\\'
                             OR lower(COALESCE(search_identity.first_name, ''))
                                LIKE lower(:pattern) ESCAPE '\\'
                             OR lower(COALESCE(search_identity.last_name, ''))
                                LIKE lower(:pattern) ESCAPE '\\'
                         )
                   )
            """
        with self.storage.locked_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT u.id, u.status, u.display_name, u.locale, u.policy_epoch,
                       u.created_at, u.first_seen_at, u.last_seen_at, u.row_version,
                       p.preset_key
                FROM users u
                LEFT JOIN user_preset_assignments upa
                  ON upa.user_id = u.id AND upa.revoked_at IS NULL
                LEFT JOIN permission_presets p ON p.id = upa.preset_id
                {where_sql}
                ORDER BY u.last_seen_at DESC, u.id
                LIMIT :limit OFFSET :offset
                """,
                params,
            ).fetchall()
            total = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM users u
                    LEFT JOIN user_preset_assignments upa
                      ON upa.user_id = u.id AND upa.revoked_at IS NULL
                    LEFT JOIN permission_presets p ON p.id = upa.preset_id
                    {where_sql}
                    """,
                    params,
                ).fetchone()["count"]
            )
            summary = conn.execute(
                """
                SELECT COUNT(*) AS overall_total,
                       SUM(CASE WHEN status != 'active' THEN 1 ELSE 0 END) AS inactive_total
                FROM users
                """
            ).fetchone()
            telegram_total = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT user_id) AS count "
                    "FROM external_identities WHERE provider = 'telegram'"
                ).fetchone()["count"]
            )
            user_ids = [str(row["id"]) for row in rows]
            identities_by_user: dict[str, list[dict[str, Any]]] = {
                user_id: [] for user_id in user_ids
            }
            if user_ids:
                placeholders = ",".join("?" for _ in user_ids)
                identities = conn.execute(
                    f"""
                    SELECT id, user_id, provider, realm_id, provider_subject_id,
                           username, first_name, last_name, first_seen_at, last_seen_at
                    FROM external_identities
                    WHERE user_id IN ({placeholders})
                    ORDER BY CASE provider WHEN 'telegram' THEN 0 ELSE 1 END,
                             last_seen_at DESC, id
                    """,
                    tuple(user_ids),
                ).fetchall()
                for identity in identities:
                    identities_by_user[str(identity["user_id"])].append(dict(identity))

        users: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            identities = identities_by_user.get(str(row["id"]), [])
            preferred = identities[0] if identities else {}
            item["identities"] = identities
            for key in (
                "identity_id",
                "provider",
                "realm_id",
                "provider_subject_id",
                "username",
                "first_name",
                "last_name",
            ):
                source_key = "id" if key == "identity_id" else key
                item[key] = preferred.get(source_key)
            users.append(item)
        return {
            "users": users,
            "total": total,
            "overall_total": int(summary["overall_total"] or 0),
            "telegram_total": telegram_total,
            "inactive_total": int(summary["inactive_total"] or 0),
            "limit": bounded_limit,
            "offset": bounded_offset,
            "search": needle,
        }

    def list_users(
        self, *, limit: int = 100, offset: int = 0, search: str = ""
    ) -> list[dict[str, Any]]:
        return list(
            self.list_users_page(limit=limit, offset=offset, search=search)["users"]
        )

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.storage.locked_connection() as conn:
            row = conn.execute(
                """
                SELECT u.id, u.status, u.display_name, u.locale, u.policy_epoch,
                       u.created_at, u.first_seen_at, u.last_seen_at, u.row_version,
                       p.preset_key, ei.id AS identity_id, ei.provider, ei.realm_id,
                       ei.provider_subject_id, ei.username, ei.first_name, ei.last_name
                FROM users u
                LEFT JOIN user_preset_assignments upa
                  ON upa.user_id = u.id AND upa.revoked_at IS NULL
                LEFT JOIN permission_presets p ON p.id = upa.preset_id
                LEFT JOIN external_identities ei ON ei.user_id = u.id
                WHERE u.id = ?
                ORDER BY ei.first_seen_at ASC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_security_ids(self) -> list[dict[str, Any]]:
        with self.storage.locked_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, security_id, description, category, risk_level,
                       default_requires_hitl, source, status, created_at, updated_at
                FROM security_ids
                ORDER BY category, security_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def list_presets(self) -> list[dict[str, Any]]:
        with self.storage.locked_connection() as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.preset_key, p.display_name, p.kind, p.active_version_id,
                       pv.version, pv.published_at, COUNT(psi.security_id_id) AS permission_count
                FROM permission_presets p
                LEFT JOIN permission_preset_versions pv ON pv.id = p.active_version_id
                LEFT JOIN preset_security_ids psi ON psi.preset_version_id = pv.id
                WHERE p.archived_at IS NULL
                GROUP BY p.id, pv.id
                ORDER BY CASE p.preset_key
                    WHEN 'owner' THEN 1 WHEN 'admin' THEN 2 WHEN 'moderator' THEN 3
                    WHEN 'user' THEN 4 WHEN 'guest' THEN 5 ELSE 6 END,
                    p.display_name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def effective_permissions(self, user_id: str) -> list[dict[str, Any]]:
        with self.storage.locked_connection() as conn:
            capability_rows = conn.execute(
                "SELECT security_id FROM security_ids ORDER BY security_id"
            ).fetchall()
        return [
            self.authorize(user_id, str(row["security_id"]), record=False).as_dict()
            for row in capability_rows
        ]

    @staticmethod
    def assert_owner_recovery_invariant(conn: sqlite3.Connection) -> None:
        """Require one active owner with every effective recovery capability.

        Counting owner presets is insufficient: another owner may carry a direct deny.
        Call this inside the same write transaction, after the proposed policy mutation,
        so a multi-step change cannot strand the installation without a recovery actor.
        """

        owners = conn.execute(
            """
            SELECT u.id AS user_id, pv.id AS preset_version_id
            FROM users u
            JOIN user_preset_assignments upa
              ON upa.user_id = u.id AND upa.revoked_at IS NULL
            JOIN permission_presets p ON p.id = upa.preset_id
            JOIN permission_preset_versions pv ON pv.id = p.active_version_id
            WHERE u.status = 'active' AND p.preset_key = 'owner'
            """
        ).fetchall()
        recovery_ids = tuple(sorted(OWNER_RECOVERY_SECURITY_IDS))
        placeholders = ",".join("?" for _ in recovery_ids)
        capabilities = conn.execute(
            f"""
            SELECT id, security_id, status
            FROM security_ids
            WHERE security_id IN ({placeholders})
            """,
            recovery_ids,
        ).fetchall()
        if len(capabilities) != len(recovery_ids) or any(
            str(row["status"]) != "active" for row in capabilities
        ):
            raise AuthorizationError(
                "Owner recovery capability catalog is incomplete or inactive"
            )

        now = _now()
        for owner in owners:
            recoverable = True
            for capability in capabilities:
                overrides = conn.execute(
                    """
                    SELECT effect FROM user_permissions
                    WHERE user_id = ? AND security_id_id = ? AND revoked_at IS NULL
                      AND (valid_until IS NULL OR valid_until > ?)
                    """,
                    (owner["user_id"], capability["id"], now),
                ).fetchall()
                preset_rules = conn.execute(
                    """
                    SELECT effect FROM preset_security_ids
                    WHERE preset_version_id = ? AND security_id_id = ?
                    """,
                    (owner["preset_version_id"], capability["id"]),
                ).fetchall()
                effects = [
                    str(row["effect"]) for row in [*overrides, *preset_rules]
                ]
                if "deny" in effects or "grant" not in effects:
                    recoverable = False
                    break
            if recoverable:
                return
        raise AuthorizationError(
            "At least one active owner must retain all recovery permissions"
        )

    @staticmethod
    def assert_actor_is_active_owner(
        conn: sqlite3.Connection,
        user_id: str,
    ) -> None:
        row = conn.execute(
            """
            SELECT 1
            FROM users u
            JOIN user_preset_assignments upa
              ON upa.user_id = u.id AND upa.revoked_at IS NULL
            JOIN permission_presets p ON p.id = upa.preset_id
            WHERE u.id = ? AND u.status = 'active' AND p.preset_key = 'owner'
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            raise AuthorizationError("Only an active owner may modify an owner")

    def assign_preset(
        self,
        *,
        user_id: str,
        preset_key: str,
        assigned_by: str,
        reason: str,
        expected_row_version: int | None = None,
    ) -> dict[str, Any]:
        if preset_key not in BUILTIN_PRESET_KEYS:
            with self.storage.locked_connection() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM permission_presets WHERE preset_key = ? AND archived_at IS NULL",
                    (preset_key,),
                ).fetchone()
            if exists is None:
                raise ValueError("Unknown preset")
        now = _now()
        with self.storage.transaction(immediate=True) as conn:
            current = conn.execute(
                """
                SELECT p.preset_key, u.row_version
                FROM users u
                JOIN user_preset_assignments upa
                  ON upa.user_id = u.id AND upa.revoked_at IS NULL
                JOIN permission_presets p ON p.id = upa.preset_id
                WHERE u.id = ?
                """,
                (user_id,),
            ).fetchone()
            if current is None:
                raise ValueError("Unknown user")
            if current["preset_key"] == "owner" or preset_key == "owner":
                self.assert_actor_is_active_owner(conn, current_actor().user_id)
            if (
                expected_row_version is not None
                and int(current["row_version"]) != expected_row_version
            ):
                raise ConcurrentPolicyUpdateError("Target user changed after authorization")
            if current and current["preset_key"] == "owner" and preset_key != "owner":
                owners = conn.execute(
                    """
                    SELECT COUNT(*) AS c
                    FROM user_preset_assignments upa
                    JOIN permission_presets p ON p.id = upa.preset_id
                    JOIN users u ON u.id = upa.user_id
                    WHERE upa.revoked_at IS NULL AND p.preset_key = 'owner'
                      AND u.status = 'active'
                    """
                ).fetchone()["c"]
                if int(owners) <= 1:
                    raise AuthorizationError("Cannot demote the last active owner")
            preset = conn.execute(
                "SELECT id FROM permission_presets WHERE preset_key = ? AND archived_at IS NULL",
                (preset_key,),
            ).fetchone()
            if preset is None:
                raise ValueError("Unknown preset")
            conn.execute(
                "UPDATE user_preset_assignments SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            conn.execute(
                """
                INSERT INTO user_preset_assignments(
                    id, user_id, preset_id, assigned_by, assigned_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_new_id("assignment"), user_id, preset["id"], assigned_by, now, reason[:500]),
            )
            if current["preset_key"] == "owner" or preset_key == "owner":
                self.assert_owner_recovery_invariant(conn)
            conn.execute(
                """
                UPDATE users SET policy_epoch = policy_epoch + 1,
                    row_version = row_version + 1, updated_at = ? WHERE id = ?
                """,
                (now, user_id),
            )
            conn.execute(
                "UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            self.append_security_audit(
                conn,
                action="user.preset.assign",
                target_type="user",
                target_id=user_id,
                target_user_id=user_id,
                reason=reason,
                before={"preset_key": str(current["preset_key"]) if current else None},
                after={"preset_key": preset_key},
                actor_user_id=assigned_by,
            )
        result = self.get_user(user_id)
        if result is None:
            raise RuntimeError("Preset target disappeared after assignment")
        return result

    def set_user_status(
        self,
        *,
        user_id: str,
        status: str,
        reason: str,
        expected_row_version: int | None = None,
    ) -> dict[str, Any]:
        if status not in {"active", "suspended", "deleted"}:
            raise ValueError("Invalid status")
        now = _now()
        with self.storage.transaction(immediate=True) as conn:
            current = conn.execute(
                """
                SELECT u.status, u.row_version, p.preset_key
                FROM users u
                LEFT JOIN user_preset_assignments upa
                  ON upa.user_id = u.id AND upa.revoked_at IS NULL
                LEFT JOIN permission_presets p ON p.id = upa.preset_id
                WHERE u.id = ?
                """,
                (user_id,),
            ).fetchone()
            if current is None:
                raise ValueError("Unknown user")
            if current["preset_key"] == "owner":
                self.assert_actor_is_active_owner(conn, current_actor().user_id)
            if (
                expected_row_version is not None
                and int(current["row_version"]) != expected_row_version
            ):
                raise ConcurrentPolicyUpdateError("Target user changed after authorization")
            if current["preset_key"] == "owner" and status != "active":
                owners = conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM users u
                    JOIN user_preset_assignments upa
                      ON upa.user_id = u.id AND upa.revoked_at IS NULL
                    JOIN permission_presets p ON p.id = upa.preset_id
                    WHERE u.status = 'active' AND p.preset_key = 'owner'
                    """
                ).fetchone()["c"]
                if int(owners) <= 1:
                    raise AuthorizationError("Cannot suspend or delete the last active owner")
            conn.execute(
                """
                UPDATE users SET status = ?, policy_epoch = policy_epoch + 1,
                    row_version = row_version + 1, updated_at = ?,
                    deleted_at = CASE WHEN ? = 'deleted' THEN ? ELSE deleted_at END
                WHERE id = ?
                """,
                (status, now, status, now, user_id),
            )
            if current["preset_key"] == "owner":
                self.assert_owner_recovery_invariant(conn)
            if status != "active":
                conn.execute(
                    "UPDATE user_sessions SET revoked_at = ? "
                    "WHERE user_id = ? AND revoked_at IS NULL",
                    (now, user_id),
                )
            self.append_security_audit(
                conn,
                action="user.status.update",
                target_type="user",
                target_id=user_id,
                target_user_id=user_id,
                reason=reason,
                before={"status": str(current["status"])},
                after={"status": status},
            )
        result = self.get_user(user_id)
        if result is None:
            raise RuntimeError("Status target disappeared after update")
        result["change_reason"] = reason[:500]
        return result

    def set_user_permission(
        self,
        *,
        user_id: str,
        security_id: str,
        effect: str,
        can_delegate: bool,
        granted_by: str,
        reason: str,
        valid_until: str | None = None,
        expected_row_version: int | None = None,
    ) -> dict[str, Any]:
        if effect not in {"grant", "deny"}:
            raise ValueError("effect must be grant or deny")
        now = _now()
        with self.storage.transaction(immediate=True) as conn:
            target = conn.execute(
                """
                SELECT p.preset_key, u.row_version
                FROM users u
                LEFT JOIN user_preset_assignments upa
                  ON upa.user_id = u.id AND upa.revoked_at IS NULL
                LEFT JOIN permission_presets p ON p.id = upa.preset_id
                WHERE u.id = ?
                """,
                (user_id,),
            ).fetchone()
            if target is None:
                raise ValueError("Unknown user")
            if target["preset_key"] == "owner":
                self.assert_actor_is_active_owner(conn, current_actor().user_id)
            if (
                expected_row_version is not None
                and int(target["row_version"]) != expected_row_version
            ):
                raise ConcurrentPolicyUpdateError("Target user changed after authorization")
            if (
                target["preset_key"] == "owner"
                and effect == "deny"
                and security_id in OWNER_RECOVERY_SECURITY_IDS
            ):
                owners = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) AS c FROM users u
                        JOIN user_preset_assignments upa
                          ON upa.user_id = u.id AND upa.revoked_at IS NULL
                        JOIN permission_presets p ON p.id = upa.preset_id
                        WHERE u.status = 'active' AND p.preset_key = 'owner'
                        """
                    ).fetchone()["c"]
                )
                if owners <= 1:
                    raise AuthorizationError(
                        "Cannot deny recovery permissions to the last active owner"
                    )
            capability = conn.execute(
                "SELECT id, risk_level FROM security_ids "
                "WHERE security_id = ? AND status = 'active'",
                (security_id,),
            ).fetchone()
            if capability is None:
                raise ValueError("Unknown security_id")
            previous = conn.execute(
                """
                SELECT effect, can_delegate, valid_until, reason
                FROM user_permissions
                WHERE user_id = ? AND security_id_id = ? AND revoked_at IS NULL
                """,
                (user_id, capability["id"]),
            ).fetchone()
            conn.execute(
                """
                UPDATE user_permissions SET revoked_at = ?
                WHERE user_id = ? AND security_id_id = ? AND revoked_at IS NULL
                """,
                (now, user_id, capability["id"]),
            )
            conn.execute(
                """
                INSERT INTO user_permissions(
                    id, user_id, security_id_id, effect, can_delegate,
                    granted_by, created_at, valid_until, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("uperm"),
                    user_id,
                    capability["id"],
                    effect,
                    int(can_delegate),
                    granted_by,
                    now,
                    valid_until,
                    reason[:500],
                ),
            )
            if (
                target["preset_key"] == "owner"
                and security_id in OWNER_RECOVERY_SECURITY_IDS
            ):
                self.assert_owner_recovery_invariant(conn)
            conn.execute(
                "UPDATE users SET policy_epoch = policy_epoch + 1, "
                "row_version = row_version + 1, updated_at = ? WHERE id = ?",
                (now, user_id),
            )
            # Session actors snapshot policy_epoch and ToolRegistry caches effective
            # permissions by that epoch.  Revoke active sessions on every direct
            # permission mutation so no cached grant survives a deny/regrant change.
            conn.execute(
                "UPDATE user_sessions SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            self.append_security_audit(
                conn,
                action="user.permission.set",
                target_type="security_id",
                target_id=security_id,
                target_user_id=user_id,
                reason=reason,
                before=dict(previous) if previous is not None else None,
                after={
                    "effect": effect,
                    "can_delegate": bool(can_delegate),
                    "valid_until": valid_until,
                },
                actor_user_id=granted_by,
            )
        return self.authorize(user_id, security_id, record=False).as_dict()
