from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from datetime import UTC, datetime, timedelta

import pytest
from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.autonomy_executor import AutonomyExecutor
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.experience import ExperienceManager
from jarvis_gpt.learning import LearningEngine
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.operations import OperationsManager, docker_container_allowed
from jarvis_gpt.storage import JarvisStorage


def _manager(monkeypatch, tmp_path) -> tuple[OperationsManager, JarvisStorage]:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return OperationsManager(settings=settings, storage=storage), storage


def _scheduler_executor(
    manager: OperationsManager,
    storage: JarvisStorage,
) -> AutonomyExecutor:
    """Build the scheduler shell for reconciliation-only tests."""

    return AutonomyExecutor(
        settings=manager.settings,
        storage=storage,
        operations=manager,
        agent=object(),
        experience=object(),
        llm=object(),
        telemetry=object(),
        dispatcher=object(),
        learning=object(),
    )


def test_browser_and_docker_policy_persist(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)

    assert manager.browser_policy()["mode"] == "open"
    assert manager.browser_policy()["require_approval_for_external"] is False
    browser = manager.update_browser_policy({"mode": "local-safe", "max_urls_per_action": 3})
    docker = manager.update_docker_policy(
        {"allowed_prefixes": ["jarvis-", "lab-"], "max_log_tail": 120}
    )
    reloaded = OperationsManager(settings=manager.settings, storage=storage)

    assert browser["mode"] == "local-safe"
    assert reloaded.browser_policy()["max_urls_per_action"] == 3
    assert docker["max_log_tail"] == 120
    assert docker_container_allowed(docker, "lab-worker") is True
    assert docker_container_allowed(docker, "postgres") is False
    storage.close()


def test_autonomy_jobs_are_budgeted(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)

    job = manager.create_job(
        {
            "title": "Diagnostics twice",
            "kind": "diagnostics",
            "budget": {"max_runs": 1, "max_minutes": 5},
        }
    )
    updated = manager.mark_job_run(job["id"], {"ok": True, "summary": "done"})

    assert job["status"] == "enabled"
    assert updated is not None
    assert updated["run_count"] == 1
    assert updated["status"] == "done"
    storage.close()


