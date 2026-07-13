#!/usr/bin/env python3
"""Build a deterministic inventory of the pre-audit source tree."""

from __future__ import annotations

import ast
import csv
import json
import re
import subprocess
from collections import Counter
from pathlib import Path


RUN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUN_ROOT.parents[2]
OUT = RUN_ROOT / "evidence" / "static"
SOURCE_COMMIT = "686424795712cb0a562750b6dade13de18c48792"


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", SOURCE_COMMIT],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return [line for line in result.stdout.splitlines() if line]


def classification(path: str) -> str:
    name = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    if "/tests/" in path or name.startswith("test_"):
        return "test"
    if path.startswith("docs/") or suffix == ".md":
        return "documentation"
    if path.startswith("scripts/") or suffix in {".cmd", ".ps1"} or path == "jarvis.py":
        return "script"
    if name in {"next-env.d.ts"}:
        return "generated"
    if suffix in {".lock"} or name == "package-lock.json":
        return "generated"
    if suffix in {".json", ".toml", ".yml", ".yaml"} or name in {
        ".env.example",
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        "dockerfile",
        ".dockerignore",
    }:
        return "config"
    if re.search(r"(^|/)(models|execution_models)\.py$", path):
        return "schema"
    if path.startswith("backend/src/") or path.startswith("frontend/"):
        return "production"
    if suffix in {".txt"}:
        return "config"
    return "unknown"


def subsystem(path: str) -> str:
    if path.startswith("frontend/"):
        return "frontend"
    if path.startswith("backend/tests/"):
        return "backend-tests"
    if path.startswith("backend/src/jarvis_gpt/"):
        stem = Path(path).stem
        groups = {
            "api": "backend-api",
            "main": "backend-api",
            "cli": "cli",
            "config": "configuration",
            "model_catalog": "profiles-models",
            "model_hub": "profiles-models",
            "dispatcher": "profiles-models",
            "llm": "llm-agent",
            "agent": "llm-agent",
            "executive_planner": "execution",
            "executive_runtime": "execution",
            "approval_executor": "execution",
            "execution_actions": "execution",
            "execution_config": "execution",
            "execution_filesystem": "execution",
            "execution_kernel": "execution",
            "execution_models": "execution",
            "execution_process": "execution",
            "execution_protocol": "execution",
            "execution_replay": "execution",
            "execution_session": "execution",
            "execution_transaction": "execution",
            "state_verification": "execution",
            "verification": "execution",
            "tools": "tools-host-web",
            "operations": "tools-host-web",
            "host_bridge": "tools-host-web",
            "browser_cdp": "tools-host-web",
            "web_orchestrator": "web-document",
            "web_surfer": "web-document",
            "web_surfer_adapter": "web-document",
            "web_surfer_worker": "web-document",
            "document_agent": "web-document",
            "document_memory": "web-document",
            "document_runtime": "web-document",
            "document_surfer": "web-document",
            "archive_runtime": "web-document",
            "file_types": "web-document",
            "ingest": "web-document",
            "shop_registry": "web-document",
            "storage": "state-memory",
            "memory_vault": "state-memory",
            "cognitive_memory": "state-memory",
            "persona": "state-memory",
            "learning": "state-memory",
            "experience": "state-memory",
            "autonomy_executor": "autonomy-jobs",
            "operator_queue": "autonomy-jobs",
            "supervisor": "autonomy-jobs",
            "telemetry": "autonomy-jobs",
            "runtime_lease": "autonomy-jobs",
            "event_bus": "backend-api",
            "redaction": "defensive-design",
        }
        return groups.get(stem, "backend-core")
    if path.startswith("scripts/") or path.endswith(".cmd") or path == "jarvis.py":
        return "launcher-runtime"
    if path.startswith("docs/") or path == "README.md":
        return "documentation"
    if "docker" in path.lower() or path == "docker-compose.yml":
        return "deployment"
    if path.startswith(".github/"):
        return "quality-gates"
    return "repository"


def public_symbols(path: str, text: str) -> list[str]:
    if not path.endswith(".py"):
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ["<syntax-error>"]
    result: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                result.append(node.name)
    return result


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    paths = tracked_files()
    test_corpus = "\n".join(
        (REPO_ROOT / p).read_text(encoding="utf-8", errors="replace")
        for p in paths
        if "/tests/" in p and p.endswith(".py")
    )
    rows: list[dict[str, object]] = []
    for path in paths:
        full = REPO_ROOT / path
        data = full.read_bytes()
        text = data.decode("utf-8", errors="replace")
        symbols = public_symbols(path, text)
        deps = []
        if path.endswith(".py"):
            deps = sorted(set(re.findall(r"^(?:from|import)\s+([\w.]+)", text, re.M)))
        side_effects = sorted(
            label
            for label, pattern in {
                "persistence": r"sqlite|write_text|write_bytes|open\(|unlink|replace\(",
                "network": r"httpx|urlopen|websocket|socket\.",
                "process": r"subprocess|Popen|create_subprocess|docker|powershell|wsl",
            }.items()
            if re.search(pattern, text, re.I)
        )
        stem = Path(path).stem
        test_refs = len(re.findall(rf"\b{re.escape(stem)}\b", test_corpus))
        rows.append(
            {
                "path": path,
                "classification": classification(path),
                "subsystem": subsystem(path),
                "size_bytes": len(data),
                "line_count": text.count("\n") + (1 if text else 0),
                "public_entry_points": ";".join(symbols),
                "outgoing_dependencies": ";".join(deps[:24]),
                "side_effect_categories": ";".join(side_effects),
                "test_reference_count": test_refs,
                "indexed": "true",
                "review_status": "subsystem-review",
                "phase_b_required": "true"
                if classification(path) == "production" and side_effects
                else "false",
            }
        )
    csv_path = OUT / "tracked_file_inventory.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "source_commit": SOURCE_COMMIT,
        "tracked_files": len(rows),
        "total_bytes": sum(int(row["size_bytes"]) for row in rows),
        "total_lines": sum(int(row["line_count"]) for row in rows),
        "by_classification": dict(sorted(Counter(str(r["classification"]) for r in rows).items())),
        "by_subsystem": dict(sorted(Counter(str(r["subsystem"]) for r in rows).items())),
    }
    (OUT / "tracked_file_inventory_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
