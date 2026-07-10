from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

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

    due = manager.due_jobs(now=now + timedelta(minutes=1))
    recovered = manager.recover_stale_running_jobs(now=now + timedelta(minutes=6))
    run = manager.list_job_runs(job_id=job["id"])[0]

    assert active is not None
    assert due == []
    assert recovered[0]["id"] == job["id"]
    assert recovered[0]["status"] == "enabled"
    assert recovered[0]["running_lease_id"] is None
    assert recovered[0]["consecutive_failures"] == 1
    assert run["ok"] is False
    assert "lease expired" in run["summary"]
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

    assert result["ok"] is True
    assert result["job"]["status"] == "done"
    assert result["job"]["payload"]["mission_id"].startswith("mis_")
    assert result["data"]["completed"] is True
    assert storage.get_mission(result["job"]["payload"]["mission_id"])["status"] == "done"
    assert operations.list_job_runs(job_id=job["id"])[0]["ok"] is True
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
    assert stored["running_lease_id"] is None
    assert run["job_status"] == "cancelled"
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
