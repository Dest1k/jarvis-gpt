from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import JarvisSettings
from .storage import JarvisStorage, new_id, utc_now

BROWSER_POLICY_KEY = "operations.browser_policy"
DOCKER_POLICY_KEY = "operations.docker_policy"
AUTONOMY_JOBS_KEY = "operations.autonomy.jobs"
AUTONOMY_JOB_RUNS_KEY = "operations.autonomy.job_runs"
ROUTINE_RUNS_KEY = "operations.routine_runs"

DEFAULT_BROWSER_POLICY: dict[str, Any] = {
    "mode": "open",
    "allow_localhost": True,
    "allowed_hosts": ["localhost", "127.0.0.1"],
    "blocked_schemes": ["file", "javascript", "data"],
    "require_approval_for_external": False,
    "max_urls_per_action": 5,
}

DEFAULT_DOCKER_POLICY: dict[str, Any] = {
    "allowed_prefixes": ["jarvis-", "jarvis_", "jarvis-gpt"],
    "allowed_containers": ["jarvis-gpt-dispatcher"],
    "max_log_tail": 200,
    "include_stopped": True,
}

DEFAULT_ROUTINES: list[dict[str, Any]] = [
    {
        "id": "routine_daily_briefing",
        "title": "Daily briefing",
        "description": "Collect briefing, diagnostics and benchmark context.",
        "steps": ["briefing", "diagnostics", "benchmark"],
    },
    {
        "id": "routine_self_heal",
        "title": "Self-heal review",
        "description": "Run self-heal scan and persist suggested actions.",
        "steps": ["self_heal"],
    },
    {
        "id": "routine_learning",
        "title": "Learning sweep",
        "description": "Mine audit/tool/approval history into deduplicated lessons.",
        "steps": ["learning.tick"],
    },
    {
        "id": "routine_background_missions",
        "title": "Background mission sweep",
        "description": "Run due long-lived LLM mission jobs within their budgets.",
        "steps": ["background.missions"],
    },
]


