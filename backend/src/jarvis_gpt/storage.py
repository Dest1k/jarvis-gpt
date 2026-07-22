from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import threading
import unicodedata
import uuid
from collections import Counter, OrderedDict, defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

from .authorization import (
    LEGACY_OWNER_USER_ID,
    AuthorizationError,
    ResourceIsolationError,
    current_actor,
    current_user_id,
    migrate_iam_schema,
)
from .memory_vault import MemoryVault
from .redaction import redact_value

LOGGER = logging.getLogger(__name__)

_RUNTIME_KEY_MAX_CHARS = 1_024
_LEGACY_RUNTIME_KEY_MAX_CHARS = 160
_RUNTIME_KEY_HASH_HEAD_CHARS = _RUNTIME_KEY_MAX_CHARS - 65

_CHAT_REQUEST_METADATA: ContextVar[dict[str, str] | None] = ContextVar(
    "jarvis_chat_request_metadata",
    default=None,
)
_CHAT_INGRESS_MESSAGE_ID: ContextVar[str | None] = ContextVar(
    "jarvis_chat_ingress_message_id",
    default=None,
)


def set_chat_ingress_message_id(message_id: str) -> None:
    """Bind the exact durable user row that the next terminal assistant row answers."""

    _CHAT_INGRESS_MESSAGE_ID.set(str(message_id or "").strip() or None)


@contextmanager
def bind_chat_request_metadata(
    *,
    request_hash: str,
    fingerprint: str,
) -> Iterable[None]:
    """Tag every history row written by one transport-fenced chat turn."""

    token = _CHAT_REQUEST_METADATA.set(
        {
            "chat_request_hash": request_hash,
            "chat_request_fingerprint": fingerprint,
        }
    )
    try:
        yield
    finally:
        _CHAT_REQUEST_METADATA.reset(token)

# Document-graph augmentation: fold uploaded documents into the memory link graph.
DOCUMENT_GRAPH_NODE_CAP = 5000
_DOC_DERIV_BUCKET_K = 6  # groups larger than this collapse to a hub-and-star, not a clique
_DOC_DERIV_PER_DOC_DEGREE_CAP = 8  # max derived edges any single document may accrue
_DOC_MENTION_EDGE_CAP = 10_000  # bound adversarial all-notes x all-documents mentions
_MEMORY_VAULT_REPAIR_KEY = "memory_vault.repair_required"
_PRIVILEGED_DERIVED_TAG = "privileged-derived"
_MEMORY_VAULT_CACHE_MAX = 512
# Mirrors document_memory._FILE_ID_RE; duplicated locally to avoid an import cycle.
_DOC_FILE_ID_RE = re.compile(r"\bfile_[0-9a-fA-F]{8,}\b")


class _FilenameMatcher:
    """Case-insensitive multi-pattern matcher with filename-aware boundaries."""

    def __init__(self, names: Iterable[str]) -> None:
        self._transitions: list[dict[str, int]] = [{}]
        self._failures = [0]
        self._outputs: list[list[str]] = [[]]
        for name in sorted(set(names)):
            state = 0
            for character in name:
                next_state = self._transitions[state].get(character)
                if next_state is None:
                    next_state = len(self._transitions)
                    self._transitions[state][character] = next_state
                    self._transitions.append({})
                    self._failures.append(0)
                    self._outputs.append([])
                state = next_state
            self._outputs[state].append(name)

        pending = deque(self._transitions[0].values())
        while pending:
            state = pending.popleft()
            for character, next_state in self._transitions[state].items():
                pending.append(next_state)
                fallback = self._failures[state]
                while fallback and character not in self._transitions[fallback]:
                    fallback = self._failures[fallback]
                self._failures[next_state] = self._transitions[fallback].get(character, 0)
                self._outputs[next_state].extend(self._outputs[self._failures[next_state]])

    def find(self, text: str) -> set[str]:
        folded = text.casefold()
        state = 0
        matches: list[tuple[int, int, str]] = []
        for end, character in enumerate(folded, start=1):
            while state and character not in self._transitions[state]:
                state = self._failures[state]
            state = self._transitions[state].get(character, 0)
            for name in self._outputs[state]:
                start = end - len(name)
                if _is_filename_boundary(folded, start, end):
                    matches.append((start, end, name))

        # File names may contain spaces and punctuation, so both ``report.pdf``
        # and ``old report.pdf`` have valid lexical boundaries in the latter
        # phrase. Keep only maximal spans; a standalone occurrence elsewhere
        # still keeps the shorter registered name in the returned set.
        found: set[str] = set()
        farthest_end = -1
        for _start, end, name in sorted(matches, key=lambda item: (item[0], -item[1])):
            if end <= farthest_end:
                continue
            found.add(name)
            farthest_end = end
        return found


def _is_filename_boundary(text: str, start: int, end: int) -> bool:
    """Reject a basename embedded in a longer filename, e.g. report.pdf in old_report.pdf."""

    def is_filename_character(character: str) -> bool:
        return character.isalnum() or character in "._-"

    return (start == 0 or not is_filename_character(text[start - 1])) and (
        end == len(text) or not is_filename_character(text[end])
    )

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


def _sql_like_prefix(prefix: str) -> str:
    """Escape a literal SQLite LIKE prefix, including namespace underscores."""

    return (
        prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        + "%"
    )


def _scoped_runtime_key_unbounded(key: str) -> str:
    clean = str(key).strip()
    actor = current_actor()
    if actor.user_id == LEGACY_OWNER_USER_ID:
        return clean
    namespace = f"user.{actor.user_id}."
    return clean if clean.startswith(namespace) else f"{namespace}{clean}"


def _normalized_runtime_key(key: str) -> str:
    """Scope and bound a runtime key without collision-prone truncation."""

    scoped = _scoped_runtime_key_unbounded(key)
    if len(scoped) <= _RUNTIME_KEY_MAX_CHARS:
        return scoped
    digest = hashlib.sha256(scoped.encode("utf-8")).hexdigest()
    return f"{scoped[:_RUNTIME_KEY_HASH_HEAD_CHARS]}~{digest}"


def _normalized_runtime_prefix(prefix: str) -> str:
    scoped = _scoped_runtime_key_unbounded(prefix)
    # Internal runtime families are short. A hashed overlong key no longer has
    # a meaningful SQL prefix, so reject ambiguous prefix scans explicitly.
    if len(scoped) > _RUNTIME_KEY_HASH_HEAD_CHARS:
        raise ValueError("runtime key prefix is too long")
    return scoped


def _legacy_runtime_row_matches_key(key: str, raw_value: str | None) -> bool:
    """Verify the one known >160-char legacy family before migrating it."""

    value = _loads(raw_value, None)
    if not isinstance(value, dict):
        return False
    if value.get("protocol") != "jarvis.interrupted-stream.v2":
        return False
    request_hash = str(value.get("request_hash") or "")
    return bool(
        re.fullmatch(r"[0-9a-f]{64}", request_hash)
        and key.endswith(f"agent.stream.interrupted.request.{request_hash}")
    )


def _runtime_row_with_legacy_migration(
    conn: sqlite3.Connection,
    key: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT key, value, updated_at FROM runtime_kv WHERE key = ?",
        (key,),
    ).fetchone()
    if row is not None or len(key) <= _LEGACY_RUNTIME_KEY_MAX_CHARS:
        return row
    legacy_key = key[:_LEGACY_RUNTIME_KEY_MAX_CHARS]
    legacy = conn.execute(
        "SELECT key, value, updated_at FROM runtime_kv WHERE key = ?",
        (legacy_key,),
    ).fetchone()
    if legacy is None or not _legacy_runtime_row_matches_key(key, legacy["value"]):
        return None
    conn.execute(
        "INSERT OR IGNORE INTO runtime_kv(key, value, updated_at) VALUES (?, ?, ?)",
        (key, legacy["value"], legacy["updated_at"]),
    )
    conn.execute(
        "DELETE FROM runtime_kv WHERE key = ? AND value = ?",
        (legacy_key, legacy["value"]),
    )
    return conn.execute(
        "SELECT key, value, updated_at FROM runtime_kv WHERE key = ?",
        (key,),
    ).fetchone()


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


def _unicode_search_fold(value: Any) -> str:
    """Stable Unicode normalization used by literal fallbacks and SQLite."""

    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _fts_query(query: str) -> str:
    return " OR ".join(f'"{term}"' for term in _query_terms(query, limit=8))


def _recoverable_fts_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "no such module: fts5",
            "no such tokenizer: trigram",
            "unknown tokenizer: trigram",
            "error in tokenizer constructor",
            "no such table: messages_fts",
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


