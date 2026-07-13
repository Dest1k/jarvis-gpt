#!/usr/bin/env python3
"""Short watchdog-bounded turbo soak with health, chat, state, and resource samples."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
import uuid

import httpx


def command(arguments: list[str]) -> dict[str, object]:
    result = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )
    return {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--duration-sec", type=int, default=120)
    parser.add_argument("--sample-sec", type=int, default=5)
    parser.add_argument("--chat-sec", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")
    marker = f"functional-soak-{uuid.uuid4().hex[:12]}"
    samples: list[dict[str, object]] = []
    chats: list[dict[str, object]] = []
    failures: list[str] = []
    started_monotonic = time.monotonic()
    deadline = started_monotonic + args.duration_sec
    next_chat = started_monotonic

    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=20, trust_env=False) as client:
        health = client.get("/health").json()
        if Path(health.get("home", "")).resolve() != args.home.resolve():
            raise SystemExit("unexpected runtime home")
        if health.get("profile") != args.profile:
            raise SystemExit(f"unexpected runtime profile: {health.get('profile')}")
        while time.monotonic() < deadline:
            cycle_started = time.monotonic()
            try:
                health_response = client.get("/health")
                status_response = client.get("/api/status")
                status_body = status_response.json() if status_response.status_code == 200 else {}
                counters = status_body.get("counters", {}) if isinstance(status_body, dict) else {}
                samples.append(
                    {
                        "at_sec": round(cycle_started - started_monotonic, 3),
                        "health_status": health_response.status_code,
                        "health": health_response.json() if health_response.status_code == 200 else None,
                        "status_status": status_response.status_code,
                        "counters": counters,
                        "docker_stats": command(
                            [
                                "docker",
                                "stats",
                                "--no-stream",
                                "--format",
                                "{{json .}}",
                                "jarvis-gpt-dispatcher",
                            ]
                        ),
                        "nvidia_smi": command(
                            [
                                "nvidia-smi",
                                "--query-gpu=utilization.gpu,memory.used,memory.free,temperature.gpu,power.draw",
                                "--format=csv,noheader",
                            ]
                        ),
                        "sizes": {
                            "state": tree_size(args.home / "data" / "jarvis-gpt" / "state"),
                            "logs": tree_size(args.home / "logs" / "jarvis-gpt"),
                            "cache": tree_size(args.home / "cache" / "jarvis-gpt"),
                        },
                    }
                )
            except Exception as exc:  # evidence capture must continue until watchdog deadline
                failures.append(f"sample@{cycle_started - started_monotonic:.3f}s:{type(exc).__name__}:{exc}")

            if cycle_started >= next_chat:
                prompt = f"[{marker}:{len(chats) + 1}] Ответь одним словом: стабильно."
                chat_started = time.perf_counter()
                try:
                    response = client.post(
                        "/api/chat",
                        json={
                            "message": prompt,
                            "mode": "chat",
                            "max_tokens": 32,
                            "thinking_enabled": False,
                        },
                    )
                    body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                    chats.append(
                        {
                            "prompt": prompt,
                            "status": response.status_code,
                            "elapsed_ms": round((time.perf_counter() - chat_started) * 1000, 2),
                            "conversation_id": body.get("conversation_id") if isinstance(body, dict) else None,
                            "message_id": body.get("message_id") if isinstance(body, dict) else None,
                            "answer": body.get("answer") if isinstance(body, dict) else None,
                            "duration_ms": body.get("duration_ms") if isinstance(body, dict) else None,
                        }
                    )
                except Exception as exc:
                    failures.append(f"chat@{cycle_started - started_monotonic:.3f}s:{type(exc).__name__}:{exc}")
                next_chat += args.chat_sec

            sleep_for = min(args.sample_sec, max(0, deadline - time.monotonic()))
            if sleep_for:
                time.sleep(sleep_for)

    elapsed = time.monotonic() - started_monotonic
    valid_samples = [
        item
        for item in samples
        if item["health_status"] == 200
        and item["status_status"] == 200
        and item["health"].get("profile") == args.profile
    ]
    result = {
        "schema": "jarvis.functional-bounded-soak.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "home": str(args.home.resolve()),
        "profile": args.profile,
        "marker": marker,
        "configured_duration_sec": args.duration_sec,
        "elapsed_sec": round(elapsed, 3),
        "samples": samples,
        "chats": chats,
        "failures": failures,
        "summary": {
            "sample_count": len(samples),
            "valid_sample_count": len(valid_samples),
            "chat_count": len(chats),
            "chat_http_200": sum(item["status"] == 200 for item in chats),
            "chat_nonempty": sum(bool(str(item.get("answer") or "").strip()) for item in chats),
            "watchdog_respected": elapsed <= args.duration_sec + args.sample_sec + 15,
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0 if len(valid_samples) == len(samples) and result["summary"]["chat_http_200"] == len(chats) else 1


if __name__ == "__main__":
    raise SystemExit(main())
