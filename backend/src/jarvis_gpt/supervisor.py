from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from .authorization import (
    CORE_CAPABILITIES,
    LEGACY_OWNER_USER_ID,
    ActorContext,
    AuthorizationService,
    bind_actor,
    current_user_id,
)
from .config import JarvisSettings
from .diagnostics import run_diagnostics
from .learning import LearningEngine
from .llm import LLMRouter, background_llm_priority
from .notify import in_quiet_hours, push_telegram_alert, telegram_targets
from .ocr import extract_ocr_job
from .storage import JarvisStorage, utc_now
from .telemetry import TelemetryCollector

_QUIET_DEFERRED_KEY = "telegram.quiet_deferred"


def _parse_utc_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _notification_chat_ids(value: Any) -> tuple[int, ...]:
    """Decode a persisted recipient list without accepting bools or junk values."""

    if not isinstance(value, list | tuple):
        return ()
    ids: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        try:
            chat_id = int(item)
        except (TypeError, ValueError):
            continue
        if chat_id not in ids:
            ids.append(chat_id)
    return tuple(ids)


def _actor_telegram_chat_ids(storage: JarvisStorage) -> tuple[int, ...]:
    """Resolve only the current user's Telegram private identity."""

    user_id = current_user_id()
    if user_id == LEGACY_OWNER_USER_ID:
        return ()
    with storage.locked_connection() as conn:
        rows = conn.execute(
            """
            SELECT provider_subject_id
            FROM external_identities
            WHERE user_id = ? AND provider = 'telegram'
            ORDER BY last_seen_at DESC
            """,
            (user_id,),
        ).fetchall()
    return _notification_chat_ids([row["provider_subject_id"] for row in rows])


def _evaluate_alerts(
    snapshot: dict[str, Any],
    *,
    gpu_temp_c: float,
    gpu_vram_ratio: float,
    disk_ratio: float,
    memory_ratio: float,
) -> dict[str, dict[str, Any]]:
    """Compute the set of *currently breached* health thresholds from a telemetry snapshot.

    Pure and defensive: every field is probed with ``.get`` and skipped when absent
    (an offline nvidia-smi, a platform without a memory probe) so a partial snapshot
    only narrows coverage — it never raises. Returns ``{key: alert}`` where each alert
    carries a human ``title``, ``detail``, ``level`` and the offending ``value``.
    """

    active: dict[str, dict[str, Any]] = {}

    gpu = snapshot.get("gpu") or {}
    gpus = gpu.get("gpus") or [] if isinstance(gpu, dict) else []
    hottest = None
    fullest = None
    for card in gpus:
        if not isinstance(card, dict):
            continue
        temp = card.get("temperature_c")
        if temp is not None and (hottest is None or temp > hottest.get("temperature_c", -1)):
            hottest = card
        ratio = card.get("memory_used_ratio")
        if ratio is not None and (fullest is None or ratio > fullest.get("memory_used_ratio", -1)):
            fullest = card
    if hottest is not None and hottest.get("temperature_c", 0) >= gpu_temp_c:
        temp = hottest["temperature_c"]
        active["gpu_temp"] = {
            "level": "error" if temp >= gpu_temp_c + 8 else "warn",
            "title": f"GPU перегрев: {temp:.0f}°C",
            "detail": (
                f"{hottest.get('name', 'GPU')} достигла {temp:.0f}°C "
                f"(порог {gpu_temp_c:.0f}°C)."
            ),
            "value": temp,
        }
    if fullest is not None and fullest.get("memory_used_ratio", 0) >= gpu_vram_ratio:
        ratio = fullest["memory_used_ratio"]
        active["gpu_vram"] = {
            "level": "warn",
            "title": f"VRAM почти заполнена: {ratio * 100:.0f}%",
            "detail": (
                f"{fullest.get('name', 'GPU')} использует {ratio * 100:.0f}% видеопамяти "
                f"(порог {gpu_vram_ratio * 100:.0f}%) — риск OOM."
            ),
            "value": ratio,
        }

    worst_disk = None
    for disk in snapshot.get("disks") or []:
        if not isinstance(disk, dict):
            continue
        used = disk.get("used_ratio")
        if used is not None and (worst_disk is None or used > worst_disk.get("used_ratio", -1)):
            worst_disk = disk
    if worst_disk is not None and worst_disk.get("used_ratio", 0) >= disk_ratio:
        used = worst_disk["used_ratio"]
        free_gb = (worst_disk.get("free") or 0) / (1024**3)
        active["disk"] = {
            "level": "error" if used >= 0.98 else "warn",
            "title": f"Мало места на диске: {used * 100:.0f}% занято",
            "detail": (
                f"{worst_disk.get('path', 'диск')} заполнен на {used * 100:.0f}% "
                f"(свободно {free_gb:.1f} ГБ, порог {disk_ratio * 100:.0f}%)."
            ),
            "value": used,
        }

    memory = snapshot.get("memory") or {}
    mem_ratio = memory.get("used_ratio") if isinstance(memory, dict) else None
    if mem_ratio is not None and mem_ratio >= memory_ratio:
        active["memory"] = {
            "level": "warn",
            "title": f"Мало ОЗУ: {mem_ratio * 100:.0f}% занято",
            "detail": (
                f"Системная память использована на {mem_ratio * 100:.0f}% "
                f"(порог {memory_ratio * 100:.0f}%)."
            ),
            "value": mem_ratio,
        }

    return active


