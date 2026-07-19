from __future__ import annotations

import argparse
import hashlib
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

# Per-check defaults. Full backend suite on a healthy host is ~230–300s, so the
# doctor test timeout must exceed that wall-clock duration.
DEFAULT_CHECK_TIMEOUT_SECONDS = 180
DEFAULT_DOCTOR_TEST_TIMEOUT_SECONDS = 600
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 3600
DOCTOR_TEST_TIMEOUT_ENV = "JARVIS_DOCTOR_TEST_TIMEOUT_SECONDS"
DOCTOR_CHECK_TIMEOUT_ENV = "JARVIS_DOCTOR_CHECK_TIMEOUT_SECONDS"


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis local readiness smoke check")
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument("--skip-http", action="store_true")
    parser.add_argument(
        "--require-runtime",
        action="store_true",
        help="Fail when the running backend, autonomy API, or frontend is unavailable.",
    )
    args = parser.parse_args()

    try:
        backend_test_timeout = resolve_timeout_seconds(
            DOCTOR_TEST_TIMEOUT_ENV,
            default=DEFAULT_DOCTOR_TEST_TIMEOUT_SECONDS,
        )
        default_check_timeout = resolve_timeout_seconds(
            DOCTOR_CHECK_TIMEOUT_ENV,
            default=DEFAULT_CHECK_TIMEOUT_SECONDS,
        )
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 2

    checks = [
        run(
            "backend tests",
            [sys.executable, "-m", "pytest"],
            env=sanitized_test_env(),
            timeout=backend_test_timeout,
        ),
        run(
            "assurance tests",
            [sys.executable, "-m", "pytest", "qa/tests"],
            env=sanitized_test_env(),
            timeout=backend_test_timeout,
        ),
        run(
            "backend lint",
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "backend/src",
                "backend/tests",
                "qa",
            ],
            timeout=default_check_timeout,
        ),
        run(
            "backend compile",
            [sys.executable, "-m", "compileall", "backend/src", "backend/tests", "qa"],
            timeout=default_check_timeout,
        ),
        run(
            "docker compose config",
            ["docker", "compose", "--profile", "llm", "config"],
            optional=True,
            env={
                **os.environ,
                "JARVIS_QWEN_MODEL_PATH": "/models/__jarvis_compose_config_check__",
                "JARVIS_API_TOKEN": "jarvis-smoke-compose-token-32-characters",
            },
            timeout=default_check_timeout,
        ),
    ]
    if not args.skip_frontend:
        checks.extend(
            [
                run(
                    "frontend audit",
                    [executable("npm"), "audit", "--audit-level=moderate"],
                    cwd=ROOT / "frontend",
                    timeout=default_check_timeout,
                ),
                run(
                    "frontend typecheck",
                    [executable("npm"), "run", "typecheck"],
                    cwd=ROOT / "frontend",
                    timeout=default_check_timeout,
                ),
                run(
                    "frontend runtime identity tests",
                    [executable("npm"), "run", "test:runtime-identity"],
                    cwd=ROOT / "frontend",
                    timeout=default_check_timeout,
                ),
                run(
                    "frontend memory graph tests",
                    [executable("npm"), "run", "test:memory-graph"],
                    cwd=ROOT / "frontend",
                    timeout=default_check_timeout,
                ),
                run(
                    "frontend stream recovery tests",
                    [executable("npm"), "run", "test:stream-placeholder"],
                    cwd=ROOT / "frontend",
                    timeout=default_check_timeout,
                ),
                run_frontend_build(
                    timeout=default_check_timeout,
                ),
            ]
        )
    if not args.skip_http:
        runtime_optional = not args.require_runtime
        runtime_checks = [
            http(
                "backend health",
                "http://localhost:8000/health",
                optional=runtime_optional,
                expect_json_ok=True,
            ),
            http(
                "backend autonomy",
                "http://localhost:8000/api/autonomy",
                optional=runtime_optional,
                headers=api_auth_headers(),
            ),
        ]
        if not args.skip_frontend:
            runtime_checks.append(
                http("frontend", "http://localhost:3000", optional=runtime_optional)
            )
        checks.extend(runtime_checks)

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
        "timeouts": {
            "backend_tests_seconds": backend_test_timeout,
            "default_check_seconds": default_check_timeout,
        },
        "checks": checks,
    }
    print(json.dumps(redact_value(report), indent=2))
    return 0 if required_ok else 1


