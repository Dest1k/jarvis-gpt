from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.supervisor import RuntimeSupervisor, _container_exit_code

OLD_CONTAINER_ID = "a" * 64
NEW_CONTAINER_ID = "b" * 64
OPERATION_NONCE = "c" * 32


class _CaptureBus:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, payload: dict) -> None:
        self.published.append(payload)


class _FakeLLM:
    """A stand-in router whose async health() returns a controllable ok flag."""

    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.calls = 0

    async def health(self) -> dict:
        self.calls += 1
        return {"ok": self.ok}


class _FakeDispatcher:
    def __init__(
        self,
        status: dict,
        *,
        up_ok: bool = True,
        up_status: dict | None = None,
    ) -> None:
        self._status = status
        self.up_ok = up_ok
        self.up_status = up_status
        self.calls: list[tuple[str, str]] = []

    def status(self) -> dict:
        return self._status

    def restart_verified(self, expected_container_id: str) -> dict:
        self.calls.append(("restart", expected_container_id))
        if self.up_ok and self.up_status is not None:
            self._status = self.up_status
        return {
            "ok": self.up_ok,
            "summary": "restart verified",
            "container_id": NEW_CONTAINER_ID,
            "operation_nonce": OPERATION_NONCE,
            "ownership_commit": {"ok": self.up_ok},
        }


def _dispatcher_status(
    *,
    docker: bool = True,
    port_open: bool = False,
    exists: bool = True,
    state: str = "Exited (137) 2 minutes ago",
    container_ok: bool = True,
    health: str = "",
    started_at: str = "",
    inspect_ok: bool | None = None,
    container_id: str = OLD_CONTAINER_ID,
) -> dict:
    if not container_ok:
        # How dispatcher._container_status reports a failed `docker ps` (daemon
        # down/restarting, timeout): an error dict with no "exists" key.
        container: dict = {"ok": False, "error": "docker ps failed"}
    else:
        container = {"ok": True, "exists": exists}
        if exists:
            container["status"] = state
            container["id"] = container_id
            container["health"] = health
            container["started_at"] = started_at
            if inspect_ok is not None:
                container["inspect_ok"] = inspect_ok
    return {
        "docker_available": docker,
        "port_open": port_open,
        "container_status": container,
    }


def _supervisor(
    monkeypatch,
    tmp_path,
    *,
    llm_ok: bool,
    dispatcher: _FakeDispatcher | None,
    bus=None,
    env=None,
    profile: str = "qwen36-vl",
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    settings = load_settings(profile)
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    supervisor = RuntimeSupervisor(
        settings=settings,
        storage=storage,
        llm=_FakeLLM(ok=llm_ok),
        dispatcher=dispatcher,
        bus=bus,
    )
    return supervisor, storage


def _patch_push(monkeypatch) -> list[str]:
    pushes: list[str] = []

    async def fake_push(text, **_kwargs):
        pushes.append(text)
        return True

    monkeypatch.setattr("jarvis_gpt.supervisor.push_telegram_alert", fake_push)
    return pushes


def test_container_exit_code_parsing():
    assert _container_exit_code("Exited (137) 2 minutes ago") == 137
    assert _container_exit_code("Exited (0) 5 minutes ago") == 0
    assert _container_exit_code("Up 3 minutes") is None
    assert _container_exit_code("Restarting (1) 4 seconds ago") is None  # not an 'exited' form
    assert _container_exit_code("Created") is None


def test_self_heal_restarts_crashed_dispatcher(monkeypatch, tmp_path):
    dispatcher = _FakeDispatcher(_dispatcher_status(state="Exited (137) 1 minute ago"))
    bus = _CaptureBus()
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, llm_ok=False, dispatcher=dispatcher, bus=bus
    )
    pushes = _patch_push(monkeypatch)

    async def scenario():
        await supervisor._maybe_self_heal()  # streak 1 (min_failures=2) — no action yet
        assert dispatcher.calls == []
        await supervisor._maybe_self_heal()  # streak 2 — crash confirmed, restart

    asyncio.run(scenario())

    assert ("restart", OLD_CONTAINER_ID) in dispatcher.calls
    assert supervisor._self_heal_count == 1
    # owner is told twice: restarting… then restored.
    assert len(pushes) == 2
    assert any("Перезапуск" in p or "Перезапускаю" in p for p in pushes)
    restart_events = [
        e for e in storage.list_events(limit=50) if e.get("kind") == "self_heal.restart"
    ]
    assert restart_events
    storage.close()


