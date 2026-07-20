from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from .execution_actions import (
    AtomicAction,
    AtomicActionExecutor,
    CopyFileAction,
    CreateDirectoryAction,
    DeleteFileAction,
    MoveFileAction,
    PathPolicy,
    RegistryDeleteValueAction,
    RegistryHive,
    RegistrySetAction,
    WriteFileAction,
)
from .execution_filesystem import (
    BoundPath,
    metadata_identity,
    pinned_directory,
    verified_binary_handle,
)
from .execution_models import ActionFeedback
from .state_verification import StateVerifier, VerificationExpectation

MutationVerifier = Callable[[tuple[ActionFeedback, ...]], Awaitable[bool]]
CommitRecordFactory = Callable[[str, tuple[ActionFeedback, ...]], Mapping[str, Any]]
CommitBarrier = Callable[[Mapping[str, Any]], None]
_AuthoritativeResultT = TypeVar("_AuthoritativeResultT")


class CheckpointStatus(StrEnum):
    ACTIVE = "active"
    ROLLING_BACK = "rolling_back"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


@dataclass(frozen=True, slots=True)
class PathCheckpoint:
    target: Path
    existed: bool
    kind: str | None
    backup: Path | None
    mode: int | None


@dataclass(frozen=True, slots=True)
class RegistryCheckpoint:
    hive: Any
    key: str
    name: str
    key_existed: bool
    existed: bool
    value: Any = None
    value_kind: int | None = None


@dataclass(slots=True)
class EnvironmentCheckpoint:
    checkpoint_id: str
    directory: Path
    paths: tuple[PathCheckpoint, ...]
    registry: tuple[RegistryCheckpoint, ...]
    status: CheckpointStatus = CheckpointStatus.ACTIVE
    rollback_errors: list[str] = field(default_factory=list)
    commit_record: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TransactionResult:
    ok: bool
    checkpoint_id: str
    status: CheckpointStatus
    actions: tuple[ActionFeedback, ...]
    failed_action_id: str | None = None
    rollback_errors: tuple[str, ...] = ()


