from __future__ import annotations

import asyncio
import base64
import re
import sys
from pathlib import Path

import pytest
from jarvis_gpt.execution_actions import ProcessAction
from jarvis_gpt.execution_kernel import ExecutionKernel, KernelCapabilities
from jarvis_gpt.execution_process import ExecutableRule, ProcessRequest


def test_kernel_transactions_and_idempotent_replay(tmp_path):
    state = tmp_path / "state"
    root = tmp_path / "root"
    root.mkdir()
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "action_id": "same-action",
            "path": str(root / "value.txt"),
            "content_base64": base64.b64encode(b"one").decode(),
        },
    }
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=state)

    first = asyncio.run(kernel.execute_payload(payload))
    second = asyncio.run(kernel.execute_payload(payload))

    assert first.ok is True
    assert first.transactional is True
    assert second.ok is True
    assert second.replayed is True
    assert (root / "value.txt").read_bytes() == b"one"

    payload["action"]["content_base64"] = base64.b64encode(b"two").decode()
    with pytest.raises(ValueError, match="reused"):
        asyncio.run(kernel.execute_payload(payload))


def test_kernel_disables_processes_without_capability(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=tmp_path / "state")
    action = ProcessAction(ProcessRequest(executable="missing"))

    result = asyncio.run(kernel.execute(action))

    assert result.ok is False
    assert "no executable rules" in result.feedback.error


def test_kernel_process_is_nontransactional_and_session_recorded(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    kernel = ExecutionKernel(
        allowed_roots=(root,),
        state_dir=tmp_path / "state",
        capabilities=KernelCapabilities(
            executable_rules=(
                ExecutableRule(Path(sys.executable), argument_patterns=(r"--version",)),
            )
        ),
    )
    session = kernel.create_session(session_id="session_process")

    result = asyncio.run(
        kernel.execute(
            ProcessAction(
                ProcessRequest(
                    executable=sys.executable,
                    arguments=("--version",),
                    cwd=root,
                ),
                session_id=session.session_id,
            )
        )
    )

    assert result.ok is True
    assert result.transactional is False
    assert kernel.sessions.snapshot(session.session_id)["status"] == "succeeded"


def test_kernel_atomic_batch_is_session_recorded_and_idempotent(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=tmp_path / "state")
    session = kernel.create_session(session_id="session_batch")
    payloads = tuple(
        {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "fs.write",
                "action_id": f"batch-{index}",
                "path": str(root / f"{index}.txt"),
                "content_base64": base64.b64encode(str(index).encode()).decode(),
            },
        }
        for index in range(2)
    )

    first = asyncio.run(
        kernel.execute_transaction_payloads(
            payloads, idempotency_key="batch-key", session_id=session.session_id
        )
    )
    replay = asyncio.run(
        kernel.execute_transaction_payloads(payloads, idempotency_key="batch-key")
    )

    assert first.ok is True
    assert len(first.feedback) == 2
    assert replay.replayed is True
    snapshot = kernel.sessions.snapshot(session.session_id)
    assert snapshot["status"] == "succeeded"
    assert len(snapshot["history"]) == 2


def test_kernel_atomic_batch_rejects_non_reversible_actions(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=tmp_path / "state")
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.stat",
            "action_id": "read-in-batch",
            "path": str(root),
        },
    }

    with pytest.raises(ValueError, match="reversible mutation"):
        asyncio.run(
            kernel.execute_transaction_payloads((payload,), idempotency_key="invalid-batch")
        )


def test_kernel_batch_exception_finalizes_session(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=tmp_path / "state")
    session = kernel.create_session(session_id="batch_exception_session")
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "action_id": "outside-batch-write",
            "path": str(outside / "value.txt"),
            "content_base64": base64.b64encode(b"blocked").decode(),
        },
    }

    with pytest.raises(ValueError, match="escapes allowed roots"):
        asyncio.run(
            kernel.execute_transaction_payloads(
                (payload,),
                idempotency_key="outside-batch",
                session_id=session.session_id,
            )
        )

    snapshot = session.snapshot()
    assert snapshot["status"] == "failed"
    assert snapshot["history"][-1]["status"] == "failed"
    assert not (outside / "value.txt").exists()


