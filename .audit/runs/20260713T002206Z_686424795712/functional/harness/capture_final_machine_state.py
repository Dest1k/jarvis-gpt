#!/usr/bin/env python3
"""Capture final Docker/WSL/port state after campaign-owned cleanup."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess


def run(arguments: list[str], timeout: int = 20) -> dict[str, object]:
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
        "stdout": completed.stdout.replace("\x00", ""),
        "stderr": completed.stderr.replace("\x00", ""),
    }


def open_port(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def main() -> int:
    output = Path(__file__).resolve().parents[1] / "evidence" / "final-machine-baseline-restored.json"
    if output.exists():
        raise SystemExit(f"refusing to overwrite {output}")
    document = {
        "schema": "jarvis.functional-final-machine-state.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "ports": {str(port): open_port(port) for port in (3000, 8000, 8001, 8765)},
        "docker_desktop_status": run(["docker", "desktop", "status"]),
        "docker_engine_info": run(["docker", "info", "--format", "{{.ServerVersion}}"]),
        "wsl": run(["wsl.exe", "--list", "--verbose"]),
        "candidate_processes": run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*jarvis-gpt*' -or $_.CommandLine -like '*audit-functional*' -or $_.Name -in @('vllm.exe','node.exe') } | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Depth 3",
            ]
        ),
    }
    document["summary"] = {
        "ports_closed": not any(document["ports"].values()),
        "docker_desktop_stopped": document["docker_desktop_status"]["returncode"] != 0,
        "docker_engine_unavailable": document["docker_engine_info"]["returncode"] != 0,
        "wsl_docker_desktop_stopped": "docker-desktop" in document["wsl"]["stdout"] and "Stopped" in document["wsl"]["stdout"],
    }
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(document["summary"], ensure_ascii=False))
    return 0 if all(document["summary"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
