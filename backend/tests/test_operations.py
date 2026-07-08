from __future__ import annotations

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
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
