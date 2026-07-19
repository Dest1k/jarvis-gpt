"""Live smoke for Telegram day-console batch 3."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx

from jarvis_gpt.notify import (
    answer_action_keyboard,
    in_quiet_hours,
    operator_reply_keyboard,
    parse_quiet_hours,
    push_telegram_alert,
    reminder_inline_keyboard,
)
from jarvis_gpt.telegram_bridge import (
    _build_forward_task_prompt,
    _format_briefing_card,
    _format_status_card,
    _is_forwarded_message,
)

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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def main() -> int:
    if not TOKEN:
        print("JARVIS_API_TOKEN required", file=sys.stderr)
        return 2
    _load_env_local()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    stamp = datetime.now().strftime("%H%M%S")

    # Pure helpers
    if parse_quiet_hours("23:00-08:00") is not None:
        ok("quiet_hours_parse")
    else:
        fail("quiet_hours_parse")
    if _is_forwarded_message({"forward_date": 1, "text": "x"}):
        ok("forward_detect")
    else:
        fail("forward_detect")
    prompt = _build_forward_task_prompt(
        {
            "forward_date": 1,
            "forward_from": {"first_name": "Live", "username": "live"},
            "text": "https://example.com/live-smoke",
        },
        "https://example.com/live-smoke",
    )
    if "forward-as-task" in prompt and "example.com" in prompt:
        ok("forward_prompt")
    else:
        fail("forward_prompt", prompt[:120])
    if operator_reply_keyboard().get("keyboard") and answer_action_keyboard().get(
        "inline_keyboard"
    ):
        ok("keyboards")
    else:
        fail("keyboards")

    async with httpx.AsyncClient(base_url=BASE, headers=headers, timeout=60.0) as client:
        r = await client.get("/health")
        if r.status_code == 200 and r.json().get("profile") == "qwen36-vl":
            ok("health")
        else:
            fail("health", f"{r.status_code}")

        r = await client.get("/api/status")
        if r.status_code == 200:
            card = _format_status_card(r.json())
            ok("status_api", card.splitlines()[0][:80])
        else:
            fail("status_api", f"{r.status_code} {r.text[:120]}")

        r = await client.get("/api/briefing")
        if r.status_code == 200:
            card = _format_briefing_card(r.json())
            ok("briefing_api", card.splitlines()[0][:80])
        else:
            fail("briefing_api", f"{r.status_code} {r.text[:120]}")

        # Prefer quiet hours set for smoke (optional)
        r = await client.get("/api/preferences")
        quiet = ""
        if r.status_code == 200:
            quiet = str(r.json().get("quiet_hours") or "")
            ok("preferences", f"quiet_hours={quiet!r}")
        else:
            fail("preferences", f"{r.status_code}")

        targets: list[int] = []
        for key in ("TELEGRAM_ALERT_CHAT_IDS", "TELEGRAM_ALLOWED_CHAT_IDS"):
            for part in (os.environ.get(key) or "").replace(";", ",").split(","):
                part = part.strip()
                if part.lstrip("-").isdigit():
                    targets.append(int(part))
        targets = list(dict.fromkeys(targets))
        if not targets:
            fail("tg_push_day_console", "no chat ids")
        else:
            silent = in_quiet_hours(quiet or "00:00-00:01")  # force false unless set
            # Demo push: forward-as-task tip + action keyboard sample
            delivered = await push_telegram_alert(
                (
                    f"🧪 Day-console smoke {stamp}\n"
                    "• Перешли любое сообщение боту — разберу как задачу\n"
                    "• Пульт: /start\n"
                    "• Под ответами: Inbox / +1ч / Ещё"
                ),
                target_chat_ids=targets[:1],
                reply_markup=answer_action_keyboard(),
                disable_notification=False,
            )
            if delivered:
                ok("tg_push_day_console", f"chat={targets[0]} silent={silent}")
            else:
                fail("tg_push_day_console", "not delivered")

            # Also push a silent-style message when quiet hours are configured and active
            if quiet and in_quiet_hours(quiet):
                delivered2 = await push_telegram_alert(
                    f"🌙 Quiet-hours silent ping {stamp}",
                    target_chat_ids=targets[:1],
                    disable_notification=True,
                    reply_markup=reminder_inline_keyboard(f"rem_quiet_{stamp}"),
                )
                if delivered2:
                    ok("tg_quiet_silent_push")
                else:
                    fail("tg_quiet_silent_push")
            else:
                ok("tg_quiet_silent_push", "quiet hours inactive/empty — skipped")

    passed = sum(1 for x in RESULTS if x[0])
    total = len(RESULTS)
    print(f"\n==== SUMMARY {passed}/{total} ====")
    for good, name, detail in RESULTS:
        if not good:
            print(f"  FAIL {name}: {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
