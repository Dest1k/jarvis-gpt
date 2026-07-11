from __future__ import annotations

import asyncio
import contextlib
import ctypes
import getpass
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from select import select as select_select
from types import MappingProxyType
from typing import Final

from .execution_filesystem import (
    PathMutationGuard,
    directory_entries,
    metadata_identity,
    verified_binary_handle,
)
from .execution_models import (
    ExecutionFeedback,
    FilesystemDiff,
    FilesystemEntry,
    PermissionSnapshot,
    ProcessNode,
    StreamCapture,
    TerminationReason,
)
from .execution_session import ExecutionSession

_MAX_ARGUMENTS: Final = 512
_MAX_ARGUMENT_LENGTH: Final = 32_768
_MAX_ENVIRONMENT_ENTRIES: Final = 512
_STREAM_READ_SIZE: Final = 64 * 1024
_POSIX_SUPERVISOR_FLAG: Final = "--jarvis-process-supervisor-v1"
_POSIX_CONTROL_LIMIT: Final = 64 * 1024 * 1024
_DEFAULT_DENIED_EXECUTABLES: Final = frozenset(
    {
        "bash",
        "cmd",
        "cmd.exe",
        "csh",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "sh",
        "wsl",
        "wsl.exe",
        "zsh",
    }
)


@dataclass(frozen=True, slots=True)
class ExecutableRule:
    executable: Path
    argument_patterns: tuple[str, ...] = ()
    additional_argument_pattern: str | None = None
    environment_patterns: tuple[tuple[str, str], ...] = ()
    expected_sha256: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        resolved = self.executable.resolve(strict=True)
        object.__setattr__(self, "executable", resolved)
        object.__setattr__(self, "expected_sha256", _pinned_executable_sha256(resolved))

    def validate(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> None:
        import re

        expected = self.executable.resolve(strict=True)
        if executable != expected:
            raise ValueError("executable does not match this rule")
        if not hmac.compare_digest(
            _pinned_executable_sha256(executable), self.expected_sha256
        ):
            raise ValueError("executable content changed since capability policy load")
        if len(arguments) < len(self.argument_patterns):
            raise ValueError("argv does not satisfy the executable capability grammar")
        if self.additional_argument_pattern is None and len(arguments) != len(
            self.argument_patterns
        ):
            raise ValueError("argv has arguments outside the executable capability grammar")
        for index, pattern in enumerate(self.argument_patterns):
            if re.fullmatch(pattern, arguments[index]) is None:
                raise ValueError(f"argv[{index}] violates the executable capability grammar")
        if self.additional_argument_pattern is not None:
            tail = arguments[len(self.argument_patterns) :]
            for index, argument in enumerate(tail, start=len(self.argument_patterns)):
                if re.fullmatch(self.additional_argument_pattern, argument) is None:
                    raise ValueError(
                        f"argv[{index}] violates the executable capability tail grammar"
                    )
        environment_rules = dict(self.environment_patterns)
        unknown_environment = sorted(set(environment) - set(environment_rules))
        if unknown_environment:
            raise ValueError(
                "process environment key is outside the executable capability grammar: "
                + unknown_environment[0]
            )
        for name, value in environment.items():
            if re.fullmatch(environment_rules[name], value) is None:
                raise ValueError(
                    f"process environment value violates the capability grammar: {name}"
                )


@dataclass(frozen=True, slots=True)
class ExecutablePolicy:
    rules: tuple[ExecutableRule, ...] = ()
    allowed_paths: tuple[Path, ...] = ()
    allowed_names: frozenset[str] = frozenset()
    denied_names: frozenset[str] = _DEFAULT_DENIED_EXECUTABLES

    def validate(
        self,
        executable: Path,
        arguments: tuple[str, ...],
        environment: Mapping[str, str],
    ) -> None:
        name = executable.name.casefold()
        if name in {item.casefold() for item in self.denied_names}:
            raise ValueError(f"shell interpreters are not executable actions: {name}")
        if self.rules:
            rule = next(
                (
                    item
                    for item in self.rules
                    if item.executable.resolve(strict=True) == executable
                ),
                None,
            )
            if rule is None:
                raise ValueError(f"executable is outside the configured rule set: {executable}")
            rule.validate(executable, arguments, environment)
        elif self.allowed_paths:
            allowed_paths = {path.resolve(strict=True) for path in self.allowed_paths}
            if executable not in allowed_paths:
                raise ValueError(f"executable is outside the configured allowlist: {executable}")
        if self.allowed_names and name not in {item.casefold() for item in self.allowed_names}:
            raise ValueError(f"executable name is outside the configured allowlist: {name}")


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    executable: str | Path
    arguments: tuple[str, ...] = ()
    cwd: Path | None = None
    environment: Mapping[str, str] = field(default_factory=dict)
    inherit_environment: bool = False
    timeout_seconds: float | None = 300.0
    stall_timeout_seconds: float | None = None
    interrupt_grace_seconds: float = 3.0
    kill_grace_seconds: float = 3.0
    max_output_bytes: int = 2 * 1024 * 1024
    observe_paths: tuple[Path, ...] = ()
    max_observed_entries: int = 4096
    sensitive_argument_indices: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.executable, str | Path):
            raise TypeError("executable must be a string or Path")
        if not isinstance(self.arguments, tuple):
            raise TypeError("arguments must be a tuple")
        if self.cwd is not None and not isinstance(self.cwd, Path):
            raise TypeError("cwd must be a Path or None")
        if not isinstance(self.environment, Mapping):
            raise TypeError("environment must be a mapping")
        if not isinstance(self.observe_paths, tuple) or any(
            not isinstance(path, Path) for path in self.observe_paths
        ):
            raise TypeError("observe_paths must be a tuple of Path values")
        if not isinstance(self.sensitive_argument_indices, frozenset):
            raise TypeError("sensitive_argument_indices must be a frozenset")
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    def validate(self) -> None:
        executable = os.fspath(self.executable)
        _validate_text(executable, name="executable", allow_empty=False)
        if len(self.arguments) > _MAX_ARGUMENTS:
            raise ValueError(f"arguments cannot contain more than {_MAX_ARGUMENTS} items")
        for index, argument in enumerate(self.arguments):
            if not isinstance(argument, str):
                raise TypeError(f"arguments[{index}] must be a string")
            _validate_text(argument, name=f"arguments[{index}]", allow_empty=True)
        if self.cwd is not None:
            if not self.cwd.is_absolute():
                raise ValueError("cwd must be an absolute path")
            if not self.cwd.is_dir():
                raise ValueError("cwd must be an existing directory")
        if len(self.environment) > _MAX_ENVIRONMENT_ENTRIES:
            raise ValueError("environment contains too many entries")
        for key, value in self.environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("environment keys and values must be strings")
            if not key or "=" in key or "\x00" in key or "\x00" in value:
                raise ValueError(f"invalid environment entry: {key!r}")
        _validate_positive_optional(self.timeout_seconds, "timeout_seconds")
        _validate_positive_optional(self.stall_timeout_seconds, "stall_timeout_seconds")
        _validate_positive(self.interrupt_grace_seconds, "interrupt_grace_seconds")
        _validate_positive(self.kill_grace_seconds, "kill_grace_seconds")
        if not 1024 <= self.max_output_bytes <= 64 * 1024 * 1024:
            raise ValueError("max_output_bytes must be between 1024 and 67108864")
        if not 1 <= self.max_observed_entries <= 100_000:
            raise ValueError("max_observed_entries must be between 1 and 100000")
        for path in self.observe_paths:
            if not path.is_absolute():
                raise ValueError("observe_paths must contain only absolute paths")
        if any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= len(self.arguments)
            for index in self.sensitive_argument_indices
        ):
            raise ValueError("sensitive_argument_indices contains an invalid argument index")