def test_autonomy_jobs_report_due_work(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    now = datetime(2026, 7, 9, 12, tzinfo=UTC)
    manager.create_job(
        {
            "title": "Manual",
            "kind": "diagnostics",
            "cadence": "manual",
            "budget": {"max_runs": 3},
        }
    )
    manager.create_job(
        {
            "title": "Due once",
            "kind": "diagnostics",
            "cadence": "once",
            "budget": {"max_runs": 3},
        }
    )
    interval = manager.create_job(
        {
            "title": "Due interval",
            "kind": "diagnostics",
            "cadence": "15m",
            "budget": {"max_runs": 3},
        }
    )
    manager.mark_job_run(
        interval["id"],
        {"ok": True, "summary": "old", "job_status": "enabled"},
    )
    stored = next(job for job in manager.list_jobs() if job["id"] == interval["id"])
    manager.update_job(
        interval["id"],
        {"last_run_at": (now - timedelta(minutes=16)).isoformat()},
    )

    due = manager.due_jobs(now=now)

    assert {job["title"] for job in due} == {"Due once", "Due interval"}
    assert stored["status"] == "enabled"
    storage.close()


def test_autonomy_job_failure_backoff_and_run_history(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    now = datetime(2026, 7, 9, 12, tzinfo=UTC)
    job = manager.create_job(
        {
            "title": "Retrying diagnostics",
            "kind": "diagnostics",
            "cadence": "1m",
            "budget": {"max_runs": 3},
        }
    )
    started_at = now.isoformat()
    finished_at = (now + timedelta(seconds=2)).isoformat()

    updated = manager.mark_job_run(
        job["id"],
        {"ok": False, "summary": "temporary failure", "job_status": "enabled"},
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=2000,
    )
    run = manager.record_job_run(
        job,
        {"ok": False, "summary": "temporary failure", "job_status": "enabled"},
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=2000,
    )

    assert updated is not None
    assert updated["status"] == "enabled"
    assert updated["consecutive_failures"] == 1
    assert updated["last_duration_ms"] == 2000
    assert updated["next_run_after"] is not None
    assert manager.due_jobs(now=now + timedelta(seconds=30)) == []
    assert [item["id"] for item in manager.due_jobs(now=now + timedelta(minutes=2))] == [job["id"]]
    assert manager.list_job_runs(job_id=job["id"])[0]["id"] == run["id"]
    storage.close()


def test_autonomy_jobs_have_priority_deadline_and_cancel(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    now = datetime(2026, 7, 9, 12, tzinfo=UTC)
    low = manager.create_job(
        {
            "title": "Low",
            "kind": "diagnostics",
            "cadence": "1m",
            "priority": 1,
            "budget": {"max_runs": 3},
        }
    )
    high = manager.create_job(
        {
            "title": "High",
            "kind": "diagnostics",
            "cadence": "1m",
            "priority": 90,
            "budget": {"max_runs": 3},
        }
    )
    expired = manager.create_job(
        {
            "title": "Expired",
            "kind": "diagnostics",
            "cadence": "1m",
            "priority": 100,
            "deadline_at": (now - timedelta(minutes=1)).isoformat(),
            "budget": {"max_runs": 3},
        }
    )

    due = manager.due_jobs(now=now)
    cancelled = manager.update_job(low["id"], {"status": "cancelled"})
    after_late_result = manager.mark_job_run(
        low["id"],
        {"ok": True, "summary": "finished after cancel", "job_status": "enabled"},
    )

    assert [item["id"] for item in due] == [high["id"], low["id"]]
    assert expired["id"] not in {item["id"] for item in due}
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelled_at"] is not None
    assert after_late_result is not None
    assert after_late_result["status"] == "cancelled"
    storage.close()


def test_autonomy_running_lease_blocks_due_and_recovers_stale(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    now = datetime(2026, 7, 9, 12, tzinfo=UTC)
    job = manager.create_job(
        {
            "title": "Leased diagnostics",
            "kind": "diagnostics",
            "cadence": "1m",
            "budget": {"max_runs": 3},
        }
    )
    active = manager.mark_job_started(
        job["id"],
        lease_id="lease-active",
        started_at=now.isoformat(),
        lease_until=(now + timedelta(minutes=5)).isoformat(),
    )
    duplicate = manager.mark_job_started(
        job["id"],
        lease_id="lease-duplicate",
        started_at=(now + timedelta(seconds=1)).isoformat(),
        lease_until=(now + timedelta(minutes=6)).isoformat(),
    )

    due = manager.due_jobs(now=now + timedelta(minutes=1))
    stored_active = next(item for item in manager.list_jobs() if item["id"] == job["id"])
    recovered = manager.recover_stale_running_jobs(now=now + timedelta(minutes=5))
    run = manager.list_job_runs(job_id=job["id"])[0]

    assert active is not None
    assert duplicate is None
    assert stored_active["running_lease_id"] == "lease-active"
    assert due == []
    assert recovered[0]["id"] == job["id"]
    assert recovered[0]["status"] == "paused"
    assert recovered[0]["running_lease_id"] is None
    assert recovered[0]["consecutive_failures"] == 1
    assert recovered[0]["next_run_after"] is None
    assert recovered[0]["last_result"]["reconcile_required"] is True
    assert recovered[0]["last_result"]["data"]["reconciliation"] == {
        "required": True,
        "reason": "lease_expired",
        "replay_original_action": False,
    }
    assert manager.due_jobs(now=now + timedelta(hours=1)) == []
    assert run["ok"] is False
    assert "lease expired" in run["summary"]
    storage.close()


@pytest.mark.parametrize(
    ("lease_expiry", "reason"),
    [
        (None, "lease_missing_expiry"),
        ("", "lease_missing_expiry"),
        ("not-a-timestamp", "lease_invalid_expiry"),
    ],
)
def test_autonomy_invalid_lease_expiry_requires_reconciliation(
    monkeypatch,
    tmp_path,
    lease_expiry,
    reason,
):
    manager, storage = _manager(monkeypatch, tmp_path)
    now = datetime(2026, 7, 9, 12, tzinfo=UTC)
    job = manager.create_job(
        {
            "title": "Ambiguous leased work",
            "kind": "mission",
            "cadence": "1m",
            "budget": {"max_runs": 3},
        }
    )
    manager.mark_job_started(
        job["id"],
        lease_id="lease-ambiguous",
        started_at=now.isoformat(),
        lease_until=(now + timedelta(minutes=5)).isoformat(),
    )
    manager.update_job(job["id"], {"running_lease_until": lease_expiry})

    recovered = manager.recover_stale_running_jobs(now=now)
    stored = next(item for item in manager.list_jobs() if item["id"] == job["id"])

    assert [item["id"] for item in recovered] == [job["id"]]
    assert stored["status"] == "paused"
    assert stored["running_lease_id"] is None
    assert stored["last_result"]["data"]["reconciliation"]["reason"] == reason
    assert stored["last_result"]["data"]["reconciliation"]["replay_original_action"] is False
    assert manager.due_jobs(now=now + timedelta(days=1)) == []
    storage.close()


def test_scheduler_reconciles_lease_that_expires_after_startup(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    executor = _scheduler_executor(manager, storage)
    now = datetime(2026, 7, 9, 12, tzinfo=UTC)
    job = manager.create_job(
        {
            "title": "Lease crosses backend restart",
            "kind": "mission",
            "cadence": "1m",
            "budget": {"max_runs": 3},
        }
    )
    manager.mark_job_started(
        job["id"],
        lease_id="lease-after-restart",
        started_at=now.isoformat(),
        lease_until=(now + timedelta(minutes=5)).isoformat(),
    )

    assert asyncio.run(executor.run_due_jobs(now=now + timedelta(minutes=1))) == []
    active = next(item for item in manager.list_jobs() if item["id"] == job["id"])
    assert active["status"] == "enabled"
    assert active["running_lease_id"] == "lease-after-restart"

    assert asyncio.run(executor.run_due_jobs(now=now + timedelta(minutes=5))) == []
    recovered = next(item for item in manager.list_jobs() if item["id"] == job["id"])
    assert recovered["status"] == "paused"
    assert recovered["running_lease_id"] is None
    assert recovered["last_result"]["reconcile_required"] is True
    assert len(manager.list_job_runs(job_id=job["id"])) == 1
    storage.close()


def test_scheduler_persists_expired_deadline_as_terminal(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    executor = _scheduler_executor(manager, storage)
    now = datetime(2026, 7, 9, 12, tzinfo=UTC)
    job = manager.create_job(
        {
            "title": "Expired before scheduler",
            "kind": "diagnostics",
            "cadence": "1m",
            "deadline_at": now.isoformat(),
            "budget": {"max_runs": 3},
        }
    )

    assert asyncio.run(executor.run_due_jobs(now=now)) == []
    expired = next(item for item in manager.list_jobs() if item["id"] == job["id"])
    history = manager.list_job_runs(job_id=job["id"])

    assert expired["status"] == "cancelled"
    assert expired["cancelled_at"] == now.isoformat(timespec="seconds")
    assert expired["last_result"]["deadline_expired"] is True
    assert expired["last_result"]["data"]["expired_without_execution"] is True
    assert manager.due_jobs(now=now + timedelta(days=1)) == []
    assert len(history) == 1
    assert history[0]["job_status"] == "cancelled"
    storage.close()


def test_deadline_expiry_cannot_clear_concurrently_acquired_lease(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    now = datetime(2026, 7, 9, 12, tzinfo=UTC)
    job = manager.create_job(
        {
            "title": "Expiry race",
            "kind": "diagnostics",
            "cadence": "1m",
            "deadline_at": now.isoformat(),
            "budget": {"max_runs": 3},
        }
    )
    original_mark_job_run = manager.mark_job_run

    def acquire_lease_then_finalize(job_id, result, **kwargs):
        acquired = manager.mark_job_started(
            job_id,
            lease_id="lease-won-race",
            started_at=now.isoformat(),
            lease_until=(now + timedelta(minutes=5)).isoformat(),
        )
        assert acquired is not None
        return original_mark_job_run(job_id, result, **kwargs)

    monkeypatch.setattr(manager, "mark_job_run", acquire_lease_then_finalize)

    expired = manager.expire_deadline_jobs(now=now)
    stored = next(item for item in manager.list_jobs() if item["id"] == job["id"])

    assert expired == []
    assert stored["status"] == "enabled"
    assert stored["running_lease_id"] == "lease-won-race"
    assert stored["run_count"] == 0
    assert manager.list_job_runs(job_id=job["id"]) == []
    storage.close()


def test_late_worker_cannot_overwrite_reconciled_lease(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    now = datetime(2026, 7, 9, 12, tzinfo=UTC)
    job = manager.create_job(
        {
            "title": "Late worker",
            "kind": "mission",
            "cadence": "1m",
            "budget": {"max_runs": 3},
        }
    )
    manager.mark_job_started(
        job["id"],
        lease_id="lease-old-worker",
        started_at=now.isoformat(),
        lease_until=(now + timedelta(minutes=1)).isoformat(),
    )
    manager.recover_stale_running_jobs(now=now + timedelta(minutes=2))

    late = manager.mark_job_run(
        job["id"],
        {"ok": True, "summary": "late success", "job_status": "enabled"},
        expected_lease_id="lease-old-worker",
        finished_at=(now + timedelta(minutes=3)).isoformat(),
    )
    stored = next(item for item in manager.list_jobs() if item["id"] == job["id"])

    assert late is None
    assert stored["status"] == "paused"
    assert stored["run_count"] == 1
    assert stored["last_result"]["reconcile_required"] is True
    storage.close()


@pytest.mark.parametrize("operator_status", ["paused", "done"])
def test_worker_finalization_preserves_concurrent_operator_status(
    monkeypatch, tmp_path, operator_status
):
    manager, storage = _manager(monkeypatch, tmp_path)
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    job = manager.create_job(
        {
            "title": "Operator status wins",
            "kind": "mission",
            "cadence": "1m",
            "budget": {"max_runs": 3},
        }
    )
    manager.mark_job_started(
        job["id"],
        lease_id="lease-status-race",
        started_at=now.isoformat(),
        lease_until=(now + timedelta(minutes=5)).isoformat(),
    )
    manager.update_job(job["id"], {"status": operator_status})

    finalized = manager.mark_job_run(
        job["id"],
        {"ok": True, "summary": "partial", "job_status": "enabled"},
        expected_lease_id="lease-status-race",
        finished_at=(now + timedelta(seconds=5)).isoformat(),
    )

    assert finalized is not None
    assert finalized["status"] == operator_status
    assert finalized["running_lease_id"] is None
    assert finalized["run_count"] == 1
    storage.close()


def test_mission_planning_is_registered_for_cancellation(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    planning_started = asyncio.Event()
    planning_cancelled = False

    class SlowAgent:
        async def create_mission_planned(self, _goal, title=None, *, mission_id=None):
            nonlocal planning_cancelled
            del title, mission_id
            planning_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                planning_cancelled = True
                raise

    executor = _scheduler_executor(manager, storage)
    executor.agent = SlowAgent()
    run_kind_called = False

    async def forbidden_run_kind(_kind, _payload):
        nonlocal run_kind_called
        run_kind_called = True
        return {"ok": True, "summary": "must not run"}

    executor.run_kind = forbidden_run_kind
    job = manager.create_job(
        {
            "title": "Cancellable planning",
            "kind": "mission",
            "cadence": "once",
            "payload": {"goal": "Plan slowly"},
            "budget": {"max_runs": 3, "max_minutes": 5},
        }
    )

    async def scenario():
        running = asyncio.create_task(executor.run_job(job))
        await asyncio.wait_for(planning_started.wait(), timeout=1)
        cancelled = await executor.cancel_job(job["id"])
        return cancelled, await asyncio.wait_for(running, timeout=1)

    cancelled, result = asyncio.run(scenario())
    stored = next(item for item in manager.list_jobs() if item["id"] == job["id"])

    assert cancelled is not None and cancelled["status"] == "cancelled"
    assert result["job"]["status"] == "cancelled"
    assert result["reconcile_required"] is True
    assert result["data"]["outcome_known"] is False
    assert result["data"]["reconciliation"]["replay_original_action"] is False
    assert planning_cancelled is True
    assert run_kind_called is False
    assert stored["status"] == "cancelled"
    storage.close()


def test_mission_planning_consumes_job_timeout(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    planning_cancelled = False

    class SlowAgent:
        async def create_mission_planned(self, _goal, title=None, *, mission_id=None):
            nonlocal planning_cancelled
            del title, mission_id
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                planning_cancelled = True
                raise

    executor = _scheduler_executor(manager, storage)
    executor.agent = SlowAgent()
    monkeypatch.setattr("jarvis_gpt.autonomy_executor._job_timeout_seconds", lambda _job: 0.01)
    job = manager.create_job(
        {
            "title": "Budgeted planning",
            "kind": "mission",
            "cadence": "once",
            "payload": {"goal": "Plan within budget"},
            "budget": {"max_runs": 3, "max_minutes": 5},
        }
    )

    result = asyncio.run(executor.run_job(job))
    stored = next(item for item in manager.list_jobs() if item["id"] == job["id"])

    assert result["ok"] is False
    assert result["reconcile_required"] is True
    assert planning_cancelled is True
    assert stored["status"] == "paused"
    assert (
        stored["last_result"]["data"]["reconciliation"]["reason"]
        == "ambiguous_job_outcome"
    )
    storage.close()


def test_mission_deadline_is_rechecked_after_planning(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    deadline_checks = iter((False, True))
    monkeypatch.setattr(
        "jarvis_gpt.autonomy_executor._deadline_expired",
        lambda _value: next(deadline_checks),
    )

    class PlanningAgent:
        async def create_mission_planned(self, goal, title=None, *, mission_id=None):
            del goal, title
            return {"id": mission_id}

    executor = _scheduler_executor(manager, storage)
    executor.agent = PlanningAgent()
    run_kind_called = False

    async def forbidden_run_kind(_kind, _payload):
        nonlocal run_kind_called
        run_kind_called = True
        return {"ok": True, "summary": "must not execute after deadline"}

    executor.run_kind = forbidden_run_kind
    job = manager.create_job(
        {
            "title": "Deadline during planning",
            "kind": "mission",
            "cadence": "once",
            "deadline_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
            "payload": {"goal": "Plan until the deadline"},
            "budget": {"max_runs": 3, "max_minutes": 5},
        }
    )

    result = asyncio.run(executor.run_job(job))
    stored = next(item for item in manager.list_jobs() if item["id"] == job["id"])

    assert result["ok"] is False
    assert result["deadline_expired"] is True
    assert result["data"]["expired_without_execution"] is True
    assert result["data"]["replay_original_action"] is False
    assert run_kind_called is False
    assert stored["status"] == "cancelled"
    storage.close()


def test_reserved_mission_id_recovers_crash_before_mission_insert(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    reserved_id = "mis_crashreserved1"
    created_ids: list[str] = []

    class RecoveringAgent:
        async def create_mission_planned(self, goal, title=None, *, mission_id=None):
            del title
            assert mission_id == reserved_id
            bound = next(item for item in manager.list_jobs() if item["id"] == job["id"])
            assert bound["payload"]["mission_id"] == reserved_id
            created_ids.append(mission_id)
            return storage.create_mission(
                mission_id=mission_id,
                title="Recovered mission",
                goal=goal,
                tasks=["Recover", "Verify"],
            )

    executor = _scheduler_executor(manager, storage)
    executor.agent = RecoveringAgent()

    async def finish_mission(_kind, payload):
        assert payload["mission_id"] == reserved_id
        return {"ok": True, "summary": "recovered", "job_status": "done"}

    executor.run_kind = finish_mission
    job = manager.create_job(
        {
            "title": "Crash-recoverable mission",
            "kind": "mission",
            "cadence": "once",
            "payload": {"goal": "Recover after reservation"},
            "budget": {"max_runs": 3, "max_minutes": 5},
        }
    )
    # Durable state left by a process that died after reserving the id but
    # before the mission transaction started.
    manager.update_job(job["id"], {"payload": {**job["payload"], "mission_id": reserved_id}})
    crashed_state = next(item for item in manager.list_jobs() if item["id"] == job["id"])

    result = asyncio.run(executor.run_job(crashed_state))

    assert result["ok"] is True
    assert result["job"]["payload"]["mission_id"] == reserved_id
    assert created_ids == [reserved_id]
    assert [mission["id"] for mission in storage.list_missions()] == [reserved_id]
    storage.close()


def test_mission_job_rejects_reserved_id_bound_to_another_goal(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    mission_id = "mis_existingbinding1"
    storage.create_mission(
        mission_id=mission_id,
        title="Existing mission",
        goal="Existing potentially mutating goal",
        tasks=["Do existing work"],
    )
    executor = _scheduler_executor(manager, storage)
    planner_called = False
    run_kind_called = False

    class ForbiddenAgent:
        async def create_mission_planned(self, _goal, title=None, *, mission_id=None):
            nonlocal planner_called
            del title, mission_id
            planner_called = True
            raise AssertionError("collision must fail before planning")

    async def forbidden_run_kind(_kind, _payload):
        nonlocal run_kind_called
        run_kind_called = True
        return {"ok": True, "summary": "must not execute colliding mission"}

    executor.agent = ForbiddenAgent()
    executor.run_kind = forbidden_run_kind
    job = manager.create_job(
        {
            "title": "Conflicting mission",
            "kind": "mission",
            "cadence": "once",
            "payload": {"goal": "Different goal", "mission_id": mission_id},
            "budget": {"max_runs": 3, "max_minutes": 5},
        }
    )

    result = asyncio.run(executor.run_job(job))
    stored = next(item for item in manager.list_jobs() if item["id"] == job["id"])

    assert result["ok"] is False
    assert result["reconcile_required"] is True
    assert result["data"]["reconciliation"]["reason"] == "mission_binding_failed"
    assert result["data"]["reconciliation"]["replay_original_action"] is False
    assert planner_called is False
    assert run_kind_called is False
    assert stored["status"] == "paused"
    storage.close()


def test_same_mission_id_and_goal_cannot_be_driven_by_two_jobs(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    mission_id = "mis_singlejobowner1"
    executor = _scheduler_executor(manager, storage)
    executed_jobs: list[str] = []

    class CreatingAgent:
        async def create_mission_planned(self, goal, title=None, *, mission_id=None):
            return storage.create_mission(
                mission_id=mission_id,
                title=title or "Owned mission",
                goal=goal,
                tasks=["Only once"],
            )

    async def finish(_kind, payload):
        executed_jobs.append(str(payload["mission_id"]))
        return {"ok": True, "summary": "ran", "job_status": "done"}

    executor.agent = CreatingAgent()
    executor.run_kind = finish
    payload = {"goal": "One owner only", "mission_id": mission_id}
    first = manager.create_job(
        {
            "title": "First owner",
            "kind": "mission",
            "cadence": "once",
            "payload": payload,
            "budget": {"max_runs": 2, "max_minutes": 5},
        }
    )
    second = manager.create_job(
        {
            "title": "Conflicting owner",
            "kind": "mission",
            "cadence": "once",
            "payload": payload,
            "budget": {"max_runs": 2, "max_minutes": 5},
        }
    )

    first_result = asyncio.run(executor.run_job(first))
    second_result = asyncio.run(executor.run_job(second))
    stored_second = next(
        item for item in manager.list_jobs() if item["id"] == second["id"]
    )

    assert first_result["ok"] is True
    assert second_result["ok"] is False
    assert second_result["data"]["reconciliation"]["reason"] == "mission_binding_failed"
    assert second_result["data"]["reconciliation"]["replay_original_action"] is False
    assert executed_jobs == [mission_id]
    assert stored_second["status"] == "paused"
    storage.close()


def test_mission_autonomy_job_runs_headless_and_persists_mission_id(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    operations = OperationsManager(settings=settings, storage=storage)
    llm = LLMRouter(settings)
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm)
    executor = AutonomyExecutor(
        settings=settings,
        storage=storage,
        operations=operations,
        agent=agent,
        experience=ExperienceManager(settings=settings, storage=storage),
        llm=llm,
        telemetry=object(),
        dispatcher=object(),
        learning=LearningEngine(storage),
    )
    job = operations.create_job(
        {
            "title": "Headless mission",
            "kind": "mission",
            "cadence": "once",
            "budget": {"max_runs": 5, "max_minutes": 30},
            "payload": {"goal": "Build tools runtime", "max_steps": 24},
        }
    )

    result = asyncio.run(executor.run_job(job))

    assert result["ok"] is False
    assert result["job"]["status"] == "paused"
    assert result["job"]["payload"]["mission_id"].startswith("mis_")
    assert result["data"]["completed"] is False
    assert result["data"]["blocked"] is True
    assert storage.get_mission(result["job"]["payload"]["mission_id"])["status"] == "planned"
    assert operations.list_job_runs(job_id=job["id"])[0]["ok"] is False
    storage.close()


def test_autonomy_executor_records_exceptions_as_failed_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    operations = OperationsManager(settings=settings, storage=storage)
    llm = LLMRouter(settings)
    executor = AutonomyExecutor(
        settings=settings,
        storage=storage,
        operations=operations,
        agent=AgentRuntime(settings=settings, storage=storage, llm=llm),
        experience=ExperienceManager(settings=settings, storage=storage),
        llm=llm,
        telemetry=object(),
        dispatcher=object(),
        learning=LearningEngine(storage),
    )
    job = operations.create_job(
        {
            "title": "Exploding diagnostics",
            "kind": "diagnostics",
            "cadence": "1m",
            "budget": {"max_runs": 3, "max_minutes": 5},
        }
    )

    async def explode(_kind, _payload):
        raise RuntimeError("boom")

    executor.run_kind = explode

    result = asyncio.run(executor.run_job(job))
    stored = operations.list_jobs()[0]
    run = operations.list_job_runs(job_id=job["id"])[0]

    assert result["ok"] is False
    assert stored["status"] == "enabled"
    assert stored["consecutive_failures"] == 1
    assert stored["next_run_after"] is not None
    assert run["ok"] is False
    assert "boom" in run["summary"]
    storage.close()


def test_mutating_autonomy_exception_pauses_for_reconciliation(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    operations = OperationsManager(settings=settings, storage=storage)
    llm = LLMRouter(settings)
    executor = AutonomyExecutor(
        settings=settings,
        storage=storage,
        operations=operations,
        agent=AgentRuntime(settings=settings, storage=storage, llm=llm),
        experience=ExperienceManager(settings=settings, storage=storage),
        llm=llm,
        telemetry=object(),
        dispatcher=object(),
        learning=LearningEngine(storage),
    )
    job = operations.create_job(
        {
            "title": "Ambiguous learning mutation",
            "kind": "learning.tick",
            "cadence": "1m",
            "budget": {"max_runs": 3, "max_minutes": 5},
        }
    )

    async def explode(_kind, _payload):
        raise RuntimeError("failed after a possible write")

    executor.run_kind = explode

    result = asyncio.run(executor.run_job(job))
    stored = operations.list_jobs()[0]

    assert result["ok"] is False
    assert stored["status"] == "paused"
    assert stored["next_run_after"] is None
    assert result["data"]["outcome_known"] is False
    assert result["data"]["reconciliation"]["required"] is True
    assert result["data"]["reconciliation"]["replay_original_action"] is False
    storage.close()


def test_mutating_autonomy_timeout_never_blindly_retries(monkeypatch, tmp_path):
    import jarvis_gpt.autonomy_executor as autonomy_module

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr(autonomy_module, "_job_timeout_seconds", lambda _job: 0.01)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    operations = OperationsManager(settings=settings, storage=storage)
    llm = LLMRouter(settings)
    executor = AutonomyExecutor(
        settings=settings,
        storage=storage,
        operations=operations,
        agent=AgentRuntime(settings=settings, storage=storage, llm=llm),
        experience=ExperienceManager(settings=settings, storage=storage),
        llm=llm,
        telemetry=object(),
        dispatcher=object(),
        learning=LearningEngine(storage),
    )
    job = operations.create_job(
        {
            "title": "Slow mutation",
            "kind": "learning.tick",
            "cadence": "1m",
            "budget": {"max_runs": 3, "max_minutes": 5},
        }
    )

    async def slow(_kind, _payload):
        await asyncio.sleep(60)
        return {"ok": True, "summary": "late"}

    executor.run_kind = slow

    result = asyncio.run(executor.run_job(job))
    stored = operations.list_jobs()[0]

    assert result["ok"] is False
    assert stored["status"] == "paused"
    assert result["reconcile_required"] is True
    assert result["data"]["reconciliation"]["replay_original_action"] is False
    storage.close()


def test_goal_mission_binding_failure_pauses_without_duplicate_creation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    operations = OperationsManager(settings=settings, storage=storage)
    llm = LLMRouter(settings)
    executor = AutonomyExecutor(
        settings=settings,
        storage=storage,
        operations=operations,
        agent=AgentRuntime(settings=settings, storage=storage, llm=llm),
        experience=ExperienceManager(settings=settings, storage=storage),
        llm=llm,
        telemetry=object(),
        dispatcher=object(),
        learning=LearningEngine(storage),
    )
    job = operations.create_job(
        {
            "title": "Bind once",
            "kind": "mission",
            "cadence": "once",
            "budget": {"max_runs": 3, "max_minutes": 5},
            "payload": {"goal": "Create one durable mission"},
        }
    )
    original_update = operations.update_job

    def fail_mission_binding(job_id, patch):
        payload = patch.get("payload") if isinstance(patch, dict) else None
        if isinstance(payload, dict) and payload.get("mission_id"):
            raise OSError("runtime KV unavailable")
        return original_update(job_id, patch)

    monkeypatch.setattr(operations, "update_job", fail_mission_binding)

    result = asyncio.run(executor.run_job(job))
    stored = operations.list_jobs()[0]

    assert result["ok"] is False
    assert stored["status"] == "paused"
    assert stored["payload"].get("mission_id") is None
    assert result["data"]["mission_id"].startswith("mis_")
    assert result["data"]["reconciliation"]["replay_original_action"] is False
    # Reservation failed before mission planning/INSERT, so no orphan durable
    # mission is allowed to exist.
    assert storage.list_missions(limit=10) == []
    storage.close()


def test_post_commit_observability_failure_does_not_change_job_outcome(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    operations = OperationsManager(settings=settings, storage=storage)
    llm = LLMRouter(settings)
    executor = AutonomyExecutor(
        settings=settings,
        storage=storage,
        operations=operations,
        agent=AgentRuntime(settings=settings, storage=storage, llm=llm),
        experience=ExperienceManager(settings=settings, storage=storage),
        llm=llm,
        telemetry=object(),
        dispatcher=object(),
        learning=LearningEngine(storage),
    )
    job = operations.create_job(
        {
            "title": "Completed diagnostics",
            "kind": "diagnostics",
            "cadence": "once",
            "budget": {"max_runs": 1, "max_minutes": 5},
        }
    )

    async def completed(_kind, _payload):
        return {"ok": True, "summary": "completed", "job_status": "done"}

    executor.run_kind = completed
    monkeypatch.setattr(
        operations,
        "record_job_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("history down")),
    )
    monkeypatch.setattr(
        storage,
        "add_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("events down")),
    )

    result = asyncio.run(executor.run_job(job))
    stored = operations.list_jobs()[0]

    assert result["ok"] is True
    assert stored["status"] == "done"
    assert result["observability_status"]["retryable"] is False
    assert set(result["observability_status"]["failed_sinks"]) == {
        "job_run_history",
        "runtime_event",
    }
    storage.close()


def test_job_lease_acquisition_is_atomic_across_storage_connections(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage_a = JarvisStorage(settings.database_path)
    storage_a.initialize()
    storage_b = JarvisStorage(settings.database_path)
    storage_b.initialize()
    operations_a = OperationsManager(settings=settings, storage=storage_a)
    operations_b = OperationsManager(settings=settings, storage=storage_b)
    job = operations_a.create_job(
        {
            "title": "Atomic lease",
            "kind": "diagnostics",
            "cadence": "1m",
            "budget": {"max_runs": 2, "max_minutes": 5},
        }
    )
    barrier = threading.Barrier(2)

    def acquire(manager, lease_id):
        barrier.wait(timeout=2)
        return manager.mark_job_started(
            job["id"],
            lease_id=lease_id,
            started_at="2026-07-16T10:00:00+00:00",
            lease_until="2026-07-16T10:05:00+00:00",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda pair: acquire(*pair),
                ((operations_a, "lease-a"), (operations_b, "lease-b")),
            )
        )

    assert sum(result is not None for result in results) == 1
    stored = operations_a.list_jobs()[0]
    assert stored["running_lease_id"] in {"lease-a", "lease-b"}
    storage_b.close()
    storage_a.close()


def test_autonomy_executor_cancels_running_child_task(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    operations = OperationsManager(settings=settings, storage=storage)
    llm = LLMRouter(settings)
    executor = AutonomyExecutor(
        settings=settings,
        storage=storage,
        operations=operations,
        agent=AgentRuntime(settings=settings, storage=storage, llm=llm),
        experience=ExperienceManager(settings=settings, storage=storage),
        llm=llm,
        telemetry=object(),
        dispatcher=object(),
        learning=LearningEngine(storage),
    )
    job = operations.create_job(
        {
            "title": "Slow diagnostics",
            "kind": "diagnostics",
            "cadence": "manual",
            "budget": {"max_runs": 3, "max_minutes": 5},
        }
    )

    async def scenario():
        started = asyncio.Event()

        async def slow(_kind, _payload):
            started.set()
            await asyncio.sleep(60)
            return {"ok": True, "summary": "late"}

        executor.run_kind = slow
        run_task = asyncio.create_task(executor.run_job(job))
        await asyncio.wait_for(started.wait(), timeout=1)
        cancelled = await executor.cancel_job(job["id"])
        result = await asyncio.wait_for(run_task, timeout=1)
        return cancelled, result

    cancelled, result = asyncio.run(scenario())
    stored = operations.list_jobs()[0]
    run = operations.list_job_runs(job_id=job["id"])[0]

    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert result["ok"] is False
    assert result["job"]["status"] == "cancelled"
    assert result["reconcile_required"] is False
    assert result["data"]["outcome_known"] is True
    assert stored["running_lease_id"] is None
    assert run["job_status"] == "cancelled"
    storage.close()


def test_cancel_still_signals_running_mutation_when_audit_sink_fails(
    monkeypatch, tmp_path
):
    manager, storage = _manager(monkeypatch, tmp_path)
    executor = _scheduler_executor(manager, storage)
    started = asyncio.Event()
    child_cancelled = False

    async def slow_mutation(_kind, _payload):
        nonlocal child_cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            child_cancelled = True
            raise

    executor.run_kind = slow_mutation
    job = manager.create_job(
        {
            "title": "Cancel despite audit outage",
            "kind": "mission",
            "cadence": "manual",
            "budget": {"max_runs": 3, "max_minutes": 5},
        }
    )

    async def scenario():
        running = asyncio.create_task(executor.run_job(job))
        await asyncio.wait_for(started.wait(), timeout=1)

        def fail_audit(**_kwargs):
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(storage, "record_audit", fail_audit)
        cancelled = await executor.cancel_job(job["id"])
        return cancelled, await asyncio.wait_for(running, timeout=1)

    cancelled, result = asyncio.run(scenario())
    stored = next(item for item in manager.list_jobs() if item["id"] == job["id"])

    assert cancelled is not None and cancelled["status"] == "cancelled"
    assert cancelled["observability_status"]["persisted"] is False
    assert cancelled["observability_status"]["retryable"] is False
    assert child_cancelled is True
    assert result["reconcile_required"] is True
    assert stored["status"] == "cancelled"
    assert stored["running_lease_id"] is None
    storage.close()


def test_autonomy_executor_does_not_run_stale_snapshot_after_cancel(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    operations = OperationsManager(settings=settings, storage=storage)
    llm = LLMRouter(settings)
    executor = AutonomyExecutor(
        settings=settings,
        storage=storage,
        operations=operations,
        agent=AgentRuntime(settings=settings, storage=storage, llm=llm),
        experience=ExperienceManager(settings=settings, storage=storage),
        llm=llm,
        telemetry=object(),
        dispatcher=object(),
        learning=LearningEngine(storage),
    )
    stale = operations.create_job(
        {
            "title": "Cancelled before scheduling",
            "kind": "diagnostics",
            "cadence": "manual",
            "budget": {"max_runs": 1, "max_minutes": 5},
        }
    )
    calls: list[str] = []

    async def fake_run_kind(kind, _payload):
        calls.append(kind)
        return {"ok": True, "summary": "should not run"}

    executor.run_kind = fake_run_kind

    async def scenario():
        cancelled = await executor.cancel_job(stale["id"])
        result = await executor.run_job(stale)
        return cancelled, result

    cancelled, result = asyncio.run(scenario())

    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert result["ok"] is False
    assert result["job"]["status"] == "cancelled"
    assert calls == []
    storage.close()


def test_cleanup_removes_only_allowed_containers(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    commands = []

    def fake_run_docker(args, *, timeout):
        commands.append(args)
        if args[:2] == ["ps", "-a"]:
            return {
                "ok": True,
                "summary": "listed",
                "stdout": (
                    '{"ID":"1","Names":"jarvis-gpt-dispatcher","Image":"vllm",'
                    '"Status":"Exited","State":"exited","Ports":""}\n'
                    '{"ID":"2","Names":"postgres","Image":"postgres",'
                    '"Status":"Running","State":"running","Ports":""}'
                ),
                "stderr": "",
                "command": ["docker", *args],
                "returncode": 0,
            }
        return {
            "ok": True,
            "summary": "ok",
            "stdout": "",
            "stderr": "",
            "command": ["docker", *args],
            "returncode": 0,
        }

    monkeypatch.setattr("jarvis_gpt.operations._run_docker", fake_run_docker)

    result = manager.cleanup()

    assert result["ok"] is True
    assert ["compose", "--profile", "llm", "down", "--remove-orphans"] in commands
    assert ["rm", "-f", "jarvis-gpt-dispatcher"] in commands
    assert ["rm", "-f", "postgres"] not in commands
    assert ["container", "prune", "-f"] not in commands
    assert result["global_prune_skipped"] is True
    storage.close()
