from __future__ import annotations

import hashlib
import json
import re

import pytest
from jarvis_gpt.cognitive_memory import ExecutionPlaybookStore
from jarvis_gpt.executive_runtime import ExecutiveCoordinator, _anchor_matches_subject
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage


def _profile(label: str) -> dict:
    return {
        "schema": "jarvis.host-profile.v1",
        "collected_at": "2026-07-10T00:00:00+00:00",
        "fingerprint_sha256": hashlib.sha256(label.encode()).hexdigest(),
        "host": {
            "os": {"system": "Windows", "release": label},
            "architecture": {"machine": "AMD64"},
            "accelerators": {"gpu": [], "cuda": {"available": False}, "npu": []},
            "tools": {"linters": [], "compilers": []},
        },
    }


def _storage(tmp_path) -> JarvisStorage:
    storage = JarvisStorage(tmp_path / "state" / "jarvis.sqlite3")
    storage.initialize()
    return storage


def _mission(storage: JarvisStorage) -> dict:
    return storage.create_mission(
        title="Adaptive mission",
        goal="Build and independently verify an adaptive runtime",
        tasks=[
            "Implement adaptive runtime foundation",
            "Inspect adaptive runtime environment",
            "Design adaptive runtime components",
            "Implement adaptive runtime",
            "Verify adaptive runtime",
            "Record adaptive runtime deliverable",
        ],
    )


def _success(action_id: str = "verified-action") -> ToolRunResponse:
    return ToolRunResponse(
        tool="execution.apply",
        ok=True,
        summary="Step completed and checked.",
        data={
            "verification": {
                "ok": True,
                "status": "passed",
                "action_id": action_id,
                "action_kind": "WriteFileAction",
                "summary": "Independent test inspector passed.",
                "evidence": [
                    {
                        "source": "test.inspector",
                        "assertion": "fresh state matches expectation",
                        "expected": True,
                        "observed": True,
                        "passed": True,
                        "captured_at": "2026-07-11T00:00:00+00:00",
                        "error": None,
                        "subject": action_id,
                    }
                ],
                "error": None,
            }
        },
    )


def _record_success(
    coordinator: ExecutiveCoordinator,
    mission_id: str,
    claim,
):
    step = next(item for item in claim.planner["steps"] if item["spec"]["step_id"] == claim.step_id)
    spec = step["spec"]
    policy = spec["evidence_policy"]
    if policy == "artifact":
        summary = (
            f"Recorded artifact for {spec['title']}: {spec['objective']} with explicit "
            "acceptance constraints."
        )
        binding = coordinator.cognitive_artifact_binding(
            mission_id,
            claim.task["id"],
        )
        result = ToolRunResponse(
            tool="mission.execute_next",
            ok=True,
            summary=summary,
            data={
                "executive_artifact": {
                    **binding,
                    "summary_sha256": hashlib.sha256(
                        json.dumps(
                            summary,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode("utf-8")
                    ).hexdigest(),
                }
            },
        )
        evidence = coordinator.capture_cognitive_evidence(
            mission_id,
            claim.task["id"],
            result,
        )
        return coordinator.record_step(
            mission_id,
            claim.task["id"],
            result,
            inspector_evidence=evidence,
        )
    if policy == "observation":
        result = ToolRunResponse(
            tool="runtime.status",
            ok=True,
            summary=f"Observed {spec['title']}: {spec['objective']}.",
            data={"observation": spec["objective"], "status": "verified"},
        )
        evidence = coordinator.capture_inspector_evidence(
            mission_id,
            claim.task["id"],
            result,
            action_arguments={},
            read_only=True,
        )
        return coordinator.record_step(
            mission_id,
            claim.task["id"],
            result,
            inspector_evidence=evidence,
        )
    slug = re.sub(r"[^a-z0-9]+", "-", spec["title"].casefold()).strip("-")
    result = _success(claim.step_id)
    arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": claim.step_id,
                "kind": "fs.write",
                "path": f"C:/{slug}.txt",
                "content_base64": "",
            },
        }
    }
    coordinator.bind_action_contract(
        mission_id,
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments,
    )
    evidence = coordinator.capture_inspector_evidence(
        mission_id,
        claim.task["id"],
        result,
        action_arguments=arguments,
    )
    return coordinator.record_step(
        mission_id,
        claim.task["id"],
        result,
        inspector_evidence=evidence,
    )


def _create_bound_approval(
    storage: JarvisStorage,
    coordinator: ExecutiveCoordinator,
    mission_id: str,
    claim,
) -> dict:
    claimed_step = next(
        item for item in claim.planner["steps"] if item["spec"]["step_id"] == claim.step_id
    )
    slug = re.sub(
        r"[^a-z0-9]+",
        "-",
        claimed_step["spec"]["title"].casefold(),
    ).strip("-")
    arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": f"approval-{claim.step_id}",
                "kind": "fs.write",
                "path": f"C:/{slug}.txt",
                "content_base64": "",
            },
        }
    }
    coordinator.bind_action_contract(
        mission_id,
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments,
    )
    planner = coordinator.snapshot(mission_id)["planner"]
    step = next(item for item in planner["steps"] if item["spec"]["step_id"] == claim.step_id)
    return storage.create_approval(
        title="Bound executive approval",
        description="Preserve only a resumable, environment-bound approval.",
        requested_action="tool.run",
        payload={
            "mission_id": mission_id,
            "task_id": claim.task["id"],
            "tool": "execution.apply",
            "arguments": arguments,
            "executive_claim": {
                "protocol": "jarvis.executive-approval.v1",
                "mission_id": mission_id,
                "task_id": claim.task["id"],
                "step_id": claim.step_id,
                "plan_revision": planner["revision"],
                "step_attempt": step["attempts"],
                "environment_digest": planner["environment"]["digest"],
                "verification_contract": step["verification_contract"],
            },
        },
    )


