from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import re
import signal
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime
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
from .execution_replay import DurableReplayJournal
from .execution_session import (
    ExecutionSession,
    SessionRegistry,
    SessionStatus,
    StepStatus,
)
from .execution_transaction import (
    CheckpointManager,
    CheckpointStatus,
    MutationVerifier,
    TransactionalExecutor,
)
from .state_verification import StateVerifier, VerificationExpectation

ActionVerifier = Callable[[ActionFeedback], Awaitable[bool]]

_BATCH_COMMIT_PROTOCOL = "jarvis.execution-batch-commit.v1"


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
    failed_action_id: str | None = None
    rollback_errors: tuple[str, ...] = ()
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
    failed_action_id: str | None = None
    rollback_errors: tuple[str, ...] = ()
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
        max_replay_results: int = 10_000,
        max_replay_result_bytes: int = 256 * 1024 * 1024,
        recover_checkpoints: bool = True,
    ) -> None:
        if not state_dir.is_absolute():
            raise ValueError("state_dir must be absolute")
        if not 1 <= max_cached_results <= 100_000:
            raise ValueError("max_cached_results must be between 1 and 100000")
        if not 1024 * 1024 <= max_cached_result_bytes <= 1024 * 1024 * 1024:
            raise ValueError("max_cached_result_bytes must be between 1 MiB and 1 GiB")
        state_dir.mkdir(parents=True, exist_ok=True)
        state_root = state_dir.resolve(strict=True)
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
            checkpoint_root=state_root / "execution-checkpoints",
        )
        self.replay_journal = DurableReplayJournal(
            state_root / "execution-replay-journal.json",
            max_entries=max_replay_results,
            max_bytes=max_replay_result_bytes,
        )
        if recover_checkpoints:
            # A committed checkpoint manifest is a write-ahead record for the
            # narrow crash window between mutation commit and journal replace.
            # Import it before checkpoint recovery is allowed to remove it.
            for record in self.checkpoints.committed_records():
                self._persist_batch_commit_record(record)
            self.recovered_checkpoints = self.checkpoints.recover_active()
        else:
            self.recovered_checkpoints = ()
        self.transactions = TransactionalExecutor(
            actions=self.actions,
            checkpoints=self.checkpoints,
        )
        self.state_verifier = StateVerifier(
            path_policy=self.path_policy,
            sessions=self.sessions,
            allow_private_network=self.capabilities.allow_private_network,
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

    @property
    def rollback_degraded(self) -> bool:
        """Whether startup recovery left an unresolved partial rollback."""

        return any(
            checkpoint.status
            in {
                CheckpointStatus.ACTIVE,
                CheckpointStatus.ROLLING_BACK,
                CheckpointStatus.ROLLBACK_FAILED,
            }
            for checkpoint in self.recovered_checkpoints
        )

    @property
    def rollback_degraded_checkpoint_ids(self) -> tuple[str, ...]:
        """Stable identifiers for checkpoints that keep mutation fail-closed."""

        return tuple(
            checkpoint.checkpoint_id
            for checkpoint in self.recovered_checkpoints
            if checkpoint.status
            in {
                CheckpointStatus.ACTIVE,
                CheckpointStatus.ROLLING_BACK,
                CheckpointStatus.ROLLBACK_FAILED,
            }
        )

    def _rollback_degraded_reason(self) -> str | None:
        checkpoint_ids = self.rollback_degraded_checkpoint_ids
        if not checkpoint_ids:
            return None
        return (
            "mutation execution is blocked because startup recovery left "
            "unresolved rollback checkpoint(s): "
            + ", ".join(checkpoint_ids)
        )

    def verification_denial(
        self,
        action: AtomicAction,
        expectation: VerificationExpectation,
    ) -> str | None:
        """Apply capability policy to a safe postcondition inspection.

        The rollback-degraded latch blocks mutations, not inspection, but every
        registry/network target and supplemental expectation remains constrained
        by the same operator-owned capability policy as execution.
        """

        denied = self._authorize(action, verification_only=True)
        if denied is not None:
            return denied
        for path in expectation.paths:
            try:
                self.path_policy.resolve(path.path, allow_root=True)
            except (OSError, PermissionError, ValueError) as exc:
                return f"verification path is outside policy: {exc}"
        for target in expectation.tcp:
            denied = self._authorize(
                TcpProbeAction(
                    host=target.host,
                    port=target.port,
                    timeout_seconds=target.timeout_seconds,
                    action_id="verification-capability-check",
                ),
                verification_only=True,
            )
            if denied is not None:
                return denied
        return None

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

    async def execute_payload(
        self,
        payload: str | bytes | dict[str, Any],
        *,
        mutation_verifier: MutationVerifier | None = None,
        action_verifier: ActionVerifier | None = None,
        replay_action_verifier: ActionVerifier | None = None,
        postcondition_fingerprint: str | None = None,
    ) -> KernelResult:
        action = parse_action(payload)
        fingerprint = _action_fingerprint(action)
        return await self.execute(
            action,
            fingerprint=fingerprint,
            mutation_verifier=mutation_verifier,
            action_verifier=action_verifier,
            replay_action_verifier=replay_action_verifier,
            postcondition_fingerprint=postcondition_fingerprint,
        )

    async def execute_transaction_payloads(
        self,
        payloads: tuple[str | bytes | dict[str, Any], ...],
        *,
        idempotency_key: str,
        session_id: str | None = None,
        mutation_verifier: MutationVerifier | None = None,
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
        recovery_error = self._rollback_degraded_reason()
        if recovery_error is not None:
            first = actions[0]
            return KernelBatchResult(
                ok=False,
                idempotency_key=idempotency_key,
                feedback=(
                    ActionFeedback(
                        ok=False,
                        action_id=first.action_id,
                        kind=type(first).__name__,
                        summary="Transaction denied while rollback recovery is degraded.",
                        error=recovery_error,
                    ),
                ),
                transaction_status="recovery_blocked",
                checkpoint_id=None,
            )
        if mutation_verifier is None:
            mutation_verifier = self._default_mutation_verifier(actions)
        fingerprint = _batch_fingerprint(actions)
        cached = await self._cached_batch(idempotency_key, fingerprint)
        cached_result = replace(cached, replayed=True) if cached is not None else None
        if cached_result is not None:
            return await _verified_batch_replay(cached_result, mutation_verifier)
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
            cached = await self._cached_batch(idempotency_key, fingerprint)
            cached_result = replace(cached, replayed=True) if cached is not None else None
            if cached_result is not None:
                return await _verified_batch_replay(cached_result, mutation_verifier)
            session = self._prepare_batch_session(session_id)
            try:
                transaction = await self.transactions.execute(
                    actions,
                    verifier=mutation_verifier,
                    commit_record_factory=lambda checkpoint_id, feedback: (
                        self._batch_commit_record(
                            idempotency_key=idempotency_key,
                            fingerprint=fingerprint,
                            checkpoint_id=checkpoint_id,
                            feedback=feedback,
                        )
                    ),
                    commit_barrier=self._persist_batch_commit_record,
                )
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
                failed_action_id=transaction.failed_action_id,
                rollback_errors=transaction.rollback_errors,
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

    async def _cached_batch(
        self,
        idempotency_key: str,
        fingerprint: str,
    ) -> KernelBatchResult | None:
        async with self._guard:
            cached = self._batch_results.get(idempotency_key)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise ValueError(
                        "idempotency_key was reused with a different transaction"
                    )
                self._batch_results.move_to_end(idempotency_key)
                return cached[1]
        durable = self.replay_journal.lookup(idempotency_key)
        if durable is None:
            for raw in self.checkpoints.committed_records():
                record = _parse_batch_commit_record(raw)
                if record["key"] != idempotency_key:
                    continue
                if record["fingerprint"] != fingerprint:
                    raise ValueError(
                        "idempotency_key was reused with a different transaction"
                    )
                # A previous live commit may have reached its durable WAL but
                # failed while replacing the replay ledger. Import that record
                # before any retry can reach action execution.
                self._persist_batch_commit_record(raw)
                result = _batch_result_from_payload(record["result"])
                await self._remember_batch(idempotency_key, fingerprint, result)
                if result.checkpoint_id is None:
                    raise RuntimeError("committed replay record has no checkpoint identity")
                self.checkpoints.retire_committed(result.checkpoint_id)
                return result
            return None
        if durable.fingerprint != fingerprint:
            raise ValueError("idempotency_key was reused with a different transaction")
        result = _batch_result_from_payload(durable.result)
        if result.idempotency_key != idempotency_key:
            raise RuntimeError("execution replay journal result key is inconsistent")
        await self._remember_batch(idempotency_key, fingerprint, result)
        return result

    @staticmethod
    def _batch_commit_record(
        *,
        idempotency_key: str,
        fingerprint: str,
        checkpoint_id: str,
        feedback: tuple[ActionFeedback, ...],
    ) -> dict[str, Any]:
        result = KernelBatchResult(
            ok=True,
            idempotency_key=idempotency_key,
            feedback=feedback,
            transaction_status=CheckpointStatus.COMMITTED.value,
            checkpoint_id=checkpoint_id,
        )
        return {
            "protocol": _BATCH_COMMIT_PROTOCOL,
            "checkpoint_id": checkpoint_id,
            "key": idempotency_key,
            "fingerprint": fingerprint,
            "recorded_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "result": _batch_result_payload(result),
        }

    def _persist_batch_commit_record(self, raw: Mapping[str, Any]) -> None:
        record = _parse_batch_commit_record(raw)
        self.replay_journal.remember(
            record["key"],
            record["fingerprint"],
            record["result"],
            recorded_at=record["recorded_at"],
        )

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
        mutation_verifier: MutationVerifier | None = None,
        action_verifier: ActionVerifier | None = None,
        replay_action_verifier: ActionVerifier | None = None,
        postcondition_fingerprint: str | None = None,
    ) -> KernelResult:
        fingerprint = fingerprint or _action_fingerprint(action)
        if postcondition_fingerprint is not None:
            fingerprint = _postcondition_bound_fingerprint(
                fingerprint, postcondition_fingerprint
            )
        action_class = classify_action(action)
        recovery_error = (
            self._rollback_degraded_reason()
            if action_class is ActionClass.MUTATION
            else None
        )
        if recovery_error is not None:
            return KernelResult(
                ok=False,
                action_class=action_class,
                feedback=ActionFeedback(
                    ok=False,
                    action_id=action.action_id,
                    kind=type(action).__name__,
                    summary="Mutation denied while rollback recovery is degraded.",
                    error=recovery_error,
                ),
                transactional=False,
                transaction_status="recovery_blocked",
                failed_action_id="startup_recovery",
            )
        if mutation_verifier is None and action_class is ActionClass.MUTATION:
            mutation_verifier = self._default_mutation_verifier((action,))
        if action_verifier is None and action_class is not ActionClass.MUTATION:
            action_verifier = self._default_action_verifier(action)
        cached = await self._cached(action.action_id, fingerprint)
        if cached is not None:
            return await _verified_single_replay(
                replace(cached, replayed=True),
                mutation_verifier,
                replay_action_verifier or action_verifier,
            )
        authorization_error = self._authorize(action)
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
                return await _verified_single_replay(
                    replace(second, replayed=True),
                    mutation_verifier,
                    replay_action_verifier or action_verifier,
                )
            if action_class is ActionClass.MUTATION:
                transaction = await self.transactions.execute(
                    (action,),
                    verifier=mutation_verifier,
                )
                feedback = transaction.actions[-1]
                result = KernelResult(
                    ok=transaction.ok,
                    action_class=action_class,
                    feedback=feedback,
                    transactional=True,
                    transaction_status=transaction.status.value,
                    checkpoint_id=transaction.checkpoint_id,
                    failed_action_id=transaction.failed_action_id,
                    rollback_errors=transaction.rollback_errors,
                )
            else:
                session = self._prepare_single_session(action)
                try:
                    feedback = await self.actions.execute(action)
                except asyncio.CancelledError:
                    self._record_single_cancellation(session, action)
                    raise
                verified = (
                    feedback.ok
                    and (
                        action_verifier is None
                        or await action_verifier(feedback)
                    )
                )
                if feedback.ok and not verified:
                    feedback = replace(
                        feedback,
                        ok=False,
                        summary="Independent state verification failed.",
                        error="postcondition verification failed",
                    )
                result = KernelResult(
                    ok=verified,
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

    def _default_mutation_verifier(
        self,
        actions: tuple[AtomicAction, ...],
    ) -> MutationVerifier:
        async def verify(feedback: tuple[ActionFeedback, ...]) -> bool:
            if len(feedback) != len(actions):
                return False
            for action, item in zip(actions, feedback, strict=True):
                result = await self.state_verifier.verify(
                    action,
                    feedback=item,
                    expectation=VerificationExpectation(),
                )
                if not result.ok:
                    return False
            return True

        return verify

    def _default_action_verifier(self, action: AtomicAction) -> ActionVerifier:
        async def verify(feedback: ActionFeedback) -> bool:
            result = await self.state_verifier.verify(
                action,
                feedback=feedback,
                expectation=VerificationExpectation(),
            )
            return result.ok

        return verify

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

    def _authorize(
        self,
        action: AtomicAction,
        *,
        verification_only: bool = False,
    ) -> str | None:
        if not verification_only and classify_action(action) is ActionClass.MUTATION:
            recovery_error = self._rollback_degraded_reason()
            if recovery_error is not None:
                return recovery_error
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


async def _verified_batch_replay(
    result: KernelBatchResult,
    verifier: MutationVerifier | None,
) -> KernelBatchResult:
    if not result.ok:
        return result
    if verifier is not None and not await verifier(result.feedback):
        return replace(result, ok=False, transaction_status="verification_failed")
    return result


async def _verified_single_replay(
    result: KernelResult,
    mutation_verifier: MutationVerifier | None,
    action_verifier: ActionVerifier | None,
) -> KernelResult:
    if not result.ok:
        return result
    if result.action_class is ActionClass.MUTATION:
        if mutation_verifier is None or not await mutation_verifier((result.feedback,)):
            return replace(
                result,
                ok=False,
                transaction_status="verification_failed",
                failed_action_id="state_verification",
            )
        return result
    if action_verifier is None or not await action_verifier(result.feedback):
        return replace(
            result,
            ok=False,
            feedback=replace(
                result.feedback,
                ok=False,
                summary="Independent state verification failed.",
                error="postcondition verification failed",
            ),
        )
    return result


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


def _postcondition_bound_fingerprint(
    action_fingerprint: str,
    postcondition_fingerprint: str,
) -> str:
    import hashlib

    if not re.fullmatch(r"[0-9a-f]{64}", postcondition_fingerprint):
        raise ValueError("postcondition_fingerprint must be a lowercase SHA-256 digest")
    canonical = json.dumps(
        {
            "action_sha256": action_fingerprint,
            "postcondition_sha256": postcondition_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _batch_fingerprint(actions: tuple[AtomicAction, ...]) -> str:
    import hashlib

    canonical = "\x1e".join(_action_fingerprint(action) for action in actions)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _batch_result_payload(result: KernelBatchResult) -> dict[str, Any]:
    payload = {
        "ok": result.ok,
        "idempotency_key": result.idempotency_key,
        "feedback": [item.to_dict() for item in result.feedback],
        "transaction_status": result.transaction_status,
        "checkpoint_id": result.checkpoint_id,
        "failed_action_id": result.failed_action_id,
        "rollback_errors": list(result.rollback_errors),
        "replayed": result.replayed,
    }
    # Normalize eagerly so a value that cannot be represented by the durable
    # protocol fails before the checkpoint is marked committed.
    return json.loads(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _batch_result_from_payload(raw: Any) -> KernelBatchResult:
    expected = {
        "ok",
        "idempotency_key",
        "feedback",
        "transaction_status",
        "checkpoint_id",
        "failed_action_id",
        "rollback_errors",
        "replayed",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise RuntimeError("execution replay result shape is invalid")
    key = raw["idempotency_key"]
    checkpoint_id = raw["checkpoint_id"]
    feedback_raw = raw["feedback"]
    if (
        raw["ok"] is not True
        or not isinstance(key, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", key) is None
        or raw["transaction_status"] != CheckpointStatus.COMMITTED.value
        or not isinstance(checkpoint_id, str)
        or re.fullmatch(r"checkpoint_[0-9a-f]{32}", checkpoint_id) is None
        or raw["failed_action_id"] is not None
        or raw["rollback_errors"] != []
        or raw["replayed"] is not False
        or not isinstance(feedback_raw, list)
        or not 1 <= len(feedback_raw) <= 10_000
    ):
        raise RuntimeError("execution replay result metadata is invalid")
    feedback: list[ActionFeedback] = []
    feedback_shape = {
        "ok",
        "action_id",
        "kind",
        "summary",
        "before",
        "after",
        "process",
        "error",
    }
    for item in feedback_raw:
        if not isinstance(item, dict) or set(item) != feedback_shape:
            raise RuntimeError("execution replay feedback shape is invalid")
        if (
            item["ok"] is not True
            or not isinstance(item["action_id"], str)
            or not 1 <= len(item["action_id"]) <= 128
            or not isinstance(item["kind"], str)
            or not 1 <= len(item["kind"]) <= 128
            or not isinstance(item["summary"], str)
            or not 1 <= len(item["summary"]) <= 4096
            or not isinstance(item["before"], dict)
            or not isinstance(item["after"], dict)
            or item["process"] is not None
            or item["error"] is not None
        ):
            raise RuntimeError("execution replay feedback metadata is invalid")
        feedback.append(
            ActionFeedback(
                ok=True,
                action_id=item["action_id"],
                kind=item["kind"],
                summary=item["summary"],
                before=item["before"],
                after=item["after"],
            )
        )
    return KernelBatchResult(
        ok=True,
        idempotency_key=key,
        feedback=tuple(feedback),
        transaction_status=CheckpointStatus.COMMITTED.value,
        checkpoint_id=checkpoint_id,
    )


def _parse_batch_commit_record(raw: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(raw)
    if set(record) != {
        "protocol",
        "checkpoint_id",
        "key",
        "fingerprint",
        "recorded_at",
        "result",
    }:
        raise RuntimeError("execution batch commit record shape is invalid")
    key = record["key"]
    checkpoint_id = record["checkpoint_id"]
    fingerprint = record["fingerprint"]
    recorded_at = record["recorded_at"]
    if (
        record["protocol"] != _BATCH_COMMIT_PROTOCOL
        or not isinstance(checkpoint_id, str)
        or re.fullmatch(r"checkpoint_[0-9a-f]{32}", checkpoint_id) is None
        or not isinstance(key, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", key) is None
        or not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or not isinstance(recorded_at, str)
        or not 1 <= len(recorded_at) <= 64
    ):
        raise RuntimeError("execution batch commit record metadata is invalid")
    try:
        timestamp = datetime.fromisoformat(recorded_at)
    except ValueError as exc:
        raise RuntimeError("execution batch commit timestamp is invalid") from exc
    if timestamp.tzinfo is None:
        raise RuntimeError("execution batch commit timestamp must include a timezone")
    result = _batch_result_from_payload(record["result"])
    if result.idempotency_key != key or result.checkpoint_id != checkpoint_id:
        raise RuntimeError("execution batch commit record identity is inconsistent")
    return {
        "key": key,
        "fingerprint": fingerprint,
        "recorded_at": recorded_at,
        "result": _batch_result_payload(result),
    }


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
