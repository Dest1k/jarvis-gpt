from __future__ import annotations

import asyncio
import ctypes
import importlib
import inspect
import json
import os
import re
import select
import signal
import socket
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any

from .execution_process import process_tree_snapshot
from .redaction import redact_text
from .web_surfer_adapter import (
    _MAX_ARGUMENT_BYTES,
    _MAX_RESULT_BYTES,
    WEB_SURFER_METHODS,
    WEB_WORKER_PROTOCOL,
    _bind_public_arguments,
    _contract_problems,
    _json_value,
    _service_from_module,
)

_MAX_FRAME_BYTES = _MAX_RESULT_BYTES + 64 * 1024


async def _write_frame(
    writer: asyncio.StreamWriter,
    payload: Mapping[str, Any],
) -> None:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_FRAME_BYTES:
        encoded = json.dumps(
            {
                "protocol": WEB_WORKER_PROTOCOL,
                "type": "fatal",
                "ok": False,
                "error": {"code": "frame_too_large", "message": "worker frame is too large"},
            },
            separators=(",", ":"),
        ).encode("utf-8")
    writer.write(encoded + b"\n")
    await writer.drain()


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    try:
        line = await reader.readline()
    except (ValueError, asyncio.LimitOverrunError) as exc:
        raise ValueError("request frame is too large") from exc
    if not line:
        return None
    if len(line) > _MAX_ARGUMENT_BYTES + 64 * 1024:
        raise ValueError("request frame is too large")
    try:
        decoded = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request frame is malformed") from exc
    if not isinstance(decoded, dict):
        raise ValueError("request frame must be an object")
    return decoded


async def _load_service(module_names: Sequence[str]) -> tuple[str, object]:
    failures: list[str] = []
    for module_name in module_names:
        service: object | None = None
        try:
            module = importlib.import_module(module_name)
            service = _service_from_module(module, construct_factory=True)
            if service is None:
                raise TypeError("module does not expose the required public methods")
            problems = _contract_problems(service)
            if problems:
                raise TypeError(", ".join(problems))
            await _start_service(service)
            return module_name, service
        except BaseException as exc:
            failures.append(
                redact_text(f"{module_name}:{type(exc).__name__}: {exc}")[:1000]
            )
            if service is not None:
                try:
                    await _close_service(service)
                except BaseException as close_exc:
                    failures.append(
                        redact_text(
                            f"{module_name}.close:{type(close_exc).__name__}: {close_exc}"
                        )[:1000]
                    )
    raise TypeError("; ".join(failures) or "no web_surfer module candidates")


async def _start_service(service: object) -> None:
    starter = getattr(service, "start", None)
    if starter is None:
        return
    if not callable(starter):
        raise TypeError("web_surfer.start must be callable")
    started = starter()
    if not inspect.isawaitable(started):
        raise TypeError("web_surfer.start must be async")
    nested = await started
    if inspect.isawaitable(nested):
        raise TypeError("web_surfer.start returned a nested awaitable")


async def _close_service(service: object) -> None:
    closer = getattr(service, "aclose", None)
    if closer is None:
        closer = getattr(service, "close", None)
    if closer is None:
        return
    if not callable(closer):
        raise TypeError("web_surfer close hook must be callable")
    closed = closer()
    if inspect.isawaitable(closed):
        nested = await closed
        if inspect.isawaitable(nested):
            raise TypeError("web_surfer close hook returned a nested awaitable")