def test_self_heal_restarts_running_but_unresponsive(monkeypatch, tmp_path):
    dispatcher = _FakeDispatcher(_dispatcher_status(port_open=False, state="Up 5 minutes"))
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, llm_ok=False, dispatcher=dispatcher,
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1"},
    )
    _patch_push(monkeypatch)

    asyncio.run(supervisor._maybe_self_heal())

    assert ("restart", OLD_CONTAINER_ID) in dispatcher.calls
    assert supervisor._self_heal_count == 1
    storage.close()


def test_self_heal_does_not_report_success_when_ownership_commit_failed(
    monkeypatch,
    tmp_path,
):
    class CommitFailDispatcher(_FakeDispatcher):
        def restart_verified(self, expected_container_id: str) -> dict:
            self.calls.append(("restart", expected_container_id))
            return {
                "ok": True,
                "summary": "runtime answered but state CAS failed",
                "container_id": NEW_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
                "ownership_commit": {
                    "ok": False,
                    "reason": "launcher-state-write-failed:OSError",
                },
            }

    dispatcher = CommitFailDispatcher(
        _dispatcher_status(port_open=False, state="Up 5 minutes")
    )
    supervisor, storage = _supervisor(
        monkeypatch,
        tmp_path,
        llm_ok=False,
        dispatcher=dispatcher,
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1"},
    )

    result = supervisor._restart_dispatcher(dispatcher)

    assert result["ok"] is False
    assert "ownership commit failed" in result["summary"]
    storage.close()


def test_self_heal_skips_cleanly_stopped_dispatcher(monkeypatch, tmp_path):
    dispatcher = _FakeDispatcher(_dispatcher_status(state="Exited (0) 5 minutes ago"))
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, llm_ok=False, dispatcher=dispatcher,
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1"},
    )
    _patch_push(monkeypatch)

    async def scenario():
        await supervisor._maybe_self_heal()  # streak 1 -> classify -> stopped-clean -> skip
        await supervisor._maybe_self_heal()  # blocked -> immediate return

    asyncio.run(scenario())

    assert dispatcher.calls == []  # owner stopped it on purpose — never restarted
    assert supervisor._self_heal_blocked is True
    skips = [e for e in storage.list_events(limit=50) if e.get("kind") == "self_heal.skip"]
    assert skips
    storage.close()


def test_self_heal_skips_missing_container(monkeypatch, tmp_path):
    dispatcher = _FakeDispatcher(_dispatcher_status(exists=False))
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, llm_ok=False, dispatcher=dispatcher,
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1"},
    )
    _patch_push(monkeypatch)

    asyncio.run(supervisor._maybe_self_heal())

    assert dispatcher.calls == []  # never auto-start a dispatcher the owner never launched
    assert supervisor._self_heal_blocked is True
    storage.close()


def test_self_heal_respects_restart_budget_and_escalates(monkeypatch, tmp_path):
    dispatcher = _FakeDispatcher(_dispatcher_status(state="Exited (137) 1 minute ago"))
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, llm_ok=False, dispatcher=dispatcher,
        env={
            "JARVIS_SELF_HEALING_MIN_FAILURES": "1",
            "JARVIS_SELF_HEALING_MAX_RESTARTS": "2",
            "JARVIS_SELF_HEALING_GRACE_SEC": "0",  # isolate the budget from the grace window
        },
    )
    pushes = _patch_push(monkeypatch)

    async def scenario():
        for _ in range(5):
            await supervisor._maybe_self_heal()
            # Deliberately expire each post-restart profile grace to exercise only the
            # independent rolling restart budget in this test.
            supervisor._self_heal_grace_until = 0.0

    asyncio.run(scenario())

    assert supervisor._self_heal_count == 2  # capped at the budget
    # After exhaustion it latches OFF (does not resume restarting when a window slot ages
    # out); the latch clears only when the dispatcher recovers.
    assert supervisor._self_heal_blocked is True
    exhausted = [
        e for e in storage.list_events(limit=80) if e.get("kind") == "self_heal.exhausted"
    ]
    assert len(exhausted) == 1  # escalation fires exactly once, not every tick
    assert any("Исчерпан лимит" in p for p in pushes)
    storage.close()