def test_coordinator_executes_only_ready_steps_and_goal_assertions(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    created = coordinator.create_for_mission(mission)

    assert created["protocol"] == "jarvis.executive.v1"
    assert created["planner"]["status"] == "ready"
    assert created["planner"]["ready_step_ids"] == ["step.001"]

    executed = 0
    while coordinator.snapshot(mission["id"])["planner"]["status"] != "succeeded":
        claim = coordinator.claim_ready_task(mission["id"])
        assert claim is not None
        outcome = _record_success(coordinator, mission["id"], claim)
        assert outcome.verified is True
        storage.update_mission_task(claim.task["id"], mission_id=mission["id"], status="done")
        executed += 1

    snapshot = coordinator.snapshot(mission["id"])
    assert snapshot["planner"]["goal_assertion_results"][0]["passed"] is True
    assert executed == len(mission["tasks"])
    assert storage.get_mission(mission["id"])["status"] == "done"
    storage.close()


def test_snapshot_does_not_reclassify_live_step_as_crash(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)

    claim = coordinator.claim_ready_task(mission["id"])
    running = coordinator.snapshot(mission["id"])["planner"]
    outcome = _record_success(coordinator, mission["id"], claim)

    step = next(
        item for item in outcome.planner["steps"] if item["spec"]["step_id"] == claim.step_id
    )
    assert running["steps"][0]["status"] == "running"
    assert running["steps"][0]["attempts"] == 1
    assert step["status"] == "succeeded"
    assert step["attempts"] == 1
    storage.close()


def test_cold_start_routes_interrupted_running_task_to_reconciliation(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    first = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    first.create_for_mission(mission)
    interrupted = first.claim_ready_task(mission["id"])

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )
    recovered_task = next(
        item
        for item in storage.list_mission_tasks(mission["id"])
        if item["id"] == interrupted.task["id"]
    )
    recovered_plan = resumed.snapshot(mission["id"])["planner"]

    assert recovered_task["status"] == "skipped"
    assert recovered_plan["revision"] == 1
    assert interrupted.step_id not in {item["spec"]["step_id"] for item in recovered_plan["steps"]}
    recovery = next(
        item
        for item in recovered_plan["steps"]
        if item["spec"]["action"]["arguments"].get("kind") == "reconciliation"
    )
    assert recovery["spec"]["action"]["tool"] == "mission.execute_step"
    assert recovery["spec"]["action"]["arguments"]["replay_original_action"] is False
    next_claim = resumed.claim_ready_task(mission["id"])
    assert next_claim.task["id"] != interrupted.task["id"]
    storage.close()


def test_reconcile_only_state_step_closes_by_exact_inspection_without_replay(tmp_path):
    storage = _storage(tmp_path)
    target = "C:/reconciled-state.json"
    goal = f"Write {target}"
    mission = storage.create_mission(
        title="Ambiguous committed write",
        goal=goal,
        tasks=[goal],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "ambiguous-write",
                "kind": "fs.write",
                "path": target,
                "content_base64": "",
            },
        }
    }
    coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments,
    )
    failed = coordinator.record_step(
        mission["id"],
        claim.task["id"],
        ToolRunResponse(
            tool="mission.resume_after_approval",
            ok=False,
            summary="[reconcile-only] approved write outcome is ambiguous",
        ),
    )
    assert failed.adapted is True

    diagnosis = coordinator.claim_ready_task(mission["id"])
    diagnosis_outcome = _record_success(coordinator, mission["id"], diagnosis)
    assert diagnosis_outcome.verified is True
    storage.update_mission_task(
        diagnosis.task["id"], mission_id=mission["id"], status="done"
    )
    recovery = coordinator.claim_ready_task(mission["id"])
    recovery_step = next(
        item
        for item in recovery.planner["steps"]
        if item["spec"]["step_id"] == recovery.step_id
    )
    verify_arguments = recovery_step["spec"]["action"]["arguments"]
    assert recovery_step["spec"]["action"]["tool"] == "execution.verify"
    assert recovery_step["spec"]["evidence_policy"] == "state"
    with pytest.raises(ValueError, match="cannot execute a mutation"):
        coordinator.bind_action_contract(
            mission["id"],
            recovery.task["id"],
            tool="execution.apply",
            arguments=arguments,
        )
    generic = ToolRunResponse(
        tool="runtime.status",
        ok=True,
        summary="Generic status cannot establish the desired write.",
        data={"status": "ok", "target": target},
    )
    with pytest.raises(ValueError, match="typed state-verifier"):
        coordinator.capture_inspector_evidence(
            mission["id"],
            recovery.task["id"],
            generic,
            action_arguments={},
            read_only=True,
        )

    verified = ToolRunResponse(
        tool="execution.verify",
        ok=True,
        summary="Exact postcondition is satisfied without replay.",
        data={
            "source_tool": "execution.apply",
            "verification": {
                "ok": True,
                "status": "passed",
                "action_id": "ambiguous-write",
                "action_kind": "WriteFileAction",
                "summary": "Current file content matches the original action.",
                "evidence": [
                    {
                        "source": "filesystem",
                        "assertion": "file sha256 matches",
                        "expected": True,
                        "observed": True,
                        "passed": True,
                        "captured_at": "2026-07-11T00:00:00+00:00",
                        "error": None,
                        "subject": target,
                    }
                ],
                "error": None,
            },
        },
    )
    evidence = coordinator.capture_inspector_evidence(
        mission["id"],
        recovery.task["id"],
        verified,
        action_arguments=verify_arguments,
        read_only=True,
    )
    outcome = coordinator.record_step(
        mission["id"],
        recovery.task["id"],
        verified,
        inspector_evidence=evidence,
    )
    assert outcome.verified is True
    assert outcome.planner["status"] == "succeeded"
    storage.close()


