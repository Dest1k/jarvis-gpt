from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
from types import SimpleNamespace

import jarvis_gpt.execution_transaction as transaction_module
import pytest
from jarvis_gpt.execution_actions import (
    AtomicActionExecutor,
    PathPolicy,
    RegistryHive,
    WriteFileAction,
)
from jarvis_gpt.execution_models import ActionFeedback
from jarvis_gpt.execution_session import (
    ExecutionSession,
    SessionRegistry,
    SessionStatus,
    StepStatus,
)
from jarvis_gpt.execution_transaction import (
    CheckpointManager,
    CheckpointStatus,
    RegistryCheckpoint,
    TransactionalExecutor,
)


def test_transaction_rolls_back_all_files_after_failure(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "value.txt"
    target.write_text("original", encoding="utf-8")
    policy = PathPolicy((tmp_path,))
    executor = TransactionalExecutor(
        actions=AtomicActionExecutor(path_policy=policy),
        checkpoints=CheckpointManager(path_policy=policy, checkpoint_root=state),
    )
    actions = (
        WriteFileAction(path=target, content=b"changed"),
        WriteFileAction(
            path=target,
            content=b"never",
            expected_sha256=hashlib.sha256(b"wrong").hexdigest(),
        ),
    )

    result = asyncio.run(executor.execute(actions))

    assert result.ok is False
    assert result.status is CheckpointStatus.ROLLED_BACK
    assert result.failed_action_id == actions[1].action_id
    assert target.read_text(encoding="utf-8") == "original"
    assert list(state.iterdir()) == []


def test_transaction_removes_parent_directories_it_created(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "new" / "nested" / "value.txt"
    policy = PathPolicy((tmp_path,))
    executor = TransactionalExecutor(
        actions=AtomicActionExecutor(path_policy=policy),
        checkpoints=CheckpointManager(path_policy=policy, checkpoint_root=state),
    )
    actions = (
        WriteFileAction(path=target, content=b"created", create_parents=True),
        WriteFileAction(path=tmp_path / "missing-parent" / "fail.txt", content=b"fail"),
    )

    result = asyncio.run(executor.execute(actions))

    assert result.status is CheckpointStatus.ROLLED_BACK
    assert not (tmp_path / "new").exists()


def test_transaction_commits_and_removes_checkpoint(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "new.txt"
    policy = PathPolicy((tmp_path,))
    executor = TransactionalExecutor(
        actions=AtomicActionExecutor(path_policy=policy),
        checkpoints=CheckpointManager(path_policy=policy, checkpoint_root=state),
    )

    result = asyncio.run(executor.execute((WriteFileAction(path=target, content=b"ok"),)))

    assert result.ok is True
    assert result.status is CheckpointStatus.COMMITTED
    assert target.read_bytes() == b"ok"
    assert list(state.iterdir()) == []


def test_checkpoint_wal_recovers_interrupted_mutation(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "value.txt"
    target.write_text("before", encoding="utf-8")
    policy = PathPolicy((tmp_path,))
    manager = CheckpointManager(path_policy=policy, checkpoint_root=state)
    checkpoint = manager.create((target,))
    target.write_text("after-crash", encoding="utf-8")

    recovered = CheckpointManager(
        path_policy=policy, checkpoint_root=state
    ).recover_active()

    assert recovered[0].checkpoint_id == checkpoint.checkpoint_id
    assert recovered[0].status is CheckpointStatus.ROLLED_BACK
    assert target.read_text(encoding="utf-8") == "before"
    assert list(state.iterdir()) == []


def test_checkpoint_rejects_targets_overlapping_its_store(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    policy = PathPolicy((tmp_path,))
    manager = CheckpointManager(path_policy=policy, checkpoint_root=state)

    with pytest.raises(ValueError, match="overlap"):
        manager.create((state,))


def test_transaction_cancellation_waits_for_action_then_rolls_back(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "value.txt"
    target.write_text("before", encoding="utf-8")
    policy = PathPolicy((tmp_path,))
    started = asyncio.Event()

    class SlowActions:
        path_policy = policy

        async def execute(self, action):
            started.set()
            await asyncio.sleep(0.05)
            target.write_text("late mutation", encoding="utf-8")
            return ActionFeedback(
                ok=True,
                action_id=action.action_id,
                kind=type(action).__name__,
                summary="late",
            )

    executor = TransactionalExecutor(
        actions=SlowActions(),
        checkpoints=CheckpointManager(path_policy=policy, checkpoint_root=state),
    )

    async def scenario():
        task = asyncio.create_task(
            executor.execute((WriteFileAction(path=target, content=b"ignored"),))
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert target.read_text(encoding="utf-8") == "before"
    assert list(state.iterdir()) == []


def test_transaction_cancellation_during_commit_returns_committed_result(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "value.txt"
    policy = PathPolicy((tmp_path,))
    manager = CheckpointManager(path_policy=policy, checkpoint_root=state)
    executor = TransactionalExecutor(
        actions=AtomicActionExecutor(path_policy=policy),
        checkpoints=manager,
    )
    commit_started = threading.Event()
    release_commit = threading.Event()
    original_commit = manager.commit

    def delayed_commit(checkpoint):
        commit_started.set()
        if not release_commit.wait(timeout=5):
            raise TimeoutError("test did not release commit")
        original_commit(checkpoint)

    manager.commit = delayed_commit

    async def scenario():
        task = asyncio.create_task(
            executor.execute((WriteFileAction(path=target, content=b"committed"),))
        )
        await asyncio.wait_for(asyncio.to_thread(commit_started.wait), timeout=2)
        task.cancel()
        release_commit.set()
        result = await asyncio.wait_for(task, timeout=2)
        return task, result

    try:
        task, result = asyncio.run(scenario())
    finally:
        release_commit.set()

    assert result.ok is True
    assert result.status is CheckpointStatus.COMMITTED
    assert task.cancelled() is False
    assert task.cancelling() == 0
    assert target.read_bytes() == b"committed"
    assert list(state.iterdir()) == []


def test_commit_manifest_failure_resets_state_and_rolls_back(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "value.txt"
    target.write_text("before", encoding="utf-8")
    policy = PathPolicy((tmp_path,))
    manager = CheckpointManager(path_policy=policy, checkpoint_root=state)
    executor = TransactionalExecutor(
        actions=AtomicActionExecutor(path_policy=policy),
        checkpoints=manager,
    )
    original_write_manifest = transaction_module._write_manifest
    rollback_entry_statuses = []
    original_rollback = manager.rollback

    def fail_committed_manifest(checkpoint):
        if checkpoint.status is CheckpointStatus.COMMITTED:
            raise OSError("simulated commit manifest fsync failure")
        original_write_manifest(checkpoint)

    def record_rollback_entry(checkpoint):
        rollback_entry_statuses.append(checkpoint.status)
        original_rollback(checkpoint)

    monkeypatch.setattr(transaction_module, "_write_manifest", fail_committed_manifest)
    manager.rollback = record_rollback_entry

    with pytest.raises(OSError, match="simulated commit manifest fsync failure"):
        asyncio.run(executor.execute((WriteFileAction(path=target, content=b"changed"),)))

    assert rollback_entry_statuses == [CheckpointStatus.ACTIVE]
    assert target.read_text(encoding="utf-8") == "before"
    assert list(state.iterdir()) == []


def test_registry_rollback_deduplicates_new_key_cleanup(monkeypatch):
    deleted: list[tuple[object, str]] = []

    class Handle:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        HKEY_LOCAL_MACHINE=object(),
        KEY_READ=1,
        OpenKey=lambda *_args: Handle(),
        QueryInfoKey=lambda _handle: (0, 0, 0),
        DeleteKey=lambda hive, key: deleted.append((hive, key)),
    )
    monkeypatch.setattr(transaction_module.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    checkpoints = (
        RegistryCheckpoint(
            RegistryHive.CURRENT_USER,
            "Software\\Jarvis\\Created",
            "first",
            False,
            False,
        ),
        RegistryCheckpoint(
            RegistryHive.CURRENT_USER,
            "Software\\Jarvis\\Created",
            "second",
            False,
            False,
        ),
    )

    transaction_module._cleanup_created_registry_keys(checkpoints)

    assert deleted == [(fake_winreg.HKEY_CURRENT_USER, "Software\\Jarvis\\Created")]


def test_cancelled_commit_failure_clears_cancellation_and_rolls_back(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    target = tmp_path / "value.txt"
    target.write_text("before", encoding="utf-8")
    policy = PathPolicy((tmp_path,))
    manager = CheckpointManager(path_policy=policy, checkpoint_root=state)
    executor = TransactionalExecutor(
        actions=AtomicActionExecutor(path_policy=policy),
        checkpoints=manager,
    )
    commit_started = threading.Event()
    release_commit = threading.Event()
    original_commit = manager.commit
    original_write_manifest = transaction_module._write_manifest

    def fail_committed_manifest(checkpoint):
        if checkpoint.status is CheckpointStatus.COMMITTED:
            raise OSError("combined commit failure")
        original_write_manifest(checkpoint)

    def delayed_commit(checkpoint):
        commit_started.set()
        if not release_commit.wait(timeout=5):
            raise TimeoutError("test did not release commit")
        original_commit(checkpoint)

    monkeypatch.setattr(transaction_module, "_write_manifest", fail_committed_manifest)
    manager.commit = delayed_commit

    async def scenario():
        task = asyncio.create_task(
            executor.execute((WriteFileAction(path=target, content=b"changed"),))
        )
        await asyncio.wait_for(asyncio.to_thread(commit_started.wait), timeout=2)
        task.cancel()
        release_commit.set()
        with pytest.raises(OSError, match="combined commit failure"):
            await task
        return task

    try:
        task = asyncio.run(scenario())
    finally:
        release_commit.set()

    assert task.cancelled() is False
    assert task.cancelling() == 0
    assert target.read_text(encoding="utf-8") == "before"
    assert list(state.iterdir()) == []


def test_session_state_machine_and_bounded_dry_fact_compression():
    session = ExecutionSession(max_history_entries=8, max_history_bytes=4096)
    session.transition(SessionStatus.RUNNING)
    for index in range(40):
        session.add_step(
            action="fs.stat",
            status=StepStatus.SUCCEEDED,
            summary=f"inspected {index}",
            facts={"index": index},
        )

    snapshot = session.snapshot()

    assert len(snapshot["history"]) <= 8
    assert snapshot["history_digest"]["compressed_steps"] >= 32
    assert snapshot["history_digest"]["status_counts"]["succeeded"] >= 32
    session.transition(SessionStatus.SUCCEEDED)
    with pytest.raises(ValueError, match="invalid session transition"):
        session.transition(SessionStatus.RUNNING)


def test_session_registry_rejects_unowned_pids_and_lists_snapshots():
    registry = SessionRegistry()
    session = registry.create(session_id="session_test")

    with pytest.raises(PermissionError, match="not a live process owned"):
        registry.require_owned_pid(session.session_id, 999_999)

    assert registry.snapshot(session.session_id)["session_id"] == session.session_id
    assert registry.list()[0]["session_id"] == session.session_id
    with pytest.raises(ValueError, match="already exists"):
        registry.create(session_id=session.session_id)
