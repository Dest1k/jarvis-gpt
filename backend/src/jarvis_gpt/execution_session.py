from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class SessionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    ROLLING_BACK = "rolling_back"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


class ProcessStatus(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    TERMINATED = "terminated"


_ALLOWED_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.CREATED: frozenset({SessionStatus.RUNNING, SessionStatus.CANCELLED}),
    SessionStatus.RUNNING: frozenset(
        {
            SessionStatus.WAITING,
            SessionStatus.ROLLING_BACK,
            SessionStatus.SUCCEEDED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        }
    ),
    SessionStatus.WAITING: frozenset(
        {SessionStatus.RUNNING, SessionStatus.FAILED, SessionStatus.CANCELLED}
    ),
    SessionStatus.ROLLING_BACK: frozenset(
        {SessionStatus.FAILED, SessionStatus.CANCELLED, SessionStatus.SUCCEEDED}
    ),
    SessionStatus.SUCCEEDED: frozenset(),
    SessionStatus.FAILED: frozenset(),
    SessionStatus.CANCELLED: frozenset(),
}
_SESSION_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class StepRecord:
    step_id: str
    action: str
    status: StepStatus
    started_at: str
    finished_at: str | None
    duration_ms: int | None
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    pid: int
    parent_pid: int | None
    argv: tuple[str, ...]
    started_at: str
    birth_marker: str
    process_group_id: int | None = None
    status: ProcessStatus = ProcessStatus.RUNNING
    exit_code: int | None = None
    finished_at: str | None = None


@dataclass(slots=True)
class HistoryDigest:
    compressed_steps: int = 0
    first_at: str | None = None
    last_at: str | None = None
    status_counts: Counter[str] = field(default_factory=Counter)
    action_counts: Counter[str] = field(default_factory=Counter)
    facts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def absorb(self, records: list[StepRecord]) -> None:
        if not records:
            return
        self.compressed_steps += len(records)
        self.first_at = self.first_at or records[0].started_at
        self.last_at = records[-1].finished_at or records[-1].started_at
        self.status_counts.update(record.status.value for record in records)
        self.action_counts.update(record.action for record in records)
        if len(self.action_counts) > 64:
            most_common = sorted(
                self.action_counts.items(), key=lambda item: (-item[1], item[0])
            )[:63]
            retained = {key for key, _count in most_common}
            other = sum(
                count for key, count in self.action_counts.items() if key not in retained
            )
            self.action_counts = Counter(dict(most_common))
            self.action_counts["__other__"] += other
        known_facts = set(self.facts)
        known_failures = set(self.failures)
        for record in records:
            fact = _dry_fact(record)
            if fact not in known_facts:
                self.facts.append(fact)
                known_facts.add(fact)
            if record.status is StepStatus.FAILED and record.summary not in known_failures:
                self.failures.append(record.summary)
                known_failures.add(record.summary)
        self.facts[:] = self.facts[-64:]
        self.failures[:] = self.failures[-32:]

    def to_dict(self) -> dict[str, Any]:
        return {
            "compressed_steps": self.compressed_steps,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "status_counts": dict(sorted(self.status_counts.items())),
            "action_counts": dict(sorted(self.action_counts.items())),
            "facts": list(self.facts),
            "failures": list(self.failures),
        }


