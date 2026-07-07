from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .config import JarvisSettings
from .llm import LLMRouter
from .model_catalog import ModelCatalog
from .models import DiagnosticCheck, DiagnosticsResponse
from .storage import JarvisStorage


def _check_path(name: str, path: Path, *, must_exist: bool = True) -> DiagnosticCheck:
    if path.exists():
        return DiagnosticCheck(
            name=name,
            status="ok",
            message=f"{path} is available",
            details={"path": str(path), "is_dir": path.is_dir()},
        )
    return DiagnosticCheck(
        name=name,
        status="error" if must_exist else "warn",
        message=f"{path} not found",
        details={"path": str(path)},
    )


def _command_version(name: str, command: list[str]) -> DiagnosticCheck:
    executable = shutil.which(command[0])
    if executable is None:
        return DiagnosticCheck(
            name=name,
            status="warn",
            message=f"{command[0]} not found in PATH",
        )
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except Exception as exc:  # noqa: BLE001
        return DiagnosticCheck(
            name=name,
            status="warn",
            message=str(exc),
            details={"path": executable},
        )
    output = (result.stdout or result.stderr).strip().splitlines()
    return DiagnosticCheck(
        name=name,
        status="ok" if result.returncode == 0 else "warn",
        message=output[0] if output else f"{command[0]} найден",
        details={"path": executable, "returncode": result.returncode},
    )


async def run_diagnostics(
    *,
    settings: JarvisSettings,
    storage: JarvisStorage,
    llm: LLMRouter,
    persist: bool = True,
) -> DiagnosticsResponse:
    catalog = ModelCatalog(settings).response()
    active_model = catalog["active_model"]
    checks: list[DiagnosticCheck] = [
        DiagnosticCheck(
            name="python",
            status="ok",
            message=sys.version.split()[0],
            details={"platform": platform.platform()},
        ),
        _check_path("runtime.home", settings.home),
        _check_path("runtime.data", settings.data_dir),
        _check_path("runtime.cache", settings.cache_dir),
        _check_path("runtime.logs", settings.log_dir),
        _check_path("models.root", settings.model_root, must_exist=False),
        DiagnosticCheck(
            name="models.profile",
            status="ok" if active_model["exists"] else "warn",
            message=f"{settings.profile.name} -> {settings.model_dir}",
            details={
                "active_model": active_model,
                "model_count": len(catalog["models"]),
                "dispatcher": catalog["dispatcher"],
            },
        ),
        _command_version("git", ["git", "--version"]),
        _command_version("docker", ["docker", "--version"]),
    ]

    try:
        storage.ping()
        checks.append(
            DiagnosticCheck(
                name="storage.sqlite",
                status="ok",
                message="SQLite storage is responding",
                details={"path": str(settings.database_path)},
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(DiagnosticCheck(name="storage.sqlite", status="error", message=str(exc)))

    llm_health = await llm.health()
    checks.append(
        DiagnosticCheck(
            name="llm.router",
            status="ok" if llm_health.get("ok") else "warn",
            message="LLM endpoint is responding"
            if llm_health.get("ok")
            else "LLM endpoint is unavailable",
            details=llm_health,
        )
    )

    if persist:
        for check in checks:
            storage.record_health(
                component=check.name,
                status=check.status,
                message=check.message,
                details=check.details,
            )

    ok = all(check.status != "error" for check in checks)
    return DiagnosticsResponse(ok=ok, checks=checks)