def test_self_heal_restarts_crash_loop(monkeypatch, tmp_path):
    # A crash-looping container under `restart: unless-stopped` is mostly seen in the
    # "Restarting (N)" state — it must be treated as a crash, not a clean stop.
    dispatcher = _FakeDispatcher(_dispatcher_status(state="Restarting (1) 5 seconds ago"))
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, llm_ok=False, dispatcher=dispatcher,
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1"},
    )
    _patch_push(monkeypatch)
    asyncio.run(supervisor._maybe_self_heal())
    assert ("restart", OLD_CONTAINER_ID) in dispatcher.calls
    assert supervisor._self_heal_count == 1
    storage.close()


def test_self_heal_transient_docker_error_does_not_latch(monkeypatch, tmp_path):
    # A `docker ps` hiccup (daemon restarting/timeout) must NOT latch self-healing off —
    # otherwise a real crash that coincides with the hiccup is never healed.
    dispatcher = _FakeDispatcher(_dispatcher_status(container_ok=False))
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, llm_ok=False, dispatcher=dispatcher,
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1"},
    )
    _patch_push(monkeypatch)

    asyncio.run(supervisor._maybe_self_heal())
    asyncio.run(supervisor._maybe_self_heal())

    assert dispatcher.calls == []  # ambiguous state → no restart
    assert supervisor._self_heal_blocked is False  # and NOT latched — keeps retrying
    storage.close()


def test_self_heal_restart_uses_remaining_container_warmup_and_still_probes(
    monkeypatch,
    tmp_path,
):
    started_at = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    dispatcher = _FakeDispatcher(
        _dispatcher_status(state="Exited (137) 1 minute ago"),
        up_status=_dispatcher_status(
            state="Up 2 minutes (health: starting)",
            health="starting",
            started_at=started_at,
            inspect_ok=True,
        ),
    )
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, llm_ok=False, dispatcher=dispatcher,
        profile="qwen36-vl",
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1", "JARVIS_SELF_HEALING_GRACE_SEC": "0"},
    )
    _patch_push(monkeypatch)

    asyncio.run(supervisor._maybe_self_heal())
    assert supervisor._self_heal_count == 1
    remaining = supervisor._self_heal_grace_until - time.monotonic()
    assert 770 <= remaining <= 780
    calls_after_restart = supervisor.llm.calls
    supervisor.llm.ok = True

    asyncio.run(supervisor._maybe_self_heal())  # observed warmup deadline does not hide probes
    asyncio.run(supervisor._maybe_self_heal())

    assert supervisor._self_heal_count == 1
    assert supervisor.llm.calls == calls_after_restart + 2
    assert supervisor._self_heal_grace_until == 0.0
    storage.close()


def test_backend_start_does_not_open_blind_grace_for_healthy_model(monkeypatch, tmp_path):
    dispatcher = _FakeDispatcher(_dispatcher_status(state="Up 1 hour", health="healthy"))
    supervisor, storage = _supervisor(
        monkeypatch,
        tmp_path,
        llm_ok=True,
        dispatcher=dispatcher,
        profile="qwen36-vl",
        env={"JARVIS_AUTONOMY_ENABLED": "0", "JARVIS_SELF_HEALING_GRACE_SEC": "0"},
    )

    async def scenario() -> None:
        await supervisor.start()
        try:
            assert supervisor._self_heal_grace_until == 0.0
            calls_before = supervisor.llm.calls
            await supervisor._maybe_self_heal()
            assert supervisor.llm.calls == calls_before + 1
            assert dispatcher.calls == []
        finally:
            await supervisor.stop()

    asyncio.run(scenario())
    storage.close()


