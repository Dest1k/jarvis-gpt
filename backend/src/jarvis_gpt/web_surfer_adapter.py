from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import math
import os
import re
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

from .execution_process import (
    _resume_windows_process,
    _WindowsJob,
    process_tree_snapshot,
)
from .redaction import redact_text, redact_value

WEB_ADAPTER_PROTOCOL = "jarvis.web-surfer-adapter.v1"
WEB_WORKER_PROTOCOL = "jarvis.web-surfer-worker.v1"
WEB_SURFER_METHODS = ("fast_fact", "deep_research", "aggressive_shopping")
_MAX_ARGUMENT_BYTES = 128 * 1024
_MAX_RESULT_BYTES = 4 * 1024 * 1024
_MAX_FRAME_BYTES = _MAX_RESULT_BYTES + 64 * 1024
_MAX_FACTORY_CONFIG_BYTES = 64 * 1024
_MAX_NODES = 20_000
_MAX_DEPTH = 20
_WORKER_STOP_TIMEOUT = 2.0


@dataclass(frozen=True)
class WebSurferResult:
    protocol: str
    ok: bool
    mode: str
    data: Any = None
    error: dict[str, str] | None = None
    unavailable: bool = False
    service: str | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _WorkerHandle:
    process: asyncio.subprocess.Process
    job: _WindowsJob | None
    generation: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    service_pid: int | None = None
    parent_guard_fd: int | None = None
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    watcher: asyncio.Task[None] | None = None
    terminated: bool = False
    tracked_pidfds: dict[int, int] = field(default_factory=dict)


class _WorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.permanent = permanent


def _worker_peer_pid(writer: asyncio.StreamWriter) -> int | None:
    """Return the kernel-authenticated PID for a Linux loopback peer."""

    if not sys.platform.startswith("linux") or not hasattr(socket, "SO_PEERCRED"):
        return None
    transport_socket = writer.get_extra_info("socket")
    if transport_socket is None:
        raise ValueError("worker IPC socket is unavailable")
    credentials = transport_socket.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize("3i"),
    )
    peer_pid, _uid, _gid = struct.unpack("3i", credentials)
    return int(peer_pid)


def _json_value(value: Any, *, max_bytes: int) -> Any:
    if hasattr(value, "model_dump") and callable(value.model_dump):
        value = value.model_dump(mode="json")
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)

    node_count = 0

    def visit(item: Any, depth: int) -> Any:
        nonlocal node_count
        node_count += 1
        if node_count > _MAX_NODES:
            raise ValueError("JSON value contains too many nodes")
        if depth > _MAX_DEPTH:
            raise ValueError("JSON value is nested too deeply")
        if item is None or isinstance(item, bool | int):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("JSON value contains a non-finite number")
            return item
        if isinstance(item, str):
            return item
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                result[key] = visit(nested, depth + 1)
            return result
        if isinstance(item, Sequence) and not isinstance(item, bytes | bytearray | memoryview):
            return [visit(nested, depth + 1) for nested in item]
        raise ValueError(f"unsupported JSON value type: {type(item).__name__}")

    normalized = visit(value, 0)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"JSON value exceeds {max_bytes} bytes")
    return normalized


def _service_from_module(
    module: ModuleType,
    *,
    construct_factory: bool = False,
) -> object | None:
    namespace = vars(module)
    if all(callable(namespace.get(method)) for method in WEB_SURFER_METHODS):
        return module
    # Singleton attributes are inspected but never constructed. The adapter invokes
    # only the three Claude-owned public methods.
    for attribute in ("web_surfer", "service"):
        candidate = namespace.get(attribute)
        if candidate is not None and all(
            callable(getattr(candidate, method, None)) for method in WEB_SURFER_METHODS
        ):
            return candidate
    if not construct_factory:
        return None
    factory = namespace.get("JarvisWebSurfer")
    if factory is None:
        return None
    if (
        not inspect.isclass(factory)
        or factory.__name__ != "JarvisWebSurfer"
        or factory.__module__ != module.__name__
    ):
        raise TypeError("JarvisWebSurfer must be a class defined by the service module")
    factory_arguments = _factory_arguments_from_environment()
    try:
        inspect.signature(factory).bind(**factory_arguments)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            "JarvisWebSurfer factory configuration does not match its public constructor"
        ) from exc
    service = factory(**factory_arguments)
    if service is None or inspect.isclass(service):
        raise TypeError("JarvisWebSurfer factory returned an invalid service instance")
    return service