def resolve_timeout_seconds(env_name: str, *, default: int) -> int:
    """Parse a positive timeout override with explicit bounds and errors.

    Invalid, zero, negative, or out-of-range values raise ValueError so doctor
    fails closed with a clear message instead of silently using a random default.
    """

    raw = os.environ.get(env_name)
    if raw is None or str(raw).strip() == "":
        return int(default)
    text = str(raw).strip()
    try:
        value = int(text, 10)
    except ValueError as exc:
        raise ValueError(
            f"{env_name} must be an integer number of seconds "
            f"(got {raw!r}); allowed range "
            f"{MIN_TIMEOUT_SECONDS}..{MAX_TIMEOUT_SECONDS}"
        ) from exc
    if value < MIN_TIMEOUT_SECONDS or value > MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"{env_name}={value} is out of range; allowed "
            f"{MIN_TIMEOUT_SECONDS}..{MAX_TIMEOUT_SECONDS} seconds"
        )
    return value


def run(
    name: str,
    command: list[str],
    *,
    cwd: Path = ROOT,
    optional: bool = False,
    env: dict[str, str] | None = None,
    timeout: int | float | None = None,
) -> dict[str, object]:
    effective_timeout = (
        DEFAULT_CHECK_TIMEOUT_SECONDS if timeout is None else float(timeout)
    )
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        return _failed_check(name, optional=optional, error=str(exc), unavailable=True)
    except subprocess.TimeoutExpired as exc:
        # Timeout remains a required failure (nonzero) for non-optional checks.
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
        "timeout_seconds": effective_timeout,
    }


def run_frontend_build(*, timeout: int | float) -> dict[str, object]:
    """Run frontend production build with resource-safe reuse on OOM.

    When a turbo/vLLM stack already holds host memory, ``npm run build`` can abort
    with a memory-allocation failure and corrupt an in-progress ``.next`` tree.
    If an unchanged, source-current production build already existed before the
    attempt, it may be reused after an OOM. Real compile errors and any partial
    mutation of the build tree still fail.
    """

    frontend = ROOT / "frontend"
    existing_before = existing_production_frontend_build(frontend)
    tree_before = (
        production_build_tree_fingerprint(frontend / ".next")
        if existing_before is not None
        else None
    )
    result = run(
        "frontend build",
        [executable("npm"), "run", "build"],
        cwd=frontend,
        timeout=timeout,
    )
    if result.get("ok"):
        return result

    stderr = str(result.get("stderr_tail") or "")
    stdout = str(result.get("stdout_tail") or "")
    combined = f"{stdout}\n{stderr}".casefold()
    oom_markers = (
        "memory allocation",
        "cannot allocate memory",
        "javascript heap out of memory",
        "enomem",
        "out of memory",
        "fatal process out of memory",
    )
    if not any(marker in combined for marker in oom_markers):
        return result

    existing = existing_production_frontend_build(frontend)
    tree_after = production_build_tree_fingerprint(frontend / ".next")
    if (
        existing_before is None
        or existing is None
        or existing != existing_before
        or tree_before is None
        or tree_after != tree_before
    ):
        return result

    return {
        "name": "frontend build",
        "ok": True,
        "optional": False,
        "status": "passed",
        "returncode": 0,
        "stdout_tail": safe_tail(
            "Reused proven production frontend build after OOM during rebuild. "
            f"BUILD_ID={existing['build_id']}"
        ),
        "stderr_tail": safe_tail(stderr),
        "timeout_seconds": float(timeout),
        "reused_production_build": True,
        "build_id": existing["build_id"],
    }


