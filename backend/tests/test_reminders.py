"""Natural-language reminder time parsing + recurrence rollover, storage, and tools."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from datetime import time as dtime

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.reminders import (
    compute_next_due,
    parse_when,
    reminder_zone,
    render_local,
    to_utc_iso,
)
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry

TZ = reminder_zone()
# A fixed reference: Friday 2026-07-17 08:00 local.
NOW = datetime(2026, 7, 17, 8, 0, tzinfo=TZ)


def _p(text, now=NOW):
    return parse_when(text, now=now)


def test_relative_hours():
    r = _p("напомни через 2 часа выпить воды")
    assert r.matched and r.recurrence is None
    assert r.due_local == NOW + timedelta(hours=2)


def test_relative_english_minutes():
    r = _p("in 5 minutes")
    assert r.matched and r.recurrence is None
    assert r.due_local == NOW + timedelta(minutes=5)


def test_iso_timestamp_roundtrips_without_hhmm_misfire():
    # Models often pass absolute ISO for "через 5 минут". The HH:MM heuristic
    # used to misread …T19:55:00+03:00 as 55:00 → 07:00 next day.
    r = _p("2026-07-17T19:55:00+03:00")
    assert r.matched
    assert r.due_local == datetime(2026, 7, 17, 19, 55, tzinfo=TZ)


def test_iso_timestamp_naive_assumes_operator_zone():
    r = _p("2026-07-17T10:30:00")
    assert r.matched
    assert r.due_local == datetime(2026, 7, 17, 10, 30, tzinfo=TZ)


def test_tomorrow_at_ten():
    r = _p("напомни завтра в 10 про встречу")
    assert r.matched
    assert r.due_local == datetime(2026, 7, 18, 10, 0, tzinfo=TZ)


def test_today_explicit_time():
    r = _p("сегодня в 18:30 позвонить маме")
    assert r.due_local == datetime(2026, 7, 17, 18, 30, tzinfo=TZ)


def test_bare_time_rolls_to_tomorrow_when_past():
    # now is 08:00; "в 7" already passed today -> tomorrow 07:00.
    r = _p("в 7 разбуди")
    assert r.due_local == datetime(2026, 7, 18, 7, 0, tzinfo=TZ)


def test_bare_time_today_when_future():
    r = _p("в 10 совещание")
    assert r.due_local == datetime(2026, 7, 17, 10, 0, tzinfo=TZ)


def test_next_weekday():
    # Friday now; "в понедельник" -> next Monday.
    r = _p("в понедельник в 14:00 отчёт")
    assert r.due_local == datetime(2026, 7, 20, 14, 0, tzinfo=TZ)


def test_recurring_daily():
    r = _p("каждый день в 9 зарядка")
    assert r.matched and r.recurrence == {"kind": "daily", "at": "09:00"}
    assert r.due_local == datetime(2026, 7, 17, 9, 0, tzinfo=TZ)


def test_recurring_interval():
    r = _p("каждые 30 минут пить воду")
    assert r.recurrence == {"kind": "interval", "seconds": 1800}


def test_no_match():
    assert _p("просто напоминание без времени").matched is False


def test_to_utc_iso_matches_storage_format():
    r = _p("через 1 час")
    iso = to_utc_iso(r.due_local)
    assert iso.endswith("+00:00") and iso.count(":") == 3  # ...T..:..:..+00:00


def test_compute_next_due_daily_rolls_past_downtime():
    # A daily 09:00 reminder whose due time is 3 days stale must fire once and jump to the
    # next FUTURE 09:00, not replay three missed days.
    stale = datetime(2026, 7, 14, 9, 0, tzinfo=TZ)
    after = datetime(2026, 7, 17, 8, 0, tzinfo=TZ)  # noqa: F841 - documents the "now"
    nxt = compute_next_due({"kind": "daily", "at": "09:00"}, after=after)
    assert nxt == datetime(2026, 7, 17, 9, 0, tzinfo=TZ)
    assert nxt > stale


def test_render_local_roundtrip():
    r = _p("завтра в 10")
    iso = to_utc_iso(r.due_local)
    assert render_local(iso) == "18.07.2026 10:00"


def test_default_time_used_without_explicit_time():
    r = _p("напомни завтра забрать посылку")
    assert r.due_local.time() == dtime(9, 0)


# --- storage + tools wiring ---------------------------------------------------


def _storage(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return settings, storage


def test_storage_reminder_roundtrip(tmp_path, monkeypatch):
    _settings, storage = _storage(tmp_path, monkeypatch)
    storage.ensure_conversation("conv-1")
    r = _p("завтра в 10 про встречу")
    created = storage.create_reminder(
        text="про встречу",
        due_at=to_utc_iso(r.due_local),
        recurrence=r.recurrence,
        conversation_id="conv-1",
        source_text="завтра в 10 про встречу",
    )
    assert created["status"] == "pending"

    listed = storage.list_reminders(status="pending")
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]
    assert listed[0]["text"] == "про встречу"
    assert listed[0]["recurrence"] is None
    assert listed[0]["payload"] == {}

    got = storage.get_reminder(created["id"])
    assert got is not None and got["conversation_id"] == "conv-1"

    # "на неделе" window includes tomorrow; a one-hour window excludes it.
    within_week = storage.list_reminders(
        status="pending", before=to_utc_iso(NOW + timedelta(days=7))
    )
    assert len(within_week) == 1
    within_hour = storage.list_reminders(
        status="pending", before=to_utc_iso(NOW + timedelta(hours=1))
    )
    assert within_hour == []

    cancelled = storage.cancel_reminder(created["id"])
    assert cancelled is not None and cancelled["status"] == "cancelled"
    assert storage.list_reminders(status="pending") == []
    # Cancelling an already-cancelled or unknown reminder is a no-op.
    assert storage.cancel_reminder(created["id"]) is None
    assert storage.cancel_reminder("rem_missing") is None
    storage.close()


def test_claim_due_reminders_one_shot_fires_once(tmp_path, monkeypatch):
    _settings, storage = _storage(tmp_path, monkeypatch)
    created = storage.create_reminder(
        text="позвонить", due_at=to_utc_iso(NOW - timedelta(hours=1))
    )
    now_iso = to_utc_iso(NOW)

    fired = storage.claim_due_reminders(now_iso)
    assert len(fired) == 1 and fired[0]["id"] == created["id"]

    got = storage.get_reminder(created["id"])
    assert got["status"] == "fired" and got["fire_count"] == 1

    # A one-shot never double-fires; the BEGIN IMMEDIATE + status guard holds.
    assert storage.claim_due_reminders(now_iso) == []
    assert storage.list_reminders(status="pending") == []
    storage.close()


def test_claim_due_reminders_recurring_rolls_past_downtime(tmp_path, monkeypatch):
    _settings, storage = _storage(tmp_path, monkeypatch)
    # A daily 09:00 reminder that is three days stale.
    stale = datetime(2026, 7, 14, 9, 0, tzinfo=TZ)
    created = storage.create_reminder(
        text="зарядка",
        due_at=to_utc_iso(stale),
        recurrence={"kind": "daily", "at": "09:00"},
    )
    now_iso = to_utc_iso(datetime(2026, 7, 17, 8, 0, tzinfo=TZ))

    fired = storage.claim_due_reminders(now_iso)
    assert len(fired) == 1  # single fire, no catch-up burst

    got = storage.get_reminder(created["id"])
    assert got["status"] == "pending"  # recurring stays pending
    assert got["fire_count"] == 1
    # Advanced to the next FUTURE 09:00 MSK, strictly after now (not a replay of misses).
    assert got["due_at"] == to_utc_iso(datetime(2026, 7, 17, 9, 0, tzinfo=TZ))

    # Not due again at the same instant.
    assert storage.claim_due_reminders(now_iso) == []
    storage.close()


def test_reminders_tools_end_to_end(tmp_path, monkeypatch):
    settings, storage = _storage(tmp_path, monkeypatch)
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    created = asyncio.run(
        tools.run("reminders.create", {"text": "проверка", "when": "через 1 час"})
    )
    assert created.ok is True
    pending = storage.list_reminders(status="pending")
    assert len(pending) == 1 and pending[0]["text"] == "проверка"

    listed = asyncio.run(tools.run("reminders.list", {"scope": "week"}))
    assert listed.ok is True
    assert "проверка" in listed.summary

    cancelled = asyncio.run(tools.run("reminders.cancel", {"match": "проверка"}))
    assert cancelled.ok is True
    assert storage.list_reminders(status="pending") == []

    # Safe reminders tools run without an approval/authorization in the gated suite.
    assert storage.counters()["tool_runs"] == 3
    storage.close()


def test_reminders_create_rejects_unparseable_time(tmp_path, monkeypatch):
    settings, storage = _storage(tmp_path, monkeypatch)
    tools = ToolRegistry(settings, storage, LLMRouter(settings))
    result = asyncio.run(
        tools.run("reminders.create", {"text": "просто напоминание без времени"})
    )
    assert result.ok is False
    assert storage.list_reminders(status="pending") == []
    storage.close()