def test_secondary_coordinator_does_not_recover_live_primary_work(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    primary = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    primary.create_for_mission(mission)
    claimed = primary.claim_ready_task(mission["id"])

    secondary = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    task = next(
        item
        for item in storage.list_mission_tasks(mission["id"])
        if item["id"] == claimed.task["id"]
    )
    step = next(
        item
        for item in secondary.snapshot(mission["id"])["planner"]["steps"]
        if item["spec"]["step_id"] == claimed.step_id
    )

    assert task["status"] == "running"
    assert step["status"] == "running"
    storage.close()


def test_cold_start_releases_task_claim_persisted_before_planner_start(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    task = mission["tasks"][0]
    assert storage.claim_mission_task(mission["id"], task["id"]) is not None

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )
    reconciled = next(
        item for item in storage.list_mission_tasks(mission["id"]) if item["id"] == task["id"]
    )
    snapshot = resumed.snapshot(mission["id"])["planner"]

    assert reconciled["status"] == "pending"
    assert snapshot["steps"][0]["status"] == "pending"
    assert resumed.claim_ready_task(mission["id"]).task["id"] == task["id"]
    storage.close()


def test_cold_start_completes_task_when_verified_planner_write_won(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    _record_success(coordinator, mission["id"], claim)

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )
    reconciled = next(
        item for item in storage.list_mission_tasks(mission["id"]) if item["id"] == claim.task["id"]
    )

    assert reconciled["status"] == "done"
    assert resumed.snapshot(mission["id"])["planner"]["steps"][0]["status"] == "succeeded"
    assert resumed.claim_ready_task(mission["id"]) is not None
    storage.close()


def test_cold_start_preserves_approval_blocked_inflight_step(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    first = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    first.create_for_mission(mission)
    blocked = first.claim_ready_task(mission["id"])
    storage.update_mission_task(
        blocked.task["id"],
        mission_id=mission["id"],
        status="blocked",
        notes="Waiting for approval.",
    )
    _create_bound_approval(storage, first, mission["id"], blocked)

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )
    snapshot = resumed.snapshot(mission["id"])["planner"]
    outcome = _record_success(resumed, mission["id"], blocked)

    assert snapshot["steps"][0]["status"] == "running"
    assert outcome.verified is True
    assert outcome.planner["steps"][0]["attempts"] == 1
    storage.close()


def test_cold_start_finishes_running_to_blocked_approval_crash_window(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    first = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    first.create_for_mission(mission)
    claimed = first.claim_ready_task(mission["id"])
    _create_bound_approval(storage, first, mission["id"], claimed)

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )
    task = next(
        item
        for item in storage.list_mission_tasks(mission["id"])
        if item["id"] == claimed.task["id"]
    )
    step = next(
        item
        for item in resumed.snapshot(mission["id"])["planner"]["steps"]
        if item["spec"]["step_id"] == claimed.step_id
    )

    assert task["status"] == "blocked"
    assert step["status"] == "running"
    assert step["attempts"] == 1
    storage.close()


def test_cold_start_rejects_approval_missing_bound_action_contract(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    first = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    first.create_for_mission(mission)
    claimed = first.claim_ready_task(mission["id"])
    first.bind_action_contract(
        mission["id"],
        claimed.task["id"],
        tool="execution.apply",
        arguments={
            "payload": {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "action_id": "bound-action",
                    "kind": "fs.write",
                    "path": "C:/implement-adaptive-runtime-foundation.txt",
                    "content_base64": "",
                },
            }
        },
    )
    storage.update_mission_task(claimed.task["id"], mission_id=mission["id"], status="blocked")
    planner = first.snapshot(mission["id"])["planner"]
    step = next(item for item in planner["steps"] if item["spec"]["step_id"] == claimed.step_id)
    stale = storage.create_approval(
        title="Approval missing contract",
        description="Must not survive cold-start validation.",
        requested_action="tool.run",
        payload={
            "mission_id": mission["id"],
            "task_id": claimed.task["id"],
            "tool": "execution.apply",
            "arguments": {},
            "executive_claim": {
                "protocol": "jarvis.executive-approval.v1",
                "mission_id": mission["id"],
                "task_id": claimed.task["id"],
                "step_id": claimed.step_id,
                "plan_revision": planner["revision"],
                "step_attempt": step["attempts"],
                "environment_digest": planner["environment"]["digest"],
            },
        },
    )

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )
    original = next(
        item
        for item in storage.list_mission_tasks(mission["id"])
        if item["id"] == claimed.task["id"]
    )

    assert original["status"] == "skipped"
    assert resumed.snapshot(mission["id"])["planner"]["revision"] == 1
    assert storage.get_approval(stale["id"])["status"] == "cancelled"
    storage.close()


def test_cold_start_recovers_running_sibling_without_invalidating_blocked_approval(
    tmp_path,
):
    storage = _storage(tmp_path)
    mission = storage.create_mission(
        title="Parallel approval mission",
        goal="Execute parallel branches safely",
        tasks=[f"Execute parallel branch {index} safely" for index in range(1, 9)],
    )
    first = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    first.create_for_mission(mission)
    for _ in range(3):
        claim = first.claim_ready_task(mission["id"])
        _record_success(first, mission["id"], claim)
        storage.update_mission_task(claim.task["id"], mission_id=mission["id"], status="done")
    blocked = first.claim_ready_task(mission["id"])
    storage.update_mission_task(blocked.task["id"], mission_id=mission["id"], status="blocked")
    _create_bound_approval(storage, first, mission["id"], blocked)
    interrupted = first.claim_ready_task(mission["id"])

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )
    snapshot = resumed.snapshot(mission["id"])["planner"]
    states = {item["spec"]["step_id"]: item["status"] for item in snapshot["steps"]}

    assert states[blocked.step_id] == "running"
    assert interrupted.step_id not in states
    assert any(step_id.startswith("diagnose.r") for step_id in states)
    assert any(step_id.startswith("recover.r") for step_id in states)
    assert (
        next(
            item
            for item in storage.list_mission_tasks(mission["id"])
            if item["id"] == blocked.task["id"]
        )["status"]
        == "blocked"
    )
    storage.close()


