from __future__ import annotations

import asyncio
import contextlib
import ctypes
import getpass
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final

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
        )
        stdout = _TailBuffer(request.max_output_bytes)
        stderr = _TailBuffer(request.max_output_bytes)
        environment = dict(os.environ) if request.inherit_environment else {}
        environment.update(request.environment)
        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
                getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            )

        if session is not None:
            if reservation_id is None:
                raise ValueError("session-bound process execution requires a reservation_id")
            if not session.has_process_start_reservation(reservation_id):
                session.reserve_process_start(reservation_id)

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                start_new_session=start_new_session,
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

        try:
            job = _WindowsJob.assign(process.pid)
        except OSError as exc:
            if session is not None and reservation_id is not None:
                session.release_process_start(reservation_id)
            return await _abort_started_process(
                process=process,
                job=None,
                stdout=stdout,
                stderr=stderr,
                request=request,
                feedback_argv=feedback_argv,
                started_at=started_at,
                started_clock=started_clock,
                permissions=permissions,
                before=before,
                before_truncated=before_truncated,
                error=exc,
                session=None,
            )

        if session is not None:
            try:
                session.register_process(
                    pid=process.pid,
                    parent_pid=os.getpid(),
                    argv=feedback_argv,
                    reservation_id=reservation_id,
                    process_group_id=process.pid if os.name != "nt" else None,
                )
            except Exception as exc:
                if reservation_id is not None:
                    session.release_process_start(reservation_id)
                return await _abort_started_process(
                    process=process,
                    job=job,
                    stdout=stdout,
                    stderr=stderr,
                    request=request,
                    feedback_argv=feedback_argv,
                    started_at=started_at,
                    started_clock=started_clock,
                    permissions=permissions,
                    before=before,
                    before_truncated=before_truncated,
                    error=exc,
                    session=None,
                )

        if os.name == "nt":
            try:
                _resume_windows_process(process.pid)
            except OSError as exc:
                return await _abort_started_process(
                    process=process,
                    job=job,
                    stdout=stdout,
                    stderr=stderr,
                    request=request,
                    feedback_argv=feedback_argv,
                    started_at=started_at,
                    started_clock=started_clock,
                    permissions=permissions,
                    before=before,
                    before_truncated=before_truncated,
                    error=exc,
                    session=session,
                )

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
            process.pid: ProcessNode(process.pid, os.getpid(), str(executable))
        }
        stop_sampler = asyncio.Event()
        sampler_task = asyncio.create_task(
            _sample_process_tree(process.pid, tree, stop_sampler),
            name=f"execution-tree-{process.pid}",
        )
        reason = TerminationReason.EXITED
        interrupt_sent = False
        kill_sent = False
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
                    )
                    break
            await asyncio.shield(wait_task)
        except asyncio.CancelledError:
            await _stop_process(
                process,
                wait_task,
                request.interrupt_grace_seconds,
                request.kill_grace_seconds,
                job,
            )
            _finish_session_process(session, process.pid, process.returncode, terminated=True)
            raise
        finally:
            stop_sampler.set()
            if os.name != "nt":
                remaining_group_killed = await _terminate_remaining_process_group(
                    process.pid,
                    interrupt_grace=min(request.interrupt_grace_seconds, 1.0),
                    kill_grace=min(request.kill_grace_seconds, 1.0),
                )
                kill_sent = kill_sent or remaining_group_killed
            stream_tasks = (stdout_task, stderr_task, sampler_task)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*stream_tasks, return_exceptions=True),
                    timeout=2.0,
                )
            except TimeoutError:
                for task in stream_tasks:
                    task.cancel()
                await asyncio.gather(*stream_tasks, return_exceptions=True)
            if process.returncode is None and not wait_task.done():
                wait_task.cancel()
            if job is not None:
                job.close()

        after, after_truncated = await asyncio.to_thread(
            filesystem_snapshot,
            request.observe_paths,
            request.max_observed_entries,
        )
        exit_code = process.returncode
        _finish_session_process(
            session,
            process.pid,
            exit_code,
            terminated=reason is not TerminationReason.EXITED,
        )
        return ExecutionFeedback(
            ok=reason is TerminationReason.EXITED and exit_code == 0,
            argv=feedback_argv,
            pid=process.pid,
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


async def _abort_started_process(
    *,
    process: asyncio.subprocess.Process,
    job: _WindowsJob | None,
    stdout: _TailBuffer,
    stderr: _TailBuffer,
    request: ProcessRequest,
    feedback_argv: tuple[str, ...],
    started_at: str,
    started_clock: float,
    permissions: PermissionSnapshot,
    before: Mapping[str, FilesystemEntry],
    before_truncated: bool,
    error: BaseException,
    session: ExecutionSession | None,
) -> ExecutionFeedback:
    try:
        if job is not None:
            job.terminate()
        elif os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except OSError:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
    captured_stdout, captured_stderr = await process.communicate()
    if captured_stdout:
        stdout.append(captured_stdout)
    if captured_stderr:
        stderr.append(captured_stderr)
    _finish_session_process(
        session,
        process.pid,
        process.returncode,
        terminated=True,
    )
    if job is not None:
        job.close()
    after, after_truncated = await asyncio.to_thread(
        filesystem_snapshot,
        request.observe_paths,
        request.max_observed_entries,
    )
    return ExecutionFeedback(
        ok=False,
        argv=feedback_argv,
        pid=process.pid,
        exit_code=process.returncode,
        termination_reason=TerminationReason.START_FAILED,
        started_at=started_at,
        finished_at=_utc_now(),
        duration_ms=_duration_ms(started_clock),
        stdout=stdout.capture(),
        stderr=stderr.capture(),
        pid_tree=(ProcessNode(process.pid, os.getpid(), feedback_argv[0]),),
        permissions=permissions,
        filesystem_diff=filesystem_diff(
            before,
            after,
            scan_truncated=before_truncated or after_truncated,
        ),
        kill_sent=True,
        error=f"{type(error).__name__}: {error}",
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
) -> tuple[bool, bool]:
    if wait_task.done():
        return False, False
    interrupt_sent = _send_interrupt(process)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(wait_task), timeout=interrupt_grace)
        return interrupt_sent, False
    if job is not None:
        job.terminate()
    elif os.name != "nt":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    try:
        await asyncio.wait_for(asyncio.shield(wait_task), timeout=kill_grace)
    except TimeoutError as exc:
        raise RuntimeError(f"process {process.pid} did not terminate after kill") from exc
    return interrupt_sent, True


def _send_interrupt(process: asyncio.subprocess.Process) -> bool:
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
) -> tuple[dict[str, FilesystemEntry], bool]:
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