class CheckpointManager:
    def __init__(
        self,
        *,
        path_policy: PathPolicy,
        checkpoint_root: Path,
        max_entries: int = 20_000,
        max_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        if not 1 <= max_entries <= 1_000_000:
            raise ValueError("max_entries must be between 1 and 1000000")
        if not 1 <= max_bytes <= 100 * 1024 * 1024 * 1024:
            raise ValueError("max_bytes is outside the supported range")
        self.path_policy = path_policy
        root = checkpoint_root
        if not root.is_absolute():
            raise ValueError("checkpoint_root must be absolute")
        if root.is_symlink():
            raise ValueError("checkpoint_root cannot be a symlink")
        self.checkpoint_root = root.resolve(strict=False)
        self.checkpoint_root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.checkpoint_root.chmod(0o700)
        self.max_entries = max_entries
        self.max_bytes = max_bytes

    def create(
        self,
        paths: tuple[Path, ...],
        registry_actions: tuple[RegistrySetAction | RegistryDeleteValueAction, ...] = (),
    ) -> EnvironmentCheckpoint:
        if paths and self.path_policy.active_mutation_guard is None:
            with self.path_policy.mutation_scope(paths):
                return self.create(paths, registry_actions)
        checkpoint_id = f"checkpoint_{uuid.uuid4().hex}"
        directory = self.checkpoint_root / checkpoint_id
        directory.mkdir(mode=0o700)
        path_checkpoints: list[PathCheckpoint] = []
        try:
            normalized = self._normalize_paths(paths)
            if any(
                target == self.checkpoint_root
                or target.is_relative_to(self.checkpoint_root)
                or self.checkpoint_root.is_relative_to(target)
                for target in normalized
            ):
                raise ValueError("transaction targets overlap the checkpoint store")
            entries, total_bytes = _measure_bound_paths(
                normalized,
                self.path_policy,
                max_entries=self.max_entries,
                max_bytes=self.max_bytes,
            )
            reserve = max(16 * 1024 * 1024, total_bytes // 10)
            if shutil.disk_usage(self.checkpoint_root).free < total_bytes + reserve:
                raise OSError("insufficient free space for checkpoint and rollback reserve")
            for index, target in enumerate(normalized):
                path_checkpoints.append(
                    _backup_path(target, directory / str(index), self.path_policy)
                )
            registry = tuple(_backup_registry(action) for action in registry_actions)
            checkpoint = EnvironmentCheckpoint(
                checkpoint_id=checkpoint_id,
                directory=directory,
                paths=tuple(path_checkpoints),
                registry=registry,
            )
            _fsync_tree(directory)
            _write_manifest(checkpoint)
            return checkpoint
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def commit(
        self,
        checkpoint: EnvironmentCheckpoint,
        commit_record: Mapping[str, Any] | None = None,
        commit_barrier: CommitBarrier | None = None,
    ) -> None:
        _require_active(checkpoint)
        if (
            commit_record is not None
            and commit_record.get("checkpoint_id") != checkpoint.checkpoint_id
        ):
            raise ValueError("commit record does not match its checkpoint")
        previous_record = checkpoint.commit_record
        checkpoint.commit_record = dict(commit_record) if commit_record is not None else None
        checkpoint.status = CheckpointStatus.COMMITTED
        try:
            _write_manifest(checkpoint)
        except BaseException:
            checkpoint.status = CheckpointStatus.ACTIVE
            checkpoint.commit_record = previous_record
            raise
        if commit_barrier is not None and checkpoint.commit_record is not None:
            commit_barrier(checkpoint.commit_record)
        with contextlib.suppress(OSError):
            shutil.rmtree(checkpoint.directory)

    def committed_records(self) -> tuple[dict[str, Any], ...]:
        """Read committed WAL records before checkpoint recovery removes them."""

        records: list[dict[str, Any]] = []
        for directory in sorted(self.checkpoint_root.glob("checkpoint_*")):
            if not directory.is_dir() or directory.is_symlink():
                continue
            manifest = directory / "manifest.json"
            if not manifest.is_file() or manifest.is_symlink():
                continue
            checkpoint = _read_manifest(manifest)
            if (
                checkpoint.status is CheckpointStatus.COMMITTED
                and checkpoint.commit_record is not None
            ):
                record = dict(checkpoint.commit_record)
                if record.get("checkpoint_id") != checkpoint.checkpoint_id:
                    raise ValueError(
                        "checkpoint commit record does not match its checkpoint"
                    )
                records.append(record)
        return tuple(records)

    def retire_committed(self, checkpoint_id: str) -> None:
        """Remove one committed WAL directory after its replay ledger is durable."""

        if re.fullmatch(r"checkpoint_[0-9a-f]{32}", checkpoint_id) is None:
            raise ValueError("invalid committed checkpoint id")
        directory = self.checkpoint_root / checkpoint_id
        if not directory.exists():
            return
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("committed checkpoint directory is not trusted")
        manifest = directory / "manifest.json"
        checkpoint = _read_manifest(manifest)
        if (
            checkpoint.checkpoint_id != checkpoint_id
            or checkpoint.status is not CheckpointStatus.COMMITTED
        ):
            raise RuntimeError("only an exact committed checkpoint can be retired")
        shutil.rmtree(directory)
        _fsync_directory(self.checkpoint_root)

    def rollback(self, checkpoint: EnvironmentCheckpoint) -> None:
        checkpoint_targets = tuple(item.target for item in checkpoint.paths)
        if checkpoint_targets and self.path_policy.active_mutation_guard is None:
            with self.path_policy.mutation_scope(checkpoint_targets):
                self.rollback(checkpoint)
                return
        if checkpoint.status not in {
            CheckpointStatus.ACTIVE,
            CheckpointStatus.ROLLING_BACK,
            CheckpointStatus.ROLLBACK_FAILED,
        }:
            raise RuntimeError(f"checkpoint is already {checkpoint.status}")
        checkpoint.status = CheckpointStatus.ROLLING_BACK
        _write_manifest(checkpoint)
        errors: list[str] = []
        for item in reversed(checkpoint.registry):
            try:
                _restore_registry(item)
                _verify_restored_registry(item)
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"registry {item.key}\\{item.name}: {type(exc).__name__}: {exc}")
        try:
            _cleanup_created_registry_keys(checkpoint.registry)
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"registry key cleanup: {type(exc).__name__}: {exc}")
        for item in reversed(checkpoint.paths):
            try:
                _restore_path(item, self.path_policy)
                _verify_restored_path(item, self.path_policy)
            except (OSError, TypeError, ValueError, RuntimeError) as exc:
                errors.append(f"path {item.target}: {type(exc).__name__}: {exc}")
        checkpoint.rollback_errors.extend(errors)
        checkpoint.status = (
            CheckpointStatus.ROLLBACK_FAILED if errors else CheckpointStatus.ROLLED_BACK
        )
        _write_manifest(checkpoint)
        if not errors:
            with contextlib.suppress(OSError):
                shutil.rmtree(checkpoint.directory)

    def recover_active(self) -> tuple[EnvironmentCheckpoint, ...]:
        recovered: list[EnvironmentCheckpoint] = []
        for directory in sorted(self.checkpoint_root.glob("checkpoint_*")):
            if not directory.is_dir() or directory.is_symlink():
                continue
            manifest = directory / "manifest.json"
            if not manifest.is_file():
                shutil.rmtree(directory)
                continue
            checkpoint = _read_manifest(manifest)
            backups = tuple(item.backup for item in checkpoint.paths if item.backup is not None)
            _measure_paths(
                backups,
                max_entries=self.max_entries,
                max_bytes=self.max_bytes,
            )
            if checkpoint.directory != directory.resolve(strict=True):
                raise RuntimeError(f"checkpoint manifest directory mismatch: {directory}")
            if checkpoint.status in {CheckpointStatus.COMMITTED, CheckpointStatus.ROLLED_BACK}:
                with contextlib.suppress(OSError):
                    shutil.rmtree(directory)
                continue
            self.rollback(checkpoint)
            recovered.append(checkpoint)
        return tuple(recovered)

    def _normalize_paths(self, paths: tuple[Path, ...]) -> tuple[Path, ...]:
        resolved = sorted(
            {self.path_policy.resolve(path) for path in paths},
            key=lambda path: (len(path.parts), str(path)),
        )
        result: list[Path] = []
        for candidate in resolved:
            if any(candidate.is_relative_to(parent) for parent in result):
                continue
            result.append(candidate)
        return tuple(result)