def test_cold_start_never_replays_ambiguous_committed_safe_mutation(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    first = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    first.create_for_mission(mission)
    interrupted = first.claim_ready_task(mission["id"])
    marker = tmp_path / "committed-once.txt"
    marker.write_text("committed-once", encoding="utf-8")

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )
    original = next(
        item
        for item in storage.list_mission_tasks(mission["id"])
        if item["id"] == interrupted.task["id"]
    )
    next_claim = resumed.claim_ready_task(mission["id"])

    assert marker.read_text(encoding="utf-8") == "committed-once"
    assert original["status"] == "skipped"
    assert next_claim.task["id"] != interrupted.task["id"]
    assert "Diagnose unexpected result" in next_claim.task["title"]
    storage.close()


def test_failed_step_revises_remaining_graph_without_resetting_success(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)

    first = coordinator.claim_ready_task(mission["id"])
    _record_success(coordinator, mission["id"], first)
    storage.update_mission_task(first.task["id"], mission_id=mission["id"], status="done")
    second = coordinator.claim_ready_task(mission["id"])
    failure = ToolRunResponse(
        tool="mission.execute_next",
        ok=False,
        summary="Environment changed unexpectedly.",
        data={},
    )

    outcome = coordinator.record_step(mission["id"], second.task["id"], failure)

    assert outcome.adapted is True
    assert len(outcome.added_task_ids) == 2
    snapshot = coordinator.snapshot(mission["id"])["planner"]
    assert snapshot["revision"] == 1
    assert (
        next(item for item in snapshot["steps"] if item["spec"]["step_id"] == "step.001")["status"]
        == "succeeded"
    )
    assert all(item["spec"]["step_id"] != "step.002" for item in snapshot["steps"])
    old_task = next(
        item
        for item in storage.list_mission_tasks(mission["id"])
        if item["id"] == second.task["id"]
    )
    assert old_task["status"] == "skipped"
    assert coordinator.claim_ready_task(mission["id"]).step_id == "diagnose.r1"
    storage.close()


def test_success_log_with_failed_verification_revises_fail_closed(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])

    outcome = coordinator.record_step(
        mission["id"],
        claim.task["id"],
        ToolRunResponse(
            tool="mission.execute_next",
            ok=True,
            summary="The model claimed success.",
            data={"verification": {"ok": False, "verdict": "revise"}},
        ),
    )

    assert outcome.verified is False
    assert outcome.adapted is True
    assert outcome.planner["revision"] == 1
    assert all(item["spec"]["step_id"] != claim.step_id for item in outcome.planner["steps"])
    storage.close()


def test_model_supplied_positive_verification_is_not_trusted(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])

    outcome = coordinator.record_step(
        mission["id"],
        claim.task["id"],
        ToolRunResponse(
            tool="mission.execute_next",
            ok=True,
            summary="The model asserted that verification passed.",
            data={"verification": {"ok": True, "status": "passed"}},
        ),
    )

    assert outcome.verified is False
    assert outcome.adapted is True
    storage.close()


def test_nested_inspector_shaped_payload_is_not_trusted(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    nested = _success(claim.step_id).model_dump()

    outcome = coordinator.record_step(
        mission["id"],
        claim.task["id"],
        ToolRunResponse(
            tool="mission.resume_after_approval",
            ok=True,
            summary="Nested approved result claimed success.",
            data={"approved_tool": nested},
        ),
    )

    assert outcome.verified is False
    assert outcome.adapted is True
    storage.close()


def test_inspector_evidence_is_bound_to_requested_action(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    result = _success("observed-action")

    arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "different-action",
                "kind": "fs.write",
                "path": "C:/implement-adaptive-runtime-foundation.txt",
                "content_base64": "",
            },
        }
    }
    coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments,
    )
    with pytest.raises(ValueError, match="requested action"):
        coordinator.capture_inspector_evidence(
            mission["id"],
            claim.task["id"],
            result,
            action_arguments=arguments,
        )
    storage.close()


def test_verified_unrelated_action_cannot_satisfy_bound_step_contract(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    expected_arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "expected-action",
                "kind": "fs.write",
                "path": "C:/implement-adaptive-runtime-foundation.txt",
                "content_base64": "",
            },
        }
    }
    unrelated_arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "unrelated-action",
                "kind": "fs.write",
                "path": "C:/unrelated-contract-test.txt",
                "content_base64": "",
            },
        }
    }
    coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=expected_arguments,
    )

    with pytest.raises(ValueError, match="bound step postcondition"):
        coordinator.capture_inspector_evidence(
            mission["id"],
            claim.task["id"],
            _success("unrelated-action"),
            action_arguments=unrelated_arguments,
        )
    storage.close()


def test_state_action_subject_must_match_planned_literal_target(tmp_path):
    storage = _storage(tmp_path)
    expected = "C:/planned-a.txt"
    mission = storage.create_mission(
        title="Literal action target",
        goal=f"Write exact file {expected}",
        tasks=[f"Write exact file {expected}"],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])

    def arguments(path: str) -> dict:
        return {
            "payload": {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "action_id": "literal-write",
                    "kind": "fs.write",
                    "path": path,
                    "content_base64": "",
                },
            }
        }

    with pytest.raises(ValueError, match="action subject"):
        coordinator.bind_action_contract(
            mission["id"],
            claim.task["id"],
            tool="execution.apply",
            arguments=arguments("C:/planned-b.txt"),
        )
    with pytest.raises(ValueError, match="action subject"):
        coordinator.bind_action_contract(
            mission["id"],
            claim.task["id"],
            tool="execution.apply",
            arguments=arguments("C:/planned-a.txt.bak"),
        )

    contract = coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments(expected),
    )

    assert contract["action_id"] == "literal-write"
    storage.close()


