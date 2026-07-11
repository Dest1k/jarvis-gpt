from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest
from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.approval_executor import (
    ApprovalExecution,
    ApprovalExecutor,
    finish_despite_cancellation,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.dispatcher import DispatcherManager
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.executive_runtime import ExecutiveCoordinator
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


def test_approval_executor_runs_memory_save(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Save lesson",
        description="Persist operator-approved lesson.",
        requested_action="memory.save",
        risk="review",
        payload={
            "content": "Approved actions must execute only through the gated executor.",
            "namespace": "learning",
            "tags": ["approval", "executor"],
        },
    )
    storage.update_approval(approval["id"], status="approved", result={"operator": "test"})

    result = asyncio.run(executor.execute(approval["id"]))
    updated = storage.get_approval(approval["id"])
    hits = storage.search_memory("gated executor", limit=5)

    assert result.ok is True
    assert result.approval is not None
    assert result.approval["status"] == "executed"
    assert updated is not None
    assert updated["result"]["ok"] is True
    assert hits
    storage.close()


def test_approval_executor_requires_approved_status(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Pending action",
        description="Still pending.",
        requested_action="memory.save",
        payload={"content": "not yet"},
    )

    result = asyncio.run(executor.execute(approval["id"]))

    assert result.ok is False
    assert result.status_code == 409
    assert storage.get_approval(approval["id"])["status"] == "pending"
    storage.close()


def test_approval_executor_rejects_unknown_action(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Unknown action",
        description="Should not execute.",
        requested_action="host.shell",
        risk="danger",
        payload={"command": "whoami"},
    )
    storage.update_approval(approval["id"], status="approved", result={"operator": "test"})

    result = asyncio.run(executor.execute(approval["id"]))

    assert result.ok is False
    assert result.status_code == 400
    assert result.finalize is False
    assert storage.get_approval(approval["id"])["status"] == "approved"
    storage.close()


def test_approval_executor_can_run_structured_danger_tool_after_approval(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    target = tmp_path / "approved.txt"
    approval = storage.create_approval(
        title="Write approved file",
        description="Danger tool requires an approved gate.",
        requested_action="tool.run",
        risk="danger",
        payload={
            "tool": "execution.apply",
            "arguments": {
                "payload": {
                    "protocol": "jarvis.execution.v1",
                    "action": {
                        "kind": "fs.write",
                        "action_id": "approval-write",
                        "path": str(target),
                        "content_base64": base64.b64encode(b"approved").decode("ascii"),
                    },
                }
            },
        },
    )
    storage.update_approval(approval["id"], status="approved", result={"operator": "test"})

    result = asyncio.run(executor.execute(approval["id"]))

    assert result.ok is True
    assert result.approval is not None
    assert result.approval["status"] == "executed"
    assert result.data["tool_run"]["tool"] == "execution.apply"
    assert result.data["tool_run"]["ok"] is True
    assert target.read_bytes() == b"approved"
    storage.close()


def test_approval_executor_claims_side_effect_once(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Single execution",
        description="Concurrent requests must not duplicate the side effect.",
        requested_action="memory.save",
        payload={"content": "exactly once approval memory"},
    )
    storage.update_approval(approval["id"], status="approved", result={"operator": "test"})
    original_execute = executor._execute_action
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_execute(claimed, action, payload):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await original_execute(claimed, action, payload)

    executor._execute_action = slow_execute

    async def race():
        first = asyncio.create_task(executor.execute(approval["id"]))
        await entered.wait()
        second = asyncio.create_task(executor.execute(approval["id"]))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(race())

    assert calls == 1
    assert sum(result.ok for result in results) == 1
    assert sorted(result.status_code for result in results) == [200, 409]
    assert storage.get_approval(approval["id"])["status"] == "executed"
    storage.close()


def test_repeated_caller_cancellation_waits_for_action_and_finalization(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Cancellation-safe execution",
        description="The acquired action and terminal write are one authoritative unit.",
        requested_action="memory.save",
        payload={"content": "cancellation-safe exactly once memory"},
    )
    storage.update_approval(approval["id"], status="approved")
    original_execute = executor._execute_action

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def slow_execute(claimed, action, payload):
            entered.set()
            await release.wait()
            return await original_execute(claimed, action, payload)

        executor._execute_action = slow_execute
        task = asyncio.create_task(executor.execute(approval["id"]))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    updated = storage.get_approval(approval["id"])
    assert updated is not None and updated["status"] == "executed"
    assert len(storage.search_memory("cancellation-safe exactly once memory", limit=5)) == 1
    storage.close()


def test_approval_executor_finalizes_unexpected_failure(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Fail safely",
        description="Unexpected executor errors must become terminal.",
        requested_action="memory.save",
        payload={"content": "will not be written"},
    )
    storage.update_approval(approval["id"], status="approved", result={"operator": "test"})

    async def fail_execute(_claimed, _action, _payload):
        raise RuntimeError("synthetic failure")

    executor._execute_action = fail_execute

    result = asyncio.run(executor.execute(approval["id"]))
    updated = storage.get_approval(approval["id"])

    assert result.ok is False
    assert "synthetic failure" in result.summary
    assert updated is not None
    assert updated["status"] == "failed"
    assert updated["result"]["data"]["error"] == "RuntimeError"
    storage.close()


def test_approval_terminal_result_and_errors_are_secret_redacted(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Redact terminal evidence",
        description="Approval output must not persist credentials.",
        requested_action="memory.save",
        payload={"content": "safe input"},
    )
    storage.update_approval(approval["id"], status="approved")

    async def secret_result(_claimed, _action, _payload):
        return ApprovalExecution(
            ok=True,
            summary="HTTPError: Authorization: ApiKey TERMINALSECRET",
            data={
                "password": "HUNTERSECRET",
                "stderr": "request failed Cookie: sid=COOKIESECRET",
            },
        )

    executor._execute_action = secret_result
    result = asyncio.run(executor.execute(approval["id"]))
    persisted = storage.get_approval(approval["id"])
    serialized = str(result) + str(persisted)

    assert result.ok is True
    assert "TERMINALSECRET" not in serialized
    assert "HUNTERSECRET" not in serialized
    assert "COOKIESECRET" not in serialized
    storage.close()


def test_post_claim_context_inspector_exception_finalizes_without_side_effect(
    monkeypatch, tmp_path
):
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Inspector failure",
        description="Post-claim inspection must fail closed.",
        requested_action="memory.save",
        payload={"content": "must not be saved"},
    )
    storage.update_approval(approval["id"], status="approved")
    checks = 0
    action_called = False

    def inspect_context(_payload, **_kwargs):
        nonlocal checks
        checks += 1
        if checks > 1:
            raise OSError("context database unavailable")
        return None

    async def forbidden_action(*_args):
        nonlocal action_called
        action_called = True
        raise AssertionError("side effect must not run")

    executor._mission_context_error = inspect_context
    executor._execute_action = forbidden_action

    result = asyncio.run(executor.execute(approval["id"]))

    assert result.ok is False
    assert result.status_code == 409
    assert action_called is False
    assert storage.get_approval(approval["id"])["status"] == "failed"
    storage.close()


def test_executing_approval_cannot_be_reapproved(monkeypatch, tmp_path):
    _executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="One-way state machine",
        description="An acquired execution lease cannot return to approved.",
        requested_action="memory.save",
        payload={"content": "single side effect"},
    )
    storage.update_approval(approval["id"], status="approved")

    claimed = storage.claim_approval_execution(approval["id"])
    with pytest.raises(ValueError, match="cannot transition"):
        storage.update_approval(approval["id"], status="approved")
    second_claim = storage.claim_approval_execution(approval["id"])

    assert claimed is not None and claimed["status"] == "executing"
    assert second_claim is None
    assert storage.get_approval(approval["id"])["status"] == "executing"
    storage.close()


