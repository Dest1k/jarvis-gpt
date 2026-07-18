from __future__ import annotations

import asyncio

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.supervisor import RuntimeSupervisor, _container_exit_code


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
    def __init__(self, status: dict, *, up_ok: bool = True) -> None:
        self._status = status
        self.up_ok = up_ok
        self.calls: list[tuple[str, str]] = []

    def status(self) -> dict:
        return self._status

    def run_compose(self, action: str) -> dict:
        self.calls.append(("compose", action))
        return {"ok": True, "summary": f"{action} ok"}

    def run_compose_verified(self, action: str) -> dict:
        self.calls.append(("verified", action))
        return {"ok": self.up_ok, "summary": f"{action} verified"}


def _dispatcher_status(
    *,
    docker: bool = True,
    port_open: bool = False,
    exists: bool = True,
    state: str = "Exited (137) 2 minutes ago",
    container_ok: bool = True,
) -> dict:
    if not container_ok:
        # How dispatcher._container_status reports a failed `docker ps` (daemon
        # down/restarting, timeout): an error dict with no "exists" key.
        container: dict = {"ok": False, "error": "docker ps failed"}
    else:
        container = {"ok": True, "exists": exists}
        if exists:
            container["status"] = state
    return {
        "docker_available": docker,
        "port_open": port_open,
        "container_status": container,
    }


def _supervisor(
    monkeypatch, tmp_path, *, llm_ok: bool, dispatcher: _FakeDispatcher | None, bus=None, env=None
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
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

    # down then verified up, so a hung/crashed container is fully replaced.
    assert ("compose", "down") in dispatcher.calls
    assert ("verified", "up") in dispatcher.calls
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

    assert ("verified", "up") in dispatcher.calls
    assert supervisor._self_heal_count == 1
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
    assert ("verified", "up") in dispatcher.calls
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


def test_self_heal_grace_suppresses_reprobe_after_restart(monkeypatch, tmp_path):
    # After a restart, the grace window must suppress the next probe entirely so a big
    # model reloading is not mistaken for a fresh failure and re-restarted mid-warmup.
    dispatcher = _FakeDispatcher(_dispatcher_status(state="Exited (137) 1 minute ago"))
    supervisor, storage = _supervisor(
        monkeypatch, tmp_path, llm_ok=False, dispatcher=dispatcher,
        env={"JARVIS_SELF_HEALING_MIN_FAILURES": "1", "JARVIS_SELF_HEALING_GRACE_SEC": "600"},
    )
    _patch_push(monkeypatch)

    asyncio.run(supervisor._maybe_self_heal())  # restart #1, opens the grace window
    assert supervisor._self_heal_count == 1
    calls_after_restart = supervisor.llm.calls

    asyncio.run(supervisor._maybe_self_heal())  # within grace → no probe, no restart
    asyncio.run(supervisor._maybe_self_heal())

    assert supervisor._self_heal_count == 1  # no thrash during warmup
    assert supervisor.llm.calls == calls_after_restart  # health not even probed in grace
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