class TransactionalExecutor:
    def __init__(self, *, actions: AtomicActionExecutor, checkpoints: CheckpointManager):
        self.actions = actions
        self.checkpoints = checkpoints
        if actions.path_policy.roots != checkpoints.path_policy.roots:
            raise ValueError("action executor and checkpoint manager must use the same path policy")
        self.state_verifier = StateVerifier(
            path_policy=actions.path_policy,
            sessions=getattr(actions, "sessions", None),
            allow_private_network=bool(getattr(actions, "allow_private_network", False)),
        )

    async def execute(
        self,
        actions: tuple[AtomicAction, ...],
        *,
        checkpoint_paths: tuple[Path, ...] = (),
        verifier: MutationVerifier | None = None,
        commit_record_factory: CommitRecordFactory | None = None,
        commit_barrier: CommitBarrier | None = None,
    ) -> TransactionResult:
        if not actions:
            raise ValueError("a transaction must contain at least one action")
        if any(not _is_reversible_mutation(action) for action in actions):
            raise ValueError("transactions accept reversible filesystem/registry mutations only")
        filesystem_paths = tuple(
            dict.fromkeys((*checkpoint_paths, *_filesystem_action_paths(actions)))
        )
        if filesystem_paths:
            with self.actions.path_policy.mutation_scope(filesystem_paths):
                return await self._execute_guarded(
                    actions,
                    checkpoint_paths=checkpoint_paths,
                    verifier=verifier,
                    commit_record_factory=commit_record_factory,
                    commit_barrier=commit_barrier,
                )
        return await self._execute_guarded(
            actions,
            checkpoint_paths=checkpoint_paths,
            verifier=verifier,
            commit_record_factory=commit_record_factory,
            commit_barrier=commit_barrier,
        )

    async def _execute_guarded(
        self,
        actions: tuple[AtomicAction, ...],
        *,
        checkpoint_paths: tuple[Path, ...],
        verifier: MutationVerifier | None,
        commit_record_factory: CommitRecordFactory | None,
        commit_barrier: CommitBarrier | None,
    ) -> TransactionResult:
        if verifier is None:

            async def verify_by_readback(results: tuple[ActionFeedback, ...]) -> bool:
                if len(results) != len(actions):
                    return False
                for action, result in zip(actions, results, strict=True):
                    verification = await self.state_verifier.verify(
                        action,
                        feedback=result,
                        expectation=VerificationExpectation(),
                    )
                    if not verification.ok:
                        return False
                return True

            verifier = verify_by_readback
        affected_paths = _affected_paths(actions)
        registry_actions = tuple(
            action
            for action in actions
            if isinstance(action, RegistrySetAction | RegistryDeleteValueAction)
        )
        create_task = asyncio.create_task(
            asyncio.to_thread(
                self.checkpoints.create,
                tuple(dict.fromkeys((*checkpoint_paths, *affected_paths))),
                registry_actions,
            )
        )
        checkpoint, create_cancelled = await _await_authoritative_task(create_task)
        if create_cancelled:
            await _finish_thread_call_before_cancellation(self.checkpoints.rollback, checkpoint)
            raise asyncio.CancelledError
        feedback: list[ActionFeedback] = []
        failed_action_id: str | None = None
        try:
            for action in actions:
                result = await _finish_action_before_cancellation(self.actions, action)
                feedback.append(result)
                if not result.ok:
                    failed_action_id = result.action_id
                    await _finish_thread_call_before_cancellation(
                        self.checkpoints.rollback, checkpoint
                    )
                    break
            else:
                verified = await verifier(tuple(feedback))
                if verified:
                    commit_record = (
                        commit_record_factory(checkpoint.checkpoint_id, tuple(feedback))
                        if commit_record_factory is not None
                        else None
                    )
                    await _finish_commit_before_cancellation(
                        self.checkpoints,
                        checkpoint,
                        commit_record,
                        commit_barrier,
                    )
                else:
                    failed_action_id = "state_verification"
                    await _finish_thread_call_before_cancellation(
                        self.checkpoints.rollback, checkpoint
                    )
        except asyncio.CancelledError:
            await _rollback_if_needed(self.checkpoints, checkpoint)
            raise
        except BaseException:
            await _rollback_if_needed(self.checkpoints, checkpoint)
            raise
        return TransactionResult(
            ok=checkpoint.status is CheckpointStatus.COMMITTED,
            checkpoint_id=checkpoint.checkpoint_id,
            status=checkpoint.status,
            actions=tuple(feedback),
            failed_action_id=failed_action_id,
            rollback_errors=tuple(checkpoint.rollback_errors),
        )


