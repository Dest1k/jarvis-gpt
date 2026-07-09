from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Any

from .config import JarvisSettings
from .diagnostics import run_diagnostics
from .learning import LearningEngine
from .llm import LLMRouter
from .storage import JarvisStorage, utc_now
from .telemetry import TelemetryCollector


class RuntimeSupervisor:
    def __init__(
        self,
        *,
        settings: JarvisSettings,
        storage: JarvisStorage,
        llm: LLMRouter | None = None,
        autonomy_executor: Any | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.llm = llm or LLMRouter(settings)
        self.autonomy_executor = autonomy_executor
        self.telemetry = TelemetryCollector(settings)
        self.learning = LearningEngine(storage, llm=self.llm)
        self._tasks: list[asyncio.Task[None]] = []
        self._started_at: str | None = None
        self._last_telemetry_at: str | None = None
        self._last_health_at: str | None = None
        self._last_learning_at: str | None = None
        self._last_cognition_at: str | None = None
        self._last_cognition_error: str | None = None
        self._last_background_job_at: str | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        self._started_at = utc_now()
        if self.autonomy_executor is not None:
            try:
                stale = self.autonomy_executor.operations.recover_stale_running_jobs()
            except Exception as exc:  # noqa: BLE001
                stale = []
                self.storage.add_event(
                    kind="autonomy.recover",
                    title=f"Autonomy stale lease recovery failed: {exc}",
                    level="warn",
                )
            if stale:
                self.storage.add_event(
                    kind="autonomy.recover",
                    title=f"Recovered {len(stale)} stale autonomy job lease(s).",
                    level="warn",
                    payload={"job_ids": [item.get("id") for item in stale]},
                )
        if not self.settings.autonomy_enabled:
            self.storage.add_event(
                kind="autonomy.disabled",
                title="Runtime supervisor is disabled",
                payload=self.status(),
            )
            return
        self._tasks = [
            asyncio.create_task(self._telemetry_loop(), name="jarvis-telemetry-loop"),
            asyncio.create_task(self._health_loop(), name="jarvis-health-loop"),
            asyncio.create_task(self._learning_loop(), name="jarvis-learning-loop"),
        ]
        if self.settings.cognition_enabled and self.settings.llm_enabled:
            self._tasks.append(
                asyncio.create_task(self._cognition_loop(), name="jarvis-cognition-loop")
            )
        if self.autonomy_executor is not None:
            self._tasks.append(
                asyncio.create_task(self._background_job_loop(), name="jarvis-background-jobs")
            )
        self.storage.add_event(
            kind="autonomy.start",
            title="Runtime supervisor started",
            payload=self.status(),
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        self.storage.add_event(
            kind="autonomy.stop",
            title="Runtime supervisor stopped",
            payload=self.status(),
        )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.autonomy_enabled,
            "started_at": self._started_at,
            "running_tasks": [task.get_name() for task in self._tasks if not task.done()],
            "telemetry_interval_sec": self.settings.telemetry_interval_sec,
            "health_interval_sec": self.settings.health_interval_sec,
            "learning_interval_sec": self.settings.learning_interval_sec,
            "cognition_enabled": self.settings.cognition_enabled,
            "cognition_interval_sec": self.settings.cognition_interval_sec,
            "cognition_max_tokens": self.settings.cognition_max_tokens,
            "mission_interval_sec": self.settings.autonomy_mission_interval_sec,
            "last_telemetry_at": self._last_telemetry_at,
            "last_health_at": self._last_health_at,
            "last_learning_at": self._last_learning_at,
            "last_cognition_at": self._last_cognition_at,
            "last_cognition_error": self._last_cognition_error,
            "last_background_job_at": self._last_background_job_at,
            "last_error": self._last_error,
            "capabilities": [
                "telemetry.persist",
                "health.persist",
                "learning.tick",
                "learning.observe_dialogues",
                "learning.observe_web",
                "learning.append_only_journal",
                "learning.deduplicate",
                "cognition.background_pulse",
                "cognition.observe_runtime",
                "cognition.persist_insights",
                "background.mission.scheduler",
                "background.mission.runner",
                "background.mission.budgeted",
                "audit.observe",
                "approval.respect",
            ],
        }

    async def _telemetry_loop(self) -> None:
        await self._record_telemetry()
        while True:
            await asyncio.sleep(max(30, self.settings.telemetry_interval_sec))
            await self._record_telemetry()

    async def _learning_loop(self) -> None:
        await self._run_learning()
        while True:
            await asyncio.sleep(max(60, self.settings.learning_interval_sec))
            await self._run_learning()

    async def _cognition_loop(self) -> None:
        await self._run_cognition()
        while True:
            await asyncio.sleep(max(60, self.settings.cognition_interval_sec))
            await self._run_cognition()

    async def _health_loop(self) -> None:
        await self._record_health()
        while True:
            await asyncio.sleep(max(60, self.settings.health_interval_sec))
            await self._record_health()

    async def _background_job_loop(self) -> None:
        await self._run_background_jobs()
        while True:
            await asyncio.sleep(max(30, self.settings.autonomy_mission_interval_sec))
            await self._run_background_jobs()

    async def _record_telemetry(self) -> None:
        try:
            snapshot = await asyncio.to_thread(self.telemetry.snapshot)
            self.storage.record_telemetry(snapshot)
            self._last_telemetry_at = str(snapshot["ts"])
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc) or exc.__class__.__name__
            self.storage.add_event(
                kind="autonomy.error",
                title="Telemetry loop failed",
                level="warn",
                payload={"error": self._last_error},
            )

    async def _record_health(self) -> None:
        try:
            result = await run_diagnostics(
                settings=self.settings,
                storage=self.storage,
                llm=self.llm,
                persist=True,
            )
            self._last_health_at = utc_now()
            error_count = sum(1 for check in result.checks if check.status == "error")
            warn_count = sum(1 for check in result.checks if check.status == "warn")
            self.storage.add_event(
                kind="autonomy.health",
                title=f"Autonomous health snapshot: {error_count} error(s), {warn_count} warn(s)",
                level="info" if error_count == 0 else "warn",
                payload={"checks": len(result.checks), "ok": result.ok},
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc) or exc.__class__.__name__
            self.storage.add_event(
                kind="autonomy.error",
                title="Health loop failed",
                level="warn",
                payload={"error": self._last_error},
            )

    async def _run_learning(self) -> None:
        try:
            result = await self.learning.tick_async()
            self._last_learning_at = utc_now()
            self.storage.add_event(
                kind="autonomy.learning",
                title=f"Autonomous learning saved {result['lesson_count']} lesson(s)",
                payload=result["examined"],
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc) or exc.__class__.__name__
            self.storage.add_event(
                kind="autonomy.error",
                title="Learning loop failed",
                level="warn",
                payload={"error": self._last_error},
            )

    async def _run_cognition(self) -> None:
        if not self.settings.llm_enabled or not hasattr(self.llm, "complete"):
            return
        try:
            messages = _cognition_messages(self.storage)
            result = await asyncio.wait_for(
                self.llm.complete(
                    messages,
                    temperature=0.2,
                    max_tokens=max(128, min(2048, self.settings.cognition_max_tokens)),
                    thinking_enabled=False,
                ),
                timeout=min(90.0, max(20.0, self.settings.llm_timeout_sec)),
            )
            if not result.ok:
                self._last_cognition_error = str(result.error or "cognition failed")
                self.storage.add_event(
                    kind="cognition.error",
                    title="Background cognition failed",
                    level="warn",
                    payload={"error": self._last_cognition_error},
                )
                return
            pulse = _normalize_cognition_payload(result.content)
            self._last_cognition_at = utc_now()
            self._last_cognition_error = None
            self.storage.set_runtime_value("cognition.last_pulse", pulse)
            self.storage.record_learning_observation(
                kind="cognition.pulse",
                content=_cognition_content(pulse),
                summary=str(pulse.get("summary") or "Background cognition pulse"),
                payload=pulse,
            )
            self.storage.add_event(
                kind="cognition.pulse",
                title=str(pulse.get("summary") or "Background cognition pulse")[:240],
                payload={
                    "insights": pulse.get("insights", []),
                    "questions": pulse.get("questions", []),
                    "suggested_jobs": pulse.get("suggested_jobs", []),
                },
            )
        except Exception as exc:  # noqa: BLE001
            self._last_cognition_error = str(exc) or exc.__class__.__name__
            self.storage.add_event(
                kind="cognition.error",
                title="Background cognition loop failed",
                level="warn",
                payload={"error": self._last_cognition_error},
            )

    async def _run_background_jobs(self) -> None:
        if self.autonomy_executor is None:
            return
        try:
            results = await self.autonomy_executor.run_due_jobs(limit=1)
            self._last_background_job_at = utc_now()
            if results:
                self.storage.add_event(
                    kind="autonomy.background",
                    title=f"Background autonomy ran {len(results)} due job(s)",
                    payload={
                        "jobs": [
                            {
                                "job_id": (item.get("job") or {}).get("id"),
                                "kind": (item.get("job") or {}).get("kind"),
                                "ok": item.get("ok"),
                                "summary": item.get("summary"),
                            }
                            for item in results
                            if isinstance(item, dict)
                        ]
                    },
                )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc) or exc.__class__.__name__
            self.storage.add_event(
                kind="autonomy.error",
                title="Background job loop failed",
                level="warn",
                payload={"error": self._last_error},
            )