def existing_production_frontend_build(frontend_dir: Path) -> dict[str, str] | None:
    """Return metadata for a proven Next.js production build, or None."""

    next_dir = frontend_dir / ".next"
    build_id_path = next_dir / "BUILD_ID"
    if not build_id_path.is_file():
        return None
    build_id = build_id_path.read_text(encoding="utf-8", errors="replace").strip()
    if not build_id:
        return None
    # Minimal structural proof that the production export is usable.
    required = [
        next_dir / "BUILD_ID",
        next_dir / "prerender-manifest.json",
        next_dir / "build-manifest.json",
    ]
    if not all(path.is_file() for path in required):
        return None
    source_files = [
        frontend_dir / "package.json",
        frontend_dir / "package-lock.json",
        frontend_dir / "tsconfig.json",
        frontend_dir / "next-env.d.ts",
    ]
    source_files.extend(
        path for path in frontend_dir.glob("next.config.*") if path.is_file()
    )
    for source_dir_name in ("app", "pages", "src", "components", "lib", "public"):
        source_dir = frontend_dir / source_dir_name
        if source_dir.is_dir():
            source_files.extend(
                path for path in source_dir.rglob("*") if path.is_file()
            )
    newest_source = max(
        (path.stat().st_mtime_ns for path in source_files if path.is_file()),
        default=0,
    )
    if build_id_path.stat().st_mtime_ns < newest_source:
        return None
    return {"build_id": build_id, "path": str(next_dir)}


def production_build_tree_fingerprint(next_dir: Path) -> str | None:
    """Fingerprint build-tree structure without reading large bundle contents."""

    if not next_dir.is_dir():
        return None
    digest = hashlib.sha256()
    files = sorted(path for path in next_dir.rglob("*") if path.is_file())
    if not files:
        return None
    for path in files:
        stat = path.stat()
        relative = path.relative_to(next_dir).as_posix()
        digest.update(relative.encode("utf-8", errors="strict"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def http(
    name: str,
    url: str,
    *,
    optional: bool = False,
    expect_json_ok: bool = False,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    try:
        request = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(request, timeout=8) as response:
            status_ok = 200 <= response.status < 400
            body_ok = True
            if expect_json_ok and status_ok:
                try:
                    body = json.loads(response.read(1_048_577).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body_ok = False
                else:
                    body_ok = isinstance(body, dict) and body.get("ok") is True
            ok = status_ok and body_ok
            return {
                "name": name,
                "ok": ok,
                "optional": optional,
                "status": "passed" if ok else "failed",
                "http_status": response.status,
                **({"body_ok": body_ok} if expect_json_ok else {}),
            }
    except urllib.error.HTTPError as exc:
        return {
            **_failed_check(name, optional=optional, error=str(exc)),
            "http_status": exc.code,
        }
    except (urllib.error.URLError, TimeoutError) as exc:
        return _failed_check(name, optional=optional, error=str(exc), unavailable=True)


def api_auth_headers() -> dict[str, str]:
    token = os.environ.get("JARVIS_API_TOKEN", "").strip()
    if not token:
        home = os.environ.get("JARVIS_HOME", "").strip()
        candidates = [Path(home) / ".jarvis" / "api.token"] if home else []
        candidates.append(Path.home() / ".jarvis" / "api.token")
        for path in candidates:
            try:
                token = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                continue
            if token:
                break
    return {"Authorization": f"Bearer {token}"} if token else {}


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


# Deployment identity injected by jarvis-launcher must not leak into pytest.
_TEST_ENV_BLOCKLIST = (
    "JARVIS_HOME",
    "JARVIS_MODEL_ROOT",
    "JARVIS_PROFILE",
)


def sanitized_test_env(
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return env for test subprocesses without deployment home/profile vars."""
    env = dict(os.environ if base is None else base)
    for key in _TEST_ENV_BLOCKLIST:
        env.pop(key, None)
    return env


if __name__ == "__main__":
    raise SystemExit(main())
