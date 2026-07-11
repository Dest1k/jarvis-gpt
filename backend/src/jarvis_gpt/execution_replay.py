from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPLAY_JOURNAL_PROTOCOL = "jarvis.execution-replay-journal.v1"
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ReplayJournalEntry:
    key: str
    fingerprint: str
    result: dict[str, Any]
    recorded_at: str
    checksum: str


class DurableReplayJournal:
    """Bounded atomic replay ledger for committed execution transactions."""

    def __init__(self, path: Path, *, max_entries: int, max_bytes: int) -> None:
        if not path.is_absolute():
            raise ValueError("replay journal path must be absolute")
        if not 1 <= max_entries <= 1_000_000:
            raise ValueError("replay journal max_entries is outside the supported range")
        if not 1024 * 1024 <= max_bytes <= 2 * 1024 * 1024 * 1024:
            raise ValueError("replay journal max_bytes must be between 1 MiB and 2 GiB")
        self.path = path
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries: OrderedDict[str, ReplayJournalEntry] = OrderedDict()
        self._payload_bytes = 0
        self._generation = 0
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeError("execution replay journal cannot be a symlink")
        self._load()

    def entries(self) -> tuple[ReplayJournalEntry, ...]:
        with self._lock:
            return tuple(self._entries.values())

    def lookup(self, key: str) -> ReplayJournalEntry | None:
        with self._lock:
            return self._entries.get(key)

    def remember(
        self,
        key: str,
        fingerprint: str,
        result: Mapping[str, Any],
        *,
        recorded_at: str | None = None,
    ) -> ReplayJournalEntry:
        normalized = self._make_entry(
            key,
            fingerprint,
            result,
            recorded_at=recorded_at or datetime.now(UTC).isoformat(timespec="milliseconds"),
        )
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise RuntimeError(
                        "execution replay key collides with a different transaction fingerprint"
                    )
                if existing.checksum != normalized.checksum:
                    raise RuntimeError(
                        "execution replay key collides with a different committed result"
                    )
                return existing
            candidate = OrderedDict(self._entries)
            candidate[key] = normalized
            payload_bytes = self._entry_size(normalized) + self._payload_bytes
            while len(candidate) > self.max_entries or payload_bytes > self.max_bytes:
                removed_key, removed = candidate.popitem(last=False)
                del removed_key
                payload_bytes -= self._entry_size(removed)
            if key not in candidate:
                raise ValueError("one execution replay result exceeds journal retention limits")
            self._write_snapshot(candidate, self._generation + 1)
            self._entries = candidate
            self._payload_bytes = payload_bytes
            self._generation += 1
            return normalized

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            metadata = self.path.lstat()
            if self.path.is_symlink() or not self.path.is_file():
                raise RuntimeError("execution replay journal is not a regular file")
            if metadata.st_size > self.max_bytes + 8 * 1024 * 1024:
                raise RuntimeError("execution replay journal exceeds its configured size limit")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or set(payload)
                != {"protocol", "generation", "entries", "snapshot_checksum"}
                or payload.get("protocol") != REPLAY_JOURNAL_PROTOCOL
            ):
                raise RuntimeError("execution replay journal protocol is invalid")
            generation = payload.get("generation")
            raw_entries = payload.get("entries")
            snapshot_checksum = payload.get("snapshot_checksum")
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 0
                or not isinstance(raw_entries, list)
                or len(raw_entries) > self.max_entries
                or generation > 0
                and not raw_entries
                or not isinstance(snapshot_checksum, str)
                or not re.fullmatch(r"[0-9a-f]{64}", snapshot_checksum)
            ):
                raise RuntimeError("execution replay journal metadata is invalid")
            expected_snapshot_checksum = hashlib.sha256(
                self._canonical_snapshot(generation, raw_entries)
            ).hexdigest()
            if not hmac.compare_digest(snapshot_checksum, expected_snapshot_checksum):
                raise RuntimeError("execution replay journal snapshot checksum is invalid")
            entries: OrderedDict[str, ReplayJournalEntry] = OrderedDict()
            payload_bytes = 0
            for raw in raw_entries:
                entry = self._parse_entry(raw)
                existing = entries.get(entry.key)
                if existing is not None:
                    raise RuntimeError("execution replay journal contains a duplicate key")
                entries[entry.key] = entry
                payload_bytes += self._entry_size(entry)
                if payload_bytes > self.max_bytes:
                    raise RuntimeError("execution replay journal payload exceeds retention limits")
            self._entries = entries
            self._payload_bytes = payload_bytes
            self._generation = generation
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise RuntimeError(f"execution replay journal is corrupt: {self.path}") from exc

    def _parse_entry(self, raw: Any) -> ReplayJournalEntry:
        if not isinstance(raw, dict) or set(raw) != {
            "key",
            "fingerprint",
            "result",
            "recorded_at",
            "checksum",
        }:
            raise RuntimeError("execution replay journal entry shape is invalid")
        key = raw["key"]
        fingerprint = raw["fingerprint"]
        result = raw["result"]
        recorded_at = raw["recorded_at"]
        checksum = raw["checksum"]
        entry = self._make_entry(key, fingerprint, result, recorded_at=recorded_at)
        if not isinstance(checksum, str) or not hashlib.sha256(
            self._canonical_entry(entry.key, entry.fingerprint, entry.result, entry.recorded_at)
        ).hexdigest() == checksum:
            raise RuntimeError("execution replay journal entry checksum is invalid")
        return ReplayJournalEntry(key, fingerprint, entry.result, recorded_at, checksum)

    @staticmethod
    def _make_entry(
        key: Any,
        fingerprint: Any,
        result: Any,
        *,
        recorded_at: Any,
    ) -> ReplayJournalEntry:
        if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
            raise RuntimeError("execution replay journal key is invalid")
        if not isinstance(fingerprint, str) or _FINGERPRINT_RE.fullmatch(fingerprint) is None:
            raise RuntimeError("execution replay journal fingerprint is invalid")
        if not isinstance(result, Mapping):
            raise RuntimeError("execution replay journal result must be an object")
        if not isinstance(recorded_at, str) or not 1 <= len(recorded_at) <= 64:
            raise RuntimeError("execution replay journal timestamp is invalid")
        normalized_result = json.loads(
            json.dumps(
                dict(result),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        canonical = DurableReplayJournal._canonical_entry(
            key, fingerprint, normalized_result, recorded_at
        )
        return ReplayJournalEntry(
            key=key,
            fingerprint=fingerprint,
            result=normalized_result,
            recorded_at=recorded_at,
            checksum=hashlib.sha256(canonical).hexdigest(),
        )

    @staticmethod
    def _canonical_entry(
        key: str,
        fingerprint: str,
        result: Mapping[str, Any],
        recorded_at: str,
    ) -> bytes:
        return json.dumps(
            {
                "key": key,
                "fingerprint": fingerprint,
                "result": result,
                "recorded_at": recorded_at,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _entry_size(entry: ReplayJournalEntry) -> int:
        return len(
            DurableReplayJournal._canonical_entry(
                entry.key,
                entry.fingerprint,
                entry.result,
                entry.recorded_at,
            )
        ) + len(entry.checksum)

    def _write_snapshot(
        self,
        entries: OrderedDict[str, ReplayJournalEntry],
        generation: int,
    ) -> None:
        payload = {
            "protocol": REPLAY_JOURNAL_PROTOCOL,
            "generation": generation,
            "entries": [
                {
                    "key": entry.key,
                    "fingerprint": entry.fingerprint,
                    "result": entry.result,
                    "recorded_at": entry.recorded_at,
                    "checksum": entry.checksum,
                }
                for entry in entries.values()
            ],
        }
        payload["snapshot_checksum"] = hashlib.sha256(
            self._canonical_snapshot(generation, payload["entries"])
        ).hexdigest()
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            _fsync_directory(self.path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(OSError):
                temporary.unlink()

    @staticmethod
    def _canonical_snapshot(generation: int, entries: Any) -> bytes:
        return json.dumps(
            {
                "protocol": REPLAY_JOURNAL_PROTOCOL,
                "generation": generation,
                "entries": entries,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
