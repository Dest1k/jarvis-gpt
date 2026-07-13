#!/usr/bin/env python3
"""Bounded live technical probes for the isolated functional namespace.

The script never writes outside the supplied evidence/temp directories and the
runtime's own backup endpoint. Persistence fault probes run only on a generated
copy of that backup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


READ_ONLY_CLI = (
    ("profiles",),
    ("status",),
    ("models",),
    ("models", "--env"),
    ("diag",),
    ("tools",),
    ("llm-health",),
    ("dispatcher-status",),
    ("dispatcher-compose",),
    ("dispatcher-compose", "--env"),
    ("telemetry",),
    ("autonomy",),
    ("persona",),
    ("files", "--limit", "5"),
    ("audit", "--limit", "5"),
    ("approvals", "--limit", "5"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request_json(base_url: str, method: str, route: str) -> dict[str, Any]:
    opener = build_opener(ProxyHandler({}))
    request = Request(
        f"{base_url.rstrip('/')}{route}",
        method=method,
        headers={"Accept": "application/json"},
        data=b"" if method == "POST" else None,
    )
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read()
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "body": json.loads(body.decode("utf-8")),
            }
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return {
            "ok": False,
            "status": exc.code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": body[:1000],
        }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "status": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }


def summarize_api_status(payload: dict[str, Any]) -> dict[str, Any]:
    body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
    settings = body.get("settings") if isinstance(body.get("settings"), dict) else {}
    profile = settings.get("profile") if isinstance(settings.get("profile"), dict) else {}
    paths = settings.get("paths") if isinstance(settings.get("paths"), dict) else {}
    health = body.get("health") if isinstance(body.get("health"), list) else []
    return {
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "elapsed_ms": payload.get("elapsed_ms"),
        "home": settings.get("home"),
        "profile": profile.get("name"),
        "max_model_len": profile.get("max_model_len"),
        "active_model": paths.get("active_model"),
        "counters": body.get("counters"),
        "health": [
            {
                "name": entry.get("name"),
                "status": entry.get("status"),
                "message": entry.get("message"),
            }
            for entry in health
            if isinstance(entry, dict)
        ],
    }


def run_cli_matrix(repo: Path, home: Path, model_root: Path, profile: str) -> list[dict[str, Any]]:
    environment = os.environ.copy()
    environment.update(
        {
            "JARVIS_HOME": str(home),
            "JARVIS_MODEL_ROOT": str(model_root),
            "JARVIS_PROFILE": profile,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    results: list[dict[str, Any]] = []
    for arguments in READ_ONLY_CLI:
        command = [sys.executable, str(repo / "jarvis.py"), *arguments]
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                cwd=repo,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                check=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            results.append(
                {
                    "command": arguments,
                    "returncode": completed.returncode,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
                    "stdout_preview": stdout[:2000],
                    "stderr_preview": stderr[:1000],
                    "status": "PASS" if completed.returncode == 0 else "FAIL",
                }
            )
        except subprocess.TimeoutExpired as exc:
            results.append(
                {
                    "command": arguments,
                    "returncode": None,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                    "stdout_preview": (exc.stdout or "")[:2000],
                    "stderr_preview": (exc.stderr or "")[:1000],
                    "status": "FAIL",
                    "error": "timeout after 45s",
                }
            )
    return results


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    counts: dict[str, int] = {}
    for name in names:
        escaped = name.replace('"', '""')
        counts[name] = int(connection.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0])
    return counts


def probe_backup_copy(backup: Path, temp_dir: Path) -> dict[str, Any]:
    temp_dir.mkdir(parents=True, exist_ok=True)
    restore_copy = temp_dir / "restored-copy.sqlite3"
    shutil.copy2(backup, restore_copy)

    with sqlite3.connect(f"file:{backup.as_posix()}?mode=ro", uri=True) as source:
        integrity = source.execute("PRAGMA integrity_check").fetchone()[0]
        source_counts = table_counts(source)
        user_version = int(source.execute("PRAGMA user_version").fetchone()[0])

    with sqlite3.connect(restore_copy) as restored:
        restored_integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
        restored_counts = table_counts(restored)

    lock_error = None
    first = sqlite3.connect(restore_copy, timeout=1)
    second = sqlite3.connect(restore_copy, timeout=0.1)
    try:
        first.execute("BEGIN EXCLUSIVE")
        try:
            second.execute("CREATE TABLE functional_lock_probe(value TEXT)")
            second.commit()
        except sqlite3.OperationalError as exc:
            lock_error = str(exc)
            second.rollback()
        finally:
            first.rollback()
    finally:
        second.close()
        first.close()

    readonly_error = None
    readonly = sqlite3.connect(f"file:{restore_copy.as_posix()}?mode=ro", uri=True)
    try:
        try:
            readonly.execute("CREATE TABLE functional_readonly_probe(value TEXT)")
            readonly.commit()
        except sqlite3.OperationalError as exc:
            readonly_error = str(exc)
            readonly.rollback()
    finally:
        readonly.close()

    temp_error = None
    with sqlite3.connect(restore_copy) as temp_probe:
        try:
            temp_probe.execute("PRAGMA temp_store=FILE")
            temp_probe.execute("PRAGMA temp_store_directory='Z:/functional-path-does-not-exist'")
            temp_probe.execute("CREATE TEMP TABLE functional_temp_probe(value TEXT)")
        except sqlite3.OperationalError as exc:
            temp_error = str(exc)

    final_hash = sha256(restore_copy)
    final_integrity = sqlite3.connect(restore_copy).execute("PRAGMA integrity_check").fetchone()[0]
    return {
        "backup_path": str(backup),
        "backup_sha256": sha256(backup),
        "backup_size": backup.stat().st_size,
        "backup_integrity": integrity,
        "user_version": user_version,
        "restored_copy": str(restore_copy),
        "restored_sha256": final_hash,
        "restored_integrity": restored_integrity,
        "final_integrity": final_integrity,
        "table_counts_equal": source_counts == restored_counts,
        "table_count": len(source_counts),
        "row_count": sum(source_counts.values()),
        "lock_error": lock_error,
        "lock_probe_pass": bool(lock_error and "locked" in lock_error.lower()),
        "readonly_error": readonly_error,
        "readonly_probe_pass": bool(readonly_error and "readonly" in readonly_error.lower()),
        "temp_error": temp_error,
        "temp_probe_pass": temp_error is not None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--temp-dir", type=Path, required=True)
    args = parser.parse_args()

    health = request_json(args.base_url, "GET", "/health")
    status_raw = request_json(args.base_url, "GET", "/api/status")
    models = request_json(args.base_url, "GET", "/api/models")
    backup = request_json(args.base_url, "POST", "/api/runtime/backup")

    backup_copy: dict[str, Any] | None = None
    if backup.get("ok") and isinstance(backup.get("body"), dict):
        raw_path = backup["body"].get("path")
        if raw_path:
            backup_path = Path(raw_path).resolve()
            home = args.home.resolve()
            if home == backup_path or home in backup_path.parents:
                backup_copy = probe_backup_copy(backup_path, args.temp_dir)
            else:
                backup_copy = {
                    "status": "FAIL",
                    "error": "backup escaped isolated home",
                    "backup_path": str(backup_path),
                }

    result = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": str(args.repo.resolve()),
        "home": str(args.home.resolve()),
        "profile": args.profile,
        "api": {
            "health": health,
            "status": summarize_api_status(status_raw),
            "models": models,
            "backup": backup,
        },
        "persistence_copy": backup_copy,
        "cli_matrix": run_cli_matrix(
            args.repo.resolve(), args.home.resolve(), args.model_root.resolve(), args.profile
        ),
    }
    result["summary"] = {
        "api_pass": all(result["api"][name].get("ok") for name in ("health", "status", "models", "backup")),
        "cli_pass": sum(item["status"] == "PASS" for item in result["cli_matrix"]),
        "cli_total": len(result["cli_matrix"]),
        "persistence_pass": bool(
            backup_copy
            and backup_copy.get("backup_integrity") == "ok"
            and backup_copy.get("final_integrity") == "ok"
            and backup_copy.get("table_counts_equal")
            and backup_copy.get("lock_probe_pass")
            and backup_copy.get("readonly_probe_pass")
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0 if result["summary"]["api_pass"] and result["summary"]["persistence_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
