from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import signal
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .execution_actions import (
    AtomicAction,
    AtomicActionExecutor,
    CopyFileAction,
    CreateDirectoryAction,
    DeleteFileAction,
    ListDirectoryAction,
    MoveFileAction,
    PathPolicy,
    ProcessAction,
    ReadFileAction,
    RegistryDeleteValueAction,
    RegistryGetAction,
    RegistrySetAction,
    ResolveHostAction,
    StatPathAction,
    TcpProbeAction,
    TerminateOwnedProcessAction,
    WriteFileAction,
)
from .execution_models import ActionFeedback
from .execution_process import AsyncProcessRunner, ExecutablePolicy, ExecutableRule
from .execution_protocol import ActionClass, classify_action, parse_action
from .execution_session import (
    ExecutionSession,
    SessionRegistry,
    SessionStatus,
    StepStatus,
)
from .execution_transaction import CheckpointManager, TransactionalExecutor


@dataclass(frozen=True, slots=True)
class KernelCapabilities:
    executable_rules: tuple[ExecutableRule, ...] = ()
    network_hosts: frozenset[str] = frozenset()
    allow_private_network: bool = False
    registry_read_prefixes: tuple[tuple[str, str], ...] = ()
    registry_write_prefixes: tuple[tuple[str, str], ...] = ()
    allow_inherited_process_environment: bool = False


@dataclass(frozen=True, slots=True)
class KernelResult:
    ok: bool
    action_class: ActionClass
    feedback: ActionFeedback
    transactional: bool
    transaction_status: str | None = None
    checkpoint_id: str | None = None
    replayed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class KernelBatchResult:
    ok: bool
    idempotency_key: str
    feedback: tuple[ActionFeedback, ...]
    transaction_status: str
    checkpoint_id: str | None
    replayed: bool = False


