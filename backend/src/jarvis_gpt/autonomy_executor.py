from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import Any

from .agent import AgentRuntime
from .diagnostics import run_diagnostics
from .dispatcher import DispatcherManager
from .event_bus import EventBus
from .experience import ExperienceManager
from .learning import LearningEngine
from .llm import LLMRouter
from .operations import OperationsManager
from .storage import JarvisStorage, new_id, utc_now
from .telemetry import TelemetryCollector
from .tools import _web_watch_state_key as _watch_state_key


class AutonomyExecutor:
    """Executes persisted autonomy jobs without assuming a visible UI request.

    The executor is intentionally budgeted and stateful: it serializes runs per
    job id, writes the final job state through OperationsManager, and keeps
    risky work inside the existing mission/tool approval gates.
    """

    def __init__(
        self,
        *,
        settings: Any,
        storage: JarvisStorage,
        operations: OperationsManager,
        agent: AgentRuntime,
        experience: ExperienceManager,
        llm: LLMRouter,
        telemetry: TelemetryCollector,
        dispatcher: DispatcherManager,
        learning: LearningEngine,
        bus: EventBus | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.operations = operations
        self.agent = agent
        self.experience = experience
        self.llm = llm
        self.telemetry = telemetry
        self.dispatcher = dispatcher
        self.learning = learning
        self.bus = bus
        self._running_job_ids: set[str] = set()
        self._running_tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._cancelled_job_ids: set[str] = set()
        self._run_lock = asyncio.Lock()

    async def run_due_jobs(self, *, limit: int = 1) -> list[dict[str, Any]]:
        results = []
        for job in self.operations.due_jobs(limit=limit):
            results.append(await self.run_job(job))
        return results

    async def run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job.get("id") or "")
        if not job_id:
            return {
                "ok": False,
                "summary": "Autonomy job has no id.",
                "data": {"job": job},
            }
        async with self._run_lock:
            if job_id in self._running_job_ids:
                return {
                    "job": job,
                    "ok": False,
                    "summary": "Autonomy job is already running.",
                    "data": {"job_id": job_id},
                }
            current = next(
                (item for item in self.operations.list_jobs() if item.get("id") == job_id),
                None,
            )
            if current is None:
                return {
                    "job": job,
                    "ok": False,
                    "summary": "Autonomy job no longer exists.",
                    "data": {"job_id": job_id},
                }
            job = current
            if job.get("status") != "enabled":
                return {
                    "job": job,
                    "ok": False,
                    "summary": f"Autonomy job is {job.get('status')}.",
                    "data": {"job_id": job_id},
                }
            if job.get("running_lease_id"):
                return {
                    "job": job,
                    "ok": False,
                    "summary": "Autonomy job already has a running lease.",
                    "data": {
                        "job_id": job_id,
                        "lease_id": job.get("running_lease_id"),
                    },
                }
            self._running_job_ids.add(job_id)
        try:
            started_at = utc_now()
            started_perf = time.perf_counter()
            lease_id = new_id("joblease")
            lease_until = _lease_until(job)
            self.operations.mark_job_started(
                job_id,
                lease_id=lease_id,
                started_at=started_at,
                lease_until=lease_until,
            )
            if _deadline_expired(job.get("deadline_at")):
                result = {
                    "ok": False,
                    "summary": "Autonomy job deadline expired before execution.",
                    "data": {"job_id": job_id, "deadline_at": job.get("deadline_at")},
                    "job_status": "cancelled",
                }
            else:
                try:
                    timeout = _job_timeout_seconds(job)
                    task = asyncio.create_task(
                        self.run_kind(str(job.get("kind") or ""), job.get("payload") or {})
                    )
                    async with self._run_lock:
                        self._running_tasks[job_id] = task
                        if job_id in self._cancelled_job_ids:
                            task.cancel()
                    if timeout:
                        result = await asyncio.wait_for(task, timeout=timeout)
                    else:
                        result = await task
                except asyncio.CancelledError:
                    async with self._run_lock:
                        explicitly_cancelled = job_id in self._cancelled_job_ids
                    if not explicitly_cancelled:
                        raise
                    result = {
                        "ok": False,
                        "summary": "Autonomy job was cancelled while running.",
                        "data": {"job_id": job_id, "lease_id": lease_id},
                        "job_status": "cancelled",
                    }
                except TimeoutError:
                    result = {
                        "ok": False,
                        "summary": "Autonomy job exceeded its runtime budget.",
                        "data": {"job_id": job_id, "timeout_sec": _job_timeout_seconds(job)},
                        "job_status": "enabled",
                    }
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "ok": False,
                        "summary": f"Autonomy job failed: {exc}",
                        "data": {"error": repr(exc), "job_id": job_id},
                        "job_status": "enabled",
                    }
            finished_at = utc_now()
            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            if job.get("kind") == "mission":
                try:
                    self._persist_mission_payload(job, result)
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "ok": False,
                        "summary": f"Mission job state persist failed: {exc}",
                        "data": {
                            "error": repr(exc),
                            "job_id": job_id,
                            "previous_result": result,
                        },
                        "job_status": "enabled",
                    }
            updated = self.operations.mark_job_run(
                job_id,
                result,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
            ) or job
            run_record = self.operations.record_job_run(
                job,
                result,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
            )
            response = {"job": updated, **result}
            self.storage.add_event(
                kind="autonomy.job.run",
                title=str(result.get("summary") or job.get("title") or job_id)[:240],
                level="info" if result.get("ok") else "warn",
                payload={
                    "job_id": job_id,
                    "kind": job.get("kind"),
                    "status": updated.get("status"),
                    "ok": bool(result.get("ok")),
                    "duration_ms": duration_ms,
                    "run_id": run_record["id"],
                },
            )
            if self.bus:
                await self.bus.publish(
                    {
                        "channel": "autonomy.jobs",
                        "action": "run",
                        "job_id": job_id,
                        "ok": bool(result.get("ok")),
                        "status": updated.get("status"),
                    }
                )
            return response
        finally:
            async with self._run_lock:
                self._running_job_ids.discard(job_id)
                self._running_tasks.pop(job_id, None)
                self._cancelled_job_ids.discard(job_id)

    async def cancel_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.operations.update_job(job_id, {"status": "cancelled"})
        if job is None:
            return None
        async with self._run_lock:
            task = self._running_tasks.get(job_id)
            if job_id in self._running_job_ids:
                self._cancelled_job_ids.add(job_id)
        if task is not None and not task.done():
            task.cancel()
        if self.bus:
            await self.bus.publish(
                {"channel": "autonomy.jobs", "action": "cancelled", "job_id": job_id}
            )
        return job

    async def run_kind(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        if kind == "briefing":
            dispatcher_status = await asyncio.to_thread(self.dispatcher.status)
            report = self.experience.daily_briefing(dispatcher_status=dispatcher_status)
            return {"ok": True, "summary": report["headline"], "data": report}
        if kind == "diagnostics":
            result = await run_diagnostics(
                settings=self.settings,
                storage=self.storage,
                llm=self.llm,
            )
            warn_count = sum(1 for check in result.checks if check.status == "warn")
            error_count = sum(1 for check in result.checks if check.status == "error")
            return {
                "ok": result.ok,
                "summary": f"Diagnostics: {error_count} error(s), {warn_count} warning(s).",
                "data": result.model_dump(),
            }
        if kind == "learning.tick":
            limit = _bounded_int(payload.get("limit"), 5, 100, 20)
            result = await self.learning.tick_async(limit=limit)
            return {
                "ok": True,
                "summary": f"Learning tick saved {result['lesson_count']} lesson(s).",
                "data": result,
            }
        if kind == "self_heal":
            diagnostics_result = await run_diagnostics(
                settings=self.settings,
                storage=self.storage,
                llm=self.llm,
            )
            telemetry_snapshot = await asyncio.to_thread(self.telemetry.snapshot)
            self.storage.record_telemetry(telemetry_snapshot)
            dispatcher_status = await asyncio.to_thread(self.dispatcher.status)
            report = self.experience.self_heal_report(
                checks=diagnostics_result.checks,
                telemetry_snapshot=telemetry_snapshot,
                dispatcher_status=dispatcher_status,
            )
            return {"ok": bool(report["ok"]), "summary": report["summary"], "data": report}
        if kind == "benchmark":
            report = await self.experience.run_benchmark(
                llm=self.llm,
                telemetry=self.telemetry,
                dispatcher=self.dispatcher,
            )
            return {"ok": True, "summary": report["summary"], "data": report}
        if kind == "mission":
            return await self._run_mission(payload)
        if kind == "web.watch":
            return await self._run_web_watch(payload)
        if kind == "background.missions":
            results = await self.run_due_jobs(limit=_bounded_int(payload.get("limit"), 1, 10, 2))
            ok = all(item.get("ok") for item in results)
            return {
                "ok": ok,
                "summary": f"Background mission sweep ran {len(results)} job(s).",
                "data": {"jobs": results},
            }
        return {
            "ok": False,
            "summary": f"Unsupported operation kind: {kind}",
            "data": {"kind": kind},
        }

    async def _run_web_watch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Re-fetch a watched public page and raise a signal when it changes.

        The watch state lives in runtime KV keyed by url+pattern, so the job can
        be recreated without losing the baseline. A change produces a warn-level
        ``web.watch`` event, a durable memory, and a bus publish; fetch failures
        keep the job enabled so a temporary block does not kill the watch.
        """

        url = _optional_text(payload.get("url"))
        if not url:
            return {
                "ok": False,
                "summary": "web.watch job needs a url in its payload.",
                "job_status": "paused",
                "data": {"payload": payload},
            }
        label = _optional_text(payload.get("label")) or url
        pattern = _optional_text(payload.get("pattern"))
        fetch = await self.agent.tools.run("web.fetch", {"url": url, "max_chars": 12000})
        if not fetch.ok:
            return {
                "ok": False,
                "summary": f"web.watch could not read {label}: {fetch.summary}",
                "data": {"url": url, "changed": False},
            }
        text = str((fetch.data or {}).get("text") or "")
        observed = " ".join(text.split())[:4000]
        if pattern:
            try:
                match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            except re.error as exc:
                return {
                    "ok": False,
                    "summary": f"web.watch pattern is invalid: {exc}",
                    "job_status": "paused",
                    "data": {"url": url, "pattern": pattern},
                }
            observed = " ".join(match.group(0).split())[:1000] if match else ""
        digest = hashlib.sha256(observed.encode("utf-8", errors="replace")).hexdigest()
        state_key = _watch_state_key(url, pattern)
        state = self.storage.get_runtime_value(state_key, None)
        now = utc_now()
        if not isinstance(state, dict) or not state.get("digest"):
            self.storage.set_runtime_value(
                state_key,
                {
                    "url": url,
                    "label": label,
                    "digest": digest,
                    "observed": observed[:1000],
                    "checked_at": now,
                    "changed_at": None,
                },
            )
            return {
                "ok": True,
                "summary": f"Watch baseline captured: {label}.",
                "data": {"url": url, "changed": False, "baseline": True},
            }
        if state.get("digest") == digest:
            self.storage.set_runtime_value(state_key, {**state, "checked_at": now})
            return {
                "ok": True,
                "summary": f"No change: {label}.",
                "data": {"url": url, "changed": False},
            }
        previous = str(state.get("observed") or "")
        self.storage.set_runtime_value(
            state_key,
            {
                **state,
                "digest": digest,
                "observed": observed[:1000],
                "checked_at": now,
                "changed_at": now,
            },
        )
        self.storage.add_event(
            kind="web.watch",
            title=f"Изменение на странице: {label}"[:240],
            level="warn",
            payload={
                "url": url,
                "current": observed[:300],
                "previous": previous[:300],
            },
        )
        self.storage.add_memory(
            content=(
                f"Web watch «{label}» ({url}) зафиксировал изменение: "
                f"было «{previous[:200]}», стало «{observed[:200]}»."
            ),
            namespace="web",
            tags=["web", "watch"],
            importance=0.7,
        )
        if self.bus:
            await self.bus.publish(
                {
                    "channel": "agent",
                    "type": "web.watch",
                    "title": f"Изменение на странице: {label}"[:240],
                    "payload": {"url": url},
                }
            )
        return {
            "ok": True,
            "summary": f"Change detected: {label}.",
            "data": {
                "url": url,
                "changed": True,
                "current": observed[:300],
                "previous": previous[:300],
            },
        }

    async def _run_mission(self, payload: dict[str, Any]) -> dict[str, Any]:
        mission_id = _optional_text(payload.get("mission_id"))
        created = False
        if mission_id:
            mission = self.storage.get_mission(mission_id)
            if mission is None:
                return {
                    "ok": False,
                    "summary": "Background mission not found.",
                    "job_status": "paused",
                    "data": {"mission_id": mission_id},
                }
        else:
            goal = _optional_text(payload.get("goal"))
            if not goal:
                return {
                    "ok": False,
                    "summary": "Background mission job needs mission_id or goal.",
                    "job_status": "paused",
                    "data": {"payload": payload},
                }
            mission = await self.agent.create_mission_planned(
                goal,
                title=_optional_text(payload.get("title")),
            )
            mission_id = str(mission["id"])
            created = True

        if mission.get("status") == "blocked":
            return {
                "ok": False,
                "summary": f"Background mission is blocked: {mission.get('title')}",
                "job_status": "paused",
                "data": {"mission_id": mission_id, "mission": mission},
            }
        if mission.get("status") == "done":
            return {
                "ok": True,
                "summary": f"Background mission is already complete: {mission.get('title')}",
                "job_status": "done",
                "data": {"mission_id": mission_id, "mission": mission, "completed": True},
            }

        max_steps = _optional_int(payload.get("max_steps"))
        result = await self.agent.run_mission(mission_id, max_steps=max_steps)
        mission_status = result.mission.status
        completed = mission_status == "done"
        blocked = result.stopped_reason == "blocked" or mission_status == "blocked"
        if completed:
            job_status = "done"
        elif blocked:
            job_status = "paused"
        else:
            job_status = "enabled"
        summary = (
            f"Background mission '{result.mission.title}' ran {result.executed_steps} step(s); "
            f"stopped={result.stopped_reason}, status={mission_status}."
        )
        return {
            "ok": not blocked,
            "summary": summary,
            "job_status": job_status,
            "data": {
                "mission_id": mission_id,
                "created": created,
                "completed": completed,
                "blocked": blocked,
                "mission_run": result.model_dump(),
            },
        }

    def _persist_mission_payload(self, job: dict[str, Any], result: dict[str, Any]) -> None:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        mission_id = _optional_text(data.get("mission_id"))
        if not mission_id:
            return
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if payload.get("mission_id") == mission_id:
            return
        self.operations.update_job(job["id"], {"payload": {**payload, "mission_id": mission_id}})


def _bounded_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _job_timeout_seconds(job: dict[str, Any]) -> float | None:
    budget = job.get("budget") if isinstance(job.get("budget"), dict) else {}
    minutes = _bounded_int(budget.get("max_minutes"), 0, 1440, 0)
    return float(minutes * 60) if minutes > 0 else None


def _lease_until(job: dict[str, Any]) -> str:
    from datetime import UTC, datetime, timedelta

    timeout = _job_timeout_seconds(job) or 600.0
    # Add breathing room so a healthy long call is not recovered as stale while
    # wait_for is still enforcing its own stricter runtime budget.
    return (datetime.now(UTC) + timedelta(seconds=timeout + 120)).isoformat(timespec="seconds")


def _deadline_expired(value: Any) -> bool:
    if not value:
        return False
    from datetime import UTC, datetime

    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return datetime.now(UTC) > parsed.astimezone(UTC)


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(24, parsed))


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