class _TailBuffer:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self.total = 0

    def append(self, data: bytes) -> None:
        self.total += len(data)
        self._data.extend(data)
        overflow = len(self._data) - self._limit
        if overflow > 0:
            del self._data[:overflow]

    def capture(self) -> StreamCapture:
        return StreamCapture(
            text=self._data.decode("utf-8", errors="replace"),
            total_bytes=self.total,
            truncated=self.total > len(self._data),
        )


class _WindowsJob:
    def __init__(self, handle: int) -> None:
        self.handle = handle

    @classmethod
    def assign(cls, pid: int) -> _WindowsJob | None:
        if os.name != "nt":
            return None

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000
        if not kernel32.SetInformationJobObject(
            job,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise ctypes.WinError(error)
        process_handle = kernel32.OpenProcess(0x0001 | 0x0100, False, pid)
        if not process_handle:
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise ctypes.WinError(error)
        try:
            if not kernel32.AssignProcessToJobObject(job, process_handle):
                error = ctypes.get_last_error()
                kernel32.CloseHandle(job)
                raise ctypes.WinError(error)
        finally:
            kernel32.CloseHandle(process_handle)
        return cls(job)

    def terminate(self, exit_code: int = 1) -> None:
        if not self.handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        kernel32.TerminateJobObject.restype = ctypes.c_int
        if not kernel32.TerminateJobObject(self.handle, exit_code):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if not self.handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.CloseHandle(self.handle)
        self.handle = 0


class _PosixSupervisor:
    """Authenticated-by-inheritance control channel for a supervised POSIX tree."""

    def __init__(
        self,
        *,
        process: asyncio.subprocess.Process,
        control: socket.socket,
        target_pid: int,
    ) -> None:
        self.process = process
        self.control = control
        self.target_pid = target_pid
        self._buffer = bytearray()
        self._closed = False

    async def resume(self) -> None:
        if self._closed:
            raise RuntimeError("POSIX process supervisor control channel is closed")
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(self.control, b"G")
        response = await _recv_supervisor_line(self.control, self._buffer)
        if response == "STARTED":
            return
        if response.startswith("ERROR "):
            raise OSError(response.removeprefix("ERROR "))
        raise RuntimeError(f"invalid POSIX process supervisor response: {response!r}")

    def send_signal(self, selected_signal: int) -> bool:
        if self.process.returncode is not None:
            return False
        try:
            os.kill(self.process.pid, selected_signal)
            return True
        except (OSError, ProcessLookupError, ValueError):
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError):
            self.control.close()


async def _spawn_posix_supervised_process(
    *,
    argv: tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str],
    executable_fd: int,
) -> tuple[asyncio.subprocess.Process, _PosixSupervisor]:
    if not sys.platform.startswith("linux"):
        raise OSError(
            "process.run requires Linux subreaper containment on POSIX platforms"
        )
    parent_control, child_control = socket.socketpair()
    child_control.set_inheritable(True)
    payload = json.dumps(
        {
            "argv": list(argv),
            "cwd": str(cwd),
            "environment": dict(environment),
            "executable_fd": executable_fd,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _POSIX_CONTROL_LIMIT:
        parent_control.close()
        child_control.close()
        raise ValueError("POSIX process supervisor payload is too large")

    wrapper_environment = _posix_supervisor_environment()
    spawn_task = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-P",
            "-m",
            "jarvis_gpt.execution_process",
            _POSIX_SUPERVISOR_FLAG,
            str(child_control.fileno()),
            str(os.getpid()),
            cwd=str(Path(__file__).resolve().parent.parent),
            env=wrapper_environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=(child_control.fileno(), executable_fd),
            start_new_session=True,
        ),
        name="execution-posix-supervisor-spawn",
    )
    process: asyncio.subprocess.Process | None = None
    pending_cancellation: asyncio.CancelledError | None = None
    try:
        while True:
            try:
                process = await asyncio.shield(spawn_task)
                break
            except asyncio.CancelledError as exc:
                pending_cancellation = exc
                if spawn_task.done():
                    process = spawn_task.result()
                    break
        child_control.close()
        parent_control.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(parent_control, len(payload).to_bytes(8, "big") + payload)
        buffer = bytearray()
        response = await _recv_supervisor_line(parent_control, buffer)
        if not response.startswith("READY "):
            if response.startswith("ERROR "):
                raise OSError(response.removeprefix("ERROR "))
            raise RuntimeError(f"invalid POSIX process supervisor response: {response!r}")
        target_pid = int(response.removeprefix("READY "))
        if target_pid <= 0 or target_pid == process.pid:
            raise RuntimeError("POSIX process supervisor returned an invalid target PID")
        supervisor = _PosixSupervisor(
            process=process,
            control=parent_control,
            target_pid=target_pid,
        )
        supervisor._buffer.extend(buffer)
        if pending_cancellation is not None:
            await _cleanup_cancelled_posix_spawn(supervisor)
            raise pending_cancellation
        return process, supervisor
    except BaseException:
        parent_control.close()
        child_control.close()
        if process is not None and process.returncode is None:
            cleanup = asyncio.create_task(
                _cleanup_raw_posix_supervisor(process),
                name=f"execution-posix-supervisor-abort-{process.pid}",
            )
            await _await_cleanup_task(cleanup)
        raise


async def _recv_supervisor_line(control: socket.socket, buffer: bytearray) -> str:
    loop = asyncio.get_running_loop()
    while True:
        newline = buffer.find(b"\n")
        if newline >= 0:
            raw = bytes(buffer[:newline])
            del buffer[: newline + 1]
            return raw.decode("utf-8", errors="replace")
        if len(buffer) > 16 * 1024:
            raise RuntimeError("POSIX process supervisor response exceeded its limit")
        chunk = await loop.sock_recv(control, 4096)
        if not chunk:
            raise RuntimeError("POSIX process supervisor closed its control channel")
        buffer.extend(chunk)


