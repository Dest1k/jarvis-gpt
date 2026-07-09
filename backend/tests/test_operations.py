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
    assert ["container", "prune", "-f"] in commands
    storage.close()