def _cognition_messages(storage: JarvisStorage) -> list[dict[str, str]]:
    counters = storage.counters()
    events = storage.list_events(limit=12)
    observations = storage.list_learning_observations(limit=16)
    jobs = storage.get_runtime_value("operations.autonomy.jobs", [])
    job_rows = jobs[:10] if isinstance(jobs, list) else []
    lines = [
        "Runtime counters:",
        json.dumps(counters, ensure_ascii=False, separators=(",", ":")),
        "Recent events:",
    ]
    for item in events:
        lines.append(
            "- "
            + "; ".join(
                [
                    str(item.get("ts") or ""),
                    str(item.get("kind") or ""),
                    str(item.get("level") or ""),
                    _compact(str(item.get("title") or ""), 180),
                ]
            )
        )
    lines.append("Recent learning observations:")
    for item in observations[:10]:
        observation_summary = str(item.get("summary") or item.get("content") or "")
        lines.append(
            f"- {item.get('kind')}: {_compact(observation_summary, 220)}"
        )
    lines.append("Autonomy jobs:")
    for job in job_rows:
        if isinstance(job, dict):
            lines.append(
                f"- {job.get('title')} [{job.get('kind')}/{job.get('status')}]: "
                f"{_compact(str((job.get('last_result') or {}).get('summary') or ''), 160)}"
            )
    return [
        {
            "role": "system",
            "content": (
                "You are JARVIS background cognition. Observe recent runtime signals while the "
                "operator is away. Do not perform actions, do not browse, and do not invent facts. "
                "Return strict JSON only with keys: summary (short), insights (array of up to 3 "
                "grounded strings), questions (array of up to 2 useful operator questions), "
                "suggested_jobs (array of up to 2 safe autonomy job suggestions with title, kind, "
                "cadence, priority, payload). Allowed job kinds: diagnostics, learning.tick, "
                "self_heal, benchmark, mission."
            ),
        },
        {"role": "user", "content": "\n".join(lines)[:6000]},
    ]