async def _cleanup_cancelled_posix_spawn(supervisor: _PosixSupervisor) -> None:
    supervisor.send_signal(signal.SIGTERM)
    supervisor.close()
    cleanup = asyncio.create_task(
        supervisor.process.wait(),
        name=f"execution-posix-supervisor-cancel-{supervisor.process.pid}",
    )
    await _await_cleanup_task(cleanup)


async def _cleanup_raw_posix_supervisor(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.kill(process.pid, signal.SIGKILL)
        await process.wait()


async def _await_cleanup_task(task: asyncio.Task[object]) -> None:
    while True:
        try:
            await asyncio.shield(task)
            return
        except asyncio.CancelledError:
            if task.done():
                task.result()
                return


def _posix_supervisor_environment() -> dict[str, str]:
    import_paths = [str(Path(__file__).resolve().parent.parent)]
    for item in sys.path:
        if not item:
            item = os.getcwd()
        candidate = os.path.abspath(item)
        if candidate not in import_paths:
            import_paths.append(candidate)
    # This wrapper is an infrastructure process, not the requested target.
    # Giving it the backend environment would let a same-UID target read API
    # keys from /proc/$PPID/environ even when inherit_environment is false.
    return {
        "PYTHONPATH": os.pathsep.join(import_paths),
        "PYTHONUNBUFFERED": "1",
        "PYTHONSAFEPATH": "1",
    }


class AsyncProcessRunner:
    """Runs a validated argv directly; a command shell is never involved."""

    def __init__(
        self,
        *,
        executable_policy: ExecutablePolicy | None = None,
        observation_roots: tuple[Path, ...] = (),
    ) -> None:
        self.executable_policy = executable_policy or ExecutablePolicy()
        self.observation_roots = tuple(root.resolve(strict=True) for root in observation_roots)

    async def run(
        self,
        request: ProcessRequest,
        *,
        session: ExecutionSession | None = None,
        reservation_id: str | None = None,
    ) -> ExecutionFeedback:
        request.validate()
        executable = _resolve_executable(request.executable, request.cwd)
        self.executable_policy.validate(executable, request.arguments, request.environment)
        self._validate_observation_paths(request.observe_paths)
        argv = (str(executable), *request.arguments)
        feedback_argv = (str(executable), *_redact_arguments(request))
        started_at = _utc_now()
        started_clock = time.monotonic()
        cwd = request.cwd or Path.cwd()
        permissions = permission_snapshot(cwd)
        before, before_truncated = await asyncio.to_thread(
            filesystem_snapshot,
            request.observe_paths,
            request.max_observed_entries,
            self.observation_roots,
        )
        stdout = _TailBuffer(request.max_output_bytes)
        stderr = _TailBuffer(request.max_output_bytes)
        environment = dict(os.environ) if request.inherit_environment else {}
        environment.update(request.environment)

        if session is not None:
            if reservation_id is None:
                raise ValueError("session-bound process execution requires a reservation_id")
            if not session.has_process_start_reservation(reservation_id):
                session.reserve_process_start(reservation_id)

        executable_fd = _open_pinned_executable(executable)
        try:
            # Re-evaluate the capability while the selected executable object is
            # pinned against replacement (Windows) or bound by fd (Linux).
            self.executable_policy.validate(executable, request.arguments, request.environment)
            if os.name == "nt":
                creationflags = int(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                ) | int(getattr(subprocess, "CREATE_SUSPENDED", 0x00000004))
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(cwd),
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=creationflags,
                )
                posix_supervisor = None
            else:
                process, posix_supervisor = await _spawn_posix_supervised_process(
                    argv=argv,
                    cwd=cwd,
                    environment=environment,
                    executable_fd=executable_fd,
                )
        except asyncio.CancelledError:
            if session is not None and reservation_id is not None:
                session.release_process_start(reservation_id)
            raise
        except (OSError, ValueError) as exc:
            if session is not None and reservation_id is not None:
                session.release_process_start(reservation_id)
            after, after_truncated = await asyncio.to_thread(
                filesystem_snapshot,
                request.observe_paths,
                request.max_observed_entries,
                self.observation_roots,
            )
            return ExecutionFeedback(
                ok=False,
                argv=feedback_argv,
                pid=None,
                exit_code=None,
                termination_reason=TerminationReason.START_FAILED,
                started_at=started_at,
                finished_at=_utc_now(),
                duration_ms=_duration_ms(started_clock),
                stdout=stdout.capture(),
                stderr=stderr.capture(),
                pid_tree=(),
                permissions=permissions,
                filesystem_diff=filesystem_diff(
                    before,
                    after,
                    scan_truncated=before_truncated or after_truncated,
                ),
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            os.close(executable_fd)

        try:
            job = _WindowsJob.assign(process.pid)
        except OSError as exc:
            if session is not None and reservation_id is not None:
                session.release_process_start(reservation_id)
            return await _abort_started_process(
                process=process,
                job=None,
                posix_supervisor=posix_supervisor,
                stdout=stdout,
                stderr=stderr,
                request=request,
                feedback_argv=feedback_argv,
                started_at=started_at,
                started_clock=started_clock,
                permissions=permissions,
                before=before,
                before_truncated=before_truncated,
                observation_roots=self.observation_roots,
                error=exc,
                session=None,
                session_pid=None,
            )

        session_pid = posix_supervisor.target_pid if posix_supervisor is not None else process.pid
        if session is not None:
            process_registered = False
            try:
                session.register_process(
                    pid=session_pid,
                    parent_pid=process.pid if posix_supervisor is not None else os.getpid(),
                    argv=feedback_argv,
                    reservation_id=reservation_id,
                    process_group_id=session_pid if posix_supervisor is not None else None,
                )
                process_registered = True
                session.authorize_process_resume(session_pid)
            except Exception as exc:
                if reservation_id is not None:
                    session.release_process_start(reservation_id)
                return await _abort_started_process(
                    process=process,
                    job=job,
                    posix_supervisor=posix_supervisor,
                    stdout=stdout,
                    stderr=stderr,
                    request=request,
                    feedback_argv=feedback_argv,
                    started_at=started_at,
                    started_clock=started_clock,
                    permissions=permissions,
                    before=before,
                    before_truncated=before_truncated,
                    observation_roots=self.observation_roots,
                    error=exc,
                    session=session if process_registered else None,
                    session_pid=session_pid if process_registered else None,
                )

        try:
            if os.name == "nt":
                _resume_windows_process(process.pid)
            elif posix_supervisor is not None:
                await posix_supervisor.resume()
        except BaseException as exc:
            feedback = await _abort_started_process(
                process=process,
                job=job,
                posix_supervisor=posix_supervisor,
                stdout=stdout,
                stderr=stderr,
                request=request,
                feedback_argv=feedback_argv,
                started_at=started_at,
                started_clock=started_clock,
                permissions=permissions,
                before=before,
                before_truncated=before_truncated,
                observation_roots=self.observation_roots,
                error=exc,
                session=session,
                session_pid=session_pid,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise exc
            return feedback

        last_activity = [time.monotonic()]
        stdout_task = asyncio.create_task(
            _pump_stream(process.stdout, stdout, last_activity),
            name=f"execution-stdout-{process.pid}",
        )
        stderr_task = asyncio.create_task(
            _pump_stream(process.stderr, stderr, last_activity),
            name=f"execution-stderr-{process.pid}",
        )
        wait_task = asyncio.create_task(process.wait(), name=f"execution-wait-{process.pid}")
        tree: dict[int, ProcessNode] = {
            session_pid: ProcessNode(
                session_pid,
                process.pid if posix_supervisor is not None else os.getpid(),
                str(executable),
            )
        }
        stop_sampler = asyncio.Event()
        sampler_task = asyncio.create_task(
            _sample_process_tree(process.pid, tree, stop_sampler),
            name=f"execution-tree-{process.pid}",
        )
        reason = TerminationReason.EXITED
        interrupt_sent = False
        kill_sent = False
        pending_error: BaseException | None = None
        try:
            while not wait_task.done():
                await asyncio.wait({wait_task}, timeout=0.05)
                if wait_task.done():
                    break
                now = time.monotonic()
                if (
                    request.timeout_seconds is not None
                    and now - started_clock >= request.timeout_seconds
                ):
                    reason = TerminationReason.TIMED_OUT
                    interrupt_sent, kill_sent = await _stop_process(
                        process,
                        wait_task,
                        request.interrupt_grace_seconds,
                        request.kill_grace_seconds,
                        job,
                        posix_supervisor,
                    )
                    break
                if (
                    request.stall_timeout_seconds is not None
                    and now - last_activity[0] >= request.stall_timeout_seconds
                ):
                    reason = TerminationReason.STALLED
                    interrupt_sent, kill_sent = await _stop_process(
                        process,
                        wait_task,
                        request.interrupt_grace_seconds,
                        request.kill_grace_seconds,
                        job,
                        posix_supervisor,
                    )
                    break
            await asyncio.shield(wait_task)
        except BaseException as exc:
            pending_error = exc

        completion_task = asyncio.create_task(
            _complete_started_process(
                process=process,
                job=job,
                posix_supervisor=posix_supervisor,
                session_pid=session_pid,
                stdout=stdout,
                stderr=stderr,
                stdout_task=stdout_task,
                stderr_task=stderr_task,
                wait_task=wait_task,
                sampler_task=sampler_task,
                stop_sampler=stop_sampler,
                tree=tree,
                request=request,
                feedback_argv=feedback_argv,
                started_at=started_at,
                started_clock=started_clock,
                permissions=permissions,
                before=before,
                before_truncated=before_truncated,
                observation_roots=self.observation_roots,
                reason=reason,
                interrupt_sent=interrupt_sent,
                kill_sent=kill_sent,
                stop_process=pending_error is not None,
                session=session,
            ),
            name=f"execution-finalize-{process.pid}",
        )
        feedback, cleanup_cancellation = await _await_critical_feedback(completion_task)
        if cleanup_cancellation is not None:
            raise cleanup_cancellation from pending_error
        if pending_error is not None:
            raise pending_error
        return feedback

    def _validate_observation_paths(self, paths: tuple[Path, ...]) -> None:
        if paths and not self.observation_roots:
            raise ValueError("observe_paths require configured observation_roots")
        for path in paths:
            resolved = path.resolve(strict=False)
            if not any(
                resolved == root or resolved.is_relative_to(root)
                for root in self.observation_roots
            ):
                raise ValueError(f"observed path escapes configured roots: {path}")


async def _complete_started_process(
    *,
    process: asyncio.subprocess.Process,
    job: _WindowsJob | None,
    posix_supervisor: _PosixSupervisor | None,
    session_pid: int,
    stdout: _TailBuffer,
    stderr: _TailBuffer,
    stdout_task: asyncio.Task[None],
    stderr_task: asyncio.Task[None],
    wait_task: asyncio.Task[int],
    sampler_task: asyncio.Task[None],
    stop_sampler: asyncio.Event,
    tree: dict[int, ProcessNode],
    request: ProcessRequest,
    feedback_argv: tuple[str, ...],
    started_at: str,
    started_clock: float,
    permissions: PermissionSnapshot,
    before: Mapping[str, FilesystemEntry],
    before_truncated: bool,
    observation_roots: tuple[Path, ...],
    reason: TerminationReason,
    interrupt_sent: bool,
    kill_sent: bool,
    stop_process: bool,
    session: ExecutionSession | None,
) -> ExecutionFeedback:
    stream_tasks = (stdout_task, stderr_task, sampler_task)
    terminated = reason is not TerminationReason.EXITED or stop_process
    try:
        if stop_process and not wait_task.done():
            cleanup_interrupt, cleanup_kill = await _stop_process(
                process,
                wait_task,
                request.interrupt_grace_seconds,
                request.kill_grace_seconds,
                job,
                posix_supervisor,
            )
            interrupt_sent = interrupt_sent or cleanup_interrupt
            kill_sent = kill_sent or cleanup_kill

        if not wait_task.done():
            await asyncio.shield(wait_task)

        stop_sampler.set()
        if os.name != "nt" and posix_supervisor is None:
            remaining_group_killed = await _terminate_remaining_process_group(
                process.pid,
                interrupt_grace=min(request.interrupt_grace_seconds, 1.0),
                kill_grace=min(request.kill_grace_seconds, 1.0),
            )
            kill_sent = kill_sent or remaining_group_killed

        try:
            await asyncio.wait_for(
                asyncio.gather(*stream_tasks, return_exceptions=True),
                timeout=2.0,
            )
        except TimeoutError:
            for task in stream_tasks:
                task.cancel()
            await asyncio.gather(*stream_tasks, return_exceptions=True)

        after, after_truncated = await asyncio.to_thread(
            filesystem_snapshot,
            request.observe_paths,
            request.max_observed_entries,
            observation_roots,
        )
        exit_code = process.returncode
        return ExecutionFeedback(
            ok=reason is TerminationReason.EXITED and not stop_process and exit_code == 0,
            argv=feedback_argv,
            pid=session_pid,
            exit_code=exit_code,
            termination_reason=reason,
            started_at=started_at,
            finished_at=_utc_now(),
            duration_ms=_duration_ms(started_clock),
            stdout=stdout.capture(),
            stderr=stderr.capture(),
            pid_tree=tuple(sorted(tree.values(), key=lambda item: item.pid)),
            permissions=permissions,
            filesystem_diff=filesystem_diff(
                before,
                after,
                scan_truncated=before_truncated or after_truncated,
            ),
            interrupt_sent=interrupt_sent,
            kill_sent=kill_sent,
        )
    finally:
        stop_sampler.set()
        if job is not None:
            job.close()
        if posix_supervisor is not None:
            posix_supervisor.close()
        if process.returncode is not None:
            _finish_session_process(
                session,
                session_pid,
                process.returncode,
                terminated=terminated,
            )


async def _await_critical_feedback(
    task: asyncio.Task[ExecutionFeedback],
) -> tuple[ExecutionFeedback, asyncio.CancelledError | None]:
    """Wait for cleanup despite repeated caller cancellation, then report cancellation."""

    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as exc:
            cancellation = exc
            if task.done():
                return task.result(), cancellation


async def _abort_started_process(
    *,
    process: asyncio.subprocess.Process,
    job: _WindowsJob | None,
    posix_supervisor: _PosixSupervisor | None,
    stdout: _TailBuffer,
    stderr: _TailBuffer,
    request: ProcessRequest,
    feedback_argv: tuple[str, ...],
    started_at: str,
    started_clock: float,
    permissions: PermissionSnapshot,
    before: Mapping[str, FilesystemEntry],
    before_truncated: bool,
    observation_roots: tuple[Path, ...],
    error: BaseException,
    session: ExecutionSession | None,
    session_pid: int | None,
) -> ExecutionFeedback:
    abort_task = asyncio.create_task(
        _abort_started_process_critical(
            process=process,
            job=job,
            posix_supervisor=posix_supervisor,
            stdout=stdout,
            stderr=stderr,
            request=request,
            feedback_argv=feedback_argv,
            started_at=started_at,
            started_clock=started_clock,
            permissions=permissions,
            before=before,
            before_truncated=before_truncated,
            observation_roots=observation_roots,
            error=error,
            session=session,
            session_pid=session_pid,
        ),
        name=f"execution-abort-{process.pid}",
    )
    feedback, cancellation = await _await_critical_feedback(abort_task)
    if cancellation is not None:
        raise cancellation
    return feedback


async def _abort_started_process_critical(
    *,
    process: asyncio.subprocess.Process,
    job: _WindowsJob | None,
    posix_supervisor: _PosixSupervisor | None,
    stdout: _TailBuffer,
    stderr: _TailBuffer,
    request: ProcessRequest,
    feedback_argv: tuple[str, ...],
    started_at: str,
    started_clock: float,
    permissions: PermissionSnapshot,
    before: Mapping[str, FilesystemEntry],
    before_truncated: bool,
    observation_roots: tuple[Path, ...],
    error: BaseException,
    session: ExecutionSession | None,
    session_pid: int | None,
) -> ExecutionFeedback:
    try:
        try:
            if job is not None:
                job.terminate()
            elif posix_supervisor is not None:
                posix_supervisor.send_signal(signal.SIGTERM)
                # The supervisor may still be waiting for its one-byte start
                # grant. Closing the inherited channel is the authoritative
                # abort signal and prevents PEP 475 from restarting recv forever.
                posix_supervisor.close()
            elif os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            elif process.returncode is None:
                process.kill()
        except OSError:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
        communicate_task = asyncio.create_task(process.communicate())
        try:
            captured_stdout, captured_stderr = await asyncio.wait_for(
                asyncio.shield(communicate_task), timeout=5.0
            )
        except TimeoutError:
            if posix_supervisor is not None:
                await asyncio.to_thread(_emergency_kill_posix_supervisor, posix_supervisor)
            elif job is not None:
                with contextlib.suppress(OSError):
                    job.terminate()
            elif process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            captured_stdout, captured_stderr = await communicate_task
        if captured_stdout:
            stdout.append(captured_stdout)
        if captured_stderr:
            stderr.append(captured_stderr)
        after, after_truncated = await asyncio.to_thread(
            filesystem_snapshot,
            request.observe_paths,
            request.max_observed_entries,
            observation_roots,
        )
        return ExecutionFeedback(
            ok=False,
            argv=feedback_argv,
            pid=session_pid or process.pid,
            exit_code=process.returncode,
            termination_reason=TerminationReason.START_FAILED,
            started_at=started_at,
            finished_at=_utc_now(),
            duration_ms=_duration_ms(started_clock),
            stdout=stdout.capture(),
            stderr=stderr.capture(),
            pid_tree=(
                ProcessNode(
                    session_pid or process.pid,
                    process.pid if posix_supervisor is not None else os.getpid(),
                    feedback_argv[0],
                ),
            ),
            permissions=permissions,
            filesystem_diff=filesystem_diff(
                before,
                after,
                scan_truncated=before_truncated or after_truncated,
            ),
            kill_sent=True,
            error=f"{type(error).__name__}: {error}",
        )
    finally:
        if job is not None:
            job.close()
        if posix_supervisor is not None:
            posix_supervisor.close()
        if process.returncode is not None:
            _finish_session_process(
                session,
                session_pid or process.pid,
                process.returncode,
                terminated=True,
            )


async def _pump_stream(
    stream: asyncio.StreamReader | None,
    destination: _TailBuffer,
    last_activity: list[float],
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(_STREAM_READ_SIZE)
        if not chunk:
            return
        destination.append(chunk)
        last_activity[0] = time.monotonic()


async def _sample_process_tree(
    root_pid: int,
    aggregate: dict[int, ProcessNode],
    stopped: asyncio.Event,
) -> None:
    while not stopped.is_set():
        with contextlib.suppress(OSError):
            for node in await asyncio.to_thread(process_tree_snapshot, root_pid):
                aggregate[node.pid] = node
        try:
            await asyncio.wait_for(stopped.wait(), timeout=0.2)
        except TimeoutError:
            continue


async def _stop_process(
    process: asyncio.subprocess.Process,
    wait_task: asyncio.Task[int],
    interrupt_grace: float,
    kill_grace: float,
    job: _WindowsJob | None,
    posix_supervisor: _PosixSupervisor | None = None,
) -> tuple[bool, bool]:
    if wait_task.done():
        return False, False
    interrupt_sent = _send_interrupt(process, posix_supervisor)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(wait_task), timeout=interrupt_grace)
        return interrupt_sent, False
    if job is not None:
        job.terminate()
    elif posix_supervisor is not None:
        posix_supervisor.send_signal(signal.SIGTERM)
    elif os.name != "nt":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    try:
        await asyncio.wait_for(asyncio.shield(wait_task), timeout=kill_grace)
    except TimeoutError as exc:
        if posix_supervisor is not None:
            await asyncio.to_thread(_emergency_kill_posix_supervisor, posix_supervisor)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=1.0)
            if wait_task.done():
                return interrupt_sent, True
        raise RuntimeError(f"process {process.pid} did not terminate after kill") from exc
    return interrupt_sent, True


def _send_interrupt(
    process: asyncio.subprocess.Process,
    posix_supervisor: _PosixSupervisor | None = None,
) -> bool:
    if posix_supervisor is not None:
        return posix_supervisor.send_signal(signal.SIGINT)
    try:
        if os.name == "nt":
            ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGINT)
            process.send_signal(ctrl_break)
        else:
            os.killpg(process.pid, signal.SIGINT)
        return True
    except (OSError, ProcessLookupError, ValueError):
        try:
            process.send_signal(signal.SIGINT)
            return True
        except (OSError, ProcessLookupError, ValueError):
            return False


