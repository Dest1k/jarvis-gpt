"""Isolated Python code sandbox.

Runs operator-supplied Python with a wall-clock timeout, a memory ceiling, a bounded
capture of stdout/stderr, a private working directory, and a curated environment that
never carries Jarvis secrets/tokens. On Windows the child tree is bound to a Job Object
(memory limit + active-process cap + kill-on-close) so a runaway or fork-bomb dies with
the job; on POSIX an `RLIMIT_AS`/`RLIMIT_NPROC` `preexec_fn` plays the same role.

SECURITY POSTURE — read this. This is **resource isolation, not security isolation**.
On this single-operator Windows box without a container, the child runs as the owner's own
user and can read/write anything that user can; `-I`, the private cwd and the curated env
reduce accidental leakage but are NOT a security boundary against hostile code. It protects
against runaway/accidental compute (infinite loops, memory blowups, stray child processes),
which is the real risk when the code comes from the owner's own requests. True isolation
against untrusted code would require a container/VM (the repo already runs a Docker
dispatcher — a `docker run --network=none --read-only` sandbox is the path if that is ever needed).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SEC = 30
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
# The numpy/OpenBLAS/matplotlib stack reserves large per-thread buffers, so a data-science
# sandbox needs headroom; 2 GiB is comfortable on this 128 GiB box (thread counts are capped
# below to keep the footprint bounded).
DEFAULT_MEM_LIMIT_MB = 2048

# Windows Job Object limit flags.
_JOB_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_LIMIT_JOB_MEMORY = 0x00000200
_JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_MAX_ACTIVE_PROCESSES = 16

# Environment variables kept for the child so the interpreter + its native extensions
# resolve on Windows. Everything else (JARVIS_*, tokens, API keys) is deliberately dropped.
_ENV_KEEP = (
    "SYSTEMROOT",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "LANG",
    "LC_ALL",
)


@dataclass
class SandboxResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_sec: float
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_sec": round(self.duration_sec, 3),
            "error": self.error,
        }


def _curated_env(workdir: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    # Native extensions (numpy/scipy/matplotlib) need their DLL directories on PATH on
    # Windows; keep the real PATH (this is resource, not egress, isolation).
    if os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    workdir_str = str(workdir)
    env.update(
        {
            "TEMP": workdir_str,
            "TMP": workdir_str,
            "HOME": workdir_str,
            "USERPROFILE": workdir_str,
            "MPLBACKEND": "Agg",  # headless matplotlib
            "MPLCONFIGDIR": str(workdir / ".mpl"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONNOUSERSITE": "1",
            # Bound native BLAS/OpenMP thread pools so numpy/scipy stay within the memory
            # ceiling and don't oversubscribe the CPU.
            "OPENBLAS_NUM_THREADS": "4",
            "OMP_NUM_THREADS": "4",
            "MKL_NUM_THREADS": "4",
            "NUMEXPR_NUM_THREADS": "4",
        }
    )
    return env


def _decode_bounded(raw: bytes, max_bytes: int) -> str:
    if not raw:
        return ""
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    if truncated:
        text += "\n…[вывод обрезан]"
    return text


def _assign_windows_job(pid: int, mem_bytes: int) -> int | None:
    """Bind the child to a Job Object: memory cap + active-process cap + kill-on-close."""

    if os.name != "nt":
        return None
    import ctypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _BasicLimit(ctypes.Structure):
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

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
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
        return None
    info = _ExtendedLimit()
    info.BasicLimitInformation.LimitFlags = (
        _JOB_LIMIT_KILL_ON_JOB_CLOSE | _JOB_LIMIT_JOB_MEMORY | _JOB_LIMIT_ACTIVE_PROCESS
    )
    info.BasicLimitInformation.ActiveProcessLimit = _MAX_ACTIVE_PROCESSES
    info.JobMemoryLimit = mem_bytes
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return None
    handle = kernel32.OpenProcess(0x0001 | 0x0100, False, pid)  # SET_QUOTA | TERMINATE
    if not handle:
        kernel32.CloseHandle(job)
        return None
    try:
        if not kernel32.AssignProcessToJobObject(job, handle):
            kernel32.CloseHandle(job)
            return None
    finally:
        kernel32.CloseHandle(handle)
    return job


def _terminate_windows_job(job: int) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateJobObject(job, 1)


def _close_windows_job(job: int) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle(job)


def _posix_limits(mem_bytes: int):
    def _apply() -> None:  # pragma: no cover - POSIX only
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        with_nproc = getattr(resource, "RLIMIT_NPROC", None)
        if with_nproc is not None:
            resource.setrlimit(with_nproc, (_MAX_ACTIVE_PROCESSES, _MAX_ACTIVE_PROCESSES))

    return _apply


def run_python(
    code: str,
    *,
    workdir: str | Path,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    mem_limit_mb: int = DEFAULT_MEM_LIMIT_MB,
) -> SandboxResult:
    """Run ``code`` in an isolated child interpreter. Never raises — errors return on the result."""

    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    (work / "main.py").write_text(code, encoding="utf-8")
    interpreter = getattr(sys, "_base_executable", None) or sys.executable
    argv = [interpreter, "-I", "main.py"]
    env = _curated_env(work)
    mem_bytes = max(64, int(mem_limit_mb)) * 1024 * 1024

    creationflags = 0
    preexec = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:  # pragma: no cover - POSIX only
        preexec = _posix_limits(mem_bytes)

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(work),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            preexec_fn=preexec,
        )
    except OSError as exc:
        return SandboxResult(
            ok=False,
            exit_code=None,
            stdout="",
            stderr="",
            timed_out=False,
            duration_sec=0.0,
            error=f"Не смог запустить интерпретатор: {exc}",
        )

    job = None
    if os.name == "nt":
        try:
            job = _assign_windows_job(proc.pid, mem_bytes)
        except Exception:  # noqa: BLE001 — job binding is best-effort hardening
            job = None

    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        if job is not None:
            _terminate_windows_job(job)
        proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            out, err = b"", b""
    finally:
        if job is not None:
            _close_windows_job(job)  # kill-on-close reaps any survivors

    duration = time.monotonic() - start
    exit_code = proc.returncode
    ok = (not timed_out) and exit_code == 0
    error = None
    if timed_out:
        error = f"Превышен лимит времени ({timeout_sec} с)."
    elif exit_code != 0:
        error = f"Код завершился с ошибкой (exit {exit_code})."
    return SandboxResult(
        ok=ok,
        exit_code=exit_code,
        stdout=_decode_bounded(out, max_output_bytes),
        stderr=_decode_bounded(err, max_output_bytes),
        timed_out=timed_out,
        duration_sec=duration,
        error=error,
    )
