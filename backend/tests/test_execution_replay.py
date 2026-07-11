from __future__ import annotations

import asyncio
import base64
import json

import pytest
from jarvis_gpt.execution_kernel import ExecutionKernel


def _write_payload(path, *, action_id: str, content: bytes) -> dict:
    return {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "action_id": action_id,
            "path": str(path),
            "content_base64": base64.b64encode(content).decode("ascii"),
            "require_absent": True,
        },
    }


def test_transaction_replays_from_durable_journal_after_kernel_restart(tmp_path):
    root = tmp_path / "root"
    state = tmp_path / "state"
    root.mkdir()
    target = root / "durable.txt"
    payload = _write_payload(target, action_id="durable-write", content=b"committed")

    first_kernel = ExecutionKernel(allowed_roots=(root,), state_dir=state)
    first = asyncio.run(
        first_kernel.execute_transaction_payloads(
            (payload,), idempotency_key="durable.restart"
        )
    )
    second_kernel = ExecutionKernel(allowed_roots=(root,), state_dir=state)
    replay = asyncio.run(
        second_kernel.execute_transaction_payloads(
            (payload,), idempotency_key="durable.restart"
        )
    )

    assert first.ok is True and first.replayed is False
    assert replay.ok is True and replay.replayed is True
    assert replay.checkpoint_id == first.checkpoint_id
    assert target.read_bytes() == b"committed"


def test_durable_journal_rehydrates_result_after_memory_cache_eviction(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    kernel = ExecutionKernel(
        allowed_roots=(root,),
        state_dir=tmp_path / "state",
        max_cached_results=1,
        max_replay_results=4,
    )
    first_payload = _write_payload(
        root / "first.txt", action_id="evicted-first", content=b"first"
    )
    second_payload = _write_payload(
        root / "second.txt", action_id="evicted-second", content=b"second"
    )

    first = asyncio.run(
        kernel.execute_transaction_payloads(
            (first_payload,), idempotency_key="durable.eviction.first"
        )
    )
    asyncio.run(
        kernel.execute_transaction_payloads(
            (second_payload,), idempotency_key="durable.eviction.second"
        )
    )
    assert "durable.eviction.first" not in kernel._batch_results

    replay = asyncio.run(
        kernel.execute_transaction_payloads(
            (first_payload,), idempotency_key="durable.eviction.first"
        )
    )

    assert replay.ok is True and replay.replayed is True
    assert replay.checkpoint_id == first.checkpoint_id


def test_committed_checkpoint_wal_repairs_crash_before_journal_replace(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    state = tmp_path / "state"
    root.mkdir()
    target = root / "crash-boundary.txt"
    payload = _write_payload(target, action_id="crash-boundary", content=b"committed")
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=state)

    def fail_before_journal_replace(*_args, **_kwargs):
        raise OSError("simulated crash before replay journal replace")

    monkeypatch.setattr(kernel.replay_journal, "remember", fail_before_journal_replace)
    with pytest.raises(OSError, match="simulated crash"):
        asyncio.run(
            kernel.execute_transaction_payloads(
                (payload,), idempotency_key="durable.crash.boundary"
            )
        )

    assert target.read_bytes() == b"committed"
    assert any(kernel.checkpoints.checkpoint_root.iterdir())

    recovered = ExecutionKernel(allowed_roots=(root,), state_dir=state)
    replay = asyncio.run(
        recovered.execute_transaction_payloads(
            (payload,), idempotency_key="durable.crash.boundary"
        )
    )

    assert replay.ok is True and replay.replayed is True
    assert not any(recovered.checkpoints.checkpoint_root.iterdir())


def test_live_retry_imports_committed_wal_before_any_reexecution(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "live-boundary.txt"
    payload = _write_payload(target, action_id="live-boundary", content=b"committed")
    payload["action"]["require_absent"] = False
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=tmp_path / "state")
    original_remember = kernel.replay_journal.remember
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated one-shot replay ledger failure")
        return original_remember(*args, **kwargs)

    monkeypatch.setattr(kernel.replay_journal, "remember", fail_once)
    with pytest.raises(OSError, match="one-shot"):
        asyncio.run(
            kernel.execute_transaction_payloads(
                (payload,), idempotency_key="durable.live.boundary"
            )
        )
    assert target.read_bytes() == b"committed"

    target.write_bytes(b"changed-after-commit")
    replay = asyncio.run(
        kernel.execute_transaction_payloads(
            (payload,), idempotency_key="durable.live.boundary"
        )
    )

    assert replay.replayed is True
    assert replay.ok is False
    assert replay.transaction_status == "verification_failed"
    assert target.read_bytes() == b"changed-after-commit"
    assert not any(kernel.checkpoints.checkpoint_root.iterdir())


def test_durable_replay_collision_and_corruption_fail_closed(tmp_path):
    root = tmp_path / "root"
    state = tmp_path / "state"
    root.mkdir()
    target = root / "collision.txt"
    original = _write_payload(target, action_id="collision-action", content=b"original")
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=state)
    asyncio.run(
        kernel.execute_transaction_payloads(
            (original,), idempotency_key="durable.collision"
        )
    )

    changed = _write_payload(target, action_id="collision-action", content=b"changed")
    restarted = ExecutionKernel(allowed_roots=(root,), state_dir=state)
    with pytest.raises(ValueError, match="reused with a different transaction"):
        asyncio.run(
            restarted.execute_transaction_payloads(
                (changed,), idempotency_key="durable.collision"
            )
        )
    assert target.read_bytes() == b"original"

    journal = state / "execution-replay-journal.json"
    payload = json.loads(journal.read_text(encoding="utf-8"))
    payload["entries"][0]["result"]["checkpoint_id"] = "checkpoint_" + "0" * 32
    journal.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="checksum is invalid"):
        ExecutionKernel(allowed_roots=(root,), state_dir=state)


