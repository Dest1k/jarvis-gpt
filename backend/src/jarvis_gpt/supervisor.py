from __future__ import annotations

import asyncio
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
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.llm = llm or LLMRouter(settings)
        self.telemetry = TelemetryCollector(settings)
        self.learning = LearningEngine(storage)
        self._tasks: list[asyncio.Task[None]] = []
        self._started_at: str | None = None
        self._last_telemetry_at: str | None = None
        self._last_health_at: str | None = None
        self._last_learning_at: str | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        self._started_at = utc_now()
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
            "last_telemetry_at": self._last_telemetry_at,
            "last_health_at": self._last_health_at,
            "last_learning_at": self._last_learning_at,
            "last_error": self._last_error,
            "capabilities": [
                "telemetry.persist",
                "health.persist",
                "learning.tick",
                "learning.deduplicate",
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

    async def _health_loop(self) -> None:
        await self._record_health()
        while True:
            await asyncio.sleep(max(60, self.settings.health_interval_sec))
            await self._record_health()

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
            result = await asyncio.to_thread(self.learning.tick)
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