def _affected_paths(actions: tuple[AtomicAction, ...]) -> tuple[Path, ...]:
    result: list[Path] = []
    for action in actions:
        if isinstance(action, CreateDirectoryAction | WriteFileAction | DeleteFileAction):
            result.append(_rollback_anchor(action.path))
        elif isinstance(action, CopyFileAction):
            result.append(_rollback_anchor(action.destination))
        elif isinstance(action, MoveFileAction):
            result.extend((action.source, _rollback_anchor(action.destination)))
    return tuple(result)


def _filesystem_action_paths(actions: tuple[AtomicAction, ...]) -> tuple[Path, ...]:
    result: list[Path] = []
    for action in actions:
        if isinstance(action, CreateDirectoryAction | WriteFileAction | DeleteFileAction):
            result.append(action.path)
        elif isinstance(action, CopyFileAction | MoveFileAction):
            result.extend((action.source, action.destination))
    return tuple(result)


def _is_reversible_mutation(action: AtomicAction) -> bool:
    return isinstance(
        action,
        CreateDirectoryAction
        | WriteFileAction
        | CopyFileAction
        | MoveFileAction
        | DeleteFileAction
        | RegistrySetAction
        | RegistryDeleteValueAction,
    )


async def _finish_action_before_cancellation(
    executor: AtomicActionExecutor,
    action: AtomicAction,
) -> ActionFeedback:
    task = asyncio.create_task(executor.execute(action))
    return await _finish_task_before_cancellation(task)


async def _finish_thread_call_before_cancellation(function, *args: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    return await _finish_task_before_cancellation(task)


async def _finish_task_before_cancellation(
    task: asyncio.Task[_AuthoritativeResultT],
) -> _AuthoritativeResultT:
    """Wait for an authoritative side effect before re-delivering cancellation.

    ``Task.cancel()`` may be called repeatedly while a mutation or rollback is
    running.  Each request must be consumed from the current task so a later
    await cannot propagate cancellation into the shielded operation.  Once the
    operation has a known outcome, its exception remains authoritative; a
    successful outcome re-delivers one cancellation to the transaction layer.
    """
    result, cancellation_requested = await _await_authoritative_task(task)
    if cancellation_requested:
        raise asyncio.CancelledError
    return result


async def _await_authoritative_task(
    task: asyncio.Task[_AuthoritativeResultT],
) -> tuple[_AuthoritativeResultT, bool]:
    """Return the task result plus whether its caller requested cancellation."""

    current = asyncio.current_task()
    baseline_cancellations = current.cancelling() if current is not None else 0
    cancellation_requested = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            pending_cancellations = (
                max(0, current.cancelling() - baseline_cancellations) if current is not None else 0
            )
            if pending_cancellations == 0:
                # The authoritative operation cancelled itself rather than the
                # transaction being cancelled by its caller.
                return task.result()
            cancellation_requested = True
            for _ in range(pending_cancellations):
                current.uncancel()
            if task.done():
                break

    result = task.result()
    return result, cancellation_requested


async def _finish_commit_before_cancellation(
    manager: CheckpointManager,
    checkpoint: EnvironmentCheckpoint,
    commit_record: Mapping[str, Any] | None,
    commit_barrier: CommitBarrier | None,
) -> None:
    """Make the durable commit outcome authoritative over concurrent cancellation."""
    task = asyncio.create_task(
        asyncio.to_thread(manager.commit, checkpoint, commit_record, commit_barrier)
    )
    cancellation_requests = 0
    try:
        while True:
            try:
                await asyncio.shield(task)
                return task.result()
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancellation_requests += 1
    finally:
        current = asyncio.current_task()
        if current is not None:
            for _ in range(cancellation_requests):
                current.uncancel()


async def _rollback_if_needed(
    manager: CheckpointManager,
    checkpoint: EnvironmentCheckpoint,
) -> None:
    if checkpoint.status in {
        CheckpointStatus.ACTIVE,
        CheckpointStatus.ROLLING_BACK,
        CheckpointStatus.ROLLBACK_FAILED,
    }:
        await _finish_thread_call_before_cancellation(manager.rollback, checkpoint)


def _rollback_anchor(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and not candidate.parent.exists():
        if candidate.parent == candidate:
            return candidate
        candidate = candidate.parent
    return candidate


def _measure_paths(
    paths: tuple[Path, ...],
    *,
    max_entries: int,
    max_bytes: int,
) -> tuple[int, int]:
    entries = 0
    total_bytes = 0
    pending = list(paths)
    while pending:
        path = pending.pop()
        if not path.exists():
            continue
        if path.is_symlink():
            raise ValueError(f"checkpoint target contains a symlink: {path}")
        metadata = path.stat()
        entries += 1
        if entries > max_entries:
            raise ValueError("checkpoint exceeds max_entries")
        if path.is_file():
            total_bytes += metadata.st_size
            if total_bytes > max_bytes:
                raise ValueError("checkpoint exceeds max_bytes")
        elif path.is_dir():
            pending.extend(path.iterdir())
        else:
            raise ValueError(f"unsupported checkpoint target: {path}")
    return entries, total_bytes


def _measure_bound_paths(
    paths: tuple[Path, ...],
    path_policy: PathPolicy,
    *,
    max_entries: int,
    max_bytes: int,
) -> tuple[int, int]:
    entries = 0
    total_bytes = 0

    def visit(current: BoundPath) -> None:
        nonlocal entries, total_bytes
        if not current.exists():
            return
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"checkpoint target contains a symlink: {current.path}")
        entries += 1
        if entries > max_entries:
            raise ValueError("checkpoint exceeds max_entries")
        if stat.S_ISREG(metadata.st_mode):
            total_bytes += metadata.st_size
            if total_bytes > max_bytes:
                raise ValueError("checkpoint exceeds max_bytes")
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"unsupported checkpoint target: {current.path}")
        with pinned_directory(current) as descriptor:
            scan_target: int | Path = current.path if descriptor is None else descriptor
            with os.scandir(scan_target) as children:
                names = tuple(entry.name for entry in children)
            for name in names:
                visit(BoundPath(current.path / name, descriptor, name))

    for target in paths:
        visit(path_policy.bind_mutation_path(target))
    return entries, total_bytes