def test_explicit_delete_intent_rejects_opposite_write_kind(tmp_path):
    storage = _storage(tmp_path)
    target = "C:/planned-delete.txt"
    mission = storage.create_mission(
        title="Typed delete intent",
        goal=f"Delete {target}",
        tasks=[f"Delete {target}"],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])

    def arguments(kind: str) -> dict:
        action = {
            "action_id": f"typed-{kind}",
            "kind": kind,
            "path": target,
        }
        if kind == "fs.write":
            action["content_base64"] = ""
        return {
            "payload": {
                "protocol": "jarvis.execution.v1",
                "action": action,
            }
        }

    with pytest.raises(ValueError, match="action subject"):
        coordinator.bind_action_contract(
            mission["id"],
            claim.task["id"],
            tool="execution.apply",
            arguments=arguments("fs.write"),
        )

    contract = coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments("fs.delete"),
    )

    assert contract["action_kind"] == "DeleteFileAction"
    storage.close()


def test_ambiguous_remove_entry_goal_keeps_content_edit_compatible(tmp_path):
    storage = _storage(tmp_path)
    target = "C:/settings.json"
    mission = storage.create_mission(
        title="Edit one configuration entry",
        goal=f"Remove obsolete entry from {target}",
        tasks=[f"Remove obsolete entry from {target}"],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "edit-settings-content",
                "kind": "fs.write",
                "path": target,
                "content_base64": "",
            },
        }
    }

    contract = coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments,
    )

    assert contract["action_kind"] == "WriteFileAction"
    storage.close()


def test_explicit_move_intent_rejects_copy_with_same_subject_roles(tmp_path):
    storage = _storage(tmp_path)
    source = "C:/planned-source.txt"
    destination = "C:/planned-destination.txt"
    mission = storage.create_mission(
        title="Typed move intent",
        goal=f"Move {source} to {destination}",
        tasks=[f"Move {source} to {destination}"],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])

    def arguments(kind: str) -> dict:
        return {
            "payload": {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "action_id": f"typed-{kind}",
                    "kind": kind,
                    "source": source,
                    "destination": destination,
                    **({"expected_sha256": "0" * 64} if kind == "fs.move" else {}),
                },
            }
        }

    with pytest.raises(ValueError, match="action subject"):
        coordinator.bind_action_contract(
            mission["id"],
            claim.task["id"],
            tool="execution.apply",
            arguments=arguments("fs.copy"),
        )

    contract = coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments("fs.move"),
    )

    assert contract["action_kind"] == "MoveFileAction"
    storage.close()


@pytest.mark.parametrize(
    ("goal", "target"),
    [
        ("Create README.md", "C:/repo/README.md"),
        ("Create PDF report", "C:/reports/report.pdf"),
        ("Update C:/config.json", "C:/config.json"),
        (
            "Write C:/delete/copy/move/start/script.py",
            "C:/delete/copy/move/start/script.py",
        ),
    ],
)
def test_explicit_file_output_intent_rejects_delete_and_path_verb_injection(
    tmp_path,
    goal,
    target,
):
    storage = _storage(tmp_path)
    mission = storage.create_mission(
        title="Typed file output intent",
        goal=goal,
        tasks=[goal],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])

    def arguments(kind: str) -> dict:
        action = {"action_id": f"intent-{kind}", "kind": kind, "path": target}
        if kind == "fs.write":
            action["content_base64"] = ""
        return {"payload": {"protocol": "jarvis.execution.v1", "action": action}}

    with pytest.raises(ValueError, match="action subject"):
        coordinator.bind_action_contract(
            mission["id"],
            claim.task["id"],
            tool="execution.apply",
            arguments=arguments("fs.delete"),
        )
    contract = coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments("fs.write"),
    )
    assert contract["action_kind"] == "WriteFileAction"
    storage.close()


def test_transaction_binds_all_declared_targets_and_verification_order(tmp_path):
    storage = _storage(tmp_path)
    first = "C:/atomic-a.json"
    second = "C:/atomic-b.json"
    mission = storage.create_mission(
        title="Atomic configuration update",
        goal=f"Write {first} and {second} atomically",
        tasks=[f"Write {first} and {second} atomically"],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])

    def action(action_id: str, path: str) -> dict:
        return {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": action_id,
                "kind": "fs.write",
                "path": path,
                "content_base64": "",
            },
        }

    arguments = {
        "actions": [action("atomic-a", first), action("atomic-b", second)],
        "idempotency_key": "executive.atomic.config",
    }
    unrelated = {
        **arguments,
        "actions": [
            *arguments["actions"],
            action("unrelated", "C:/unrelated.json"),
        ],
    }
    with pytest.raises(ValueError, match="action subject"):
        coordinator.bind_action_contract(
            mission["id"],
            claim.task["id"],
            tool="execution.transaction",
            arguments=unrelated,
        )

    contract = coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.transaction",
        arguments=arguments,
    )
    assert contract["action_id"] == "transaction:executive.atomic.config"

    def verified(action_id: str, subject: str) -> dict:
        return {
            "ok": True,
            "status": "passed",
            "action_id": action_id,
            "action_kind": "WriteFileAction",
            "summary": "Independent filesystem inspection passed.",
            "evidence": [
                {
                    "source": "test.filesystem",
                    "assertion": "file state matches the exact transaction action",
                    "expected": True,
                    "observed": True,
                    "passed": True,
                    "captured_at": "2026-07-11T00:00:00+00:00",
                    "error": None,
                    "subject": subject,
                }
            ],
            "error": None,
        }

    swapped = ToolRunResponse(
        tool="execution.transaction",
        ok=True,
        summary="Transaction committed.",
        data={
            "verification": [
                verified("atomic-b", second),
                verified("atomic-a", first),
            ]
        },
    )
    with pytest.raises(ValueError, match="typed state-verifier"):
        coordinator.capture_inspector_evidence(
            mission["id"],
            claim.task["id"],
            swapped,
            action_arguments=arguments,
        )

    result = ToolRunResponse(
        tool="execution.transaction",
        ok=True,
        summary="Transaction committed.",
        data={
            "verification": [
                verified("atomic-a", first),
                verified("atomic-b", second),
            ]
        },
    )
    evidence = coordinator.capture_inspector_evidence(
        mission["id"],
        claim.task["id"],
        result,
        action_arguments=arguments,
    )
    outcome = coordinator.record_step(
        mission["id"],
        claim.task["id"],
        result,
        inspector_evidence=evidence,
    )
    assert outcome.verified is True
    storage.close()