def test_interrupted_approval_recovery_is_terminal_and_reconcile_only(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    mission = storage.create_mission(
        title="Interrupted mission",
        goal="Do not replay an ambiguous side effect",
        tasks=["Mutate state"],
    )
    task = mission["tasks"][0]
    storage.update_mission_task(task["id"], mission_id=mission["id"], status="blocked")
    approval = storage.create_approval(
        title="Interrupted action",
        description="Simulate process death after acquiring the gate.",
        requested_action="memory.save",
        payload={
            "mission_id": mission["id"],
            "task_id": task["id"],
            "content": "must never be replayed",
        },
    )
    storage.update_approval(approval["id"], status="approved")
    assert storage.claim_approval_execution(approval["id"]) is not None
    callbacks: list[tuple[str, str]] = []

    async def abort_mission(current, reason):
        callbacks.append((current["id"], reason))
        return ToolRunResponse(
            tool="mission.approval.abort",
            ok=False,
            summary="Mission branch reconciled.",
            data={"aborted": True},
        )

    executor.mission_aborter = abort_mission
    reconciled = asyncio.run(executor.reconcile_interrupted_executions())
    updated = storage.get_approval(approval["id"])

    assert [item["id"] for item in reconciled] == [approval["id"]]
    assert updated is not None and updated["status"] == "failed"
    assert updated["result"]["data"] == {
        "error": "InterruptedApprovalExecution",
        "reconcile_only": True,
    }
    assert updated["result"]["reconciliation"]["status"] == "completed"
    assert callbacks == [
        (
            approval["id"],
            "[reconcile-only] approval execution was interrupted by runtime restart; "
            "inspect authoritative state and do not replay the original action",
        )
    ]
    assert storage.search_memory("must never be replayed", limit=5) == []
    assert asyncio.run(executor.reconcile_interrupted_executions()) == []
    assert len(callbacks) == 1
    audit_actions = {
        item["action"]
        for item in storage.list_audit(target_type="approval", target_id=approval["id"])
    }
    assert "approval.execute.recover" in audit_actions
    assert "approval.reconcile.complete" in audit_actions
    storage.close()


def test_operator_rejection_uses_durable_reconciliation_outbox(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    mission = storage.create_mission(
        title="Rejected approval mission",
        goal="Reconcile rejection after any process exit",
        tasks=["Never run rejected action"],
    )
    task = mission["tasks"][0]
    storage.update_mission_task(task["id"], mission_id=mission["id"], status="blocked")
    approval = storage.create_approval(
        title="Reject durably",
        description="The callback is the second half of a durable transition.",
        requested_action="memory.save",
        payload={
            "mission_id": mission["id"],
            "task_id": task["id"],
            "content": "must stay absent",
        },
    )
    rejected = storage.update_approval(
        approval["id"],
        status="rejected",
        result={"operator": "test"},
    )
    callbacks: list[str] = []

    async def abort_mission(_approval, reason):
        callbacks.append(reason)
        return ToolRunResponse(
            tool="mission.approval.abort",
            ok=False,
            summary="Mission branch reconciled.",
            data={"aborted": True},
        )

    executor.mission_aborter = abort_mission
    assert rejected is not None
    assert rejected["result"]["reconciliation"]["status"] == "pending"

    reconciled = asyncio.run(executor.reconcile_pending_approvals(approval_id=approval["id"]))
    updated = storage.get_approval(approval["id"])

    assert [item["id"] for item in reconciled] == [approval["id"]]
    assert updated is not None and updated["status"] == "rejected"
    assert updated["result"]["operator"] == "test"
    assert updated["result"]["reconciliation"]["status"] == "completed"
    assert callbacks == [
        "[reconcile-only] operator rejected the approval; do not execute or replay "
        "the original action"
    ]
    assert storage.search_memory("must stay absent", limit=5) == []
    storage.close()


def test_empty_mission_abort_result_uses_executive_terminal_fallback(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    mission = storage.create_mission(
        title="Fallback reconciliation",
        goal="Never acknowledge an unreconciled approval branch",
        tasks=["Mutate state"],
    )
    task = mission["tasks"][0]
    storage.update_mission_task(task["id"], mission_id=mission["id"], status="blocked")
    approval = storage.create_approval(
        title="Reject with fallback",
        description="Exercise the terminal executive fallback.",
        requested_action="memory.save",
        payload={
            "mission_id": mission["id"],
            "task_id": task["id"],
            "content": "must stay absent",
        },
    )
    storage.update_approval(approval["id"], status="rejected")
    terminations: list[tuple[str, str]] = []

    async def no_reconciliation(_approval, _reason):
        return None

    class ExecutiveFallback:
        @staticmethod
        def terminate_mission(mission_id, *, reason):
            terminations.append((mission_id, reason))

    executor.mission_aborter = no_reconciliation
    executor.tools.executive = ExecutiveFallback()
    reconciled = asyncio.run(executor.reconcile_pending_approvals(approval_id=approval["id"]))
    updated = storage.get_approval(approval["id"])

    assert [item["id"] for item in reconciled] == [approval["id"]]
    assert updated["result"]["reconciliation"]["status"] == "completed"
    assert terminations == [
        (
            mission["id"],
            "approval reconciliation callback failed: "
            "mission reconciliation callback returned no result",
        )
    ]
    storage.close()


def test_empty_mission_abort_result_retains_reconciliation_without_fallback(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    mission = storage.create_mission(
        title="Retained reconciliation",
        goal="Keep the durable outbox pending",
        tasks=["Mutate state"],
    )
    task = mission["tasks"][0]
    storage.update_mission_task(task["id"], mission_id=mission["id"], status="blocked")
    approval = storage.create_approval(
        title="Reject without fallback",
        description="The callback cannot reconcile this branch.",
        requested_action="memory.save",
        payload={
            "mission_id": mission["id"],
            "task_id": task["id"],
            "content": "must stay absent",
        },
    )
    storage.update_approval(approval["id"], status="rejected")

    async def no_reconciliation(_approval, _reason):
        return None

    executor.mission_aborter = no_reconciliation
    executor.tools.executive = None
    reconciled = asyncio.run(executor.reconcile_pending_approvals(approval_id=approval["id"]))
    updated = storage.get_approval(approval["id"])

    assert reconciled == []
    assert updated["result"]["reconciliation"]["status"] == "pending"
    storage.close()


@pytest.mark.parametrize("interrupted", [False, True])
def test_cold_start_reconciliation_outbox_is_idempotent_after_dag_adaptation(
    monkeypatch,
    tmp_path,
    interrupted,
):
    executor, storage = _runtime(monkeypatch, tmp_path)
    profile = {
        "schema": "jarvis.host-profile.v1",
        "fingerprint_sha256": hashlib.sha256(b"outbox-order").hexdigest(),
        "host": {"os": {}, "architecture": {}, "accelerators": {}, "tools": {}},
    }
    mission = storage.create_mission(
        title="Outbox ordering",
        goal="Write exact recovery artifact",
        tasks=["Write exact recovery artifact"],
    )
    first = ExecutiveCoordinator(storage=storage, host_profile=profile)
    first.create_for_mission(mission)
    claim = first.claim_ready_task(mission["id"])
    storage.update_mission_task(claim.task["id"], mission_id=mission["id"], status="blocked")
    arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "outbox-write",
                "kind": "fs.write",
                "path": str(tmp_path / "exact-recovery-artifact.txt"),
                "content_base64": "",
            },
        }
    }
    first.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments,
    )
    planner = first.snapshot(mission["id"])["planner"]
    step = next(item for item in planner["steps"] if item["spec"]["step_id"] == claim.step_id)
    approval = storage.create_approval(
        title="Outbox capability",
        description="Exercise at-least-once reconciliation after planner recovery.",
        requested_action="tool.run",
        risk="danger",
        payload={
            "mission_id": mission["id"],
            "task_id": claim.task["id"],
            "tool": "execution.apply",
            "arguments": arguments,
            "executive_claim": {
                "protocol": "jarvis.executive-approval.v1",
                "mission_id": mission["id"],
                "task_id": claim.task["id"],
                "step_id": claim.step_id,
                "plan_revision": planner["revision"],
                "step_attempt": step["attempts"],
                "environment_digest": planner["environment"]["digest"],
                "verification_contract": step["verification_contract"],
            },
        },
    )
    if interrupted:
        storage.update_approval(approval["id"], status="approved")
        assert storage.claim_approval_execution(approval["id"]) is not None
        storage.recover_interrupted_approval_executions()
    else:
        storage.update_approval(approval["id"], status="rejected")

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=profile,
        recover_interrupted=True,
    )
    executor.tools.executive = resumed
    agent = AgentRuntime(
        settings=executor.tools.settings,
        storage=storage,
        llm=executor.llm,
        bus=EventBus(),
        tools=executor.tools,
        executive=resumed,
    )
    executor.mission_aborter = agent.abort_mission_after_approval

    reconciled = asyncio.run(executor.reconcile_pending_approvals(approval_id=approval["id"]))
    snapshot = resumed.snapshot(mission["id"])["planner"]

    assert [item["id"] for item in reconciled] == [approval["id"]]
    assert storage.get_approval(approval["id"])["result"]["reconciliation"]["status"] == "completed"
    assert snapshot["status"] in {"ready", "running"}
    assert snapshot["revision"] == 1
    recovery = next(
        item for item in snapshot["steps"] if item["spec"]["step_id"].startswith("recover.r")
    )
    assert recovery["spec"]["action"]["tool"] == "execution.verify"
    assert recovery["spec"]["action"]["arguments"]["source_tool"] == "execution.apply"
    assert not (tmp_path / "exact-recovery-artifact.txt").exists()
    storage.close()