class RuntimeSupervisor:
    def __init__(
        self,
        *,
        settings: JarvisSettings,
        storage: JarvisStorage,
        llm: LLMRouter | None = None,
        autonomy_executor: Any | None = None,
        bus: Any | None = None,
        dispatcher: Any | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.authorization = AuthorizationService(storage)
        # Supervisor can run in isolation in CLI/tests; scheduled/background PEPs
        # must exist even when the FastAPI lifespan was not the initializer.
        self.authorization.sync_capabilities(CORE_CAPABILITIES, catalog_key="core.v1")
        self.llm = llm or LLMRouter(settings)
        self.autonomy_executor = autonomy_executor
        self.bus = bus
        # The dispatcher handle self-healing restarts. Prefer an injected one, fall back
        # to the executor's, and lazily construct one on first need (see
        # `_dispatcher_manager`) so a supervisor built without either can still heal.
        self.dispatcher = dispatcher or getattr(autonomy_executor, "dispatcher", None)
        self.telemetry = TelemetryCollector(settings)
        self.learning = LearningEngine(storage, llm=self.llm)
        self._tasks: list[asyncio.Task[None]] = []
        # Fire-and-forget scheduled-task runs, held so they are not GC'd mid-flight.
        self._scheduled_runs: set[asyncio.Task[None]] = set()
        # One task per watcher.  The reminder row is advanced before dispatch, so this map
        # is the in-process lease that prevents a slow VLM poll overlapping its next tick.
        self._screen_watch_runs: dict[str, asyncio.Task[None]] = {}
        # One sender per durable notification. A VLM run and the periodic outbox flush can
        # otherwise overlap while awaiting Telegram and send the same recipient twice.
        self._screen_watch_delivery_ids: set[str] = set()
        self._started_at: str | None = None
        self._last_telemetry_at: str | None = None
        self._last_health_at: str | None = None
        self._last_health_attempt_at: str | None = None
        self._last_health_attempt_ok: bool | None = None
        # A failed required readiness component is retried quickly until it recovers.
        # The regular diagnostics cadence can be several minutes and must not keep
        # /health stale after a long-running model finally becomes ready.
        self._health_recovery_pending = False
        self._last_learning_at: str | None = None
        self._last_cognition_at: str | None = None
        self._last_cognition_error: str | None = None
        self._last_background_job_at: str | None = None
        self._last_ocr_job_at: str | None = None
        self._last_ocr_error: str | None = None
        self._background_user_cursor = ""
        self._last_error: str | None = None
        # Edge-triggered health-alert state: key -> last-fired alert payload. Kept so a
        # standing breach alerts once (not every telemetry tick) and clearing it emits a
        # single recovery notice.
        self._alert_active: dict[str, dict[str, Any]] = {}
        # Self-healing state. `_self_heal_streak` counts consecutive unhealthy probes so a
        # single blip never triggers a restart; `_self_heal_history` holds the monotonic
        # timestamps of recent restarts for the rolling-window budget; `_self_heal_blocked`
        # is set when the dispatcher is down for a reason we must NOT auto-fix (owner
        # stopped it cleanly / no container / no docker) and cleared when it comes back;
        # `_self_heal_budget_alerted` makes the budget-exhausted escalation fire once.
        self._self_heal_streak = 0
        self._self_heal_history: list[float] = []
        self._self_heal_blocked = False
        self._self_heal_budget_alerted = False
        self._self_heal_count = 0
        self._last_self_heal_at: str | None = None
        # Observed Docker warmup deadline, anchored to the container's StartedAt. It is
        # diagnostic state only: health is still probed so a real crash is never hidden.
        self._self_heal_grace_until = 0.0

    async def start(self) -> None:
        self._started_at = utc_now()
        if self.autonomy_executor is not None:
            try:
                stale = self.autonomy_executor.operations.recover_stale_running_jobs()
            except Exception as exc:  # noqa: BLE001
                stale = []
                with suppress(Exception):
                    self.storage.add_event(
                        kind="autonomy.recover",
                        title=f"Autonomy stale lease recovery failed: {exc}",
                        level="warn",
                    )
            if stale:
                with suppress(Exception):
                    self.storage.add_event(
                        kind="autonomy.recover",
                        title=f"Recovered {len(stale)} stale autonomy job lease(s).",
                        level="warn",
                        payload={"job_ids": [item.get("id") for item in stale]},
                    )
        # Readiness must be based on a current snapshot, not on an empty table
        # or a result left by a previous process. This initial check also runs
        # when background autonomy is disabled; disabled LLM routing is recorded
        # as an intentional warning and interpreted by /health accordingly.
        await self._record_health()
        # Readiness monitoring is a runtime concern, not an autonomy feature.
        # Keep refreshing diagnostics even when autonomous jobs, learning, and
        # cognition have been disabled, otherwise /health can serve a stale
        # successful snapshot forever after a dependency fails.
        # Reminders are user-facing, not an autonomy feature: they must fire even when
        # background autonomy is disabled, so the loop is scheduled next to the health
        # loop, ahead of the autonomy early-return below.
        self._tasks = [
            asyncio.create_task(self._health_loop(), name="jarvis-health-loop"),
            asyncio.create_task(self._reminder_loop(), name="jarvis-reminder-loop"),
        ]
        if self.settings.llm_enabled and self.settings.profile.vision_capable:
            self._tasks.append(
                asyncio.create_task(self._ocr_job_loop(), name="jarvis-ocr-jobs")
            )
        # Self-healing is a reliability guarantee (keep the local brain alive), not an
        # opt-in autonomy behavior, so it runs even when background autonomy is disabled.
        if self.settings.self_healing_enabled:
            self._tasks.append(
                asyncio.create_task(self._self_heal_loop(), name="jarvis-self-heal-loop")
            )
        if not self.settings.autonomy_enabled:
            with suppress(Exception):
                self.storage.add_event(
                    kind="autonomy.disabled",
                    title="Runtime supervisor is disabled",
                    payload=self.status(),
                )
            return
        self._tasks.extend(
            [
                asyncio.create_task(
                    self._telemetry_loop(), name="jarvis-telemetry-loop"
                ),
                asyncio.create_task(
                    self._learning_loop(), name="jarvis-learning-loop"
                ),
            ]
        )
        if self.settings.cognition_enabled and self.settings.llm_enabled:
            self._tasks.append(
                asyncio.create_task(self._cognition_loop(), name="jarvis-cognition-loop")
            )
        if self.autonomy_executor is not None:
            self._tasks.append(
                asyncio.create_task(self._background_job_loop(), name="jarvis-background-jobs")
            )
        with suppress(Exception):
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
        watch_runs = list(self._screen_watch_runs.values())
        for task in watch_runs:
            task.cancel()
        for task in watch_runs:
            with suppress(asyncio.CancelledError):
                await task
        self._screen_watch_runs.clear()
        self._screen_watch_delivery_ids.clear()
        with suppress(Exception):
            self.storage.add_event(
                kind="autonomy.stop",
                title="Runtime supervisor stopped",
                payload=self.status(),
            )

    def status(self) -> dict[str, Any]:
        admission_status = getattr(self.llm, "admission_status", None)
        admission = admission_status() if callable(admission_status) else {}
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
            "last_health_attempt_at": self._last_health_attempt_at,
            "last_health_attempt_ok": self._last_health_attempt_ok,
            "health_recovery_pending": self._health_recovery_pending,
            "last_learning_at": self._last_learning_at,
            "last_cognition_at": self._last_cognition_at,
            "last_cognition_error": self._last_cognition_error,
            "last_background_job_at": self._last_background_job_at,
            "last_ocr_job_at": self._last_ocr_job_at,
            "last_ocr_error": self._last_ocr_error,
            "last_error": self._last_error,
            "self_healing_enabled": self.settings.self_healing_enabled,
            "self_heal_count": self._self_heal_count,
            "last_self_heal_at": self._last_self_heal_at,
            "llm_admission": admission,
            "capabilities": [
                "telemetry.persist",
                "health.persist",
                "health.alert.threshold",
                "health.alert.telegram",
                "health.self_heal.dispatcher_restart",
                "health.self_heal.restart_budget",
                "health.self_heal.telegram_escalation",
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
                "documents.ocr.queue",
                "reminders.scheduler",
                "reminders.fire",
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
        while True:
            await asyncio.sleep(max(60, self.settings.learning_interval_sec))
            await self._run_learning()

    async def _cognition_loop(self) -> None:
        while True:
            await asyncio.sleep(max(60, self.settings.cognition_interval_sec))
            await self._run_cognition()

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self._health_poll_interval())
            await self._record_health()

    def _health_poll_interval(self) -> float:
        regular = float(max(60, self.settings.health_interval_sec))
        # During warmup/recovery, converge persisted readiness quickly. This is bounded
        # and returns to the configured cadence as soon as every required component is ok.
        return min(regular, 15.0) if self._health_recovery_pending else regular

    async def _background_job_loop(self) -> None:
        while True:
            await asyncio.sleep(max(30, self.settings.autonomy_mission_interval_sec))
            await self._run_background_jobs()

    async def _ocr_job_loop(self) -> None:
        while True:
            try:
                processed = await self._run_ocr_job()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the durable worker alive.
                self._last_ocr_error = f"{type(exc).__name__}: {exc}"[:1000]
                processed = False
                with suppress(Exception):
                    self.storage.add_event(
                        kind="documents.ocr.worker_error",
                        title="Automatic OCR worker iteration failed",
                        level="warn",
                        payload={"error": self._last_ocr_error},
                    )
            await asyncio.sleep(1 if processed else 10)

    async def _run_ocr_job(self) -> bool:
        system_actor = ActorContext(
            user_id=LEGACY_OWNER_USER_ID,
            preset_key="owner",
            source="ocr-supervisor",
        )
        with bind_actor(system_actor):
            job = await asyncio.to_thread(
                self.storage.claim_next_file_ocr_job,
                worker_id="runtime-ocr",
                lease_seconds=3_600,
                all_users=True,
            )
        if job is None:
            return False
        actor = self.authorization.actor_for_user(
            str(job["user_id"]),
            source="ocr-supervisor",
        )
        if actor is None:
            # The queue claim is restricted to active users, so this can only be a
            # concurrent suspension/deletion. Leave the lease to expire safely.
            return True
        try:
            with bind_actor(actor), background_llm_priority(self.llm):
                result = await extract_ocr_job(
                    job,
                    self.llm,
                    profile_name=self.settings.profile.name,
                )
                completed = await asyncio.to_thread(
                    self.storage.complete_file_ocr_job,
                    str(job["id"]),
                    str(job["lease_token"]),
                    str(result["text"]),
                    source=str(result["source"]),
                    details=dict(result["details"]),
                    warning=str(result["warning"]) if result.get("warning") else None,
                )
                self._last_ocr_job_at = utc_now()
                self._last_ocr_error = None
                self.storage.add_event(
                    kind="documents.ocr.completed",
                    title="Automatic document OCR completed",
                    payload={
                        "file_id": job["file_id"],
                        "job_id": job["id"],
                        "result_status": completed.get("result_status"),
                    },
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - durable retry is the queue contract.
            self._last_ocr_error = f"{type(exc).__name__}: {exc}"[:1000]
            with bind_actor(actor), suppress(Exception):
                await asyncio.to_thread(
                    self.storage.fail_file_ocr_job,
                    str(job["id"]),
                    str(job["lease_token"]),
                    self._last_ocr_error,
                )
        return True

    async def _reminder_loop(self) -> None:
        while True:
            await asyncio.sleep(max(5, self.settings.reminder_interval_sec))
            await self._fire_due_reminders()

    async def _fire_due_reminders(self) -> None:
        """Claim and deliver every due reminder for this tick.

        SQLite is blocking under an RLock, so the atomic claim runs in a worker thread;
        the async EventBus publish stays on the loop. Delivery per reminder: a runtime
        event, an assistant message back into the originating conversation (if bound),
        and a live ``reminder.fire`` push so the UI updates immediately.
        """

        # Drain persisted notices before claiming new watches. A transient Telegram
        # outage therefore retries after restart and a recurring watcher cannot outrun
        # its previous notification.
        await self._flush_screen_watch_notifications()
        # Outside quiet hours, flush any passive reminders held overnight.
        with suppress(Exception):
            await self._flush_quiet_deferred_pushes()
        try:
            due = await asyncio.to_thread(
                self.storage.claim_due_reminders,
                utc_now(),
                tz_name=self.settings.reminder_tz,
                skip_ids=tuple(self._screen_watch_runs),
                excluded_payload_kinds=(
                    ("screen_watch",) if not self.settings.screen_watch_enabled else ()
                ),
                all_users=True,
            )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc) or exc.__class__.__name__
            with suppress(Exception):
                self.storage.add_event(
                    kind="autonomy.error",
                    title="Reminder loop failed",
                    level="warn",
                    payload={"error": self._last_error},
                )
            return
        authorization = self.authorization
        for reminder in due:
            actor = authorization.actor_for_user(
                str(reminder.get("user_id") or ""), source="reminder-scheduler"
            )
            if actor is None:
                continue
            await self._fire_due_reminder_as_actor(reminder, actor)

    async def _fire_due_reminder_as_actor(
        self, reminder: dict[str, Any], actor: ActorContext
    ) -> None:
        with bind_actor(actor):
            recurring = bool(reminder.get("recurrence"))
            payload = (
                reminder.get("payload")
                if isinstance(reminder.get("payload"), dict)
                else {}
            )
            is_screen_watch = payload.get("kind") == "screen_watch"
            if is_screen_watch:
                if not self.settings.screen_watch_enabled:
                    return
                reminder_id = str(reminder.get("id") or "")
                if not reminder_id or reminder_id in self._screen_watch_runs:
                    return
                authorization = self.authorization
                required_security_ids = (
                    "background.screen_watch.create",
                    "privacy.screen.capture",
                )
                denied: list[str] = []
                for security_id in required_security_ids:
                    try:
                        decision = authorization.authorize_current(
                            security_id,
                            resource_type="screen_watch",
                            resource_ref=reminder_id,
                            context={"operation": "scheduled_capture"},
                        )
                    except Exception:  # noqa: BLE001 - scheduler privacy checks fail closed.
                        denied.append(security_id)
                    else:
                        if not decision.allowed:
                            denied.append(security_id)
                if denied:
                    with suppress(Exception):
                        self.storage.cancel_reminder(reminder_id)
                    with suppress(Exception):
                        self.storage.add_event(
                            kind="screen_watch.permission_denied",
                            title="Наблюдение за экраном остановлено политикой доступа",
                            level="warn",
                            payload={
                                "reminder_id": reminder_id,
                                "missing_security_ids": denied,
                            },
                        )
                    return
                run = asyncio.create_task(
                    self._run_screen_watch(reminder),
                    name=f"jarvis-screen-watch-{reminder_id}",
                )
                self._screen_watch_runs[reminder_id] = run

                def _discard(
                    done: asyncio.Task[None], *, watched_id: str = reminder_id
                ) -> None:
                    if self._screen_watch_runs.get(watched_id) is done:
                        self._screen_watch_runs.pop(watched_id, None)
                    if done.cancelled():
                        return
                    error = done.exception()
                    if error is not None:
                        with suppress(Exception):
                            self.storage.add_event(
                                kind="screen_watch.error",
                                title="Screen-watch task failed",
                                level="warn",
                                payload={
                                    "reminder_id": watched_id,
                                    "error": (str(error) or error.__class__.__name__)[:500],
                                },
                            )

                run.add_done_callback(_discard)
                return
            kind = str(payload.get("kind") or "").strip().lower()
            is_briefing = kind == "briefing" and self.settings.scheduled_tasks_enabled
            is_task = (
                kind == "agent_task" and self.settings.scheduled_tasks_enabled
            ) or is_briefing
            with suppress(Exception):
                self.storage.add_event(
                    kind="reminder.fire",
                    title=f"Напоминание: {reminder['text']}",
                    level="info",
                    payload={
                        "reminder_id": reminder["id"],
                        "due_at": reminder["due_at"],
                        "recurring": recurring,
                        "agent_task": is_task and not is_briefing,
                        "briefing": is_briefing,
                        "conversation_id": reminder.get("conversation_id"),
                    },
                )
            conversation_id = reminder.get("conversation_id")
            if is_task:
                try:
                    execute_decision = self.authorization.authorize_current(
                        "background.scheduled_task.execute",
                        resource_type="scheduled_task",
                        resource_ref=str(reminder.get("id") or ""),
                        context={"operation": "fire"},
                    )
                except Exception:  # noqa: BLE001 - scheduled execution fails closed.
                    execute_decision = None
                if execute_decision is None or not execute_decision.allowed:
                    if recurring:
                        with suppress(Exception):
                            self.storage.cancel_reminder(str(reminder["id"]))
                    with suppress(Exception):
                        self.storage.add_event(
                            kind="scheduled_task.permission_denied",
                            title="Плановая задача заблокирована политикой доступа",
                            level="warn",
                            payload={
                                "reminder_id": reminder.get("id"),
                                "security_id": "background.scheduled_task.execute",
                            },
                        )
                    return
                runner = (
                    self._run_briefing_task if is_briefing else self._run_scheduled_task
                )
                run = asyncio.create_task(runner(reminder))
                self._scheduled_runs.add(run)
                run.add_done_callback(self._scheduled_runs.discard)
            else:
                # PassivePassive nudge** ("напомни через час…"): post into the bound
                # conversation *and* push Telegram when deliver=telegram. Without the
                # push, Telegram-first users never see the reminder until they open web.
                if conversation_id:
                    with suppress(Exception):
                        self.storage.add_message(
                            conversation_id=str(conversation_id),
                            role="assistant",
                            content=f"Напоминание: {reminder['text']}",
                            metadata={"kind": "reminder", "reminder_id": reminder["id"]},
                        )
                with suppress(Exception):
                    await self._deliver_passive_reminder(reminder)
            if self.bus is not None:
                with suppress(Exception):
                    await self.bus.publish(
                        {
                            "channel": "reminders",
                            "action": "fire",
                            "reminder": {
                                "id": reminder["id"],
                                "text": reminder["text"],
                                "due_at": reminder["due_at"],
                                "recurring": recurring,
                                "conversation_id": conversation_id,
                            },
                        }
                    )

    async def _deliver_passive_reminder(self, reminder: dict[str, Any]) -> bool:
        """Push a one-shot/recurring *nudge* to the owner's Telegram when configured.

        Returns True when at least one Telegram chat received the text. Fail-soft:
        missing token/allowlist or transport errors yield False without raising.
        Passive fires carry inline snooze/done buttons so the phone can reschedule
        without opening the web UI.
        """

        payload = reminder.get("payload") if isinstance(reminder.get("payload"), dict) else {}
        deliver = str(payload.get("deliver") or "telegram").strip().lower()
        if deliver in {"none", "off", "conversation", "web", "0", "false"}:
            return False
        text = str(reminder.get("text") or "").strip()
        if not text:
            return False
        body = f"⏰ Напоминание: {text}"[:3900]
        target_chat_id = payload.get("telegram_chat_id")
        requested: tuple[int, ...] = ()
        if isinstance(target_chat_id, bool):
            target_chat_id = None
        if target_chat_id is not None:
            try:
                requested = (int(target_chat_id),)
            except (TypeError, ValueError):
                requested = ()
        if not requested:
            requested = _actor_telegram_chat_ids(self.storage)
        reply_markup = None
        reminder_id = str(reminder.get("id") or "").strip()
        if reminder_id:
            from .notify import reminder_inline_keyboard

            reply_markup = reminder_inline_keyboard(reminder_id)
        # Quiet hours: hold passive nudges and flush as a digest after the window.
        # Critical health alerts still use silent push elsewhere; reminders wait.
        if self._telegram_quiet_now():
            targets = requested
            if not targets and current_user_id() == LEGACY_OWNER_USER_ID:
                targets = ()  # resolve at flush via allowlist default
            self._enqueue_quiet_deferred(
                text=body,
                target_chat_ids=targets,
                reply_markup=reply_markup,
                kind="reminder",
                reminder_id=reminder_id or None,
            )
            return True  # accepted into hold buffer (not dropped)
        if requested:
            return await push_telegram_alert(
                body,
                target_chat_ids=requested,
                reply_markup=reply_markup,
                disable_notification=False,
                storage=self.storage,
            )
        if current_user_id() == LEGACY_OWNER_USER_ID:
            return await push_telegram_alert(
                body,
                target_chat_ids=None,
                reply_markup=reply_markup,
                disable_notification=False,
                storage=self.storage,
            )
        return False

    def _telegram_quiet_hours_spec(self) -> str:
        try:
            prefs = self.storage.get_runtime_value("experience.preferences", {})
        except Exception:  # noqa: BLE001 - quiet-hours probe must never break delivery
            prefs = {}
        if not isinstance(prefs, dict):
            return ""
        return str(prefs.get("quiet_hours") or "")

    def _telegram_quiet_now(self) -> bool:
        """True when operator preferences put the wall-clock in quiet hours."""

        quiet = self._telegram_quiet_hours_spec()
        tz_name = str(getattr(self.settings, "reminder_tz", None) or "Europe/Moscow")
        return in_quiet_hours(quiet, tz_name=tz_name)

    def _enqueue_quiet_deferred(
        self,
        *,
        text: str,
        target_chat_ids: tuple[int, ...],
        reply_markup: dict[str, Any] | None,
        kind: str,
        reminder_id: str | None = None,
    ) -> None:
        """Persist one deferred phone push for post-quiet digest delivery."""

        entry = {
            "ts": utc_now(),
            "text": str(text or "")[:3900],
            "target_chat_ids": list(target_chat_ids),
            "reply_markup": reply_markup,
            "kind": kind,
            "reminder_id": reminder_id,
            "user_id": current_user_id(),
        }
        current = self.storage.get_runtime_value(_QUIET_DEFERRED_KEY, [])
        items: list[Any]
        items = list(current) if isinstance(current, list) else []
        items.append(entry)
        # Cap buffer so a long vacation cannot grow unbounded.
        items = items[-80:]
        self.storage.set_runtime_value(_QUIET_DEFERRED_KEY, items)

    async def _flush_quiet_deferred_pushes(self) -> None:
        """Deliver held quiet-hours pushes once the wall-clock leaves the window."""

        if self._telegram_quiet_now():
            return
        current = self.storage.get_runtime_value(_QUIET_DEFERRED_KEY, [])
        if not isinstance(current, list) or not current:
            return
        # Claim by clearing first so a crash mid-flush does not double-send forever;
        # undelivered items are re-queued below.
        self.storage.set_runtime_value(_QUIET_DEFERRED_KEY, [])
        remaining: list[Any] = []
        # Group by user for a short digest header when multiple items.
        by_user: dict[str, list[dict[str, Any]]] = {}
        for raw in current:
            if not isinstance(raw, dict):
                continue
            user_id = str(raw.get("user_id") or "")
            by_user.setdefault(user_id, []).append(raw)
        for _user_id, entries in by_user.items():
            if len(entries) == 1:
                item = entries[0]
                targets = tuple(
                    int(x)
                    for x in (item.get("target_chat_ids") or [])
                    if not isinstance(x, bool)
                    and str(x).lstrip("-").isdigit()
                )
                markup = (
                    item.get("reply_markup")
                    if isinstance(item.get("reply_markup"), dict)
                    else None
                )
                ok = await push_telegram_alert(
                    str(item.get("text") or ""),
                    target_chat_ids=targets or None,
                    reply_markup=markup,
                    storage=self.storage,
                )
                if not ok:
                    remaining.append(item)
                continue
            # Multi-item digest for this user.
            lines = [f"🌙 За quiet hours накопилось {len(entries)}:"]
            targets: tuple[int, ...] = ()
            for item in entries[:15]:
                text = str(item.get("text") or "").strip()
                if text.startswith("⏰ "):
                    text = text[2:].strip()
                if text.startswith("Напоминание:"):
                    text = text[len("Напоминание:") :].strip()
                lines.append(f"• {text[:200]}")
                raw_targets = item.get("target_chat_ids") or []
                if not targets and isinstance(raw_targets, list) and raw_targets:
                    with suppress(TypeError, ValueError):
                        targets = tuple(
                            int(x)
                            for x in raw_targets
                            if not isinstance(x, bool) and str(x).lstrip("-").isdigit()
                        )
            if len(entries) > 15:
                lines.append(f"…и ещё {len(entries) - 15}")
            ok = await push_telegram_alert(
                "\n".join(lines)[:3900],
                target_chat_ids=targets or None,
                storage=self.storage,
            )
            if not ok:
                remaining.extend(entries)
        if remaining:
            self.storage.set_runtime_value(_QUIET_DEFERRED_KEY, remaining[-80:])

    async def _run_briefing_task(self, reminder: dict[str, Any]) -> None:
        """Build the structured daily briefing and push it to Telegram (no LLM turn)."""

        payload = reminder.get("payload") if isinstance(reminder.get("payload"), dict) else {}
        label = str(reminder.get("text") or "Утренняя сводка").strip()
        experience = getattr(self.autonomy_executor, "experience", None)
        answer = ""
        error: str | None = None
        try:
            if experience is None:
                # Fall back to a full agent turn when experience is not wired (tests/CLI).
                await self._run_scheduled_task(
                    {
                        **reminder,
                        "payload": {
                            **payload,
                            "kind": "agent_task",
                            "prompt": payload.get("prompt")
                            or (
                                "Составь краткую утреннюю сводку: runtime, риски, "
                                "что сделать сегодня."
                            ),
                        },
                    }
                )
                return
            dispatcher_status = None
            dispatcher = self._dispatcher_manager()
            if dispatcher is not None:
                with suppress(Exception):
                    dispatcher_status = dispatcher.status()
            briefing = experience.daily_briefing(dispatcher_status=dispatcher_status)
            answer = _format_daily_briefing(briefing)
        except Exception as exc:  # noqa: BLE001 — briefing failure must not touch the loop
            error = str(exc) or exc.__class__.__name__
        delivered = False
        if answer:
            if str(payload.get("deliver") or "telegram") == "telegram":
                with suppress(Exception):
                    target_ids: tuple[int, ...] = ()
                    raw_chat = payload.get("telegram_chat_id")
                    if not isinstance(raw_chat, bool) and raw_chat is not None:
                        with suppress(TypeError, ValueError):
                            target_ids = (int(raw_chat),)
                    if not target_ids:
                        target_ids = _actor_telegram_chat_ids(self.storage)
                    if target_ids or current_user_id() == LEGACY_OWNER_USER_ID:
                        delivered = await push_telegram_alert(
                            f"📋 {label}\n\n{answer}"[:3900],
                            target_chat_ids=target_ids or None,
                            disable_notification=self._telegram_quiet_now(),
                            storage=self.storage,
                        )
            conversation_id = reminder.get("conversation_id")
            if conversation_id:
                with suppress(Exception):
                    self.storage.add_message(
                        conversation_id=str(conversation_id),
                        role="assistant",
                        content=answer,
                        metadata={
                            "kind": "daily_briefing",
                            "reminder_id": reminder["id"],
                            "telegram_transport_visible": False,
                        },
                    )
        with suppress(Exception):
            self.storage.add_event(
                kind="scheduled_task.run",
                title=f"Сводка: {label}",
                level="warn" if error else "info",
                payload={
                    "reminder_id": reminder["id"],
                    "delivered": delivered,
                    "chars": len(answer),
                    "error": error,
                    "kind": "briefing",
                },
            )

    async def _run_scheduled_task(self, reminder: dict[str, Any]) -> None:
        """Run one scheduled agent task: a full agent turn whose answer is pushed to the owner.

        Runs even with the autonomy background loops off — the owner explicitly scheduled it.
        Never raises into the reminder loop (it is spawned fire-and-forget).
        """

        payload = reminder.get("payload") if isinstance(reminder.get("payload"), dict) else {}
        prompt = str(payload.get("prompt") or reminder.get("text") or "").strip()
        label = str(reminder.get("text") or "Плановая задача").strip()
        agent = getattr(self.autonomy_executor, "agent", None)
        if not prompt or agent is None:
            return
        try:
            execute_decision = self.authorization.authorize_current(
                "background.scheduled_task.execute",
                resource_type="scheduled_task",
                resource_ref=str(reminder.get("id") or ""),
                context={"operation": "execute"},
            )
        except Exception:  # noqa: BLE001 - scheduled execution fails closed.
            return
        if not execute_decision.allowed:
            return
        answer = ""
        error: str | None = None
        response_message_id: str | None = None
        transport_request_id = (
            f"scheduled:{reminder.get('id')}:{reminder.get('due_at') or reminder.get('updated_at')}"
        )
        transport_request_hash = hashlib.sha256(transport_request_id.encode()).hexdigest()
        try:
            response = await agent.chat(
                prompt,
                conversation_id=reminder.get("conversation_id"),
                transport_request_id=transport_request_id,
            )
            answer = (getattr(response, "answer", "") or "").strip()
            response_message_id = str(getattr(response, "message_id", "") or "") or None
            with self.storage.transaction(immediate=True) as conn:
                conn.execute(
                    """
                    UPDATE messages
                    SET metadata = json_set(
                        metadata, '$.telegram_transport_visible', json('false')
                    )
                    WHERE user_id = ?
                      AND json_extract(metadata, '$.chat_request_hash') = ?
                    """,
                    (current_user_id(), transport_request_hash),
                )
        except Exception as exc:  # noqa: BLE001 — a task failure must not touch the loop
            error = str(exc) or exc.__class__.__name__
        delivered = False
        if answer:
            if str(payload.get("deliver") or "telegram") == "telegram":
                with suppress(Exception):
                    target_ids: tuple[int, ...] = ()
                    raw_chat = payload.get("telegram_chat_id")
                    if not isinstance(raw_chat, bool) and raw_chat is not None:
                        with suppress(TypeError, ValueError):
                            target_ids = (int(raw_chat),)
                    if not target_ids:
                        target_ids = _actor_telegram_chat_ids(self.storage)
                    if target_ids or current_user_id() == LEGACY_OWNER_USER_ID:
                        delivered = await push_telegram_alert(
                            f"🕒 {label}\n\n{answer}"[:3900],
                            target_chat_ids=target_ids or None,
                            disable_notification=self._telegram_quiet_now(),
                            storage=self.storage,
                        )
            if response_message_id:
                with suppress(Exception):
                    self.storage.merge_message_metadata(
                        response_message_id,
                        {"telegram_transport_visible": False},
                    )
            conversation_id = reminder.get("conversation_id")
            if conversation_id:
                with suppress(Exception):
                    self.storage.add_message(
                        conversation_id=str(conversation_id),
                        role="assistant",
                        content=answer,
                        metadata={
                            "kind": "scheduled_task",
                            "reminder_id": reminder["id"],
                            "telegram_transport_visible": False,
                        },
                    )
        with suppress(Exception):
            self.storage.add_event(
                kind="scheduled_task.run",
                title=f"Плановая задача: {label}",
                level="warn" if error else "info",
                payload={
                    "reminder_id": reminder["id"],
                    "delivered": delivered,
                    "chars": len(answer),
                    "error": error,
                },
            )

    async def _run_screen_watch(self, reminder: dict[str, Any]) -> None:
        """Run one bounded screen capture + VLM condition check, fail-soft."""

        payload = reminder.get("payload") if isinstance(reminder.get("payload"), dict) else {}
        condition = str(payload.get("condition") or "").strip()
        reminder_id = str(reminder.get("id") or "")
        expected_fire_count = int(reminder.get("fire_count") or 0) + 1
        if not condition or not reminder_id:
            return
        if not self._screen_watch_claim_is_current(reminder_id, expected_fire_count):
            return

        expires_at = _parse_utc_datetime(payload.get("expires_at"))
        if expires_at is None:
            staged = self.storage.stage_screen_watch_notification(
                reminder_id,
                expected_fire_count=expected_fire_count,
                terminal_status="cancelled",
                text=(
                    f"Наблюдение за экраном остановлено: у условия «{condition}» "
                    "повреждён или отсутствует срок действия."
                ),
                event_kind="screen_watch.invalid",
                level="warn",
                met=False,
            )
            if staged is not None:
                await self._deliver_screen_watch_notice(staged)
            return
        # A poll whose *scheduled* due time is on the expiry boundary must still run.
        # Compare scheduler lateness to the supervisor cadence instead of using a fixed
        # grace: a slow cadence can legitimately exceed 30 seconds, while a stale claim
        # recovered after a long outage must not resurrect an expired watcher.
        now = datetime.now(UTC)
        scheduled_due = _parse_utc_datetime(reminder.get("due_at"))
        max_scheduler_lateness = timedelta(
            seconds=max(60, 3 * max(5, int(self.settings.reminder_interval_sec)))
        )
        boundary_poll_is_timely = bool(
            scheduled_due is not None
            and scheduled_due <= expires_at
            and now <= scheduled_due + max_scheduler_lateness
        )
        if now >= expires_at and not boundary_poll_is_timely:
            await self._expire_screen_watch(reminder_id, expected_fire_count, condition)
            return

        agent = getattr(self.autonomy_executor, "agent", None)
        if agent is None or not hasattr(agent, "check_screen_condition"):
            if datetime.now(UTC) >= expires_at:
                await self._expire_screen_watch(reminder_id, expected_fire_count, condition)
            return

        try:
            check = await agent.check_screen_condition(condition)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one failed poll must not stop the loop
            check = None
            error = str(exc) or exc.__class__.__name__
        else:
            error = str(getattr(check, "error", "") or "")

        met: bool | None
        if isinstance(check, bool):
            met = check
        elif check is None:
            met = None
        else:
            raw_met = getattr(check, "met", None)
            met = raw_met if isinstance(raw_met, bool) else None
        if met is not True:
            if met is None and error:
                with suppress(Exception):
                    self.storage.add_event(
                        kind="screen_watch.error",
                        title=f"Не удалось проверить экран: {condition}",
                        level="warn",
                        payload={"reminder_id": reminder_id, "error": error[:500]},
                    )
            # The boundary observation was the watcher's last permitted poll. Seal the
            # bounded task now instead of leaving it pending until one more recurrence.
            if datetime.now(UTC) >= expires_at:
                await self._expire_screen_watch(reminder_id, expected_fire_count, condition)
            return

        # A user cancellation or a newer claim wins over this in-flight observation.
        if not self._screen_watch_claim_is_current(reminder_id, expected_fire_count):
            return
        keep = bool(payload.get("keep"))
        notice = f"Условие на экране выполнено: «{condition}»."
        staged = self.storage.stage_screen_watch_notification(
            reminder_id,
            expected_fire_count=expected_fire_count,
            terminal_status=None if keep else "fired",
            text=notice,
            event_kind="screen_watch.fire",
            level="info",
            met=True,
        )
        if staged is not None:
            await self._deliver_screen_watch_notice(staged)

    async def _expire_screen_watch(
        self,
        reminder_id: str,
        expected_fire_count: int,
        condition: str,
    ) -> None:
        staged = self.storage.stage_screen_watch_notification(
            reminder_id,
            expected_fire_count=expected_fire_count,
            terminal_status="cancelled",
            text=(
                f"Наблюдение за экраном завершено: условие «{condition}» "
                "не было замечено до истечения срока."
            ),
            event_kind="screen_watch.expire",
            level="info",
            met=False,
        )
        if staged is not None:
            await self._deliver_screen_watch_notice(staged)

    def _screen_watch_claim_is_current(
        self, reminder_id: str, expected_fire_count: int
    ) -> bool:
        with suppress(Exception):
            current = self.storage.get_reminder(reminder_id)
            return bool(
                current
                and current.get("status") == "pending"
                and int(current.get("fire_count") or 0) == expected_fire_count
            )
        return False

    async def _flush_screen_watch_notifications(self) -> None:
        with suppress(Exception):
            pending = self.storage.list_pending_screen_watch_notifications(
                limit=50, all_users=True
            )
            authorization = self.authorization
            for reminder in pending:
                actor = authorization.actor_for_user(
                    str(reminder.get("user_id") or ""),
                    source="screen-watch-outbox",
                )
                if actor is None:
                    continue
                with bind_actor(actor):
                    await self._deliver_screen_watch_notice(reminder)

    async def _deliver_screen_watch_notice(
        self,
        reminder: dict[str, Any],
    ) -> None:
        payload = reminder.get("payload") if isinstance(reminder.get("payload"), dict) else {}
        notification = payload.get("notification")
        if not isinstance(notification, dict) or notification.get("state") != "pending":
            return
        reminder_id = str(reminder.get("id") or "")
        notification_id = str(notification.get("id") or "")
        if not reminder_id or not notification_id:
            return
        delivery_id = f"{reminder_id}:{notification_id}"
        if delivery_id in self._screen_watch_delivery_ids:
            return
        self._screen_watch_delivery_ids.add(delivery_id)
        try:
            # A flush may have listed this row before another delivery completed while
            # it was waiting on an earlier notice. Re-read under the in-process lease so
            # a stale pending snapshot can never resend an already sealed notification.
            current = self.storage.get_reminder(reminder_id)
            current_payload = (
                current.get("payload")
                if isinstance(current, dict) and isinstance(current.get("payload"), dict)
                else {}
            )
            current_notification = current_payload.get("notification")
            if (
                not isinstance(current_notification, dict)
                or current_notification.get("id") != notification_id
                or current_notification.get("state") != "pending"
            ):
                return
            await self._deliver_screen_watch_notice_claimed(current)
        finally:
            self._screen_watch_delivery_ids.discard(delivery_id)

    async def _deliver_screen_watch_notice_claimed(
        self,
        reminder: dict[str, Any],
    ) -> None:
        payload = reminder.get("payload") if isinstance(reminder.get("payload"), dict) else {}
        notification = payload.get("notification")
        if not isinstance(notification, dict) or notification.get("state") != "pending":
            return
        reminder_id = str(reminder.get("id") or "")
        notification_id = str(notification.get("id") or "")
        text = str(notification.get("text") or "")[:3900]
        event_kind = str(notification.get("event_kind") or "screen_watch.notice")
        level = str(notification.get("level") or "info")
        met = bool(notification.get("met"))
        if not reminder_id or not notification_id or not text:
            return

        # A manual cancellation of a persistent watch retracts an unsent match notice.
        if (
            reminder.get("status") == "cancelled"
            and bool(payload.get("keep"))
            and event_kind == "screen_watch.fire"
        ):
            self.storage.update_screen_watch_notification(
                reminder_id, notification_id, completed=True
            )
            return

        telegram_required = str(payload.get("deliver") or "telegram") == "telegram"
        telegram_target_ids = _notification_chat_ids(notification.get("telegram_target_ids"))
        telegram_delivered_ids = set(
            _notification_chat_ids(notification.get("telegram_delivered_ids"))
        )
        if telegram_required:
            if not telegram_target_ids:
                target_chat_id = payload.get("telegram_chat_id")
                requested = (
                    (target_chat_id,)
                    if isinstance(target_chat_id, int)
                    else _actor_telegram_chat_ids(self.storage)
                )
                if requested:
                    telegram_target_ids = requested
                elif current_user_id() == LEGACY_OWNER_USER_ID:
                    with suppress(Exception):
                        _, resolved = telegram_targets(requested_chat_ids=None)
                        telegram_target_ids = resolved
                # Freeze a non-empty recipient set into the durable outbox. If Telegram
                # is not configured yet, leave it unresolved so a later retry can pick up
                # a corrected configuration instead of silently completing the notice.
                if telegram_target_ids:
                    self.storage.update_screen_watch_notification(
                        reminder_id,
                        notification_id,
                        telegram_target_ids=telegram_target_ids,
                    )
            if bool(notification.get("telegram_delivered")):
                # Upgrade compatibility for an outbox written by the old all-or-nothing
                # sender. New notices persist progress after every individual recipient.
                telegram_delivered_ids.update(telegram_target_ids)
            for target_id in telegram_target_ids:
                if target_id in telegram_delivered_ids:
                    continue
                delivered = False
                with suppress(Exception):
                    delivered = await push_telegram_alert(
                        f"👁 {text}"[:3900],
                        target_chat_ids=(target_id,),
                        storage=self.storage,
                    )
                if delivered:
                    telegram_delivered_ids.add(target_id)
                    self.storage.update_screen_watch_notification(
                        reminder_id,
                        notification_id,
                        telegram_delivered_ids=tuple(sorted(telegram_delivered_ids)),
                        telegram_delivered=(
                            bool(telegram_target_ids)
                            and telegram_delivered_ids.issuperset(telegram_target_ids)
                        ),
                    )
            telegram_delivered = bool(telegram_target_ids) and telegram_delivered_ids.issuperset(
                telegram_target_ids
            )
        else:
            telegram_delivered = True

        local_delivered = bool(notification.get("local_delivered"))
        conversation_id = reminder.get("conversation_id")
        if not local_delivered:
            local_event_payload = {
                "reminder_id": reminder.get("id"),
                "condition": payload.get("condition"),
                "met": met,
                "telegram_delivered": telegram_delivered,
                "telegram_target_ids": list(telegram_target_ids),
                "telegram_delivered_ids": sorted(telegram_delivered_ids),
                "conversation_id": conversation_id,
                "notification_id": notification_id,
            }
            local_result = None
            try:
                local_result = self.storage.deliver_screen_watch_local_notification(
                    reminder_id,
                    notification_id,
                    text=text,
                    event_kind=event_kind,
                    level=level,
                    met=met,
                    event_payload=local_event_payload,
                )
            except Exception:  # noqa: BLE001 - durable outbox remains pending for retry
                local_result = None
            if local_result is not None:
                local_payload = (
                    local_result.get("payload")
                    if isinstance(local_result.get("payload"), dict)
                    else {}
                )
                local_notification = local_payload.get("notification")
                local_delivered = bool(
                    isinstance(local_notification, dict)
                    and local_notification.get("local_delivered")
                )
            if local_delivered and self.bus is not None:
                with suppress(Exception):
                    await self.bus.publish(
                        {
                            "channel": "reminders",
                            "action": event_kind,
                            "reminder_id": reminder.get("id"),
                            "met": met,
                        }
                    )

        if telegram_delivered and local_delivered:
            self.storage.update_screen_watch_notification(
                reminder_id,
                notification_id,
                completed=True,
            )

    async def _record_telemetry(self) -> None:
        try:
            snapshot = await asyncio.to_thread(self.telemetry.snapshot)
            self.storage.record_telemetry(snapshot)
            self._last_telemetry_at = str(snapshot["ts"])
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc) or exc.__class__.__name__
            with suppress(Exception):
                self.storage.add_event(
                    kind="autonomy.error",
                    title="Telemetry loop failed",
                    level="warn",
                    payload={"error": self._last_error},
                )
            return
        with suppress(Exception):
            await self._check_health_alerts(snapshot)

    async def _check_health_alerts(self, snapshot: dict[str, Any]) -> None:
        """Diff live thresholds against the last-fired set and deliver the transitions.

        Reuses the telemetry snapshot the loop already fetched (no extra probe). Only
        state *changes* are delivered: a new breach → warn/error event + UI bus + phone
        push; a cleared breach → an info recovery notice. Delivery is best-effort — each
        channel is wrapped so a dead bus or unconfigured Telegram never blocks the rest.
        """

        if not self.settings.health_alerts_enabled:
            return
        current = _evaluate_alerts(
            snapshot,
            gpu_temp_c=self.settings.health_alert_gpu_temp_c,
            gpu_vram_ratio=self.settings.health_alert_gpu_vram_ratio,
            disk_ratio=self.settings.health_alert_disk_ratio,
            memory_ratio=self.settings.health_alert_memory_ratio,
        )
        for key, alert in current.items():
            if key in self._alert_active:
                continue
            await self._deliver_alert(key, alert, recovered=False)
        for key, alert in list(self._alert_active.items()):
            if key not in current:
                await self._deliver_alert(key, alert, recovered=True)
        self._alert_active = current

    async def _deliver_alert(self, key: str, alert: dict[str, Any], *, recovered: bool) -> None:
        title = alert.get("title", key)
        detail = alert.get("detail", "")
        if recovered:
            event_title = f"Восстановлено: {title}"
            level = "info"
            phone_text = f"✅ Восстановлено: {title}\n{detail}".strip()
        else:
            event_title = title
            level = str(alert.get("level", "warn"))
            emoji = "🔴" if level == "error" else "🟠"
            phone_text = f"{emoji} {title}\n{detail}".strip()
        with suppress(Exception):
            self.storage.add_event(
                kind="health.alert",
                title=event_title,
                level=level,
                payload={"alert": key, "recovered": recovered, "detail": detail},
            )
        if self.bus is not None:
            with suppress(Exception):
                await self.bus.publish(
                    {
                        "channel": "health",
                        "action": "alert",
                        "alert": {
                            "key": key,
                            "title": title,
                            "detail": detail,
                            "level": level,
                            "recovered": recovered,
                        },
                    }
                )
        with suppress(Exception):
            await push_telegram_alert(phone_text, storage=self.storage)

    async def _self_heal_loop(self) -> None:
        while True:
            await asyncio.sleep(max(30, self.settings.self_healing_interval_sec))
            with suppress(Exception):
                await self._maybe_self_heal()

    async def _maybe_self_heal(self) -> None:
        """Restart the local model dispatcher when it has crashed/OOM'd or hung.

        Detection is a cheap ``llm.health()`` probe (a GET to the model endpoint). A
        single failure never acts — a restart fires only after ``self_healing_min_failures``
        consecutive unhealthy probes, and only after ``_classify_dispatcher`` confirms the
        container actually crashed (not a clean owner stop or a never-started box).

        Three guards keep it from doing harm: (1) Docker ``health=starting`` plus the
        remaining StartedAt-based profile deadline distinguishes warmup from a hang without
        hiding real health probes; (2) only a STABLE non-restart reason (owner stopped it,
        no container, no docker) latches self-healing off until the dispatcher returns —
        a TRANSIENT reason (a `docker ps` hiccup) is retried, never latched; (3) a
        rolling-window restart budget; exhausting it escalates once AND stops restarting
        until the dispatcher recovers.
        """

        if not self.settings.self_healing_enabled or not self.settings.llm_enabled:
            return
        if await self._dispatcher_is_live():
            self._self_heal_streak = 0
            self._self_heal_blocked = False
            self._self_heal_budget_alerted = False
            self._self_heal_grace_until = 0.0
            # A recovered dispatcher earns a fresh restart budget for any future incident.
            self._self_heal_history = []
            return
        if self._self_heal_blocked:
            # Latched off (owner stopped it / gave up after the budget); wait for recovery.
            return
        self._self_heal_streak += 1
        if self._self_heal_streak < max(1, self.settings.self_healing_min_failures):
            return
        dispatcher = self._dispatcher_manager()
        if dispatcher is None:
            # No handle to restart with. Do NOT latch — one may appear; retry next tick.
            return
        action, detail, warmup_remaining = await asyncio.to_thread(
            self._classify_dispatcher,
            dispatcher,
        )
        if action == "transient":
            # An ambiguous probe (docker daemon hiccup / status timeout). Do NOT latch —
            # the real state will be seen on a later tick and healed/escalated then.
            return
        if action == "warming":
            # Anchor the deadline to Docker StartedAt. Re-detecting the same warmup must
            # never grant a fresh full profile window.
            self._self_heal_streak = 0
            self._open_self_heal_grace(warmup_remaining)
            return
        if action == "stable":
            # Owner stopped it / never launched it / no docker — latch until it returns.
            # This also covers a dead Docker Desktop: we never try to restart the daemon,
            # only the dispatcher container, so latching avoids a restart storm of no-ops.
            if not self._self_heal_blocked:
                self._self_heal_blocked = True
                with suppress(Exception):
                    self.storage.add_event(
                        kind="self_heal.skip",
                        title=f"Self-healing: перезапуск не требуется ({detail})",
                        level="info",
                        payload={"detail": detail},
                    )
            return
        # action == "restart" — re-probe once more to avoid restarting a dispatcher that
        # recovered between the health probe and the classify.
        if await self._dispatcher_is_live():
            self._self_heal_streak = 0
            return
        # Soft "Up but unresponsive" must not thrash during post-restart / warmup grace.
        # Hard failures (Exited N, Restarting) still heal immediately even inside grace.
        if (
            detail == "running-but-unresponsive"
            and time.monotonic() < self._self_heal_grace_until
        ):
            return
        if not self._self_heal_budget_ok():
            await self._escalate_budget_exhausted(detail)
            return
        await self._perform_self_heal(dispatcher, detail)

    async def _dispatcher_is_live(self) -> bool:
        try:
            result = await self.llm.health()
        except Exception:  # noqa: BLE001
            return False
        return bool(isinstance(result, dict) and result.get("ok"))

    def _dispatcher_manager(self) -> Any | None:
        if self.dispatcher is not None:
            return self.dispatcher
        try:
            from .dispatcher import DispatcherManager

            self.dispatcher = DispatcherManager(self.settings, storage=self.storage)
        except Exception:  # noqa: BLE001
            self.dispatcher = None
        return self.dispatcher

    def _classify_dispatcher(self, dispatcher: Any) -> tuple[str, str, float]:
        """Classify an unresponsive dispatcher into restart / stable-skip / transient-skip.

        - ``"restart"``: a genuine failure worth an automatic restart — a crashed/OOM-killed
          container (non-zero ``Exited (N)``), a crash-looping one (``Restarting (N)`` under
          the compose restart policy), or one that claims to run but does not serve (``Up``
          while ``llm.health()`` fails — a hung engine keeps its listening socket, so the
          open TCP port is NOT proof of health).
        - ``"stable"``: a state that only the owner changes — a clean ``Exited (0)`` (they
          stopped it), a genuinely-absent container (never launched), or no Docker at all.
          The caller latches self-healing off until the dispatcher returns.
        - ``"warming"``: Docker reports ``health=starting`` and the container age is still
          within this profile's readiness deadline. The caller opens a warmup grace window.
        - ``"transient"``: an ambiguous probe that must be retried, never latched — a
          ``docker ps`` that errored (daemon restarting/timeout), a raised ``status()``, or
          an unrecognized container state.
        """

        try:
            status = dispatcher.status()
        except Exception as exc:  # noqa: BLE001
            return ("transient", f"status-error:{exc.__class__.__name__}", 0.0)
        if not isinstance(status, dict):
            return ("transient", "status-unavailable", 0.0)
        if not status.get("docker_available"):
            return ("stable", "docker-unavailable", 0.0)
        container = status.get("container_status")
        if isinstance(container, dict) and container.get("ok") is False:
            # `docker ps` itself failed (daemon down/restarting, timeout) — not proof the
            # container is gone. Retry rather than latch off on a transient docker hiccup.
            return ("transient", "docker-query-failed", 0.0)
        if not isinstance(container, dict) or not container.get("exists"):
            return ("stable", "no-container", 0.0)
        state = str(container.get("status") or "")
        lowered = state.casefold()
        if lowered.startswith("up"):
            if container.get("inspect_ok") is False:
                return ("transient", "container-inspect-failed", 0.0)
            health = str(container.get("health") or "").casefold()
            if not health and "health: starting" in lowered:
                health = "starting"
            if health == "starting":
                started_at = _parse_utc_datetime(container.get("started_at"))
                if started_at is None:
                    return ("transient", "warming-start-time-unavailable", 0.0)
                age = max(0.0, (datetime.now(UTC) - started_at).total_seconds())
                readiness_deadline = max(
                    0.0, float(self.settings.profile.readiness_deadline_sec)
                )
                if age < readiness_deadline:
                    return (
                        "warming",
                        "container-health-starting",
                        max(0.0, readiness_deadline - age),
                    )
            return ("restart", "running-but-unresponsive", 0.0)
        if lowered.startswith("restarting"):
            # Crash-looping under the compose restart policy — the dominant crash signature.
            return ("restart", "restarting-crash-loop", 0.0)
        exit_code = _container_exit_code(state)
        if exit_code is not None and exit_code != 0:
            return ("restart", f"exited-{exit_code}", 0.0)
        if exit_code == 0:
            return ("stable", "stopped-clean", 0.0)
        # Created / Paused / Removing / Dead / empty — don't guess a restart; retry.
        return ("transient", f"unknown-state:{lowered[:24]}", 0.0)

    def _self_heal_budget_ok(self) -> bool:
        now = time.monotonic()
        window = max(60, self.settings.self_healing_window_sec)
        self._self_heal_history = [t for t in self._self_heal_history if now - t < window]
        return len(self._self_heal_history) < max(1, self.settings.self_healing_max_restarts)

    def _open_self_heal_grace(self, remaining_seconds: float) -> None:
        # Floor with JARVIS_SELF_HEALING_GRACE_SEC so a post-restart probe that
        # does not yet see health=starting still gets a no-thrash window. Never
        # shrink an already-open longer warmup deadline (profile readiness).
        floor = max(0.0, float(self.settings.self_healing_grace_sec))
        remaining = max(0.0, float(remaining_seconds), floor)
        deadline = time.monotonic() + remaining
        if deadline > self._self_heal_grace_until:
            self._self_heal_grace_until = deadline

    def _restart_dispatcher(self, dispatcher: Any) -> dict[str, Any]:
        # Force a fresh container: stop first (clears a hung process), then bring it up
        # with independent state verification.
        try:
            status = dispatcher.status()
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "summary": f"restart identity probe failed: {exc.__class__.__name__}",
            }
        container = status.get("container_status") if isinstance(status, dict) else None
        previous_id = (
            str(container.get("id") or "").strip().casefold()
            if isinstance(container, dict)
            else ""
        )
        if re.fullmatch(r"[0-9a-f]{64}", previous_id) is None:
            return {
                "ok": False,
                "summary": "restart refused: full immutable dispatcher ID is unavailable",
            }
        restart = getattr(dispatcher, "restart_verified", None)
        if not callable(restart):
            return {
                "ok": False,
                "summary": "restart refused: atomic exact-ID dispatcher API is unavailable",
            }
        up = restart(previous_id)
        # If the stop failed, `up` may take the "reused" branch and keep the still-running
        # hung container instead of replacing it — report that as a failed restart so the
        # owner is alerted rather than being told the brain is fine.
        if isinstance(up, dict) and up.get("ok"):
            ownership_commit = up.get("ownership_commit")
            if isinstance(ownership_commit, dict) and not ownership_commit.get("ok"):
                up = {
                    **up,
                    "ok": False,
                    "summary": "restart verification passed but ownership commit failed",
                }
        return up

    async def _perform_self_heal(self, dispatcher: Any, detail: str) -> None:
        self._self_heal_history.append(time.monotonic())
        self._self_heal_streak = 0
        self._self_heal_count += 1
        self._last_self_heal_at = utc_now()
        # A previous container's grace must never suppress retries for this attempt.
        self._self_heal_grace_until = 0.0
        with suppress(Exception):
            self.storage.add_event(
                kind="self_heal.restart",
                title=f"Self-healing: перезапуск диспетчера ({detail})",
                level="warn",
                payload={"detail": detail, "attempt": len(self._self_heal_history)},
            )
        await self._push_owner(
            f"🛠 Self-healing: локальный мозг не отвечает ({detail}). Перезапускаю диспетчер…"
        )
        result = await asyncio.to_thread(self._restart_dispatcher, dispatcher)
        ok = bool(isinstance(result, dict) and result.get("ok"))
        summary = str(result.get("summary")) if isinstance(result, dict) else ""
        if ok:
            action, _post_restart_detail, warmup_remaining = await asyncio.to_thread(
                self._classify_dispatcher,
                dispatcher,
            )
            # Always open a floor grace after a successful restart; extend to the
            # remaining profile warmup when Docker reports health=starting.
            self._open_self_heal_grace(
                warmup_remaining if action == "warming" else 0.0
            )
        with suppress(Exception):
            self.storage.add_event(
                kind="self_heal.result",
                title="Диспетчер восстановлен" if ok else "Перезапуск диспетчера не удался",
                level="info" if ok else "error",
                payload={"detail": detail, "ok": ok, "summary": summary},
            )
        if self.bus is not None:
            with suppress(Exception):
                await self.bus.publish(
                    {
                        "channel": "health",
                        "action": "self_heal",
                        "self_heal": {"detail": detail, "ok": ok, "summary": summary},
                    }
                )
        if ok:
            await self._push_owner("✅ Self-healing: диспетчер перезапущен и снова отвечает.")
        else:
            await self._push_owner(
                f"❌ Self-healing: перезапуск не удался — {summary}. Нужно вмешательство."
            )

    async def _escalate_budget_exhausted(self, detail: str) -> None:
        if self._self_heal_budget_alerted:
            return
        self._self_heal_budget_alerted = True
        # Give up until the dispatcher recovers on its own / the owner intervenes — do NOT
        # keep restarting every time a window slot frees up (that would thrash the GPU
        # indefinitely for a permanently-broken brain). The latch clears when it goes live.
        self._self_heal_blocked = True
        window_min = max(1, self.settings.self_healing_window_sec // 60)
        with suppress(Exception):
            self.storage.add_event(
                kind="self_heal.exhausted",
                title="Self-healing: лимит автоперезапусков исчерпан — нужна помощь",
                level="error",
                payload={"detail": detail, "restarts": len(self._self_heal_history)},
            )
        await self._push_owner(
            f"🆘 Self-healing: диспетчер падает повторно ({detail}). Исчерпан лимит "
            f"{self.settings.self_healing_max_restarts} перезапуск(ов) за {window_min} мин — "
            "нужно вмешаться вручную."
        )

    async def _push_owner(self, text: str) -> None:
        with suppress(Exception):
            await push_telegram_alert(text, storage=self.storage)

    async def _record_health(self) -> None:
        self._last_health_attempt_at = utc_now()
        self._last_health_attempt_ok = False
        try:
            result = await run_diagnostics(
                settings=self.settings,
                storage=self.storage,
                llm=self.llm,
                persist=True,
            )
            required_components = {
                "runtime.home",
                "runtime.data",
                "runtime.cache",
                "runtime.logs",
                "storage.sqlite",
            }
            if self.settings.llm_enabled:
                required_components.add("llm.router")
            check_statuses = {check.name: check.status.casefold() for check in result.checks}
            self._health_recovery_pending = any(
                check_statuses.get(component) != "ok" for component in required_components
            )
            self._last_health_at = utc_now()
            self._last_health_attempt_ok = True
            error_count = sum(1 for check in result.checks if check.status == "error")
            warn_count = sum(1 for check in result.checks if check.status == "warn")
            with suppress(Exception):
                self.storage.add_event(
                    kind="autonomy.health",
                    title=(
                        "Autonomous health snapshot: "
                        f"{error_count} error(s), {warn_count} warn(s)"
                    ),
                    level="info" if error_count == 0 else "warn",
                    payload={"checks": len(result.checks), "ok": result.ok},
                )
        except Exception as exc:  # noqa: BLE001
            self._health_recovery_pending = True
            self._last_error = str(exc) or exc.__class__.__name__
            with suppress(Exception):
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
            with suppress(Exception):
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
            with background_llm_priority(self.llm):
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
        except TimeoutError:
            # Expected when foreground traffic occupies the model for the full
            # maintenance window. The next scheduled pulse will retry.
            return
        except Exception as exc:  # noqa: BLE001
            self._last_cognition_error = str(exc) or exc.__class__.__name__
            with suppress(Exception):
                self.storage.add_event(
                    kind="cognition.error",
                    title="Background cognition loop failed",
                    level="warn",
                    payload={"error": self._last_cognition_error},
                )

    async def _run_background_jobs(self) -> None:
        if self.autonomy_executor is None:
            return
        authorization = self.authorization
        for user_id in self._background_job_user_ids(limit=100):
            self._background_user_cursor = user_id
            actor = authorization.actor_for_user(user_id, source="autonomy-scheduler")
            if actor is None:
                continue
            decision = authorization.authorize(
                user_id,
                "background.autonomy.execute",
                context={"scheduler": "runtime-supervisor"},
            )
            if not decision.allowed:
                continue
            with bind_actor(actor):
                try:
                    with background_llm_priority(self.llm):
                        results = await self.autonomy_executor.run_due_jobs(limit=1)
                    if not results:
                        continue
                    self._last_background_job_at = utc_now()
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
                    # One global execution per tick preserves the existing resource budget.
                    return
                except Exception as exc:  # noqa: BLE001
                    self._last_error = str(exc) or exc.__class__.__name__
                    with suppress(Exception):
                        self.storage.add_event(
                            kind="autonomy.error",
                            title="Background job loop failed",
                            level="warn",
                            payload={"error": self._last_error},
                        )

    def _background_job_user_ids(self, *, limit: int) -> list[str]:
        """Page fairly through tenants that actually have a persisted job collection."""

        bounded = max(1, min(500, int(limit)))

        def page(after: str) -> list[str]:
            with self.storage.locked_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT u.id
                    FROM users u
                    JOIN runtime_kv kv
                      ON kv.key = CASE
                          WHEN u.id = ? THEN 'operations.autonomy.jobs'
                          ELSE 'user.' || u.id || '.operations.autonomy.jobs'
                      END
                    WHERE u.status = 'active' AND u.id > ? AND kv.value != '[]'
                    ORDER BY u.id
                    LIMIT ?
                    """,
                    (LEGACY_OWNER_USER_ID, after, bounded),
                ).fetchall()
            return [str(row["id"]) for row in rows]

        user_ids = page(self._background_user_cursor)
        if not user_ids and self._background_user_cursor:
            self._background_user_cursor = ""
            user_ids = page("")
        return user_ids


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


def _format_daily_briefing(briefing: dict[str, Any]) -> str:
    """Render experience.daily_briefing() into a compact Telegram-ready body."""

    headline = str(briefing.get("headline") or "Сводка").strip()
    operator = str(briefing.get("operator_name") or "").strip()
    lines = [headline]
    if operator:
        lines.append(f"Оператор: {operator}")
    focus = [
        str(item).strip()
        for item in (briefing.get("focus") or [])
        if str(item).strip()
    ]
    if focus:
        lines.append("")
        lines.append("Фокус:")
        lines.extend(f"• {item}" for item in focus[:6])
    risks = [
        str(item).strip()
        for item in (briefing.get("risks") or [])
        if str(item).strip()
    ]
    if risks:
        lines.append("")
        lines.append("Риски:")
        lines.extend(f"• {item}" for item in risks[:4])
    suggestions = [
        str(item).strip()
        for item in (briefing.get("suggestions") or [])
        if str(item).strip()
    ]
    if suggestions:
        lines.append("")
        lines.append("Что сделать:")
        lines.extend(f"• {item}" for item in suggestions[:4])
    pending = briefing.get("pending_approvals")
    if pending:
        lines.append("")
        lines.append(f"Ожидают approval: {pending}")
    return "\n".join(lines).strip()


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _bounded_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(minimum, min(maximum, parsed))


def _container_exit_code(status_text: str) -> int | None:
    """Parse the exit code out of a docker status string like 'Exited (137) 2 min ago'.

    Returns ``None`` when the status is not an exited-with-code form (e.g. 'Up 3 min',
    'Created', 'Restarting'), so the caller treats a missing code as "not a clean stop".
    """

    match = re.search(r"exited \((\d+)\)", status_text.casefold())
    return int(match.group(1)) if match else None