def test_external_start_health_starting_is_not_restarted(monkeypatch, tmp_path):
    started_at = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    dispatcher = _FakeDispatcher(
        _dispatcher_status(
            state="Up 2 minutes (health: starting)",
            health="starting",
            started_at=started_at,
            inspect_ok=True,
        )
    )
    supervisor, storage = _supervisor(
        monkeypatch,
        tmp_path,
        llm_ok=False,
        dispatcher=dispatcher,
        profile="qwen36-vl",
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1", "JARVIS_SELF_HEALING_GRACE_SEC": "0"},
    )

    asyncio.run(supervisor._maybe_self_heal())

    assert dispatcher.calls == []
    assert supervisor._self_heal_streak == 0
    first_deadline = supervisor._self_heal_grace_until
    assert 770 <= first_deadline - time.monotonic() <= 780

    asyncio.run(supervisor._maybe_self_heal())

    assert abs(supervisor._self_heal_grace_until - first_deadline) < 1.0
    storage.close()


def test_observed_warmup_deadline_does_not_hide_real_container_crash(monkeypatch, tmp_path):
    started_at = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    dispatcher = _FakeDispatcher(
        _dispatcher_status(
            state="Up 2 minutes (health: starting)",
            health="starting",
            started_at=started_at,
            inspect_ok=True,
        )
    )
    supervisor, storage = _supervisor(
        monkeypatch,
        tmp_path,
        llm_ok=False,
        dispatcher=dispatcher,
        profile="qwen36-vl",
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1"},
    )
    _patch_push(monkeypatch)

    asyncio.run(supervisor._maybe_self_heal())
    assert supervisor._self_heal_grace_until > time.monotonic()

    dispatcher._status = _dispatcher_status(state="Exited (139) 1 second ago")
    asyncio.run(supervisor._maybe_self_heal())

    assert supervisor._self_heal_count == 1
    assert ("restart", OLD_CONTAINER_ID) in dispatcher.calls
    storage.close()


def test_failed_dispatcher_restart_does_not_leave_retry_grace(monkeypatch, tmp_path):
    dispatcher = _FakeDispatcher(
        _dispatcher_status(state="Exited (137) 1 minute ago"),
        up_ok=False,
    )
    supervisor, storage = _supervisor(
        monkeypatch,
        tmp_path,
        llm_ok=False,
        dispatcher=dispatcher,
        profile="qwen36-vl",
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1"},
    )
    _patch_push(monkeypatch)

    asyncio.run(supervisor._maybe_self_heal())
    assert supervisor._self_heal_grace_until == 0.0

    asyncio.run(supervisor._maybe_self_heal())

    assert dispatcher.calls.count(("restart", OLD_CONTAINER_ID)) == 2
    assert supervisor._self_heal_count == 2
    storage.close()


def test_grace_suppresses_running_but_unresponsive_not_hard_crash(monkeypatch, tmp_path):
    # Soft hang during grace must not thrash compose; a real Exited(N) still heals.
    dispatcher = _FakeDispatcher(
        _dispatcher_status(port_open=False, state="Up 5 minutes", health="healthy")
    )
    supervisor, storage = _supervisor(
        monkeypatch,
        tmp_path,
        llm_ok=False,
        dispatcher=dispatcher,
        env={
            "JARVIS_SELF_HEALING_MIN_FAILURES": "1",
            "JARVIS_SELF_HEALING_GRACE_SEC": "300",
        },
    )
    _patch_push(monkeypatch)
    supervisor._self_heal_grace_until = time.monotonic() + 120

    asyncio.run(supervisor._maybe_self_heal())
    assert dispatcher.calls == []
    assert supervisor._self_heal_count == 0

    dispatcher._status = _dispatcher_status(state="Exited (137) 1 second ago")
    asyncio.run(supervisor._maybe_self_heal())
    assert ("restart", OLD_CONTAINER_ID) in dispatcher.calls
    assert supervisor._self_heal_count == 1
    storage.close()


