#!/usr/bin/env python3
"""Build the machine-readable feature and requirement catalog."""

from __future__ import annotations

import json
import re
from pathlib import Path


RUN_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = RUN_ROOT.parents[2]
MACHINE = RUN_ROOT / "machine"
EVIDENCE = RUN_ROOT / "evidence" / "static"


REQUIREMENTS = [
    ("REQ-API-001", "API schemas, auth and status codes match the public contract", "high"),
    ("REQ-STREAM-001", "Stream and WebSocket events preserve order, identity and terminal truth", "high"),
    ("REQ-CLI-001", "CLI commands validate input and report real outcomes without silent success", "high"),
    ("REQ-TOOL-001", "Tool arguments are schema-validated and results never claim false success", "high"),
    ("REQ-APPROVAL-001", "Approval is bound to the exact action, arguments and current state", "critical"),
    ("REQ-IDEMPOTENCY-001", "Retries and cancellation cannot duplicate irreversible effects", "critical"),
    ("REQ-STORAGE-001", "Persistent state transitions are atomic, isolated and recoverable", "critical"),
    ("REQ-PROFILE-001", "Each profile resolves only its intended model and dispatcher identity", "high"),
    ("REQ-OFFLINE-001", "Local core startup remains usable without unrequested network access", "high"),
    ("REQ-LAUNCHER-001", "Start/stop/restart are owned, scoped, idempotent and diagnostic", "high"),
    ("REQ-FRONTEND-001", "Frontend status reflects current backend truth and explicit degradation", "high"),
    ("REQ-TRUST-001", "Untrusted web/document input cannot gain control-plane privileges", "critical"),
    ("REQ-URL-001", "Every navigation hop is restricted to validated public HTTP(S) destinations", "critical"),
    ("REQ-FILES-001", "File operations stay in allowed roots and preserve originals unless approved", "critical"),
    ("REQ-ARCHIVE-001", "Archive limits fail atomically without partial final outputs", "high"),
    ("REQ-SECRET-001", "Secrets and sensitive arguments are minimized and redacted at every sink", "critical"),
    ("REQ-JOBS-001", "Background jobs use concurrency-safe leases, bounded history and truthful lifecycle", "high"),
    ("REQ-RESOURCE-001", "Queues, histories, tasks, processes and caches are bounded and cleaned", "high"),
    ("REQ-DEPENDENCY-001", "Dependencies and images are necessary, pinned and reproducible", "high"),
    ("REQ-TEST-001", "High-risk contracts have positive, error and recovery regression oracles", "high"),
    ("REQ-DOC-001", "Operator docs, defaults, implementation and UI expose one non-conflicting contract", "medium"),
    ("REQ-LEGACY-001", "Disabled or legacy paths are absent from active routing and UX", "medium"),
]


KIND_REQUIREMENT = {
    "api": ["REQ-API-001", "REQ-TOOL-001"],
    "websocket": ["REQ-STREAM-001", "REQ-API-001"],
    "cli": ["REQ-CLI-001", "REQ-LAUNCHER-001"],
    "tool": ["REQ-TOOL-001", "REQ-APPROVAL-001"],
    "profile": ["REQ-PROFILE-001", "REQ-OFFLINE-001"],
    "service": ["REQ-LAUNCHER-001", "REQ-OFFLINE-001"],
    "ui": ["REQ-FRONTEND-001", "REQ-DOC-001"],
    "subsystem": ["REQ-STORAGE-001", "REQ-RESOURCE-001"],
}