def test_kernel_cancel_escalates_only_session_owned_process(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    kernel = ExecutionKernel(
        allowed_roots=(root,),
        state_dir=tmp_path / "state",
        capabilities=KernelCapabilities(
            executable_rules=(
                ExecutableRule(
                    Path(sys.executable),
                    argument_patterns=(r"-c", r"import time; time\.sleep\(30\)"),
                ),
            )
        ),
    )
    session = kernel.create_session(session_id="session_cancel")
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "process.run",
            "action_id": "cancel-process",
            "executable": sys.executable,
            "arguments": ["-c", "import time; time.sleep(30)"],
            "cwd": str(root),
            "timeout_seconds": 60,
            "session_id": session.session_id,
        },
    }

    async def scenario():
        task = asyncio.create_task(kernel.execute_payload(payload))
        for _ in range(100):
            if session.running_pids():
                break
            await asyncio.sleep(0.01)
        assert session.running_pids()
        cancelled = await kernel.cancel_session(session.session_id)
        process_result = await asyncio.wait_for(task, timeout=5)
        return cancelled, process_result

    cancelled, process_result = asyncio.run(scenario())

    assert cancelled["ok"] is True
    assert cancelled["terminated_pids"]
    assert cancelled["session"]["status"] == "cancelled"
    assert process_result.ok is False


def test_kernel_serializes_same_action_id_before_any_second_mutation(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=tmp_path / "state")
    first_payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "action_id": "shared-action-id",
            "path": str(root / "first.txt"),
            "content_base64": base64.b64encode(b"first").decode(),
        },
    }
    second_payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            **first_payload["action"],
            "path": str(root / "second.txt"),
            "content_base64": base64.b64encode(b"second").decode(),
        },
    }
    original_execute = kernel.transactions.execute
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed_execute(actions, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original_execute(actions, **kwargs)

    monkeypatch.setattr(kernel.transactions, "execute", delayed_execute)

    async def scenario():
        first = asyncio.create_task(kernel.execute_payload(first_payload))
        await entered.wait()
        second = asyncio.create_task(kernel.execute_payload(second_payload))
        await asyncio.sleep(0.05)
        assert calls == 1
        release.set()
        assert (await first).ok is True
        with pytest.raises(ValueError, match="reused"):
            await second

    asyncio.run(scenario())

    assert calls == 1
    assert (root / "first.txt").read_bytes() == b"first"
    assert not (root / "second.txt").exists()


def test_committed_mutation_records_idempotency_before_honoring_cancellation(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=tmp_path / "state")
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "action_id": "cancel-at-remember",
            "path": str(root / "committed.txt"),
            "content_base64": base64.b64encode(b"committed").decode(),
        },
    }
    original_remember = kernel._remember
    remember_started = asyncio.Event()
    release_remember = asyncio.Event()

    async def delayed_remember(action_id, fingerprint, result):
        remember_started.set()
        await release_remember.wait()
        await original_remember(action_id, fingerprint, result)

    monkeypatch.setattr(kernel, "_remember", delayed_remember)

    async def scenario():
        task = asyncio.create_task(kernel.execute_payload(payload))
        await remember_started.wait()
        task.cancel()
        release_remember.set()
        result = await task
        replay = await kernel.execute_payload(payload)
        return task, result, replay

    task, result, replay = asyncio.run(scenario())
    assert task.cancelled() is False
    assert task.cancelling() == 0
    assert result.ok is True
    assert replay.replayed is True
    assert (root / "committed.txt").read_bytes() == b"committed"


