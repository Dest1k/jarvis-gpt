#!/usr/bin/env python3
"""Run one approved hermetic audit command and persist reproducible evidence."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


RUN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUN_ROOT.parents[2]
EVIDENCE_ROOT = RUN_ROOT / "evidence" / "static"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("test_id")
    parser.add_argument("evidence_id")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("command is required")

    cwd = (REPO_ROOT / args.cwd).resolve()
    if REPO_ROOT not in cwd.parents and cwd != REPO_ROOT and not str(cwd).startswith("/tmp/"):
        parser.error("cwd must be inside the checkout or /tmp")

    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    stem = f"{args.evidence_id}_{args.test_id}"
    stdout_path = EVIDENCE_ROOT / f"{stem}.stdout.txt"
    stderr_path = EVIDENCE_ROOT / f"{stem}.stderr.txt"
    meta_path = EVIDENCE_ROOT / f"{stem}.json"
    started = datetime.now(UTC)
    t0 = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            args.command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout,
            check=False,
            env={**os.environ, "PYTHONHASHSEED": "0", "NO_COLOR": "1"},
        )
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = 124
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\nTIMEOUT after {args.timeout}s\n"
    duration = time.monotonic() - t0
    finished = datetime.now(UTC)
    stdout_path.write_text(stdout, encoding="utf-8", errors="replace")
    stderr_path.write_text(stderr, encoding="utf-8", errors="replace")
    metadata = {
        "schema_version": 1,
        "test_id": args.test_id,
        "evidence_id": args.evidence_id,
        "started_utc": started.isoformat().replace("+00:00", "Z"),
        "finished_utc": finished.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round(duration, 6),
        "command_argv": args.command,
        "command_display": shlex.join(args.command),
        "working_directory": str(cwd),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_path": str(stdout_path.relative_to(REPO_ROOT)),
        "stderr_path": str(stderr_path.relative_to(REPO_ROOT)),
        "environment": {
            "python": sys.version.replace("\n", " "),
            "platform": platform.platform(),
        },
    }
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