def emit(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def main() -> None:
    MACHINE.mkdir(parents=True, exist_ok=True)
    features: list[dict[str, object]] = []

    routes = [json.loads(line) for line in (MACHINE / "api_routes.jsonl").read_text().splitlines()]
    api_index = 1
    ws_index = 1
    for route in routes:
        path = str(route["path"])
        methods = [m for m in route["methods"] if m not in {"HEAD", "OPTIONS"}]
        if methods:
            for method in methods:
                kind = "api"
                features.append(
                    {
                        "id": f"FEAT-API-{api_index:03d}",
                        "name": f"{method} {path}",
                        "kind": kind,
                        "status": "STATIC_SUPPORTED",
                        "source_path": "backend/src/jarvis_gpt/api.py",
                        "symbol": route.get("name"),
                        "requirement_ids": KIND_REQUIREMENT[kind],
                        "phase_b_required": True,
                    }
                )
                api_index += 1
        elif path.startswith("/ws/"):
            kind = "websocket"
            features.append(
                {
                    "id": f"FEAT-WS-{ws_index:03d}",
                    "name": path,
                    "kind": kind,
                    "status": "STATIC_SUPPORTED",
                    "source_path": "backend/src/jarvis_gpt/api.py",
                    "symbol": route.get("name"),
                    "requirement_ids": KIND_REQUIREMENT[kind],
                    "phase_b_required": True,
                }
            )
            ws_index += 1

    cli_text = (REPO_ROOT / "backend/src/jarvis_gpt/cli.py").read_text(encoding="utf-8")
    for index, (name, help_text) in enumerate(
        re.findall(r'sub\.add_parser\(\s*"([^"]+)"(?:,\s*help="([^"]*)")?', cli_text), 1
    ):
        features.append(
            {
                "id": f"FEAT-CLI-{index:03d}",
                "name": name,
                "kind": "cli",
                "status": "STATIC_SUPPORTED",
                "source_path": "backend/src/jarvis_gpt/cli.py",
                "symbol": f"command:{name}",
                "description": help_text,
                "requirement_ids": KIND_REQUIREMENT["cli"],
                "phase_b_required": name not in {"profiles"},
            }
        )

    tools_text = (REPO_ROOT / "backend/src/jarvis_gpt/tools.py").read_text(encoding="utf-8")
    tool_names = sorted(set(re.findall(r'ToolSpec\(\s*\n\s*name="([^"]+)"', tools_text)))
    for index, name in enumerate(tool_names, 1):
        extra = []
        if name.startswith("web.") or name.startswith("internet."):
            extra = ["REQ-URL-001", "REQ-TRUST-001"]
        elif name.startswith(("files.", "filesystem.", "documents.")):
            extra = ["REQ-FILES-001", "REQ-TRUST-001"]
        elif name.startswith("approval."):
            extra = ["REQ-APPROVAL-001", "REQ-SECRET-001"]
        features.append(
            {
                "id": f"FEAT-TOOL-{index:03d}",
                "name": name,
                "kind": "tool",
                "status": "STATIC_SUPPORTED",
                "source_path": "backend/src/jarvis_gpt/tools.py",
                "symbol": name,
                "requirement_ids": sorted(set(KIND_REQUIREMENT["tool"] + extra)),
                "phase_b_required": True,
            }
        )

    for index, (name, model) in enumerate(
        [
            ("gemma4-turbo", "gemma4-26b-a4b-nvfp4"),
            ("gemma4-mono-perf", "gemma4-31b-it-nvfp4"),
            ("gemma4-mono", "gemma4-31b-it-nvfp4"),
        ], 1
    ):
        features.append(
            {
                "id": f"FEAT-PROFILE-{index:03d}",
                "name": name,
                "model": model,
                "kind": "profile",
                "status": "STATIC_SUPPORTED",
                "source_path": "backend/src/jarvis_gpt/config.py",
                "requirement_ids": KIND_REQUIREMENT["profile"],
                "phase_b_required": True,
            }
        )

    for index, name in enumerate(["dispatcher", "backend", "frontend"], 1):
        features.append(
            {
                "id": f"FEAT-SERVICE-{index:03d}",
                "name": name,
                "kind": "service",
                "status": "STATIC_SUPPORTED",
                "source_path": "docker-compose.yml",
                "requirement_ids": KIND_REQUIREMENT["service"],
                "phase_b_required": True,
            }
        )

    ui_features = [
        "chat and streaming transcript",
        "session history restore",
        "mission planning and progress",
        "approval queue",
        "model catalog and activation",
        "dispatcher controls",
        "telemetry and readiness",
        "autonomy controls and jobs",
        "persona editor",
        "memory and file search",
        "tool runner",
        "operator queue",
        "thought trace view",
        "live event feed",
        "PWA/service worker cache",
    ]
    for index, name in enumerate(ui_features, 1):
        features.append(
            {
                "id": f"FEAT-UI-{index:03d}",
                "name": name,
                "kind": "ui",
                "status": "STATIC_SUPPORTED",
                "source_path": "frontend/app/page.tsx",
                "requirement_ids": KIND_REQUIREMENT["ui"],
                "phase_b_required": True,
            }
        )

    subsystems = [
        ("SQLite/WAL runtime state", ["REQ-STORAGE-001"]),
        ("memory/persona/learning", ["REQ-STORAGE-001", "REQ-SECRET-001"]),
        ("executive planning and replay", ["REQ-IDEMPOTENCY-001", "REQ-APPROVAL-001"]),
        ("document ingestion and conversion", ["REQ-FILES-001", "REQ-TRUST-001"]),
        ("archive processing", ["REQ-ARCHIVE-001", "REQ-FILES-001"]),
        ("web worker and browser", ["REQ-URL-001", "REQ-TRUST-001", "REQ-SECRET-001"]),
        ("autonomy supervisor", ["REQ-JOBS-001", "REQ-RESOURCE-001"]),
        ("host bridge and deterministic execution", ["REQ-APPROVAL-001", "REQ-FILES-001"]),
    ]
    for index, (name, reqs) in enumerate(subsystems, 1):
        features.append(
            {
                "id": f"FEAT-SUBSYS-{index:03d}",
                "name": name,
                "kind": "subsystem",
                "status": "STATIC_SUPPORTED",
                "source_path": "backend/src/jarvis_gpt",
                "requirement_ids": reqs,
                "phase_b_required": True,
            }
        )

    requirements = [
        {
            "id": req_id,
            "text": text,
            "risk": risk,
            "status": "STATIC_SUPPORTED",
            "source": "README/docs/public schemas/tests/implementation reconciliation",
        }
        for req_id, text, risk in REQUIREMENTS
    ]
    emit(MACHINE / "features.jsonl", features)
    emit(MACHINE / "requirements.jsonl", requirements)
    summary = {
        "features": len(features),
        "requirements": len(requirements),
        "api_features": sum(f["kind"] == "api" for f in features),
        "websocket_features": sum(f["kind"] == "websocket" for f in features),
        "cli_features": sum(f["kind"] == "cli" for f in features),
        "tool_features": sum(f["kind"] == "tool" for f in features),
        "ui_features": sum(f["kind"] == "ui" for f in features),
    }
    (EVIDENCE / "traceability_seed_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
