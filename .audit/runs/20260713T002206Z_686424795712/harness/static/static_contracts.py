#!/usr/bin/env python3
"""Repository-only schema/config/API contract checks."""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path

import yaml


RUN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUN_ROOT.parents[2]
OUT = RUN_ROOT / "evidence" / "static"
MACHINE = RUN_ROOT / "machine"
sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    MACHINE.mkdir(parents=True, exist_ok=True)

    parsed_json: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*.json")):
        if ".git" in path.parts or ".audit" in path.parts:
            continue
        json.loads(path.read_text(encoding="utf-8"))
        parsed_json.append(str(path.relative_to(REPO_ROOT)))
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {"dispatcher", "backend", "frontend"}

    env_keys: list[str] = []
    for raw in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            assert "=" in line, f"malformed .env.example line: {raw!r}"
            env_keys.append(line.split("=", 1)[0])
    assert len(env_keys) == len(set(env_keys)), "duplicate keys in .env.example"

    from jarvis_gpt.api import app
    from jarvis_gpt.config import PROFILES, load_settings

    openapi = app.openapi()
    (OUT / "openapi.json").write_text(
        json.dumps(openapi, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    route_rows: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = sorted(getattr(route, "methods", None) or [])
        path = getattr(route, "path", "")
        for method in methods:
            key = (method, path)
            assert key not in seen, f"duplicate route: {method} {path}"
            seen.add(key)
        route_rows.append(
            {
                "path": path,
                "methods": methods,
                "name": getattr(route, "name", None),
                "include_in_schema": getattr(route, "include_in_schema", None),
            }
        )
    (MACHINE / "api_routes.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in route_rows),
        encoding="utf-8",
    )

    profile_names = set(PROFILES)
    launcher = (REPO_ROOT / "scripts" / "jarvis-launcher.ps1").read_text(encoding="utf-8")
    validate_sets = [
        set(re.findall(r'"([^"]+)"', match.group(1)))
        for match in re.finditer(r'\[ValidateSet\(([^\]]+)\)\]', launcher)
    ]
    assert profile_names in validate_sets
    for name, profile in PROFILES.items():
        settings = load_settings(name)
        assert settings.profile is profile
        assert settings.model_dir.name == profile.model_dir_name

    observations = {
        "json_files_parsed": parsed_json,
        "project_name": project["project"]["name"],
        "compose_services": sorted(compose["services"]),
        "env_keys": env_keys,
        "profiles": sorted(profile_names),
        "openapi_paths": len(openapi.get("paths", {})),
        "app_routes": len(route_rows),
        "compose_frontend_requires_backend_healthy": (
            compose["services"]["frontend"].get("depends_on", {}).get("backend", {}).get("condition")
            == "service_healthy"
        ),
        "compose_backend_has_healthcheck": "healthcheck" in compose["services"]["backend"],
        "dev_extra_has_unused_httpx2": "httpx2==2.5.0"
        in project["project"]["optional-dependencies"]["dev"],
    }
    secret_patterns = {
        "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "github_token": re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    }
    secret_hits: list[dict[str, str]] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or ".git" in path.parts or ".audit" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in secret_patterns.items():
            if pattern.search(text):
                secret_hits.append({"path": str(path.relative_to(REPO_ROOT)), "pattern": label})
    observations["tracked_secret_signature_hits"] = secret_hits
    assert not secret_hits
    (OUT / "static_contract_observations.json").write_text(
        json.dumps(observations, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(observations, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    os.environ.setdefault("JARVIS_HOME", "/tmp/jarvis-phase-a-contracts")
    os.environ.setdefault("JARVIS_LLM_ENABLED", "0")
    main()