def _backup_path(target: Path, backup: Path, path_policy: PathPolicy) -> PathCheckpoint:
    bound = path_policy.bind_mutation_path(target)
    if not bound.exists():
        return PathCheckpoint(target, False, None, None, None)
    metadata = bound.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"checkpoint target contains a symlink: {target}")
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISREG(metadata.st_mode):
        _durable_copy_bound_file(bound, backup)
        return PathCheckpoint(target, True, "file", backup, mode)
    if stat.S_ISDIR(metadata.st_mode):
        _copy_bound_tree(bound, backup)
        return PathCheckpoint(target, True, "directory", backup, mode)
    raise ValueError(f"unsupported checkpoint target: {target}")


def _restore_path(checkpoint: PathCheckpoint, path_policy: PathPolicy) -> None:
    path_policy.release_mutation_descendants(checkpoint.target)
    target = path_policy.bind_mutation_path(checkpoint.target)
    if target.exists() and stat.S_ISLNK(target.lstat().st_mode):
        raise RuntimeError("refusing to replace a symlink created after checkpoint")
    if target.exists():
        metadata = target.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            _remove_bound_tree(target)
        elif stat.S_ISREG(metadata.st_mode):
            _remove_bound_file(target)
        else:
            raise RuntimeError("refusing to remove unsupported filesystem object")
    if not checkpoint.existed:
        _fsync_bound_parent(target)
        return
    if checkpoint.backup is None or checkpoint.kind is None:
        raise RuntimeError("checkpoint backup is incomplete")
    temporary_name = f".{target.name}.{uuid.uuid4().hex}.rollback"
    temporary = target.sibling(temporary_name)
    if checkpoint.kind == "file":
        try:
            _durable_copy_file_to_bound(checkpoint.backup, temporary)
            target.replace_from(temporary)
        finally:
            _unlink_bound_if_exists(temporary)
    elif checkpoint.kind == "directory":
        try:
            _restore_tree_to_bound(checkpoint.backup, temporary)
            target.replace_from(temporary)
        finally:
            if temporary.exists():
                _remove_bound_tree(temporary)
    else:
        raise RuntimeError(f"unsupported checkpoint kind: {checkpoint.kind}")
    if checkpoint.mode is not None:
        target.chmod(checkpoint.mode)
    _fsync_bound_parent(target)


def _verify_restored_path(checkpoint: PathCheckpoint, path_policy: PathPolicy) -> None:
    target = path_policy.bind_mutation_path(checkpoint.target)
    if not checkpoint.existed:
        if target.exists():
            raise RuntimeError("rollback target should be absent")
        return
    if checkpoint.backup is None or checkpoint.kind is None:
        raise RuntimeError("checkpoint backup is incomplete")
    if _bound_content_manifest(target) != _path_content_manifest(checkpoint.backup):
        raise RuntimeError("rollback readback does not match the durable checkpoint")
    if checkpoint.mode is not None and stat.S_IMODE(target.lstat().st_mode) != checkpoint.mode:
        raise RuntimeError("rollback target mode does not match the checkpoint")


def _durable_copy_bound_file(source: BoundPath, destination: Path) -> str:
    with verified_binary_handle(source) as source_handle, destination.open("xb") as target_handle:
        shutil.copyfileobj(source_handle, target_handle, length=128 * 1024)
        target_handle.flush()
        os.fsync(target_handle.fileno())
        mode = stat.S_IMODE(os.fstat(source_handle.fileno()).st_mode)
    destination.chmod(mode)
    return str(destination)