def _resume_windows_process(pid: int) -> None:
    if os.name != "nt":
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.OpenProcess(0x0800 | 0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = int(ntdll.NtResumeProcess(handle))
        if status != 0:
            raise OSError(f"NtResumeProcess failed with NTSTATUS 0x{status & 0xFFFFFFFF:08x}")
    finally:
        kernel32.CloseHandle(handle)


async def _terminate_remaining_process_group(
    process_group_id: int,
    *,
    interrupt_grace: float,
    kill_grace: float,
) -> bool:
    if not _process_group_alive(process_group_id):
        return False
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGTERM)
    deadline = time.monotonic() + interrupt_grace
    while _process_group_alive(process_group_id) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    if not _process_group_alive(process_group_id):
        return False
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group_id, signal.SIGKILL)
    deadline = time.monotonic() + kill_grace
    while _process_group_alive(process_group_id) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    return True


def _process_group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _posix_process_supervisor_main(control_fd: int, expected_parent_pid: int) -> int:
    control = socket.socket(fileno=control_fd)
    pending_signals: list[int] = []

    def record_signal(selected_signal: int, _frame: object) -> None:
        pending_signals.append(selected_signal)

    for selected_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected_signal, record_signal)

    target_pid: int | None = None
    try:
        _enable_posix_supervisor_kernel_guards(expected_parent_pid)
        payload = _read_posix_supervisor_payload(control)
        argv = payload.get("argv")
        cwd = payload.get("cwd")
        environment = payload.get("environment")
        executable_fd = payload.get("executable_fd")
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(item, str) or "\x00" in item for item in argv)
            or not isinstance(cwd, str)
            or not os.path.isabs(cwd)
            or "\x00" in cwd
            or not isinstance(environment, dict)
            or not isinstance(executable_fd, int)
            or executable_fd < 3
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or "=" in key
                or "\x00" in key
                or "\x00" in value
                for key, value in environment.items()
            )
        ):
            raise ValueError("invalid supervised process payload")

        start_read, start_write = os.pipe()
        ready_read, ready_write = os.pipe()
        exec_status_read, exec_status_write = os.pipe()
        target_pid = os.fork()
        if target_pid == 0:
            _run_posix_supervised_target(
                control=control,
                start_read=start_read,
                start_write=start_write,
                ready_read=ready_read,
                ready_write=ready_write,
                exec_status_read=exec_status_read,
                exec_status_write=exec_status_write,
                argv=argv,
                cwd=cwd,
                environment=environment,
                executable_fd=executable_fd,
            )
        os.close(executable_fd)
        os.close(start_read)
        os.close(ready_write)
        os.close(exec_status_write)
        try:
            if _read_exact_fd(ready_read, 1) != b"R":
                raise RuntimeError("supervised target failed before session creation")
        finally:
            os.close(ready_read)
        control.sendall(f"READY {target_pid}\n".encode("ascii"))
        command = _recv_exact_socket(control, 1)
        if command != b"G" or pending_signals:
            raise RuntimeError("supervised target start was cancelled")
        os.write(start_write, b"G")
        os.close(start_write)
        exec_error = _read_limited_fd(exec_status_read, 16 * 1024)
        os.close(exec_status_read)
        if exec_error:
            message = " ".join(exec_error.decode("utf-8", errors="replace").split())[:1000]
            control.sendall(f"ERROR {message}\n".encode())
            _hard_kill_supervisor_descendants(os.getpid())
            _reap_supervisor_children()
            return 127
        control.sendall(b"STARTED\n")
        return _monitor_posix_supervised_target(control, target_pid, pending_signals)
    except BaseException as exc:
        with contextlib.suppress(OSError):
            message = " ".join(f"{type(exc).__name__}: {exc}".split())[:1000]
            control.sendall(f"ERROR {message}\n".encode("utf-8", errors="replace"))
        if target_pid is not None and target_pid > 0:
            _hard_kill_supervisor_descendants(os.getpid())
            _reap_supervisor_children()
        return 127
    finally:
        with contextlib.suppress(OSError):
            control.close()


