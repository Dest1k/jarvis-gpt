"""Live smoke for Telegram-first remainder features against a running backend.

Usage (PowerShell):
  $env:JARVIS_API_TOKEN = (Get-Content D:\\jarvis\\.jarvis\\api.token -Raw).Trim()
  $env:JARVIS_HOME = "D:\\jarvis"
  $env:PYTHONPATH = "D:\\jarvis-gpt\\backend\\src"
  py -3.11 D:\\jarvis-gpt\\backend\\scripts\\live_tg_remainder_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

import httpx

BASE = os.environ.get("JARVIS_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = (os.environ.get("JARVIS_API_TOKEN") or "").strip()
RESULTS: list[tuple[bool, str, str]] = []


def ok(name: str, detail: str = "") -> None:
    RESULTS.append((True, name, detail))
    print(f"PASS  {name}" + (f" — {detail}" if detail else ""))


def fail(name: str, detail: str = "") -> None:
    RESULTS.append((False, name, detail))
    print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))


def _load_env_local() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env.local"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _tool_data(body: dict) -> dict:
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


async def main() -> int:
    if not TOKEN:
        print("JARVIS_API_TOKEN is required", file=sys.stderr)
        return 2
    _load_env_local()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    stamp = datetime.now().strftime("%H%M%S")

    async with httpx.AsyncClient(base_url=BASE, headers=headers, timeout=180.0) as client:
        # 1) health
        r = await client.get("/health")
        if (
            r.status_code == 200
            and r.json().get("ok") is True
            and r.json().get("profile") == "qwen36-vl"
        ):
            ok("health", f"profile={r.json().get('profile')}")
        else:
            fail("health", f"{r.status_code} {r.text[:200]}")

        # 2) quick-capture memory path
        content = f"live-smoke capture {stamp}: проверить GTD inbox"
        r = await client.post(
            "/api/memory",
            json={
                "content": content,
                "namespace": "inbox",
                "tags": ["capture", "telegram", "gtd", "live-smoke"],
                "importance": 0.6,
            },
        )
        if r.status_code == 200 and r.json().get("namespace") == "inbox":
            ok("quick_capture_memory", f"id={r.json().get('id')}")
        else:
            fail("quick_capture_memory", f"{r.status_code} {r.text[:200]}")

        r = await client.get("/api/memory", params={"q": "live-smoke capture", "limit": 10})
        if r.status_code == 200 and any(
            "live-smoke" in str(item.get("content") or "") for item in r.json()
        ):
            ok("memory_search_inbox")
        else:
            fail("memory_search_inbox", f"{r.status_code} {r.text[:200]}")

        # 3) create passive reminder via tools
        rem_id: str | None = None
        r = await client.post(
            "/api/tools/run",
            json={
                "tool": "reminders.create",
                "arguments": {
                    "text": f"напомни через 2 минуты live-smoke-кнопки {stamp}",
                    "when": "через 2 минуты",
                },
            },
        )
        body = r.json() if r.content else {}
        data = _tool_data(body)
        reminder = data.get("reminder") if isinstance(data.get("reminder"), dict) else {}
        if r.status_code == 200 and body.get("ok") and reminder.get("id"):
            rem_id = str(reminder["id"])
            payload = reminder.get("payload") if isinstance(reminder.get("payload"), dict) else {}
            ok(
                "reminders_create",
                f"id={rem_id} deliver={payload.get('deliver')} "
                f"agent_task={data.get('agent_task')}",
            )
        else:
            fail("reminders_create", f"{r.status_code} {str(body)[:300]}")

        # 4) snooze + ack
        if rem_id:
            r = await client.post(f"/api/reminders/{rem_id}/snooze", json={"minutes": 10})
            if r.status_code == 200 and r.json().get("ok"):
                ok("reminder_snooze", str(r.json().get("detail") or ""))
            else:
                fail("reminder_snooze", f"{r.status_code} {r.text[:200]}")
            r = await client.post(f"/api/reminders/{rem_id}/ack")
            if r.status_code == 200 and r.json().get("ok"):
                ok("reminder_ack", str(r.json().get("detail") or ""))
            else:
                fail("reminder_ack", f"{r.status_code} {r.text[:200]}")
        else:
            fail("reminder_snooze", "no reminder id")
            fail("reminder_ack", "no reminder id")

        # 5) briefing API + scheduled briefing create
        r = await client.get("/api/briefing")
        if r.status_code == 200 and r.json().get("headline"):
            ok("briefing_api", str(r.json().get("headline")))
        else:
            fail("briefing_api", f"{r.status_code} {r.text[:200]}")

        r = await client.post(
            "/api/tools/run",
            json={
                "tool": "reminders.create",
                "arguments": {
                    "text": "каждое утро в 9 присылай сводку по системе",
                    "when": "каждое утро в 9",
                },
            },
        )
        body = r.json() if r.content else {}
        data = _tool_data(body)
        summary = str(body.get("summary") or "")
        if r.status_code == 200 and body.get("ok") and data.get("briefing") is True:
            ok(
                "briefing_schedule_create",
                f"kind={(data.get('reminder') or {}).get('payload', {}).get('kind')}",
            )
            bid = (data.get("reminder") or {}).get("id")
            if bid:
                await client.post(
                    "/api/tools/run",
                    json={"tool": "reminders.cancel", "arguments": {"reminder_id": bid}},
                )
        elif "approval" in summary.lower() or (
            isinstance(data.get("danger_level"), str) and data.get("danger_level") == "danger"
        ):
            ok("briefing_schedule_create", f"approval-gated: {summary[:120]}")
        else:
            fail("briefing_schedule_create", f"{r.status_code} {summary or str(body)[:200]}")

        # 6) cancel idle
        r = await client.post("/api/chat/cancel", json={"notification_chat_id": 424242})
        if r.status_code == 200 and r.json().get("cancelled") is False:
            ok("cancel_idle", str(r.json().get("detail") or ""))
        else:
            fail("cancel_idle", f"{r.status_code} {r.text[:200]}")

        # 7) cancel active long chat
        long_msg = (
            "Сделай подробный пошаговый разбор архитектуры микросервисов: "
            "gateway, service mesh, sagas, outbox, observability — 12 разделов по 5 пунктов. "
            "Не вызывай tools."
        )
        req_id = f"live-smoke-cancel-{stamp}"
        chat_task = asyncio.create_task(
            client.post(
                "/api/chat",
                json={
                    "message": long_msg,
                    "request_id": req_id,
                    "notification_chat_id": 900001,
                    "thinking_enabled": False,
                },
                timeout=180.0,
            )
        )
        await asyncio.sleep(2.5)
        r_cancel = await client.post(
            "/api/chat/cancel",
            json={"request_id": req_id, "notification_chat_id": 900001},
        )
        cancelled_flag = (
            r_cancel.status_code == 200 and r_cancel.json().get("cancelled") is True
        )
        answer = ""
        chat_status: object
        try:
            r_chat = await asyncio.wait_for(chat_task, timeout=90)
            chat_status = r_chat.status_code
            if r_chat.status_code == 200:
                answer = (r_chat.json().get("answer") or "")[:180]
            else:
                answer = r_chat.text[:180]
        except Exception as exc:  # noqa: BLE001
            chat_status = type(exc).__name__
            answer = str(exc)[:180]
        if cancelled_flag:
            ok(
                "cancel_active_turn",
                f"chat_status={chat_status} answer={answer!r}",
            )
        else:
            fail(
                "cancel_active_turn",
                f"cancel={r_cancel.status_code}/{r_cancel.text[:120]} "
                f"chat={chat_status} {answer}",
            )

        # 8) real Telegram push with inline buttons
        from jarvis_gpt.notify import push_telegram_alert, reminder_inline_keyboard

        targets: list[int] = []
        for key in ("TELEGRAM_ALERT_CHAT_IDS", "TELEGRAM_ALLOWED_CHAT_IDS"):
            raw = os.environ.get(key) or ""
            for part in raw.replace(";", ",").split(","):
                part = part.strip()
                if part.lstrip("-").isdigit():
                    targets.append(int(part))
        targets = list(dict.fromkeys(targets))
        if not targets:
            fail("telegram_push_buttons", "no chat ids configured")
        else:
            delivered = await push_telegram_alert(
                f"🧪 Live smoke {stamp}: кнопки snooze/done (если видишь — push OK)",
                target_chat_ids=targets[:1],
                reply_markup=reminder_inline_keyboard(rem_id or f"rem_live_{stamp}"),
            )
            if delivered:
                ok("telegram_push_buttons", f"chat={targets[0]}")
            else:
                fail("telegram_push_buttons", f"not delivered targets={targets[:1]}")

        # 9) files / image download path (sendPhoto substrate)
        r = await client.get("/api/files", params={"limit": 50})
        if r.status_code == 200:
            files = r.json()
            images = [
                item
                for item in files
                if str(item.get("mime_type") or "").startswith("image/")
                or str(item.get("name") or "").lower().endswith(
                    (".png", ".jpg", ".jpeg", ".webp", ".gif")
                )
            ]
            ok("files_list", f"total={len(files)} images={len(images)}")
            if images:
                img = images[0]
                dr = await client.get(f"/api/files/{img['id']}/download")
                if dr.status_code == 200 and dr.content:
                    ok(
                        "image_download",
                        f"name={img.get('name')} bytes={len(dr.content)}",
                    )
                else:
                    fail("image_download", f"{dr.status_code}")
            else:
                ok("image_download", "no images in storage — unit coverage only")
        else:
            fail("files_list", f"{r.status_code}")

        # 10) in-process fire of passive reminder with buttons (uses live DB + TG)
        try:
            from jarvis_gpt.config import ensure_runtime_dirs, load_settings
            from jarvis_gpt.storage import JarvisStorage
            from jarvis_gpt.supervisor import RuntimeSupervisor

            settings = load_settings()
            ensure_runtime_dirs(settings)
            storage = JarvisStorage(settings.database_path)
            storage.initialize()
            supervisor = RuntimeSupervisor(settings=settings, storage=storage)
            target = targets[0] if targets else None
            fire_rem = storage.create_reminder(
                text=f"live-smoke fire {stamp}",
                due_at="2000-01-01T00:00:00+00:00",
                payload={
                    "deliver": "telegram",
                    **({"telegram_chat_id": target} if target is not None else {}),
                },
            )
            delivered = await supervisor._deliver_passive_reminder(fire_rem)
            if delivered:
                ok(
                    "passive_reminder_fire_buttons",
                    f"id={fire_rem['id']} chat={target}",
                )
            else:
                fail("passive_reminder_fire_buttons", "push returned False")
            storage.close()
        except Exception as exc:  # noqa: BLE001
            fail("passive_reminder_fire_buttons", f"{type(exc).__name__}: {exc}")

    passed = sum(1 for item in RESULTS if item[0])
    total = len(RESULTS)
    print("\n==== SUMMARY ====")
    print(f"{passed}/{total} passed")
    for good, name, detail in RESULTS:
        if not good:
            print(f"  FAIL {name}: {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
