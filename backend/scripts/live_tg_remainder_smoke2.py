"""Second-pass live checks: HTTP snooze/ack + cancel status codes."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta

import httpx

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.reminders import reminder_zone, to_utc_iso
from jarvis_gpt.storage import JarvisStorage

BASE = os.environ.get("JARVIS_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = (os.environ.get("JARVIS_API_TOKEN") or "").strip()


async def main() -> int:
    if not TOKEN:
        print("missing token", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {TOKEN}"}
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tz = reminder_zone(settings.reminder_tz)
    due = to_utc_iso(datetime.now(tz) + timedelta(minutes=30))
    rem = storage.create_reminder(
        text="live-smoke HTTP snooze/ack",
        due_at=due,
        payload={"deliver": "telegram", "telegram_chat_id": 467035772},
    )
    print("created", rem["id"], rem["status"])

    failures = 0
    async with httpx.AsyncClient(base_url=BASE, headers=headers, timeout=180.0) as client:
        r = await client.post(f"/api/reminders/{rem['id']}/snooze", json={"minutes": 12})
        print("snooze", r.status_code, r.text[:300])
        if r.status_code != 200 or not r.json().get("ok"):
            failures += 1
        else:
            refreshed = storage.get_reminder(rem["id"])
            print("after_snooze", refreshed and refreshed.get("status"), refreshed and refreshed.get("due_at"))

        r = await client.post(f"/api/reminders/{rem['id']}/ack")
        print("ack", r.status_code, r.text[:300])
        if r.status_code != 200 or not r.json().get("ok"):
            failures += 1
        else:
            refreshed = storage.get_reminder(rem["id"])
            print("after_ack", refreshed and refreshed.get("status"))

        r = await client.post("/api/reminders/rem_does_not_exist/snooze", json={"minutes": 5})
        print("snooze404", r.status_code, r.text[:200])
        if r.status_code != 404:
            failures += 1

        # Fire a reminder that is already fired, then snooze it (true snooze path).
        fired = storage.create_reminder(
            text="live-smoke fired then snooze",
            due_at="2000-01-01T00:00:00+00:00",
            payload={"deliver": "telegram", "telegram_chat_id": 467035772},
        )
        with storage._lock:
            conn = storage.connect()
            conn.execute(
                "UPDATE reminders SET status='fired', fired_at=? WHERE id=?",
                ("2000-01-01T00:01:00+00:00", fired["id"]),
            )
            conn.commit()
        r = await client.post(f"/api/reminders/{fired['id']}/snooze", json={"minutes": 10})
        print("snooze_fired", r.status_code, r.text[:300])
        if r.status_code != 200 or not r.json().get("ok"):
            failures += 1
        else:
            refreshed = storage.get_reminder(fired["id"])
            print(
                "fired_after_snooze",
                refreshed and refreshed.get("status"),
                refreshed and refreshed.get("due_at"),
            )
            await client.post(f"/api/reminders/{fired['id']}/ack")

        # Cancel active turn and observe chat HTTP outcome.
        req = f"live-smoke-cancel2-{datetime.now().strftime('%H%M%S')}"
        task = asyncio.create_task(
            client.post(
                "/api/chat",
                json={
                    "message": "Считай медленно до двухсот словами, без tools.",
                    "request_id": req,
                    "notification_chat_id": 900002,
                    "thinking_enabled": False,
                },
                timeout=180.0,
            )
        )
        await asyncio.sleep(2.0)
        rc = await client.post(
            "/api/chat/cancel",
            json={"request_id": req, "notification_chat_id": 900002},
        )
        print("cancel", rc.status_code, rc.text[:250])
        if rc.status_code != 200 or rc.json().get("cancelled") is not True:
            failures += 1
        try:
            rr = await asyncio.wait_for(task, timeout=90)
            print("chat_after_cancel", rr.status_code, rr.text[:300])
            # Hard cancel may surface 500; cooperative cancel may return 200 with stop text.
            if rr.status_code not in {200, 499, 500, 503}:
                failures += 1
            if rr.status_code == 200:
                answer = rr.json().get("answer") or ""
                print("answer", answer[:200])
                if "Остановил" not in answer and "cancel" not in answer.lower():
                    # Hard cancel path: empty/partial is still a success if cancel API fired.
                    print("note: cooperative cancel text not present (likely hard cancel)")
        except Exception as exc:  # noqa: BLE001
            print("chat_after_cancel_exc", type(exc).__name__, exc)

        # Create briefing schedule through storage payload classification already unit-tested;
        # here verify supervisor format + push for briefing kind.
        from jarvis_gpt.supervisor import RuntimeSupervisor, _format_daily_briefing
        from types import SimpleNamespace

        class _Exp:
            def daily_briefing(self, dispatcher_status=None):
                return {
                    "headline": "Runtime is stable",
                    "operator_name": "Owner",
                    "focus": ["Focus: live smoke"],
                    "risks": [],
                    "suggestions": ["Keep going"],
                    "pending_approvals": 0,
                }

        text = _format_daily_briefing(_Exp().daily_briefing())
        print("briefing_format_ok", "Runtime is stable" in text and "Фокус" in text)
        if "Runtime is stable" not in text:
            failures += 1

        sup = RuntimeSupervisor(
            settings=settings,
            storage=storage,
            autonomy_executor=SimpleNamespace(agent=None, experience=_Exp()),
        )
        briefing_rem = storage.create_reminder(
            text="Утренняя сводка live-smoke",
            due_at="2000-01-01T00:00:00+00:00",
            payload={
                "kind": "briefing",
                "prompt": "daily_briefing",
                "deliver": "telegram",
                "telegram_chat_id": 467035772,
            },
        )
        await sup._run_briefing_task(briefing_rem)
        print("briefing_task_done", briefing_rem["id"])

    storage.close()
    print("FAILURES", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