class ExecutionSession:
    """Thread-safe, bounded in-memory state for one deterministic execution session."""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        max_history_entries: int = 256,
        max_history_bytes: int = 512 * 1024,
        max_process_records: int = 1024,
    ) -> None:
        if not 8 <= max_history_entries <= 100_000:
            raise ValueError("max_history_entries must be between 8 and 100000")
        if not 4096 <= max_history_bytes <= 64 * 1024 * 1024:
            raise ValueError("max_history_bytes must be between 4096 and 67108864")
        if not 8 <= max_process_records <= 100_000:
            raise ValueError("max_process_records must be between 8 and 100000")
        self.session_id = session_id or f"session_{uuid.uuid4().hex}"
        if not _SESSION_ID_RE.fullmatch(self.session_id):
            raise ValueError("session_id contains unsupported characters")
        self.created_at = _utc_now()
        self.updated_at = self.created_at
        self.status = SessionStatus.CREATED
        self.max_history_entries = max_history_entries
        self.max_history_bytes = max_history_bytes
        self.max_process_records = max_process_records
        self._history: list[StepRecord] = []
        self._processes: dict[int, ProcessRecord] = {}
        self._process_start_reservation: str | None = None
        self._cancel_requested = False
        self._digest = HistoryDigest()
        self._lock = threading.RLock()

    def transition(self, target: SessionStatus) -> SessionStatus:
        if not isinstance(target, SessionStatus):
            raise TypeError("target must be a SessionStatus")
        with self._lock:
            if target not in _ALLOWED_TRANSITIONS[self.status]:
                raise ValueError(f"invalid session transition: {self.status} -> {target}")
            self.status = target
            self.updated_at = _utc_now()
            return self.status

    def add_step(
        self,
        *,
        action: str,
        status: StepStatus,
        summary: str,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
        facts: dict[str, Any] | None = None,
        step_id: str | None = None,
    ) -> StepRecord:
        if not isinstance(status, StepStatus):
            raise TypeError("status must be a StepStatus")
        action = _bounded_text(action, 160, "action")
        summary = _bounded_text(summary, 1000, "summary", allow_empty=True)
        if duration_ms is not None and duration_ms < 0:
            raise ValueError("duration_ms cannot be negative")
        record = StepRecord(
            step_id=step_id or f"step_{uuid.uuid4().hex}",
            action=action,
            status=status,
            started_at=started_at or _utc_now(),
            finished_at=finished_at,
            duration_ms=duration_ms,
            summary=summary,
            facts=_sanitize_mapping(facts or {}),
        )
        with self._lock:
            self._history.append(record)
            self.updated_at = _utc_now()
            self._compress_if_needed()
        return record

    def register_process(
        self,
        *,
        pid: int,
        parent_pid: int | None,
        argv: tuple[str, ...],
        reservation_id: str | None = None,
        process_group_id: int | None = None,
    ) -> ProcessRecord:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("pid must be a positive integer")
        if parent_pid is not None and (
            isinstance(parent_pid, bool) or not isinstance(parent_pid, int) or parent_pid <= 0
        ):
            raise ValueError("parent_pid must be a positive integer or None")
        if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
            raise ValueError("argv must contain NUL-free strings")
        birth_marker = _process_birth_marker(pid)
        if birth_marker is None:
            raise RuntimeError(f"cannot establish stable identity for process {pid}")
        if process_group_id is not None and (
            isinstance(process_group_id, bool)
            or not isinstance(process_group_id, int)
            or process_group_id <= 0
        ):
            raise ValueError("process_group_id must be a positive integer or None")
        record = ProcessRecord(
            pid,
            parent_pid,
            tuple(argv),
            _utc_now(),
            birth_marker,
            process_group_id,
        )
        with self._lock:
            if reservation_id is not None:
                if self._process_start_reservation != reservation_id:
                    raise ValueError("process start reservation does not match this action")
                if self.status not in {SessionStatus.RUNNING, SessionStatus.WAITING}:
                    raise ValueError(f"session cannot register a process while {self.status.value}")
                if self._cancel_requested:
                    raise ValueError(
                        "session cancellation was requested before process registration"
                    )
            existing = self._processes.get(pid)
            if existing is not None and existing.status is ProcessStatus.RUNNING:
                raise ValueError(f"process {pid} is already registered")
            if pid not in self._processes and len(self._processes) >= self.max_process_records:
                removable = min(
                    (
                        item
                        for item in self._processes.values()
                        if item.status is not ProcessStatus.RUNNING
                    ),
                    key=lambda item: item.started_at,
                    default=None,
                )
                if removable is None:
                    raise RuntimeError("session process record capacity is exhausted")
                del self._processes[removable.pid]
            self._processes[pid] = record
            if reservation_id is not None:
                self._process_start_reservation = None
            self.updated_at = _utc_now()
        return record

    def authorize_process_resume(self, pid: int) -> None:
        """Atomically reject a registered-but-suspended process after cancellation."""

        with self._lock:
            record = self._processes.get(pid)
            if record is None or record.status is not ProcessStatus.RUNNING:
                raise ValueError(f"process {pid} is not registered as running")
            if self._cancel_requested or self.status is not SessionStatus.RUNNING:
                raise ValueError("session cancellation was requested before process resume")

    def reserve_process_start(self, action_id: str) -> None:
        action_id = _bounded_text(action_id, 128, "action_id")
        with self._lock:
            if self.status is SessionStatus.CREATED:
                self.status = SessionStatus.RUNNING
            if self.status is not SessionStatus.RUNNING or self._cancel_requested:
                raise ValueError(f"session is not accepting process starts: {self.status.value}")
            if self._process_start_reservation is not None or any(
                record.status is ProcessStatus.RUNNING for record in self._processes.values()
            ):
                raise ValueError("session already owns an active or starting process")
            self._process_start_reservation = action_id
            self.updated_at = _utc_now()

    def has_process_start_reservation(self, action_id: str) -> bool:
        with self._lock:
            return self._process_start_reservation == action_id

    def release_process_start(self, action_id: str) -> None:
        with self._lock:
            if self._process_start_reservation == action_id:
                self._process_start_reservation = None
                self.updated_at = _utc_now()

    def process_start_pending(self) -> bool:
        with self._lock:
            return self._process_start_reservation is not None

    def request_process_cancellation(self) -> None:
        with self._lock:
            if self.status is SessionStatus.CREATED:
                self._cancel_requested = True
                self.status = SessionStatus.CANCELLED
            elif self.status is SessionStatus.RUNNING:
                if self._process_start_reservation is None and not any(
                    record.status is ProcessStatus.RUNNING
                    for record in self._processes.values()
                ):
                    raise ValueError("session has no cancellable process operation")
                self._cancel_requested = True
                self.status = SessionStatus.WAITING
            elif self.status in {SessionStatus.WAITING, SessionStatus.CANCELLED}:
                self._cancel_requested = True
            elif self.status in {SessionStatus.SUCCEEDED, SessionStatus.FAILED}:
                raise ValueError(f"session is already terminal: {self.status.value}")
            else:
                raise ValueError(f"session cannot be cancelled while {self.status.value}")
            self.updated_at = _utc_now()

    def complete_process_cancellation(self) -> None:
        with self._lock:
            if self._process_start_reservation is not None or any(
                record.status is ProcessStatus.RUNNING for record in self._processes.values()
            ):
                raise RuntimeError("session still owns an active or starting process")
            if self.status is SessionStatus.WAITING:
                self.status = SessionStatus.CANCELLED
            self.updated_at = _utc_now()

    def has_failed_steps(self) -> bool:
        with self._lock:
            return any(record.status is StepStatus.FAILED for record in self._history)

    def finish_process(
        self,
        pid: int,
        *,
        exit_code: int | None,
        terminated: bool = False,
    ) -> ProcessRecord:
        with self._lock:
            existing = self._processes.get(pid)
            if existing is None:
                raise KeyError(f"process {pid} is not owned by session {self.session_id}")
            if existing.status is not ProcessStatus.RUNNING:
                raise ValueError(f"process {pid} is already {existing.status}")
            updated = ProcessRecord(
                pid=existing.pid,
                parent_pid=existing.parent_pid,
                argv=existing.argv,
                started_at=existing.started_at,
                birth_marker=existing.birth_marker,
                process_group_id=existing.process_group_id,
                status=ProcessStatus.TERMINATED if terminated else ProcessStatus.EXITED,
                exit_code=exit_code,
                finished_at=_utc_now(),
            )
            self._processes[pid] = updated
            self.updated_at = updated.finished_at or self.updated_at
            return updated

    def owns_running_pid(self, pid: int) -> bool:
        with self._lock:
            record = self._processes.get(pid)
            return record is not None and record.status is ProcessStatus.RUNNING

    def running_pids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(
                sorted(
                    record.pid
                    for record in self._processes.values()
                    if record.status is ProcessStatus.RUNNING
                )
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            processes = sorted(self._processes.values(), key=lambda item: item.pid)
            return {
                "session_id": self.session_id,
                "status": self.status.value,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "history": [asdict(record) for record in self._history],
                "history_digest": self._digest.to_dict(),
                "processes": [asdict(record) for record in processes],
                "running_pids": list(self.running_pids()),
                "process_start_pending": self._process_start_reservation is not None,
                "cancel_requested": self._cancel_requested,
            }

    def _compress_if_needed(self) -> None:
        while (
            len(self._history) > self.max_history_entries
            or self._history_size() > self.max_history_bytes
        ):
            count = max(1, min(len(self._history) - 1, max(2, len(self._history) // 4)))
            self._digest.absorb(self._history[:count])
            del self._history[:count]

    def _history_size(self) -> int:
        payload = [asdict(record) for record in self._history]
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


class SessionRegistry:
    """Owns live sessions and enforces that PID controls stay session-scoped."""

    def __init__(self, *, max_sessions: int = 256) -> None:
        if not 1 <= max_sessions <= 10_000:
            raise ValueError("max_sessions must be between 1 and 10000")
        self.max_sessions = max_sessions
        self._sessions: dict[str, ExecutionSession] = {}
        self._lock = threading.RLock()

    def create(self, **kwargs: Any) -> ExecutionSession:
        session = ExecutionSession(**kwargs)
        with self._lock:
            if session.session_id in self._sessions:
                raise ValueError(f"execution session already exists: {session.session_id}")
            if len(self._sessions) >= self.max_sessions:
                removable = next(
                    (
                        key
                        for key, value in self._sessions.items()
                        if value.status
                        in {SessionStatus.SUCCEEDED, SessionStatus.FAILED, SessionStatus.CANCELLED}
                    ),
                    None,
                )
                if removable is None:
                    raise RuntimeError("all session slots are active")
                del self._sessions[removable]
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> ExecutionSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            sessions = tuple(self._sessions.values())
        return tuple(
            session.snapshot()
            for session in sorted(sessions, key=lambda item: item.created_at, reverse=True)
        )

    def snapshot(self, session_id: str) -> dict[str, Any] | None:
        session = self.get(session_id)
        return session.snapshot() if session is not None else None

    def require_owned_pid(self, session_id: str, pid: int) -> ExecutionSession:
        session = self.get(session_id)
        if session is None:
            raise KeyError(f"unknown execution session: {session_id}")
        if not session.owns_running_pid(pid):
            raise PermissionError(f"process {pid} is not a live process owned by {session_id}")
        with session._lock:
            record = session._processes[pid]
        if _process_birth_marker(pid) != record.birth_marker:
            raise PermissionError(f"process {pid} identity no longer matches its ownership record")
        return session

    def signal_owned_pid(
        self,
        session_id: str,
        pid: int,
        selected_signal: int,
        *,
        finalize: bool = True,
    ) -> None:
        session = self.require_owned_pid(session_id, pid)
        with session._lock:
            record = session._processes[pid]
        _signal_exact_process(
            pid,
            record.birth_marker,
            selected_signal,
            process_group_id=record.process_group_id,
        )
        if finalize:
            session.finish_process(pid, exit_code=None, terminated=True)

    def owned_pid_alive(self, session_id: str, pid: int) -> bool:
        session = self.get(session_id)
        if session is None:
            return False
        with session._lock:
            record = session._processes.get(pid)
        return bool(
            record is not None
            and record.status is ProcessStatus.RUNNING
            and _process_birth_marker(pid) == record.birth_marker
        )

    def owned_process_tree_alive(self, session_id: str, pid: int) -> bool:
        session = self.get(session_id)
        if session is None:
            return False
        with session._lock:
            record = session._processes.get(pid)
        if record is None:
            return False
        if os.name != "nt" and record.process_group_id is not None:
            try:
                os.killpg(record.process_group_id, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
        return self.owned_pid_alive(session_id, pid)


def _dry_fact(record: StepRecord) -> str:
    summary = " ".join(record.summary.split())[:240]
    return f"{record.action}|{record.status.value}|{summary}"


def _sanitize_mapping(value: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:32]:
        clean_key = _bounded_text(str(key), 100, "fact key")
        result[clean_key] = _sanitize_value(item, depth=0)
    return result


def _sanitize_value(value: Any, *, depth: int) -> Any:
    if depth >= 3:
        return _bounded_text(str(value), 300, "fact value", allow_empty=True)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, list | tuple):
        return [_sanitize_value(item, depth=depth + 1) for item in value[:32]]
    if isinstance(value, dict):
        return {
            str(key)[:100]: _sanitize_value(item, depth=depth + 1)
            for key, item in list(value.items())[:32]
        }
    return _bounded_text(str(value), 300, "fact value", allow_empty=True)


def _bounded_text(value: str, limit: int, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{name} must be a NUL-free string")
    value = value.strip()
    if not allow_empty and not value:
        raise ValueError(f"{name} cannot be empty")
    return value[:limit]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _process_birth_marker(pid: int) -> str | None:
    if os.name == "nt":
        handle = _windows_open_process(pid, 0x1000)
        if not handle:
            return None
        try:
            return _windows_creation_marker(handle)
        finally:
            _windows_close_handle(handle)
    stat_path = f"/proc/{pid}/stat"
    try:
        raw = Path(stat_path).read_text(encoding="utf-8")
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split()
        return f"procfs:{fields[19]}"
    except (OSError, IndexError):
        return _portable_process_birth_marker(pid)


def _portable_process_birth_marker(pid: int) -> str | None:
    executable = shutil.which("ps")
    if executable is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed OS inspection argv
            [executable, "-o", "lstart=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
            env={"LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = " ".join(completed.stdout.split())
    return f"ps:{started}" if completed.returncode == 0 and started else None


def _signal_exact_process(
    pid: int,
    expected_marker: str,
    selected_signal: int,
    *,
    process_group_id: int | None = None,
) -> None:
    if os.name == "nt":
        handle = _windows_open_process(pid, 0x0001 | 0x1000)
        if not handle:
            raise ProcessLookupError(pid)
        try:
            if _windows_creation_marker(handle) != expected_marker:
                raise PermissionError("process identity changed before signal delivery")
            if selected_signal == signal.SIGINT:
                ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.GenerateConsoleCtrlEvent.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
                kernel32.GenerateConsoleCtrlEvent.restype = ctypes.c_int
                if not kernel32.GenerateConsoleCtrlEvent(ctrl_break, pid):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                kernel32.TerminateProcess.restype = ctypes.c_int
                if not kernel32.TerminateProcess(handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            _windows_close_handle(handle)
        return
    if process_group_id is not None:
        if _process_birth_marker(pid) != expected_marker:
            raise PermissionError("process identity changed before group signal delivery")
        os.killpg(process_group_id, selected_signal)
        return
    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
        descriptor = os.pidfd_open(pid)
        try:
            if _process_birth_marker(pid) != expected_marker:
                raise PermissionError("process identity changed before signal delivery")
            signal.pidfd_send_signal(descriptor, selected_signal)
        finally:
            os.close(descriptor)
        return
    if _process_birth_marker(pid) != expected_marker:
        raise PermissionError("process identity changed before signal delivery")
    os.kill(pid, selected_signal)


def _windows_open_process(pid: int, access: int) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    return int(kernel32.OpenProcess(access, False, pid) or 0)


def _windows_creation_marker(handle: int) -> str:
    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    creation = FileTime()
    exit_time = FileTime()
    kernel = FileTime()
    user = FileTime()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return f"win:{creation.high:08x}{creation.low:08x}"


def _windows_close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle(handle)