def test_authoritative_callback_finishes_before_cancellation_propagates():
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = False

    async def callback():
        nonlocal finished
        entered.set()
        await release.wait()
        finished = True

    async def scenario():
        task = asyncio.create_task(finish_despite_cancellation(callback()))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert finished is True


def test_stale_mission_approval_is_rejected_before_side_effect(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    mission = storage.create_mission(
        title="Bound mission",
        goal="Write once",
        tasks=["Write file"],
    )
    task = mission["tasks"][0]
    storage.update_mission_task(task["id"], mission_id=mission["id"], status="blocked")
    target = tmp_path / "must-not-exist.txt"
    approval = storage.create_approval(
        title="Stale write",
        description="Must stay bound to the blocked task.",
        requested_action="tool.run",
        risk="danger",
        payload={
            "mission_id": mission["id"],
            "task_id": task["id"],
            "tool": "execution.apply",
            "arguments": {
                "payload": {
                    "protocol": "jarvis.execution.v1",
                    "action": {
                        "kind": "fs.write",
                        "action_id": "stale-write",
                        "path": str(target),
                        "content_base64": base64.b64encode(b"forbidden").decode("ascii"),
                    },
                }
            },
        },
    )
    storage.update_approval(approval["id"], status="approved")
    storage.update_mission_task(task["id"], mission_id=mission["id"], status="done")

    result = asyncio.run(executor.execute(approval["id"]))

    assert result.ok is False
    assert result.status_code == 409
    assert result.data["stale"] is True
    assert not target.exists()
    assert storage.get_approval(approval["id"])["status"] == "approved"
    storage.close()


def test_mission_approval_without_durable_executive_plan_fails_closed(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    mission = storage.create_mission(
        title="Missing executive plan",
        goal="Never execute without a durable DAG capability",
        tasks=["Write file"],
    )
    task = mission["tasks"][0]
    storage.update_mission_task(task["id"], mission_id=mission["id"], status="blocked")
    target = tmp_path / "missing-plan-must-not-exist.txt"
    approval = storage.create_approval(
        title="Unbound write",
        description="A blocked task alone is not an execution capability.",
        requested_action="tool.run",
        risk="danger",
        payload={
            "mission_id": mission["id"],
            "task_id": task["id"],
            "tool": "execution.apply",
            "arguments": {
                "payload": {
                    "protocol": "jarvis.execution.v1",
                    "action": {
                        "kind": "fs.write",
                        "action_id": "unbound-write",
                        "path": str(target),
                        "content_base64": base64.b64encode(b"forbidden").decode("ascii"),
                    },
                }
            },
        },
    )
    storage.update_approval(approval["id"], status="approved")
    executor.tools.executive = None

    result = asyncio.run(executor.execute(approval["id"]))

    assert result.ok is False and result.status_code == 409
    assert "executive coordinator is unavailable" in result.summary
    assert storage.get_approval(approval["id"])["status"] == "approved"
    assert not target.exists()
    storage.close()


def test_post_claim_context_race_fails_terminal_before_side_effect(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    profile = {
        "schema": "jarvis.host-profile.v1",
        "collected_at": "2026-07-10T00:00:00+00:00",
        "fingerprint_sha256": hashlib.sha256(b"approval-race").hexdigest(),
        "host": {
            "os": {"system": "Windows"},
            "architecture": {"machine": "AMD64"},
            "accelerators": {},
            "tools": {},
        },
    }
    mission = storage.create_mission(
        title="Post-claim race",
        goal="Never execute against stale mission state",
        tasks=["Execute mission state mutation once"],
    )
    executive = ExecutiveCoordinator(storage=storage, host_profile=profile)
    executive.create_for_mission(mission)
    claim = executive.claim_ready_task(mission["id"])
    storage.update_mission_task(claim.task["id"], mission_id=mission["id"], status="blocked")
    executor.tools.executive = executive
    arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "post-claim-race",
                "kind": "fs.write",
                "path": str(tmp_path / "mission-state-mutation-once.txt"),
                "content_base64": "",
            },
        }
    }
    executive.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments,
    )
    planner = executive.snapshot(mission["id"])["planner"]
    step = next(item for item in planner["steps"] if item["spec"]["step_id"] == claim.step_id)
    executive_claim = {
        "protocol": "jarvis.executive-approval.v1",
        "mission_id": mission["id"],
        "task_id": claim.task["id"],
        "step_id": claim.step_id,
        "plan_revision": planner["revision"],
        "step_attempt": step["attempts"],
        "environment_digest": planner["environment"]["digest"],
        "verification_contract": step["verification_contract"],
    }
    approval = storage.create_approval(
        title="Race-bound approval",
        description="State changes immediately after the capability is claimed.",
        requested_action="tool.run",
        payload={
            "mission_id": mission["id"],
            "task_id": claim.task["id"],
            "tool": "execution.apply",
            "arguments": arguments,
            "executive_claim": executive_claim,
        },
    )
    storage.update_approval(approval["id"], status="approved")
    original_claim = storage.claim_approval_execution
    action_calls = 0

    def claim_then_change_task(approval_id):
        claimed = original_claim(approval_id)
        storage.update_mission_task(claim.task["id"], mission_id=mission["id"], status="done")
        return claimed

    async def forbidden_action(*_args):
        nonlocal action_calls
        action_calls += 1
        raise AssertionError("stale action executed")

    monkeypatch.setattr(storage, "claim_approval_execution", claim_then_change_task)
    executor._execute_action = forbidden_action

    result = asyncio.run(executor.execute(approval["id"]))
    updated = storage.get_approval(approval["id"])

    assert result.ok is False and result.status_code == 409
    assert result.data == {"stale": True, "post_claim_validation": False}
    assert action_calls == 0
    assert updated is not None and updated["status"] == "failed"
    assert storage.search_memory("must not be persisted after race", limit=5) == []
    storage.close()