def _run_posix_supervised_target(
    *,
    control: socket.socket,
    start_read: int,
    start_write: int,
    ready_read: int,
    ready_write: int,
    exec_status_read: int,
    exec_status_write: int,
    argv: list[str],
    cwd: str,
    environment: dict[str, str],
    executable_fd: int,
) -> None:
    try:
        control.close()
        os.close(start_write)
        os.close(ready_read)
        os.close(exec_status_read)
        for selected_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(selected_signal, signal.SIG_DFL)
        os.setsid()
        _set_linux_parent_death_signal(signal.SIGKILL, expected_parent_pid=os.getppid())
        os.write(ready_write, b"R")
        os.close(ready_write)
        if _read_exact_fd(start_read, 1) != b"G":
            raise RuntimeError("supervised target did not receive its start grant")
        os.close(start_read)
        os.chdir(cwd)
        os.execve(f"/proc/self/fd/{executable_fd}", argv, environment)
    except BaseException as exc:
        message = f"{type(exc).__name__}: {exc}".encode("utf-8", errors="replace")[:16_000]
        with contextlib.suppress(OSError):
            os.write(exec_status_write, message)
        os._exit(127)


def _monitor_posix_supervised_target(
    control: socket.socket,
    target_pid: int,
    pending_signals: list[int],
) -> int:
    control.setblocking(False)
    target_status: int | None = None
    while target_status is None:
        while pending_signals:
            selected_signal = pending_signals.pop(0)
            if selected_signal == signal.SIGINT:
                _signal_supervisor_descendants(os.getpid(), signal.SIGINT)
                continue
            _hard_kill_supervisor_descendants(os.getpid())
            _reap_supervisor_children()
            return 128 + int(selected_signal)

        try:
            readable, _, _ = select_select((control,), (), (), 0.02)
        except (OSError, ValueError):
            readable = (control,)
        if readable:
            try:
                unexpected = control.recv(1)
            except BlockingIOError:
                unexpected = b"x"
            if not unexpected:
                _hard_kill_supervisor_descendants(os.getpid())
                _reap_supervisor_children()
                return 128 + int(signal.SIGTERM)
            _hard_kill_supervisor_descendants(os.getpid())
            _reap_supervisor_children()
            return 127

        for child_pid, status in _reap_supervisor_children():
            if child_pid == target_pid:
                target_status = status
    _hard_kill_supervisor_descendants(os.getpid())
    _reap_supervisor_children()
    return _exit_code_from_wait_status(target_status)