def _decorate_message_hit(row: dict[str, Any], query: str) -> dict[str, Any]:
    terms = _query_terms(query)
    if not terms and query.strip():
        terms = [query.strip()]
    matched = _matched_terms(str(row.get("content") or ""), terms)
    row["metadata"] = _loads(row.get("metadata"), {})
    row["matched_terms"] = matched
    row["snippet"] = _snippet(str(row.get("content") or ""), matched or terms)
    row["relevance"] = _relevance(
        row.get("rank"),
        matched_terms=matched,
        query_terms=terms,
        importance=0,
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
    safe_table = str(table).replace("'", "''")
    row = conn.execute(
        f"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '{safe_table}'",
    ).fetchone()
    return row is not None


def _sqlite_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _sqlite_fts_tokenizer(conn: sqlite3.Connection, table: str) -> str | None:
    safe_table = str(table).replace("'", "''")
    row = conn.execute(
        f"SELECT sql FROM sqlite_master WHERE type = 'table' AND name = '{safe_table}'",
    ).fetchone()
    if row is None or not row[0]:
        return None
    match = re.search(
        r"\btokenize\s*=\s*(?:'([^']+)'|\"([^\"]+)\"|([^\s,)]+))",
        str(row[0]),
        flags=re.IGNORECASE,
    )
    if match is None:
        return "unicode61"
    specification = next((item for item in match.groups() if item is not None), "")
    return specification.strip().split(maxsplit=1)[0].casefold() or None


def _physical_file_suffix(name: str) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    return suffix if re.fullmatch(r"\.[\w-]{1,16}", suffix, flags=re.UNICODE) else ""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_AUTOMATIC_OCR_IMAGE_SUFFIXES = {
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


class JarvisStorage:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._read_only = False
        self._message_fts_available = False
        self._memory_fts_available = False
        self._file_fts_available = False
        self._files_root = database_path.parent.parent / "files"
        self._memory_vault_root = database_path.parent.parent / "memory-vault"
        self.memory_vault = MemoryVault(self._memory_vault_root)
        self._user_memory_vaults: OrderedDict[str, MemoryVault] = OrderedDict()

    @property
    def read_only(self) -> bool:
        return self._read_only

    def _can_read_privileged_derived(self) -> bool:
        """Recheck the current role in SQLite before exposing classified history."""

        actor = current_actor()
        if actor.preset_key not in {"owner", "admin"}:
            return False
        with self._lock:
            row = self.connect().execute(
                """
                SELECT p.preset_key, u.status
                FROM users u
                JOIN user_preset_assignments upa
                  ON upa.user_id = u.id AND upa.revoked_at IS NULL
                JOIN permission_presets p ON p.id = upa.preset_id
                WHERE u.id = ?
                """,
                (actor.user_id,),
            ).fetchone()
        return bool(
            row is not None
            and str(row["status"]) == "active"
            and str(row["preset_key"]) in {"owner", "admin"}
        )

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.database_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.create_function(
                "jarvis_search_fold",
                1,
                _unicode_search_fold,
                deterministic=True,
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def locked_connection(self):
        """Expose the shared connection under its process-local serialization lock."""

        with self._lock:
            yield self.connect()

    @contextmanager
    def transaction(self, *, immediate: bool = False):
        """Run one IAM/admin mutation atomically on the shared SQLite connection."""

        with self._lock:
            conn = self.connect()
            if conn.in_transaction:
                raise RuntimeError("nested storage transactions are not supported")
            try:
                conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def cleanup_deleted_user_artifacts(
        self,
        user_id: str,
        *,
        stored_paths: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Remove managed filesystem artifacts after a user's DB deletion commits.

        Account deletion is authoritative in SQLite. Filesystem cleanup is deliberately
        restricted to the deleted tenant's managed directories and recorded generated
        documents under ``data_dir``; an unexpected path is skipped instead of allowing
        an IAM operation to erase an arbitrary host file.
        """

        owner_id = str(user_id).strip()
        if (
            not owner_id
            or owner_id == LEGACY_OWNER_USER_ID
            or Path(owner_id).name != owner_id
            or "/" in owner_id
            or "\\" in owner_id
            or owner_id in {".", ".."}
        ):
            return {
                "complete": False,
                "removed": 0,
                "failures": ["invalid or protected user artifact scope"],
            }

        data_root = self.database_path.parent.parent.resolve(strict=False)
        files_users_root = (data_root / "files" / "users").resolve(strict=False)
        vault_users_root = (data_root / "memory-vault" / "users").resolve(strict=False)
        generated_root = (data_root / "document-outputs").resolve(strict=False)
        files_root = files_users_root / owner_id
        vault_root = vault_users_root / owner_id
        failures: list[str] = []
        removed = 0

        with self._lock:
            self._user_memory_vaults.pop(owner_id, None)

        for root, expected_parent in (
            (files_root, files_users_root),
            (vault_root, vault_users_root),
        ):
            try:
                if root.parent.resolve(strict=False) != expected_parent:
                    failures.append(f"unsafe managed directory: {root}")
                    continue
                if root.is_symlink():
                    root.unlink(missing_ok=True)
                    removed += 1
                elif root.exists():
                    shutil.rmtree(root)
                    removed += 1
            except OSError as exc:
                failures.append(f"{root}: {type(exc).__name__}")

        # Generated documents predate per-user filesystem directories. Delete only exact
        # DB-recorded files that remain inside the managed output directory.
        for raw_path in dict.fromkeys(str(item) for item in stored_paths if str(item)):
            path = Path(raw_path)
            try:
                resolved = path.resolve(strict=False)
                if not resolved.is_relative_to(generated_root):
                    continue
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError as exc:
                failures.append(f"{path}: {type(exc).__name__}")

        if failures:
            LOGGER.error(
                "Deleted user %s but could not remove every managed artifact: %s",
                owner_id,
                "; ".join(failures),
            )
        return {"complete": not failures, "removed": removed, "failures": failures}

    def _memory_vault_for(self, user_id: str | None = None) -> MemoryVault:
        owner_id = user_id or current_user_id()
        if owner_id == LEGACY_OWNER_USER_ID:
            return self.memory_vault
        with self._lock:
            vault = self._user_memory_vaults.pop(owner_id, None)
            if vault is None:
                vault = MemoryVault(self._memory_vault_root / "users" / owner_id)
            self._user_memory_vaults[owner_id] = vault
            while len(self._user_memory_vaults) > _MEMORY_VAULT_CACHE_MAX:
                self._user_memory_vaults.popitem(last=False)
            return vault

    @staticmethod
    def _require_owned_resource(
        conn: sqlite3.Connection,
        *,
        table: str,
        resource_id: str,
        id_column: str = "id",
        user_id: str | None = None,
    ) -> None:
        """Fail closed when a caller references another user's resource."""

        owner_id = user_id or current_user_id()
        row = conn.execute(
            f'SELECT user_id FROM "{table}" WHERE "{id_column}" = ?',
            (resource_id,),
        ).fetchone()
        if row is None:
            raise KeyError(resource_id)
        if str(row["user_id"]) != owner_id:
            raise ResourceIsolationError("resource is not available to this user")

    @staticmethod
    def _require_system_scope(operation: str) -> None:
        """Reserve cross-tenant maintenance entrypoints for an owner/system actor."""

        if not current_actor().is_owner:
            raise AuthorizationError(f"{operation} requires owner system scope")

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
                self._message_fts_available = _sqlite_table_exists(
                    conn, "messages_fts"
                ) and _sqlite_fts_tokenizer(conn, "messages_fts") == "trigram"
                self._memory_fts_available = _sqlite_table_exists(
                    conn, "memories_fts"
                ) and _sqlite_fts_tokenizer(conn, "memories_fts") == "trigram"
                self._file_fts_available = _sqlite_table_exists(
                    conn, "file_chunks_fts"
                ) and _sqlite_fts_tokenizer(conn, "file_chunks_fts") == "trigram"
            except BaseException:
                conn.close()
                raise
            self._conn = conn
            self._read_only = True

    def initialize(self) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.executescript(SCHEMA)
                migrate_iam_schema(conn)
                _migrate_messenger_schema(conn)
                _migrate_file_reliability_schema(conn)
                self._message_fts_available = self._ensure_messages_fts(conn)
                self._memory_fts_available = self._ensure_memory_fts(conn)
                self._reconcile_upload_intents(conn)
                self._reconcile_file_ocr_jobs(conn)
                self._reconcile_file_index_states(conn)
                self._file_fts_available = self._ensure_file_chunks_fts(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            # SQLite is authoritative. A damaged/unavailable Markdown mirror
            # must not leave migrations open or prevent the service starting;
            # the durable repair marker makes the next write retry a full sync.
            self._sync_memory_vault_after_commit(conn, force_full=True)

    def _tenant_files_root(self, user_id: str) -> Path:
        if user_id == LEGACY_OWNER_USER_ID:
            return self._files_root
        return self._files_root / "users" / user_id

    def _upload_intent_paths(
        self,
        *,
        user_id: str,
        intent_id: str,
        name: str,
        sha256: str = "",
    ) -> tuple[Path, Path, Path | None]:
        tenant_root = self._tenant_files_root(user_id)
        staging_root = tenant_root / ".staging"
        if tenant_root.is_symlink() or staging_root.is_symlink():
            raise ValueError("managed upload directories cannot be symbolic links")
        part_path = staging_root / f"{intent_id}.part"
        ready_path = staging_root / f"{intent_id}.ready"
        final_path = (
            tenant_root / f"{sha256}{_physical_file_suffix(name)}" if sha256 else None
        )
        return part_path, ready_path, final_path

    def _validate_upload_intent_paths(self, row: dict[str, Any]) -> None:
        expected_part, expected_ready, expected_final = self._upload_intent_paths(
            user_id=str(row["user_id"]),
            intent_id=str(row["id"]),
            name=str(row["name"]),
            sha256=str(row.get("sha256") or ""),
        )
        for actual, expected, label in (
            (Path(str(row["part_path"])), expected_part, "part"),
            (Path(str(row["ready_path"])), expected_ready, "ready"),
        ):
            if actual.resolve(strict=False) != expected.resolve(strict=False):
                raise ValueError(f"upload intent {label} path escaped its managed directory")
        raw_final = str(row.get("final_path") or "").strip()
        if expected_final is not None and (
            not raw_final
            or Path(raw_final).resolve(strict=False) != expected_final.resolve(strict=False)
        ):
            raise ValueError("upload intent final path escaped its managed directory")

    def begin_file_upload(
        self,
        *,
        name: str,
        mime_type: str,
        source_path: Path | None = None,
    ) -> dict[str, Any]:
        """Persist an intent before the first staging byte is written."""

        intent_id = new_id("upload")
        user_id = current_user_id()
        part_path, ready_path, _final_path = self._upload_intent_paths(
            user_id=user_id,
            intent_id=intent_id,
            name=name,
        )
        now = utc_now()
        row = {
            "id": intent_id,
            "user_id": user_id,
            "status": "receiving",
            "name": str(name)[:260],
            "source_path": str(source_path) if source_path else None,
            "part_path": str(part_path),
            "ready_path": str(ready_path),
            "final_path": None,
            "mime_type": str(mime_type)[:120],
            "size": 0,
            "sha256": "",
            "file_id": None,
            "created_file": 0,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "committed_at": None,
        }
        with self.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO file_upload_intents(
                    id, user_id, status, name, source_path, part_path, ready_path,
                    final_path, mime_type, size, sha256, file_id, created_file,
                    error, created_at, updated_at, committed_at
                ) VALUES (
                    :id, :user_id, :status, :name, :source_path, :part_path, :ready_path,
                    :final_path, :mime_type, :size, :sha256, :file_id, :created_file,
                    :error, :created_at, :updated_at, :committed_at
                )
                """,
                row,
            )
        return row

    @staticmethod
    def _decode_upload_intent(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return dict(row)

    def get_file_upload_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                """
                SELECT * FROM file_upload_intents
                WHERE id = ? AND user_id = ?
                """,
                (intent_id, current_user_id()),
            ).fetchone()
        return self._decode_upload_intent(row) if row is not None else None

    def fail_file_upload_intent(self, intent_id: str, error: str) -> dict[str, Any] | None:
        with self.transaction(immediate=True) as conn:
            conn.execute(
                """
                UPDATE file_upload_intents
                SET status = 'failed', error = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'receiving'
                """,
                (str(error)[:4_000], utc_now(), intent_id, current_user_id()),
            )
        return self.get_file_upload_intent(intent_id)

    def _prepare_file_upload_conn(
        self,
        conn: sqlite3.Connection,
        row: dict[str, Any],
        *,
        sha256: str | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        if str(row.get("status")) != "receiving":
            return row
        self._validate_upload_intent_paths(row)
        ready_path = Path(str(row["ready_path"]))
        if not ready_path.is_file():
            raise FileNotFoundError(f"Completed upload staging blob is missing: {ready_path}")
        actual_size = ready_path.stat().st_size
        actual_sha256 = _file_sha256(ready_path)
        if sha256 is not None and str(sha256).casefold() != actual_sha256:
            raise ValueError("staged upload hash changed before it was prepared")
        if size is not None and int(size) != actual_size:
            raise ValueError("staged upload size changed before it was prepared")
        _part, _ready, final_path = self._upload_intent_paths(
            user_id=str(row["user_id"]),
            intent_id=str(row["id"]),
            name=str(row["name"]),
            sha256=actual_sha256,
        )
        assert final_path is not None
        now = utc_now()
        conn.execute(
            """
            UPDATE file_upload_intents
            SET status = 'ready', sha256 = ?, size = ?, final_path = ?,
                error = NULL, updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'receiving'
            """,
            (
                actual_sha256,
                actual_size,
                str(final_path),
                now,
                row["id"],
                row["user_id"],
            ),
        )
        updated = conn.execute(
            "SELECT * FROM file_upload_intents WHERE id = ? AND user_id = ?",
            (row["id"], row["user_id"]),
        ).fetchone()
        if updated is None:
            raise KeyError(str(row["id"]))
        return dict(updated)

    def prepare_file_upload(
        self,
        intent_id: str,
        *,
        sha256: str,
        size: int,
    ) -> dict[str, Any]:
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM file_upload_intents WHERE id = ? AND user_id = ?",
                (intent_id, current_user_id()),
            ).fetchone()
            if row is None:
                raise KeyError(intent_id)
            updated = self._prepare_file_upload_conn(
                conn,
                dict(row),
                sha256=sha256,
                size=size,
            )
        return updated

    def _claim_file_upload_conn(
        self,
        conn: sqlite3.Connection,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        status = str(row.get("status") or "")
        if status in {"claimed", "committed"}:
            existing = conn.execute(
                "SELECT * FROM files WHERE id = ? AND user_id = ?",
                (row.get("file_id"), row["user_id"]),
            ).fetchone()
            if existing is None:
                raise KeyError(str(row.get("file_id") or ""))
            return dict(existing)
        if status != "ready":
            raise ValueError(f"upload intent is not ready to claim: {status}")
        self._validate_upload_intent_paths(row)
        user_id = str(row["user_id"])
        sha256 = str(row["sha256"])
        claimed = conn.execute(
            """
            SELECT f.*
            FROM file_ingest_claims claim
            JOIN files f ON f.id = claim.file_id AND f.user_id = claim.user_id
            WHERE claim.user_id = ? AND claim.sha256 = ?
            """,
            (user_id, sha256),
        ).fetchone()
        if claimed is None:
            conn.execute(
                """
                DELETE FROM file_ingest_claims
                WHERE user_id = ? AND sha256 = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM files
                      WHERE files.id = file_ingest_claims.file_id
                        AND files.user_id = file_ingest_claims.user_id
                  )
                """,
                (user_id, sha256),
            )
            claimed = conn.execute(
                """
                SELECT * FROM files
                WHERE sha256 = ? AND user_id = ?
                ORDER BY
                    CASE WHEN status = 'indexed' THEN 0 ELSE 1 END,
                    CASE WHEN mime_type = 'application/octet-stream' THEN 1 ELSE 0 END,
                    created_at ASC, rowid ASC
                LIMIT 1
                """,
                (sha256, user_id),
            ).fetchone()
        created = claimed is None
        if created:
            now = utc_now()
            file_id = new_id("file")
            conn.execute(
                """
                INSERT INTO files(
                    id, name, source_path, stored_path, mime_type, size, sha256,
                    status, error, chunk_count, created_at, updated_at, user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'stored', ?, 0, ?, ?, ?)
                """,
                (
                    file_id,
                    str(row["name"])[:260],
                    row.get("source_path"),
                    str(row["final_path"]),
                    str(row["mime_type"])[:120],
                    int(row["size"]),
                    sha256,
                    "Upload committed; content indexing is pending.",
                    now,
                    now,
                    user_id,
                ),
            )
            claimed = conn.execute(
                "SELECT * FROM files WHERE id = ? AND user_id = ?",
                (file_id, user_id),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO file_ingest_claims(user_id, sha256, file_id, claimed_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, sha256, file_id, now),
            )
        else:
            file_id = str(claimed["id"])
            conn.execute(
                """
                INSERT INTO file_ingest_claims(user_id, sha256, file_id, claimed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, sha256) DO NOTHING
                """,
                (user_id, sha256, file_id, utc_now()),
            )
        conn.execute(
            """
            UPDATE file_upload_intents
            SET status = 'claimed', file_id = ?, created_file = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'ready'
            """,
            (file_id, int(created), utc_now(), row["id"], user_id),
        )
        assert claimed is not None
        return dict(claimed)

    def claim_file_upload(self, intent_id: str) -> dict[str, Any]:
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM file_upload_intents WHERE id = ? AND user_id = ?",
                (intent_id, current_user_id()),
            ).fetchone()
            if row is None:
                raise KeyError(intent_id)
            file_record = self._claim_file_upload_conn(conn, dict(row))
        return file_record

    def _commit_file_upload_blob_conn(
        self,
        conn: sqlite3.Connection,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        status = str(row.get("status") or "")
        if status == "committed":
            file_record = conn.execute(
                "SELECT * FROM files WHERE id = ? AND user_id = ?",
                (row.get("file_id"), row["user_id"]),
            ).fetchone()
            if file_record is None:
                raise KeyError(str(row.get("file_id") or ""))
            return dict(file_record)
        if status != "claimed":
            raise ValueError(f"upload intent is not claimed: {status}")
        self._validate_upload_intent_paths(row)
        user_id = str(row["user_id"])
        sha256 = str(row["sha256"])
        ready_path = Path(str(row["ready_path"]))
        final_path = Path(str(row["final_path"]))
        file_record = conn.execute(
            "SELECT * FROM files WHERE id = ? AND user_id = ?",
            (row["file_id"], user_id),
        ).fetchone()
        if file_record is None:
            raise KeyError(str(row.get("file_id") or ""))
        existing_path = Path(str(file_record["stored_path"]))

        def matches(path: Path) -> bool:
            try:
                return (
                    path.is_file()
                    and path.stat().st_size == int(row["size"])
                    and _file_sha256(path) == sha256
                )
            except OSError:
                return False

        existing_blob_present = existing_path.exists()
        existing_matches = matches(existing_path)
        ready_matches = matches(ready_path)
        final_matches = matches(final_path)
        prefer_incoming_path = (
            str(file_record["status"]) != "indexed"
            and ready_path.is_file()
            and existing_path.resolve(strict=False) != final_path.resolve(strict=False)
        )
        blob_healed = False
        if existing_matches and not prefer_incoming_path:
            active_path = existing_path
            if ready_path.exists():
                if not ready_matches:
                    raise ValueError("recorded ready blob failed its content hash")
                ready_path.unlink()
        else:
            if final_matches:
                active_path = final_path
                if ready_path.exists():
                    if not ready_matches:
                        raise ValueError("recorded ready blob failed its content hash")
                    ready_path.unlink()
            else:
                if not ready_matches:
                    if final_path.exists():
                        raise ValueError(
                            "managed final blob exists with an unexpected content hash"
                        )
                    raise FileNotFoundError("prepared upload blob is missing or corrupt")
                final_path.parent.mkdir(parents=True, exist_ok=True)
                # ``Path.replace`` is an atomic same-volume replacement. A verified
                # reupload therefore heals a corrupt managed blob without a delete gap.
                ready_path.replace(final_path)
                active_path = final_path
                blob_healed = existing_blob_present and not existing_matches
            conn.execute(
                """
                UPDATE files
                SET stored_path = ?, size = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (str(active_path), int(row["size"]), utc_now(), row["file_id"], user_id),
            )
        part_path = Path(str(row["part_path"]))
        if part_path.exists():
            part_path.unlink()
        now = utc_now()
        conn.execute(
            """
            UPDATE file_upload_intents
            SET status = 'committed', error = NULL, updated_at = ?, committed_at = ?
            WHERE id = ? AND user_id = ? AND status = 'claimed'
            """,
            (now, now, row["id"], user_id),
        )
        updated = conn.execute(
            "SELECT * FROM files WHERE id = ? AND user_id = ?",
            (row["file_id"], user_id),
        ).fetchone()
        if updated is None:
            raise KeyError(str(row.get("file_id") or ""))
        result = dict(updated)
        if blob_healed:
            result["_blob_healed"] = True
        return result

    def commit_file_upload(self, intent_id: str) -> dict[str, Any]:
        """Idempotently claim and promote a prepared blob into its canonical record."""

        with self.transaction(immediate=True) as conn:
            raw = conn.execute(
                "SELECT * FROM file_upload_intents WHERE id = ? AND user_id = ?",
                (intent_id, current_user_id()),
            ).fetchone()
            if raw is None:
                raise KeyError(intent_id)
            row = dict(raw)
            if row["status"] == "receiving" and Path(str(row["ready_path"])).is_file():
                row = self._prepare_file_upload_conn(conn, row)
            if row["status"] == "ready":
                self._claim_file_upload_conn(conn, row)
                claimed = conn.execute(
                    "SELECT * FROM file_upload_intents WHERE id = ? AND user_id = ?",
                    (intent_id, current_user_id()),
                ).fetchone()
                if claimed is None:
                    raise KeyError(intent_id)
                row = dict(claimed)
            return self._commit_file_upload_blob_conn(conn, row)

    def _reconcile_upload_intents(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT * FROM file_upload_intents
            WHERE status IN ('receiving', 'ready', 'claimed')
            ORDER BY created_at ASC
            LIMIT 1000
            """
        ).fetchall()
        stale_before = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        for raw in rows:
            row = dict(raw)
            savepoint = f"upload_recovery_{re.sub(r'[^a-zA-Z0-9_]', '_', str(row['id']))}"
            conn.execute(f'SAVEPOINT "{savepoint}"')
            try:
                self._validate_upload_intent_paths(row)
                if row["status"] == "receiving":
                    ready_path = Path(str(row["ready_path"]))
                    part_path = Path(str(row["part_path"]))
                    if ready_path.is_file():
                        row = self._prepare_file_upload_conn(conn, row)
                    elif str(row["updated_at"]) <= stale_before:
                        if part_path.exists():
                            part_path.unlink()
                        conn.execute(
                            """
                            UPDATE file_upload_intents
                            SET status = 'failed', error = ?, updated_at = ?
                            WHERE id = ? AND user_id = ? AND status = 'receiving'
                            """,
                            (
                                "Interrupted before the upload completion marker was written.",
                                utc_now(),
                                row["id"],
                                row["user_id"],
                            ),
                        )
                        conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')
                        continue
                    else:
                        conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')
                        continue
                if row["status"] == "ready":
                    self._claim_file_upload_conn(conn, row)
                    claimed = conn.execute(
                        "SELECT * FROM file_upload_intents WHERE id = ? AND user_id = ?",
                        (row["id"], row["user_id"]),
                    ).fetchone()
                    if claimed is None:
                        raise KeyError(str(row["id"]))
                    row = dict(claimed)
                self._commit_file_upload_blob_conn(conn, row)
                conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')
            except Exception as exc:  # noqa: BLE001 - preserve the intent for a later retry
                conn.execute(f'ROLLBACK TO SAVEPOINT "{savepoint}"')
                conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')
                conn.execute(
                    """
                    UPDATE file_upload_intents
                    SET error = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        f"Upload recovery pending ({type(exc).__name__}): {exc}"[:4_000],
                        utc_now(),
                        row["id"],
                        row["user_id"],
                    ),
                )

    @staticmethod
    def _reconcile_file_ocr_jobs(conn: sqlite3.Connection) -> None:
        now = utc_now()
        conn.execute(
            """
            UPDATE file_ocr_jobs
            SET status = CASE WHEN attempt_count >= max_attempts THEN 'failed' ELSE 'retry' END,
                available_at = ?, lease_token = NULL, lease_owner = NULL,
                lease_expires_at = NULL,
                last_error = COALESCE(last_error, 'OCR worker lease expired before completion.'),
                updated_at = ?
            WHERE status = 'leased' AND lease_expires_at <= ?
            """,
            (now, now, now),
        )
        candidates = conn.execute(
            """
            SELECT f.id, f.user_id, f.name, f.mime_type, f.sha256,
                   f.status, f.chunk_count
            FROM files f
            WHERE NOT EXISTS (
                  SELECT 1 FROM file_ocr_jobs job
                  WHERE job.file_id = f.id AND job.user_id = f.user_id
              )
            ORDER BY f.created_at ASC, f.rowid ASC
            LIMIT 1000
            """
        ).fetchall()
        for file_record in candidates:
            suffix = Path(str(file_record["name"] or "")).suffix.casefold()
            mime_type = str(file_record["mime_type"] or "").casefold()
            is_image = mime_type.startswith("image/") or suffix in _AUTOMATIC_OCR_IMAGE_SUFFIXES
            is_pdf = mime_type == "application/pdf" or suffix == ".pdf"
            if not is_image and not is_pdf:
                continue
            if (
                is_image
                and str(file_record["status"]) == "indexed"
                and int(file_record["chunk_count"]) > 0
            ):
                continue
            conn.execute(
                """
                INSERT INTO file_ocr_jobs(
                    id, user_id, file_id, status, reason, source_sha256,
                    attempt_count, max_attempts, available_at, result_metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, 0, 4, ?, '{}', ?, ?)
                ON CONFLICT(file_id) DO NOTHING
                """,
                (
                    new_id("ocrjob"),
                    file_record["user_id"],
                    file_record["id"],
                    "image_upload_recovered" if is_image else "pdf_completeness_recovered",
                    file_record["sha256"],
                    now,
                    now,
                    now,
                ),
            )

    @staticmethod
    def _reconcile_file_index_states(conn: sqlite3.Connection) -> None:
        """Fail closed after an interrupted file-index transaction.

        ``indexing`` is deliberately non-searchable, transient state.  A process
        restart cannot prove that any chunks left by an older implementation form
        a complete document, so discard those derived rows while retaining the
        authoritative uploaded file and an actionable failure reason.  Conversely,
        a completed ``indexed`` row with committed chunks is trustworthy; repair
        only its denormalized count.
        """

        interrupted = conn.execute("SELECT id FROM files WHERE status = 'indexing'").fetchall()
        interrupted_ids = [str(row["id"]) for row in interrupted]
        if interrupted_ids:
            placeholders = ",".join("?" for _item in interrupted_ids)
            conn.execute(
                f"DELETE FROM file_chunks WHERE file_id IN ({placeholders})",
                interrupted_ids,
            )
            conn.execute(
                f"DELETE FROM file_index_metadata WHERE file_id IN ({placeholders})",
                interrupted_ids,
            )
            conn.execute(
                f"""
                UPDATE files
                SET status = 'failed',
                    error = 'Indexing was interrupted before durable completion; retry the upload.',
                    chunk_count = 0,
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (utc_now(), *interrupted_ids),
            )
            LOGGER.warning(
                "Recovered %s interrupted file indexing operation(s)",
                len(interrupted_ids),
            )

        conn.execute(
            """
            UPDATE files
            SET chunk_count = (
                    SELECT COUNT(*) FROM file_chunks c
                    WHERE c.file_id = files.id AND c.user_id = files.user_id
                ),
                updated_at = ?
            WHERE status = 'indexed'
              AND chunk_count != (
                    SELECT COUNT(*) FROM file_chunks c
                    WHERE c.file_id = files.id AND c.user_id = files.user_id
                )
              AND EXISTS (
                    SELECT 1 FROM file_chunks c
                    WHERE c.file_id = files.id AND c.user_id = files.user_id
                )
            """,
            (utc_now(),),
        )
        conn.execute(
            """
            UPDATE files
            SET status = 'failed',
                error = 'Indexed metadata had no durable chunks; retry the upload.',
                chunk_count = 0,
                updated_at = ?
            WHERE status = 'indexed'
              AND NOT EXISTS (
                    SELECT 1 FROM file_chunks c
                    WHERE c.file_id = files.id AND c.user_id = files.user_id
                )
            """,
            (utc_now(),),
        )
        conn.execute(
            """
            DELETE FROM file_index_metadata
            WHERE file_id IN (
                SELECT f.id
                FROM files f
                WHERE f.status = 'failed'
                  AND f.error = 'Indexed metadata had no durable chunks; retry the upload.'
            )
            """
        )

    def _ensure_messages_fts(self, conn: sqlite3.Connection) -> bool:
        schema_version = "messages-trigram-v2"
        trigger_names = (
            "messages_fts_after_insert",
            "messages_fts_after_update",
            "messages_fts_after_delete",
        )
        try:
            table_exists = _sqlite_table_exists(conn, "messages_fts")
            columns = (
                _sqlite_table_columns(conn, "messages_fts")
                if table_exists
                else set()
            )
            expected_columns = {"id", "user_id", "conversation_id", "role", "content"}
            schema_matches = bool(table_exists) and (
                columns == expected_columns
                and _sqlite_fts_tokenizer(conn, "messages_fts") == "trigram"
            )
            marker = conn.execute(
                "SELECT schema_version FROM fts_index_metadata WHERE name = ?",
                ("messages",),
            ).fetchone()
            needs_rebuild = not schema_matches or marker is None or marker[0] != schema_version
            if needs_rebuild:
                for trigger_name in trigger_names:
                    conn.execute(f'DROP TRIGGER IF EXISTS "{trigger_name}"')
            if table_exists and not schema_matches:
                conn.execute("DROP TABLE messages_fts")
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(
                    id UNINDEXED,
                    user_id UNINDEXED,
                    conversation_id UNINDEXED,
                    role UNINDEXED,
                    content,
                    tokenize='trigram'
                )
                """
            )
            if needs_rebuild:
                # The enclosing initialize transaction makes this rebuild atomic: a
                # crash restores the previous complete index rather than a partial one.
                conn.execute("DELETE FROM messages_fts")
                conn.execute(
                    """
                    INSERT INTO messages_fts(id, user_id, conversation_id, role, content)
                    SELECT id, user_id, conversation_id, role, content
                    FROM messages
                    WHERE is_deleted = 0
                    """
                )
                conn.execute(
                    """
                    INSERT INTO fts_index_metadata(name, schema_version, rebuilt_at)
                    VALUES ('messages', ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        schema_version = excluded.schema_version,
                        rebuilt_at = excluded.rebuilt_at
                    """,
                    (schema_version, utc_now()),
                )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_fts_after_insert
                AFTER INSERT ON messages
                WHEN NEW.is_deleted = 0
                BEGIN
                    INSERT INTO messages_fts(id, user_id, conversation_id, role, content)
                    VALUES (NEW.id, NEW.user_id, NEW.conversation_id, NEW.role, NEW.content);
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_fts_after_update
                AFTER UPDATE OF user_id, conversation_id, role, content, is_deleted ON messages
                BEGIN
                    DELETE FROM messages_fts
                    WHERE id = OLD.id AND user_id = OLD.user_id;
                    INSERT INTO messages_fts(id, user_id, conversation_id, role, content)
                    SELECT NEW.id, NEW.user_id, NEW.conversation_id, NEW.role, NEW.content
                    WHERE NEW.is_deleted = 0;
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_fts_after_delete
                AFTER DELETE ON messages
                BEGIN
                    DELETE FROM messages_fts
                    WHERE id = OLD.id AND user_id = OLD.user_id;
                END
                """
            )
            return True
        except sqlite3.OperationalError as exc:
            if _recoverable_fts_error(exc):
                return False
            raise

    def _ensure_memory_fts(self, conn: sqlite3.Connection) -> bool:
        schema_version = "memories-trigram-v2"
        try:
            table_exists = _sqlite_table_exists(conn, "memories_fts")
            columns = (
                _sqlite_table_columns(conn, "memories_fts")
                if table_exists
                else set()
            )
            expected_columns = {"id", "user_id", "namespace", "content", "tags"}
            schema_matches = bool(table_exists) and (
                columns == expected_columns
                and _sqlite_fts_tokenizer(conn, "memories_fts") == "trigram"
            )
            marker = conn.execute(
                "SELECT schema_version FROM fts_index_metadata WHERE name = ?",
                ("memories",),
            ).fetchone()
            needs_rebuild = not schema_matches or marker is None or marker[0] != schema_version
            if table_exists and not schema_matches:
                conn.execute("DROP TABLE memories_fts")
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
                USING fts5(
                    id UNINDEXED,
                    user_id UNINDEXED,
                    namespace,
                    content,
                    tags,
                    tokenize='trigram'
                )
                """
            )
            if needs_rebuild:
                conn.execute("DELETE FROM memories_fts")
                conn.execute(
                    """
                    INSERT INTO memories_fts(id, user_id, namespace, content, tags)
                    SELECT id, user_id, namespace, content, tags FROM memories
                    """
                )
                conn.execute(
                    """
                    INSERT INTO fts_index_metadata(name, schema_version, rebuilt_at)
                    VALUES ('memories', ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        schema_version = excluded.schema_version,
                        rebuilt_at = excluded.rebuilt_at
                    """,
                    (schema_version, utc_now()),
                )
            return True
        except sqlite3.OperationalError as exc:
            if _recoverable_fts_error(exc):
                return False
            raise

    def _ensure_file_chunks_fts(self, conn: sqlite3.Connection) -> bool:
        schema_version = "file-chunks-trigram-v2"
        try:
            table_exists = _sqlite_table_exists(conn, "file_chunks_fts")
            columns = (
                _sqlite_table_columns(conn, "file_chunks_fts")
                if table_exists
                else set()
            )
            expected_columns = {"file_id", "chunk_id", "user_id", "content"}
            schema_matches = bool(table_exists) and (
                columns == expected_columns
                and _sqlite_fts_tokenizer(conn, "file_chunks_fts") == "trigram"
            )
            marker = conn.execute(
                "SELECT schema_version FROM fts_index_metadata WHERE name = ?",
                ("file_chunks",),
            ).fetchone()
            needs_rebuild = not schema_matches or marker is None or marker[0] != schema_version
            if table_exists and not schema_matches:
                conn.execute("DROP TABLE file_chunks_fts")
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks_fts
                USING fts5(
                    file_id UNINDEXED,
                    chunk_id UNINDEXED,
                    user_id UNINDEXED,
                    content,
                    tokenize='trigram'
                )
                """
            )
            if needs_rebuild:
                conn.execute("DELETE FROM file_chunks_fts")
                conn.execute(
                    """
                    INSERT INTO file_chunks_fts(file_id, chunk_id, user_id, content)
                    SELECT file_id, id, user_id, content FROM file_chunks
                    """
                )
                conn.execute(
                    """
                    INSERT INTO fts_index_metadata(name, schema_version, rebuilt_at)
                    VALUES ('file_chunks', ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        schema_version = excluded.schema_version,
                        rebuilt_at = excluded.rebuilt_at
                    """,
                    (schema_version, utc_now()),
                )
            return True
        except sqlite3.OperationalError as exc:
            if _recoverable_fts_error(exc):
                return False
            raise

    def _load_memories_for_vault(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        owner_id = user_id or current_user_id()
        privileged_clause = (
            ""
            if owner_id == current_user_id() and self._can_read_privileged_derived()
            else f" AND tags NOT LIKE '%{_PRIVILEGED_DERIVED_TAG}%'"
        )
        rows = conn.execute(
            f"""
            SELECT id, user_id, namespace, content, tags, importance, created_at, updated_at
            FROM memories
            WHERE user_id = ?
              {privileged_clause}
            ORDER BY updated_at DESC
            """,
            (owner_id,),
        ).fetchall()
        return [{**dict(row), "tags": _loads(row["tags"], [])} for row in rows]

    def _sync_memory_vault(self, conn: sqlite3.Connection, *, user_id: str | None = None) -> None:
        owner_id = user_id or current_user_id()
        self._memory_vault_for(owner_id).sync(
            self._load_memories_for_vault(conn, user_id=owner_id)
        )

    def _memory_vault_repair_pending(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT value FROM runtime_kv WHERE key = ?",
            (_normalized_runtime_key(_MEMORY_VAULT_REPAIR_KEY),),
        ).fetchone()
        if row is None:
            return False
        value = _loads(row["value"], False)
        if isinstance(value, dict):
            return bool(value.get("required"))
        return bool(value)

    def _mark_memory_vault_repair_required(
        self, conn: sqlite3.Connection, exc: BaseException
    ) -> None:
        try:
            if conn.in_transaction:
                conn.rollback()
            now = utc_now()
            value = {
                "required": True,
                "failed_at": now,
                "error_type": type(exc).__name__,
            }
            conn.execute(
                """
                INSERT INTO runtime_kv(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (_normalized_runtime_key(_MEMORY_VAULT_REPAIR_KEY), _json(value), now),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            LOGGER.exception("Failed to persist memory-vault repair marker")

    def _clear_memory_vault_repair_marker(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                "DELETE FROM runtime_kv WHERE key = ?",
                (_normalized_runtime_key(_MEMORY_VAULT_REPAIR_KEY),),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            LOGGER.exception("Failed to clear memory-vault repair marker")

    def _sync_memory_vault_after_commit(
        self,
        conn: sqlite3.Connection,
        *,
        upserts: Iterable[dict[str, Any]] = (),
        removed_ids: Iterable[str] = (),
        force_full: bool = False,
        raise_on_error: bool = False,
    ) -> bool:
        """Update the derived Markdown mirror only after SQLite is durable.

        A previous mirror failure switches the next write to a full rebuild. The
        marker itself lives in SQLite, so a process restart cannot lose the need
        for repair. Filesystem errors never leave an implicit DB transaction open.
        """

        if conn.in_transaction:
            raise RuntimeError("memory-vault sync requires a committed DB transaction")
        repair_pending = self._memory_vault_repair_pending(conn)
        try:
            if force_full or repair_pending:
                self._sync_memory_vault(conn)
            else:
                vault = self._memory_vault_for()
                for memory in upserts:
                    vault.upsert_memory(memory)
                for memory_id in removed_ids:
                    vault.remove_memory(str(memory_id))
        except Exception as exc:
            self._mark_memory_vault_repair_required(conn, exc)
            LOGGER.warning(
                "Memory-vault mirror update failed; full repair is pending",
                exc_info=True,
            )
            if raise_on_error:
                raise
            return False
        if repair_pending:
            self._clear_memory_vault_repair_marker(conn)
        return True

    def ping(self) -> bool:
        with self._lock:
            self.connect().execute("SELECT 1").fetchone()
        return True

    def backup_database(self, target_dir: str | Path | None = None) -> dict[str, Any]:
        """Create a consistent SQLite backup using the SQLite backup API."""

        self._require_system_scope("database backup")
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
            actor = current_actor()
            result: dict[str, int] = {}
            for table in tables:
                if table == "runtime_kv":
                    if actor.user_id == LEGACY_OWNER_USER_ID:
                        row = conn.execute(
                            "SELECT COUNT(*) AS c FROM runtime_kv WHERE key NOT LIKE 'user.%'"
                        ).fetchone()
                    else:
                        row = conn.execute(
                            """
                            SELECT COUNT(*) AS c
                            FROM runtime_kv
                            WHERE key LIKE ? ESCAPE '\\'
                            """,
                            (_sql_like_prefix(f"user.{actor.user_id}."),),
                        ).fetchone()
                elif "user_id" in _sqlite_table_columns(conn, table):
                    row = conn.execute(
                        f"SELECT COUNT(*) AS c FROM {table} WHERE user_id = ?",
                        (actor.user_id,),
                    ).fetchone()
                else:
                    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                result[table] = int(row["c"])
            return result

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
            "user_id": current_user_id(),
        }
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO runtime_events(id, ts, level, kind, title, payload, user_id)
                VALUES (:id, :ts, :level, :kind, :title, :payload, :user_id)
                """,
                {**row, "payload": _json(row["payload"])},
            )
            self.connect().commit()
        return row

    def list_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            actor = current_actor()
            rows = self.connect().execute(
                """
                SELECT id, ts, level, kind, title, payload, user_id
                FROM runtime_events
                WHERE user_id = ?
                ORDER BY ts DESC, rowid DESC
                LIMIT ?
                """,
                (actor.user_id, limit),
            )
        return [
            {**dict(row), "payload": _loads(row["payload"], {})}
            for row in rows.fetchall()
        ]

    def get_runtime_value(self, key: str, default: Any = None) -> Any:
        safe_key = _normalized_runtime_key(key)
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                """
                SELECT value
                FROM runtime_kv
                WHERE key = ?
                """,
                (safe_key,),
            ).fetchone()
            if row is None and len(safe_key) > _LEGACY_RUNTIME_KEY_MAX_CHARS:
                owns_transaction = not conn.in_transaction
                try:
                    if owns_transaction:
                        conn.execute("BEGIN IMMEDIATE")
                    row = _runtime_row_with_legacy_migration(conn, safe_key)
                    if owns_transaction:
                        conn.commit()
                except Exception:  # noqa: BLE001 - migration is transactional
                    if owns_transaction:
                        conn.rollback()
                    raise
        if row is None:
            return default
        return _loads(row["value"], default)

    def set_runtime_value(self, key: str, value: Any) -> dict[str, Any]:
        safe_key = _normalized_runtime_key(key)
        now = utc_now()
        row = {"key": safe_key, "value": value, "updated_at": now}
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                _runtime_row_with_legacy_migration(conn, safe_key)
                conn.execute(
                    """
                    INSERT INTO runtime_kv(key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    (row["key"], _json(row["value"]), row["updated_at"]),
                )
                conn.commit()
            except Exception:  # noqa: BLE001 - key migration and write are atomic
                conn.rollback()
                raise
        return row

    def delete_runtime_value(self, key: str) -> bool:
        safe_key = _normalized_runtime_key(key)
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                _runtime_row_with_legacy_migration(conn, safe_key)
                cursor = conn.execute(
                    "DELETE FROM runtime_kv WHERE key = ?",
                    (safe_key,),
                )
                conn.commit()
            except Exception:  # noqa: BLE001 - migration and deletion are atomic
                conn.rollback()
                raise
        return cursor.rowcount > 0

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

        safe_key = _normalized_runtime_key(key)
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = _runtime_row_with_legacy_migration(conn, safe_key)
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

    def update_runtime_value_atomic_pruned(
        self,
        key: str,
        updater: Callable[[Any], Any],
        *,
        default: Any = None,
        prune_prefixes: Sequence[str],
        older_than: str,
        hard_cap: int,
    ) -> Any:
        """Atomically prune a scoped key family and update one member.

        Per-transport request fences need one independent row so ordinary chat
        volume cannot inflate and rewrite a single JSON aggregate.  Pruning and
        the emergency cap check share the same SQLite write transaction as the
        claim, so concurrent workers cannot both step past the cap.
        """

        safe_key = _normalized_runtime_key(key)
        safe_prefixes = tuple(
            dict.fromkeys(_normalized_runtime_prefix(prefix) for prefix in prune_prefixes)
        )
        if not safe_prefixes:
            raise ValueError("at least one prune prefix is required")
        prefix_patterns = tuple(_sql_like_prefix(prefix) for prefix in safe_prefixes)
        bounded_cap = max(1, int(hard_cap))
        with self._lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for pattern in prefix_patterns:
                    conn.execute(
                        """
                        DELETE FROM runtime_kv
                        WHERE key LIKE ? ESCAPE '\\' AND updated_at < ?
                        """,
                        (pattern, older_than),
                    )
                row = _runtime_row_with_legacy_migration(conn, safe_key)
                if row is None:
                    where = " OR ".join(
                        "key LIKE ? ESCAPE '\\'" for _ in prefix_patterns
                    )
                    count = int(
                        conn.execute(
                            f"SELECT COUNT(*) FROM runtime_kv WHERE {where}",  # noqa: S608
                            prefix_patterns,
                        ).fetchone()[0]
                    )
                    if count >= bounded_cap:
                        raise RuntimeError("scoped runtime key family hard cap reached")
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
            except Exception:  # noqa: BLE001 - the complete claim must roll back
                conn.rollback()
                raise
        return updated

    def list_runtime_values(self, prefix: str | None = None) -> list[dict[str, Any]]:
        actor = current_actor()
        legacy_namespace = actor.user_id == LEGACY_OWNER_USER_ID
        scoped_prefix = (
            _normalized_runtime_prefix(prefix or "")
            if (prefix or not legacy_namespace)
            else None
        )
        with self._lock:
            if scoped_prefix is not None:
                rows = self.connect().execute(
                    """
                    SELECT key, value, updated_at
                    FROM runtime_kv
                    WHERE key LIKE ? ESCAPE '\\'
                    ORDER BY updated_at DESC
                    """,
                    (_sql_like_prefix(scoped_prefix),),
                ).fetchall()
            elif legacy_namespace:
                rows = self.connect().execute(
                    """
                    SELECT key, value, updated_at
                    FROM runtime_kv
                    WHERE key NOT LIKE 'user.%'
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
            else:
                raise AssertionError("non-legacy runtime namespace must be scoped")
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
        user_id = current_user_id()
        with self._lock:
            self.connect().execute(
                """
                INSERT INTO conversations(id, title, created_at, updated_at, user_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (cid, title[:200], now, now, user_id),
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
        user_id = current_user_id()
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO conversations(
                        id, title, created_at, updated_at, user_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (conversation_id, title[:200], now, now, user_id),
                )
                self._require_owned_resource(
                    conn,
                    table="conversations",
                    resource_id=conversation_id,
                    user_id=user_id,
                )
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
        return conversation_id

    def add_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        reply_to_message_id: str | None = None,
    ) -> str:
        mid = new_id("msg")
        now = utc_now()
        user_id = current_user_id()
        effective_metadata = dict(metadata or {})
        request_metadata = _CHAT_REQUEST_METADATA.get()
        if request_metadata is not None and role in {"user", "assistant"}:
            effective_metadata.update(request_metadata)
            if role == "assistant":
                effective_metadata["chat_request_terminal"] = True
        effective_reply_to = reply_to_message_id
        if role == "assistant" and not effective_reply_to:
            effective_reply_to = _CHAT_INGRESS_MESSAGE_ID.get()
        privileged_derived = bool(
            role == "assistant" and effective_metadata.get("privileged_derived") is True
        )
        preview = (
            "[owner/admin result]"
            if privileged_derived
            else " ".join(content.split())[:120]
        )
        with self._lock:
            conn = self.connect()
            self._require_owned_resource(
                conn,
                table="conversations",
                resource_id=conversation_id,
                user_id=user_id,
            )
            ingress_row = None
            if role == "assistant" and effective_reply_to:
                ingress_row = conn.execute(
                    """
                    SELECT id, metadata
                    FROM messages
                    WHERE id = ? AND conversation_id = ? AND user_id = ? AND role = 'user'
                    """,
                    (effective_reply_to, conversation_id, user_id),
                ).fetchone()
                if ingress_row is None:
                    effective_reply_to = None
            conn.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, metadata, created_at,
                    user_id, reply_to_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mid, conversation_id, role, content, _json(effective_metadata), now,
                 user_id, effective_reply_to),
            )
            if ingress_row is not None:
                ingress_metadata = _loads(ingress_row["metadata"], {})
                if not isinstance(ingress_metadata, dict):
                    ingress_metadata = {}
                if ingress_metadata.get("ingress_status") == "accepted":
                    ingress_metadata.update(
                        {
                            "ingress_status": "processed",
                            "ingress_processed_at": now,
                            "ingress_terminal_message_id": mid,
                        }
                    )
                    conn.execute(
                        "UPDATE messages SET metadata = ? WHERE id = ? AND user_id = ?",
                        (_json(ingress_metadata), ingress_row["id"], user_id),
                    )
            conn.execute(
                """UPDATE conversations
                   SET updated_at = ?, last_message = ?, last_message_at = ?
                   WHERE id = ? AND user_id = ?""",
                (now, preview, now, conversation_id, user_id),
            )
            self._insert_learning_observation(
                conn,
                kind="conversation.message",
                source_id=mid,
                conversation_id=conversation_id,
                role=role,
                content="" if privileged_derived else content,
                summary=(
                    "Privileged assistant content omitted from learning history"
                    if privileged_derived
                    else f"{role} message captured for learning"
                ),
                payload=(
                    {
                        "privileged_derived": True,
                        "content_omitted": True,
                        "required_presets": ["owner", "admin"],
                    }
                    if privileged_derived
                    else {"metadata": effective_metadata}
                ),
                ts=now,
            )
            conn.commit()
        return mid

    def find_chat_request_message(
        self,
        *,
        conversation_id: str,
        request_hash: str,
        role: str,
        legacy_request_fingerprint: str | None = None,
        terminal_only: bool = False,
    ) -> dict[str, Any] | None:
        """Recover a request-bound message after a crash between DB commits.

        The durable request ledger is written before chat history.  If the process
        dies immediately after ``add_message`` commits, the next lease holder can
        recover that exact row instead of appending a duplicate logical turn.
        """

        user_id = current_user_id()
        visibility_clause = (
            ""
            if self._can_read_privileged_derived()
            else " AND COALESCE(json_extract(metadata, '$.privileged_derived'), 0) != 1"
        )
        with self._lock:
            conn = self.connect()
            self._require_owned_resource(
                conn,
                table="conversations",
                resource_id=conversation_id,
                user_id=user_id,
            )
            row = conn.execute(
                f"""
                SELECT id, conversation_id, role, content, metadata, created_at, user_id
                FROM messages
                WHERE conversation_id = ? AND user_id = ? AND role = ?
                  AND (
                      ? = 0
                      OR json_extract(metadata, '$.chat_request_terminal') = 1
                  )
                  AND (
                      json_extract(metadata, '$.chat_request_hash') = ?
                      OR (
                          ? <> ''
                          AND json_extract(metadata, '$.guest_request_fingerprint') = ?
                      )
                  )
                  {visibility_clause}
                ORDER BY created_at ASC, rowid ASC
                LIMIT 1
                """,
                (
                    conversation_id,
                    user_id,
                    role,
                    int(terminal_only),
                    request_hash,
                    str(legacy_request_fingerprint or ""),
                    str(legacy_request_fingerprint or ""),
                ),
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["metadata"] = _loads(item.get("metadata"), {})
        return item

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT
                    c.id, c.title, c.created_at, c.updated_at,
                    c.last_message, c.last_message_at,
                    c.unread_count, c.is_pinned, c.is_archived,
                    COUNT(m.id) AS message_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id AND m.user_id = c.user_id
                    AND m.is_deleted = 0
                WHERE c.user_id = ?
                GROUP BY c.id
                ORDER BY c.is_pinned DESC, c.updated_at DESC, c.rowid DESC
                LIMIT ?
                """,
                (current_user_id(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_dialogue_across_conversations(
        self,
        *,
        hours: int = 24,
        max_conversations: int = 10,
        messages_per_conversation: int = 6,
        exclude_conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recent user/assistant turns across the operator's chats (post-reboot recall).

        Returns newest-first snippets with conversation title so the agent can
        answer «о чём мы говорили пару часов назад» without only seeing the
        brand-new empty session after a service restart.
        """

        hours = max(1, min(int(hours or 24), 168))
        max_conversations = max(1, min(int(max_conversations or 10), 30))
        messages_per_conversation = max(1, min(int(messages_per_conversation or 6), 20))
        # ISO cutoff matches storage.utc_now() strings better than SQLite datetime().
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        visibility_clause = (
            ""
            if self._can_read_privileged_derived()
            else " AND COALESCE(json_extract(metadata, '$.privileged_derived'), 0) != 1"
        )
        with self._lock:
            conn = self.connect()
            conv_rows = conn.execute(
                """
                SELECT id, title, updated_at
                FROM conversations
                WHERE user_id = ?
                  AND updated_at >= ?
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
                """,
                (current_user_id(), cutoff, max_conversations + 2),
            ).fetchall()
            snippets: list[dict[str, Any]] = []
            for conv in conv_rows:
                conv_id = str(conv["id"] or "")
                if not conv_id or conv_id == exclude_conversation_id:
                    continue
                msg_rows = conn.execute(
                    f"""
                    SELECT role, content, created_at
                    FROM messages
                    WHERE conversation_id = ? AND user_id = ?
                      AND role IN ('user', 'assistant')
                      {visibility_clause}
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (conv_id, current_user_id(), messages_per_conversation),
                ).fetchall()
                for row in reversed(list(msg_rows)):
                    content = str(row["content"] or "").strip()
                    if not content:
                        continue
                    snippets.append(
                        {
                            "conversation_id": conv_id,
                            "title": str(conv["title"] or "")[:120],
                            "role": str(row["role"] or ""),
                            "content": content[:500],
                            "created_at": str(row["created_at"] or ""),
                            "updated_at": str(conv["updated_at"] or ""),
                        }
                    )
                if len({item["conversation_id"] for item in snippets}) >= max_conversations:
                    break
        return snippets

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
                LEFT JOIN messages m ON m.conversation_id = c.id AND m.user_id = c.user_id
                WHERE c.id = ? AND c.user_id = ?
                GROUP BY c.id
                """,
                (conversation_id, current_user_id()),
            ).fetchone()
        return dict(row) if row else None

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        visibility_clause = (
            ""
            if self._can_read_privileged_derived()
            else " AND COALESCE(json_extract(metadata, '$.privileged_derived'), 0) != 1"
        )
        with self._lock:
            row = self.connect().execute(
                f"""
                SELECT id, conversation_id, role, content, metadata, created_at
                FROM messages
                WHERE id = ? AND user_id = ?
                  {visibility_clause}
                """,
                (message_id, current_user_id()),
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
                "UPDATE messages SET metadata = ? WHERE id = ? AND user_id = ?",
                (_json(metadata), message_id, current_user_id()),
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

    def merge_message_metadata(
        self,
        message_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Merge trusted runtime metadata into one current-tenant message."""

        user_id = current_user_id()
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                "SELECT metadata FROM messages WHERE id = ? AND user_id = ?",
                (message_id, user_id),
            ).fetchone()
            if row is None:
                return None
            metadata = _loads(row["metadata"], {})
            if not isinstance(metadata, dict):
                metadata = {}
            metadata.update(dict(updates))
            conn.execute(
                "UPDATE messages SET metadata = ? WHERE id = ? AND user_id = ?",
                (_json(metadata), message_id, user_id),
            )
            conn.commit()
        return self.get_message(message_id)

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            conn = self.connect()
            existing = conn.execute(
                "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, current_user_id()),
            ).fetchone()
            if existing is None:
                return False
            message_count = conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ? AND user_id = ?",
                (conversation_id, current_user_id()),
            ).fetchone()["c"]
            conn.execute(
                "DELETE FROM messages WHERE conversation_id = ? AND user_id = ?",
                (conversation_id, current_user_id()),
            )
            conn.execute(
                "DELETE FROM conversations WHERE id = ? AND user_id = ?",
                (conversation_id, current_user_id()),
            )
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
        visibility_clause = (
            ""
            if self._can_read_privileged_derived()
            else " AND COALESCE(json_extract(metadata, '$.privileged_derived'), 0) != 1"
        )
        with self._lock:
            rows = self.connect().execute(
                f"""
                SELECT id, conversation_id, role, content, metadata, created_at
                FROM messages
                WHERE conversation_id = ? AND user_id = ?
                  {visibility_clause}
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
                """,
                (conversation_id, current_user_id(), limit),
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
        visibility_clause = (
            ""
            if self._can_read_privileged_derived()
            else " AND COALESCE(json_extract(metadata, '$.privileged_derived'), 0) != 1"
        )
        with self._lock:
            rows = self.connect().execute(
                f"""
                SELECT id, conversation_id, role, content, metadata, created_at
                FROM messages
                WHERE conversation_id = ? AND user_id = ?
                  {visibility_clause}
                ORDER BY created_at ASC, rowid ASC
                LIMIT ? OFFSET ?
                """,
                (conversation_id, current_user_id(), max(1, limit), max(0, offset)),
            ).fetchall()
        return [
            {**dict(row), "metadata": _loads(row["metadata"], {})}
            for row in rows
        ]

    def recent_messages(self, conversation_id: str, limit: int = 20) -> list[dict[str, Any]]:
        visibility_clause = (
            ""
            if self._can_read_privileged_derived()
            else " AND COALESCE(json_extract(metadata, '$.privileged_derived'), 0) != 1"
        )
        with self._lock:
            rows = self.connect().execute(
                f"""
                SELECT id, role, content, metadata, created_at
                FROM messages
                WHERE conversation_id = ? AND user_id = ?
                  {visibility_clause}
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (conversation_id, current_user_id(), limit),
            ).fetchall()
        return [
            {**dict(row), "metadata": _loads(row["metadata"], {})}
            for row in reversed(rows)
        ]

    def edit_message(self, message_id: str, content: str) -> dict[str, Any] | None:
        now = utc_now()
        user_id = current_user_id()
        preview = " ".join(content.split())[:120]
        with self._lock:
            conn = self.connect()
            self._require_owned_resource(
                conn,
                table="messages",
                resource_id=message_id,
                user_id=user_id,
            )
            cursor = conn.execute(
                """UPDATE messages SET content = ?, edited_at = ?
                   WHERE id = ? AND user_id = ? AND is_deleted = 0""",
                (content, now, message_id, user_id),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT conversation_id FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE conversations SET last_message = ?, last_message_at = ?
                       WHERE id = ? AND user_id = ?""",
                    (preview, now, row["conversation_id"], user_id),
                )
            conn.commit()
            return self.get_message(message_id)

    def delete_message(self, message_id: str) -> bool:
        user_id = current_user_id()
        with self._lock:
            conn = self.connect()
            self._require_owned_resource(
                conn,
                table="messages",
                resource_id=message_id,
                user_id=user_id,
            )
            cursor = conn.execute(
                """UPDATE messages SET is_deleted = 1
                   WHERE id = ? AND user_id = ? AND is_deleted = 0""",
                (message_id, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_conversation(
        self, conversation_id: str, *, is_pinned: bool | None = None,
        is_archived: bool | None = None, unread_count: int | None = None,
        title: str | None = None,
    ) -> dict[str, Any] | None:
        user_id = current_user_id()
        updates: list[str] = []
        params: list[Any] = []
        if is_pinned is not None:
            updates.append("is_pinned = ?")
            params.append(1 if is_pinned else 0)
        if is_archived is not None:
            updates.append("is_archived = ?")
            params.append(1 if is_archived else 0)
        if unread_count is not None:
            updates.append("unread_count = ?")
            params.append(max(0, unread_count))
        if title is not None and title.strip():
            updates.append("title = ?")
            params.append(title.strip()[:200])
        if not updates:
            return self.get_conversation(conversation_id)
        params.extend([conversation_id, user_id])
        with self._lock:
            conn = self.connect()
            conn.execute(
                f"""UPDATE conversations SET {', '.join(updates)}
                   WHERE id = ? AND user_id = ?""",
                tuple(params),
            )
            conn.commit()
            return self.get_conversation(conversation_id)

    def search_messages(
        self,
        query: str,
        limit: int = 25,
        *,
        roles: Iterable[str] | None = None,
        conversation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search the complete current tenant's user/assistant message history.

        Trigram FTS handles arbitrary Unicode scripts and old rows without a recency
        window. SQLite FTS cannot index one- or two-codepoint tokens, so literal
        normalized ``instr`` predicates provide a Unicode-casefold fallback and are
        merged with FTS results for mixed short/long queries.
        """

        clean_query = str(query or "").strip()
        requested_limit = max(0, int(limit))
        if not clean_query or requested_limit == 0:
            return []

        requested_roles = (roles,) if isinstance(roles, str) else roles
        normalized_roles: list[str] = []
        for role in requested_roles or ("user", "assistant"):
            normalized = str(role).strip().casefold()
            if normalized in {"user", "assistant"} and normalized not in normalized_roles:
                normalized_roles.append(normalized)
        if not normalized_roles:
            return []

        user_id = current_user_id()
        visibility_clause = (
            ""
            if self._can_read_privileged_derived()
            else " AND COALESCE(json_extract(m.metadata, '$.privileged_derived'), 0) != 1"
        )
        role_placeholders = ",".join("?" for _ in normalized_roles)
        seen_ids: set[str] = set()
        results: list[dict[str, Any]] = []

        def add_rows(rows: Iterable[sqlite3.Row]) -> None:
            for row in rows:
                item = dict(row)
                message_id = str(item.get("id") or "")
                if not message_id or message_id in seen_ids:
                    continue
                seen_ids.add(message_id)
                results.append(_decorate_message_hit(item, clean_query))

        terms = _query_terms(clean_query, limit=8)
        folded_terms = [_unicode_search_fold(term) for term in terms]
        fts_terms = [term for term in terms if len(_unicode_search_fold(term)) >= 3]
        fts_returned = False
        if self._message_fts_available and fts_terms:
            match = " OR ".join(f'"{term}"' for term in fts_terms)
            conversation_clause = ""
            params: list[Any] = [match, user_id, user_id, *normalized_roles]
            if conversation_id is not None:
                conversation_clause = " AND m.conversation_id = ?"
                params.append(str(conversation_id))
            params.append(min(500, max(requested_limit * 4, requested_limit)))
            try:
                with self._lock:
                    rows = self.connect().execute(
                        f"""
                        SELECT
                            m.id,
                            m.conversation_id,
                            m.role,
                            m.content,
                            m.metadata,
                            m.created_at,
                            m.edited_at,
                            bm25(messages_fts) AS rank
                        FROM messages_fts
                        JOIN messages m
                          ON m.id = messages_fts.id
                         AND m.user_id = messages_fts.user_id
                        WHERE messages_fts MATCH ?
                          AND messages_fts.user_id = ?
                          AND m.user_id = ?
                          AND m.is_deleted = 0
                          AND m.role IN ({role_placeholders})
                          {visibility_clause}
                          {conversation_clause}
                        ORDER BY rank ASC, m.created_at DESC, m.rowid DESC
                        LIMIT ?
                        """,
                        tuple(params),
                    ).fetchall()
                add_rows(rows)
                fts_returned = bool(rows)
            except sqlite3.OperationalError as exc:
                if not _recoverable_fts_error(exc):
                    raise

        literal_terms = [
            folded
            for folded in folded_terms
            if folded and len(folded) < 3
        ]
        if not self._message_fts_available or not fts_returned:
            literal_terms.extend(folded_terms)
            literal_terms.append(_unicode_search_fold(clean_query))
        literal_terms = list(dict.fromkeys(term for term in literal_terms if term))
        if literal_terms:
            literal_clauses = " OR ".join(
                "instr(jarvis_search_fold(m.content), ?) > 0" for _ in literal_terms
            )
            conversation_clause = ""
            params = [user_id, *normalized_roles]
            if conversation_id is not None:
                conversation_clause = " AND m.conversation_id = ?"
                params.append(str(conversation_id))
            params.extend(literal_terms)
            params.append(min(500, max(requested_limit * 8, requested_limit)))
            with self._lock:
                rows = self.connect().execute(
                    f"""
                    SELECT
                        m.id,
                        m.conversation_id,
                        m.role,
                        m.content,
                        m.metadata,
                        m.created_at,
                        m.edited_at,
                        NULL AS rank
                    FROM messages m
                    WHERE m.user_id = ?
                      AND m.is_deleted = 0
                      AND m.role IN ({role_placeholders})
                      {visibility_clause}
                      {conversation_clause}
                      AND ({literal_clauses})
                    ORDER BY m.created_at DESC, m.rowid DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
            add_rows(rows)

        return results[:requested_limit]

    def search_recent_user_messages(self, query: str, limit: int = 15) -> list[dict[str, Any]]:
        """Backward-compatible user-only view over complete message search."""

        return self.search_messages(query, limit=limit, roles=("user",))

    def retire_compacted_raw_messages(self, conversation_id: str) -> int:
        """Mark raw-message memories for a conversation as compacted (no longer fresh).

        Updates tags to ['raw-message', 'compacted'] so they can be excluded from search.
        Returns the number of rows updated.
        """
        with self._lock:
            conn = self.connect()
            cursor = conn.execute(
                """
                UPDATE memories
                SET tags = '["raw-message", "compacted"]', importance = 0.05
                WHERE namespace = 'conversation'
                  AND tags LIKE '%raw-message%'
                  AND tags NOT LIKE '%compacted%'
                  AND content LIKE ?
                  AND user_id = ?
                """,
                (f"%[conversation_id={conversation_id}]%", current_user_id()),
            )
            conn.commit()
            return cursor.rowcount

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
        privileged_derived = _PRIVILEGED_DERIVED_TAG in tags
        importance = max(0.0, min(1.0, float(importance)))
        content_key = _normalize_memory_content(content)
        row = {
            "id": new_id("mem"),
            "user_id": current_user_id(),
            "namespace": namespace,
            "content": content,
            "tags": tags,
            "importance": importance,
            "created_at": now,
            "updated_at": now,
        }
        merged_existing = False
        with self._lock:
            conn = self.connect()
            if conn.in_transaction:
                raise RuntimeError("memory mutation requires a clean storage transaction")
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing_rows = conn.execute(
                    """
                    SELECT id, user_id, namespace, content, tags, importance, created_at, updated_at
                    FROM memories
                    WHERE namespace = ? AND user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT 250
                    """,
                    (namespace, row["user_id"]),
                ).fetchall()
                for existing in existing_rows:
                    existing_dict = dict(existing)
                    if _normalize_memory_content(str(existing_dict["content"])) != content_key:
                        continue
                    merged_tags = _merge_tags(_loads(existing_dict["tags"], []), tags)
                    merged_importance = max(
                        float(existing_dict["importance"] or 0), importance
                    )
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
                        WHERE id = ? AND user_id = ?
                        """,
                        (
                            _json(merged_tags),
                            merged_importance,
                            now,
                            row["id"],
                            row["user_id"],
                        ),
                    )
                    self._replace_memory_fts(conn, row)
                    merged_existing = True
                    break
                if not merged_existing:
                    conn.execute(
                        """
                        INSERT INTO memories(
                            id, user_id, namespace, content, tags, importance,
                            created_at, updated_at
                        )
                        VALUES (
                            :id, :user_id, :namespace, :content, :tags, :importance,
                            :created_at, :updated_at
                        )
                        """,
                        {**row, "tags": _json(row["tags"])},
                    )
                    self._replace_memory_fts(conn, row)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            self._sync_memory_vault_after_commit(conn, upserts=(row,))
        if merged_existing:
            self.record_audit(
                actor="system",
                action="memory.merge",
                target_type="memory",
                target_id=row["id"],
                summary=(
                    "Privileged-derived memory refreshed; content omitted from audit."
                    if privileged_derived
                    else f"Memory refreshed in namespace {row['namespace']}."
                ),
                after=(
                    {
                        "id": row["id"],
                        "namespace": row["namespace"],
                        "privileged_derived": True,
                        "content_omitted": True,
                    }
                    if privileged_derived
                    else row
                ),
            )
        else:
            self.record_audit(
                actor="system",
                action="memory.create",
                target_type="memory",
                target_id=row["id"],
                summary=(
                    "Privileged-derived memory saved; content omitted from audit."
                    if privileged_derived
                    else f"Memory saved in namespace {row['namespace']}."
                ),
                after=(
                    {
                        "id": row["id"],
                        "namespace": row["namespace"],
                        "privileged_derived": True,
                        "content_omitted": True,
                    }
                    if privileged_derived
                    else row
                ),
            )
        return row

    def _replace_memory_fts(self, conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        if not self._memory_fts_available:
            return
        conn.execute(
            "DELETE FROM memories_fts WHERE id = ? AND user_id = ?",
            (row["id"], row["user_id"]),
        )
        conn.execute(
            """
            INSERT INTO memories_fts(id, user_id, namespace, content, tags)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["user_id"],
                row["namespace"],
                row["content"],
                _json(row["tags"]),
            ),
        )

    def search_memory(
        self,
        query: str | None = None,
        limit: int = 25,
        *,
        namespaces: Iterable[str] | None = None,
        since_date: str | None = None,
    ) -> list[dict[str, Any]]:
        namespace_filter = [str(item) for item in (namespaces or []) if str(item).strip()]
        privileged_fts_clause = (
            ""
            if self._can_read_privileged_derived()
            else f" AND m.tags NOT LIKE '%{_PRIVILEGED_DERIVED_TAG}%'"
        )
        privileged_plain_clause = (
            ""
            if self._can_read_privileged_derived()
            else f" AND tags NOT LIKE '%{_PRIVILEGED_DERIVED_TAG}%'"
        )
        seen_ids: set[str] = set()
        decorated: list[dict[str, Any]] = []

        since_clause = ""
        since_params: list[Any] = []
        if since_date:
            since_clause = " AND created_at >= ?"
            since_params = [since_date]

        def add_rows(rows: Iterable[sqlite3.Row]) -> None:
            for row in rows:
                item = dict(row)
                if item["id"] in seen_ids:
                    continue
                seen_ids.add(item["id"])
                decorated.append(_decorate_memory_hit(item, query))

        terms = _query_terms(query, limit=8) if query else []
        folded_terms = [_unicode_search_fold(term) for term in terms]
        fts_terms = [term for term in terms if len(_unicode_search_fold(term)) >= 3]
        fts_returned = False
        if query and self._memory_fts_available and fts_terms:
            match = " OR ".join(f'"{term}"' for term in fts_terms)
            try:
                namespace_sql = ""
                user_id = current_user_id()
                params: list[Any] = [match, user_id, user_id]
                if namespace_filter:
                    placeholders = ",".join("?" for _ in namespace_filter)
                    namespace_sql = f" AND m.namespace IN ({placeholders})"
                    params.extend(namespace_filter)
                oversample = max(limit * 4, limit)
                params.extend(since_params)
                params.append(min(500, oversample))
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
                        JOIN memories m
                          ON m.id = memories_fts.id
                         AND m.user_id = memories_fts.user_id
                        WHERE memories_fts MATCH ?
                          AND memories_fts.user_id = ?
                          AND m.user_id = ?
                          AND m.tags NOT LIKE '%compacted%'
                          {privileged_fts_clause}
                        {namespace_sql}
                        {since_clause}
                        ORDER BY rank ASC, m.importance DESC, m.updated_at DESC
                        LIMIT ?
                        """,
                        tuple(params),
                    ).fetchall()
                add_rows(rows)
                fts_returned = bool(rows)
            except sqlite3.OperationalError as exc:
                if not _recoverable_fts_error(exc):
                    raise

        if query:
            literal_terms = [term for term in folded_terms if term and len(term) < 3]
            if not self._memory_fts_available or not fts_returned:
                literal_terms.extend(folded_terms)
                literal_terms.append(_unicode_search_fold(query))
            literal_terms = list(dict.fromkeys(term for term in literal_terms if term))
            clauses: list[str] = []
            params = [current_user_id()]
            for term in literal_terms:
                clauses.append(
                    "(instr(jarvis_search_fold(content), ?) > 0 "
                    "OR instr(jarvis_search_fold(tags), ?) > 0 "
                    "OR instr(jarvis_search_fold(namespace), ?) > 0)"
                )
                params.extend([term, term, term])
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
                    WHERE user_id = ? AND ({" OR ".join(clauses)}){namespace_sql}
                    {since_clause}
                    AND tags NOT LIKE '%compacted%'
                    {privileged_plain_clause}
                    ORDER BY importance DESC, updated_at DESC
                    LIMIT ?
                """
                params.extend(since_params)
                params.append(min(500, max(limit * 8, limit)))
                with self._lock:
                    rows = self.connect().execute(sql, tuple(params)).fetchall()
                add_rows(rows)

        if decorated:
            decorated.sort(key=_memory_sort_key, reverse=True)
            return decorated[:limit]

        params: list[Any]
        where = "user_id = ?"
        namespace_sql = ""
        if namespace_filter:
            placeholders = ",".join("?" for _ in namespace_filter)
            namespace_sql = f"namespace IN ({placeholders})"
        if query:
            where = f"{where} AND (content LIKE ? OR tags LIKE ? OR namespace LIKE ?)"
            like = f"%{query}%"
            params = [current_user_id(), like, like, like]
        else:
            params = [current_user_id()]
        if namespace_sql:
            where = f"{where} AND {namespace_sql}"
            params.extend(namespace_filter)
        params.extend(since_params)
        params.append(limit)
        sql = f"""
            SELECT id, namespace, content, tags, importance, created_at, updated_at, NULL AS rank
            FROM memories
            WHERE {where}
            {since_clause}
            AND tags NOT LIKE '%compacted%'
            {privileged_plain_clause}
            ORDER BY importance DESC, updated_at DESC
            LIMIT ?
        """
        with self._lock:
            rows = self.connect().execute(sql, tuple(params)).fetchall()
        return [_decorate_memory_hit(dict(row), query) for row in rows]

    def consolidate_memories(self, limit: int = 1000) -> dict[str, int]:
        privileged_clause = (
            ""
            if self._can_read_privileged_derived()
            else f" AND tags NOT LIKE '%{_PRIVILEGED_DERIVED_TAG}%'"
        )
        with self._lock:
            conn = self.connect()
            removed = 0
            merged = 0
            vault_upserts: list[dict[str, Any]] = []
            removed_ids: list[str] = []
            if conn.in_transaction:
                raise RuntimeError("memory mutation requires a clean storage transaction")
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    f"""
                    SELECT id, user_id, namespace, content, tags, importance, created_at, updated_at
                    FROM memories
                    WHERE user_id = ?
                      {privileged_clause}
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (current_user_id(), max(1, min(5000, limit))),
                ).fetchall()
                groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
                for row in rows:
                    item = dict(row)
                    key = (
                        str(item["namespace"]),
                        _normalize_memory_content(str(item["content"])),
                    )
                    if not key[1]:
                        continue
                    groups.setdefault(key, []).append(item)

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
                    duplicate_ids = [
                        item["id"] for item in items if item["id"] != keep["id"]
                    ]
                    merged_tags = _merge_tags(
                        *(_loads(item.get("tags"), []) for item in items)
                    )
                    merged_importance = max(
                        float(item.get("importance") or 0) for item in items
                    )
                    now = utc_now()
                    conn.execute(
                        """
                        UPDATE memories
                        SET tags = ?, importance = ?, updated_at = ?
                        WHERE id = ? AND user_id = ?
                        """,
                        (
                            _json(merged_tags),
                            merged_importance,
                            now,
                            keep["id"],
                            current_user_id(),
                        ),
                    )
                    keep = {
                        **keep,
                        "tags": merged_tags,
                        "importance": merged_importance,
                        "updated_at": now,
                    }
                    self._replace_memory_fts(conn, keep)
                    vault_upserts.append(keep)
                    for duplicate_id in duplicate_ids:
                        if self._memory_fts_available:
                            conn.execute(
                                "DELETE FROM memories_fts WHERE id = ? AND user_id = ?",
                                (duplicate_id, current_user_id()),
                            )
                        conn.execute(
                            "DELETE FROM memories WHERE id = ? AND user_id = ?",
                            (duplicate_id, current_user_id()),
                        )
                        removed_ids.append(str(duplicate_id))
                    removed += len(duplicate_ids)
                    merged += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            self._sync_memory_vault_after_commit(
                conn,
                upserts=vault_upserts,
                removed_ids=removed_ids,
            )
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
        # Build the graph straight from the DB rows — no per-request disk sync/read on the
        # hot read path. The on-disk markdown vault is kept fresh incrementally on the write
        # path (add_memory / consolidate_memories); an explicit full resync lives in
        # rebuild_memory_vault(). See MemoryVault.graph_from_memories for the equivalence.
        with self._lock:
            conn = self.connect()
            memories = self._load_memories_for_vault(conn)
        graph = self._memory_vault_for().graph_from_memories(memories)
        self._augment_graph_with_documents(graph)
        return graph

    def rebuild_memory_vault(self) -> dict[str, Any]:
        with self._lock:
            conn = self.connect()
            self._sync_memory_vault_after_commit(
                conn,
                force_full=True,
                raise_on_error=True,
            )
        graph = self._memory_vault_for().graph()
        self._augment_graph_with_documents(graph)
        return graph

    def _augment_graph_with_documents(self, graph: dict[str, Any]) -> None:
        """Fold uploaded documents into the memory link graph, in place.

        Appends ``document`` nodes plus meaningful, capped edges to the dict returned
        by ``MemoryVault.graph()``: ``mentions`` edges (a memory note that names a file
        by its exact id or full filename) and derived document<->document links (same
        source folder / identical content / same upload day). Runs OUTSIDE the storage
        lock — ``list_files`` acquires ``self._lock`` itself and ``threading.Lock`` is
        not re-entrant. The ``.md`` vault layer and the API route are untouched.
        """

        files = self.list_files(limit=DOCUMENT_GRAPH_NODE_CAP)
        nodes: list[dict[str, Any]] = graph.setdefault("nodes", [])
        edges: list[dict[str, Any]] = graph.setdefault("edges", [])
        stats: dict[str, int] = graph.setdefault("stats", {})
        if not files:
            stats["documents"] = 0
            stats["document_edges"] = 0
            return

        docs_by_id: dict[str, dict[str, Any]] = {}
        for row in files:
            fid = str(row.get("id") or "")
            if not fid:
                continue
            docs_by_id[fid] = row
            nodes.append(
                {
                    "id": f"document:{fid}",
                    "label": row.get("name") or fid,
                    "kind": "document",
                    "doc_id": fid,
                    "mime": row.get("mime_type"),
                    "size": row.get("size"),
                    "status": row.get("status"),
                    "chunk_count": row.get("chunk_count"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                }
            )
        doc_ids = set(docs_by_id)

        # 1) mentions: a memory note that names a file (exact file id or full filename).
        name_to_ids: dict[str, list[str]] = defaultdict(list)
        for fid, row in docs_by_id.items():
            name = str(row.get("name") or "").strip()
            if len(name) >= 5:  # skip trivially short names that would match everywhere
                name_to_ids[name.casefold()].append(fid)
        filename_matcher = _FilenameMatcher(name_to_ids)
        mention_edges: list[dict[str, str]] = []
        seen_mentions: set[tuple[str, str]] = set()
        for note in graph.get("notes", []):
            if len(mention_edges) >= _DOC_MENTION_EDGE_CAP:
                break
            mem_id = str(note.get("id") or note.get("path") or "")
            content = str(note.get("content") or "")
            if not mem_id or not content:
                continue
            hits: set[str] = {fid for fid in _DOC_FILE_ID_RE.findall(content) if fid in doc_ids}
            for name in filename_matcher.find(content):
                hits.update(name_to_ids[name])
            for fid in sorted(hits):
                if len(mention_edges) >= _DOC_MENTION_EDGE_CAP:
                    break
                key = (mem_id, fid)
                if key in seen_mentions:
                    continue
                seen_mentions.add(key)
                mention_edges.append(
                    {"source": mem_id, "target": f"document:{fid}", "kind": "mentions"}
                )

        # 2) derived document<->document links, strongest signal first, hard-capped.
        derived_edges: list[dict[str, str]] = []
        hub_nodes: dict[str, dict[str, Any]] = {}
        per_doc_deg: Counter[str] = Counter()
        pair_seen: set[frozenset[str]] = set()
        global_cap = 3 * len(doc_ids)

        def _emit_pair(a: str, b: str, kind: str) -> None:
            if a == b or len(derived_edges) >= global_cap:
                return
            pair = frozenset((a, b))
            if pair in pair_seen:
                return
            if (
                per_doc_deg[a] >= _DOC_DERIV_PER_DOC_DEGREE_CAP
                or per_doc_deg[b] >= _DOC_DERIV_PER_DOC_DEGREE_CAP
            ):
                return
            pair_seen.add(pair)
            per_doc_deg[a] += 1
            per_doc_deg[b] += 1
            derived_edges.append(
                {"source": f"document:{a}", "target": f"document:{b}", "kind": kind}
            )

        def _emit_star(
            members: list[str], kind: str, hub_id: str, label: str, hub_kind: str
        ) -> None:
            added = False
            for member in members:
                if len(derived_edges) >= global_cap:
                    break
                if per_doc_deg[member] >= _DOC_DERIV_PER_DOC_DEGREE_CAP:
                    continue
                per_doc_deg[member] += 1
                derived_edges.append(
                    {"source": f"document:{member}", "target": hub_id, "kind": kind}
                )
                added = True
            if added:
                hub_nodes.setdefault(hub_id, {"id": hub_id, "label": label, "kind": hub_kind})

        def _process(
            groups: dict[str, list[str]], kind: str, hub_prefix: str | None, hub_kind: str
        ) -> None:
            for key in sorted(groups):
                members = sorted(set(groups[key]))
                if len(members) < 2:
                    continue
                if hub_prefix is not None and len(members) > _DOC_DERIV_BUCKET_K:
                    if hub_prefix == "folder":
                        hub_id = f"folder:{hashlib.sha1(key.encode()).hexdigest()[:12]}"
                        label = key.rstrip("/").split("/")[-1] or key
                    else:
                        hub_id = f"{hub_prefix}:{key}"
                        label = key
                    _emit_star(members, kind, hub_id, label, hub_kind)
                else:
                    for a, b in combinations(members[:50], 2):
                        if len(derived_edges) >= global_cap:
                            break
                        _emit_pair(a, b, kind)

        co_source: dict[str, list[str]] = defaultdict(list)
        same_content: dict[str, list[str]] = defaultdict(list)
        co_day: dict[str, list[str]] = defaultdict(list)
        for fid, row in docs_by_id.items():
            source = str(row.get("source_path") or "").strip()
            if source:
                parts = re.split(r"[\\/]+", source)
                if len(parts) > 1:
                    co_source["/".join(parts[:-1])].append(fid)
            sha = str(row.get("sha256") or "").strip()
            if sha:
                same_content[sha].append(fid)
            created = str(row.get("created_at") or "")[:10]
            if created:
                co_day[created].append(fid)

        _process(same_content, "same-content", None, "document")
        _process(co_source, "co-source", "folder", "folder")
        _process(co_day, "co-day", "daybucket", "daybucket")

        # Merge, then recompute degree + top_nodes + stats over the unified graph.
        edges.extend(mention_edges)
        edges.extend(derived_edges)
        nodes.extend(hub_nodes.values())

        degree: dict[str, int] = {}
        for edge in edges:
            degree[edge["source"]] = degree.get(edge["source"], 0) + 1
            degree[edge["target"]] = degree.get(edge["target"], 0) + 1
        for node in nodes:
            node["degree"] = degree.get(str(node.get("id") or ""), 0)
        graph["top_nodes"] = sorted(
            ({**node} for node in nodes),
            key=lambda item: (int(item.get("degree") or 0), str(item.get("label") or "")),
            reverse=True,
        )[:12]
        stats["nodes"] = len(nodes)
        stats["edges"] = len(edges)
        stats["documents"] = len(doc_ids)
        stats["document_edges"] = len(mention_edges) + len(derived_edges)

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
        user_id = current_user_id()
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
                        id, title, goal, status, progress, created_at, updated_at, user_id
                    )
                    VALUES (?, ?, ?, 'planned', 0, ?, ?, ?)
                    """,
                    (selected_id, title[:240], goal, now, now, user_id),
                )
                created = cursor.rowcount == 1
                if created:
                    for position, task_title in enumerate(tasks, start=1):
                        conn.execute(
                            """
                            INSERT INTO mission_tasks(
                                id, mission_id, title, status, notes, position,
                                created_at, updated_at, user_id
                            )
                            VALUES (?, ?, ?, 'pending', NULL, ?, ?, ?, ?)
                            """,
                            (
                                new_id("task"),
                                selected_id,
                                task_title,
                                position,
                                now,
                                now,
                                user_id,
                            ),
                        )
                    self._refresh_mission_progress(conn, selected_id, now=now)
                else:
                    existing = conn.execute(
                        "SELECT goal, user_id FROM missions WHERE id = ?",
                        (selected_id,),
                    ).fetchone()
                    if existing is not None and str(existing["user_id"]) != user_id:
                        raise ResourceIsolationError(
                            "mission id is not available to this user"
                        )
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
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (current_user_id(), limit),
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
                WHERE id = ? AND user_id = ?
                """,
                (mission_id, current_user_id()),
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
                WHERE mission_id = ? AND status = 'pending' AND user_id = ?
                ORDER BY position ASC
                LIMIT 1
                """,
                (mission_id, current_user_id()),
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
            mission = conn.execute(
                "SELECT id FROM missions WHERE id = ? AND user_id = ?",
                (mission_id, current_user_id()),
            ).fetchone()
            if mission is None:
                raise KeyError(f"unknown mission: {mission_id}")
            last_position = int(
                conn.execute(
                    "SELECT COALESCE(MAX(position), 0) AS value FROM mission_tasks "
                    "WHERE mission_id = ? AND user_id = ?",
                    (mission_id, current_user_id()),
                ).fetchone()["value"]
            )
            selected = last_position + 1 if position is None else int(position)
            if selected < 1 or selected > last_position + 1:
                raise ValueError("mission task position is outside the insert range")
            if selected <= last_position:
                conn.execute(
                    "UPDATE mission_tasks SET position = position + 1, updated_at = ? "
                    "WHERE mission_id = ? AND position >= ? AND user_id = ?",
                    (now, mission_id, selected, current_user_id()),
                )
            conn.execute(
                """
                INSERT INTO mission_tasks(
                    id, mission_id, title, status, notes, position, created_at, updated_at,
                    user_id
                )
                VALUES (?, ?, ?, 'pending', NULL, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    mission_id,
                    clean_title[:500],
                    selected,
                    now,
                    now,
                    current_user_id(),
                ),
            )
            self._refresh_mission_progress(conn, mission_id, now=now)
            conn.commit()
            row = conn.execute(
                "SELECT id, mission_id, title, status, notes, position, created_at, updated_at "
                "FROM mission_tasks WHERE id = ? AND user_id = ?",
                (task_id, current_user_id()),
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
                WHERE id = ? AND mission_id = ? AND status = 'pending' AND user_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM mission_tasks AS active
                      WHERE active.mission_id = ? AND active.status = 'running'
                        AND active.user_id = ?
                  )
                RETURNING id, mission_id, title, status, notes, position, created_at, updated_at
                """,
                (
                    now,
                    task_id,
                    mission_id,
                    current_user_id(),
                    mission_id,
                    current_user_id(),
                ),
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
                      AND pending.user_id = ?
                      AND pending.status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM mission_tasks AS active
                          WHERE active.mission_id = pending.mission_id
                            AND active.user_id = pending.user_id
                            AND active.status IN ('running', 'blocked')
                      )
                    ORDER BY pending.position ASC
                    LIMIT 1
                )
                  AND status = 'pending' AND user_id = ?
                RETURNING
                    id, mission_id, title, status, notes, position, created_at, updated_at
                """,
                (now, mission_id, current_user_id(), current_user_id()),
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
            "user_id": current_user_id(),
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
            if conversation_id:
                self._require_owned_resource(
                    conn,
                    table="conversations",
                    resource_id=conversation_id,
                    user_id=row["user_id"],
                )
            conn.execute(
                """
                INSERT INTO reminders(
                    id, created_at, updated_at, text, due_at, recurrence, status,
                    conversation_id, source_text, fired_at, fire_count, payload, user_id
                )
                VALUES (
                    :id, :created_at, :updated_at, :text, :due_at, :recurrence, :status,
                    :conversation_id, :source_text, :fired_at, :fire_count, :payload, :user_id
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
            "conversation_id, source_text, fired_at, fire_count, payload, user_id "
            "FROM reminders"
        )
        clauses: list[str] = ["user_id = ?"]
        params: list[Any] = [current_user_id()]
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if before:
            clauses.append("due_at <= ?")
            params.append(before)
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
                       conversation_id, source_text, fired_at, fire_count, payload, user_id
                FROM reminders
                WHERE id = ? AND user_id = ?
                """,
                (reminder_id, current_user_id()),
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
                WHERE id = ? AND status = 'pending' AND user_id = ?
                RETURNING id, created_at, updated_at, text, due_at, recurrence, status,
                          conversation_id, source_text, fired_at, fire_count, payload, user_id
                """,
                (now, reminder_id, current_user_id()),
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

    def reschedule_reminder(
        self,
        reminder_id: str,
        *,
        due_at: str,
    ) -> dict[str, Any] | None:
        """Re-open a pending or already-fired reminder at a new UTC due-time (snooze).

        Fired one-shots become pending again so the next supervisor tick can redeliver.
        Cancelled rows stay cancelled (operator must recreate).
        """

        now = utc_now()
        clean_due = str(due_at or "").strip()
        if not clean_due:
            return None
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                """
                UPDATE reminders
                SET status = 'pending', due_at = ?, fired_at = NULL, updated_at = ?
                WHERE id = ? AND status IN ('pending', 'fired') AND user_id = ?
                RETURNING id, created_at, updated_at, text, due_at, recurrence, status,
                          conversation_id, source_text, fired_at, fire_count, payload, user_id
                """,
                (clean_due, now, reminder_id, current_user_id()),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        updated = self._decode_reminder(row)
        self.record_audit(
            actor="operator",
            action="reminder.reschedule",
            target_type="reminder",
            target_id=reminder_id,
            summary=f"Reminder snoozed to {clean_due}: {updated['text']}",
            after=updated,
        )
        return updated

    def claim_due_reminders(
        self,
        now_iso: str | None = None,
        *,
        tz_name: str | None = None,
        limit: int = 20,
        skip_ids: Iterable[str] = (),
        excluded_payload_kinds: Iterable[str] = (),
        all_users: bool = False,
    ) -> list[dict[str, Any]]:
        """Atomically fire every due reminder in a single BEGIN IMMEDIATE transaction.

        One-shot rows flip to ``fired``; recurring rows advance ``due_at`` to the first
        occurrence strictly after ``now`` (rolled past downtime in one shot — no
        catch-up burst) and stay ``pending``. Mirrors ``create_mission`` /
        ``claim_next_mission_task``: ``BEGIN IMMEDIATE`` plus a ``status='pending'``
        guard on every UPDATE prevents a double fire even if a manual run races the loop.
        Returns one snapshot per fired reminder (its state *before* advancement).
        """

        if all_users:
            self._require_system_scope("cross-tenant reminder claim")
        from .reminders import compute_next_due, reminder_zone, to_utc_iso

        now = now_iso or utc_now()
        tz = reminder_zone(tz_name) if tz_name else reminder_zone()
        now_local = datetime.fromisoformat(now).astimezone(tz)
        skipped = tuple(dict.fromkeys(str(item) for item in skip_ids if str(item)))
        excluded_kinds = tuple(
            dict.fromkeys(str(item) for item in excluded_payload_kinds if str(item))
        )
        fired: list[dict[str, Any]] = []
        with self._lock:
            conn = self.connect()
            if conn.in_transaction:
                raise RuntimeError("reminder claim requires a clean storage transaction")
            try:
                conn.execute("BEGIN IMMEDIATE")
                clauses = ["status = 'pending'", "due_at <= ?"]
                params: list[Any] = [now]
                if not all_users:
                    clauses.append("user_id = ?")
                    params.append(current_user_id())
                if skipped:
                    placeholders = ",".join("?" for _ in skipped)
                    clauses.append(f"id NOT IN ({placeholders})")
                    params.extend(skipped)
                if excluded_kinds:
                    placeholders = ",".join("?" for _ in excluded_kinds)
                    clauses.append(
                        "COALESCE(json_extract(payload, '$.kind'), '') "
                        f"NOT IN ({placeholders})"
                    )
                    params.extend(excluded_kinds)
                # A persisted notification is an outbox lease: recurring watches do not
                # capture again until the prior notice has been delivered or retried.
                clauses.append(
                    "NOT (COALESCE(json_extract(payload, '$.kind'), '') = 'screen_watch' "
                    "AND COALESCE(json_extract(payload, '$.notification.state'), '') = 'pending')"
                )
                params.append(limit)
                rows = conn.execute(
                    f"""
                    SELECT id, created_at, updated_at, text, due_at, recurrence, status,
                           conversation_id, source_text, fired_at, fire_count, payload, user_id
                    FROM reminders
                    WHERE {' AND '.join(clauses)}
                    ORDER BY due_at ASC
                    LIMIT ?
                    """,
                    tuple(params),
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
                            WHERE id = ? AND status = 'pending' AND user_id = ?
                            """,
                            (
                                to_utc_iso(next_due),
                                now,
                                now,
                                snapshot["id"],
                                snapshot["user_id"],
                            ),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE reminders
                            SET status = 'fired', fired_at = ?, fire_count = fire_count + 1,
                                updated_at = ?
                            WHERE id = ? AND status = 'pending' AND user_id = ?
                            """,
                            (now, now, snapshot["id"], snapshot["user_id"]),
                        )
                    fired.append(snapshot)
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
        return fired

    def stage_screen_watch_notification(
        self,
        reminder_id: str,
        *,
        expected_fire_count: int,
        terminal_status: str | None,
        text: str,
        event_kind: str,
        level: str,
        met: bool,
    ) -> dict[str, Any] | None:
        """Atomically seal a watcher result into its durable delivery outbox."""

        if terminal_status not in {None, "fired", "cancelled"}:
            raise ValueError("invalid screen-watch terminal status")
        now = utc_now()
        with self._lock:
            conn = self.connect()
            if conn.in_transaction:
                raise RuntimeError("screen-watch transition requires a clean transaction")
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT id, created_at, updated_at, text, due_at, recurrence, status,
                           conversation_id, source_text, fired_at, fire_count, payload, user_id
                    FROM reminders WHERE id = ? AND user_id = ?
                    """,
                    (reminder_id, current_user_id()),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                current = self._decode_reminder(row)
                payload = current.get("payload")
                if (
                    current.get("status") != "pending"
                    or int(current.get("fire_count") or 0) != int(expected_fire_count)
                    or not isinstance(payload, dict)
                    or payload.get("kind") != "screen_watch"
                ):
                    conn.rollback()
                    return None
                notification_id = f"{reminder_id}:{expected_fire_count}"
                payload = {
                    **payload,
                    "notification": {
                        "id": notification_id,
                        "state": "pending",
                        "text": str(text)[:3900],
                        "event_kind": str(event_kind)[:120],
                        "level": str(level)[:20],
                        "met": bool(met),
                        "telegram_delivered": False,
                        "telegram_delivered_ids": [],
                        "conversation_delivered": False,
                        "event_delivered": False,
                        "local_delivered": False,
                        "created_at": now,
                    },
                }
                next_status = terminal_status or "pending"
                updated = conn.execute(
                    """
                    UPDATE reminders
                    SET status = ?, payload = ?, updated_at = ?
                    WHERE id = ? AND status = 'pending' AND fire_count = ? AND user_id = ?
                    """,
                    (
                        next_status,
                        _json(payload),
                        now,
                        reminder_id,
                        expected_fire_count,
                        current_user_id(),
                    ),
                )
                if updated.rowcount != 1:
                    conn.rollback()
                    return None
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
        return self.get_reminder(reminder_id)

    def list_pending_screen_watch_notifications(
        self, limit: int = 50, *, all_users: bool = False
    ) -> list[dict[str, Any]]:
        if all_users:
            self._require_system_scope("cross-tenant screen-watch delivery")
        with self._lock:
            user_clause = "" if all_users else " AND user_id = ?"
            params: list[Any] = [] if all_users else [current_user_id()]
            params.append(max(1, min(500, int(limit))))
            rows = self.connect().execute(
                f"""
                SELECT id, created_at, updated_at, text, due_at, recurrence, status,
                       conversation_id, source_text, fired_at, fire_count, payload, user_id
                FROM reminders
                WHERE COALESCE(json_extract(payload, '$.kind'), '') = 'screen_watch'
                  AND COALESCE(json_extract(payload, '$.notification.state'), '') = 'pending'
                  {user_clause}
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._decode_reminder(row) for row in rows]

    def update_screen_watch_notification(
        self,
        reminder_id: str,
        notification_id: str,
        *,
        telegram_delivered: bool | None = None,
        telegram_target_ids: tuple[int, ...] | list[int] | None = None,
        telegram_delivered_ids: tuple[int, ...] | list[int] | None = None,
        conversation_delivered: bool | None = None,
        event_delivered: bool | None = None,
        local_delivered: bool | None = None,
        completed: bool = False,
    ) -> dict[str, Any] | None:
        """Persist outbox progress without reopening a completed watcher."""

        now = utc_now()
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                """
                SELECT id, created_at, updated_at, text, due_at, recurrence, status,
                       conversation_id, source_text, fired_at, fire_count, payload, user_id
                FROM reminders WHERE id = ? AND user_id = ?
                """,
                (reminder_id, current_user_id()),
            ).fetchone()
            if row is None:
                return None
            current = self._decode_reminder(row)
            payload = current.get("payload")
            notification = payload.get("notification") if isinstance(payload, dict) else None
            if not isinstance(notification, dict) or notification.get("id") != notification_id:
                return None
            notification = dict(notification)
            if telegram_delivered is not None:
                notification["telegram_delivered"] = bool(telegram_delivered)
            if telegram_target_ids is not None:
                notification["telegram_target_ids"] = list(
                    dict.fromkeys(int(item) for item in telegram_target_ids)
                )
            if telegram_delivered_ids is not None:
                notification["telegram_delivered_ids"] = list(
                    dict.fromkeys(int(item) for item in telegram_delivered_ids)
                )
            if conversation_delivered is not None:
                notification["conversation_delivered"] = bool(conversation_delivered)
            if event_delivered is not None:
                notification["event_delivered"] = bool(event_delivered)
            if local_delivered is not None:
                notification["local_delivered"] = bool(local_delivered)
            if completed:
                notification["state"] = "delivered"
                notification["delivered_at"] = now
            payload = {**payload, "notification": notification}
            conn.execute(
                "UPDATE reminders SET payload = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (_json(payload), now, reminder_id, current_user_id()),
            )
            conn.commit()
        return self.get_reminder(reminder_id)

    def deliver_screen_watch_local_notification(
        self,
        reminder_id: str,
        notification_id: str,
        *,
        text: str,
        event_kind: str,
        level: str,
        met: bool,
        event_payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Atomically persist the conversation message, event, and outbox receipt.

        The deterministic row ids plus one SQLite transaction make retries idempotent:
        a crash cannot commit the local artifacts without also sealing local delivery.
        """

        now = utc_now()
        digest = hashlib.sha256(notification_id.encode("utf-8")).hexdigest()[:24]
        message_id = f"msg_swn_{digest}"
        event_id = f"evt_swn_{digest}"
        learning_id = f"learn_swn_{digest}"
        with self._lock:
            conn = self.connect()
            if conn.in_transaction:
                raise RuntimeError("screen-watch local delivery requires a clean transaction")
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT id, created_at, updated_at, text, due_at, recurrence, status,
                           conversation_id, source_text, fired_at, fire_count, payload, user_id
                    FROM reminders WHERE id = ? AND user_id = ?
                    """,
                    (reminder_id, current_user_id()),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                current = self._decode_reminder(row)
                payload = current.get("payload")
                notification = payload.get("notification") if isinstance(payload, dict) else None
                if (
                    not isinstance(notification, dict)
                    or notification.get("id") != notification_id
                ):
                    conn.rollback()
                    return None
                if notification.get("state") != "pending":
                    conn.commit()
                    return current
                if bool(notification.get("local_delivered")):
                    conn.commit()
                    return current

                conversation_id = current.get("conversation_id")
                if conversation_id:
                    metadata = {
                        "kind": "screen_watch",
                        "reminder_id": reminder_id,
                        "met": bool(met),
                        "notification_id": notification_id,
                    }
                    conn.execute(
                        """
                        INSERT INTO messages(
                            id, conversation_id, role, content, metadata, created_at, user_id
                        ) VALUES (?, ?, 'assistant', ?, ?, ?, ?)
                        """,
                        (
                            message_id,
                            str(conversation_id),
                            text,
                            _json(metadata),
                            now,
                            current_user_id(),
                        ),
                    )
                    conn.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ? AND user_id = ?",
                        (now, str(conversation_id), current_user_id()),
                    )
                    learning_row = self._learning_observation_row(
                        kind="conversation.message",
                        source_id=message_id,
                        conversation_id=str(conversation_id),
                        role="assistant",
                        content=text,
                        summary="assistant message captured for learning",
                        payload={"metadata": metadata},
                        ts=now,
                    )
                    learning_row["id"] = learning_id
                    self._insert_learning_observation(conn, **learning_row)

                conn.execute(
                    """
                    INSERT INTO runtime_events(id, ts, level, kind, title, payload, user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        now,
                        str(level)[:20],
                        str(event_kind)[:120],
                        text.splitlines()[0][:240],
                        _json(event_payload),
                        current_user_id(),
                    ),
                )
                notification = {
                    **notification,
                    "conversation_delivered": bool(conversation_id),
                    "event_delivered": True,
                    "local_delivered": True,
                }
                payload = {**payload, "notification": notification}
                conn.execute(
                    "UPDATE reminders SET payload = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (_json(payload), now, reminder_id, current_user_id()),
                )
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise
        return self.get_reminder(reminder_id)

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
                WHERE id = ? AND (? IS NULL OR mission_id = ?) AND user_id = ?
                """,
                (task_id, mission_id, mission_id, current_user_id()),
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
                WHERE id = ? AND (? IS NULL OR mission_id = ?) AND user_id = ?
                """,
                (
                    next_title,
                    next_status,
                    next_notes,
                    now,
                    task_id,
                    mission_id,
                    mission_id,
                    current_user_id(),
                ),
            )
            self._refresh_mission_progress(conn, existing["mission_id"], now=now)
            conn.commit()
            row = conn.execute(
                """
                SELECT id, mission_id, title, status, notes, position, created_at, updated_at
                FROM mission_tasks
                WHERE id = ? AND user_id = ?
                """,
                (task_id, current_user_id()),
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
                WHERE mission_id = ? AND user_id = ?
                ORDER BY position ASC
                """,
                (mission_id, current_user_id()),
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
            "user_id": current_user_id(),
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
            if mission_id:
                self._require_owned_resource(
                    conn, table="missions", resource_id=mission_id
                )
            if task_id:
                self._require_owned_resource(
                    conn, table="mission_tasks", resource_id=task_id
                )
            conn.execute(
                """
                INSERT INTO tool_runs(
                    id, ts, tool, ok, summary, arguments, data, mission_id, task_id,
                    user_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    row["user_id"],
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
                WHERE user_id = ?
                ORDER BY ts DESC, rowid DESC
                LIMIT ?
                """,
                (current_user_id(), limit),
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
        params: list[Any] = [current_user_id()]
        where = "WHERE user_id = ?"
        if kind:
            where += " AND kind = ?"
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
            "user_id": current_user_id(),
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
                id, ts, kind, source_id, conversation_id, role, content, summary,
                payload, user_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                row.get("user_id") or current_user_id(),
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
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if user_id is not None and user_id != current_user_id():
            self._require_system_scope("cross-tenant audit write")
        row = {
            "id": new_id("aud"),
            "user_id": user_id or current_user_id(),
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
                    id, ts, actor, action, target_type, target_id, summary, before_json,
                    after_json, user_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    row["user_id"],
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
        conditions: list[str] = ["user_id = ?"]
        params: list[Any] = [current_user_id()]
        if target_type:
            conditions.append("target_type = ?")
            params.append(target_type)
        if target_id:
            conditions.append("target_id = ?")
            params.append(target_id)
        where = f"WHERE {' AND '.join(conditions)}"
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
            "user_id": current_user_id(),
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
                    status, error, chunk_count, created_at, updated_at, user_id
                )
                VALUES (
                    :id, :name, :source_path, :stored_path, :mime_type, :size, :sha256,
                    :status, :error, :chunk_count, :created_at, :updated_at, :user_id
                )
                """,
                row,
            )
            self.connect().commit()
        return row

    def claim_file_ingest(
        self,
        *,
        name: str,
        stored_path: Path,
        sha256: str,
        size: int,
        mime_type: str,
        source_path: Path | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Claim one canonical durable record for a tenant/content hash.

        The claim table provides a database-enforced serialization point without
        deleting legacy duplicate rows (whose ids may still be referenced from old
        conversations).  ``True`` means this caller created the canonical row and
        therefore owns its synchronous indexing attempt.
        """

        now = utc_now()
        row = {
            "id": new_id("file"),
            "user_id": current_user_id(),
            "name": name[:260],
            "source_path": str(source_path) if source_path else None,
            "stored_path": str(stored_path),
            "mime_type": mime_type[:120],
            "size": int(size),
            "sha256": str(sha256),
            "status": "indexing",
            "error": None,
            "chunk_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        select_columns = """
            f.id, f.name, f.source_path, f.stored_path, f.mime_type, f.size,
            f.sha256, f.status, f.error, f.chunk_count, f.created_at, f.updated_at
        """
        with self.transaction(immediate=True) as conn:
            claimed = conn.execute(
                f"""
                SELECT {select_columns}
                FROM file_ingest_claims claim
                JOIN files f ON f.id = claim.file_id AND f.user_id = claim.user_id
                WHERE claim.user_id = ? AND claim.sha256 = ?
                """,
                (row["user_id"], row["sha256"]),
            ).fetchone()
            if claimed is not None:
                return dict(claimed), False

            conn.execute(
                """
                DELETE FROM file_ingest_claims
                WHERE user_id = ? AND sha256 = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM files
                      WHERE files.id = file_ingest_claims.file_id
                        AND files.user_id = file_ingest_claims.user_id
                  )
                """,
                (row["user_id"], row["sha256"]),
            )
            legacy = conn.execute(
                f"""
                SELECT {select_columns}
                FROM files f
                WHERE f.sha256 = ? AND f.user_id = ?
                ORDER BY
                    CASE WHEN f.status = 'indexed' THEN 0 ELSE 1 END,
                    CASE WHEN f.mime_type = 'application/octet-stream' THEN 1 ELSE 0 END,
                    f.created_at ASC,
                    f.rowid ASC
                LIMIT 1
                """,
                (row["sha256"], row["user_id"]),
            ).fetchone()
            if legacy is not None:
                conn.execute(
                    """
                    INSERT INTO file_ingest_claims(user_id, sha256, file_id, claimed_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (row["user_id"], row["sha256"], legacy["id"], now),
                )
                return dict(legacy), False

            conn.execute(
                """
                INSERT INTO files(
                    id, name, source_path, stored_path, mime_type, size, sha256,
                    status, error, chunk_count, created_at, updated_at, user_id
                )
                VALUES (
                    :id, :name, :source_path, :stored_path, :mime_type, :size, :sha256,
                    :status, :error, :chunk_count, :created_at, :updated_at, :user_id
                )
                """,
                row,
            )
            conn.execute(
                """
                INSERT INTO file_ingest_claims(user_id, sha256, file_id, claimed_at)
                VALUES (?, ?, ?, ?)
                """,
                (row["user_id"], row["sha256"], row["id"], now),
            )
        return row, True

    def begin_file_reindex(
        self,
        file_id: str,
        *,
        name: str,
        stored_path: Path,
        size: int,
        mime_type: str,
        source_path: Path | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Acquire a failed/stored canonical record for one indexing attempt."""

        user_id = current_user_id()
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            self._require_owned_resource(
                conn,
                table="files",
                resource_id=file_id,
                user_id=user_id,
            )
            cursor = conn.execute(
                """
                UPDATE files
                SET name = ?, source_path = ?, stored_path = ?, mime_type = ?, size = ?,
                    status = 'indexing', error = NULL, chunk_count = 0, updated_at = ?
                WHERE id = ? AND user_id = ?
                  AND status NOT IN ('indexed', 'indexing')
                """,
                (
                    name[:260],
                    str(source_path) if source_path else None,
                    str(stored_path),
                    mime_type[:120],
                    int(size),
                    now,
                    file_id,
                    user_id,
                ),
            )
            if cursor.rowcount == 1:
                self._replace_file_chunks(conn, file_id, [], now=now)
                conn.execute(
                    "DELETE FROM file_index_metadata WHERE file_id = ? AND user_id = ?",
                    (file_id, user_id),
                )
            current = conn.execute(
                """
                SELECT id, name, source_path, stored_path, mime_type, size, sha256,
                       status, error, chunk_count, created_at, updated_at
                FROM files
                WHERE id = ? AND user_id = ?
                """,
                (file_id, user_id),
            ).fetchone()
            if current is None:
                raise KeyError(file_id)
            return dict(current), cursor.rowcount == 1

    def fail_file_indexing(self, file_id: str, error: str) -> dict[str, Any] | None:
        """Move an owned transient record to an honest, retryable terminal state."""

        user_id = current_user_id()
        with self.transaction(immediate=True) as conn:
            self._require_owned_resource(
                conn,
                table="files",
                resource_id=file_id,
                user_id=user_id,
            )
            conn.execute(
                """
                UPDATE files
                SET status = 'failed', error = ?, chunk_count = 0, updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'indexing'
                """,
                (str(error)[:4_000], utc_now(), file_id, user_id),
            )
        return self.get_file(file_id)

    @staticmethod
    def _extracted_text_chunks(
        text: str,
        *,
        chunk_chars: int,
        chunk_overlap: int,
    ) -> list[str]:
        content = str(text or "").strip()
        if not content:
            raise ValueError("extracted text cannot be empty")
        bounded_chunk_chars = max(500, min(20_000, int(chunk_chars)))
        bounded_overlap = max(0, min(bounded_chunk_chars // 2, int(chunk_overlap)))
        chunks: list[str] = []
        start = 0
        while start < len(content):
            end = min(len(content), start + bounded_chunk_chars)
            chunk = content[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end >= len(content):
                break
            start = max(start + 1, end - bounded_overlap)
        if not chunks:
            raise ValueError("extracted text produced no searchable chunks")
        return chunks

    def _persist_file_extracted_text_conn(
        self,
        conn: sqlite3.Connection,
        file_id: str,
        text: str,
        *,
        user_id: str,
        source: str,
        details: dict[str, Any] | None,
        warning: str | None,
        chunk_chars: int = 1_800,
        chunk_overlap: int = 180,
    ) -> dict[str, Any]:
        extraction_source = str(source or "").strip()
        if not extraction_source:
            raise ValueError("extraction source is required")
        chunks = self._extracted_text_chunks(
            text,
            chunk_chars=chunk_chars,
            chunk_overlap=chunk_overlap,
        )

        now = utc_now()
        self._require_owned_resource(
            conn,
            table="files",
            resource_id=file_id,
            user_id=user_id,
        )
        self._replace_file_chunks(conn, file_id, chunks, now=now)
        conn.execute(
            """
            UPDATE files
            SET status = 'indexed', error = ?, chunk_count = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                str(warning)[:4_000] if warning else None,
                len(chunks),
                now,
                file_id,
                user_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO file_index_metadata(
                file_id, user_id, source, details_json, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                user_id = excluded.user_id,
                source = excluded.source,
                details_json = excluded.details_json,
                updated_at = excluded.updated_at
            """,
            (
                file_id,
                user_id,
                extraction_source[:120],
                _json(details or {}),
                now,
            ),
        )
        updated = conn.execute(
            """
            SELECT id, name, source_path, stored_path, mime_type, size, sha256,
                   status, error, chunk_count, created_at, updated_at
            FROM files WHERE id = ? AND user_id = ?
            """,
            (file_id, user_id),
        ).fetchone()
        if updated is None:
            raise KeyError(file_id)
        record = dict(updated)
        record["index_source"] = extraction_source[:120]
        record["index_details"] = dict(details or {})
        return record

    def persist_file_extracted_text(
        self,
        file_id: str,
        text: str,
        *,
        source: str,
        details: dict[str, Any] | None = None,
        warning: str | None = None,
        chunk_chars: int = 1_800,
        chunk_overlap: int = 180,
    ) -> dict[str, Any]:
        """Atomically replace an owned file's index with derived text."""

        user_id = current_user_id()
        with self.transaction(immediate=True) as conn:
            record = self._persist_file_extracted_text_conn(
                conn,
                file_id,
                text,
                user_id=user_id,
                source=source,
                details=details,
                warning=warning,
                chunk_chars=chunk_chars,
                chunk_overlap=chunk_overlap,
            )
        return record

    def _merge_file_extracted_text_conn(
        self,
        conn: sqlite3.Connection,
        file_id: str,
        text: str,
        *,
        user_id: str,
        source: str,
        details: dict[str, Any] | None,
        warning: str | None,
        chunk_chars: int = 1_800,
        chunk_overlap: int = 180,
    ) -> dict[str, Any]:
        """Append supplemental OCR chunks without discarding native PDF extraction."""

        extraction_source = str(source or "").strip()
        if not extraction_source:
            raise ValueError("extraction source is required")
        supplemental_chunks = self._extracted_text_chunks(
            text,
            chunk_chars=chunk_chars,
            chunk_overlap=chunk_overlap,
        )
        file_record = conn.execute(
            "SELECT * FROM files WHERE id = ? AND user_id = ?",
            (file_id, user_id),
        ).fetchone()
        if file_record is None:
            raise KeyError(file_id)
        native_rows = conn.execute(
            """
            SELECT content FROM file_chunks
            WHERE file_id = ? AND user_id = ?
            ORDER BY position ASC, rowid ASC
            """,
            (file_id, user_id),
        ).fetchall()
        native_chunks = [str(row["content"]) for row in native_rows if str(row["content"]).strip()]
        if not native_chunks:
            return self._persist_file_extracted_text_conn(
                conn,
                file_id,
                text,
                user_id=user_id,
                source=source,
                details=details,
                warning=warning,
                chunk_chars=chunk_chars,
                chunk_overlap=chunk_overlap,
            )

        def fingerprint(value: str) -> str:
            return " ".join(value.casefold().split())

        seen = {fingerprint(chunk) for chunk in native_chunks}
        added_chunks: list[str] = []
        for chunk in supplemental_chunks:
            key = fingerprint(chunk)
            if not key or key in seen:
                continue
            seen.add(key)
            added_chunks.append(chunk)
        combined_chunks = [*native_chunks, *added_chunks]

        metadata_row = conn.execute(
            """
            SELECT source, details_json FROM file_index_metadata
            WHERE file_id = ? AND user_id = ?
            """,
            (file_id, user_id),
        ).fetchone()
        native_source = str(metadata_row["source"]) if metadata_row else "native_extraction"
        native_details = _loads(metadata_row["details_json"], {}) if metadata_row else {}
        merged_source = f"{native_source}+{extraction_source}"[:120]
        merged_details = {
            **dict(details or {}),
            "ocr_merge": {
                "native_source": native_source,
                "native_details": native_details,
                "native_chunks_preserved": len(native_chunks),
                "ocr_chunks_added": len(added_chunks),
            },
        }
        warnings: list[str] = []
        for item in (file_record["error"], warning):
            clean = str(item or "").strip()
            if clean and clean not in warnings:
                warnings.append(clean)

        now = utc_now()
        self._replace_file_chunks(conn, file_id, combined_chunks, now=now)
        conn.execute(
            """
            UPDATE files
            SET status = 'indexed', error = ?, chunk_count = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                "; ".join(warnings)[:4_000] or None,
                len(combined_chunks),
                now,
                file_id,
                user_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO file_index_metadata(
                file_id, user_id, source, details_json, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                user_id = excluded.user_id,
                source = excluded.source,
                details_json = excluded.details_json,
                updated_at = excluded.updated_at
            """,
            (file_id, user_id, merged_source, _json(merged_details), now),
        )
        updated = conn.execute(
            """
            SELECT id, name, source_path, stored_path, mime_type, size, sha256,
                   status, error, chunk_count, created_at, updated_at
            FROM files WHERE id = ? AND user_id = ?
            """,
            (file_id, user_id),
        ).fetchone()
        if updated is None:
            raise KeyError(file_id)
        record = dict(updated)
        record["index_source"] = merged_source
        record["index_details"] = merged_details
        return record

    def merge_file_extracted_text(
        self,
        file_id: str,
        text: str,
        *,
        source: str,
        details: dict[str, Any] | None = None,
        warning: str | None = None,
        chunk_chars: int = 1_800,
        chunk_overlap: int = 180,
    ) -> dict[str, Any]:
        """Atomically merge supplemental OCR text into an owned file index."""

        user_id = current_user_id()
        with self.transaction(immediate=True) as conn:
            return self._merge_file_extracted_text_conn(
                conn,
                file_id,
                text,
                user_id=user_id,
                source=source,
                details=details,
                warning=warning,
                chunk_chars=chunk_chars,
                chunk_overlap=chunk_overlap,
            )

    def get_file_index_metadata(self, file_id: str) -> dict[str, Any] | None:
        """Return extraction provenance for one owned file."""

        user_id = current_user_id()
        with self._lock:
            conn = self.connect()
            self._require_owned_resource(
                conn,
                table="files",
                resource_id=file_id,
                user_id=user_id,
            )
            row = conn.execute(
                """
                SELECT file_id, source, details_json, updated_at
                FROM file_index_metadata
                WHERE file_id = ? AND user_id = ?
                """,
                (file_id, user_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "file_id": row["file_id"],
            "source": row["source"],
            "details": _loads(row["details_json"], {}),
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _decode_file_ocr_job(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["result_metadata"] = _loads(item.pop("result_metadata_json", "{}"), {})
        return item

    def enqueue_file_ocr_job(
        self,
        file_id: str,
        *,
        reason: str,
        max_attempts: int = 4,
        max_generations: int = 3,
        allow_existing_index: bool = False,
        restart_failed: bool = False,
    ) -> dict[str, Any] | None:
        """Idempotently enqueue OCR, optionally restarting a verified failed generation."""

        user_id = current_user_id()
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            self._require_owned_resource(
                conn,
                table="files",
                resource_id=file_id,
                user_id=user_id,
            )
            file_record = conn.execute(
                """
                SELECT status, chunk_count, sha256, stored_path, size
                FROM files WHERE id = ? AND user_id = ?
                """,
                (file_id, user_id),
            ).fetchone()
            if file_record is None:
                raise KeyError(file_id)
            existing = conn.execute(
                "SELECT * FROM file_ocr_jobs WHERE file_id = ? AND user_id = ?",
                (file_id, user_id),
            ).fetchone()
            if existing is not None:
                if not restart_failed or str(existing["status"]) != "failed":
                    return self._decode_file_ocr_job(existing)
                generation = int(existing["generation"])
                generation_limit = int(existing["max_generations"])
                if generation >= generation_limit:
                    return self._decode_file_ocr_job(existing)
                stored_path = Path(str(file_record["stored_path"]))
                try:
                    blob_verified = (
                        stored_path.is_file()
                        and stored_path.stat().st_size == int(file_record["size"])
                        and _file_sha256(stored_path) == str(file_record["sha256"])
                    )
                except OSError:
                    blob_verified = False
                if not blob_verified:
                    raise ValueError("OCR retry requires a verified source blob")
                result_metadata = _loads(existing["result_metadata_json"], {})
                retry_history = list(result_metadata.get("retry_history") or [])[-4:]
                retry_history.append(
                    {
                        "generation": generation,
                        "attempts": int(existing["attempt_count"]),
                        "last_error": str(existing["last_error"] or "")[:1_000],
                        "failed_at": str(existing["updated_at"]),
                    }
                )
                conn.execute(
                    """
                    UPDATE file_ocr_jobs
                    SET status = 'pending', reason = ?, source_sha256 = ?,
                        attempt_count = 0, generation = generation + 1,
                        available_at = ?, lease_token = NULL, lease_owner = NULL,
                        lease_expires_at = NULL, completion_token = NULL,
                        result_status = NULL, result_metadata_json = ?,
                        last_error = NULL, updated_at = ?, completed_at = NULL
                    WHERE id = ? AND user_id = ? AND status = 'failed'
                      AND generation < max_generations
                    """,
                    (
                        str(reason)[:240],
                        str(file_record["sha256"]),
                        now,
                        _json(
                            {
                                "retry_history": retry_history,
                                "retry_trigger": str(reason)[:240],
                            }
                        ),
                        now,
                        existing["id"],
                        user_id,
                    ),
                )
                restarted = conn.execute(
                    "SELECT * FROM file_ocr_jobs WHERE id = ? AND user_id = ?",
                    (existing["id"], user_id),
                ).fetchone()
                if restarted is None:
                    raise KeyError(str(existing["id"]))
                return self._decode_file_ocr_job(restarted)
            if (
                not allow_existing_index
                and str(file_record["status"]) == "indexed"
                and int(file_record["chunk_count"]) > 0
            ):
                return None
            job_id = new_id("ocrjob")
            conn.execute(
                """
                INSERT INTO file_ocr_jobs(
                    id, user_id, file_id, status, reason, source_sha256,
                    attempt_count, max_attempts, generation, max_generations,
                    available_at, result_metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, 0, ?, 1, ?, ?, '{}', ?, ?)
                """,
                (
                    job_id,
                    user_id,
                    file_id,
                    str(reason)[:240],
                    str(file_record["sha256"]),
                    max(1, min(12, int(max_attempts))),
                    max(1, min(5, int(max_generations))),
                    now,
                    now,
                    now,
                ),
            )
            job = conn.execute(
                "SELECT * FROM file_ocr_jobs WHERE id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            return self._decode_file_ocr_job(job)

    def retry_file_ocr_job(
        self,
        file_id: str,
        *,
        reason: str = "explicit_retry",
    ) -> dict[str, Any] | None:
        """Explicitly restart one terminal OCR generation after verifying its blob."""

        return self.enqueue_file_ocr_job(
            file_id,
            reason=reason,
            allow_existing_index=True,
            restart_failed=True,
        )

    def get_file_ocr_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                "SELECT * FROM file_ocr_jobs WHERE id = ? AND user_id = ?",
                (job_id, current_user_id()),
            ).fetchone()
        return self._decode_file_ocr_job(row) if row is not None else None

    def get_file_ocr_job_for_file(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connect().execute(
                "SELECT * FROM file_ocr_jobs WHERE file_id = ? AND user_id = ?",
                (file_id, current_user_id()),
            ).fetchone()
        return self._decode_file_ocr_job(row) if row is not None else None

    def claim_next_file_ocr_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        all_users: bool = False,
    ) -> dict[str, Any] | None:
        """Lease one ready OCR job; owner/system scope is required across tenants."""

        if all_users:
            self._require_system_scope("cross-tenant OCR queue claim")
        worker = str(worker_id or "").strip()
        if not worker:
            raise ValueError("OCR worker_id is required")
        now = utc_now()
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        expires_at = (
            now_dt + timedelta(seconds=max(30, min(3_600, int(lease_seconds))))
        ).isoformat()
        token = new_id("ocrlease")
        with self.transaction(immediate=True) as conn:
            self._reconcile_file_ocr_jobs(conn)
            user_clause = "" if all_users else "AND job.user_id = ?"
            params: tuple[Any, ...] = (now,) if all_users else (now, current_user_id())
            row = conn.execute(
                f"""
                SELECT job.id
                FROM file_ocr_jobs job
                JOIN files f ON f.id = job.file_id AND f.user_id = job.user_id
                JOIN users u ON u.id = job.user_id AND u.status = 'active'
                WHERE job.status IN ('pending', 'retry')
                  AND job.available_at <= ?
                  {user_clause}
                ORDER BY job.available_at ASC, job.created_at ASC, job.rowid ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE file_ocr_jobs
                SET status = 'leased', attempt_count = attempt_count + 1,
                    lease_token = ?, lease_owner = ?, lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ? AND status IN ('pending', 'retry')
                """,
                (token, worker[:160], expires_at, now, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            leased = conn.execute(
                """
                SELECT job.*, f.name AS file_name, f.stored_path, f.mime_type,
                       f.size AS file_size, f.status AS file_status,
                       f.chunk_count AS file_chunk_count
                FROM file_ocr_jobs job
                JOIN files f ON f.id = job.file_id AND f.user_id = job.user_id
                WHERE job.id = ?
                """,
                (row["id"],),
            ).fetchone()
            if leased is None:
                raise KeyError(str(row["id"]))
            return self._decode_file_ocr_job(leased)

    def fail_file_ocr_job(
        self,
        job_id: str,
        lease_token: str,
        error: str,
        *,
        retry_delay_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Release a current lease to bounded retry/backoff without touching file text."""

        user_id = current_user_id()
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM file_ocr_jobs WHERE id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] != "leased" or row["lease_token"] != lease_token:
                raise ValueError("OCR job lease is no longer current")
            attempts = int(row["attempt_count"])
            terminal = attempts >= int(row["max_attempts"])
            delay = (
                max(0, min(86_400, int(retry_delay_seconds)))
                if retry_delay_seconds is not None
                else min(3_600, 5 * (2 ** max(0, attempts - 1)))
            )
            now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
            available_at = (now_dt + timedelta(seconds=delay)).isoformat()
            conn.execute(
                """
                UPDATE file_ocr_jobs
                SET status = ?, available_at = ?, lease_token = NULL,
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE id = ? AND user_id = ? AND status = 'leased' AND lease_token = ?
                """,
                (
                    "failed" if terminal else "retry",
                    available_at,
                    str(error)[:4_000],
                    now,
                    job_id,
                    user_id,
                    lease_token,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM file_ocr_jobs WHERE id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
            if updated is None:
                raise KeyError(job_id)
            return self._decode_file_ocr_job(updated)

    def complete_file_ocr_job(
        self,
        job_id: str,
        lease_token: str,
        text: str,
        *,
        source: str,
        details: dict[str, Any] | None = None,
        warning: str | None = None,
    ) -> dict[str, Any]:
        """Atomically persist OCR text and complete its lease, idempotently by token."""

        user_id = current_user_id()
        now = utc_now()
        with self.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM file_ocr_jobs WHERE id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] == "completed" and row["completion_token"] == lease_token:
                return self._decode_file_ocr_job(row)
            if row["status"] != "leased" or row["lease_token"] != lease_token:
                raise ValueError("OCR job lease is no longer current")
            if str(row["lease_expires_at"] or "") <= now:
                raise ValueError("OCR job lease expired before completion")
            file_record = conn.execute(
                "SELECT * FROM files WHERE id = ? AND user_id = ?",
                (row["file_id"], user_id),
            ).fetchone()
            if file_record is None:
                raise KeyError(str(row["file_id"]))
            if str(file_record["sha256"]) != str(row["source_sha256"]):
                raise ValueError("OCR source file changed after the job was queued")
            source_path = Path(str(file_record["stored_path"]))
            try:
                source_verified = (
                    source_path.is_file()
                    and source_path.stat().st_size == int(file_record["size"])
                    and _file_sha256(source_path) == str(row["source_sha256"])
                )
            except OSError:
                source_verified = False
            if not source_verified:
                raise ValueError("OCR source blob failed content verification")
            result_details = {
                **_loads(row["result_metadata_json"], {}),
                **dict(details or {}),
                "ocr_job_id": job_id,
                "attempt": int(row["attempt_count"]),
                "generation": int(row["generation"]),
            }
            has_good_index = (
                str(file_record["status"]) == "indexed"
                and int(file_record["chunk_count"]) > 0
            )
            suffix = Path(str(file_record["name"] or "")).suffix.casefold()
            mime_type = str(file_record["mime_type"] or "").casefold()
            is_pdf = mime_type == "application/pdf" or suffix == ".pdf"
            if has_good_index and is_pdf:
                self._merge_file_extracted_text_conn(
                    conn,
                    str(row["file_id"]),
                    text,
                    user_id=user_id,
                    source=source,
                    details=result_details,
                    warning=warning,
                )
                result_status = "augmented_existing_index"
            elif has_good_index:
                result_status = "skipped_existing_index"
            else:
                self._persist_file_extracted_text_conn(
                    conn,
                    str(row["file_id"]),
                    text,
                    user_id=user_id,
                    source=source,
                    details=result_details,
                    warning=warning,
                )
                result_status = "indexed"
            conn.execute(
                """
                UPDATE file_ocr_jobs
                SET status = 'completed', lease_token = NULL, lease_owner = NULL,
                    lease_expires_at = NULL, completion_token = ?, result_status = ?,
                    result_metadata_json = ?, last_error = NULL,
                    updated_at = ?, completed_at = ?
                WHERE id = ? AND user_id = ? AND status = 'leased'
                """,
                (
                    lease_token,
                    result_status,
                    _json(result_details),
                    now,
                    now,
                    job_id,
                    user_id,
                ),
            )
            updated = conn.execute(
                "SELECT * FROM file_ocr_jobs WHERE id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
            if updated is None:
                raise KeyError(job_id)
            return self._decode_file_ocr_job(updated)

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
            try:
                self._require_owned_resource(conn, table="files", resource_id=file_id)
                self._replace_file_chunks(conn, file_id, chunks, now=now)
                if status is None:
                    conn.execute(
                        """
                        UPDATE files
                        SET chunk_count = ?, updated_at = ?
                        WHERE id = ? AND user_id = ?
                        """,
                        (len(chunks), now, file_id, current_user_id()),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE files
                        SET chunk_count = ?, status = ?, error = ?, updated_at = ?
                        WHERE id = ? AND user_id = ?
                        """,
                        (
                            len(chunks),
                            status[:40],
                            error,
                            now,
                            file_id,
                            current_user_id(),
                        ),
                    )
                conn.commit()
            except BaseException:  # chunks and metadata are one transaction
                conn.rollback()
                raise

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

        normalized_status = str(status).strip().casefold()
        if normalized_status not in {"indexed", "stored", "failed"}:
            raise ValueError("file index status must be indexed, stored, or failed")
        if (normalized_status == "indexed") != bool(chunks):
            raise ValueError("indexed status requires chunks; non-indexed status forbids chunks")

        now = utc_now()
        with self._lock:
            conn = self.connect()
            try:
                self._require_owned_resource(conn, table="files", resource_id=file_id)
                self._replace_file_chunks(conn, file_id, chunks, now=now)
                conn.execute(
                    """
                    UPDATE files
                    SET name = ?, source_path = ?, stored_path = ?, mime_type = ?, size = ?,
                        chunk_count = ?, status = ?, error = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        name[:260],
                        str(source_path) if source_path else None,
                        str(stored_path),
                        mime_type[:120],
                        int(size),
                        len(chunks),
                        normalized_status,
                        str(error)[:4_000] if error else None,
                        now,
                        file_id,
                        current_user_id(),
                    ),
                )
                conn.commit()
            except BaseException:  # transaction must roll back on any failure
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
        user_id = current_user_id()
        self._require_owned_resource(conn, table="files", resource_id=file_id, user_id=user_id)
        conn.execute(
            "DELETE FROM file_chunks WHERE file_id = ? AND user_id = ?",
            (file_id, user_id),
        )
        if self._file_fts_available:
            conn.execute(
                "DELETE FROM file_chunks_fts WHERE file_id = ? AND user_id = ?",
                (file_id, user_id),
            )
        for position, content in enumerate(chunks, start=1):
            chunk_id = new_id("chunk")
            conn.execute(
                """
                INSERT INTO file_chunks(
                    id, file_id, position, content, char_count, created_at, user_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (chunk_id, file_id, position, content, len(content), now, user_id),
            )
            if self._file_fts_available:
                conn.execute(
                    """
                    INSERT INTO file_chunks_fts(file_id, chunk_id, user_id, content)
                    VALUES (?, ?, ?, ?)
                    """,
                    (file_id, chunk_id, user_id, content),
                )

    def list_files(self, limit: int = 50, *, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connect().execute(
                """
                SELECT
                    id, name, source_path, stored_path, mime_type, size, sha256,
                    status, error, chunk_count, created_at, updated_at
                FROM files
                WHERE user_id = ?
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ? OFFSET ?
                """,
                (current_user_id(), limit, max(0, offset)),
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
                WHERE created_at >= ? AND created_at < ? AND user_id = ?
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
                """,
                (start, end, current_user_id(), max(1, limit)),
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
                WHERE user_id = ? AND ({name_clauses})
                ORDER BY updated_at DESC, rowid DESC
                LIMIT ?
                """,
                (current_user_id(), *name_params, max(100, limit * 20)),
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
                WHERE id = ? AND user_id = ?
                """,
                (file_id, current_user_id()),
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
                WHERE sha256 = ? AND user_id = ?
                ORDER BY
                    CASE WHEN status = 'indexed' THEN 0 ELSE 1 END,
                    CASE WHEN mime_type = 'application/octet-stream' THEN 1 ELSE 0 END,
                    created_at ASC,
                    rowid ASC
                LIMIT 1
                """,
                (sha256, current_user_id()),
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
                JOIN files f ON f.id = c.file_id AND f.user_id = c.user_id
                WHERE c.file_id = ? AND c.user_id = ? AND f.user_id = ?
                ORDER BY c.position ASC
                LIMIT ?
                """,
                (file_id, current_user_id(), current_user_id(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_file_chunks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        clean_query = " ".join(str(query or "").split()).strip()
        requested_limit = max(0, int(limit))
        if not clean_query or requested_limit == 0:
            return []
        user_id = current_user_id()
        terms = _query_terms(clean_query, limit=8)
        folded_terms = [_unicode_search_fold(term) for term in terms]
        fts_terms = [term for term in terms if len(_unicode_search_fold(term)) >= 3]
        seen_ids: set[str] = set()
        results: list[dict[str, Any]] = []

        def add_rows(rows: Iterable[sqlite3.Row]) -> None:
            for row in rows:
                item = dict(row)
                chunk_id = str(item.get("chunk_id") or "")
                if not chunk_id or chunk_id in seen_ids:
                    continue
                seen_ids.add(chunk_id)
                results.append(_decorate_file_hit(item, clean_query))

        fts_returned = False
        if self._file_fts_available and fts_terms:
            match = " OR ".join(f'"{term}"' for term in fts_terms)
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
                        JOIN file_chunks c
                          ON c.id = file_chunks_fts.chunk_id
                         AND c.user_id = file_chunks_fts.user_id
                        JOIN files f ON f.id = c.file_id AND f.user_id = c.user_id
                        WHERE file_chunks_fts MATCH ?
                          AND file_chunks_fts.user_id = ?
                          AND c.user_id = ?
                          AND f.user_id = ?
                        ORDER BY rank ASC, c.position ASC
                        LIMIT ?
                        """,
                        (
                            match,
                            user_id,
                            user_id,
                            user_id,
                            min(500, max(requested_limit * 4, requested_limit)),
                        ),
                    ).fetchall()
                add_rows(rows)
                fts_returned = bool(rows)
            except sqlite3.OperationalError as exc:
                if not _recoverable_fts_error(exc):
                    raise

        content_terms = [term for term in folded_terms if term and len(term) < 3]
        if not self._file_fts_available or not fts_returned:
            content_terms.extend(folded_terms)
            content_terms.append(_unicode_search_fold(clean_query))
        content_terms = list(dict.fromkeys(term for term in content_terms if term))
        name_terms = list(
            dict.fromkeys(
                term
                for term in [*folded_terms, _unicode_search_fold(clean_query)]
                if term
            )
        )
        clauses: list[str] = []
        literal_params: list[Any] = [user_id, user_id]
        for term in content_terms:
            clauses.append("instr(jarvis_search_fold(c.content), ?) > 0")
            literal_params.append(term)
        for term in name_terms:
            clauses.append("instr(jarvis_search_fold(f.name), ?) > 0")
            literal_params.append(term)
        if clauses:
            literal_params.append(min(500, max(requested_limit * 8, requested_limit)))
            with self._lock:
                rows = self.connect().execute(
                    f"""
                    SELECT
                        f.id AS file_id,
                        f.name AS file_name,
                        c.id AS chunk_id,
                        c.position,
                        c.content,
                        c.created_at,
                        NULL AS rank
                    FROM file_chunks c
                    JOIN files f ON f.id = c.file_id AND f.user_id = c.user_id
                    WHERE c.user_id = ? AND f.user_id = ?
                      AND ({" OR ".join(clauses)})
                    ORDER BY f.updated_at DESC, c.position ASC
                    LIMIT ?
                    """,
                    tuple(literal_params),
                ).fetchall()
            add_rows(rows)
        return results[:requested_limit]

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
                JOIN files f ON f.id = c.file_id AND f.user_id = c.user_id
                WHERE c.user_id = ? AND f.user_id = ?
                ORDER BY f.updated_at DESC, f.rowid DESC, c.position ASC
                LIMIT ?
                """,
                (current_user_id(), current_user_id(), limit),
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
            "user_id": current_user_id(),
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
                    requested_action, payload, result, user_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    row["user_id"],
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
        where = "WHERE user_id = ?"
        if status:
            where += " AND status = ?"
            params = (current_user_id(), status, limit)
        else:
            params = (current_user_id(), limit)
        with self._lock:
            rows = self.connect().execute(
                f"""
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result, user_id
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
                WHERE id = ? AND user_id = ?
                """,
                (approval_id, current_user_id()),
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
                WHERE id = ? AND user_id = ?
                """,
                (approval_id, current_user_id()),
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
                WHERE id = ? AND status = ? AND user_id = ?
                """,
                (
                    status,
                    _json(stored_result),
                    now,
                    approval_id,
                    current_status,
                    current_user_id(),
                ),
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
                WHERE id = ? AND user_id = ?
                """,
                (approval_id, current_user_id()),
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
                WHERE id = ? AND user_id = ?
                """,
                (approval_id, current_user_id()),
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
                WHERE id = ? AND status = ? AND user_id = ?
                """,
                (_json(result), now, approval_id, current_status, current_user_id()),
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
                WHERE id = ? AND user_id = ?
                """,
                (approval_id, current_user_id()),
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
                WHERE id = ? AND user_id = ?
                """,
                (approval_id, current_user_id()),
            ).fetchone()
            if before_row is None:
                return None
            before = _approval_record(before_row)
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = 'executing', updated_at = ?
                WHERE id = ? AND status = 'approved' AND user_id = ?
                """,
                (now, approval_id, current_user_id()),
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
                WHERE id = ? AND user_id = ?
                """,
                (approval_id, current_user_id()),
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
                WHERE id = ? AND user_id = ?
                """,
                (approval_id, current_user_id()),
            ).fetchone()
            if before_row is None:
                return None
            before = _approval_record(before_row)
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, result = ?, updated_at = ?
                WHERE id = ? AND status = 'executing' AND user_id = ?
                """,
                (
                    status,
                    _json(sanitized_result),
                    now,
                    approval_id,
                    current_user_id(),
                ),
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
                WHERE id = ? AND user_id = ?
                """,
                (approval_id, current_user_id()),
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

        self._require_system_scope("cross-tenant approval recovery")
        now = utc_now()
        recovered: list[tuple[dict[str, Any], dict[str, Any]]] = []
        with self._lock:
            conn = self.connect()
            rows = conn.execute(
                """
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result, user_id
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
                    WHERE id = ? AND status = 'executing' AND user_id = ?
                    """,
                    (_json(result), now, before["id"], before["user_id"]),
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
                user_id=str(after["user_id"]),
            )
        return [after for _before, after in recovered]

    def pending_approval_reconciliations(
        self,
        *,
        limit: int = 200,
        all_users: bool = False,
    ) -> list[dict[str, Any]]:
        """Return terminal approvals whose mission branch still needs reconciliation."""

        if all_users:
            self._require_system_scope("cross-tenant approval reconciliation")
        with self._lock:
            user_clause = "" if all_users else " AND user_id = ?"
            params: tuple[Any, ...] = () if all_users else (current_user_id(),)
            rows = self.connect().execute(
                f"""
                SELECT
                    id, created_at, updated_at, status, risk, title, description,
                    requested_action, payload, result, user_id
                FROM approvals
                WHERE status IN ('failed', 'rejected', 'cancelled')
                  {user_clause}
                ORDER BY updated_at ASC, rowid ASC
                """,
                params,
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
                  AND user_id = ?
                """,
                (approval_id, current_user_id()),
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
                  AND user_id = ?
                """,
                (
                    _json(updated_result),
                    now,
                    approval_id,
                    raw_result,
                    current_user_id(),
                ),
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
            "SELECT status FROM mission_tasks WHERE mission_id = ? AND user_id = ?",
            (mission_id, current_user_id()),
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
            WHERE id = ? AND user_id = ?
            """,
            (mission_status, progress, now or utc_now(), mission_id, current_user_id()),
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

CREATE TABLE IF NOT EXISTS fts_index_metadata (
    name TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    rebuilt_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_events (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_message TEXT NOT NULL DEFAULT '',
    last_message_at TEXT NOT NULL DEFAULT '',
    unread_count INTEGER NOT NULL DEFAULT 0,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    is_archived INTEGER NOT NULL DEFAULT 0,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    edited_at TEXT,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    reply_to_message_id TEXT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS telegram_operator_sends (
    id TEXT PRIMARY KEY,
    operator_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    client_request_id TEXT NOT NULL,
    realm_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL CHECK(chat_id > 0),
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'delivered', 'failed', 'uncertain')),
    telegram_message_id INTEGER,
    message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    error_code TEXT,
    delivery_claimed_at TEXT,
    delivery_attempt_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(operator_user_id, client_request_id)
);

CREATE TABLE IF NOT EXISTS telegram_message_log (
    id TEXT PRIMARY KEY,
    realm_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL CHECK(chat_id > 0),
    direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
    sender_kind TEXT NOT NULL CHECK(sender_kind IN ('user', 'bot', 'operator')),
    source_key TEXT NOT NULL,
    telegram_message_id INTEGER,
    update_id INTEGER,
    latest_update_id INTEGER,
    conversation_id TEXT,
    user_id TEXT,
    content TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    edited_at TEXT,
    UNIQUE(realm_id, source_key)
);

CREATE TABLE IF NOT EXISTS telegram_turn_deliveries (
    realm_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL CHECK(chat_id > 0),
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('claimed', 'delivered', 'uncertain')),
    claimed_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    PRIMARY KEY(realm_id, chat_id, request_hash)
);

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    importance REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS mission_tasks (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
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
    payload TEXT NOT NULL DEFAULT '{}',
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
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
    updated_at TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS file_chunks (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    content TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS file_ingest_claims (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    file_id TEXT NOT NULL UNIQUE REFERENCES files(id) ON DELETE CASCADE,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY(user_id, sha256)
);

CREATE TABLE IF NOT EXISTS file_index_metadata (
    file_id TEXT PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_upload_intents (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('receiving', 'ready', 'claimed', 'committed', 'failed')),
    name TEXT NOT NULL,
    source_path TEXT,
    part_path TEXT NOT NULL,
    ready_path TEXT NOT NULL,
    final_path TEXT,
    mime_type TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    file_id TEXT REFERENCES files(id) ON DELETE SET NULL,
    created_file INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    committed_at TEXT
);

CREATE TABLE IF NOT EXISTS file_ocr_jobs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL UNIQUE REFERENCES files(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('pending', 'leased', 'retry', 'completed', 'failed')),
    reason TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 4,
    generation INTEGER NOT NULL DEFAULT 1,
    max_generations INTEGER NOT NULL DEFAULT 3,
    available_at TEXT NOT NULL,
    lease_token TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    completion_token TEXT,
    result_status TEXT,
    result_metadata_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
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
    task_id TEXT REFERENCES mission_tasks(id) ON DELETE SET NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
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
    payload TEXT NOT NULL DEFAULT '{}',
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
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
    result TEXT NOT NULL DEFAULT '{}',
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
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
    after_json TEXT NOT NULL DEFAULT '{}',
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_runtime_kv_updated ON runtime_kv(updated_at);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_telegram_operator_sends_chat
ON telegram_operator_sends(realm_id, chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_telegram_operator_sends_user
ON telegram_operator_sends(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_telegram_message_log_chat
ON telegram_message_log(realm_id, chat_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_telegram_message_log_user
ON telegram_message_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_namespace ON memories(namespace, importance);
CREATE INDEX IF NOT EXISTS idx_mission_tasks_mission ON mission_tasks(mission_id, position);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at);
CREATE INDEX IF NOT EXISTS idx_reminders_conversation ON reminders(conversation_id, due_at);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_updated ON files(updated_at);
CREATE INDEX IF NOT EXISTS idx_file_chunks_file ON file_chunks(file_id, position);
CREATE INDEX IF NOT EXISTS idx_file_ingest_claims_file ON file_ingest_claims(file_id);
CREATE INDEX IF NOT EXISTS idx_file_index_metadata_user ON file_index_metadata(user_id);
CREATE INDEX IF NOT EXISTS idx_file_upload_intents_status
ON file_upload_intents(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_file_upload_intents_user
ON file_upload_intents(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_file_ocr_jobs_ready
ON file_ocr_jobs(status, available_at, created_at);
CREATE INDEX IF NOT EXISTS idx_file_ocr_jobs_user
ON file_ocr_jobs(user_id, status, created_at DESC);
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


def _migrate_messenger_schema(conn: sqlite3.Connection) -> None:
    for stmt in _MESSENGER_MIGRATIONS:
        with suppress(sqlite3.OperationalError):
            conn.execute(stmt)


def _migrate_file_reliability_schema(conn: sqlite3.Connection) -> None:
    for stmt in _FILE_RELIABILITY_MIGRATIONS:
        with suppress(sqlite3.OperationalError):
            conn.execute(stmt)


_MESSENGER_MIGRATIONS = [
    "ALTER TABLE conversations ADD COLUMN last_message TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN last_message_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN unread_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN edited_at TEXT",
    "ALTER TABLE messages ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN reply_to_message_id TEXT",
    "ALTER TABLE telegram_operator_sends ADD COLUMN delivery_claimed_at TEXT",
    "ALTER TABLE telegram_operator_sends ADD COLUMN delivery_attempt_count "
    "INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE telegram_message_log ADD COLUMN latest_update_id INTEGER",
]


_FILE_RELIABILITY_MIGRATIONS = [
    "ALTER TABLE file_ocr_jobs ADD COLUMN generation INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE file_ocr_jobs ADD COLUMN max_generations INTEGER NOT NULL DEFAULT 3",
]