def test_executive_approval_rejects_argument_substitution_before_side_effect(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    profile = {
        "schema": "jarvis.host-profile.v1",
        "fingerprint_sha256": hashlib.sha256(b"argument-binding").hexdigest(),
        "host": {"os": {}, "architecture": {}, "accelerators": {}, "tools": {}},
    }
    mission = storage.create_mission(
        title="Argument-bound mission",
        goal="Write only the exactly reviewed artifact",
        tasks=["Write reviewed artifact"],
    )
    executive = ExecutiveCoordinator(storage=storage, host_profile=profile)
    executive.create_for_mission(mission)
    claim = executive.claim_ready_task(mission["id"])
    storage.update_mission_task(claim.task["id"], mission_id=mission["id"], status="blocked")
    executor.tools.executive = executive
    reviewed_target = tmp_path / "reviewed.txt"
    substituted_target = tmp_path / "substituted.txt"
    reviewed_arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "reviewed-write",
                "kind": "fs.write",
                "path": str(reviewed_target),
                "content_base64": base64.b64encode(b"reviewed").decode("ascii"),
            },
        }
    }
    executive.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=reviewed_arguments,
    )
    planner = executive.snapshot(mission["id"])["planner"]
    step = next(item for item in planner["steps"] if item["spec"]["step_id"] == claim.step_id)
    substituted_arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "substituted-write",
                "kind": "fs.write",
                "path": str(substituted_target),
                "content_base64": base64.b64encode(b"forbidden").decode("ascii"),
            },
        }
    }
    approval = storage.create_approval(
        title="Substituted approval",
        description="A copied claim must not authorize different arguments.",
        requested_action="tool.run",
        risk="danger",
        payload={
            "mission_id": mission["id"],
            "task_id": claim.task["id"],
            "tool": "execution.apply",
            "arguments": substituted_arguments,
            "executive_claim": {
                "protocol": "jarvis.executive-approval.v1",
                "mission_id": mission["id"],
                "task_id": claim.task["id"],
                "step_id": claim.step_id,
                "plan_revision": planner["revision"],
                "step_attempt": step["attempts"],
                "environment_digest": planner["environment"]["digest"],
                "verification_contract": step["verification_contract"],
            },
        },
    )
    storage.update_approval(approval["id"], status="approved")

    result = asyncio.run(executor.execute(approval["id"]))

    assert result.ok is False and result.status_code == 409
    assert "exact execution arguments" in result.summary
    assert storage.get_approval(approval["id"])["status"] == "approved"
    assert not reviewed_target.exists()
    assert not substituted_target.exists()
    storage.close()


