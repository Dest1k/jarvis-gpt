from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import os
import re
import shutil
import signal
import socket
import stat
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeAlias

from .execution_filesystem import (
    BoundPath,
    PathMutationGuard,
    directory_entries,
    metadata_identity,
    verified_binary_handle,
)
from .execution_models import ActionFeedback
from .execution_process import AsyncProcessRunner, ProcessRequest
from .execution_session import SessionRegistry

_REGISTRY_KEY_RE = re.compile(r"^[^\x00:*?\"<>|]{1,1024}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_()]*$")


class PathPolicy:
    """Resolves operation paths and rejects traversal or symlink escapes."""

    def __init__(
        self,
        allowed_roots: tuple[Path, ...],
        *,
        denied_paths: tuple[Path, ...] = (),
    ) -> None:
        if not allowed_roots:
            raise ValueError("at least one allowed root is required")
        roots: list[Path] = []
        for root in allowed_roots:
            if not root.is_absolute():
                raise ValueError("allowed roots must be absolute")
            resolved = root.resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError(f"allowed root is not a directory: {resolved}")
            roots.append(resolved)
        self._roots = tuple(dict.fromkeys(roots))
        self._root_identities = {
            root: metadata_identity(root.stat(follow_symlinks=False)) for root in self._roots
        }
        denied: list[Path] = []
        for path in denied_paths:
            if not path.is_absolute():
                raise ValueError("denied paths must be absolute")
            resolved = path.resolve(strict=False)
            if resolved not in denied:
                denied.append(resolved)
        self._denied = tuple(denied)
        self._active_mutation_guard: ContextVar[PathMutationGuard | None] = ContextVar(
            f"jarvis_path_guard_{id(self)}", default=None
        )

    @property
    def roots(self) -> tuple[Path, ...]:
        return self._roots

    @property
    def denied_paths(self) -> tuple[Path, ...]:
        return self._denied

    def resolve(
        self,
        path: Path,
        *,
        must_exist: bool = False,
        allow_root: bool = False,
    ) -> Path:
        if not path.is_absolute():
            raise ValueError("action paths must be absolute")
        if "\x00" in os.fspath(path):
            raise ValueError("action paths cannot contain NUL")
        if path.is_symlink():
            raise ValueError("action paths cannot be symbolic links or reparse aliases")
        try:
            resolved = path.resolve(strict=must_exist)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"path cannot be resolved: {path}") from exc
        matching_root = next(
            (root for root in self._roots if resolved == root or resolved.is_relative_to(root)),
            None,
        )
        if matching_root is None:
            raise ValueError(f"path escapes allowed roots: {path}")
        if any(resolved == denied or resolved.is_relative_to(denied) for denied in self._denied):
            raise PermissionError(f"path is reserved for runtime secrets or state: {path}")
        if resolved == matching_root and not allow_root:
            raise ValueError("the allowed root itself cannot be mutated")
        return resolved

    @contextlib.contextmanager
    def mutation_scope(self, paths: tuple[Path, ...]) -> Iterator[PathMutationGuard]:
        """Pin path ancestry for one mutation or an entire transaction."""

        current = self._active_mutation_guard.get()
        if current is not None:
            for path in paths:
                with contextlib.suppress(FileNotFoundError):
                    bound = current.bind(path, allow_root=True)
                    if os.name != "nt" and stat.S_ISDIR(bound.lstat().st_mode):
                        current.bind(path / ".jarvis-directory-pin")
            yield current
            return
        guard = PathMutationGuard(
            self._roots,
            self._denied,
            self._root_identities,
        )
        token = self._active_mutation_guard.set(guard)
        try:
            # Eagerly pin every currently existing parent. Missing destination
            # parents are created later through the same root-anchored guard.
            for path in paths:
                with contextlib.suppress(FileNotFoundError):
                    bound = guard.bind(path, allow_root=True)
                    if os.name != "nt" and stat.S_ISDIR(bound.lstat().st_mode):
                        guard.bind(path / ".jarvis-directory-pin")
            yield guard
        finally:
            self._active_mutation_guard.reset(token)
            guard.close()

    def bind_mutation_path(
        self,
        path: Path,
        *,
        create_parents: bool = False,
        allow_root: bool = False,
    ) -> BoundPath:
        guard = self._active_mutation_guard.get()
        if guard is None:
            raise RuntimeError("filesystem mutations require an active path guard")
        return guard.bind(
            path,
            create_parents=create_parents,
            allow_root=allow_root,
        )

    def release_mutation_descendants(self, path: Path) -> None:
        guard = self._active_mutation_guard.get()
        if guard is None:
            raise RuntimeError("filesystem mutations require an active path guard")
        guard.release_descendants(path)

    @property
    def active_mutation_guard(self) -> PathMutationGuard | None:
        return self._active_mutation_guard.get()


