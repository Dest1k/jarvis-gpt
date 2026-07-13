#!/usr/bin/env python3
"""Owned occupied-port and interrupted-start launcher fixtures for an offline stack."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from typing import Any


PORTS = (3000, 8000, 8001, 8765)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def port_map() -> dict[str, bool]:
    return {str(port): port_open(port) for port in PORTS}


def read_temp(stream) -> str:
    stream.flush()
    stream.seek(0)
    return stream.read().decode("utf-8", "replace")


def run(command: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    timed_out = False
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=timeout,
                check=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = None
        return {
            "command": command,
            "returncode": returncode,
            "timed_out": timed_out,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "stdout": read_temp(stdout_file),
            "stderr": read_temp(stderr_file),
        }


def launcher(repo: Path, action: str, profile: str, home: Path, model_root: Path) -> list[str]:
    return [
        "cmd.exe",
        "/d",
        "/s",
        "/c",
        str((repo / "jarvis.cmd").resolve()),
        action,
        "-Profile",
        profile,
        "-HomePath",
        str(home.resolve()),
        "-ModelRoot",
        str(model_root.resolve()),
    ]


def inspect_runtime() -> dict[str, Any]:
    return {
        "ports": port_map(),
        "docker": run(
            ["docker", "ps", "-a", "--filter", "name=jarvis-gpt", "--format", "{{json .}}"],
            Path.cwd(),
            20,
        ),
        "netstat": run(["cmd.exe", "/d", "/c", "netstat", "-ano"], Path.cwd(), 20),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--profile", default="gemma4-turbo")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")

    initial_ports = port_map()
    if any(initial_ports.values()):
        raise SystemExit(f"fixture requires an offline stack: {initial_ports}")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 8001))
    listener.listen(2)
    listener.settimeout(2)
    occupied_started = datetime.now(timezone.utc).isoformat()
    occupied = run(
        launcher(args.repo, "start", args.profile, args.home, args.model_root),
        args.repo,
        120,
    )
    listener_survived = False
    try:
        listener_survived = listener.fileno() >= 0 and listener.getsockname() == ("127.0.0.1", 8001)
    finally:
        listener.close()
    occupied_inspect = inspect_runtime()
    occupied_cleanup = run(
        launcher(args.repo, "stop", args.profile, args.home, args.model_root),
        args.repo,
        180,
    )
    time.sleep(2)
    after_occupied_cleanup = inspect_runtime()

    interrupt_stdout = tempfile.TemporaryFile()
    interrupt_stderr = tempfile.TemporaryFile()
    interrupt_command = launcher(args.repo, "start", args.profile, args.home, args.model_root)
    interrupt_started_at = datetime.now(timezone.utc).isoformat()
    interrupt_started = time.perf_counter()
    process = subprocess.Popen(
        interrupt_command,
        cwd=args.repo,
        stdout=interrupt_stdout,
        stderr=interrupt_stderr,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    time.sleep(12)
    exited_before_interrupt = process.poll() is not None
    taskkill: dict[str, Any] | None = None
    if not exited_before_interrupt:
        taskkill = run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            args.repo,
            30,
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    interrupt_record = {
        "command": interrupt_command,
        "pid": process.pid,
        "started_at": interrupt_started_at,
        "elapsed_ms": round((time.perf_counter() - interrupt_started) * 1000, 2),
        "exited_before_interrupt": exited_before_interrupt,
        "returncode": process.returncode,
        "taskkill": taskkill,
        "stdout": read_temp(interrupt_stdout),
        "stderr": read_temp(interrupt_stderr),
    }
    interrupt_stdout.close()
    interrupt_stderr.close()
    interrupted_inspect = inspect_runtime()
    interrupted_cleanup = run(
        launcher(args.repo, "stop", args.profile, args.home, args.model_root),
        args.repo,
        180,
    )
    time.sleep(2)
    final_inspect = inspect_runtime()

    result = {
        "schema": "jarvis.functional-startup-fault-fixtures.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "home": str(args.home.resolve()),
        "profile": args.profile,
        "initial_ports": initial_ports,
        "occupied_port": {
            "listener_pid": os.getpid(),
            "port": 8001,
            "started_at": occupied_started,
            "launcher": occupied,
            "listener_survived": listener_survived,
            "after_launcher": occupied_inspect,
            "cleanup": occupied_cleanup,
            "after_cleanup": after_occupied_cleanup,
        },
        "interrupted_start": {
            "launcher": interrupt_record,
            "after_interrupt": interrupted_inspect,
            "cleanup": interrupted_cleanup,
            "after_cleanup": final_inspect,
        },
    }
    result["summary"] = {
        "occupied_launcher_nonzero": occupied["returncode"] not in (0, None),
        "occupied_listener_survived": listener_survived,
        "occupied_cleanup_ports_closed": not any(after_occupied_cleanup["ports"].values()),
        "interrupt_was_applied": not exited_before_interrupt and taskkill is not None,
        "interrupt_cleanup_ports_closed": not any(final_inspect["ports"].values()),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0 if all(result["summary"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