def test_executive_transaction_approval_executes_once_without_replay(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    profile = {
        "schema": "jarvis.host-profile.v1",
        "fingerprint_sha256": hashlib.sha256(b"transaction-approval").hexdigest(),
        "host": {"os": {}, "architecture": {}, "accelerators": {}, "tools": {}},
    }
    target = tmp_path / "transaction-approved.txt"
    objective = f"Write {target} exactly once"
    mission = storage.create_mission(
        title="Transaction-bound mission",
        goal=objective,
        tasks=[objective],
    )
    executive = ExecutiveCoordinator(storage=storage, host_profile=profile)
    executive.create_for_mission(mission)
    claim = executive.claim_ready_task(mission["id"])
    storage.update_mission_task(claim.task["id"], mission_id=mission["id"], status="blocked")
    executor.tools.executive = executive
    arguments = {
        "actions": [
            {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "action_id": "transaction-approved-write",
                    "kind": "fs.write",
                    "path": str(target),
                    "content_base64": base64.b64encode(b"approved-once").decode("ascii"),
                },
            }
        ],
        "idempotency_key": "approval.transaction.once",
    }
    executive.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.transaction",
        arguments=arguments,
    )
    planner = executive.snapshot(mission["id"])["planner"]
    step = next(item for item in planner["steps"] if item["spec"]["step_id"] == claim.step_id)
    executive_claim = {
        "protocol": "jarvis.executive-approval.v1",
        "mission_id": mission["id"],
        "task_id": claim.task["id"],
        "step_id": claim.step_id,
        "plan_revision": planner["revision"],
        "step_attempt": step["attempts"],
        "environment_digest": planner["environment"]["digest"],
        "verification_contract": step["verification_contract"],
    }
    substituted_arguments = {**arguments, "idempotency_key": "approval.transaction.substituted"}
    substituted = storage.create_approval(
        title="Substituted transaction",
        description="A copied claim must not authorize a different transaction.",
        requested_action="tool.run",
        risk="danger",
        payload={
            "mission_id": mission["id"],
            "task_id": claim.task["id"],
            "tool": "execution.transaction",
            "arguments": substituted_arguments,
            "executive_claim": executive_claim,
        },
    )
    storage.update_approval(substituted["id"], status="approved")
    rejected = asyncio.run(executor.execute(substituted["id"]))
    assert rejected.ok is False and rejected.status_code == 409
    assert "exact execution arguments" in rejected.summary
    assert not target.exists()

    approval = storage.create_approval(
        title="Approved transaction",
        description="Execute the exactly bound transaction once.",
        requested_action="tool.run",
        risk="danger",
        payload={
            "mission_id": mission["id"],
            "task_id": claim.task["id"],
            "tool": "execution.transaction",
            "arguments": arguments,
            "executive_claim": executive_claim,
        },
    )
    storage.update_approval(approval["id"], status="approved")

    first = asyncio.run(executor.execute(approval["id"]))
    assert first.ok is True
    assert first.data["tool_run"]["tool"] == "execution.transaction"
    assert target.read_bytes() == b"approved-once"
    assert storage.get_approval(approval["id"])["status"] == "executed"

    target.write_bytes(b"changed-after-execution")
    second = asyncio.run(executor.execute(approval["id"]))

    assert second.ok is False and second.status_code == 409
    assert target.read_bytes() == b"changed-after-execution"
    assert storage.get_approval(approval["id"])["status"] == "executed"
    storage.close()