def _factory_arguments_from_environment() -> dict[str, Any]:
    """Read bounded JSON kwargs for the isolated public service constructor."""

    raw = os.environ.get("JARVIS_WEB_SURFER_FACTORY_KWARGS_JSON", "").strip()
    if not raw:
        return {}
    if len(raw.encode("utf-8", errors="strict")) > _MAX_FACTORY_CONFIG_BYTES:
        raise ValueError("web_surfer factory configuration is too large")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("web_surfer factory configuration is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("web_surfer factory configuration must be a JSON object")
    return _json_value(value, max_bytes=_MAX_FACTORY_CONFIG_BYTES)


def _bind_public_arguments(
    method_name: str,
    method: Any,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = dict(arguments)
    signature = inspect.signature(method)
    try:
        signature.bind(**normalized)
        return normalized
    except TypeError as original:
        # The bundled Claude service names its single public input
        # ``product_url``. Preserve compatibility with an older black-box
        # implementation that used ``query`` without weakening either
        # signature: only one exact alias is attempted.
        if (
            method_name == "aggressive_shopping"
            and "query" in normalized
            and "product_url" not in normalized
        ):
            aliased = dict(normalized)
            aliased["product_url"] = aliased.pop("query")
            try:
                signature.bind(**aliased)
            except TypeError:
                pass
            else:
                return aliased
        if (
            method_name == "aggressive_shopping"
            and "product_url" in normalized
            and "query" not in normalized
        ):
            aliased = dict(normalized)
            aliased["query"] = aliased.pop("product_url")
            try:
                signature.bind(**aliased)
            except TypeError:
                pass
            else:
                return aliased
        raise original


def _require_public_http_url(value: Any) -> str:
    """Validate one direct browser target before it reaches the black box.

    This is an outer trust-boundary check, not web-surfer implementation logic.
    Every currently resolved address must be globally routable; credentials,
    local aliases, malformed ports, and non-HTTP schemes fail closed.
    """

    if not isinstance(value, str):
        raise ValueError("aggressive_shopping requires a product_url string")
    target = value.strip()
    if not target or len(target) > 4096 or any(ord(char) < 32 for char in target):
        raise ValueError("product_url is empty, too long, or contains control characters")
    try:
        parsed = urlsplit(target)
        port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    except ValueError as exc:
        raise ValueError("product_url contains an invalid port or host") from exc
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("product_url must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("product_url cannot contain credentials")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("product_url must contain a host")
    if "%" in hostname:
        raise ValueError("product_url cannot contain an IPv6 zone identifier")
    try:
        normalized_host = hostname.encode("idna").decode("ascii").rstrip(".").casefold()
    except UnicodeError as exc:
        raise ValueError("product_url host is not valid IDNA") from exc
    if (
        not normalized_host
        or normalized_host == "localhost"
        or normalized_host.endswith(
            (".localhost", ".local", ".internal", ".home.arpa")
        )
    ):
        raise ValueError("product_url host is local or reserved")
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    try:
        addresses.add(ipaddress.ip_address(normalized_host))
    except ValueError:
        try:
            resolved = socket.getaddrinfo(
                normalized_host,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise ValueError("product_url host could not be resolved") from exc
        for item in resolved:
            try:
                addresses.add(ipaddress.ip_address(str(item[4][0]).split("%", 1)[0]))
            except (IndexError, TypeError, ValueError):
                continue
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("product_url must resolve only to public network addresses")
    return target


def _contract_problems(service: object) -> list[str]:
    problems: list[str] = []
    for method_name in WEB_SURFER_METHODS:
        method = getattr(service, method_name, None)
        if not callable(method):
            problems.append(f"missing {method_name}")
            continue
        if not inspect.iscoroutinefunction(method):
            problems.append(f"{method_name} must be async")
            continue
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            problems.append(f"uninspectable {method_name}")
            continue
        if any(
            parameter.kind is inspect.Parameter.POSITIONAL_ONLY
            and parameter.default is inspect.Parameter.empty
            for parameter in signature.parameters.values()
        ):
            problems.append(f"{method_name} requires positional-only arguments")
            continue
        try:
            probe_arguments = (
                {"product_url": "https://example.com/product"}
                if method_name == "aggressive_shopping"
                else {"query": "contract-probe"}
            )
            _bind_public_arguments(method_name, method, probe_arguments)
        except (TypeError, ValueError):
            problems.append(f"{method_name} cannot accept its canonical input")
    return problems


class WebSurferAdapter:
    """Strict black-box adapter for Claude's optional web_surfer service.

    Installed modules run in a resident, process-tree-contained worker. Therefore
    blocking imports, CPU loops, cancellation-resistant coroutines, and Playwright
    descendants cannot freeze or outlive the Jarvis API process. Direct object
    injection exists only as an explicit test boundary.
    """

    def __init__(
        self,
        service: object | None = None,
        *,
        module_names: Sequence[str] | None = None,
        timeout_sec: float = 120.0,
        max_result_bytes: int = _MAX_RESULT_BYTES,
        unsafe_in_process: bool = False,
    ) -> None:
        self._timeout_sec = max(0.1, min(float(timeout_sec), 900.0))
        self._max_result_bytes = max(1024, min(int(max_result_bytes), _MAX_RESULT_BYTES))
        self._service: object | None = None
        self._module_name: str | None = None
        self._module_candidates: tuple[str, ...] = ()
        self._service_name: str | None = None
        self._unavailable_reason: str | None = None
        self._disabled = False
        self._probed = False
        self._async_inflight: dict[str, asyncio.Task[Any]] = {}
        self._worker: _WorkerHandle | None = None
        self._worker_generation = 0
        self._call_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        if service is not None:
            if not unsafe_in_process:
                raise ValueError(
                    "direct web_surfer injection is test-only; pass unsafe_in_process=True"
                )
            self._service = service
            self._service_name = (
                f"{service.__class__.__module__}.{service.__class__.__qualname__}"
            )
            self._validate_in_process_contract()
            self._probed = self._service is not None
        else:
            self._discover_module(module_names)

    def _discover_module(self, module_names: Sequence[str] | None) -> None:
        configured = os.environ.get("JARVIS_WEB_SURFER_MODULE", "").strip()
        candidates = tuple(
            dict.fromkeys(
                name
                for name in (
                    configured,
                    *(module_names or ("jarvis_gpt.web_surfer",)),
                )
                if name
            )
        )
        candidates = candidates[:8]
        valid_candidates: list[str] = []
        for name in candidates:
            # Dynamically injected modules are supported for tests only. Installed
            # services are deliberately not imported into the backend process.
            loaded = sys.modules.get(name)
            if isinstance(loaded, ModuleType) and getattr(loaded, "__spec__", None) is None:
                service = _service_from_module(loaded)
                if service is not None:
                    self._service = service
                    self._service_name = name
                    self._validate_in_process_contract()
                    self._probed = self._service is not None
                    return
                continue
            if (
                len(name) > 240
                or re.fullmatch(
                    r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*",
                    name,
                    flags=re.ASCII,
                )
                is None
            ):
                continue
            valid_candidates.append(name)
        if valid_candidates:
            self._module_candidates = tuple(valid_candidates)
            self._module_name = valid_candidates[0]
            self._service_name = valid_candidates[0]
            return
        self._unavailable_reason = "no valid module candidates configured"

    def _validate_in_process_contract(self) -> None:
        if self._service is None:
            return
        problems = _contract_problems(self._service)
        if problems:
            self._service = None
            self._disabled = True
            self._unavailable_reason = ", ".join(problems)

    @property
    def available(self) -> bool:
        with self._state_lock:
            return (
                not self._closed
                and not self._disabled
                and (
                    self._service is not None
                    or self._module_name is not None
                    and self._probed
                )
            )

    async def start(self, *, timeout_sec: float = 15.0) -> bool:
        """Probe an installed service in isolation without making startup fatal."""

        if self._service is not None or self._module_name is None:
            return self.available
        timeout = max(0.1, min(float(timeout_sec), 60.0))
        try:
            async with self._call_lock:
                await asyncio.wait_for(self._ensure_worker_locked(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._disabled = True
            self._probed = True
            self._unavailable_reason = redact_text(f"{type(exc).__name__}: {exc}")[:2000]
            return False
        self._probed = True
        return self.available

    def capabilities(self) -> dict[str, Any]:
        with self._state_lock:
            inflight = sorted(
                mode for mode, task in self._async_inflight.items() if not task.done()
            )
            worker = self._worker
            closed = self._closed
        isolation = "process" if self._module_name is not None else "in_process_test"
        if self._module_name is None and self._service is None:
            isolation = "unavailable"
        return {
            "protocol": WEB_ADAPTER_PROTOCOL,
            "worker_protocol": WEB_WORKER_PROTOCOL,
            "available": self.available,
            "modes": list(WEB_SURFER_METHODS),
            "service": self._service_name,
            "reason": (
                self._unavailable_reason
                or ("service has not been probed" if not self._probed else None)
                if not self.available
                else None
            ),
            "isolation": isolation,
            "worker_pid": worker.service_pid if worker is not None else None,
            "supervisor_pid": worker.process.pid if worker is not None else None,
            "worker_generation": worker.generation if worker is not None else None,
            "async_inflight": inflight,
            "closed": closed,
        }

    async def invoke(
        self,
        mode: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_sec: float | None = None,
    ) -> WebSurferResult:
        started = time.monotonic()
        if mode not in WEB_SURFER_METHODS:
            return self._error(
                mode,
                "invalid_mode",
                f"unsupported web-surfer mode: {mode}",
                started=started,
            )
        if not self.available:
            return self._error(
                mode,
                "service_unavailable",
                self._unavailable_reason or "web_surfer service is unavailable",
                unavailable=True,
                started=started,
            )
        try:
            normalized_arguments = _json_value(
                dict(arguments or {}), max_bytes=_MAX_ARGUMENT_BYTES
            )
        except (TypeError, ValueError) as exc:
            return self._error(mode, "invalid_arguments", str(exc), started=started)
        try:
            effective_timeout = self._timeout_sec if timeout_sec is None else float(timeout_sec)
            effective_timeout = max(0.1, min(effective_timeout, 900.0))
        except (TypeError, ValueError):
            return self._error(
                mode,
                "invalid_arguments",
                "timeout_sec must be a finite number",
                started=started,
            )
        if not math.isfinite(effective_timeout):
            return self._error(
                mode,
                "invalid_arguments",
                "timeout_sec must be a finite number",
                started=started,
            )
        if self._module_name is not None and mode == "aggressive_shopping":
            raw_target = normalized_arguments.get(
                "product_url", normalized_arguments.get("query")
            )
            if (
                "product_url" in normalized_arguments
                and "query" in normalized_arguments
            ):
                return self._error(
                    mode,
                    "invalid_target",
                    "provide product_url only, not both product_url and query",
                    started=started,
                )
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_require_public_http_url, raw_target),
                    timeout=min(5.0, effective_timeout),
                )
            except TimeoutError:
                return self._error(
                    mode,
                    "invalid_target",
                    "product_url DNS validation timed out",
                    started=started,
                )
            except ValueError as exc:
                return self._error(
                    mode,
                    "invalid_target",
                    str(exc),
                    started=started,
                )

        try:
            if self._module_name is not None:
                raw_result = await self._invoke_process_bounded(
                    mode,
                    normalized_arguments,
                    timeout_sec=effective_timeout,
                )
            else:
                service = self._service
                if service is None:
                    raise RuntimeError("web_surfer service became unavailable")
                method = getattr(service, mode)
                try:
                    bound_arguments = _bind_public_arguments(
                        mode, method, normalized_arguments
                    )
                except TypeError as exc:
                    return self._error(
                        mode, "signature_mismatch", str(exc), started=started
                    )
                raw_result = await self._invoke_in_process(
                    mode,
                    method,
                    bound_arguments,
                    timeout_sec=effective_timeout,
                )
            if inspect.isawaitable(raw_result):
                raise ValueError("web_surfer async method returned a nested awaitable")
            if raw_result is None:
                raise ValueError("web_surfer returned no result")
            data = redact_value(_json_value(raw_result, max_bytes=self._max_result_bytes))
        except TimeoutError:
            return self._error(
                mode,
                "timeout",
                f"web_surfer exceeded {effective_timeout:g}s",
                started=started,
            )
        except asyncio.CancelledError:
            raise
        except _WorkerError as exc:
            if exc.permanent:
                self._disabled = True
                self._unavailable_reason = str(exc)
            return self._error(
                mode,
                exc.code,
                str(exc),
                unavailable=exc.permanent,
                started=started,
            )
        except Exception as exc:
            return self._error(
                mode,
                "service_error",
                f"{type(exc).__name__}: {str(exc)[:1000]}",
                started=started,
            )
        if isinstance(data, dict) and data.get("ok") is False:
            message = data.get("error") or data.get("message") or "web operation failed"
            return WebSurferResult(
                protocol=WEB_ADAPTER_PROTOCOL,
                ok=False,
                mode=mode,
                data=data,
                error={
                    "code": "operation_failed",
                    "message": redact_text(str(message))[:2000],
                },
                service=self._service_name,
                elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
            )
        return WebSurferResult(
            protocol=WEB_ADAPTER_PROTOCOL,
            ok=True,
            mode=mode,
            data=data,
            service=self._service_name,
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        )

    async def fast_fact(self, query: str, **arguments: Any) -> WebSurferResult:
        return await self.invoke("fast_fact", {"query": query, **arguments})

    async def deep_research(self, query: str, **arguments: Any) -> WebSurferResult:
        return await self.invoke("deep_research", {"query": query, **arguments})

    async def aggressive_shopping(self, product_url: str, **arguments: Any) -> WebSurferResult:
        return await self.invoke(
            "aggressive_shopping", {"product_url": product_url, **arguments}
        )

    async def _invoke_process(self, mode: str, arguments: dict[str, Any]) -> Any:
        async with self._call_lock:
            handle = await self._ensure_worker_locked()
            request_id = uuid.uuid4().hex
            request = {
                "protocol": WEB_WORKER_PROTOCOL,
                "type": "invoke",
                "request_id": request_id,
                "mode": mode,
                "arguments": arguments,
            }
            try:
                await self._write_worker_frame(handle, request)
                response = await self._read_worker_frame(handle)
            except asyncio.CancelledError:
                await self._finish_cleanup_after_cancellation(handle)
                raise
            except Exception:
                await self._terminate_worker(handle)
                raise
            if (
                response.get("protocol") != WEB_WORKER_PROTOCOL
                or response.get("type") != "result"
                or response.get("request_id") != request_id
            ):
                await self._terminate_worker(handle)
                raise _WorkerError("protocol_error", "invalid web worker response")
            if not response.get("ok"):
                error = response.get("error")
                if not isinstance(error, dict):
                    raise _WorkerError("service_error", "web worker returned an invalid error")
                raise _WorkerError(
                    str(error.get("code") or "service_error")[:80],
                    str(error.get("message") or "web_surfer service failed")[:2000],
                )
            return response.get("data")

    async def _invoke_process_bounded(
        self,
        mode: str,
        arguments: dict[str, Any],
        *,
        timeout_sec: float,
    ) -> Any:
        """Bound caller wait and worker lifetime, including repeated cancellation."""

        task = asyncio.create_task(self._invoke_process(mode, arguments))
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout_sec)
        except asyncio.CancelledError:
            task.cancel()
            await self._drain_cancelled_call(task)
            raise
        if not done:
            task.cancel()
            await self._drain_cancelled_call(task)
            raise TimeoutError
        return task.result()

    @staticmethod
    async def _drain_cancelled_call(task: asyncio.Task[Any]) -> None:
        current = asyncio.current_task()
        if current is not None:
            while current.cancelling():
                current.uncancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if current is not None:
                    while current.cancelling():
                        current.uncancel()
        with suppress(asyncio.CancelledError):
            task.result()

    async def _ensure_worker_locked(self) -> _WorkerHandle:
        with self._state_lock:
            if self._closed:
                raise _WorkerError("service_unavailable", "web_surfer adapter is closed")
            current = self._worker
        if current is not None and current.process.returncode is None:
            return current
        if current is not None:
            await self._terminate_worker(current)
        module_candidates = self._module_candidates
        if not module_candidates:
            raise _WorkerError("service_unavailable", "web_surfer module is unavailable")

        loop = asyncio.get_running_loop()
        connection: asyncio.Future[
            tuple[asyncio.StreamReader, asyncio.StreamWriter, int]
        ] = loop.create_future()
        spawned_pid: asyncio.Future[int] = loop.create_future()
        token = secrets.token_urlsafe(32)

        async def authenticate(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                if not line or len(line) > _MAX_FRAME_BYTES:
                    raise ValueError("invalid worker hello frame")
                hello = json.loads(line)
                pid = hello.get("pid") if isinstance(hello, dict) else None
                if (
                    not isinstance(hello, dict)
                    or hello.get("protocol") != WEB_WORKER_PROTOCOL
                    or hello.get("type") != "hello"
                    or not secrets.compare_digest(str(hello.get("token") or ""), token)
                    or not isinstance(pid, int)
                    or pid <= 0
                ):
                    raise ValueError("worker hello authentication failed")
                expected_pid = await asyncio.wait_for(
                    asyncio.shield(spawned_pid), timeout=2.0
                )
                if pid != expected_pid:
                    raise ValueError("worker hello PID does not match spawned process")
                peer_pid = _worker_peer_pid(writer)
                if peer_pid is not None and peer_pid != expected_pid:
                    raise ValueError("worker IPC peer PID does not match spawned process")
                if connection.done():
                    raise ValueError("worker connection is already established")
                connection.set_result((reader, writer, pid))
            except BaseException:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()

        def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            asyncio.create_task(authenticate(reader, writer))

        ipc_name: str | None = None
        if sys.platform.startswith("linux"):
            ipc_name = f"jarvis-gpt.web.{os.getpid()}.{uuid.uuid4().hex}"
            server = await asyncio.start_unix_server(
                connected,
                path=f"\0{ipc_name}",
                limit=_MAX_FRAME_BYTES,
            )
        else:
            server = await asyncio.start_server(
                connected,
                host="127.0.0.1",
                port=0,
                limit=_MAX_FRAME_BYTES,
            )
        sockets = server.sockets or ()
        if not sockets:
            server.close()
            raise _WorkerError("worker_start_failed", "worker IPC listener has no socket")
        port = 0 if ipc_name is not None else int(sockets[0].getsockname()[1])
        creationflags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
                getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            )
        guard_read_fd: int | None = None
        guard_write_fd: int | None = None
        if os.name != "nt":
            guard_read_fd, guard_write_fd = os.pipe()
            os.set_inheritable(guard_read_fd, True)
            os.set_inheritable(guard_write_fd, False)
        try:
            environment = dict(os.environ)
            package_source_root = str(Path(__file__).resolve().parent.parent)
            existing_pythonpath = environment.get("PYTHONPATH", "")
            pythonpath_entries = [
                item for item in existing_pythonpath.split(os.pathsep) if item
            ]
            inherited_import_roots = [
                item
                for item in sys.path
                if item and Path(item).is_absolute() and Path(item).is_dir()
            ]
            environment["PYTHONPATH"] = os.pathsep.join(
                dict.fromkeys(
                    (package_source_root, *inherited_import_roots, *pythonpath_entries)
                )
            )
            environment["JARVIS_WEB_WORKER_TOKEN"] = token
            environment["JARVIS_WEB_PARENT_PID"] = str(os.getpid())
            if ipc_name is not None:
                environment["JARVIS_WEB_IPC_NAME"] = ipc_name
            if guard_read_fd is not None:
                environment["JARVIS_WEB_PARENT_FD"] = str(guard_read_fd)
            worker_executable = (
                str(getattr(sys, "_base_executable", sys.executable))
                if os.name == "nt"
                else sys.executable
            )
            process_options: dict[str, Any] = {
                "stdin": asyncio.subprocess.DEVNULL,
                "stdout": asyncio.subprocess.DEVNULL,
                "stderr": asyncio.subprocess.DEVNULL,
                "env": environment,
                "creationflags": creationflags,
                "start_new_session": start_new_session,
            }
            if guard_read_fd is not None:
                process_options["pass_fds"] = (guard_read_fd,)
            process = await asyncio.create_subprocess_exec(
                worker_executable,
                "-m",
                "jarvis_gpt.web_surfer_worker",
                str(port),
                *module_candidates,
                **process_options,
            )
            if guard_read_fd is not None:
                os.close(guard_read_fd)
                guard_read_fd = None
            spawned_pid.set_result(process.pid)
        except BaseException:
            server.close()
            if guard_read_fd is not None:
                os.close(guard_read_fd)
            if guard_write_fd is not None:
                os.close(guard_write_fd)
            if not spawned_pid.done():
                spawned_pid.cancel()
            raise
        job: _WindowsJob | None = None
        accepted_writer: asyncio.StreamWriter | None = None
        process_waiter: asyncio.Task[int] | None = None
        try:
            if os.name == "nt":
                job = _WindowsJob.assign(process.pid)
                _resume_windows_process(process.pid)
            process_waiter = asyncio.create_task(process.wait())
            done, _pending = await asyncio.wait(
                {connection, process_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if connection not in done:
                exit_code = process_waiter.result()
                raise _WorkerError(
                    "worker_start_failed",
                    f"web worker exited before IPC authentication with code {exit_code}",
                )
            process_waiter.cancel()
            with suppress(asyncio.CancelledError):
                await process_waiter
            process_waiter = None
            reader, writer, service_pid = connection.result()
            accepted_writer = writer
            server.close()
            self._worker_generation += 1
            handle = _WorkerHandle(
                process=process,
                job=job,
                generation=self._worker_generation,
                reader=reader,
                writer=writer,
                service_pid=service_pid,
                parent_guard_fd=guard_write_fd,
            )
            guard_write_fd = None
            with self._state_lock:
                if self._closed:
                    raise _WorkerError("service_unavailable", "web_surfer adapter is closed")
                self._worker = handle
            response = await self._read_worker_frame(handle)
            if response.get("protocol") != WEB_WORKER_PROTOCOL or response.get("type") != "ready":
                raise _WorkerError("protocol_error", "invalid web worker startup response")
            if not response.get("ok"):
                error = response.get("error")
                message = (
                    str(error.get("message") or "web_surfer contract validation failed")
                    if isinstance(error, dict)
                    else "web_surfer contract validation failed"
                )
                raise _WorkerError("service_unavailable", message, permanent=True)
            ready_pid = response.get("pid")
            reported_service_pid = response.get("service_pid", ready_pid)
            service_name = response.get("service")
            if ready_pid != service_pid:
                raise _WorkerError("protocol_error", "web worker returned an invalid PID")
            if not isinstance(reported_service_pid, int) or reported_service_pid <= 0:
                raise _WorkerError(
                    "protocol_error", "web worker returned an invalid service PID"
                )
            if not isinstance(service_name, str) or service_name not in module_candidates:
                raise _WorkerError("protocol_error", "web worker returned an invalid service")
            self._module_name = service_name
            self._service_name = service_name
            handle.service_pid = reported_service_pid
            self._remember_worker_tree(handle)
            handle.watcher = asyncio.create_task(
                self._watch_worker_exit(handle),
                name=f"web-surfer-watch-{process.pid}",
            )
            return handle
        except BaseException:
            server.close()
            if process_waiter is not None:
                process_waiter.cancel()
                with suppress(asyncio.CancelledError):
                    await process_waiter
            handle = locals().get("handle")
            if isinstance(handle, _WorkerHandle):
                await self._terminate_worker(handle)
            else:
                if accepted_writer is not None:
                    accepted_writer.close()
                    with suppress(Exception):
                        await accepted_writer.wait_closed()
                if job is not None:
                    with suppress(OSError):
                        job.terminate()
                    job.close()
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                with suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=_WORKER_STOP_TIMEOUT)
                if guard_write_fd is not None:
                    os.close(guard_write_fd)
            raise

    async def _write_worker_frame(
        self, handle: _WorkerHandle, payload: Mapping[str, Any]
    ) -> None:
        writer = handle.writer
        if writer.is_closing():
            raise _WorkerError("worker_stopped", "web worker input is closed")
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_ARGUMENT_BYTES + 64 * 1024:
            raise _WorkerError("invalid_arguments", "web worker request frame is too large")
        writer.write(encoded + b"\n")
        await writer.drain()

    async def _read_worker_frame(self, handle: _WorkerHandle) -> dict[str, Any]:
        reader = handle.reader
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise _WorkerError("protocol_error", "web worker response is too large") from exc
        if not line:
            code = handle.process.returncode
            raise _WorkerError("worker_stopped", f"web worker exited with code {code}")
        if len(line) > _MAX_FRAME_BYTES:
            raise _WorkerError("protocol_error", "web worker response is too large")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _WorkerError("protocol_error", "web worker returned malformed JSON") from exc
        if not isinstance(value, dict):
            raise _WorkerError("protocol_error", "web worker response must be an object")
        return value

    async def _invoke_in_process(
        self,
        mode: str,
        method: Any,
        arguments: dict[str, Any],
        *,
        timeout_sec: float,
    ) -> Any:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("web_surfer adapter is closed")
            existing = self._async_inflight.get(mode)
            if existing is not None and not existing.done():
                raise RuntimeError(
                    f"web_surfer.{mode} is still running after an earlier timeout"
                )
            task = asyncio.create_task(method(**arguments))
            self._async_inflight[mode] = task

        def clear(completed: asyncio.Task[Any]) -> None:
            with self._state_lock:
                if self._async_inflight.get(mode) is completed:
                    self._async_inflight.pop(mode, None)
            if not completed.cancelled():
                with suppress(asyncio.CancelledError, Exception):
                    completed.exception()

        task.add_done_callback(clear)
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout_sec)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not done:
            task.cancel()
            raise TimeoutError
        try:
            return task.result()
        except asyncio.CancelledError as exc:
            raise RuntimeError(f"web_surfer.{mode} cancelled its own operation") from exc

    async def _finish_cleanup_after_cancellation(self, handle: _WorkerHandle) -> None:
        current = asyncio.current_task()
        if current is not None:
            while current.cancelling():
                current.uncancel()
        cleanup = asyncio.create_task(self._terminate_worker(handle))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                if current is not None:
                    while current.cancelling():
                        current.uncancel()
        await cleanup

    async def _terminate_worker(
        self,
        handle: _WorkerHandle,
        *,
        graceful: bool = False,
    ) -> None:
        async with handle.termination_lock:
            if handle.terminated:
                return
            process = handle.process
            if graceful and process.returncode is None:
                request_id = uuid.uuid4().hex
                with suppress(Exception):
                    await self._write_worker_frame(
                        handle,
                        {
                            "protocol": WEB_WORKER_PROTOCOL,
                            "type": "shutdown",
                            "request_id": request_id,
                        },
                    )
                    response = await asyncio.wait_for(
                        self._read_worker_frame(handle), timeout=0.5
                    )
                    if (
                        response.get("type") == "shutdown"
                        and response.get("request_id") == request_id
                        and response.get("ok") is True
                    ):
                        await asyncio.wait_for(
                            process.wait(), timeout=_WORKER_STOP_TIMEOUT
                        )
            if os.name != "nt" and handle.parent_guard_fd is not None:
                os.close(handle.parent_guard_fd)
                handle.parent_guard_fd = None
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=0.75)
            if os.name != "nt":
                self._kill_tracked_worker_processes(handle)
                if process.returncode is None:
                    frozen_pids = self._freeze_posix_process_tree(process.pid)
                    for pid in sorted(frozen_pids, reverse=True):
                        if pid == process.pid:
                            continue
                        with suppress(ProcessLookupError, PermissionError):
                            os.kill(pid, signal.SIGKILL)
                # A process group survives its leader.  Always address the owned
                # group so a crashed supervisor cannot orphan its service/browser
                # descendants until another request happens to arrive.
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
            if process.returncode is None:
                if handle.job is not None:
                    with suppress(OSError):
                        handle.job.terminate()
                elif os.name == "nt":
                    with suppress(ProcessLookupError):
                        process.kill()
                with suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=_WORKER_STOP_TIMEOUT)
            if process.returncode is None:
                if os.name != "nt":
                    with suppress(ProcessLookupError, PermissionError):
                        os.killpg(process.pid, signal.SIGKILL)
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=_WORKER_STOP_TIMEOUT)
            writer = handle.writer
            if not writer.is_closing():
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
            if handle.job is not None:
                handle.job.close()
            for descriptor in handle.tracked_pidfds.values():
                with suppress(OSError):
                    os.close(descriptor)
            handle.tracked_pidfds.clear()
            handle.terminated = True
            with self._state_lock:
                if self._worker is handle:
                    self._worker = None

    async def _watch_worker_exit(self, handle: _WorkerHandle) -> None:
        """Tear down the containment object as soon as its leader exits."""

        wait_task = asyncio.create_task(handle.process.wait())
        while not wait_task.done():
            if os.name != "nt":
                self._remember_worker_tree(handle)
            await asyncio.wait({wait_task}, timeout=0.02)
        await wait_task
        with self._state_lock:
            owned = self._worker is handle and not self._closed
        if owned:
            await self._terminate_worker(handle)

    @staticmethod
    def _remember_worker_tree(handle: _WorkerHandle) -> None:
        pidfd_open = getattr(os, "pidfd_open", None)
        if not callable(pidfd_open):
            return
        for pid, descriptor in tuple(handle.tracked_pidfds.items()):
            try:
                readable, _writable, _exceptional = select.select(
                    (descriptor,), (), (), 0
                )
            except (OSError, ValueError):
                readable = (descriptor,)
            if readable:
                with suppress(OSError):
                    os.close(descriptor)
                handle.tracked_pidfds.pop(pid, None)
        for node in process_tree_snapshot(handle.process.pid):
            if (
                node.pid == handle.process.pid
                or node.pid in handle.tracked_pidfds
                or len(handle.tracked_pidfds) >= 4096
            ):
                continue
            with suppress(OSError):
                handle.tracked_pidfds[node.pid] = pidfd_open(node.pid, 0)

    @staticmethod
    def _kill_tracked_worker_processes(handle: _WorkerHandle) -> None:
        pidfd_send = getattr(signal, "pidfd_send_signal", None)
        if not callable(pidfd_send):
            return
        for descriptor in tuple(handle.tracked_pidfds.values()):
            with suppress(ProcessLookupError, PermissionError, OSError):
                pidfd_send(descriptor, signal.SIGKILL, None, 0)

    @staticmethod
    def _freeze_posix_process_tree(root_pid: int) -> tuple[int, ...]:
        """Freeze a POSIX descendant tree before killing escaped process groups."""

        discovered: set[int] = {root_pid}
        stable_passes = 0
        for _attempt in range(8):
            current = {node.pid for node in process_tree_snapshot(root_pid)}
            new_pids = current - discovered
            discovered.update(current)
            for pid in current:
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGSTOP)
            if new_pids:
                stable_passes = 0
            else:
                stable_passes += 1
                if stable_passes >= 2:
                    break
        return tuple(sorted(discovered))

    async def aclose(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            handle = self._worker
            tasks = tuple(self._async_inflight.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if handle is not None:
            cleanup = asyncio.create_task(
                self._terminate_worker(handle, graceful=True)
            )
            current = asyncio.current_task()
            cancellation_requested = False
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.cancelled():
                        raise
                    cancellation_requested = True
                    if current is not None:
                        while current.cancelling():
                            current.uncancel()
            cleanup.result()
            if cancellation_requested:
                raise asyncio.CancelledError

    def close(self) -> None:
        """Synchronously terminate and reap the worker tree.

        This path is used by synchronous context-manager teardown in the CLI,
        including while an ``asyncio.run`` loop is still active.  It therefore
        cannot defer cleanup to another task.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            running_loop = False
        else:
            running_loop = True
        with self._state_lock:
            if self._closed:
                return
            if running_loop and self._worker is not None:
                raise RuntimeError(
                    "an active web worker must be closed with `await adapter.aclose()`"
                )
            self._closed = True
            handle = self._worker
            self._worker = None
            tasks = tuple(self._async_inflight.values())
        for task in tasks:
            if not task.done():
                loop = task.get_loop()
                if loop.is_running():
                    loop.call_soon_threadsafe(task.cancel)
        if handle is None:
            return
        if not handle.writer.is_closing():
            handle.writer.close()
        transport = getattr(handle.process, "_transport", None)
        raw_process = (
            transport.get_extra_info("subprocess") if transport is not None else None
        )
        if os.name != "nt" and handle.parent_guard_fd is not None:
            os.close(handle.parent_guard_fd)
            handle.parent_guard_fd = None
            if isinstance(raw_process, subprocess.Popen):
                with suppress(subprocess.TimeoutExpired):
                    raw_process.wait(timeout=_WORKER_STOP_TIMEOUT)
        process_is_running = (
            raw_process.poll() is None
            if isinstance(raw_process, subprocess.Popen)
            else handle.process.returncode is None
        )
        if os.name != "nt":
            self._kill_tracked_worker_processes(handle)
        if handle.job is not None:
            with suppress(OSError):
                handle.job.terminate()
        elif os.name != "nt":
            if process_is_running:
                frozen_pids = self._freeze_posix_process_tree(handle.process.pid)
                for pid in sorted(frozen_pids, reverse=True):
                    if pid == handle.process.pid:
                        continue
                    with suppress(ProcessLookupError, PermissionError):
                        os.kill(pid, signal.SIGKILL)
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(handle.process.pid, signal.SIGKILL)
        elif process_is_running:
            with suppress(ProcessLookupError):
                handle.process.kill()
        if isinstance(raw_process, subprocess.Popen):
            try:
                raw_process.wait(timeout=_WORKER_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    raw_process.kill()
                with suppress(subprocess.TimeoutExpired):
                    raw_process.wait(timeout=_WORKER_STOP_TIMEOUT)
        if transport is not None:
            with suppress(Exception):
                transport.close()
        if handle.job is not None:
            handle.job.close()
        for descriptor in handle.tracked_pidfds.values():
            with suppress(OSError):
                os.close(descriptor)
        handle.tracked_pidfds.clear()
        handle.terminated = True

    def _error(
        self,
        mode: str,
        code: str,
        message: str,
        *,
        started: float,
        unavailable: bool = False,
    ) -> WebSurferResult:
        return WebSurferResult(
            protocol=WEB_ADAPTER_PROTOCOL,
            ok=False,
            mode=mode,
            error={"code": code, "message": redact_text(message)[:2000]},
            unavailable=unavailable,
            service=self._service_name,
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        )
