from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

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
from .execution_models import ActionFeedback


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
            entries, total_bytes = _measure_paths(
                normalized,
                max_entries=self.max_entries,
                max_bytes=self.max_bytes,
            )
            reserve = max(16 * 1024 * 1024, total_bytes // 10)
            if shutil.disk_usage(self.checkpoint_root).free < total_bytes + reserve:
                raise OSError("insufficient free space for checkpoint and rollback reserve")
            for index, target in enumerate(normalized):
                path_checkpoints.append(_backup_path(target, directory / str(index)))
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

    def commit(self, checkpoint: EnvironmentCheckpoint) -> None:
        _require_active(checkpoint)
        checkpoint.status = CheckpointStatus.COMMITTED
        try:
            _write_manifest(checkpoint)
        except BaseException:
            # A failed durability barrier is not a commit. Keep the in-memory
            # state rollback-eligible so the transactional executor can restore
            # the checkpoint before surfacing the error.
            checkpoint.status = CheckpointStatus.ACTIVE
            raise
        with contextlib.suppress(OSError):
            shutil.rmtree(checkpoint.directory)

    def rollback(self, checkpoint: EnvironmentCheckpoint) -> None:
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
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"registry {item.key}\\{item.name}: {type(exc).__name__}: {exc}")
        try:
            _cleanup_created_registry_keys(checkpoint.registry)
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"registry key cleanup: {type(exc).__name__}: {exc}")
        for item in reversed(checkpoint.paths):
            try:
                self.path_policy.resolve(item.target)
                _restore_path(item)
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

    async def execute(
        self,
        actions: tuple[AtomicAction, ...],
        *,
        checkpoint_paths: tuple[Path, ...] = (),
    ) -> TransactionResult:
        if not actions:
            raise ValueError("a transaction must contain at least one action")
        if any(not _is_reversible_mutation(action) for action in actions):
            raise ValueError("transactions accept reversible filesystem/registry mutations only")
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
        try:
            checkpoint = await asyncio.shield(create_task)
        except asyncio.CancelledError:
            checkpoint = await create_task
            await asyncio.shield(asyncio.to_thread(self.checkpoints.rollback, checkpoint))
            raise
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
                await _finish_commit_before_cancellation(self.checkpoints, checkpoint)
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
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _finish_thread_call_before_cancellation(function, *args: Any) -> Any:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def _finish_commit_before_cancellation(
    manager: CheckpointManager,
    checkpoint: EnvironmentCheckpoint,
) -> None:
    """Make the durable commit outcome authoritative over concurrent cancellation."""
    task = asyncio.create_task(asyncio.to_thread(manager.commit, checkpoint))
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


def _backup_path(target: Path, backup: Path) -> PathCheckpoint:
    if not target.exists():
        return PathCheckpoint(target, False, None, None, None)
    metadata = target.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    if target.is_file():
        _durable_copy_file(target, backup)
        return PathCheckpoint(target, True, "file", backup, mode)
    if target.is_dir():
        shutil.copytree(target, backup, symlinks=True, copy_function=_durable_copy_file)
        return PathCheckpoint(target, True, "directory", backup, mode)
    raise ValueError(f"unsupported checkpoint target: {target}")


def _restore_path(checkpoint: PathCheckpoint) -> None:
    target = checkpoint.target
    if target.is_symlink():
        raise RuntimeError("refusing to replace a symlink created after checkpoint")
    if target.exists():
        if target.is_dir():
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink()
        else:
            raise RuntimeError("refusing to remove unsupported filesystem object")
    if not checkpoint.existed:
        _fsync_directory(target.parent)
        return
    if checkpoint.backup is None or checkpoint.kind is None:
        raise RuntimeError("checkpoint backup is incomplete")
    target.parent.mkdir(parents=True, exist_ok=True)
    if checkpoint.kind == "file":
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.rollback"
        try:
            _durable_copy_file(checkpoint.backup, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    elif checkpoint.kind == "directory":
        temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.rollback"
        try:
            shutil.copytree(
                checkpoint.backup,
                temporary,
                symlinks=True,
                copy_function=_durable_copy_file,
            )
            _fsync_tree(temporary)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    else:
        raise RuntimeError(f"unsupported checkpoint kind: {checkpoint.kind}")
    if checkpoint.mode is not None:
        target.chmod(checkpoint.mode)
    _fsync_directory(target.parent)


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
    return RegistryCheckpoint(
        action.hive, action.key, action.name, True, True, value, value_kind
    )


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
    with contextlib.suppress(FileNotFoundError), winreg.OpenKey(
        hive, checkpoint.key, 0, winreg.KEY_SET_VALUE
    ) as handle:
        winreg.DeleteValue(handle, checkpoint.name)


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
        return EnvironmentCheckpoint(
            checkpoint_id=payload["checkpoint_id"],
            directory=directory,
            paths=paths,
            registry=registry,
            status=CheckpointStatus(payload["status"]),
            rollback_errors=list(payload.get("rollback_errors") or []),
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
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(
            descriptor_metadata.st_mode
        ):
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