def test_kernel_serializes_sibling_transactions_under_new_parent(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    kernel = ExecutionKernel(allowed_roots=(root,), state_dir=tmp_path / "state")
    payloads = [
        {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "fs.write",
                "action_id": f"sibling-{index}",
                "path": str(root / "new-parent" / f"{index}.txt"),
                "content_base64": base64.b64encode(str(index).encode()).decode(),
                "create_parents": True,
            },
        }
        for index in range(2)
    ]
    original_execute = kernel.transactions.execute
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def delayed_execute(actions, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original_execute(actions, **kwargs)

    monkeypatch.setattr(kernel.transactions, "execute", delayed_execute)

    async def scenario():
        first = asyncio.create_task(kernel.execute_payload(payloads[0]))
        await entered.wait()
        second = asyncio.create_task(kernel.execute_payload(payloads[1]))
        await asyncio.sleep(0.05)
        assert calls == 1
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
        assert first_result.ok and second_result.ok

    asyncio.run(scenario())

    assert calls == 2
    assert (root / "new-parent" / "0.txt").read_text() == "0"
    assert (root / "new-parent" / "1.txt").read_text() == "1"


def test_kernel_rejects_process_cwd_outside_execution_roots(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    kernel = ExecutionKernel(
        allowed_roots=(root,),
        state_dir=tmp_path / "state",
        capabilities=KernelCapabilities(
            executable_rules=(
                ExecutableRule(Path(sys.executable), argument_patterns=(r"--version",)),
            )
        ),
    )
    session = kernel.create_session(session_id="outside_cwd_session")

    result = asyncio.run(
        kernel.execute(
            ProcessAction(
                ProcessRequest(
                    executable=sys.executable,
                    arguments=("--version",),
                    cwd=outside,
                ),
                session_id=session.session_id,
            )
        )
    )

    assert result.ok is False
    assert "escapes allowed roots" in (result.feedback.error or "")
    snapshot = session.snapshot()
    assert snapshot["status"] == "failed"
    assert snapshot["process_start_pending"] is False


def test_kernel_requires_per_executable_environment_grammar(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    blocked_kernel = ExecutionKernel(
        allowed_roots=(root,),
        state_dir=tmp_path / "blocked-state",
        capabilities=KernelCapabilities(
            executable_rules=(
                ExecutableRule(Path(sys.executable), argument_patterns=(r"--version",)),
            )
        ),
    )
    allowed_kernel = ExecutionKernel(
        allowed_roots=(root,),
        state_dir=tmp_path / "allowed-state",
        capabilities=KernelCapabilities(
            executable_rules=(
                ExecutableRule(
                    Path(sys.executable),
                    argument_patterns=(r"--version",),
                    environment_patterns=(("JARVIS_TEST_MODE", r"safe"),),
                ),
            )
        ),
    )
    request = ProcessRequest(
        executable=sys.executable,
        arguments=("--version",),
        cwd=root,
        environment={"JARVIS_TEST_MODE": "safe"},
    )

    blocked = asyncio.run(blocked_kernel.execute(ProcessAction(request)))
    allowed = asyncio.run(allowed_kernel.execute(ProcessAction(request)))

    assert blocked.ok is False
    assert "environment key" in (blocked.feedback.error or "")
    assert allowed.ok is True


def test_cancelling_process_task_finalizes_session_state(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    script = "import time; time.sleep(30)"
    kernel = ExecutionKernel(
        allowed_roots=(root,),
        state_dir=tmp_path / "state",
        capabilities=KernelCapabilities(
            executable_rules=(
                ExecutableRule(Path(sys.executable), argument_patterns=(r"-c", re.escape(script))),
            )
        ),
    )
    session = kernel.create_session(session_id="cancel_task_session")
    action = ProcessAction(
        ProcessRequest(
            executable=sys.executable,
            arguments=("-c", script),
            cwd=root,
            timeout_seconds=60,
        ),
        session_id=session.session_id,
        action_id="cancel-task-action",
    )

    async def scenario():
        task = asyncio.create_task(kernel.execute(action))
        for _ in range(200):
            if session.running_pids():
                break
            await asyncio.sleep(0.01)
        assert session.running_pids()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    snapshot = session.snapshot()
    assert snapshot["status"] == "cancelled"
    assert snapshot["running_pids"] == []
    assert snapshot["process_start_pending"] is False
    assert snapshot["history"][-1]["status"] == "cancelled"


def test_session_cancel_waits_for_reserved_process_registration(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    script = "import time; time.sleep(30)"
    kernel = ExecutionKernel(
        allowed_roots=(root,),
        state_dir=tmp_path / "state",
        capabilities=KernelCapabilities(
            executable_rules=(
                ExecutableRule(Path(sys.executable), argument_patterns=(r"-c", re.escape(script))),
            )
        ),
    )
    session = kernel.create_session(session_id="reserved_cancel_session")
    original_run = kernel.actions.process_runner.run
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_run(request, *, session=None, reservation_id=None):
        entered.set()
        await release.wait()
        return await original_run(
            request,
            session=session,
            reservation_id=reservation_id,
        )

    monkeypatch.setattr(kernel.actions.process_runner, "run", delayed_run)
    action = ProcessAction(
        ProcessRequest(
            executable=sys.executable,
            arguments=("-c", script),
            cwd=root,
            timeout_seconds=60,
        ),
        session_id=session.session_id,
        action_id="reserved-cancel-action",
    )

    async def scenario():
        process_task = asyncio.create_task(kernel.execute(action))
        await entered.wait()
        assert session.snapshot()["process_start_pending"] is True
        cancel_task = asyncio.create_task(kernel.cancel_session(session.session_id))
        await asyncio.sleep(0.05)
        assert session.status.value == "waiting"
        assert not cancel_task.done()
        release.set()
        cancelled, process_result = await asyncio.gather(cancel_task, process_task)
        return cancelled, process_result

    cancelled, process_result = asyncio.run(scenario())
    assert cancelled["ok"] is True
    assert cancelled["session"]["status"] == "cancelled"
    assert process_result.ok is False
    assert session.running_pids() == ()