def test_mixed_effect_transaction_cannot_swap_operations_between_targets(tmp_path):
    storage = _storage(tmp_path)
    deleted = "C:/obsolete.json"
    written = "C:/current.json"
    goal = f"Delete {deleted} and write {written}"
    mission = storage.create_mission(
        title="Mixed atomic effects",
        goal=goal,
        tasks=[goal],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    arguments = {
        "actions": [
            {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "action_id": "wrong-write",
                    "kind": "fs.write",
                    "path": deleted,
                    "content_base64": "",
                },
            },
            {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "action_id": "wrong-delete",
                    "kind": "fs.delete",
                    "path": written,
                },
            },
        ],
        "idempotency_key": "mixed.effects.swapped",
    }

    with pytest.raises(ValueError, match="action subject"):
        coordinator.bind_action_contract(
            mission["id"],
            claim.task["id"],
            tool="execution.transaction",
            arguments=arguments,
        )
    storage.close()


@pytest.mark.parametrize("tool", ["execution.apply", "execution.transaction"])
def test_executive_mutation_contract_requires_explicit_stable_action_id(tmp_path, tool):
    storage = _storage(tmp_path)
    target = "C:/stable-action-id.txt"
    mission = storage.create_mission(
        title="Stable mutation identity",
        goal=f"Write {target}",
        tasks=[f"Write {target}"],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "path": target,
            "content_base64": "",
        },
    }
    arguments = (
        {"payload": payload}
        if tool == "execution.apply"
        else {"actions": [payload], "idempotency_key": "stable.identity"}
    )

    with pytest.raises(ValueError, match="action subject"):
        coordinator.bind_action_contract(
            mission["id"],
            claim.task["id"],
            tool=tool,
            arguments=arguments,
        )
    storage.close()


def test_process_command_can_bind_exact_planned_executable_path(tmp_path):
    storage = _storage(tmp_path)
    executable = "C:/tools/runtime.py"
    mission = storage.create_mission(
        title="Exact process executable",
        goal=f"Execute {executable}",
        tasks=[f"Execute {executable}"],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "run-exact-script",
                "kind": "process.run",
                "executable": executable,
            },
        },
        "verification": {
            "paths": [
                {
                    "path": executable,
                    "exists": True,
                    "kind": "file",
                }
            ]
        },
    }

    contract = coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments,
    )

    assert contract["action_id"] == "run-exact-script"
    storage.close()


def test_process_command_binds_quoted_windows_path_with_spaces(tmp_path):
    storage = _storage(tmp_path)
    executable = "C:/Program Files/Jarvis Tools/check-runtime.py"
    goal = f'Execute "{executable}"'
    mission = storage.create_mission(
        title="Quoted executable path",
        goal=goal,
        tasks=[goal],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "run-quoted-script",
                "kind": "process.run",
                "executable": executable,
            },
        },
        "verification": {
            "paths": [{"path": executable, "exists": True, "kind": "file"}]
        },
    }

    contract = coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments,
    )
    assert contract["action_id"] == "run-quoted-script"
    storage.close()


def test_process_run_requires_typed_tcp_postcondition_for_planned_port(tmp_path):
    storage = _storage(tmp_path)
    goal = "Deploy Jarvis service on TCP port 3000"
    mission = storage.create_mission(
        title="Port-bound deployment",
        goal=goal,
        tasks=[goal],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "action_id": "run-jarvis-3000",
            "kind": "process.run",
            "executable": "C:/tools/jarvis-service.exe",
            "arguments": ["jarvis", "service", "--port", "3000"],
            "session_id": "jarvis-service-3000",
        },
    }

    with pytest.raises(ValueError, match="process postcondition"):
        coordinator.bind_action_contract(
            mission["id"],
            claim.task["id"],
            tool="execution.apply",
            arguments={
                "payload": payload,
                "verification": {
                    "tcp": [{"host": "127.0.0.1", "port": 13000, "reachable": True}]
                },
            },
        )
    contract = coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments={
            "payload": payload,
            "verification": {
                "tcp": [{"host": "127.0.0.1", "port": 3000, "reachable": True}]
            },
        },
    )
    assert contract["action_id"] == "run-jarvis-3000"
    storage.close()


def test_process_termination_requires_exact_owned_pid_postcondition(tmp_path):
    storage = _storage(tmp_path)
    goal = "Stop process PID 1234"
    mission = storage.create_mission(
        title="PID-bound termination",
        goal=goal,
        tasks=[goal],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "action_id": "terminate-owned-1234",
            "kind": "process.terminate",
            "session_id": "owned-session",
            "pid": 1234,
        },
    }
    with pytest.raises(ValueError, match="process postcondition"):
        coordinator.bind_action_contract(
            mission["id"],
            claim.task["id"],
            tool="execution.apply",
            arguments={
                "payload": payload,
                "verification": {
                    "processes": [
                        {"session_id": "owned-session", "pid": 4321, "running": False}
                    ]
                },
            },
        )
    contract = coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments={
            "payload": payload,
            "verification": {
                "processes": [
                    {"session_id": "owned-session", "pid": 1234, "running": False}
                ]
            },
        },
    )
    assert contract["action_id"] == "terminate-owned-1234"
    storage.close()


def test_subject_identity_preserves_posix_and_url_case_but_folds_windows():
    assert not _anchor_matches_subject(
        "/srv/Jarvis/config.json",
        "/srv/jarvis/config.json",
    )
    assert not _anchor_matches_subject(
        "https://example.com/Jarvis/config.json",
        "https://EXAMPLE.com/jarvis/config.json",
    )
    assert _anchor_matches_subject(
        "C:/Jarvis/Config.json",
        "c:\\jarvis\\config.JSON",
    )
    assert _anchor_matches_subject(
        "\\\\SERVER\\Share\\Jarvis\\Config.json",
        "\\\\server\\share\\jarvis\\config.JSON",
    )


