#!/usr/bin/env python3
"""Run one standard jarvis.cmd action and persist complete bounded evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--action", choices=("start", "stop", "restart", "status", "doctor"), required=True)
    parser.add_argument("--profile")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--model-root", type=Path)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")

    command = [str((args.repo / "jarvis.cmd").resolve()), args.action]
    if args.profile:
        command.extend(("-Profile", args.profile))
    if args.home:
        command.extend(("-HomePath", str(args.home.resolve())))
    if args.model_root:
        command.extend(("-ModelRoot", str(args.model_root.resolve())))

    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    timed_out = False
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            completed = subprocess.run(
                ["cmd.exe", "/d", "/s", "/c", *command],
                cwd=args.repo,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=args.timeout,
                check=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = None
        stdout_file.flush()
        stderr_file.flush()
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read().decode("utf-8", "replace")
        stderr = stderr_file.read().decode("utf-8", "replace")

    record = {
        "schema": "jarvis.functional-launcher-action.v1",
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "command": command,
        "timeout_sec": args.timeout,
        "timed_out": timed_out,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: record[key] for key in ("returncode", "timed_out", "elapsed_ms")}, ensure_ascii=False))
    return 0 if returncode == 0 and not timed_out else 1


if __name__ == "__main__":
    raise SystemExit(main())