def _copy_bound_tree(source: BoundPath, destination: Path) -> None:
    metadata = source.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(source.path)
    destination.mkdir(mode=stat.S_IMODE(metadata.st_mode))
    with pinned_directory(source) as descriptor:
        scan_target: int | Path = source.path if descriptor is None else descriptor
        with os.scandir(scan_target) as entries:
            for entry in entries:
                child = BoundPath(
                    source.path / entry.name,
                    descriptor,
                    entry.name,
                )
                child_metadata = child.lstat()
                if stat.S_ISLNK(child_metadata.st_mode):
                    raise ValueError(f"checkpoint target contains a symlink: {child.path}")
                backup_child = destination / entry.name
                if stat.S_ISREG(child_metadata.st_mode):
                    _durable_copy_bound_file(child, backup_child)
                elif stat.S_ISDIR(child_metadata.st_mode):
                    _copy_bound_tree(child, backup_child)
                else:
                    raise ValueError(f"unsupported checkpoint target object: {child.path}")
    destination.chmod(stat.S_IMODE(metadata.st_mode))


def _durable_copy_file_to_bound(source: Path, destination: BoundPath) -> None:
    descriptor = destination.open(os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
            descriptor = -1
            source_metadata = os.fstat(source_handle.fileno())
            path_metadata = source.lstat()
            if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(source_metadata.st_mode):
                raise RuntimeError("checkpoint backup file is unsafe")
            if metadata_identity(source_metadata) != metadata_identity(path_metadata):
                raise RuntimeError("checkpoint backup identity changed while opening")
            shutil.copyfileobj(source_handle, target_handle, length=128 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        destination.chmod(stat.S_IMODE(source_metadata.st_mode))
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _restore_tree_to_bound(source: Path, destination: BoundPath) -> None:
    source_metadata = source.lstat()
    if stat.S_ISLNK(source_metadata.st_mode) or not stat.S_ISDIR(source_metadata.st_mode):
        raise RuntimeError("checkpoint directory backup is unsafe")
    destination.mkdir(mode=stat.S_IMODE(source_metadata.st_mode))
    with pinned_directory(destination) as destination_fd, os.scandir(source) as entries:
        for entry in entries:
            source_child = source / entry.name
            child_metadata = source_child.lstat()
            if stat.S_ISLNK(child_metadata.st_mode):
                raise RuntimeError("checkpoint directory backup contains a symlink")
            destination_child = BoundPath(
                destination.path / entry.name,
                destination_fd,
                entry.name,
            )
            if stat.S_ISREG(child_metadata.st_mode):
                _durable_copy_file_to_bound(source_child, destination_child)
            elif stat.S_ISDIR(child_metadata.st_mode):
                _restore_tree_to_bound(source_child, destination_child)
            else:
                raise RuntimeError("checkpoint directory backup contains an unsafe object")
    destination.chmod(stat.S_IMODE(source_metadata.st_mode))


def _remove_bound_file(target: BoundPath) -> None:
    with verified_binary_handle(target) as handle:
        identity = metadata_identity(os.fstat(handle.fileno()))
    staged_name = f".{target.name}.{uuid.uuid4().hex}.rollback-remove"
    staged = target.sibling(staged_name)
    staged.replace_from(target)
    try:
        if metadata_identity(staged.lstat()) != identity:
            raise RuntimeError("rollback file identity changed before removal")
    except BaseException:
        if not target.exists() and staged.exists():
            target.replace_from(staged)
        raise
    staged.unlink()


def _remove_bound_tree(target: BoundPath) -> None:
    metadata = target.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeError("refusing to recursively remove an unsafe object")
    with pinned_directory(target) as descriptor:
        scan_target: int | Path = target.path if descriptor is None else descriptor
        with os.scandir(scan_target) as entries:
            children = tuple(entry.name for entry in entries)
        for name in children:
            child = BoundPath(target.path / name, descriptor, name)
            child_metadata = child.lstat()
            if stat.S_ISLNK(child_metadata.st_mode):
                child.unlink()
            elif stat.S_ISDIR(child_metadata.st_mode):
                _remove_bound_tree(child)
            elif stat.S_ISREG(child_metadata.st_mode):
                _remove_bound_file(child)
            else:
                raise RuntimeError("refusing to remove unsupported filesystem object")
    target.rmdir()


def _bound_content_manifest(
    root: BoundPath,
) -> tuple[tuple[str, str, int, str | None], ...]:
    entries: list[tuple[str, str, int, str | None]] = []

    def visit(current: BoundPath, relative: str) -> None:
        metadata = current.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("rollback readback contains a symlink")
        if stat.S_ISREG(metadata.st_mode):
            digest = hashlib.sha256()
            with verified_binary_handle(current) as handle:
                while chunk := handle.read(128 * 1024):
                    digest.update(chunk)
            entries.append((relative, "file", mode, digest.hexdigest()))
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("rollback readback contains an unsupported object")
        entries.append((relative, "directory", mode, None))
        with pinned_directory(current) as descriptor:
            scan_target: int | Path = current.path if descriptor is None else descriptor
            with os.scandir(scan_target) as children:
                names = sorted(entry.name for entry in children)
            for name in names:
                child_relative = name if relative == "." else f"{relative}/{name}"
                visit(BoundPath(current.path / name, descriptor, name), child_relative)

    visit(root, ".")
    return tuple(sorted(entries))


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


def _path_content_manifest(path: Path) -> tuple[tuple[str, str, int, str | None], ...]:
    if path.is_symlink() or not path.exists():
        raise RuntimeError("rollback readback target is missing or a symlink")
    root = path
    pending = [path]
    entries: list[tuple[str, str, int, str | None]] = []
    while pending:
        current = pending.pop()
        if current.is_symlink():
            raise RuntimeError("rollback readback contains a symlink")
        relative = "." if current == root else current.relative_to(root).as_posix()
        mode = stat.S_IMODE(current.stat().st_mode)
        if current.is_file():
            entries.append((relative, "file", mode, _file_digest(current)))
        elif current.is_dir():
            entries.append((relative, "directory", mode, None))
            pending.extend(sorted(current.iterdir(), reverse=True))
        else:
            raise RuntimeError("rollback readback contains an unsupported object")
    return tuple(sorted(entries))


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_registry(
    action: RegistrySetAction | RegistryDeleteValueAction,
) -> RegistryCheckpoint:
    if os.name != "nt":
        return RegistryCheckpoint(action.hive, action.key, action.name, False, False)
    import winreg

    hive = {
        "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
        "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
    }[action.hive.value]
    try:
        handle = winreg.OpenKey(hive, action.key, 0, winreg.KEY_QUERY_VALUE)
    except FileNotFoundError:
        return RegistryCheckpoint(action.hive, action.key, action.name, False, False)
    try:
        with handle:
            value, value_kind = winreg.QueryValueEx(handle, action.name)
    except FileNotFoundError:
        return RegistryCheckpoint(action.hive, action.key, action.name, True, False)
    return RegistryCheckpoint(action.hive, action.key, action.name, True, True, value, value_kind)


def _restore_registry(checkpoint: RegistryCheckpoint) -> None:
    if os.name != "nt":
        return
    import winreg

    hive = {
        "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
        "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
    }[checkpoint.hive.value]
    if checkpoint.existed:
        with winreg.CreateKeyEx(hive, checkpoint.key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.SetValueEx(
                handle,
                checkpoint.name,
                0,
                checkpoint.value_kind,
                checkpoint.value,
            )
        return
    with (
        contextlib.suppress(FileNotFoundError),
        winreg.OpenKey(hive, checkpoint.key, 0, winreg.KEY_SET_VALUE) as handle,
    ):
        winreg.DeleteValue(handle, checkpoint.name)


def _verify_restored_registry(checkpoint: RegistryCheckpoint) -> None:
    if os.name != "nt":
        return
    import winreg

    hive = {
        "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
        "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
    }[checkpoint.hive.value]
    try:
        with winreg.OpenKey(hive, checkpoint.key, 0, winreg.KEY_QUERY_VALUE) as handle:
            value, value_kind = winreg.QueryValueEx(handle, checkpoint.name)
    except FileNotFoundError:
        if checkpoint.existed:
            raise RuntimeError("rollback registry value is missing") from None
        return
    if not checkpoint.existed:
        raise RuntimeError("rollback registry value should be absent")
    if value != checkpoint.value or value_kind != checkpoint.value_kind:
        raise RuntimeError("rollback registry readback does not match the checkpoint")


def _cleanup_created_registry_keys(checkpoints: tuple[RegistryCheckpoint, ...]) -> None:
    if os.name != "nt":
        return
    import winreg

    created = {
        (checkpoint.hive, checkpoint.key)
        for checkpoint in checkpoints
        if not checkpoint.key_existed
    }
    for registry_hive, key in sorted(
        created,
        key=lambda item: (item[1].count("\\"), item[1].casefold()),
        reverse=True,
    ):
        hive = {
            "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
            "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
        }[registry_hive.value]
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ) as handle:
                subkeys, values, _modified = winreg.QueryInfoKey(handle)
        except FileNotFoundError:
            continue
        if subkeys or values:
            continue
        winreg.DeleteKey(hive, key)


def _require_active(checkpoint: EnvironmentCheckpoint) -> None:
    if checkpoint.status is not CheckpointStatus.ACTIVE:
        raise RuntimeError(f"checkpoint is already {checkpoint.status}")


def _write_manifest(checkpoint: EnvironmentCheckpoint) -> None:
    payload = {
        "version": 1,
        "checkpoint_id": checkpoint.checkpoint_id,
        "directory": str(checkpoint.directory.resolve(strict=True)),
        "status": checkpoint.status.value,
        "rollback_errors": checkpoint.rollback_errors,
        "commit_record": checkpoint.commit_record,
        "paths": [
            {
                "target": str(item.target),
                "existed": item.existed,
                "kind": item.kind,
                "backup": item.backup.name if item.backup is not None else None,
                "mode": item.mode,
            }
            for item in checkpoint.paths
        ],
        "registry": [
            {
                "hive": item.hive.value,
                "key": item.key,
                "name": item.name,
                "key_existed": item.key_existed,
                "existed": item.existed,
                "value": _json_registry_value(item.value),
                "value_kind": item.value_kind,
            }
            for item in checkpoint.registry
        ],
    }
    temporary = checkpoint.directory / ".manifest.json.tmp"
    manifest = checkpoint.directory / "manifest.json"
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, manifest)
    _fsync_directory(checkpoint.directory)


def _read_manifest(path: Path) -> EnvironmentCheckpoint:
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("checkpoint manifest is too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise ValueError("unsupported checkpoint manifest version")
        directory = path.parent.resolve(strict=True)
        if Path(payload["directory"]).resolve(strict=True) != directory:
            raise ValueError("checkpoint manifest escaped its directory")
        if payload["checkpoint_id"] != directory.name:
            raise ValueError("checkpoint manifest id does not match its directory")
        paths_list: list[PathCheckpoint] = []
        for item in payload["paths"]:
            target = Path(item["target"])
            existed = bool(item["existed"])
            kind = item["kind"]
            mode = item["mode"]
            if not target.is_absolute():
                raise ValueError("checkpoint target is not absolute")
            if kind not in {None, "file", "directory"}:
                raise ValueError("checkpoint path kind is invalid")
            if mode is not None and (
                isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777
            ):
                raise ValueError("checkpoint path mode is invalid")
            backup_name = item["backup"]
            backup = None
            if backup_name is not None:
                if (
                    not isinstance(backup_name, str)
                    or Path(backup_name).name != backup_name
                    or not backup_name.isdigit()
                ):
                    raise ValueError("checkpoint backup name is invalid")
                backup = directory / backup_name
                if backup.is_symlink() or not backup.exists():
                    raise ValueError("checkpoint backup is missing or unsafe")
                if kind == "file" and not backup.is_file():
                    raise ValueError("checkpoint file backup has the wrong type")
                if kind == "directory" and not backup.is_dir():
                    raise ValueError("checkpoint directory backup has the wrong type")
            if existed != (backup is not None) or existed != (kind is not None):
                raise ValueError("checkpoint path existence metadata is inconsistent")
            paths_list.append(
                PathCheckpoint(
                    target=target,
                    existed=existed,
                    kind=kind,
                    backup=backup,
                    mode=mode,
                )
            )
        paths = tuple(paths_list)
        registry = tuple(
            RegistryCheckpoint(
                hive=RegistryHive(item["hive"]),
                key=item["key"],
                name=item["name"],
                key_existed=bool(item["key_existed"]),
                existed=bool(item["existed"]),
                value=_from_json_registry_value(item["value"]),
                value_kind=item["value_kind"],
            )
            for item in payload["registry"]
        )
        commit_record = payload.get("commit_record")
        if commit_record is not None and not isinstance(commit_record, dict):
            raise ValueError("checkpoint commit record is invalid")
        return EnvironmentCheckpoint(
            checkpoint_id=payload["checkpoint_id"],
            directory=directory,
            paths=paths,
            registry=registry,
            status=CheckpointStatus(payload["status"]),
            rollback_errors=list(payload.get("rollback_errors") or []),
            commit_record=dict(commit_record) if commit_record is not None else None,
        )
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid checkpoint manifest: {path}") from exc


def _json_registry_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        return {"kind": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, str | int):
        return {"kind": "scalar", "value": value}
    raise TypeError(f"unsupported registry checkpoint value: {type(value).__name__}")


def _from_json_registry_value(payload: dict[str, Any]) -> Any:
    if payload.get("kind") == "bytes":
        return base64.b64decode(payload["value"], validate=True)
    if payload.get("kind") == "scalar":
        value = payload.get("value")
        if value is None or isinstance(value, str | int):
            return value
    raise ValueError("invalid registry checkpoint value")


def _fsync_tree(root: Path) -> None:
    if os.name == "nt":
        return
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    _fsync_directory(root)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_copy_file(source: str | Path, destination: str | Path) -> str:
    source_path = Path(source)
    destination_path = Path(destination)
    with source_path.open("rb") as source_handle:
        descriptor_metadata = os.fstat(source_handle.fileno())
        path_metadata = source_path.lstat()
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(descriptor_metadata.st_mode):
            raise ValueError("checkpoint source became a symlink or non-regular file")
        if (descriptor_metadata.st_dev, descriptor_metadata.st_ino) != (
            path_metadata.st_dev,
            path_metadata.st_ino,
        ):
            raise RuntimeError("checkpoint source identity changed while opening")
        with destination_path.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=128 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
    shutil.copystat(source_path, destination_path, follow_symlinks=False)
    return str(destination_path)
