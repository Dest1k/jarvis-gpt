#!/usr/bin/env python3
"""Exercise CLI mutation guards against the isolated API-owned runtime."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")

    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=15, trust_env=False) as client:
        health = client.get("/health").json()
        if Path(health.get("home", "")).resolve() != args.home.resolve():
            raise SystemExit("unexpected runtime home")
        persona_before = client.get("/api/persona").json()
        approvals_before = client.get("/api/approvals").json()
        policy_before = client.get("/api/autonomy/policy").json()

    probe_path = args.home / "data" / "jarvis-gpt" / "functional-cli-guard-probe.txt"
    if probe_path.exists():
        raise SystemExit(f"probe path already exists: {probe_path}")
    commands = (
        ("persona-set", "--set", "functional_guard_probe=SHOULD_NOT_APPLY"),
        (
            "approval-request",
            "Functional guard probe",
            "This request must be rejected while the API owns executive state.",
            "--risk",
            "review",
            "--action",
            "fs.write",
            "--payload",
            "{}",
        ),
        (
            "tool-run",
            "filesystem.write_text",
            "--arguments",
            json.dumps({"path": str(probe_path), "content": "SHOULD_NOT_EXIST", "mode": "create"}),
        ),
        ("mission-run", "functional-missing-mission", "--max-steps", "1"),
    )

    environment = os.environ.copy()
    environment.update(
        {
            "JARVIS_HOME": str(args.home.resolve()),
            "JARVIS_MODEL_ROOT": str(args.model_root.resolve()),
            "JARVIS_PROFILE": args.profile,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    rows = []
    for command_args in commands:
        started = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, str(args.repo / "jarvis.py"), *command_args],
            cwd=args.repo,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        combined = f"{completed.stdout}\n{completed.stderr}"
        rows.append(
            {
                "arguments": command_args,
                "returncode": completed.returncode,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "guard_message": "Jarvis API currently owns executive state" in combined,
            }
        )

    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=15, trust_env=False) as client:
        persona_after = client.get("/api/persona").json()
        approvals_after = client.get("/api/approvals").json()
        policy_after = client.get("/api/autonomy/policy").json()

    result = {
        "schema": "jarvis.functional-guarded-cli-matrix.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "home": str(args.home.resolve()),
        "health": health,
        "rows": rows,
        "before_after": {
            "persona_equal": persona_before == persona_after,
            "approvals_equal": approvals_before == approvals_after,
            "policy_equal": policy_before == policy_after,
            "probe_path_exists": probe_path.exists(),
        },
    }
    result["summary"] = {
        "commands": len(rows),
        "nonzero": sum(row["returncode"] != 0 for row in rows),
        "guarded": sum(row["guard_message"] for row in rows),
        "state_unchanged": all(
            (
                result["before_after"]["persona_equal"],
                result["before_after"]["approvals_equal"],
                result["before_after"]["policy_equal"],
                not result["before_after"]["probe_path_exists"],
            )
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0 if result["summary"]["nonzero"] == len(rows) and result["summary"]["state_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