class RegistryHive(StrEnum):
    CURRENT_USER = "HKEY_CURRENT_USER"
    LOCAL_MACHINE = "HKEY_LOCAL_MACHINE"


class RegistryValueKind(StrEnum):
    STRING = "string"
    EXPAND_STRING = "expand_string"
    DWORD = "dword"
    QWORD = "qword"
    BINARY = "binary"


@dataclass(frozen=True, slots=True)
class CreateDirectoryAction:
    path: Path
    parents: bool = True
    action_id: str = field(default_factory=lambda: _new_id("mkdir"))


@dataclass(frozen=True, slots=True)
class StatPathAction:
    path: Path
    action_id: str = field(default_factory=lambda: _new_id("stat"))


@dataclass(frozen=True, slots=True)
class ListDirectoryAction:
    path: Path
    max_entries: int = 1000
    action_id: str = field(default_factory=lambda: _new_id("list"))


@dataclass(frozen=True, slots=True)
class ReadFileAction:
    path: Path
    offset: int = 0
    max_bytes: int = 1024 * 1024
    action_id: str = field(default_factory=lambda: _new_id("read"))


@dataclass(frozen=True, slots=True)
class WriteFileAction:
    path: Path
    content: bytes
    create_parents: bool = False
    require_absent: bool = False
    expected_sha256: str | None = None
    mode: int | None = None
    action_id: str = field(default_factory=lambda: _new_id("write"))


@dataclass(frozen=True, slots=True)
class CopyFileAction:
    source: Path
    destination: Path
    overwrite: bool = False
    create_parents: bool = False
    expected_sha256: str | None = None
    action_id: str = field(default_factory=lambda: _new_id("copy"))


@dataclass(frozen=True, slots=True)
class MoveFileAction:
    source: Path
    destination: Path
    overwrite: bool = False
    create_parents: bool = False
    expected_sha256: str | None = None
    action_id: str = field(default_factory=lambda: _new_id("move"))


@dataclass(frozen=True, slots=True)
class DeleteFileAction:
    path: Path
    missing_ok: bool = False
    expected_sha256: str | None = None
    action_id: str = field(default_factory=lambda: _new_id("delete"))


@dataclass(frozen=True, slots=True)
class ProcessAction:
    request: ProcessRequest
    session_id: str | None = None
    action_id: str = field(default_factory=lambda: _new_id("process"))


class ProcessSignal(StrEnum):
    INTERRUPT = "interrupt"
    TERMINATE = "terminate"
    KILL = "kill"


@dataclass(frozen=True, slots=True)
class TerminateOwnedProcessAction:
    session_id: str
    pid: int
    signal: ProcessSignal = ProcessSignal.TERMINATE
    action_id: str = field(default_factory=lambda: _new_id("terminate"))


@dataclass(frozen=True, slots=True)
class ResolveHostAction:
    host: str
    port: int = 443
    action_id: str = field(default_factory=lambda: _new_id("resolve"))


@dataclass(frozen=True, slots=True)
class TcpProbeAction:
    host: str
    port: int
    timeout_seconds: float = 5.0
    action_id: str = field(default_factory=lambda: _new_id("tcp"))


@dataclass(frozen=True, slots=True)
class RegistrySetAction:
    hive: RegistryHive
    key: str
    name: str
    value: str | int | bytes
    value_kind: RegistryValueKind
    action_id: str = field(default_factory=lambda: _new_id("regset"))


@dataclass(frozen=True, slots=True)
class RegistryGetAction:
    hive: RegistryHive
    key: str
    name: str
    action_id: str = field(default_factory=lambda: _new_id("regget"))


@dataclass(frozen=True, slots=True)
class RegistryDeleteValueAction:
    hive: RegistryHive
    key: str
    name: str
    missing_ok: bool = False
    action_id: str = field(default_factory=lambda: _new_id("regdelete"))