class OperationsManager:
    def __init__(self, *, settings: JarvisSettings, storage: JarvisStorage) -> None:
        self.settings = settings
        self.storage = storage

    def browser_policy(self) -> dict[str, Any]:
        stored = self.storage.get_runtime_value(BROWSER_POLICY_KEY, {})
        return _normalize_browser_policy({**DEFAULT_BROWSER_POLICY, **_dict(stored)})

    def update_browser_policy(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.browser_policy()
        allowed = {key: value for key, value in patch.items() if key in DEFAULT_BROWSER_POLICY}
        updated = _normalize_browser_policy({**current, **allowed})
        self.storage.set_runtime_value(BROWSER_POLICY_KEY, updated)
        self.storage.record_audit(
            actor="operator",
            action="browser.policy.update",
            target_type="runtime",
            target_id=BROWSER_POLICY_KEY,
            summary="Browser automation policy updated",
            before=current,
            after=updated,
        )
        return updated

    def docker_policy(self) -> dict[str, Any]:
        stored = self.storage.get_runtime_value(DOCKER_POLICY_KEY, {})
        return _normalize_docker_policy({**DEFAULT_DOCKER_POLICY, **_dict(stored)})

    def update_docker_policy(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.docker_policy()
        allowed = {key: value for key, value in patch.items() if key in DEFAULT_DOCKER_POLICY}
        updated = _normalize_docker_policy({**current, **allowed})
        self.storage.set_runtime_value(DOCKER_POLICY_KEY, updated)
        self.storage.record_audit(
            actor="operator",
            action="docker.policy.update",
            target_type="runtime",
            target_id=DOCKER_POLICY_KEY,
            summary="Docker policy updated",
            before=current,
            after=updated,
        )
        return updated

    def docker_containers(self) -> dict[str, Any]:
        policy = self.docker_policy()
        command = ["ps", "--format", "{{json .}}"]
        if policy["include_stopped"]:
            command.insert(1, "-a")
        result = _run_docker(command, timeout=10)
        containers = _parse_docker_ps(result["stdout"]) if result["ok"] else []
        for container in containers:
            container["allowed"] = docker_container_allowed(
                policy,
                str(container.get("name") or ""),
            )
        summary = (
            f"Listed {len(containers)} container(s)."
            if result["ok"]
            else result["summary"]
        )
        return {
            "ok": result["ok"],
            "summary": summary,
            "policy": policy,
            "containers": containers,
            "command": result["command"],
            "error": result["stderr"] if not result["ok"] else None,
        }

    def cleanup(self, *, aggressive: bool = False) -> dict[str, Any]:
        policy = self.docker_policy()
        steps = []
        commands = [
            ["compose", "--profile", "llm", "down", "--remove-orphans"],
        ]
        containers = self.docker_containers()
        for container in containers.get("containers", []):
            name = str(container.get("name") or "")
            if docker_container_allowed(policy, name):
                commands.append(["rm", "-f", name])
        commands.append(["container", "prune", "-f"])
        if aggressive:
            commands.extend([["image", "prune", "-f"], ["builder", "prune", "-f"]])
        for command in commands:
            result = _run_docker(command, timeout=60)
            steps.append(
                {
                    "ok": result["ok"],
                    "summary": result["summary"],
                    "command": result["command"],
                    "stdout": result["stdout"][-4000:],
                    "stderr": result["stderr"][-4000:],
                    "returncode": result["returncode"],
                }
            )
        ok = all(step["ok"] for step in steps)
        self.storage.record_audit(
            actor="operator",
            action="runtime.cleanup",
            target_type="runtime",
            target_id="docker",
            summary="Runtime cleanup completed" if ok else "Runtime cleanup had warnings",
            after={"aggressive": aggressive, "steps": steps},
        )
        return {
            "ok": ok,
            "summary": "Очистка выполнена." if ok else "Очистка завершилась с предупреждениями.",
            "aggressive": aggressive,
            "steps": steps,
        }

    def list_jobs(self) -> list[dict[str, Any]]:
        stored = self.storage.get_runtime_value(AUTONOMY_JOBS_KEY, [])
        return [_normalize_job(item) for item in _list(stored)]

    def list_job_runs(
        self,
        *,
        limit: int = 50,
        job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        stored = [
            item for item in _list(self.storage.get_runtime_value(AUTONOMY_JOB_RUNS_KEY, []))
            if isinstance(item, dict)
        ]
        if job_id:
            stored = [item for item in stored if item.get("job_id") == job_id]
        return stored[: max(1, min(200, int(limit)))]

    def due_jobs(self, *, now: datetime | None = None, limit: int = 3) -> list[dict[str, Any]]:
        current = now or datetime.now(UTC)
        due = [job for job in self.list_jobs() if _job_is_due(job, current)]
        due.sort(key=_job_due_sort_key)
        return due[: max(1, min(10, int(limit)))]

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        jobs = self.list_jobs()
        job = _normalize_job(
            {
                "id": new_id("job"),
                "created_at": utc_now(),
                "run_count": 0,
                **payload,
            }
        )
        jobs = [job, *jobs][:100]
        self.storage.set_runtime_value(AUTONOMY_JOBS_KEY, jobs)
        self.storage.record_audit(
            actor="operator",
            action="autonomy.job.create",
            target_type="autonomy_job",
            target_id=job["id"],
            summary=f"Autonomy job created: {job['title']}",
            after=job,
        )
        return job

    def update_job(self, job_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
        jobs = self.list_jobs()
        updated: dict[str, Any] | None = None
        next_jobs: list[dict[str, Any]] = []
        for job in jobs:
            if job["id"] == job_id:
                before = job
                updated = _normalize_job({**job, **patch, "updated_at": utc_now()})
                if updated["status"] == "cancelled" and not updated.get("cancelled_at"):
                    updated["cancelled_at"] = utc_now()
                self.storage.record_audit(
                    actor="operator",
                    action="autonomy.job.update",
                    target_type="autonomy_job",
                    target_id=job_id,
                    summary=f"Autonomy job updated: {updated['title']}",
                    before=before,
                    after=updated,
                )
                next_jobs.append(updated)
            else:
                next_jobs.append(job)
        if updated is None:
            return None
        self.storage.set_runtime_value(AUTONOMY_JOBS_KEY, next_jobs)
        return updated

    def mark_job_run(
        self,
        job_id: str,
        result: dict[str, Any],
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any] | None:
        jobs = self.list_jobs()
        updated: dict[str, Any] | None = None
        next_jobs: list[dict[str, Any]] = []
        now = finished_at or utc_now()
        for job in jobs:
            if job["id"] == job_id:
                status = job["status"]
                run_count = int(job.get("run_count") or 0) + 1
                requested_status = str(result.get("job_status") or "")
                if status == "cancelled":
                    requested_status = "cancelled"
                if requested_status in {"enabled", "paused", "done", "cancelled"}:
                    status = requested_status
                elif run_count >= int(job["budget"]["max_runs"]):
                    status = "done"
                ok = bool(result.get("ok"))
                consecutive_failures = 0 if ok else int(job.get("consecutive_failures") or 0) + 1
                next_run_after = None
                if status == "enabled" and not ok:
                    next_run_after = _iso_after(now, _retry_delay(consecutive_failures))
                updated = {
                    **job,
                    "status": status,
                    "run_count": run_count,
                    "consecutive_failures": consecutive_failures,
                    "last_started_at": started_at or now,
                    "last_finished_at": now,
                    "last_duration_ms": duration_ms,
                    "last_run_at": now,
                    "next_run_after": next_run_after,
                    "last_result": result,
                    "cancelled_at": job.get("cancelled_at")
                    or (now if status == "cancelled" else None),
                    "updated_at": now,
                }
                next_jobs.append(updated)
            else:
                next_jobs.append(job)
        if updated is None:
            return None
        self.storage.set_runtime_value(AUTONOMY_JOBS_KEY, next_jobs)
        return updated

    def record_job_run(
        self,
        job: dict[str, Any],
        result: dict[str, Any],
        *,
        started_at: str,
        finished_at: str,
        duration_ms: int,
    ) -> dict[str, Any]:
        item = {
            "id": new_id("jobrun"),
            "job_id": job["id"],
            "title": job.get("title"),
            "kind": job.get("kind"),
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "ok": bool(result.get("ok")),
            "summary": str(result.get("summary") or "")[:500],
            "job_status": result.get("job_status"),
            "priority": _bounded_int(job.get("priority"), 0, 100, 0),
        }
        history = _list(self.storage.get_runtime_value(AUTONOMY_JOB_RUNS_KEY, []))
        self.storage.set_runtime_value(AUTONOMY_JOB_RUNS_KEY, [item, *history][:200])
        return item

    def routines(self) -> list[dict[str, Any]]:
        return [dict(item) for item in DEFAULT_ROUTINES]

    def record_routine_run(self, routine: dict[str, Any], result: dict[str, Any]) -> None:
        history = self.storage.get_runtime_value(ROUTINE_RUNS_KEY, [])
        item = {
            "id": new_id("routine_run"),
            "routine_id": routine["id"],
            "title": routine["title"],
            "ts": utc_now(),
            "ok": bool(result.get("ok")),
            "summary": result.get("summary"),
        }
        self.storage.set_runtime_value(ROUTINE_RUNS_KEY, [item, *_list(history)][:50])
        self.storage.add_event(
            kind="routine.run",
            title=str(item["summary"] or item["title"]),
            level="info" if item["ok"] else "warn",
            payload=item,
        )


def docker_container_allowed(policy: dict[str, Any], container: str) -> bool:
    lowered = container.lower()
    if not lowered:
        return False
    allowed = [str(item).lower() for item in _list(policy.get("allowed_containers"))]
    prefixes = [str(item).lower() for item in _list(policy.get("allowed_prefixes"))]
    return lowered in allowed or any(lowered.startswith(prefix) for prefix in prefixes)


def _normalize_browser_policy(value: dict[str, Any]) -> dict[str, Any]:
    mode = str(value.get("mode") or "open")
    if mode not in {"open", "approval-only", "local-safe", "locked"}:
        mode = "open"
    require_approval_for_external = bool(value.get("require_approval_for_external", False))
    if mode == "open":
        require_approval_for_external = False
    elif mode == "approval-only":
        require_approval_for_external = True
    return {
        "mode": mode,
        "allow_localhost": bool(value.get("allow_localhost", True)),
        "allowed_hosts": _clean_string_list(value.get("allowed_hosts"), ["localhost", "127.0.0.1"]),
        "blocked_schemes": _clean_string_list(
            value.get("blocked_schemes"),
            ["file", "javascript", "data"],
        ),
        "require_approval_for_external": require_approval_for_external,
        "max_urls_per_action": _bounded_int(value.get("max_urls_per_action"), 1, 20, 5),
    }


def _normalize_docker_policy(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "allowed_prefixes": _clean_string_list(
            value.get("allowed_prefixes"),
            ["jarvis-", "jarvis_", "jarvis-gpt"],
        ),
        "allowed_containers": _clean_string_list(
            value.get("allowed_containers"),
            ["jarvis-gpt-dispatcher"],
        ),
        "max_log_tail": _bounded_int(value.get("max_log_tail"), 10, 1000, 200),
        "include_stopped": bool(value.get("include_stopped", True)),
    }


def _normalize_job(value: dict[str, Any]) -> dict[str, Any]:
    kind = str(value.get("kind") or "diagnostics")
    if kind not in {"diagnostics", "learning.tick", "self_heal", "benchmark", "mission"}:
        kind = "diagnostics"
    status = str(value.get("status") or "enabled")
    if status not in {"enabled", "paused", "done", "cancelled"}:
        status = "enabled"
    budget = _dict(value.get("budget"))
    default_max_runs = 100 if kind == "mission" else 1
    return {
        "id": str(value.get("id") or new_id("job")),
        "title": str(value.get("title") or kind)[:120],
        "kind": kind,
        "status": status,
        "cadence": str(value.get("cadence") or "manual")[:80],
        "budget": {
            "max_runs": _bounded_int(budget.get("max_runs"), 1, 1000, default_max_runs),
            "max_minutes": _bounded_int(budget.get("max_minutes"), 1, 1440, 10),
        },
        "payload": _dict(value.get("payload")),
        "run_count": _bounded_int(value.get("run_count"), 0, 10000, 0),
        "priority": _bounded_int(value.get("priority"), 0, 100, 0),
        "consecutive_failures": _bounded_int(value.get("consecutive_failures"), 0, 1000, 0),
        "created_at": str(value.get("created_at") or utc_now()),
        "updated_at": str(value.get("updated_at") or utc_now()),
        "last_started_at": value.get("last_started_at"),
        "last_finished_at": value.get("last_finished_at"),
        "last_duration_ms": value.get("last_duration_ms"),
        "last_run_at": value.get("last_run_at"),
        "next_run_after": value.get("next_run_after"),
        "deadline_at": value.get("deadline_at"),
        "cancelled_at": value.get("cancelled_at"),
        "last_result": _dict(value.get("last_result")),
    }


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _clean_string_list(value: Any, fallback: list[str], *, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return fallback
    cleaned: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text[:180])
        if len(cleaned) >= limit:
            break
    return cleaned or fallback


def _bounded_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _job_is_due(job: dict[str, Any], now: datetime) -> bool:
    if job.get("status") != "enabled":
        return False
    budget = _dict(job.get("budget"))
    if int(job.get("run_count") or 0) >= _bounded_int(budget.get("max_runs"), 1, 1000, 1):
        return False
    deadline = _parse_datetime(job.get("deadline_at"))
    if deadline is not None and now > deadline:
        return False
    next_run_after = _parse_datetime(job.get("next_run_after"))
    if next_run_after is not None and now < next_run_after:
        return False
    cadence = str(job.get("cadence") or "manual").strip().lower()
    if cadence in {"", "manual", "off", "disabled"}:
        return False
    if cadence in {"once", "startup", "on-start"}:
        return int(job.get("run_count") or 0) == 0
    interval = _cadence_interval(cadence)
    if interval is None:
        return False
    last_run = _parse_datetime(job.get("last_run_at"))
    if last_run is None:
        return True
    return now - last_run >= interval


def _job_due_sort_key(job: dict[str, Any]) -> tuple[int, datetime, str]:
    created = _parse_datetime(job.get("created_at")) or datetime.now(UTC)
    return (-_bounded_int(job.get("priority"), 0, 100, 0), created, str(job.get("id") or ""))


def _cadence_interval(cadence: str) -> timedelta | None:
    aliases = {
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "background": timedelta(minutes=15),
    }
    if cadence in aliases:
        return aliases[cadence]
    text = cadence.removeprefix("interval:").removeprefix("every ").strip()
    if text.isdigit():
        return timedelta(minutes=max(1, int(text)))
    unit = text[-1:] if text else ""
    raw_value = text[:-1]
    if not raw_value.isdigit():
        return None
    value = max(1, int(raw_value))
    if unit == "s":
        return timedelta(seconds=value)
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso_after(value: str, delta: timedelta) -> str:
    base = _parse_datetime(value) or datetime.now(UTC)
    return (base + delta).isoformat(timespec="seconds")


def _retry_delay(consecutive_failures: int) -> timedelta:
    minutes = min(60, 2 ** max(0, min(6, consecutive_failures - 1)))
    return timedelta(minutes=minutes)


def _run_docker(args: list[str], *, timeout: int) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        return {
            "ok": False,
            "summary": "Docker is not available in PATH.",
            "stdout": "",
            "stderr": "docker not found",
            "command": ["docker", *args],
            "returncode": None,
        }
    command = [docker, *args]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "summary": f"Docker command failed: {exc}",
            "stdout": "",
            "stderr": str(exc),
            "command": command,
            "returncode": None,
        }
    return {
        "ok": result.returncode == 0,
        "summary": f"Docker exited with {result.returncode}.",
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "command": command,
        "returncode": result.returncode,
    }


def _parse_docker_ps(stdout: str) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(
            {
                "id": item.get("ID"),
                "name": item.get("Names"),
                "image": item.get("Image"),
                "status": item.get("Status"),
                "state": item.get("State"),
                "ports": item.get("Ports"),
                "created_at": item.get("CreatedAt"),
            }
        )
    return containers