def test_successful_restart_opens_configured_grace_floor(monkeypatch, tmp_path):
    dispatcher = _FakeDispatcher(
        _dispatcher_status(state="Exited (137) 1 minute ago"),
        up_status=_dispatcher_status(
            state="Up 5 seconds",
            health="healthy",
            inspect_ok=True,
        ),
    )
    supervisor, storage = _supervisor(
        monkeypatch,
        tmp_path,
        llm_ok=False,
        dispatcher=dispatcher,
        env={
            "JARVIS_SELF_HEALING_MIN_FAILURES": "1",
            "JARVIS_SELF_HEALING_GRACE_SEC": "90",
        },
    )
    _patch_push(monkeypatch)

    asyncio.run(supervisor._maybe_self_heal())
    remaining = supervisor._self_heal_grace_until - time.monotonic()
    assert 85 <= remaining <= 95
    storage.close()


def test_expired_external_warmup_is_treated_as_unresponsive(monkeypatch, tmp_path):
    started_at = (datetime.now(UTC) - timedelta(seconds=901)).isoformat()
    dispatcher = _FakeDispatcher(
        _dispatcher_status(
            state="Up 15 minutes (health: starting)",
            health="starting",
            started_at=started_at,
            inspect_ok=True,
        )
    )
    supervisor, storage = _supervisor(
        monkeypatch,
        tmp_path,
        llm_ok=False,
        dispatcher=dispatcher,
        profile="qwen36-vl",
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1", "JARVIS_SELF_HEALING_GRACE_SEC": "0"},
    )
    _patch_push(monkeypatch)

    asyncio.run(supervisor._maybe_self_heal())

    assert ("restart", OLD_CONTAINER_ID) in dispatcher.calls
    storage.close()


def test_health_readiness_retries_quickly_until_llm_recovers(monkeypatch, tmp_path):
    supervisor, storage = _supervisor(
        monkeypatch,
        tmp_path,
        llm_ok=False,
        dispatcher=None,
        profile="qwen36-vl",
        env={"JARVIS_HEALTH_INTERVAL_SEC": "300"},
    )

    asyncio.run(supervisor._record_health())
    assert supervisor._health_recovery_pending is True
    assert supervisor._health_poll_interval() == 15.0
    first_llm = next(
        row for row in storage.latest_complete_health(limit=20) if row["component"] == "llm.router"
    )
    assert first_llm["status"] == "warn"

    supervisor.llm.ok = True
    asyncio.run(supervisor._record_health())
    assert supervisor._health_recovery_pending is False
    assert supervisor._health_poll_interval() == 300.0
    recovered_llm = next(
        row for row in storage.latest_complete_health(limit=20) if row["component"] == "llm.router"
    )
    assert recovered_llm["status"] == "ok"
    storage.close()


def test_self_heal_needs_llm_and_flag(monkeypatch, tmp_path):
    dispatcher = _FakeDispatcher(_dispatcher_status())
    # Disabled by flag: no probe, no action even with a crashed container.
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, llm_ok=False, dispatcher=dispatcher,
        env={"JARVIS_SELF_HEALING_ENABLED": "0", "JARVIS_SELF_HEALING_MIN_FAILURES": "1"},
    )
    _patch_push(monkeypatch)
    asyncio.run(supervisor._maybe_self_heal())
    assert dispatcher.calls == []
    assert supervisor.llm.calls == 0  # short-circuits before the health probe
    storage.close()


def test_self_heal_healthy_resets_state(monkeypatch, tmp_path):
    dispatcher = _FakeDispatcher(_dispatcher_status(state="Exited (137) 1 minute ago"))
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, llm_ok=True, dispatcher=dispatcher,
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1"},
    )
    _patch_push(monkeypatch)
    supervisor._self_heal_streak = 3
    supervisor._self_heal_blocked = True

    asyncio.run(supervisor._maybe_self_heal())

    assert supervisor._self_heal_streak == 0
    assert supervisor._self_heal_blocked is False
    assert dispatcher.calls == []  # a live dispatcher is never restarted
    storage.close()


def test_supervisor_status_exposes_self_healing(monkeypatch, tmp_path):
    dispatcher = _FakeDispatcher(_dispatcher_status())
    supervisor, storage = _supervisor(monkeypatch, tmp_path, llm_ok=True, dispatcher=dispatcher)
    status = supervisor.status()
    assert status["self_healing_enabled"] is True
    assert status["self_heal_count"] == 0
    assert "health.self_heal.dispatcher_restart" in status["capabilities"]
    storage.close()