def test_state_action_can_bind_to_trusted_predecessor_discovery(tmp_path):
    storage = _storage(tmp_path)
    mission = storage.create_mission(
        title="Dependency update",
        goal="Update project dependencies",
        tasks=[
            "Inspect project dependency manifest",
            "Update project dependencies",
        ],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    inspection = coordinator.claim_ready_task(mission["id"])
    discovered = "C:/repo/pyproject.toml"
    result = ToolRunResponse(
        tool="files.search",
        ok=True,
        summary="Project dependency manifest was discovered.",
        data={"items": [{"path": discovered, "kind": "file"}]},
    )
    evidence = coordinator.capture_inspector_evidence(
        mission["id"],
        inspection.task["id"],
        result,
        action_arguments={"query": "dependency manifest"},
        read_only=True,
    )
    outcome = coordinator.record_step(
        mission["id"],
        inspection.task["id"],
        result,
        inspector_evidence=evidence,
    )
    assert outcome.verified is True
    storage.update_mission_task(
        inspection.task["id"],
        mission_id=mission["id"],
        status="done",
    )
    state_claim = coordinator.claim_ready_task(mission["id"])
    arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "update-discovered-dependencies",
                "kind": "fs.write",
                "path": discovered,
                "content_base64": "",
            },
        }
    }

    with pytest.raises(ValueError, match="action subject"):
        coordinator.bind_action_contract(
            mission["id"],
            state_claim.task["id"],
            tool="execution.apply",
            arguments={
                "payload": {
                    "protocol": "jarvis.execution.v1",
                    "action": {
                        "action_id": "delete-discovered-dependencies",
                        "kind": "fs.delete",
                        "path": discovered,
                    },
                }
            },
        )

    contract = coordinator.bind_action_contract(
        mission["id"],
        state_claim.task["id"],
        tool="execution.apply",
        arguments=arguments,
    )

    assert contract["action_id"] == "update-discovered-dependencies"
    storage.close()


def test_inspection_supplemental_subject_cannot_expand_mutation_authority(tmp_path):
    storage = _storage(tmp_path)
    inspected = "C:/repo/goal-config.json"
    supplemental = "C:/repo/unrelated-secrets.json"
    mission = storage.create_mission(
        title="Scoped dependency update",
        goal=f"Inspect {inspected} and update project dependencies",
        tasks=[
            f"Inspect {inspected}",
            "Update project dependencies",
        ],
    )
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    inspection = coordinator.claim_ready_task(mission["id"])
    inspect_arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "action_id": "inspect-goal-config",
                "kind": "fs.stat",
                "path": inspected,
            },
        }
    }
    result = ToolRunResponse(
        tool="execution.inspect",
        ok=True,
        summary=f"Inspected project dependency configuration at {inspected}.",
        data={
            "result": {"action_class": "read_only"},
            "verification": {
                "ok": True,
                "status": "passed",
                "action_id": "inspect-goal-config",
                "action_kind": "StatPathAction",
                "summary": "Primary and supplemental path checks passed.",
                "evidence": [
                    {
                        "source": "filesystem",
                        "assertion": "primary path exists",
                        "expected": True,
                        "observed": True,
                        "passed": True,
                        "captured_at": "2026-07-11T00:00:00+00:00",
                        "error": None,
                        "subject": inspected,
                    },
                    {
                        "source": "filesystem",
                        "assertion": "supplemental path exists",
                        "expected": True,
                        "observed": True,
                        "passed": True,
                        "captured_at": "2026-07-11T00:00:00+00:00",
                        "error": None,
                        "subject": supplemental,
                    },
                ],
                "error": None,
            },
        },
    )
    evidence = coordinator.capture_inspector_evidence(
        mission["id"],
        inspection.task["id"],
        result,
        action_arguments=inspect_arguments,
        read_only=True,
    )
    outcome = coordinator.record_step(
        mission["id"],
        inspection.task["id"],
        result,
        inspector_evidence=evidence,
    )
    assert outcome.verified is True
    storage.update_mission_task(
        inspection.task["id"],
        mission_id=mission["id"],
        status="done",
    )
    state_claim = coordinator.claim_ready_task(mission["id"])

    def write_arguments(action_id: str, path: str) -> dict:
        return {
            "payload": {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "action_id": action_id,
                    "kind": "fs.write",
                    "path": path,
                    "content_base64": "",
                },
            }
        }

    with pytest.raises(ValueError, match="action subject"):
        coordinator.bind_action_contract(
            mission["id"],
            state_claim.task["id"],
            tool="execution.apply",
            arguments=write_arguments("write-supplemental", supplemental),
        )

    contract = coordinator.bind_action_contract(
        mission["id"],
        state_claim.task["id"],
        tool="execution.apply",
        arguments=write_arguments("write-inspected", inspected),
    )
    assert contract["action_id"] == "write-inspected"
    storage.close()


def test_minted_evidence_is_rejected_after_contract_changes(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])

    def arguments(action_id: str, path: str) -> dict:
        return {
            "payload": {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "action_id": action_id,
                    "kind": "fs.write",
                    "path": path,
                    "content_base64": "",
                },
            }
        }

    first_arguments = arguments("first-action", "C:/implement-adaptive-runtime-foundation-v1.txt")
    coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=first_arguments,
    )
    first_result = _success("first-action")
    evidence = coordinator.capture_inspector_evidence(
        mission["id"],
        claim.task["id"],
        first_result,
        action_arguments=first_arguments,
    )
    coordinator.bind_action_contract(
        mission["id"],
        claim.task["id"],
        tool="execution.apply",
        arguments=arguments("second-action", "C:/implement-adaptive-runtime-foundation-v2.txt"),
    )

    outcome = coordinator.record_step(
        mission["id"],
        claim.task["id"],
        first_result,
        inspector_evidence=evidence,
    )

    assert outcome.verified is False
    assert outcome.adapted is True
    storage.close()


