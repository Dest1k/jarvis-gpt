from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_BACKEND_SRC = ROOT / "backend" / "src"
if str(_BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(_BACKEND_SRC))

from jarvis_gpt.redaction import redact_text, redact_value  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis local readiness smoke check")
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument("--skip-http", action="store_true")
    args = parser.parse_args()

    checks = [
        run("backend tests", [sys.executable, "-m", "pytest"]),
        run(
            "backend lint",
            [sys.executable, "-m", "ruff", "check", "backend/src", "backend/tests"],
        ),
        run(
            "backend compile",
            [sys.executable, "-m", "compileall", "backend/src", "backend/tests"],
        ),
        run(
            "docker compose config",
            ["docker", "compose", "--profile", "llm", "config"],
            optional=True,
            env={
                **os.environ,
                "JARVIS_QWEN_MODEL_PATH": "/models/__jarvis_compose_config_check__",
            },
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

    required_ok = all(bool(item["ok"]) for item in checks if not item["optional"])
    optional_gaps = sum(
        1 for item in checks if item["optional"] and not bool(item["ok"])
    )
    report = {
        "ok": required_ok,
        "degraded": optional_gaps > 0,
        "summary": {
            "passed": sum(1 for item in checks if item["status"] == "passed"),
            "failed": sum(1 for item in checks if item["status"] == "failed"),
            "skipped": sum(1 for item in checks if item["status"] == "skipped"),
            "optional_gaps": optional_gaps,
        },
        "checks": checks,
    }
    print(json.dumps(redact_value(report), indent=2))
    return 0 if required_ok else 1


def run(
    name: str,
    command: list[str],
    *,
    cwd: Path = ROOT,
    optional: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        return _failed_check(name, optional=optional, error=str(exc), unavailable=True)
    except subprocess.TimeoutExpired as exc:
        return _failed_check(
            name,
            optional=optional,
            error=f"timed out after {exc.timeout}s",
        )
    ok = result.returncode == 0
    return {
        "name": name,
        "ok": ok,
        "optional": optional,
        "status": "passed" if ok else "failed",
        "returncode": result.returncode,
        "stdout_tail": safe_tail(result.stdout),
        "stderr_tail": safe_tail(result.stderr),
    }


def http(name: str, url: str, *, optional: bool = False) -> dict[str, object]:
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            return {
                "name": name,
                "ok": 200 <= response.status < 400,
                "optional": optional,
                "status": "passed" if 200 <= response.status < 400 else "failed",
                "http_status": response.status,
            }
    except urllib.error.HTTPError as exc:
        return {
            **_failed_check(name, optional=optional, error=str(exc)),
            "http_status": exc.code,
        }
    except (urllib.error.URLError, TimeoutError) as exc:
        return _failed_check(name, optional=optional, error=str(exc), unavailable=True)


def _failed_check(
    name: str,
    *,
    optional: bool,
    error: str,
    unavailable: bool = False,
) -> dict[str, object]:
    return {
        "name": name,
        "ok": False,
        "optional": optional,
        "status": "skipped" if optional and unavailable else "failed",
        "error": redact_text(error),
    }


def tail(text: str, limit: int = 800) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def safe_tail(text: str, limit: int = 800) -> str:
    """Return a length-limited command tail with secrets redacted."""
    return tail(redact_text(text), limit=limit)


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