def test_executive_approval_rejects_filesystem_wrapper_before_side_effect(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    profile = {
        "schema": "jarvis.host-profile.v1",
        "fingerprint_sha256": hashlib.sha256(b"wrapper-policy").hexdigest(),
        "host": {"os": {}, "architecture": {}, "accelerators": {}, "tools": {}},
    }
    mission = storage.create_mission(
        title="Canonical mutation mission",
        goal="Reject wrapper mutations",
        tasks=["Write once"],
    )
    executive = ExecutiveCoordinator(storage=storage, host_profile=profile)
    executive.create_for_mission(mission)
    claim = executive.claim_ready_task(mission["id"])
    storage.update_mission_task(claim.task["id"], mission_id=mission["id"], status="blocked")
    executor.tools.executive = executive
    step = next(item for item in claim.planner["steps"] if item["spec"]["step_id"] == claim.step_id)
    target = tmp_path / "wrapper-must-not-run.txt"
    approval = storage.create_approval(
        title="Forbidden wrapper",
        description="Mission wrappers must fail closed.",
        requested_action="tool.run",
        payload={
            "mission_id": mission["id"],
            "task_id": claim.task["id"],
            "tool": "filesystem.write_text",
            "arguments": {"path": str(target), "content": "forbidden"},
            "executive_claim": {
                "protocol": "jarvis.executive-approval.v1",
                "mission_id": mission["id"],
                "task_id": claim.task["id"],
                "step_id": claim.step_id,
                "plan_revision": claim.planner["revision"],
                "step_attempt": step["attempts"],
                "environment_digest": claim.planner["environment"]["digest"],
            },
        },
    )
    storage.update_approval(approval["id"], status="approved")

    result = asyncio.run(executor.execute(approval["id"]))

    assert result.status_code == 409
    assert "contract-bound execution.apply" in result.summary
    assert not target.exists()
    assert storage.get_approval(approval["id"])["status"] == "approved"
    storage.close()


def test_executive_approval_is_bound_to_persisted_and_current_environment(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    mission = storage.create_mission(
        title="Environment-bound mission",
        goal="Run only against the claimed host state",
        tasks=["Inspect host"],
    )
    task = mission["tasks"][0]
    storage.update_mission_task(task["id"], mission_id=mission["id"], status="blocked")
    digest = "a" * 64
    contract = {
        "protocol": "jarvis.step-verification-contract.v1",
        "tool": "execution.apply",
        "arguments_sha256": "c" * 64,
        "action_id": "environment-check",
        "action_kind": "WriteFileAction",
        "postcondition_sha256": "d" * 64,
        "objective_sha256": "e" * 64,
    }

    class Environment:
        def __init__(self, value):
            self.digest = value

    class Executive:
        environment = Environment("b" * 64)

        @staticmethod
        def snapshot(_mission_id):
            return {
                "planner": {
                    "revision": 2,
                    "environment": {"digest": digest},
                    "steps": [
                        {
                            "spec": {"step_id": "step_1"},
                            "status": "running",
                            "attempts": 1,
                            "verification_contract": contract,
                        }
                    ],
                },
                "task_map": {"step_1": task["id"]},
            }

        @staticmethod
        def action_contract_matches(
            _mission_id,
            _task_id,
            *,
            tool,
            arguments,
            expected_contract,
        ):
            return (
                tool == "execution.apply"
                and arguments == {"payload": "fixture"}
                and expected_contract == contract
            )

    executor.tools.executive = Executive()
    payload = {
        "mission_id": mission["id"],
        "task_id": task["id"],
        "tool": "execution.apply",
        "arguments": {"payload": "fixture"},
        "executive_claim": {
            "protocol": "jarvis.executive-approval.v1",
            "mission_id": mission["id"],
            "task_id": task["id"],
            "step_id": "step_1",
            "plan_revision": 2,
            "step_attempt": 1,
            "environment_digest": digest,
            "verification_contract": contract,
        },
    }

    assert "environment fingerprint" in executor._mission_context_error(payload)
    executor.tools.executive.environment = Environment(digest)
    assert executor._mission_context_error(payload) is None
    payload["executive_claim"]["environment_digest"] = "c" * 64
    assert "environment fingerprint" in executor._mission_context_error(payload)
    storage.close()


def _runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = LLMRouter(settings)
    tools = ToolRegistry(settings, storage, llm)
    executor = ApprovalExecutor(
        storage=storage,
        llm=llm,
        dispatcher=DispatcherManager(settings, repo_root=tmp_path),
        tools=tools,
    )
    return executor, storage