def test_cold_start_quarantines_tasks_from_uncommitted_dag_revision(
    tmp_path,
    monkeypatch,
):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(mission)
    claim = coordinator.claim_ready_task(mission["id"])
    original_task_ids = {item["id"] for item in mission["tasks"]}

    def crash_before_plan_persist(_mission_id, _record):
        raise RuntimeError("simulated power loss before revised DAG commit")

    monkeypatch.setattr(coordinator, "_persist", crash_before_plan_persist)
    with pytest.raises(RuntimeError, match="simulated power loss"):
        coordinator.record_step(
            mission["id"],
            claim.task["id"],
            ToolRunResponse(
                tool="mission.execute_next",
                ok=False,
                summary="Unexpected state requires adaptation.",
                data={},
            ),
        )

    orphan_ids = {
        item["id"]
        for item in storage.list_mission_tasks(mission["id"])
        if item["id"] not in original_task_ids
    }
    assert len(orphan_ids) == 2
    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )
    recovered = {item["id"]: item for item in storage.list_mission_tasks(mission["id"])}
    durable_map = set(resumed.snapshot(mission["id"])["task_map"].values())

    assert all(recovered[task_id]["status"] == "skipped" for task_id in orphan_ids)
    assert orphan_ids.isdisjoint(durable_map)
    storage.close()


def test_cold_start_isolates_malformed_plan_and_recovers_other_missions(tmp_path):
    storage = _storage(tmp_path)
    malformed_mission = _mission(storage)
    healthy_mission = _mission(storage)
    coordinator = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    coordinator.create_for_mission(malformed_mission)
    coordinator.create_for_mission(healthy_mission)
    storage.set_runtime_value(
        f"executive.plan.{malformed_mission['id']}",
        {
            "protocol": "jarvis.executive.v1",
            "mission_id": malformed_mission["id"],
            "planner": {"protocol": "jarvis.planner.v1"},
            "task_map": {},
        },
    )

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )

    damaged = storage.get_mission(malformed_mission["id"])
    assert damaged is not None
    assert all(task["status"] == "blocked" for task in damaged["tasks"])
    assert resumed.snapshot(healthy_mission["id"])["planner"]["status"] == "ready"
    assert resumed.claim_ready_task(healthy_mission["id"]) is not None
    quarantines = [
        item
        for item in storage.list_events(limit=50)
        if item["kind"] == "executive.plan.quarantine"
    ]
    assert any(item["payload"].get("mission_id") == malformed_mission["id"] for item in quarantines)
    storage.close()


def test_cold_start_backfills_pristine_mission_without_plan(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )

    snapshot = resumed.snapshot(mission["id"])
    assert snapshot is not None
    assert snapshot["planner"]["status"] == "ready"
    assert resumed.claim_ready_task(mission["id"]) is not None
    storage.close()


def test_cold_start_quarantines_partially_executed_mission_without_plan(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    assert storage.claim_mission_task(mission["id"], mission["tasks"][0]["id"])

    resumed = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        recover_interrupted=True,
    )
    refreshed = storage.get_mission(mission["id"])

    assert resumed.snapshot(mission["id"]) is None
    assert all(
        item["status"] == "blocked"
        for item in refreshed["tasks"]
        if item["status"] not in {"done", "skipped"}
    )
    storage.close()


def test_cold_start_fingerprint_change_inserts_revalidation_branch(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    first = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    first.create_for_mission(mission)
    resumed = ExecutiveCoordinator(storage=storage, host_profile=_profile("two"))

    claim = resumed.claim_ready_task(mission["id"])

    assert claim is not None
    assert claim.step_id == "environment.r1"
    snapshot = resumed.snapshot(mission["id"])["planner"]
    assert snapshot["revision"] == 1
    assert (
        snapshot["environment"]["facts"]["profile_fingerprint"]
        == _profile("two")["fingerprint_sha256"]
    )
    storage.close()


def test_fingerprint_change_invalidates_active_approval_and_reconciles(tmp_path):
    storage = _storage(tmp_path)
    mission = _mission(storage)
    first = ExecutiveCoordinator(storage=storage, host_profile=_profile("one"))
    first.create_for_mission(mission)
    blocked = first.claim_ready_task(mission["id"])
    storage.update_mission_task(
        blocked.task["id"],
        mission_id=mission["id"],
        status="blocked",
    )
    approval = _create_bound_approval(storage, first, mission["id"], blocked)

    resumed = ExecutiveCoordinator(storage=storage, host_profile=_profile("two"))
    claim = resumed.claim_ready_task(mission["id"])
    snapshot = resumed.snapshot(mission["id"])["planner"]
    recovery = next(
        item for item in snapshot["steps"] if item["spec"]["step_id"].startswith("recover.r")
    )

    assert claim is not None and claim.step_id == "diagnose.r1"
    invalidated = storage.get_approval(approval["id"])
    assert invalidated["status"] == "cancelled"
    assert invalidated["result"]["reconciliation"]["status"] == "completed"
    assert invalidated["result"]["reconciliation"]["mode"] == "environment_invalidated"
    assert storage.pending_approval_reconciliations() == []
    assert all(item["spec"]["step_id"] != blocked.step_id for item in snapshot["steps"])
    assert recovery["spec"]["action"]["tool"] == "execution.verify"
    assert recovery["spec"]["action"]["arguments"]["source_tool"] == "execution.apply"
    assert snapshot["environment"]["digest"] == resumed.environment.digest
    storage.close()


def test_plan_creation_consults_execution_playbooks(tmp_path):
    storage = _storage(tmp_path)
    playbooks = ExecutionPlaybookStore(tmp_path / "state" / "playbooks.sqlite3")
    playbooks.record(
        symptom="adaptive runtime dependency conflict",
        solution="pin compatible dependencies",
        verification="run the complete test suite",
        outcome="success",
    )
    mission = _mission(storage)
    coordinator = ExecutiveCoordinator(
        storage=storage,
        host_profile=_profile("one"),
        playbooks=playbooks,
    )

    record = coordinator.create_for_mission(mission)

    assert record["playbooks"]
    assert "dependency conflict" in record["playbooks"][0]["symptom"]
    playbooks.close()
    storage.close()
