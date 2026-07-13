#!/usr/bin/env python3
"""Capture a bounded profile/process/model snapshot as JSON evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def command(arguments: list[str], timeout: int = 20) -> dict[str, object]:
    started = time.perf_counter()
    completed = subprocess.run(
        arguments,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return {
        "arguments": arguments,
        "returncode": completed.returncode,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def get(url: str, timeout: int = 5) -> dict[str, object]:
    started = time.perf_counter()
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            try:
                body: object = json.loads(raw)
            except json.JSONDecodeError:
                body = raw
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "body": body,
            }
    except HTTPError as exc:
        return {
            "ok": False,
            "status": exc.code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": exc.read().decode("utf-8", "replace")[:2000],
        }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "status": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--container", default="jarvis-gpt-dispatcher")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--note", default="")
    args = parser.parse_args()

    result = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "profile": args.profile,
        "note": args.note,
        "backend_health": get("http://127.0.0.1:8000/health"),
        "backend_status": get("http://127.0.0.1:8000/api/status"),
        "model_health": get("http://127.0.0.1:8001/health"),
        "model_catalog": get("http://127.0.0.1:8001/v1/models"),
        "docker_ps": command(
            ["docker", "ps", "--filter", f"name={args.container}", "--format", "{{json .}}"]
        ),
        "docker_inspect": command(["docker", "inspect", args.container]),
        "docker_stats": command(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                args.container,
            ]
        ),
        "docker_logs_tail": command(["docker", "logs", "--tail", "250", args.container]),
        "nvidia_smi": command(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.free,temperature.gpu,power.draw",
                "--format=csv,noheader",
            ]
        ),
        "ports": {str(port): port_open(port) for port in (3000, 8000, 8001, 8765)},
        "candidate_processes": command(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*jarvis-gpt*' -or $_.CommandLine -like '*audit-functional*' } | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Depth 3",
            ]
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "model_health": result["model_health"]["ok"],
                "model_catalog": result["model_catalog"]["ok"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