def _normalize_cognition_payload(content: str) -> dict[str, Any]:
    try:
        data = json.loads(_json_object_text(content))
    except Exception:  # noqa: BLE001
        data = {
            "summary": _compact(content, 240),
            "insights": [],
            "questions": [],
            "suggested_jobs": [],
        }
    summary = _compact(str(data.get("summary") or "Background cognition pulse"), 240)
    insights = [_compact(str(item), 240) for item in _list(data.get("insights"))[:3] if str(item)]
    questions = [_compact(str(item), 220) for item in _list(data.get("questions"))[:2] if str(item)]
    suggested_jobs = []
    for item in _list(data.get("suggested_jobs"))[:2]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind not in {"diagnostics", "learning.tick", "self_heal", "benchmark", "mission"}:
            continue
        suggested_jobs.append(
            {
                "title": _compact(str(item.get("title") or kind), 120),
                "kind": kind,
                "cadence": _compact(str(item.get("cadence") or "manual"), 40),
                "priority": _bounded_int(item.get("priority"), 0, 100, 25),
                "payload": item.get("payload") if isinstance(item.get("payload"), dict) else {},
            }
        )
    return {
        "summary": summary,
        "insights": insights,
        "questions": questions,
        "suggested_jobs": suggested_jobs,
        "source": "background_cognition",
        "saved_at": utc_now(),
    }


def _cognition_content(pulse: dict[str, Any]) -> str:
    lines = [str(pulse.get("summary") or "Background cognition pulse")]
    for insight in _list(pulse.get("insights")):
        lines.append(f"Insight: {insight}")
    for question in _list(pulse.get("questions")):
        lines.append(f"Question: {question}")
    for job in _list(pulse.get("suggested_jobs")):
        if isinstance(job, dict):
            lines.append(f"Suggested job: {job.get('title')} ({job.get('kind')})")
    return "\n".join(lines)


def _json_object_text(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("No JSON object found")
    return text[start : end + 1]


def _compact(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _bounded_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))
