from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="JARVIS GPT local readiness smoke check")
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument("--skip-http", action="store_true")
    args = parser.parse_args()

    checks = [
        run("backend tests", [sys.executable, "-m", "pytest"]),
        run("backend lint", [sys.executable, "-m", "ruff", "check", "backend/src", "backend/tests"]),
        run("backend compile", [sys.executable, "-m", "compileall", "backend/src", "backend/tests"]),
        run(
            "docker compose config",
            ["docker", "compose", "--profile", "llm", "config"],
            optional=True,
        ),
    ]
    if not args.skip_frontend:
        checks.extend(
            [
                run(
                    "frontend audit",
                    [executable("npm"), "audit", "--audit-level=moderate"],
                    cwd=ROOT / "frontend",
                ),
                run(
                    "frontend typecheck",
                    [executable("npm"), "run", "typecheck"],
                    cwd=ROOT / "frontend",
                ),
                run(
                    "frontend build",
                    [executable("npm"), "run", "build"],
                    cwd=ROOT / "frontend",
                ),
            ]
        )
    if not args.skip_http:
        checks.extend(
            [
                http("backend health", "http://localhost:8000/health", optional=True),
                http("backend autonomy", "http://localhost:8000/api/autonomy", optional=True),
                http("frontend", "http://localhost:3000", optional=True),
            ]
        )

    print(json.dumps({"ok": all(item["ok"] for item in checks), "checks": checks}, indent=2))
    return 0 if all(item["ok"] for item in checks) else 1


def run(
    name: str,
    command: list[str],
    *,
    cwd: Path = ROOT,
    optional: bool = False,
) -> dict[str, object]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except FileNotFoundError as exc:
        return {"name": name, "ok": optional, "optional": optional, "error": str(exc)}
    return {
        "name": name,
        "ok": result.returncode == 0 or optional,
        "optional": optional,
        "returncode": result.returncode,
        "stdout_tail": tail(result.stdout),
        "stderr_tail": tail(result.stderr),
    }


def http(name: str, url: str, *, optional: bool = False) -> dict[str, object]:
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            return {
                "name": name,
                "ok": 200 <= response.status < 400,
                "optional": optional,
                "status": response.status,
            }
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"name": name, "ok": optional, "optional": optional, "error": str(exc)}


def tail(text: str, limit: int = 800) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def executable(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    if sys.platform == "win32":
        found = shutil.which(f"{name}.cmd")
        if found:
            return found
    return name


if __name__ == "__main__":
    raise SystemExit(main())