AtomicAction: TypeAlias = (
    CreateDirectoryAction
    | StatPathAction
    | ListDirectoryAction
    | ReadFileAction
    | WriteFileAction
    | CopyFileAction
    | MoveFileAction
    | DeleteFileAction
    | ProcessAction
    | TerminateOwnedProcessAction
    | ResolveHostAction
    | TcpProbeAction
    | RegistrySetAction
    | RegistryGetAction
    | RegistryDeleteValueAction
)


class AtomicActionExecutor:
    def __init__(
        self,
        *,
        path_policy: PathPolicy,
        process_runner: AsyncProcessRunner | None = None,
        sessions: SessionRegistry | None = None,
        allow_private_network: bool = False,
    ):
        self.path_policy = path_policy
        self.process_runner = process_runner or AsyncProcessRunner(
            observation_roots=path_policy.roots
        )
        self.sessions = sessions
        self.allow_private_network = allow_private_network

    async def execute(self, action: AtomicAction) -> ActionFeedback:
        mutation_paths = _filesystem_mutation_paths(action)
        guarded_paths = mutation_paths or _filesystem_read_paths(action)
        if guarded_paths and self.path_policy.active_mutation_guard is None:
            try:
                with self.path_policy.mutation_scope(guarded_paths):
                    return await self.execute(action)
            except (
                OSError,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                PermissionError,
            ) as exc:
                return ActionFeedback(
                    ok=False,
                    action_id=getattr(action, "action_id", "invalid"),
                    kind=type(action).__name__,
                    summary="Atomic action failed validation or execution.",
                    error=f"{type(exc).__name__}: {exc}",
                )
        try:
            if isinstance(action, CreateDirectoryAction):
                return await asyncio.to_thread(self._create_directory, action)
            if isinstance(action, StatPathAction):
                return await asyncio.to_thread(self._stat_path, action)
            if isinstance(action, ListDirectoryAction):
                return await asyncio.to_thread(self._list_directory, action)
            if isinstance(action, ReadFileAction):
                return await asyncio.to_thread(self._read_file, action)
            if isinstance(action, WriteFileAction):
                return await asyncio.to_thread(self._write_file, action)
            if isinstance(action, CopyFileAction):
                return await asyncio.to_thread(self._copy_file, action)
            if isinstance(action, MoveFileAction):
                return await asyncio.to_thread(self._move_file, action)
            if isinstance(action, DeleteFileAction):
                return await asyncio.to_thread(self._delete_file, action)
            if isinstance(action, ProcessAction):
                return await self._run_process(action)
            if isinstance(action, TerminateOwnedProcessAction):
                return await asyncio.to_thread(self._terminate_owned_process, action)
            if isinstance(action, ResolveHostAction):
                return await self._resolve_host(action)
            if isinstance(action, TcpProbeAction):
                return await self._tcp_probe(action)
            if isinstance(action, RegistrySetAction):
                return await asyncio.to_thread(self._registry_set, action)
            if isinstance(action, RegistryGetAction):
                return await asyncio.to_thread(self._registry_get, action)
            if isinstance(action, RegistryDeleteValueAction):
                return await asyncio.to_thread(self._registry_delete, action)
            raise TypeError(f"unsupported atomic action: {type(action).__name__}")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, PermissionError) as exc:
            return ActionFeedback(
                ok=False,
                action_id=getattr(action, "action_id", "invalid"),
                kind=type(action).__name__,
                summary="Atomic action failed validation or execution.",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _create_directory(self, action: CreateDirectoryAction) -> ActionFeedback:
        path = self.path_policy.bind_mutation_path(
            action.path,
            create_parents=action.parents,
        )
        existed = path.exists()
        if existed and not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError("destination exists and is not a directory")
        if not existed:
            path.mkdir()
        _fsync_bound_parent(path)
        return _feedback(
            action,
            ok=True,
            summary="Directory exists.",
            before={"exists": existed},
            after=_bound_path_state(path),
        )

    def _stat_path(self, action: StatPathAction) -> ActionFeedback:
        path = self.path_policy.bind_mutation_path(action.path, allow_root=True)
        if not path.exists():
            raise FileNotFoundError(path.path)
        return _feedback(
            action,
            ok=True,
            summary="Filesystem metadata read.",
            after=_bound_path_state(path),
        )

    def _list_directory(self, action: ListDirectoryAction) -> ActionFeedback:
        path = self.path_policy.bind_mutation_path(action.path, allow_root=True)
        if not path.exists() or not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError("list target must be a directory")
        if not 1 <= action.max_entries <= 10_000:
            raise ValueError("max_entries must be between 1 and 10000")
        entries = []
        truncated = False
        snapshots = directory_entries(path, max_entries=action.max_entries + 1)
        for index, entry in enumerate(snapshots):
            if index >= action.max_entries:
                truncated = True
                break
            entries.append(
                {
                    "name": entry.name,
                    "kind": entry.kind,
                    "size": entry.size,
                    "mtime_ns": entry.mtime_ns,
                }
            )
        entries.sort(key=lambda item: (item["kind"], item["name"].casefold()))
        return _feedback(
            action,
            ok=True,
            summary=f"Listed {len(entries)} filesystem entries.",
            after={"path": str(path.path), "entries": entries, "truncated": truncated},
        )

    def _read_file(self, action: ReadFileAction) -> ActionFeedback:
        path = self.path_policy.bind_mutation_path(action.path)
        if not path.exists() or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("read target must be a regular file")
        if (
            isinstance(action.offset, bool)
            or not isinstance(action.offset, int)
            or action.offset < 0
        ):
            raise ValueError("offset must be a non-negative integer")
        if not 1 <= action.max_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 16777216")
        with verified_binary_handle(path) as handle:
            handle.seek(action.offset)
            content = handle.read(action.max_bytes + 1)
        truncated = len(content) > action.max_bytes
        content = content[: action.max_bytes]
        return _feedback(
            action,
            ok=True,
            summary=f"Read {len(content)} byte(s).",
            after={
                "path": str(path.path),
                "offset": action.offset,
                "content_base64": base64.b64encode(content).decode("ascii"),
                "bytes": len(content),
                "truncated": truncated,
            },
        )

    def _write_file(self, action: WriteFileAction) -> ActionFeedback:
        if not isinstance(action.content, bytes):
            raise TypeError("content must be bytes")
        path = self.path_policy.bind_mutation_path(
            action.path,
            create_parents=action.create_parents,
        )
        before = _bound_path_state(path)
        if path.exists() and not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("destination exists and is not a regular file")
        if action.require_absent and path.exists():
            raise FileExistsError(path)
        _validate_expected_digest_bound(path, action.expected_sha256)
        if action.mode is not None and not 0 <= action.mode <= 0o777:
            raise ValueError("mode must be between 0 and 0o777")
        descriptor, temporary = _temporary_bound_file(path)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(action.content)
                handle.flush()
                os.fsync(handle.fileno())
            if action.mode is not None:
                temporary.chmod(action.mode)
            _install_temporary_file(
                temporary,
                path,
                overwrite=not action.require_absent,
                expected_sha256=action.expected_sha256,
            )
            _fsync_bound_parent(path)
        finally:
            _unlink_bound_if_exists(temporary)
        return _feedback(
            action,
            ok=True,
            summary="File written atomically.",
            before=before,
            after=_bound_path_state(path),
        )

    def _copy_file(self, action: CopyFileAction) -> ActionFeedback:
        source = self.path_policy.bind_mutation_path(action.source)
        destination = self.path_policy.bind_mutation_path(
            action.destination,
            create_parents=action.create_parents,
        )
        if not source.exists() or not stat.S_ISREG(source.lstat().st_mode):
            raise ValueError("source must be a regular file")
        _validate_expected_digest_bound(source, action.expected_sha256)
        before = _bound_path_state(destination)
        _prepare_bound_destination(destination, action.overwrite)
        descriptor, temporary = _temporary_bound_file(destination)
        try:
            with (
                os.fdopen(descriptor, "wb") as target_handle,
                verified_binary_handle(source) as source_handle,
            ):
                shutil.copyfileobj(source_handle, target_handle, length=128 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
                source_mode = stat.S_IMODE(os.fstat(source_handle.fileno()).st_mode)
            temporary.chmod(source_mode)
            _install_temporary_file(
                temporary,
                destination,
                overwrite=action.overwrite,
            )
            _fsync_bound_parent(destination)
        finally:
            _unlink_bound_if_exists(temporary)
        return _feedback(
            action,
            ok=True,
            summary="File copied atomically.",
            before=before,
            after=_bound_path_state(destination),
        )

    def _move_file(self, action: MoveFileAction) -> ActionFeedback:
        source = self.path_policy.bind_mutation_path(action.source)
        destination = self.path_policy.bind_mutation_path(
            action.destination,
            create_parents=action.create_parents,
        )
        if not source.exists() or not stat.S_ISREG(source.lstat().st_mode):
            raise ValueError("source must be a regular file")
        _validate_expected_digest_bound(source, action.expected_sha256)
        before = {
            "source": _bound_path_state(source),
            "destination": _bound_path_state(destination),
        }
        _prepare_bound_destination(destination, action.overwrite)
        _move_bound_file(source, destination, overwrite=action.overwrite)
        _fsync_bound_parent(source)
        _fsync_bound_parent(destination)
        return _feedback(
            action,
            ok=True,
            summary="File moved atomically.",
            before=before,
            after={
                "source": _bound_path_state(source),
                "destination": _bound_path_state(destination),
            },
        )

    def _delete_file(self, action: DeleteFileAction) -> ActionFeedback:
        path = self.path_policy.bind_mutation_path(action.path)
        before = _bound_path_state(path)
        if not path.exists():
            if action.missing_ok:
                return _feedback(action, ok=True, summary="File was already absent.", before=before)
            raise FileNotFoundError(path)
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("only regular files can be deleted")
        _delete_bound_file(path, action.expected_sha256)
        _fsync_bound_parent(path)
        return _feedback(
            action,
            ok=True,
            summary="File deleted.",
            before=before,
            after=_bound_path_state(path),
        )

    async def _run_process(self, action: ProcessAction) -> ActionFeedback:
        session = None
        if action.session_id is not None:
            if self.sessions is None:
                raise RuntimeError("session-bound processes require a SessionRegistry")
            session = self.sessions.get(action.session_id)
            if session is None:
                raise KeyError(f"unknown execution session: {action.session_id}")
        try:
            if action.request.cwd is not None:
                self.path_policy.resolve(
                    action.request.cwd,
                    must_exist=True,
                    allow_root=True,
                )
            for observed in action.request.observe_paths:
                self.path_policy.resolve(observed, allow_root=True)
            feedback = await self.process_runner.run(
                action.request,
                session=session,
                reservation_id=action.action_id if session is not None else None,
            )
        except BaseException:
            if session is not None:
                session.release_process_start(action.action_id)
            raise
        return ActionFeedback(
            ok=feedback.ok,
            action_id=action.action_id,
            kind=type(action).__name__,
            summary="Process exited successfully." if feedback.ok else "Process execution failed.",
            process=feedback,
            error=feedback.error,
        )

    def _terminate_owned_process(self, action: TerminateOwnedProcessAction) -> ActionFeedback:
        if self.sessions is None:
            raise RuntimeError("owned process control requires a SessionRegistry")
        if not isinstance(action.signal, ProcessSignal):
            raise TypeError("signal must be a ProcessSignal")
        if isinstance(action.pid, bool) or not isinstance(action.pid, int) or action.pid <= 0:
            raise ValueError("pid must be a positive integer")
        selected = {
            ProcessSignal.INTERRUPT: signal.SIGINT,
            ProcessSignal.TERMINATE: signal.SIGTERM,
            ProcessSignal.KILL: getattr(signal, "SIGKILL", signal.SIGTERM),
        }[action.signal]
        self.sessions.signal_owned_pid(
            action.session_id,
            action.pid,
            selected,
            finalize=False,
        )
        still_running = self.sessions.owned_process_tree_alive(action.session_id, action.pid)
        if action.signal is not ProcessSignal.INTERRUPT:
            deadline = time.monotonic() + 2.0
            while still_running and time.monotonic() < deadline:
                time.sleep(0.02)
                still_running = self.sessions.owned_process_tree_alive(
                    action.session_id, action.pid
                )
            if still_running:
                raise RuntimeError("owned process did not terminate after signal delivery")
            session = self.sessions.get(action.session_id)
            if session is not None:
                with contextlib.suppress(KeyError, ValueError):
                    session.finish_process(action.pid, exit_code=None, terminated=True)
        return _feedback(
            action,
            ok=True,
            summary=f"Signal {action.signal.value} sent to owned process.",
            after={
                "session_id": action.session_id,
                "pid": action.pid,
                "still_running": still_running,
            },
        )

    async def _resolve_host(self, action: ResolveHostAction) -> ActionFeedback:
        host = _validate_host(action.host)
        port = _validate_port(action.port)
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = sorted({record[4][0] for record in records})
        self._validate_resolved_addresses(addresses)
        return _feedback(
            action,
            ok=bool(addresses),
            summary=f"Resolved {len(addresses)} address(es).",
            after={"host": host, "port": port, "addresses": addresses},
        )

    async def _tcp_probe(self, action: TcpProbeAction) -> ActionFeedback:
        host = _validate_host(action.host)
        port = _validate_port(action.port)
        if not 0.05 <= action.timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be between 0.05 and 60")
        writer: asyncio.StreamWriter | None = None
        try:
            loop = asyncio.get_running_loop()
            records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = sorted({record[4][0] for record in records})
            self._validate_resolved_addresses(addresses)
            if not addresses:
                raise OSError("network target did not resolve")
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(addresses[0], port),
                timeout=action.timeout_seconds,
            )
            peer = writer.get_extra_info("peername")
            return _feedback(
                action,
                ok=True,
                summary="TCP endpoint accepted a connection.",
                after={"host": host, "port": port, "peer": list(peer) if peer else None},
            )
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()

    def _validate_resolved_addresses(self, addresses: list[str]) -> None:
        if self.allow_private_network:
            return
        for raw in addresses:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
            if not address.is_global:
                raise PermissionError(
                    f"resolved network address requires private-network capability: {address}"
                )

    def _registry_set(self, action: RegistrySetAction) -> ActionFeedback:
        winreg = _load_winreg()
        hive, key, name = _validate_registry_target(action.hive, action.key, action.name, winreg)
        value, registry_kind = _validate_registry_value(action.value, action.value_kind, winreg)
        before = _registry_read(winreg, hive, key, name)
        access = winreg.KEY_SET_VALUE
        with winreg.CreateKeyEx(hive, key, 0, access) as handle:
            winreg.SetValueEx(handle, name, 0, registry_kind, value)
        after = _registry_read(winreg, hive, key, name)
        return _feedback(
            action,
            ok=True,
            summary="Registry value set.",
            before=before,
            after=after,
        )

    def _registry_get(self, action: RegistryGetAction) -> ActionFeedback:
        winreg = _load_winreg()
        hive, key, name = _validate_registry_target(action.hive, action.key, action.name, winreg)
        current = _registry_read(winreg, hive, key, name)
        return _feedback(
            action,
            ok=True,
            summary="Registry value inspected.",
            after=current,
        )

    def _registry_delete(self, action: RegistryDeleteValueAction) -> ActionFeedback:
        winreg = _load_winreg()
        hive, key, name = _validate_registry_target(action.hive, action.key, action.name, winreg)
        before = _registry_read(winreg, hive, key, name)
        if not before["exists"] and action.missing_ok:
            return _feedback(action, ok=True, summary="Registry value was already absent.")
        with winreg.OpenKey(hive, key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.DeleteValue(handle, name)
        return _feedback(
            action,
            ok=True,
            summary="Registry value deleted.",
            before=before,
            after={"exists": False},
        )


def _feedback(
    action: AtomicAction,
    *,
    ok: bool,
    summary: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> ActionFeedback:
    return ActionFeedback(
        ok=ok,
        action_id=action.action_id,
        kind=type(action).__name__,
        summary=summary,
        before=before or {},
        after=after or {},
    )


def _filesystem_mutation_paths(action: AtomicAction) -> tuple[Path, ...]:
    if isinstance(action, CreateDirectoryAction | WriteFileAction | DeleteFileAction):
        return (action.path,)
    if isinstance(action, CopyFileAction | MoveFileAction):
        return (action.source, action.destination)
    return ()


def _filesystem_read_paths(action: AtomicAction) -> tuple[Path, ...]:
    if isinstance(action, StatPathAction | ListDirectoryAction | ReadFileAction):
        return (action.path,)
    return ()


def _prepare_bound_destination(destination: BoundPath, overwrite: bool) -> None:
    if not destination.exists():
        return
    metadata = destination.lstat()
    if not overwrite:
        raise FileExistsError(destination.path)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("destination exists and is not a regular file")


def _temporary_bound_file(destination: BoundPath) -> tuple[int, BoundPath]:
    guard_name = f".{destination.name}.{uuid.uuid4().hex}.tmp"
    temporary = destination.sibling(guard_name)
    descriptor = temporary.open(
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    return descriptor, temporary


def _install_temporary_file(
    temporary: BoundPath,
    destination: BoundPath,
    *,
    overwrite: bool,
    expected_sha256: str | None = None,
) -> None:
    staged: BoundPath | None = None
    if expected_sha256 is not None:
        staged = _stage_bound_file(destination, expected_sha256)
    try:
        if overwrite:
            destination.replace_from(temporary)
        else:
            destination.link_from(temporary)
            temporary.unlink()
    except BaseException:
        if staged is not None and not destination.exists():
            destination.replace_from(staged)
        raise
    if staged is not None:
        staged.unlink()


def _move_bound_file(
    source: BoundPath,
    destination: BoundPath,
    *,
    overwrite: bool,
) -> None:
    staged = _stage_bound_file(source, None)
    try:
        if overwrite:
            destination.replace_from(staged)
        else:
            destination.link_from(staged)
            staged.unlink()
    except BaseException:
        if not source.exists() and staged.exists():
            source.replace_from(staged)
        raise


def _delete_bound_file(path: BoundPath, expected_sha256: str | None) -> None:
    staged = _stage_bound_file(path, expected_sha256)
    staged.unlink()


def _stage_bound_file(path: BoundPath, expected_sha256: str | None) -> BoundPath:
    """Move and verify one exact file identity before replacing or unlinking it."""

    if expected_sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("expected_sha256 must contain 64 hexadecimal characters")
    with verified_binary_handle(path) as handle:
        identity = metadata_identity(os.fstat(handle.fileno()))
    staged_name = f".{path.name}.{uuid.uuid4().hex}.jarvis-stage"
    staged = path.sibling(staged_name)
    staged.replace_from(path)
    try:
        if metadata_identity(staged.lstat()) != identity:
            raise RuntimeError("file identity changed before the mutation")
        if expected_sha256 is not None and _bound_file_sha256(staged) != expected_sha256.lower():
            raise RuntimeError("file changed since it was inspected")
    except BaseException:
        if not path.exists() and staged.exists():
            path.replace_from(staged)
        raise
    return staged


def _unlink_bound_if_exists(path: BoundPath) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _fsync_bound_parent(path: BoundPath) -> None:
    if os.name == "nt":
        return
    if path.parent_fd is None:
        _fsync_directory(path.path.parent)
    else:
        os.fsync(path.parent_fd)


def _bound_path_state(path: BoundPath) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"exists": False, "path": str(path.path)}
    kind = (
        "symlink"
        if stat.S_ISLNK(metadata.st_mode)
        else "directory"
        if stat.S_ISDIR(metadata.st_mode)
        else "file"
    )
    result: dict[str, Any] = {
        "exists": True,
        "path": str(path.path),
        "kind": kind,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "mode": stat.S_IMODE(metadata.st_mode),
    }
    if kind == "file":
        result["sha256"] = _bound_file_sha256(path)
    return result


def _validate_expected_digest_bound(path: BoundPath, expected: str | None) -> None:
    if expected is None:
        return
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise ValueError("expected_sha256 must contain 64 hexadecimal characters")
    if not path.exists() or not stat.S_ISREG(path.lstat().st_mode):
        raise FileNotFoundError(path.path)
    if _bound_file_sha256(path) != expected.lower():
        raise RuntimeError("file changed since it was inspected")


def _bound_file_sha256(path: BoundPath) -> str:
    digest = hashlib.sha256()
    with verified_binary_handle(path) as handle:
        while chunk := handle.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_destination(
    destination: Path,
    overwrite: bool,
    create_parents: bool,
    policy: PathPolicy,
) -> None:
    if destination.exists() and not overwrite:
        raise FileExistsError(destination)
    if destination.exists() and not destination.is_file():
        raise ValueError("destination exists and is not a regular file")
    if create_parents:
        parent = policy.resolve(destination.parent, allow_root=True)
        parent.mkdir(parents=True, exist_ok=True)
    if not destination.parent.is_dir():
        raise FileNotFoundError(destination.parent)


def _temporary_file(destination: Path) -> tuple[int, Path]:
    descriptor, raw = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    return descriptor, Path(raw)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _path_state(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {"exists": False, "path": str(path)}
    kind = "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file"
    result: dict[str, Any] = {
        "exists": True,
        "path": str(path),
        "kind": kind,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
        "mode": stat.S_IMODE(metadata.st_mode),
    }
    if kind == "file":
        result["sha256"] = _file_sha256(path)
    return result


def _validate_expected_digest(path: Path, expected: str | None) -> None:
    if expected is None:
        return
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise ValueError("expected_sha256 must contain 64 hexadecimal characters")
    if not path.is_file():
        raise FileNotFoundError(path)
    if _file_sha256(path) != expected.lower():
        raise RuntimeError("file changed since it was inspected")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with _verified_binary_reader(path) as handle:
        while chunk := handle.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def _verified_binary_reader(path: Path):
    with path.open("rb") as handle:
        descriptor_metadata = os.fstat(handle.fileno())
        path_metadata = path.lstat()
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(descriptor_metadata.st_mode):
            raise ValueError("file became a symlink or non-regular object")
        if (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            raise RuntimeError("file identity changed while it was being opened")
        yield handle


def _validate_host(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValueError("host must contain between 1 and 253 characters")
    if any(character.isspace() for character in value) or "\x00" in value:
        raise ValueError("host contains invalid characters")
    candidate = value.rstrip(".")
    with contextlib.suppress(ValueError):
        return str(ipaddress.ip_address(candidate))
    try:
        encoded = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("host is not a valid IDNA name") from exc
    labels = encoded.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        raise ValueError("host is not a valid DNS name")
    return encoded


def _validate_port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    return value


def _load_winreg():
    if os.name != "nt":
        raise OSError("registry actions are available only on Windows")
    import winreg

    return winreg


def _validate_registry_target(hive, key: str, name: str, winreg):
    if not isinstance(hive, RegistryHive):
        raise TypeError("hive must be a RegistryHive")
    if not _REGISTRY_KEY_RE.fullmatch(key) or key.startswith("\\") or ".." in key.split("\\"):
        raise ValueError("invalid registry key")
    if not isinstance(name, str) or not name or len(name) > 16_383 or "\x00" in name:
        raise ValueError("invalid registry value name")
    handle = {
        RegistryHive.CURRENT_USER: winreg.HKEY_CURRENT_USER,
        RegistryHive.LOCAL_MACHINE: winreg.HKEY_LOCAL_MACHINE,
    }[hive]
    return handle, key, name


def _validate_registry_value(value, kind: RegistryValueKind, winreg):
    if kind is RegistryValueKind.STRING:
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("string registry values must be NUL-free strings")
        return value, winreg.REG_SZ
    if kind is RegistryValueKind.EXPAND_STRING:
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("expanded registry values must be NUL-free strings")
        for match in re.findall(r"%([^%]+)%", value):
            if not _ENV_NAME_RE.fullmatch(match):
                raise ValueError("expanded registry value has an invalid variable name")
        return value, winreg.REG_EXPAND_SZ
    if kind is RegistryValueKind.DWORD:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("DWORD registry values must be 32-bit unsigned integers")
        return value, winreg.REG_DWORD
    if kind is RegistryValueKind.QWORD:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError("QWORD registry values must be 64-bit unsigned integers")
        return value, winreg.REG_QWORD
    if kind is RegistryValueKind.BINARY:
        if not isinstance(value, bytes) or len(value) > 16 * 1024 * 1024:
            raise ValueError("binary registry values must be bytes no larger than 16 MiB")
        return value, winreg.REG_BINARY
    raise ValueError("unsupported registry value kind")


def _registry_read(winreg, hive, key: str, name: str) -> dict[str, Any]:
    try:
        with winreg.OpenKey(hive, key, 0, winreg.KEY_QUERY_VALUE) as handle:
            value, kind = winreg.QueryValueEx(handle, name)
    except FileNotFoundError:
        return {"exists": False}
    if re.search(r"(?i)(?:password|passwd|secret|token|credential|private.?key|api.?key)", name):
        encoded = (
            value if isinstance(value, bytes) else str(value).encode("utf-8", errors="replace")
        )
        value = {
            "redacted": True,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size": len(encoded),
        }
    elif isinstance(value, bytes):
        value = {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    return {"exists": True, "value": value, "registry_kind": int(kind)}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"