async def _serve_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    module_names: Sequence[str],
    *,
    reported_pid: int,
    service_pid: int,
) -> int:
    service: object | None = None
    try:
        try:
            module_name, service = await _load_service(module_names)
        except BaseException as exc:
            await _write_frame(
                writer,
                {
                    "protocol": WEB_WORKER_PROTOCOL,
                    "type": "ready",
                    "ok": False,
                    "pid": reported_pid,
                    "service_pid": service_pid,
                    "error": {
                        "code": "contract_error",
                        "message": redact_text(f"{type(exc).__name__}: {exc}")[:2000],
                    },
                },
            )
            return 2

        await _write_frame(
            writer,
            {
                "protocol": WEB_WORKER_PROTOCOL,
                "type": "ready",
                "ok": True,
                "pid": reported_pid,
                "service_pid": service_pid,
                "service": module_name,
            },
        )
        while True:
            request: dict[str, Any] = {}
            try:
                decoded = await _read_frame(reader)
                if decoded is None:
                    return 0
                request = decoded
                if request.get("protocol") != WEB_WORKER_PROTOCOL:
                    raise ValueError("unsupported worker protocol")
                request_type = request.get("type")
                request_id = str(request.get("request_id") or "")[:128]
                if request_type == "shutdown":
                    await _write_frame(
                        writer,
                        {
                            "protocol": WEB_WORKER_PROTOCOL,
                            "type": "shutdown",
                            "request_id": request_id,
                            "ok": True,
                        },
                    )
                    return 0
                if request_type != "invoke":
                    raise ValueError("unsupported worker request type")
                mode = str(request.get("mode") or "")
                if mode not in WEB_SURFER_METHODS:
                    raise ValueError(f"unsupported web-surfer mode: {mode}")
                arguments = request.get("arguments")
                if not isinstance(arguments, dict):
                    raise ValueError("arguments must be an object")
                normalized = _json_value(arguments, max_bytes=_MAX_ARGUMENT_BYTES)
                method = getattr(service, mode)
                bound_arguments = _bind_public_arguments(mode, method, normalized)
                result = await method(**bound_arguments)
                if inspect.isawaitable(result):
                    raise ValueError("web_surfer async method returned a nested awaitable")
                if result is None:
                    raise ValueError("web_surfer returned no result")
                data = _json_value(result, max_bytes=_MAX_RESULT_BYTES)
                await _write_frame(
                    writer,
                    {
                        "protocol": WEB_WORKER_PROTOCOL,
                        "type": "result",
                        "request_id": request_id,
                        "ok": True,
                        "data": data,
                    },
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await _write_frame(
                    writer,
                    {
                        "protocol": WEB_WORKER_PROTOCOL,
                        "type": "result",
                        "request_id": str(request.get("request_id") or "")[:128],
                        "ok": False,
                        "error": {
                            "code": "service_error",
                            "message": redact_text(f"{type(exc).__name__}: {exc}")[:2000],
                        },
                    },
                )
    finally:
        try:
            if service is not None:
                with suppress(BaseException):
                    await _close_service(service)
        finally:
            writer.close()
            with suppress(ConnectionError, OSError):
                await writer.wait_closed()


async def _serve(port: int, token: str, module_names: Sequence[str]) -> int:
    reader, writer = await asyncio.open_connection(
        host="127.0.0.1",
        port=port,
        limit=_MAX_FRAME_BYTES,
    )
    await _write_frame(
        writer,
        {
            "protocol": WEB_WORKER_PROTOCOL,
            "type": "hello",
            "token": token,
            "pid": os.getpid(),
        },
    )
    return await _serve_connection(
        reader,
        writer,
        module_names,
        reported_pid=os.getpid(),
        service_pid=os.getpid(),
    )


def _silence_standard_fds() -> None:
    descriptor = os.open(os.devnull, os.O_RDWR)
    try:
        for target in (0, 1, 2):
            os.dup2(descriptor, target)
    finally:
        if descriptor > 2:
            os.close(descriptor)


def _enable_linux_subreaper(expected_parent_pid: int) -> bool:
    """Keep double-forked browser descendants parented to this worker."""

    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        # Linux PR_SET_CHILD_SUBREAPER. Descendant daemons are reparented here,
        # preserving a complete PPID tree for the adapter's teardown snapshot.
        subreaper_ok = prctl(36, 1, 0, 0, 0) == 0
        return subreaper_ok and os.getppid() == expected_parent_pid
    except (AttributeError, OSError):
        return False


def _valid_posix_parent_guard(parent_fd: int, parent_pid: int) -> bool:
    """PID 1 is a valid adapter parent inside a container PID namespace."""

    return parent_fd >= 3 and parent_pid >= 1


def _supervise_service(descriptor: int, service_pid: int) -> int:
    """Remain the subreaper parent and contain every service descendant."""

    supervisor_pid = os.getpid()
    discovered: set[int] = {service_pid}
    pidfds: dict[int, int] = {}

    def remember_tree() -> tuple[set[int], set[int]]:
        current = {node.pid for node in process_tree_snapshot(supervisor_pid)}
        new_pids = current - discovered
        discovered.update(current)
        pidfd_open = getattr(os, "pidfd_open", None)
        if callable(pidfd_open):
            for pid in current:
                if pid in pidfds or pid == supervisor_pid or len(pidfds) >= 4096:
                    continue
                with suppress(OSError):
                    pidfds[pid] = pidfd_open(pid, 0)
        return current, new_pids

    os.set_blocking(descriptor, False)
    service_status: int | None = None
    while True:
        remember_tree()
        try:
            waited_pid, status = os.waitpid(service_pid, os.WNOHANG)
            if waited_pid == service_pid:
                service_status = status
                break
            readable, _writable, _exceptional = select.select(
                [descriptor], [], [], 0.05
            )
            if readable and os.read(descriptor, 1) == b"":
                break
        except OSError:
            break
    with suppress(OSError):
        os.close(descriptor)

    def send(pid: int, sent_signal: signal.Signals) -> None:
        pidfd = pidfds.get(pid)
        pidfd_send = getattr(signal, "pidfd_send_signal", None)
        if pidfd is not None and callable(pidfd_send):
            with suppress(ProcessLookupError, PermissionError):
                pidfd_send(pidfd, sent_signal, None, 0)
            return
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sent_signal)

    stable_passes = 0
    for _attempt in range(8):
        current, new_pids = remember_tree()
        for pid in current:
            if pid == supervisor_pid:
                continue
            send(pid, signal.SIGSTOP)
        if new_pids:
            stable_passes = 0
        else:
            stable_passes += 1
            if stable_passes >= 2:
                break
        time.sleep(0.005)
    for pid in sorted(discovered, reverse=True):
        if pid == supervisor_pid:
            continue
        send(pid, signal.SIGKILL)
    if service_status is None:
        with suppress(ChildProcessError):
            _waited_pid, service_status = os.waitpid(service_pid, 0)
    while True:
        try:
            adopted_pid, _adopted_status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if adopted_pid <= 0:
            break
    for pidfd in pidfds.values():
        with suppress(OSError):
            os.close(pidfd)
    if service_status is None:
        return 1
    return os.waitstatus_to_exitcode(service_status)