def test_replay_snapshot_rejects_valid_entry_removal_and_reordering(tmp_path):
    root = tmp_path / "root"
    state = tmp_path / "state"
    root.mkdir()
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=state)
    for index in range(2):
        payload = _write_payload(
            root / f"snapshot-{index}.txt",
            action_id=f"snapshot-{index}",
            content=str(index).encode("ascii"),
        )
        result = asyncio.run(
            kernel.execute_transaction_payloads(
                (payload,), idempotency_key=f"durable.snapshot.{index}"
            )
        )
        assert result.ok is True

    journal = state / "execution-replay-journal.json"
    original = json.loads(journal.read_text(encoding="utf-8"))
    mutations = (
        [],
        list(reversed(original["entries"])),
        original["entries"][1:],
    )
    for entries in mutations:
        changed = json.loads(json.dumps(original))
        changed["entries"] = entries
        journal.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(RuntimeError, match="metadata|snapshot checksum"):
            ExecutionKernel(allowed_roots=(root,), state_dir=state)
    journal.write_text(json.dumps(original), encoding="utf-8")
    restored = ExecutionKernel(allowed_roots=(root,), state_dir=state)
    assert len(restored.replay_journal.entries()) == 2


def test_durable_replay_retention_prunes_oldest_entries(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    kernel = ExecutionKernel(
        allowed_roots=(root,),
        state_dir=tmp_path / "state",
        max_replay_results=2,
    )

    for index in range(3):
        payload = _write_payload(
            root / f"{index}.txt",
            action_id=f"retention-{index}",
            content=str(index).encode("ascii"),
        )
        result = asyncio.run(
            kernel.execute_transaction_payloads(
                (payload,), idempotency_key=f"durable.retention.{index}"
            )
        )
        assert result.ok is True

    assert [entry.key for entry in kernel.replay_journal.entries()] == [
        "durable.retention.1",
        "durable.retention.2",
    ]
