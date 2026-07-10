from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class TerminationReason(StrEnum):
    EXITED = "exited"
    STALLED = "stalled"
    TIMED_OUT = "timed_out"
    START_FAILED = "start_failed"


@dataclass(frozen=True, slots=True)
class ProcessNode:
    pid: int
    parent_pid: int | None
    executable: str | None = None


@dataclass(frozen=True, slots=True)
class PermissionSnapshot:
    identity: str
    elevated: bool
    can_read_cwd: bool
    can_write_cwd: bool
    can_execute_cwd: bool


@dataclass(frozen=True, slots=True)
class FilesystemEntry:
    path: str
    kind: str
    size: int
    mtime_ns: int
    mode: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class FilesystemDiff:
    created: tuple[FilesystemEntry, ...] = ()
    modified: tuple[FilesystemEntry, ...] = ()
    deleted: tuple[FilesystemEntry, ...] = ()
    scan_truncated: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.created or self.modified or self.deleted)


@dataclass(frozen=True, slots=True)
class StreamCapture:
    text: str
    total_bytes: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ExecutionFeedback:
    ok: bool
    argv: tuple[str, ...]
    pid: int | None
    exit_code: int | None
    termination_reason: TerminationReason
    started_at: str
    finished_at: str
    duration_ms: int
    stdout: StreamCapture
    stderr: StreamCapture
    pid_tree: tuple[ProcessNode, ...]
    permissions: PermissionSnapshot
    filesystem_diff: FilesystemDiff
    interrupt_sent: bool = False
    kill_sent: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ActionFeedback:
    ok: bool
    action_id: str
    kind: str
    summary: str
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    process: ExecutionFeedback | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