def _enable_posix_supervisor_kernel_guards(expected_parent_pid: int) -> None:
    if expected_parent_pid <= 0 or os.getppid() != expected_parent_pid:
        raise RuntimeError("process supervisor parent identity changed during startup")
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")
    if prctl(1, int(signal.SIGTERM), 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG failed")
    if os.getppid() != expected_parent_pid:
        raise RuntimeError("process supervisor parent exited during startup")


def _set_linux_parent_death_signal(selected_signal: int, *, expected_parent_pid: int) -> None:
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, int(selected_signal), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "target PR_SET_PDEATHSIG failed")
    if os.getppid() != expected_parent_pid:
        raise RuntimeError("process supervisor exited during target startup")


def _read_posix_supervisor_payload(control: socket.socket) -> dict[str, object]:
    raw_size = _recv_exact_socket(control, 8)
    size = int.from_bytes(raw_size, "big")
    if not 2 <= size <= _POSIX_CONTROL_LIMIT:
        raise ValueError("invalid process supervisor payload size")
    decoded = json.loads(_recv_exact_socket(control, size).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("process supervisor payload must be an object")
    return decoded


def _recv_exact_socket(control: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = control.recv(size - len(chunks))
        if not chunk:
            raise EOFError("process supervisor parent control channel closed")
        chunks.extend(chunk)
    return bytes(chunks)


def _read_exact_fd(descriptor: int, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = os.read(descriptor, size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _read_limited_fd(descriptor: int, limit: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < limit:
        chunk = os.read(descriptor, min(4096, limit - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _supervisor_descendant_pids(root_pid: int) -> tuple[int, ...]:
    processes = _procfs_processes() if Path("/proc").is_dir() else _portable_ps_processes()
    by_parent: dict[int, list[int]] = {}
    for node in processes:
        if node.parent_pid is not None:
            by_parent.setdefault(node.parent_pid, []).append(node.pid)
    pending = list(by_parent.get(root_pid, ()))
    descendants: list[int] = []
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen or pid == root_pid:
            continue
        seen.add(pid)
        descendants.append(pid)
        pending.extend(by_parent.get(pid, ()))
    return tuple(descendants)


def _signal_supervisor_descendants(root_pid: int, selected_signal: int) -> None:
    for pid in reversed(_supervisor_descendant_pids(root_pid)):
        _signal_pid_safely(pid, selected_signal)


def _hard_kill_supervisor_descendants(root_pid: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        descendants = _supervisor_descendant_pids(root_pid)
        if not descendants:
            return
        for pid in reversed(descendants):
            _signal_pid_safely(pid, signal.SIGSTOP)
        for pid in reversed(descendants):
            _signal_pid_safely(pid, signal.SIGKILL)
        _reap_supervisor_children()
        time.sleep(0.01)


def _signal_pid_safely(pid: int, selected_signal: int) -> None:
    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
        descriptor: int | None = None
        try:
            descriptor = os.pidfd_open(pid)
            signal.pidfd_send_signal(descriptor, selected_signal)
            return
        except (OSError, ProcessLookupError):
            return
        finally:
            if descriptor is not None:
                os.close(descriptor)
    with contextlib.suppress(OSError, ProcessLookupError):
        os.kill(pid, selected_signal)


def _reap_supervisor_children() -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return result
        if pid == 0:
            return result
        result.append((pid, status))


def _exit_code_from_wait_status(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return 127


def _emergency_kill_posix_supervisor(supervisor: _PosixSupervisor) -> None:
    supervisor.close()
    descendants = process_tree_snapshot(supervisor.process.pid)
    for node in reversed(descendants):
        if node.pid != supervisor.process.pid:
            _signal_pid_safely(node.pid, signal.SIGKILL)
    _signal_pid_safely(supervisor.process.pid, signal.SIGKILL)


def process_tree_snapshot(root_pid: int) -> tuple[ProcessNode, ...]:
    processes = _windows_processes() if os.name == "nt" else _procfs_processes()
    by_parent: dict[int, list[ProcessNode]] = {}
    by_pid = {node.pid: node for node in processes}
    for node in processes:
        if node.parent_pid is not None:
            by_parent.setdefault(node.parent_pid, []).append(node)
    root = by_pid.get(root_pid, ProcessNode(root_pid, None, None))
    result: list[ProcessNode] = []
    pending = [root]
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if node.pid in seen:
            continue
        seen.add(node.pid)
        result.append(node)
        pending.extend(by_parent.get(node.pid, ()))
    return tuple(result)


def _procfs_processes() -> tuple[ProcessNode, ...]:
    proc = Path("/proc")
    if not proc.is_dir():
        return _portable_ps_processes()
    result: list[ProcessNode] = []
    for item in proc.iterdir():
        if not item.name.isdigit():
            continue
        try:
            raw = (item / "stat").read_text(encoding="utf-8")
            closing = raw.rfind(")")
            fields = raw[closing + 2 :].split()
            parent_pid = int(fields[1])
            executable = (item / "comm").read_text(encoding="utf-8").strip()
            result.append(ProcessNode(int(item.name), parent_pid, executable))
        except (OSError, ValueError, IndexError):
            continue
    return tuple(result)


def _portable_ps_processes() -> tuple[ProcessNode, ...]:
    executable = shutil.which("ps")
    if executable is None:
        return ()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed OS inspection argv
            [executable, "-axo", "pid=,ppid=,comm="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
            env={"LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if completed.returncode != 0:
        return ()
    result: list[ProcessNode] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) < 2:
            continue
        try:
            pid = int(fields[0])
            parent_pid = int(fields[1])
        except ValueError:
            continue
        result.append(
            ProcessNode(
                pid=pid,
                parent_pid=parent_pid,
                executable=fields[2] if len(fields) > 2 else None,
            )
        )
    return tuple(result)


def _windows_processes() -> tuple[ProcessNode, ...]:
    if os.name != "nt":
        return ()

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        return ()
    entry = ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(entry)
    result: list[ProcessNode] = []
    try:
        success = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while success:
            result.append(
                ProcessNode(
                    pid=int(entry.th32ProcessID),
                    parent_pid=int(entry.th32ParentProcessID),
                    executable=str(entry.szExeFile),
                )
            )
            success = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(result)


def permission_snapshot(cwd: Path) -> PermissionSnapshot:
    elevated = False
    if os.name == "nt":
        try:
            elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            elevated = False
    elif hasattr(os, "geteuid"):
        elevated = os.geteuid() == 0
    return PermissionSnapshot(
        identity=getpass.getuser(),
        elevated=elevated,
        can_read_cwd=os.access(cwd, os.R_OK),
        can_write_cwd=os.access(cwd, os.W_OK),
        can_execute_cwd=os.access(cwd, os.X_OK),
    )


def filesystem_snapshot(
    roots: Sequence[Path],
    max_entries: int,
    allowed_roots: Sequence[Path] = (),
) -> tuple[dict[str, FilesystemEntry], bool]:
    if not roots:
        return {}, False
    if allowed_roots:
        return _guarded_filesystem_snapshot(roots, max_entries, allowed_roots)
    entries: dict[str, FilesystemEntry] = {}
    truncated = False
    pending = [path.resolve(strict=False) for path in roots]
    while pending:
        path = pending.pop()
        key = str(path)
        if key in entries:
            continue
        if len(entries) >= max_entries:
            truncated = True
            break
        try:
            metadata = path.lstat()
        except OSError:
            continue
        kind = (
            "symlink"
            if stat.S_ISLNK(metadata.st_mode)
            else "directory"
            if path.is_dir()
            else "file"
        )
        digest = None
        if kind == "file" and metadata.st_size <= 2 * 1024 * 1024:
            try:
                digest = _sha256_file(path)
            except OSError:
                digest = None
        entries[key] = FilesystemEntry(
            path=key,
            kind=kind,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            mode=stat.S_IMODE(metadata.st_mode),
            sha256=digest,
        )
        if kind == "directory":
            try:
                pending.extend(child for child in path.iterdir())
            except OSError:
                continue
    return entries, truncated


def _guarded_filesystem_snapshot(
    roots: Sequence[Path],
    max_entries: int,
    allowed_roots: Sequence[Path],
) -> tuple[dict[str, FilesystemEntry], bool]:
    """Snapshot only handle-anchored, no-follow objects below allowed roots."""

    normalized_roots = tuple(Path(root).resolve(strict=True) for root in allowed_roots)
    identities = {
        root: metadata_identity(root.stat(follow_symlinks=False)) for root in normalized_roots
    }
    entries: dict[str, FilesystemEntry] = {}
    truncated = False
    with PathMutationGuard(normalized_roots, (), identities) as guard:
        pending = [Path(path) for path in roots]
        while pending:
            path = pending.pop()
            key = str(path)
            if key in entries:
                continue
            if len(entries) >= max_entries:
                truncated = True
                break
            try:
                bound = guard.bind(path, allow_root=True)
                metadata = bound.lstat()
            except (OSError, RuntimeError, ValueError):
                truncated = True
                continue
            kind = (
                "symlink"
                if stat.S_ISLNK(metadata.st_mode)
                else "directory"
                if stat.S_ISDIR(metadata.st_mode)
                else "file"
            )
            digest = None
            if kind == "file" and metadata.st_size <= 2 * 1024 * 1024:
                try:
                    digest = _sha256_bound_file(bound)
                except (OSError, RuntimeError, ValueError):
                    truncated = True
            entries[key] = FilesystemEntry(
                path=key,
                kind=kind,
                size=metadata.st_size,
                mtime_ns=metadata.st_mtime_ns,
                mode=stat.S_IMODE(metadata.st_mode),
                sha256=digest,
            )
            if kind == "directory":
                try:
                    remaining = max(1, max_entries - len(entries) + 1)
                    children = directory_entries(bound, max_entries=remaining)
                except (OSError, RuntimeError, ValueError):
                    truncated = True
                    continue
                if len(children) >= remaining:
                    truncated = True
                pending.extend(path / child.name for child in children[:remaining])
    return entries, truncated


def _sha256_bound_file(path) -> str:
    digest = hashlib.sha256()
    with verified_binary_handle(path) as handle:
        while chunk := handle.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def filesystem_diff(
    before: Mapping[str, FilesystemEntry],
    after: Mapping[str, FilesystemEntry],
    *,
    scan_truncated: bool = False,
) -> FilesystemDiff:
    before_paths = set(before)
    after_paths = set(after)
    created = tuple(after[path] for path in sorted(after_paths - before_paths))
    deleted = tuple(before[path] for path in sorted(before_paths - after_paths))
    modified = tuple(
        after[path]
        for path in sorted(before_paths & after_paths)
        if before[path] != after[path]
    )
    return FilesystemDiff(
        created=created,
        modified=modified,
        deleted=deleted,
        scan_truncated=scan_truncated,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_executable(executable: str | Path, cwd: Path | None) -> Path:
    raw = os.fspath(executable)
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=True)
    elif candidate.parent != Path("."):
        if cwd is None:
            raise ValueError("relative executable paths require cwd")
        resolved = (cwd / candidate).resolve(strict=True)
    else:
        found = shutil.which(raw)
        if found is None:
            raise ValueError(f"executable was not found: {raw}")
        resolved = Path(found).resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"executable is not a file: {resolved}")
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise ValueError(f"executable is not executable: {resolved}")
    return resolved


def _open_pinned_executable(executable: Path) -> int:
    """Pin the validated image through CreateProcess or Linux fd-based exec."""

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(executable, flags)
    try:
        opened = os.fstat(descriptor)
        current = executable.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or metadata_identity(opened) != metadata_identity(current)
        ):
            raise RuntimeError("executable identity changed while it was pinned")
        if os.name != "nt" and not os.access(executable, os.X_OK):
            raise PermissionError("pinned executable is not executable")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _pinned_executable_sha256(executable: Path) -> str:
    descriptor = _open_pinned_executable(executable)
    try:
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 128 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _redact_arguments(request: ProcessRequest) -> tuple[str, ...]:
    secret_flag = re.compile(
        r"(?i)^--?(?:password|passwd|secret|token|credential|api[-_]?key|private[-_]?key)(?:=|$)"
    )
    redacted: list[str] = []
    redact_next = False
    for index, argument in enumerate(request.arguments):
        explicit = index in request.sensitive_argument_indices
        embedded_secret = bool(secret_flag.match(argument))
        credential_url = bool(re.search(r"^[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", argument, re.I))
        if explicit or redact_next or embedded_secret or credential_url:
            redacted.append("<redacted>")
        else:
            redacted.append(argument)
        redact_next = embedded_secret and "=" not in argument
    return tuple(redacted)


def _validate_text(value: str, *, name: str, allow_empty: bool) -> None:
    if "\x00" in value:
        raise ValueError(f"{name} cannot contain NUL")
    if not allow_empty and not value:
        raise ValueError(f"{name} cannot be empty")
    if len(value) > _MAX_ARGUMENT_LENGTH:
        raise ValueError(f"{name} is too long")


def _validate_positive_optional(value: float | None, name: str) -> None:
    if value is not None:
        _validate_positive(value, name)


def _validate_positive(value: float, name: str) -> None:
    if not 0 < value <= 86_400:
        raise ValueError(f"{name} must be greater than zero and at most 86400")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _duration_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def _finish_session_process(
    session: ExecutionSession | None,
    pid: int,
    exit_code: int | None,
    *,
    terminated: bool,
) -> None:
    if session is None:
        return
    with contextlib.suppress(KeyError, ValueError):
        session.finish_process(pid, exit_code=exit_code, terminated=terminated)


if __name__ == "__main__":
    if (
        os.name == "nt"
        or len(sys.argv) != 4
        or sys.argv[1] != _POSIX_SUPERVISOR_FLAG
        or not sys.argv[2].isdigit()
        or not sys.argv[3].isdigit()
    ):
        raise SystemExit(2)
    supervisor_exit = _posix_process_supervisor_main(int(sys.argv[2]), int(sys.argv[3]))
    if supervisor_exit < 0:
        target_signal = -supervisor_exit
        signal.signal(target_signal, signal.SIG_DFL)
        os.kill(os.getpid(), target_signal)
        os._exit(128 + target_signal)
    raise SystemExit(supervisor_exit)