class ExecutionKernel:
    """Capability-gated facade over validation, execution, sessions and rollback."""

    def __init__(
        self,
        *,
        allowed_roots: tuple[Path, ...],
        state_dir: Path,
        denied_paths: tuple[Path, ...] = (),
        capabilities: KernelCapabilities | None = None,
        max_cached_results: int = 1024,
        max_cached_result_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if not state_dir.is_absolute():
            raise ValueError("state_dir must be absolute")
        if not 1 <= max_cached_results <= 100_000:
            raise ValueError("max_cached_results must be between 1 and 100000")
        if not 1024 * 1024 <= max_cached_result_bytes <= 1024 * 1024 * 1024:
            raise ValueError("max_cached_result_bytes must be between 1 MiB and 1 GiB")
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path_policy = PathPolicy(allowed_roots, denied_paths=denied_paths)
        self.capabilities = capabilities or KernelCapabilities()
        runner = AsyncProcessRunner(
            executable_policy=ExecutablePolicy(rules=self.capabilities.executable_rules),
            observation_roots=self.path_policy.roots,
        )
        self.sessions = SessionRegistry()
        self.actions = AtomicActionExecutor(
            path_policy=self.path_policy,
            process_runner=runner,
            sessions=self.sessions,
            allow_private_network=self.capabilities.allow_private_network,
        )
        self.checkpoints = CheckpointManager(
            path_policy=self.path_policy,
            checkpoint_root=state_dir.resolve(strict=True) / "execution-checkpoints",
        )
        self.recovered_checkpoints = self.checkpoints.recover_active()
        self.transactions = TransactionalExecutor(
            actions=self.actions,
            checkpoints=self.checkpoints,
        )
        self.max_cached_results = max_cached_results
        self.max_cached_result_bytes = max_cached_result_bytes
        self._results: OrderedDict[str, tuple[str, KernelResult]] = OrderedDict()
        self._batch_results: OrderedDict[
            str, tuple[str, KernelBatchResult]
        ] = OrderedDict()
        self._result_sizes: dict[str, int] = {}
        self._batch_result_sizes: dict[str, int] = {}
        self._result_bytes = 0
        self._batch_result_bytes = 0
        self._resource_locks: dict[str, asyncio.Lock] = {}
        self._resource_lock_users: dict[str, int] = {}
        self._guard = asyncio.Lock()

    def create_session(self, **kwargs: Any) -> ExecutionSession:
        return self.sessions.create(**kwargs)

    async def cancel_session(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown execution session: {session_id}")
        session.request_process_cancellation()
        terminated: list[int] = []
        errors: list[dict[str, Any]] = []
        pending_deadline = asyncio.get_running_loop().time() + 5.0
        while (
            session.process_start_pending()
            and asyncio.get_running_loop().time() < pending_deadline
        ):
            await asyncio.sleep(0.02)
        if session.process_start_pending():
            errors.append(
                {
                    "pid": None,
                    "error": "process start did not settle before cancellation deadline",
                }
            )
        for pid in session.running_pids():
            try:
                await asyncio.to_thread(
                    self.sessions.signal_owned_pid,
                    session_id,
                    pid,
                    signal.SIGTERM,
                    finalize=False,
                )
                deadline = asyncio.get_running_loop().time() + 2.0
                while (
                    self.sessions.owned_process_tree_alive(session_id, pid)
                    and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.05)
                if self.sessions.owned_process_tree_alive(session_id, pid):
                    await asyncio.to_thread(
                        self.sessions.signal_owned_pid,
                        session_id,
                        pid,
                        getattr(signal, "SIGKILL", signal.SIGTERM),
                        finalize=False,
                    )
                    await asyncio.sleep(0.05)
                if self.sessions.owned_process_tree_alive(session_id, pid):
                    raise RuntimeError("owned process did not terminate after escalation")
                with contextlib.suppress(KeyError, ValueError):
                    session.finish_process(pid, exit_code=None, terminated=True)
                terminated.append(pid)
            except (OSError, PermissionError, ProcessLookupError, RuntimeError) as exc:
                errors.append({"pid": pid, "error": f"{type(exc).__name__}: {exc}"})
        if not session.running_pids() and not session.process_start_pending():
            session.complete_process_cancellation()
        snapshot = session.snapshot()
        ok = (
            not errors
            and not snapshot["running_pids"]
            and not snapshot["process_start_pending"]
            and snapshot["status"] == SessionStatus.CANCELLED.value
        )
        return {
            "ok": ok,
            "summary": (
                f"Cancelled session {session_id}; terminated {len(terminated)} process(es)."
                if ok
                else f"Session {session_id} still owns running processes after cancellation."
            ),
            "terminated_pids": terminated,
            "errors": errors,
            "session": snapshot,
        }

    async def execute_payload(self, payload: str | bytes | dict[str, Any]) -> KernelResult:
        action = parse_action(payload)
        fingerprint = _action_fingerprint(action)
        return await self.execute(action, fingerprint=fingerprint)

    async def execute_transaction_payloads(
        self,
        payloads: tuple[str | bytes | dict[str, Any], ...],
        *,
        idempotency_key: str,
        session_id: str | None = None,
    ) -> KernelBatchResult:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", idempotency_key):
            raise ValueError("invalid transaction idempotency_key")
        if not payloads:
            raise ValueError("transaction payloads cannot be empty")
        actions = tuple(parse_action(payload) for payload in payloads)
        if len({action.action_id for action in actions}) != len(actions):
            raise ValueError("transaction action_id values must be unique")
        if any(classify_action(action) is not ActionClass.MUTATION for action in actions):
            raise ValueError("atomic batches accept reversible mutation actions only")
        fingerprint = _batch_fingerprint(actions)
        async with self._guard:
            cached = self._batch_results.get(idempotency_key)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise ValueError("idempotency_key was reused with a different transaction")
                self._batch_results.move_to_end(idempotency_key)
                return replace(cached[1], replayed=True)
        for action in actions:
            denied = self._authorize(action)
            if denied is not None:
                return KernelBatchResult(
                    ok=False,
                    idempotency_key=idempotency_key,
                    feedback=(
                        ActionFeedback(
                            ok=False,
                            action_id=action.action_id,
                            kind=type(action).__name__,
                            summary="Transaction denied by execution capability policy.",
                            error=denied,
                        ),
                    ),
                    transaction_status="denied",
                    checkpoint_id=None,
                )
        keys = tuple(
            sorted(
                {
                    f"batch:{idempotency_key}",
                    *(
                        key
                        for action in actions
                        for key in _resource_keys(action, self.path_policy.roots)
                    ),
                }
            )
        )
        locks = await self._locks(keys)
        acquired: list[asyncio.Lock] = []
        try:
            for lock in locks:
                await lock.acquire()
                acquired.append(lock)
            async with self._guard:
                cached = self._batch_results.get(idempotency_key)
                if cached is not None:
                    if cached[0] != fingerprint:
                        raise ValueError(
                            "idempotency_key was reused with a different transaction"
                        )
                    return replace(cached[1], replayed=True)
            session = self._prepare_batch_session(session_id)
            try:
                transaction = await self.transactions.execute(actions)
            except asyncio.CancelledError:
                self._record_batch_exception(session, cancelled=True, error=None)
                raise
            except BaseException as exc:
                self._record_batch_exception(session, cancelled=False, error=exc)
                raise
            result = KernelBatchResult(
                ok=transaction.ok,
                idempotency_key=idempotency_key,
                feedback=transaction.actions,
                transaction_status=transaction.status.value,
                checkpoint_id=transaction.checkpoint_id,
            )
            self._record_batch_session(session, result)
            await _await_authoritative(
                self._remember_batch(idempotency_key, fingerprint, result)
            )
            return result
        finally:
            for lock in reversed(acquired):
                lock.release()
            await self._retire_locks(keys)

    def _prepare_batch_session(self, session_id: str | None) -> ExecutionSession | None:
        if session_id is None:
            return None
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown execution session: {session_id}")
        if session.status is SessionStatus.CREATED:
            session.transition(SessionStatus.RUNNING)
        elif session.status is not SessionStatus.RUNNING:
            raise ValueError(f"session is not runnable: {session.status}")
        return session

    async def _remember_batch(
        self,
        idempotency_key: str,
        fingerprint: str,
        result: KernelBatchResult,
    ) -> None:
        async with self._guard:
            existing = self._batch_results.get(idempotency_key)
            if existing is not None and existing[0] != fingerprint:
                raise ValueError("idempotency_key was reused with a different transaction")
            previous_size = self._batch_result_sizes.get(idempotency_key, 0)
            size = _result_size(result)
            self._batch_result_bytes += size - previous_size
            self._batch_result_sizes[idempotency_key] = size
            self._batch_results[idempotency_key] = (fingerprint, result)
            self._batch_results.move_to_end(idempotency_key)
            while (
                len(self._batch_results) > self.max_cached_results
                or self._batch_result_bytes > self.max_cached_result_bytes
            ):
                removed_key, _removed = self._batch_results.popitem(last=False)
                self._batch_result_bytes -= self._batch_result_sizes.pop(removed_key)

    def _prepare_single_session(self, action: AtomicAction) -> ExecutionSession | None:
        if not isinstance(action, ProcessAction) or action.session_id is None:
            return None
        session = self.sessions.get(action.session_id)
        if session is None:
            raise KeyError(f"unknown execution session: {action.session_id}")
        if session.status is SessionStatus.CREATED:
            session.transition(SessionStatus.RUNNING)
        elif session.status is not SessionStatus.RUNNING:
            raise ValueError(f"session is not runnable: {session.status}")
        session.reserve_process_start(action.action_id)
        return session

    @staticmethod
    def _record_single_session(
        session: ExecutionSession | None,
        feedback: ActionFeedback,
    ) -> None:
        if session is None:
            return
        session.add_step(
            action=feedback.kind,
            status=StepStatus.SUCCEEDED if feedback.ok else StepStatus.FAILED,
            summary=feedback.summary,
            facts={"action_id": feedback.action_id, "error": feedback.error},
        )
        if (
            session.status is SessionStatus.RUNNING
            and not session.running_pids()
            and not session.process_start_pending()
        ):
            session.transition(
                SessionStatus.FAILED
                if session.has_failed_steps()
                else SessionStatus.SUCCEEDED
            )

    @staticmethod
    def _record_single_cancellation(
        session: ExecutionSession | None,
        action: AtomicAction,
    ) -> None:
        if session is None:
            return
        session.release_process_start(action.action_id)
        session.add_step(
            action=type(action).__name__,
            status=StepStatus.CANCELLED,
            summary="Process execution task was cancelled.",
            facts={"action_id": action.action_id},
        )
        if (
            session.status is SessionStatus.RUNNING
            and not session.running_pids()
            and not session.process_start_pending()
        ):
            session.transition(SessionStatus.CANCELLED)

    @staticmethod
    def _record_batch_session(
        session: ExecutionSession | None,
        result: KernelBatchResult,
    ) -> None:
        if session is None:
            return
        for feedback in result.feedback:
            session.add_step(
                action=feedback.kind,
                status=StepStatus.SUCCEEDED if feedback.ok else StepStatus.FAILED,
                summary=feedback.summary,
                facts={"action_id": feedback.action_id, "error": feedback.error},
            )
        if result.ok:
            session.transition(SessionStatus.SUCCEEDED)
        else:
            session.transition(SessionStatus.ROLLING_BACK)
            session.transition(SessionStatus.FAILED)

    @staticmethod
    def _record_batch_exception(
        session: ExecutionSession | None,
        *,
        cancelled: bool,
        error: BaseException | None,
    ) -> None:
        if session is None:
            return
        session.add_step(
            action="execution.transaction",
            status=StepStatus.CANCELLED if cancelled else StepStatus.FAILED,
            summary=(
                "Execution transaction was cancelled and rolled back."
                if cancelled
                else "Execution transaction failed before a committed outcome."
            ),
            facts={
                "error": f"{type(error).__name__}: {error}" if error is not None else None
            },
        )
        if session.status is SessionStatus.RUNNING:
            if cancelled:
                session.transition(SessionStatus.ROLLING_BACK)
                session.transition(SessionStatus.CANCELLED)
            else:
                session.transition(SessionStatus.FAILED)

    async def execute(
        self,
        action: AtomicAction,
        *,
        fingerprint: str | None = None,
    ) -> KernelResult:
        fingerprint = fingerprint or _action_fingerprint(action)
        cached = await self._cached(action.action_id, fingerprint)
        if cached is not None:
            return replace(cached, replayed=True)
        authorization_error = self._authorize(action)
        action_class = classify_action(action)
        if authorization_error is not None:
            result = KernelResult(
                ok=False,
                action_class=action_class,
                feedback=ActionFeedback(
                    ok=False,
                    action_id=action.action_id,
                    kind=type(action).__name__,
                    summary="Action denied by execution capability policy.",
                    error=authorization_error,
                ),
                transactional=False,
            )
            await _await_authoritative(
                self._remember(action.action_id, fingerprint, result)
            )
            return result
        keys = _resource_keys(action, self.path_policy.roots)
        locks = await self._locks(keys)
        acquired: list[asyncio.Lock] = []
        try:
            for lock in locks:
                await lock.acquire()
                acquired.append(lock)
            second = await self._cached(action.action_id, fingerprint)
            if second is not None:
                return replace(second, replayed=True)
            if action_class is ActionClass.MUTATION:
                transaction = await self.transactions.execute((action,))
                feedback = transaction.actions[-1]
                result = KernelResult(
                    ok=transaction.ok,
                    action_class=action_class,
                    feedback=feedback,
                    transactional=True,
                    transaction_status=transaction.status.value,
                    checkpoint_id=transaction.checkpoint_id,
                )
            else:
                session = self._prepare_single_session(action)
                try:
                    feedback = await self.actions.execute(action)
                except asyncio.CancelledError:
                    self._record_single_cancellation(session, action)
                    raise
                result = KernelResult(
                    ok=feedback.ok,
                    action_class=action_class,
                    feedback=feedback,
                    transactional=False,
                )
                self._record_single_session(session, feedback)
            await _await_authoritative(
                self._remember(action.action_id, fingerprint, result)
            )
            return result
        finally:
            for lock in reversed(acquired):
                lock.release()
            await self._retire_locks(keys)

    async def _cached(self, action_id: str, fingerprint: str) -> KernelResult | None:
        async with self._guard:
            cached = self._results.get(action_id)
            if cached is None:
                return None
            previous_fingerprint, result = cached
            if previous_fingerprint != fingerprint:
                raise ValueError(f"action_id {action_id!r} was reused with a different payload")
            self._results.move_to_end(action_id)
            return result

    async def _remember(self, action_id: str, fingerprint: str, result: KernelResult) -> None:
        async with self._guard:
            existing = self._results.get(action_id)
            if existing is not None and existing[0] != fingerprint:
                raise ValueError(f"action_id {action_id!r} was reused with a different payload")
            previous_size = self._result_sizes.get(action_id, 0)
            size = _result_size(result)
            self._result_bytes += size - previous_size
            self._result_sizes[action_id] = size
            self._results[action_id] = (fingerprint, result)
            self._results.move_to_end(action_id)
            while (
                len(self._results) > self.max_cached_results
                or self._result_bytes > self.max_cached_result_bytes
            ):
                removed_key, _removed = self._results.popitem(last=False)
                self._result_bytes -= self._result_sizes.pop(removed_key)

    async def _locks(self, keys: tuple[str, ...]) -> tuple[asyncio.Lock, ...]:
        async with self._guard:
            locks = tuple(self._resource_locks.setdefault(key, asyncio.Lock()) for key in keys)
            for key in keys:
                self._resource_lock_users[key] = self._resource_lock_users.get(key, 0) + 1
            return locks

    async def _retire_locks(self, keys: tuple[str, ...]) -> None:
        async with self._guard:
            for key in keys:
                users = self._resource_lock_users.get(key, 0) - 1
                if users <= 0:
                    self._resource_lock_users.pop(key, None)
                    self._resource_locks.pop(key, None)
                else:
                    self._resource_lock_users[key] = users

    def _authorize(self, action: AtomicAction) -> str | None:
        if isinstance(action, ProcessAction):
            if not self.capabilities.executable_rules:
                return "process execution is disabled because no executable rules are configured"
            if action.request.max_output_bytes > 8 * 1024 * 1024:
                return "kernel process output capture is limited to 8 MiB per stream"
            if (
                action.request.inherit_environment
                and not self.capabilities.allow_inherited_process_environment
            ):
                return "inherited process environment is not authorized"
        if isinstance(action, ResolveHostAction | TcpProbeAction):
            normalized = action.host.rstrip(".").casefold()
            allowed_hosts = {
                host.rstrip(".").casefold() for host in self.capabilities.network_hosts
            }
            if normalized not in allowed_hosts:
                return f"network host is not allowlisted: {action.host}"
            if not self.capabilities.allow_private_network:
                try:
                    address = ipaddress.ip_address(normalized)
                except ValueError:
                    address = None
                if address is not None and not address.is_global:
                    return "private, loopback and metadata-range network targets require capability"
        if isinstance(action, RegistryGetAction) and not _registry_allowed(
            action, self.capabilities.registry_read_prefixes
        ):
            return "registry read target is outside configured prefixes"
        if isinstance(
            action, RegistrySetAction | RegistryDeleteValueAction
        ) and not _registry_allowed(action, self.capabilities.registry_write_prefixes):
            return "registry write target is outside configured prefixes"
        return None


def _registry_allowed(action: Any, prefixes: tuple[tuple[str, str], ...]) -> bool:
    hive = action.hive.value.casefold()
    key = action.key.strip("\\").casefold()
    return any(
        hive == allowed_hive.casefold()
        and (
            key == prefix.strip("\\").casefold()
            or key.startswith(prefix.strip("\\").casefold() + "\\")
        )
        for allowed_hive, prefix in prefixes
    )


def _resource_keys(
    action: AtomicAction,
    allowed_roots: tuple[Path, ...] = (),
) -> tuple[str, ...]:
    keys = {f"id:{action.action_id}"}
    paths: list[Path] = []
    if isinstance(
        action,
        StatPathAction
        | ListDirectoryAction
        | ReadFileAction
        | CreateDirectoryAction
        | WriteFileAction
        | DeleteFileAction,
    ):
        paths.append(action.path)
    elif isinstance(action, CopyFileAction | MoveFileAction):
        paths.extend((action.source, action.destination))
    elif isinstance(action, ProcessAction):
        if action.request.cwd is not None:
            paths.append(action.request.cwd)
        paths.extend(action.request.observe_paths)
    if paths:
        for path in paths:
            resolved = path.resolve(strict=False)
            matching_roots = tuple(
                root
                for root in allowed_roots
                if resolved == root or resolved.is_relative_to(root)
            )
            matching_root = max(matching_roots, key=lambda item: len(item.parts), default=None)
            current = resolved
            keys.add(f"fs:{current}")
            if matching_root is not None:
                while current != matching_root:
                    current = current.parent
                    if current != matching_root:
                        keys.add(f"fs:{current}")
        return tuple(sorted(keys))
    if isinstance(action, RegistryGetAction | RegistrySetAction | RegistryDeleteValueAction):
        normalized_key = action.key.strip("\\")
        keys.add(f"registry:{action.hive.value}:{normalized_key}:{action.name}".casefold())
        keys.add(f"registry-key:{action.hive.value}:{normalized_key}".casefold())
        return tuple(sorted(keys))
    if isinstance(action, ResolveHostAction | TcpProbeAction):
        keys.add(f"network:{action.host.casefold()}:{action.port}")
        return tuple(sorted(keys))
    if isinstance(action, TerminateOwnedProcessAction):
        keys.add(f"process:{action.session_id}:{action.pid}")
    return tuple(sorted(keys))


def _action_fingerprint(action: AtomicAction) -> str:
    import hashlib

    canonical = json.dumps(
        _canonical_value(action),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8", errors="strict")).hexdigest()


def _batch_fingerprint(actions: tuple[AtomicAction, ...]) -> str:
    import hashlib

    canonical = "\x1e".join(_action_fingerprint(action) for action in actions)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _result_size(result: KernelResult | KernelBatchResult) -> int:
    return len(repr(result).encode("utf-8", errors="replace"))


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        import hashlib

        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Path):
        return {"path": str(value)}
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list | tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_canonical_value(item) for item in value), key=repr)
    raise TypeError(f"unsupported canonical action value: {type(value).__name__}")


async def _await_authoritative(awaitable: Any) -> Any:
    """Finish a tiny outcome-recording barrier and make its result authoritative."""
    task = asyncio.create_task(awaitable)
    cancellation_requests = 0
    try:
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancellation_requests += 1
    finally:
        current = asyncio.current_task()
        if current is not None:
            for _ in range(cancellation_requests):
                current.uncancel()