def _send_sync_frame(stream: socket.socket, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_FRAME_BYTES:
        raise ValueError("worker hello frame is too large")
    stream.sendall(encoded + b"\n")


async def _serve_inherited_socket(
    stream: socket.socket,
    module_names: Sequence[str],
    *,
    supervisor_pid: int,
) -> int:
    stream.setblocking(False)
    reader, writer = await asyncio.open_connection(
        sock=stream,
        limit=_MAX_FRAME_BYTES,
    )
    return await _serve_connection(
        reader,
        writer,
        module_names,
        reported_pid=supervisor_pid,
        service_pid=os.getpid(),
    )


def _run_posix_supervisor(
    ipc_name: str,
    token: str,
    module_names: Sequence[str],
    parent_fd: int,
) -> int:
    supervisor_pid = os.getpid()
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stream.settimeout(5.0)
    stream.connect(f"\0{ipc_name}")
    _send_sync_frame(
        stream,
        {
            "protocol": WEB_WORKER_PROTOCOL,
            "type": "hello",
            "token": token,
            "pid": supervisor_pid,
        },
    )
    service_pid = os.fork()
    if service_pid == 0:
        os.close(parent_fd)
        return asyncio.run(
            _serve_inherited_socket(
                stream,
                module_names,
                supervisor_pid=supervisor_pid,
            )
        )
    stream.close()
    return _supervise_service(parent_fd, service_pid)


def main() -> int:
    if len(sys.argv) < 3:
        return 64
    try:
        port = int(sys.argv[1])
    except ValueError:
        return 64
    token = os.environ.pop("JARVIS_WEB_WORKER_TOKEN", "")
    parent_fd_raw = os.environ.pop("JARVIS_WEB_PARENT_FD", "")
    parent_pid_raw = os.environ.pop("JARVIS_WEB_PARENT_PID", "")
    ipc_name = os.environ.pop("JARVIS_WEB_IPC_NAME", "")
    module_names = tuple(sys.argv[2:10])
    if (
        (os.name == "nt" and not 1 <= port <= 65535)
        or (
            os.name != "nt"
            and (
                port != 0
                or re.fullmatch(r"jarvis-gpt\.web\.[0-9]+\.[0-9a-f]{32}", ipc_name) is None
            )
        )
        or not 20 <= len(token) <= 256
        or not module_names
        or any(
            len(name) > 240
            or re.fullmatch(
                r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*",
                name,
                flags=re.ASCII,
            )
            is None
            for name in module_names
        )
    ):
        return 64
    if os.name != "nt":
        if not sys.platform.startswith("linux"):
            # BSD/macOS lack a subreaper primitive. Refuse to claim complete
            # browser-tree containment instead of leaking daemonized children.
            return 70
        try:
            parent_fd = int(parent_fd_raw)
            parent_pid = int(parent_pid_raw)
        except ValueError:
            return 64
        if not _valid_posix_parent_guard(parent_fd, parent_pid):
            return 64
        if not _enable_linux_subreaper(parent_pid):
            return 70
    _silence_standard_fds()
    if os.name != "nt":
        return _run_posix_supervisor(ipc_name, token, module_names, parent_fd)
    return asyncio.run(_serve(port, token, module_names))


if __name__ == "__main__":
    raise SystemExit(main())
