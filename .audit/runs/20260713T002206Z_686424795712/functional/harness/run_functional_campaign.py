#!/usr/bin/env python3
"""Safe live functional acceptance harness for the local JARVIS stack.

The harness intentionally has no model, dispatcher, cleanup, host-bridge, browser,
autonomy-run, approval-execute, or arbitrary tool-execution code paths.  Every
state-changing HTTP request is checked against a small explicit allowlist.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import hashlib
import json
import math
import os
import re
import secrets
import statistics
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import httpx
except ImportError as exc:  # pragma: no cover - runtime dependency check
    raise SystemExit("httpx is required; run with the project Python environment") from exc

try:
    from websockets.asyncio.client import connect as websocket_connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus
except ImportError as exc:  # pragma: no cover - runtime dependency check
    raise SystemExit("websockets is required; run with the project Python environment") from exc


SCRIPT_PATH = Path(__file__).resolve()
FUNCTIONAL_DIR = SCRIPT_PATH.parent.parent
EVIDENCE_DIR = FUNCTIONAL_DIR / "evidence"
READ_ONLY_TOOL_NAMES = ("runtime.status", "environment.profile", "memory.search")
READ_ONLY_CLI_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("profiles",),
    ("status",),
    ("models",),
    ("llm-health",),
)
REDACTED_KEYS = {
    "authorization",
    "api_token",
    "token",
    "password",
    "secret",
    "x-jarvis-api-token",
}
CAMPAIGN_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")


class CaseSkip(RuntimeError):
    """A checked precondition made a case not applicable or unavailable."""


class SafetyViolation(RuntimeError):
    """The harness attempted an operation outside its hard safety allowlist."""


@dataclass(slots=True)
class Outcome:
    checks: dict[str, bool]
    expected: Any
    observed: Any
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StreamCapture:
    status_code: int
    content_type: str
    items: list[dict[str, Any]]
    raw_lines: list[str]
    parse_errors: list[str]
    latency_ms: float


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "<max-depth>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.casefold() in REDACTED_KEYS or any(
                marker in key_text.casefold() for marker in ("password", "secret", "credential")
            ):
                result[key_text] = "<redacted>"
            else:
                result[key_text] = sanitize(item, depth=depth + 1)
        return result
    if isinstance(value, list | tuple):
        return [sanitize(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value if len(value) <= 2400 else f"{value[:2400]}...<truncated>"
    if isinstance(value, int | float | bool) or value is None:
        return value
    return sanitize(str(value), depth=depth + 1)


def response_snapshot(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    try:
        body: Any = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = response.text[:2400]
    return sanitize(
        {
            "status_code": response.status_code,
            "content_type": content_type,
            "body": body,
        }
    )


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def find_repo_root() -> Path:
    for candidate in SCRIPT_PATH.parents:
        if (candidate / "jarvis.py").is_file() and (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError("repository root containing jarvis.py and pyproject.toml was not found")


def build_campaign_identity(prefix: str) -> tuple[str, str]:
    if not CAMPAIGN_PREFIX_RE.fullmatch(prefix):
        raise ValueError("campaign prefix must match [A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    nonce = secrets.token_hex(6)
    campaign_id = f"{prefix}-{timestamp}-{nonce}"
    namespace = f"functional_acceptance.{timestamp}.{nonce}"
    return campaign_id, namespace


class EvidenceRecorder:
    FIELDS = (
        "campaign_id",
        "namespace",
        "case_id",
        "category",
        "required",
        "status",
        "started_at",
        "finished_at",
        "latency_ms",
        "checks",
        "expected",
        "observed",
        "notes",
    )

    def __init__(self, campaign_id: str, namespace: str) -> None:
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        self.campaign_id = campaign_id
        self.namespace = namespace
        self.jsonl_path = EVIDENCE_DIR / f"{campaign_id}.jsonl"
        self.csv_path = EVIDENCE_DIR / f"{campaign_id}.csv"
        self.manifest_path = EVIDENCE_DIR / f"{campaign_id}.manifest.json"
        self.synthetic_dir = EVIDENCE_DIR / "synthetic" / campaign_id
        self.records: list[dict[str, Any]] = []
        with self.jsonl_path.open("x", encoding="utf-8", newline="\n"):
            pass
        with self.csv_path.open("x", encoding="utf-8-sig", newline="") as handle:
            csv.DictWriter(handle, fieldnames=self.FIELDS).writeheader()

    def _append(self, record: dict[str, Any]) -> None:
        record = sanitize(record)
        self.records.append(record)
        with self.jsonl_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(compact_json(record))
            handle.write("\n")
        csv_record = dict(record)
        for key in ("checks", "expected", "observed", "notes"):
            csv_record[key] = compact_json(csv_record.get(key))
        with self.csv_path.open("a", encoding="utf-8-sig", newline="") as handle:
            csv.DictWriter(handle, fieldnames=self.FIELDS).writerow(csv_record)

    def verified(
        self,
        *,
        case_id: str,
        category: str,
        required: bool,
        started_at: str,
        started_perf: float,
        outcome: Outcome,
    ) -> None:
        checks = {str(key): bool(value) for key, value in outcome.checks.items()}
        if not checks:
            self.error(
                case_id=case_id,
                category=category,
                required=required,
                started_at=started_at,
                started_perf=started_perf,
                error="case returned no verifiable assertions",
            )
            return
        status = "PASS" if all(checks.values()) else "FAIL"
        self._append(
            {
                "campaign_id": self.campaign_id,
                "namespace": self.namespace,
                "case_id": case_id,
                "category": category,
                "required": required,
                "status": status,
                "started_at": started_at,
                "finished_at": utc_now(),
                "latency_ms": round((time.perf_counter() - started_perf) * 1000, 3),
                "checks": checks,
                "expected": outcome.expected,
                "observed": outcome.observed,
                "notes": outcome.notes,
            }
        )

    def skipped(
        self,
        *,
        case_id: str,
        category: str,
        required: bool,
        started_at: str,
        started_perf: float,
        reason: str,
    ) -> None:
        self._append(
            {
                "campaign_id": self.campaign_id,
                "namespace": self.namespace,
                "case_id": case_id,
                "category": category,
                "required": required,
                "status": "SKIP",
                "started_at": started_at,
                "finished_at": utc_now(),
                "latency_ms": round((time.perf_counter() - started_perf) * 1000, 3),
                "checks": {},
                "expected": None,
                "observed": None,
                "notes": [reason],
            }
        )

    def error(
        self,
        *,
        case_id: str,
        category: str,
        required: bool,
        started_at: str,
        started_perf: float,
        error: str,
    ) -> None:
        self._append(
            {
                "campaign_id": self.campaign_id,
                "namespace": self.namespace,
                "case_id": case_id,
                "category": category,
                "required": required,
                "status": "ERROR",
                "started_at": started_at,
                "finished_at": utc_now(),
                "latency_ms": round((time.perf_counter() - started_perf) * 1000, 3),
                "checks": {},
                "expected": None,
                "observed": None,
                "notes": [error],
            }
        )

    def finalize(self, metadata: dict[str, Any]) -> tuple[str, int]:
        counts = {status: 0 for status in ("PASS", "FAIL", "ERROR", "SKIP")}
        for record in self.records:
            counts[str(record["status"])] += 1
        blocking_skips = [
            record
            for record in self.records
            if record["status"] == "SKIP" and bool(record["required"])
        ]
        if counts["FAIL"] or counts["ERROR"]:
            verdict, exit_code = "FAIL", 1
        elif blocking_skips:
            verdict, exit_code = "INCOMPLETE", 2
        else:
            verdict, exit_code = "PASS", 0
        manifest = sanitize(
            {
                **metadata,
                "campaign_id": self.campaign_id,
                "namespace": self.namespace,
                "finished_at": utc_now(),
                "verdict": verdict,
                "counts": counts,
                "required_skips": [item["case_id"] for item in blocking_skips],
                "evidence": {
                    "jsonl": str(self.jsonl_path),
                    "csv": str(self.csv_path),
                },
            }
        )
        with self.manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return verdict, exit_code


class SafeApiClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.owned_conversation_ids: set[str] = set()
        self.owned_message_ids: set[str] = set()
        self.owned_mission_ids: set[str] = set()
        self.owned_approval_ids: set[str] = set()

    def _guard(self, method: str, path: str, body: Any) -> None:
        method = method.upper()
        clean_path = path.split("?", 1)[0]
        if method in {"GET", "HEAD", "OPTIONS"}:
            return
        if method == "POST" and clean_path in {
            "/api/chat",
            "/api/chat/stream",
            "/api/files/upload",
            "/api/files/ingest-directory",
            "/api/memory",
            "/api/approvals",
            "/api/missions",
        }:
            return
        if (
            method == "POST"
            and clean_path.startswith("/api/tools/")
            and clean_path.endswith("/run")
        ):
            tool_name = clean_path.removeprefix("/api/tools/").removesuffix("/run")
            if tool_name in READ_ONLY_TOOL_NAMES:
                return
        feedback_match = re.fullmatch(r"/api/messages/([^/]+)/feedback", clean_path)
        if (
            method == "POST"
            and feedback_match
            and feedback_match.group(1) in self.owned_message_ids
        ):
            return
        approval_match = re.fullmatch(r"/api/approvals/([^/]+)", clean_path)
        if method == "PATCH" and approval_match:
            approval_id = approval_match.group(1)
            if approval_id in self.owned_approval_ids and body == {"status": "cancelled"}:
                return
        mission_task_match = re.fullmatch(
            r"/api/missions/([^/]+)/tasks/([^/]+)", clean_path
        )
        if method == "PATCH" and mission_task_match:
            mission_id = mission_task_match.group(1)
            if mission_id in self.owned_mission_ids and body == {"status": "done"}:
                return
        conversation_match = re.fullmatch(r"/api/conversations/([^/]+)", clean_path)
        if (
            method == "DELETE"
            and conversation_match
            and conversation_match.group(1) in self.owned_conversation_ids
        ):
            return
        raise SafetyViolation(f"blocked non-allowlisted request: {method} {clean_path}")

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        files: Any = None,
    ) -> httpx.Response:
        self._guard(method, path, json_body)
        return await self.client.request(method, path, json=json_body, files=files)

    async def capture_stream(self, path: str, body: dict[str, Any]) -> StreamCapture:
        self._guard("POST", path, body)
        started = time.perf_counter()
        items: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        parse_errors: list[str] = []
        async with self.client.stream("POST", path, json=body) as response:
            status_code = response.status_code
            content_type = response.headers.get("content-type", "")
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                raw_lines.append(line)
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as exc:
                    parse_errors.append(f"{exc.msg} at {exc.pos}: {line[:240]}")
                    continue
                if not isinstance(parsed, dict):
                    parse_errors.append(f"NDJSON item is not an object: {type(parsed).__name__}")
                    continue
                items.append(parsed)
        return StreamCapture(
            status_code=status_code,
            content_type=content_type,
            items=items,
            raw_lines=raw_lines,
            parse_errors=parse_errors,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


@dataclass(slots=True)
class CampaignContext:
    args: argparse.Namespace
    campaign_id: str
    namespace: str
    marker: str
    recorder: EvidenceRecorder
    api: SafeApiClient
    repo_root: Path
    state: dict[str, Any] = field(default_factory=dict)

    def require(self, key: str) -> Any:
        if key not in self.state:
            raise CaseSkip(f"required prior evidence is unavailable: {key}")
        return self.state[key]


CaseFunction = Callable[[CampaignContext], Awaitable[Outcome]]


async def run_case(
    context: CampaignContext,
    case_id: str,
    category: str,
    function: CaseFunction,
    *,
    required: bool = True,
) -> None:
    started_at = utc_now()
    started_perf = time.perf_counter()
    try:
        outcome = await function(context)
    except CaseSkip as exc:
        context.recorder.skipped(
            case_id=case_id,
            category=category,
            required=required,
            started_at=started_at,
            started_perf=started_perf,
            reason=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - evidence must capture the actual failure
        context.recorder.error(
            case_id=case_id,
            category=category,
            required=required,
            started_at=started_at,
            started_perf=started_perf,
            error=f"{type(exc).__name__}: {exc}",
        )
    else:
        context.recorder.verified(
            case_id=case_id,
            category=category,
            required=required,
            started_at=started_at,
            started_perf=started_perf,
            outcome=outcome,
        )


async def case_health(context: CampaignContext) -> Outcome:
    response = await context.api.request("GET", "/health")
    body = response.json() if response.status_code == 200 else {}
    if response.status_code == 200 and isinstance(body, dict):
        context.state["health"] = body
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "ok_true": body.get("ok") is True,
            "home_present": isinstance(body.get("home"), str) and bool(body.get("home")),
            "profile_present": isinstance(body.get("profile"), str) and bool(body.get("profile")),
        },
        expected={"status_code": 200, "ok": True, "home": "non-empty", "profile": "non-empty"},
        observed=response_snapshot(response),
    )


async def case_status(context: CampaignContext) -> Outcome:
    response = await context.api.request("GET", "/api/status")
    body = response.json() if response.status_code == 200 else {}
    if response.status_code == 200 and isinstance(body, dict):
        context.state["status"] = body
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "settings_object": isinstance(body.get("settings"), dict),
            "counters_object": isinstance(body.get("counters"), dict),
            "health_array": isinstance(body.get("health"), list),
            "recent_events_array": isinstance(body.get("recent_events"), list),
        },
        expected="StatusResponse contract",
        observed=response_snapshot(response),
    )


async def case_environment_profile(context: CampaignContext) -> Outcome:
    response = await context.api.request("GET", "/api/environment/profile")
    body = response.json() if response.status_code == 200 else {}
    profile = body.get("profile") if isinstance(body, dict) else None
    fingerprint = profile.get("fingerprint_sha256") if isinstance(profile, dict) else None
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "profile_object": isinstance(profile, dict),
            "fingerprint_sha256": isinstance(fingerprint, str)
            and bool(re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint)),
        },
        expected="verified host profile with a SHA-256 capability fingerprint",
        observed=response_snapshot(response),
    )


async def case_model_profiles(context: CampaignContext) -> Outcome:
    response = await context.api.request("GET", "/api/model-profiles")
    body = response.json() if response.status_code == 200 else {}
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "active_profile": isinstance(body.get("active_profile"), str)
            and bool(body.get("active_profile")),
            "active_model": isinstance(body.get("active_model"), str),
            "profiles_array": isinstance(body.get("profiles"), list) and bool(body.get("profiles")),
        },
        expected="ModelProfilesResponse contract",
        observed=response_snapshot(response),
    )


async def case_model_catalog(context: CampaignContext) -> Outcome:
    response = await context.api.request("GET", "/api/models")
    body = response.json() if response.status_code == 200 else {}
    if response.status_code == 200 and isinstance(body, dict):
        context.state["model_catalog"] = body
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "root_present": isinstance(body.get("root"), str) and bool(body.get("root")),
            "active_profile_present": isinstance(body.get("active_profile"), str),
            "active_model_object": isinstance(body.get("active_model"), dict),
            "models_array": isinstance(body.get("models"), list),
            "dispatcher_object": isinstance(body.get("dispatcher"), dict),
        },
        expected="read-only model inventory contract",
        observed=response_snapshot(response),
    )


async def case_dispatcher_read(context: CampaignContext) -> Outcome:
    response = await context.api.request("GET", "/api/dispatcher")
    body = response.json() if response.status_code == 200 else {}
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "service_present": isinstance(body.get("service"), str),
            "port_integer": isinstance(body.get("port"), int),
            "port_open_boolean": isinstance(body.get("port_open"), bool),
            "active_model_object": isinstance(body.get("active_model"), dict),
        },
        expected="dispatcher status is readable; no dispatcher action is issued",
        observed=response_snapshot(response),
    )


def cli_json(stdout: str) -> Any:
    text = stdout.strip()
    if not text:
        raise ValueError("CLI produced empty stdout")
    return json.loads(text)


async def run_cli_case(context: CampaignContext, command: tuple[str, ...]) -> Outcome:
    if context.args.skip_cli:
        raise CaseSkip("CLI checks disabled by --skip-cli")
    if command not in READ_ONLY_CLI_COMMANDS:
        raise SafetyViolation(f"CLI command is not read-only allowlisted: {command}")
    health = context.require("health")
    runtime_home = str(health.get("home") or "").strip()
    if not runtime_home:
        raise CaseSkip("live runtime home was not returned by /health")
    environment = os.environ.copy()
    environment["JARVIS_HOME"] = runtime_home
    process = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, str(context.repo_root / "jarvis.py"), *command],
        cwd=context.repo_root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=context.args.cli_timeout,
        check=False,
        shell=False,
    )
    parsed: Any = None
    parse_ok = False
    try:
        parsed = cli_json(process.stdout)
        parse_ok = True
    except (json.JSONDecodeError, ValueError):
        pass
    checks = {
        "exit_zero": process.returncode == 0,
        "stdout_json": parse_ok,
        "stderr_empty": not process.stderr.strip(),
    }
    if command == ("profiles",):
        checks["profiles_nonempty"] = isinstance(parsed, dict) and bool(parsed)
    elif command == ("status",):
        checks["status_contract"] = isinstance(parsed, dict) and isinstance(
            parsed.get("settings"), dict
        ) and isinstance(parsed.get("counters"), dict)
    elif command == ("models",):
        checks["models_contract"] = isinstance(parsed, dict) and isinstance(
            parsed.get("models"), list
        )
    elif command == ("llm-health",):
        checks["model_route_healthy"] = isinstance(parsed, dict) and parsed.get("ok") is True
    return Outcome(
        checks=checks,
        expected={"command": command, "returncode": 0, "stdout": "valid JSON"},
        observed={
            "command": [sys.executable, "jarvis.py", *command],
            "returncode": process.returncode,
            "stdout": sanitize(parsed if parse_ok else process.stdout),
            "stderr": sanitize(process.stderr),
        },
        notes=["CLI invocation is shell=False and restricted to a fixed read-only allowlist."],
    )


async def case_chat(context: CampaignContext) -> Outcome:
    prompt = (
        f"[{context.marker}] Функциональная проверка диалога. "
        "Ответь одной короткой фразой по-русски и не вызывай инструменты."
    )
    response = await context.api.request(
        "POST",
        "/api/chat",
        json_body={
            "message": prompt,
            "mode": "chat",
            "max_tokens": 128,
            "thinking_enabled": False,
        },
    )
    body = response.json() if response.status_code == 200 else {}
    conversation_id = body.get("conversation_id") if isinstance(body, dict) else None
    message_id = body.get("message_id") if isinstance(body, dict) else None
    if isinstance(conversation_id, str):
        context.api.owned_conversation_ids.add(conversation_id)
    if isinstance(message_id, str):
        context.api.owned_message_ids.add(message_id)
    if response.status_code == 200 and isinstance(body, dict):
        context.state["chat"] = {"prompt": prompt, "response": body}
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "conversation_id": isinstance(conversation_id, str)
            and conversation_id.startswith("conv_"),
            "message_id": isinstance(message_id, str) and message_id.startswith("msg_"),
            "answer_nonempty": isinstance(body.get("answer"), str)
            and bool(body.get("answer", "").strip()),
            "events_array": isinstance(body.get("events"), list) and bool(body.get("events")),
            "duration_nonnegative": isinstance(body.get("duration_ms"), int)
            and body.get("duration_ms") >= 0,
        },
        expected="one bounded chat turn with persisted identifiers and runtime events",
        observed=response_snapshot(response),
    )


async def case_chat_live_route(context: CampaignContext) -> Outcome:
    chat = context.require("chat")["response"]
    events = chat.get("events", [])
    fallback_events = [
        event
        for event in events
        if isinstance(event, dict)
        and (
            str(event.get("title", "")).casefold() == "offline fallback"
            or (
                isinstance(event.get("payload"), dict)
                and event["payload"].get("source") == "fallback"
            )
        )
    ]
    return Outcome(
        checks={
            "no_offline_fallback": not fallback_events,
            "assistant_answer_nonempty": bool(str(chat.get("answer", "")).strip()),
        },
        expected="live model path, not the offline fallback",
        observed={"fallback_events": fallback_events, "event_count": len(events)},
    )


async def case_chat_history(context: CampaignContext) -> Outcome:
    chat = context.require("chat")
    response_body = chat["response"]
    conversation_id = response_body["conversation_id"]
    response = await context.api.request(
        "GET", f"/api/conversations/{conversation_id}/messages?limit=20"
    )
    body = response.json() if response.status_code == 200 else []
    user_matches = [
        item
        for item in body
        if isinstance(item, dict) and context.marker in str(item.get("content", ""))
    ]
    assistant_matches = [
        item
        for item in body
        if isinstance(item, dict) and item.get("id") == response_body.get("message_id")
    ]
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "messages_array": isinstance(body, list),
            "campaign_user_message": bool(user_matches),
            "assistant_message_id": bool(assistant_matches),
            "answer_persisted": bool(assistant_matches)
            and assistant_matches[0].get("content") == response_body.get("answer"),
        },
        expected="the exact campaign turn is persisted in conversation history",
        observed=response_snapshot(response),
    )


async def case_conversation_list(context: CampaignContext) -> Outcome:
    conversation_id = context.require("chat")["response"]["conversation_id"]
    response = await context.api.request("GET", "/api/conversations?limit=100")
    body = response.json() if response.status_code == 200 else []
    matches = [
        item for item in body if isinstance(item, dict) and item.get("id") == conversation_id
    ]
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "array": isinstance(body, list),
            "campaign_conversation_listed": len(matches) == 1,
            "message_count_at_least_two": bool(matches) and matches[0].get("message_count", 0) >= 2,
        },
        expected="campaign conversation appears once with user and assistant messages",
        observed=response_snapshot(response),
    )


async def case_trace_message(context: CampaignContext) -> Outcome:
    chat = context.require("chat")
    message_id = chat["response"]["message_id"]
    response = await context.api.request("GET", f"/api/agent/trace/message/{message_id}")
    body = response.json() if response.status_code == 200 else {}
    output = body.get("output") if isinstance(body, dict) else None
    input_item = body.get("input") if isinstance(body, dict) else None
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "output_matches": isinstance(output, dict) and output.get("id") == message_id,
            "input_has_marker": isinstance(input_item, dict)
            and context.marker in str(input_item.get("content", "")),
            "nodes_array_nonempty": isinstance(body.get("nodes"), list) and bool(body.get("nodes")),
            "edges_array": isinstance(body.get("edges"), list),
        },
        expected="message trace links the exact marked input to the persisted assistant output",
        observed=response_snapshot(response),
    )


async def case_trace_conversation(context: CampaignContext) -> Outcome:
    conversation_id = context.require("chat")["response"]["conversation_id"]
    response = await context.api.request("GET", f"/api/agent/trace/{conversation_id}")
    body = response.json() if response.status_code == 200 else {}
    conversation = body.get("conversation") if isinstance(body, dict) else None
    turns = body.get("turns") if isinstance(body, dict) else None
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "conversation_id_matches": isinstance(conversation, dict)
            and conversation.get("id") == conversation_id,
            "turns_array_nonempty": isinstance(turns, list) and bool(turns),
        },
        expected="conversation-level trace for the campaign chat",
        observed=response_snapshot(response),
    )


async def case_stream_capture(context: CampaignContext) -> Outcome:
    prompt = (
        f"[{context.marker}:stream] Проверка NDJSON. "
        "Ответь одной короткой фразой и не вызывай инструменты."
    )
    capture = await context.api.capture_stream(
        "/api/chat/stream",
        {
            "message": prompt,
            "mode": "chat",
            "max_tokens": 128,
            "thinking_enabled": False,
        },
    )
    context.state["stream"] = {"prompt": prompt, "capture": capture}
    return Outcome(
        checks={
            "http_200": capture.status_code == 200,
            "ndjson_content_type": capture.content_type.startswith("application/x-ndjson"),
            "at_least_one_line": bool(capture.raw_lines),
        },
        expected="HTTP 200 application/x-ndjson with at least one non-empty line",
        observed={
            "status_code": capture.status_code,
            "content_type": capture.content_type,
            "line_count": len(capture.raw_lines),
            "latency_ms": round(capture.latency_ms, 3),
        },
    )


async def case_stream_json(context: CampaignContext) -> Outcome:
    capture: StreamCapture = context.require("stream")["capture"]
    types = [str(item.get("type", "")) for item in capture.items]
    allowed = {"meta", "event", "delta", "done", "error"}
    return Outcome(
        checks={
            "all_lines_parse": not capture.parse_errors,
            "object_per_line": len(capture.items) == len(capture.raw_lines),
            "known_types_only": bool(types) and all(item in allowed for item in types),
        },
        expected="every non-empty line is one JSON object with a known stream type",
        observed={"types": types, "parse_errors": capture.parse_errors},
    )


async def case_stream_order(context: CampaignContext) -> Outcome:
    capture: StreamCapture = context.require("stream")["capture"]
    types = [str(item.get("type", "")) for item in capture.items]
    done_positions = [index for index, item in enumerate(types) if item == "done"]
    return Outcome(
        checks={
            "meta_first": bool(types) and types[0] == "meta",
            "exactly_one_done": len(done_positions) == 1,
            "done_last": done_positions == [len(types) - 1],
            "delta_present": "delta" in types,
            "no_error_item": "error" not in types,
        },
        expected="meta first, at least one delta, exactly one terminal done, no error item",
        observed={"types": types},
    )


async def case_stream_delta_done(context: CampaignContext) -> Outcome:
    capture: StreamCapture = context.require("stream")["capture"]
    delta_text = "".join(
        str(item.get("content", ""))
        for item in capture.items
        if item.get("type") == "delta"
    )
    done_items = [item for item in capture.items if item.get("type") == "done"]
    done = done_items[0] if len(done_items) == 1 else {}
    conversation_id = done.get("conversation_id")
    message_id = done.get("message_id")
    if isinstance(conversation_id, str):
        context.api.owned_conversation_ids.add(conversation_id)
    if isinstance(message_id, str):
        context.api.owned_message_ids.add(message_id)
    context.state["stream_done"] = done
    return Outcome(
        checks={
            "done_answer_nonempty": isinstance(done.get("answer"), str)
            and bool(done.get("answer", "").strip()),
            "delta_equals_done": bool(delta_text) and delta_text == done.get("answer"),
            "conversation_id": isinstance(conversation_id, str)
            and conversation_id.startswith("conv_"),
            "message_id": isinstance(message_id, str) and message_id.startswith("msg_"),
            "duration_nonnegative": isinstance(done.get("duration_ms"), int)
            and done.get("duration_ms") >= 0,
        },
        expected="concatenated deltas equal the terminal persisted answer",
        observed=sanitize({"delta_text": delta_text, "done": done}),
    )


async def case_stream_history(context: CampaignContext) -> Outcome:
    done = context.require("stream_done")
    prompt = context.require("stream")["prompt"]
    response = await context.api.request(
        "GET", f"/api/conversations/{done['conversation_id']}/messages?limit=20"
    )
    body = response.json() if response.status_code == 200 else []
    output = [
        item
        for item in body
        if isinstance(item, dict) and item.get("id") == done.get("message_id")
    ]
    inputs = [item for item in body if isinstance(item, dict) and item.get("content") == prompt]
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "exact_input_persisted": len(inputs) == 1,
            "exact_output_persisted": len(output) == 1,
            "output_equals_done": bool(output) and output[0].get("content") == done.get("answer"),
        },
        expected="NDJSON terminal result is present in durable history",
        observed=response_snapshot(response),
    )


async def case_parallel_chats(context: CampaignContext) -> Outcome:
    count = context.args.parallel_chats
    prompts = [
        f"[{context.marker}:parallel:{index}] Ответь только: канал {index}. Без инструментов."
        for index in range(count)
    ]

    async def one(prompt: str) -> tuple[httpx.Response, float]:
        started = time.perf_counter()
        response = await context.api.request(
            "POST",
            "/api/chat",
            json_body={
                "message": prompt,
                "mode": "chat",
                "max_tokens": 96,
                "thinking_enabled": False,
            },
        )
        return response, (time.perf_counter() - started) * 1000

    results = await asyncio.gather(*(one(prompt) for prompt in prompts), return_exceptions=True)
    bodies: list[dict[str, Any]] = []
    latencies: list[float] = []
    errors: list[str] = []
    statuses: list[int] = []
    for result in results:
        if isinstance(result, BaseException):
            errors.append(f"{type(result).__name__}: {result}")
            continue
        response, latency = result
        statuses.append(response.status_code)
        latencies.append(latency)
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            errors.append(f"JSONDecodeError: {exc}")
            continue
        if isinstance(body, dict):
            bodies.append(body)
            conversation_id = body.get("conversation_id")
            message_id = body.get("message_id")
            if isinstance(conversation_id, str):
                context.api.owned_conversation_ids.add(conversation_id)
            if isinstance(message_id, str):
                context.api.owned_message_ids.add(message_id)
    context.state["parallel"] = {"prompts": prompts, "bodies": bodies, "latencies": latencies}
    conversation_ids = [body.get("conversation_id") for body in bodies]
    message_ids = [body.get("message_id") for body in bodies]
    return Outcome(
        checks={
            "requested_count_completed": len(results) == count,
            "no_exceptions": not errors,
            "all_http_200": len(statuses) == count and all(status == 200 for status in statuses),
            "all_bodies": len(bodies) == count,
            "unique_conversations": len(set(conversation_ids)) == count,
            "unique_messages": len(set(message_ids)) == count,
            "answers_nonempty": len(bodies) == count
            and all(bool(str(body.get("answer", "")).strip()) for body in bodies),
            "within_chat_budget": bool(latencies)
            and max(latencies) <= context.args.chat_latency_budget_ms,
        },
        expected={
            "parallel_chats": count,
            "unique_conversations": count,
            "max_latency_ms": context.args.chat_latency_budget_ms,
        },
        observed={
            "statuses": statuses,
            "conversation_ids": conversation_ids,
            "message_ids": message_ids,
            "latencies_ms": [round(item, 3) for item in latencies],
            "errors": errors,
        },
    )


async def case_parallel_history(context: CampaignContext) -> Outcome:
    parallel = context.require("parallel")
    prompts: list[str] = parallel["prompts"]
    bodies: list[dict[str, Any]] = parallel["bodies"]
    if len(prompts) != len(bodies):
        raise CaseSkip("parallel chat responses were incomplete")

    async def history(body: dict[str, Any]) -> httpx.Response:
        return await context.api.request(
            "GET", f"/api/conversations/{body['conversation_id']}/messages?limit=20"
        )

    responses = await asyncio.gather(*(history(body) for body in bodies))
    checks: dict[str, bool] = {}
    observations: list[dict[str, Any]] = []
    for index, (prompt, body, response) in enumerate(zip(prompts, bodies, responses, strict=True)):
        messages = response.json() if response.status_code == 200 else []
        checks[f"history_{index}_http_200"] = response.status_code == 200
        checks[f"history_{index}_prompt"] = any(
            isinstance(item, dict) and item.get("content") == prompt for item in messages
        )
        checks[f"history_{index}_answer"] = any(
            isinstance(item, dict)
            and item.get("id") == body.get("message_id")
            and item.get("content") == body.get("answer")
            for item in messages
        )
        observations.append(
            {
                "conversation_id": body.get("conversation_id"),
                "status_code": response.status_code,
                "message_count": len(messages) if isinstance(messages, list) else None,
            }
        )
    return Outcome(
        checks=checks,
        expected="every concurrent channel preserves its own exact prompt and answer",
        observed=observations,
    )


async def case_synthetic_fixtures(context: CampaignContext) -> Outcome:
    root = context.recorder.synthetic_dir
    root.mkdir(parents=True, exist_ok=False)
    marker = context.marker
    fixtures = {
        "acceptance.txt": f"JARVIS functional marker: {marker}\nSynthetic plain text fixture.\n",
        "acceptance.md": (
            f"# Synthetic acceptance document\n\nCampaign marker: `{marker}`.\n\n"
            "This file contains no instructions and no external data.\n"
        ),
        "acceptance.json": compact_json(
            {"schema": "jarvis.functional.synthetic.v1", "marker": marker, "safe": True}
        )
        + "\n",
    }
    paths: dict[str, Path] = {}
    for name, content in fixtures.items():
        path = root / name
        path.write_text(content, encoding="utf-8", newline="\n")
        paths[name] = path
    context.state["fixtures"] = {"root": root, "paths": paths, "contents": fixtures}
    return Outcome(
        checks={
            "synthetic_root_under_evidence": root.is_relative_to(EVIDENCE_DIR),
            "three_files_created": len(paths) == 3
            and all(path.is_file() for path in paths.values()),
            "marker_in_every_file": all(
                marker in path.read_text(encoding="utf-8") for path in paths.values()
            ),
        },
        expected="three unique synthetic fixtures under functional/evidence only",
        observed={"root": str(root), "files": [str(path) for path in paths.values()]},
    )


async def upload_fixture(context: CampaignContext, name: str) -> Outcome:
    fixtures = context.require("fixtures")
    path: Path = fixtures["paths"][name]
    content = path.read_bytes()
    response = await context.api.request(
        "POST",
        "/api/files/upload",
        files={"file": (name, content, "text/plain" if name.endswith(".txt") else "text/markdown")},
    )
    body = response.json() if response.status_code == 200 else {}
    file_item = body.get("file") if isinstance(body, dict) else None
    if isinstance(file_item, dict) and isinstance(file_item.get("id"), str):
        context.state.setdefault("uploaded_files", {})[name] = {
            "item": file_item,
            "content": content,
        }
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "file_object": isinstance(file_item, dict),
            "name_matches": isinstance(file_item, dict) and file_item.get("name") == name,
            "size_matches": isinstance(file_item, dict) and file_item.get("size") == len(content),
            "sha256_matches": isinstance(file_item, dict)
            and file_item.get("sha256") == hashlib.sha256(content).hexdigest(),
            "chunks_indexed": isinstance(body.get("chunks_indexed"), int)
            and body.get("chunks_indexed") >= 1,
        },
        expected="uploaded synthetic fixture with exact name, size, SHA-256, and indexed content",
        observed=response_snapshot(response),
    )


async def case_file_metadata(context: CampaignContext) -> Outcome:
    uploads = context.require("uploaded_files")
    checks: dict[str, bool] = {}
    observed: list[Any] = []
    for name, entry in uploads.items():
        file_id = entry["item"]["id"]
        response = await context.api.request("GET", f"/api/files/{file_id}")
        body = response.json() if response.status_code == 200 else {}
        checks[f"{name}_http_200"] = response.status_code == 200
        checks[f"{name}_id"] = body.get("id") == file_id
        checks[f"{name}_sha256"] = body.get("sha256") == hashlib.sha256(
            entry["content"]
        ).hexdigest()
        observed.append(response_snapshot(response))
    return Outcome(
        checks=checks,
        expected="metadata endpoint preserves IDs and hashes for every uploaded fixture",
        observed=observed,
    )


async def case_file_download(context: CampaignContext) -> Outcome:
    uploads = context.require("uploaded_files")
    checks: dict[str, bool] = {}
    observed: list[Any] = []
    for name, entry in uploads.items():
        file_id = entry["item"]["id"]
        response = await context.api.request("GET", f"/api/files/{file_id}/download")
        checks[f"{name}_http_200"] = response.status_code == 200
        checks[f"{name}_bytes"] = response.content == entry["content"]
        checks[f"{name}_sha256"] = hashlib.sha256(response.content).hexdigest() == hashlib.sha256(
            entry["content"]
        ).hexdigest()
        observed.append(
            {
                "name": name,
                "status_code": response.status_code,
                "bytes": len(response.content),
                "sha256": hashlib.sha256(response.content).hexdigest(),
            }
        )
    return Outcome(
        checks=checks,
        expected="download bytes and SHA-256 exactly match the local synthetic fixtures",
        observed=observed,
    )


async def case_directory_ingest(context: CampaignContext) -> Outcome:
    root: Path = context.require("fixtures")["root"]
    response = await context.api.request(
        "POST",
        "/api/files/ingest-directory",
        json_body={"path": str(root), "max_files": 10},
    )
    body = response.json() if response.status_code == 200 else {}
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "root_matches": response.status_code == 200
            and Path(str(body.get("root", ""))).resolve(strict=False) == root.resolve(strict=False),
            "files_seen": isinstance(body.get("files_seen"), int) and body.get("files_seen") >= 3,
            "files_failed_zero": body.get("files_failed") == 0,
            "results_array": isinstance(body.get("results"), list),
        },
        expected="only the campaign synthetic directory is ingested without file failures",
        observed=response_snapshot(response),
    )


async def case_file_search(context: CampaignContext) -> Outcome:
    response = await context.api.request("GET", f"/api/files/search?q={context.marker}&limit=20")
    body = response.json() if response.status_code == 200 else []
    matches = [
        item
        for item in body
        if isinstance(item, dict)
        and context.marker.casefold()
        in str(item.get("content", item.get("snippet", ""))).casefold()
    ]
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "array": isinstance(body, list),
            "marker_match": bool(matches),
        },
        expected="indexed synthetic marker is retrievable from file chunks",
        observed=response_snapshot(response),
    )


async def case_memory_create(context: CampaignContext) -> Outcome:
    content = f"Synthetic functional memory for {context.marker}; not operator preference."
    response = await context.api.request(
        "POST",
        "/api/memory",
        json_body={
            "content": content,
            "namespace": context.namespace,
            "tags": ["functional", "synthetic", context.campaign_id],
            "importance": 0.1,
        },
    )
    body = response.json() if response.status_code == 200 else {}
    if response.status_code == 200:
        context.state["memory"] = body
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "id_present": isinstance(body.get("id"), str) and bool(body.get("id")),
            "namespace_exact": body.get("namespace") == context.namespace,
            "content_exact": body.get("content") == content,
            "low_importance": body.get("importance") == 0.1,
        },
        expected="one low-importance synthetic memory in the unique campaign namespace",
        observed=response_snapshot(response),
    )


async def case_memory_search(context: CampaignContext) -> Outcome:
    memory = context.require("memory")
    response = await context.api.request("GET", f"/api/memory?q={context.marker}&limit=20")
    body = response.json() if response.status_code == 200 else []
    matches = [
        item
        for item in body
        if isinstance(item, dict) and item.get("id") == memory.get("id")
    ]
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "array": isinstance(body, list),
            "created_memory_found_once": len(matches) == 1,
            "namespace_preserved": bool(matches)
            and matches[0].get("namespace") == context.namespace,
        },
        expected="search returns the exact memory ID in the campaign namespace",
        observed=response_snapshot(response),
    )


async def case_memory_vault(context: CampaignContext) -> Outcome:
    response = await context.api.request("GET", "/api/memory/vault")
    body = response.json() if response.status_code == 200 else {}
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "root_present": isinstance(body.get("root"), str),
            "nodes_array": isinstance(body.get("nodes"), list),
            "edges_array": isinstance(body.get("edges"), list),
            "stats_object": isinstance(body.get("stats"), dict),
        },
        expected="memory vault graph is readable after namespaced insertion",
        observed=response_snapshot(response),
    )


async def case_approval_create(context: CampaignContext) -> Outcome:
    response = await context.api.request(
        "POST",
        "/api/approvals",
        json_body={
            "title": f"Synthetic review {context.marker}",
            "description": "Functional acceptance marker only; never execute.",
            "requested_action": "manual.review",
            "risk": "review",
            "payload": {"campaign_id": context.campaign_id, "synthetic": True},
        },
    )
    body = response.json() if response.status_code == 200 else {}
    approval_id = body.get("id") if isinstance(body, dict) else None
    if isinstance(approval_id, str):
        context.api.owned_approval_ids.add(approval_id)
        context.state["approval"] = body
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "id_present": isinstance(approval_id, str) and approval_id.startswith("apr_"),
            "pending": body.get("status") == "pending",
            "review_risk": body.get("risk") == "review",
            "manual_action": body.get("requested_action") == "manual.review",
            "not_executed": not body.get("result"),
        },
        expected="pending synthetic review approval with no execution result",
        observed=response_snapshot(response),
        notes=["The harness contains no call to /api/approvals/{id}/execute."],
    )


async def case_approval_list(context: CampaignContext) -> Outcome:
    approval = context.require("approval")
    response = await context.api.request("GET", "/api/approvals?status=pending&limit=200")
    body = response.json() if response.status_code == 200 else []
    matches = [
        item
        for item in body
        if isinstance(item, dict) and item.get("id") == approval.get("id")
    ]
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "listed_once": len(matches) == 1,
            "still_pending": bool(matches) and matches[0].get("status") == "pending",
            "still_unexecuted": bool(matches) and not matches[0].get("result"),
        },
        expected="created approval remains pending and unexecuted in the queue",
        observed=response_snapshot(response),
    )


async def case_approval_cancel(context: CampaignContext) -> Outcome:
    approval = context.require("approval")
    response = await context.api.request(
        "PATCH",
        f"/api/approvals/{approval['id']}",
        json_body={"status": "cancelled"},
    )
    body = response.json() if response.status_code == 200 else {}
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "cancelled": body.get("status") == "cancelled",
            "not_executed": not body.get("result"),
        },
        expected="synthetic approval is cancelled without execution",
        observed=response_snapshot(response),
    )


async def case_tools_registry(context: CampaignContext) -> Outcome:
    response = await context.api.request("GET", "/api/tools")
    body = response.json() if response.status_code == 200 else []
    tools = {item.get("name"): item for item in body if isinstance(item, dict)}
    selected = {name: tools.get(name) for name in READ_ONLY_TOOL_NAMES}
    context.state["tools"] = tools
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "array": isinstance(body, list),
            "allowlisted_tools_present": all(isinstance(selected[name], dict) for name in selected),
            "allowlisted_tools_safe": all(
                isinstance(selected[name], dict) and selected[name].get("danger_level") == "safe"
                for name in selected
            ),
            "raw_host_tool_absent": "host.bridge.execute" not in tools,
        },
        expected={"safe_tools": READ_ONLY_TOOL_NAMES, "absent": "host.bridge.execute"},
        observed={"selected": selected, "tool_count": len(tools)},
    )


async def run_read_only_tool(
    context: CampaignContext,
    tool_name: str,
    arguments: dict[str, Any],
) -> Outcome:
    tools = context.require("tools")
    if tool_name not in READ_ONLY_TOOL_NAMES:
        raise SafetyViolation(f"tool not in harness read-only allowlist: {tool_name}")
    registered = tools.get(tool_name)
    if not isinstance(registered, dict) or registered.get("danger_level") != "safe":
        raise CaseSkip(f"tool is missing or not declared safe: {tool_name}")
    response = await context.api.request(
        "POST", f"/api/tools/{tool_name}/run", json_body={"arguments": arguments}
    )
    body = response.json() if response.status_code == 200 else {}
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "tool_name_exact": body.get("tool") == tool_name,
            "ok_true": body.get("ok") is True,
            "summary_nonempty": isinstance(body.get("summary"), str) and bool(body.get("summary")),
            "data_object": isinstance(body.get("data"), dict),
        },
        expected=f"safe read-only tool {tool_name} succeeds without approval",
        observed=response_snapshot(response),
    )


async def case_mission_create(context: CampaignContext) -> Outcome:
    goal = (
        f"[{context.marker}:mission] Acceptance-only mission. Do not call tools and do not "
        "perform any action. Produce a plan whose completion requires unavailable independent "
        "evidence; leave it incomplete rather than claiming success."
    )
    response = await context.api.request(
        "POST", "/api/missions", json_body={"goal": goal, "title": f"Synthetic {context.marker}"}
    )
    body = response.json() if response.status_code == 200 else {}
    mission_id = body.get("id") if isinstance(body, dict) else None
    if isinstance(mission_id, str):
        context.api.owned_mission_ids.add(mission_id)
        context.state["mission"] = body
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "id_present": isinstance(mission_id, str) and mission_id.startswith("mis_"),
            "goal_exact": body.get("goal") == goal,
            "tasks_nonempty": isinstance(body.get("tasks"), list) and bool(body.get("tasks")),
            "not_done": body.get("status") != "done",
        },
        expected="synthetic no-action mission is planned but not completed",
        observed=response_snapshot(response),
        notes=["The harness never calls execute-next or mission run endpoints."],
    )


async def case_mission_plan(context: CampaignContext) -> Outcome:
    mission = context.require("mission")
    response = await context.api.request("GET", f"/api/executive/plans/{mission['id']}")
    body = response.json() if response.status_code == 200 else {}
    planner = body.get("planner") if isinstance(body, dict) else None
    return Outcome(
        checks={
            "http_200": response.status_code == 200,
            "protocol": body.get("protocol") == "jarvis.executive.v1",
            "planner_object": isinstance(planner, dict),
            "steps_nonempty": isinstance(planner, dict)
            and isinstance(planner.get("steps"), list)
            and bool(planner.get("steps")),
            "no_goal_assertions": isinstance(planner, dict)
            and planner.get("goal_assertion_results") == [],
        },
        expected="executive plan exists without verified goal assertions",
        observed=response_snapshot(response),
    )


async def case_mission_bypass_rejected(context: CampaignContext) -> Outcome:
    mission = context.require("mission")
    tasks = mission.get("tasks", [])
    if not tasks or not isinstance(tasks[0], dict):
        raise CaseSkip("mission has no task suitable for the state-machine bypass probe")
    task_id = tasks[0].get("id")
    response = await context.api.request(
        "PATCH",
        f"/api/missions/{mission['id']}/tasks/{task_id}",
        json_body={"status": "done"},
    )
    body = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    return Outcome(
        checks={
            "http_409": response.status_code == 409,
            "state_machine_detail": "state machine" in str(body.get("detail", "")).casefold(),
        },
        expected="direct completion bypass is rejected with HTTP 409",
        observed=response_snapshot(response),
    )


async def case_mission_report_closed(context: CampaignContext) -> Outcome:
    mission = context.require("mission")
    response = await context.api.request("GET", f"/api/missions/{mission['id']}/report")
    return Outcome(
        checks={"http_404": response.status_code == 404},
        expected="no report exists without verified mission execution",
        observed=response_snapshot(response),
    )


async def case_invalid_chat(context: CampaignContext) -> Outcome:
    response = await context.api.request(
        "POST", "/api/chat", json_body={"message": "", "mode": "chat"}
    )
    body = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else {}
    )
    return Outcome(
        checks={
            "http_422": response.status_code == 422,
            "validation_detail": isinstance(body.get("detail"), list) and bool(body.get("detail")),
        },
        expected="empty chat message fails request validation",
        observed=response_snapshot(response),
    )


async def case_missing_conversation(context: CampaignContext) -> Outcome:
    missing_id = f"conv_missing_{context.campaign_id}"
    response = await context.api.request("GET", f"/api/conversations/{missing_id}/messages")
    return Outcome(
        checks={"http_404": response.status_code == 404},
        expected="unknown campaign conversation returns HTTP 404",
        observed=response_snapshot(response),
    )


async def case_missing_file(context: CampaignContext) -> Outcome:
    missing_id = f"file_missing_{context.campaign_id}"
    response = await context.api.request("GET", f"/api/files/{missing_id}")
    return Outcome(
        checks={"http_404": response.status_code == 404},
        expected="unknown campaign file returns HTTP 404",
        observed=response_snapshot(response),
    )


async def case_unknown_tool(context: CampaignContext) -> Outcome:
    # SafeApiClient intentionally blocks POSTing an arbitrary tool.  The negative
    # API surface is checked with a GET-inert path instead of weakening that guard.
    response = await context.api.request(
        "GET", f"/api/tools/not.registered.{context.campaign_id}/run"
    )
    return Outcome(
        checks={"http_405": response.status_code == 405},
        expected="an inert GET cannot execute an unknown tool and returns method-not-allowed",
        observed=response_snapshot(response),
        notes=["No arbitrary tool POST is possible through the harness safety guard."],
    )


def websocket_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws/events"


def websocket_token_protocol(token: str) -> str:
    encoded = base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii").rstrip("=")
    return f"jarvis.token.{encoded}"


async def case_websocket_event(context: CampaignContext) -> Outcome:
    chat = context.require("chat")["response"]
    message_id = chat["message_id"]
    protocols = [websocket_token_protocol(context.args.token)] if context.args.token else None
    url = websocket_url(context.args.base_url)
    async with websocket_connect(
        url,
        origin=context.args.ws_origin,
        subprotocols=protocols,
        open_timeout=context.args.ws_timeout,
        close_timeout=context.args.ws_timeout,
        ping_interval=None,
    ) as websocket:
        feedback_response = await context.api.request(
            "POST",
            f"/api/messages/{message_id}/feedback",
            json_body={"rating": "up", "comment": context.marker},
        )
        raw_event = await asyncio.wait_for(websocket.recv(), timeout=context.args.ws_timeout)
    event = json.loads(raw_event)
    feedback_body = feedback_response.json() if feedback_response.status_code == 200 else {}
    feedback_metadata = (
        feedback_body.get("metadata") if isinstance(feedback_body, dict) else None
    )
    return Outcome(
        checks={
            "feedback_http_200": feedback_response.status_code == 200,
            "feedback_saved": isinstance(feedback_metadata, dict)
            and isinstance(feedback_metadata.get("feedback"), dict)
            and feedback_metadata["feedback"].get("rating") == "up",
            "event_object": isinstance(event, dict),
            "agent_channel": event.get("channel") == "agent",
            "feedback_type": event.get("type") == "feedback",
            "message_id_matches": isinstance(event.get("payload"), dict)
            and event["payload"].get("message_id") == message_id,
        },
        expected="authenticated WebSocket receives the exact harmless feedback event",
        observed={"event": sanitize(event), "feedback": response_snapshot(feedback_response)},
    )


async def websocket_denied(
    context: CampaignContext,
    *,
    origin: str,
    token: str,
) -> tuple[bool, dict[str, Any]]:
    protocols = [websocket_token_protocol(token)] if token else None
    url = websocket_url(context.args.base_url)
    try:
        async with websocket_connect(
            url,
            origin=origin,
            subprotocols=protocols,
            open_timeout=context.args.ws_timeout,
            close_timeout=context.args.ws_timeout,
            ping_interval=None,
        ) as websocket:
            try:
                await asyncio.wait_for(websocket.recv(), timeout=min(context.args.ws_timeout, 3.0))
            except ConnectionClosed as exc:
                return exc.code == 1008, {"close_code": exc.code, "reason": exc.reason}
            except TimeoutError:
                return False, {"accepted": True, "event": "no close within timeout"}
            return False, {"accepted": True, "event": "application data received"}
    except InvalidStatus as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return status_code in {401, 403}, {"handshake_status": status_code}
    except ConnectionClosed as exc:
        return exc.code == 1008, {"close_code": exc.code, "reason": exc.reason}


async def case_websocket_bad_origin(context: CampaignContext) -> Outcome:
    denied, observed = await websocket_denied(
        context,
        origin="https://example.invalid",
        token=context.args.token,
    )
    return Outcome(
        checks={"connection_denied": denied},
        expected="hostile WebSocket Origin is denied",
        observed=observed,
    )


async def case_websocket_bad_token(context: CampaignContext) -> Outcome:
    response = await context.api.request("GET", "/api/runtime/security")
    body = response.json() if response.status_code == 200 else {}
    if not body.get("loopback_requires_token"):
        raise CaseSkip(
            "strict loopback token mode is disabled; invalid-token denial is not applicable"
        )
    denied, observed = await websocket_denied(
        context,
        origin=context.args.ws_origin,
        token=f"invalid-{secrets.token_hex(8)}",
    )
    return Outcome(
        checks={"connection_denied": denied},
        expected="invalid WebSocket token is denied in strict loopback mode",
        observed=observed,
    )


async def concurrent_gets(
    context: CampaignContext,
    path: str,
    count: int,
) -> tuple[list[int], list[float], list[str]]:
    async def one() -> tuple[int, float]:
        started = time.perf_counter()
        response = await context.api.request("GET", path)
        return response.status_code, (time.perf_counter() - started) * 1000

    results = await asyncio.gather(*(one() for _ in range(count)), return_exceptions=True)
    statuses: list[int] = []
    latencies: list[float] = []
    errors: list[str] = []
    for result in results:
        if isinstance(result, BaseException):
            errors.append(f"{type(result).__name__}: {result}")
        else:
            status, latency = result
            statuses.append(status)
            latencies.append(latency)
    return statuses, latencies, errors


async def latency_case(context: CampaignContext, path: str) -> Outcome:
    statuses, latencies, errors = await concurrent_gets(
        context, path, context.args.read_concurrency
    )
    p50 = statistics.median(latencies) if latencies else math.inf
    p95 = percentile(latencies, 0.95)
    return Outcome(
        checks={
            "all_requests_returned": len(statuses) == context.args.read_concurrency,
            "no_exceptions": not errors,
            "all_http_200": bool(statuses) and all(status == 200 for status in statuses),
            "p95_within_budget": p95 <= context.args.read_latency_budget_ms,
        },
        expected={
            "path": path,
            "concurrency": context.args.read_concurrency,
            "p95_max_ms": context.args.read_latency_budget_ms,
        },
        observed={
            "statuses": statuses,
            "latencies_ms": [round(item, 3) for item in latencies],
            "p50_ms": round(p50, 3),
            "p95_ms": round(p95, 3),
            "max_ms": round(max(latencies), 3) if latencies else None,
            "errors": errors,
        },
    )


async def case_cleanup_conversations(context: CampaignContext) -> Outcome:
    if context.args.keep_conversations:
        raise CaseSkip("conversation cleanup disabled by --keep-conversations")
    conversation_ids = sorted(context.api.owned_conversation_ids)
    if not conversation_ids:
        raise CaseSkip("no campaign-owned conversations were created")
    responses = await asyncio.gather(
        *(
            context.api.request("DELETE", f"/api/conversations/{conversation_id}")
            for conversation_id in conversation_ids
        )
    )
    statuses = [response.status_code for response in responses]
    return Outcome(
        checks={
            "all_deleted": len(statuses) == len(conversation_ids)
            and all(status == 200 for status in statuses),
            "owned_only": all(
                conversation_id in context.api.owned_conversation_ids
                for conversation_id in conversation_ids
            ),
        },
        expected="only campaign-owned conversations are deleted after evidence capture",
        observed={"conversation_ids": conversation_ids, "statuses": statuses},
    )


async def run_campaign(context: CampaignContext) -> None:
    await run_case(context, "F001", "core", case_health)
    await run_case(context, "F002", "core", case_status)
    await run_case(context, "F003", "core", case_environment_profile)
    await run_case(context, "F004", "model", case_model_profiles)
    await run_case(context, "F005", "model", case_model_catalog)
    await run_case(context, "F006", "model", case_dispatcher_read)

    await run_case(context, "F007", "cli", lambda ctx: run_cli_case(ctx, ("profiles",)))
    await run_case(context, "F008", "cli", lambda ctx: run_cli_case(ctx, ("status",)))
    await run_case(context, "F009", "cli", lambda ctx: run_cli_case(ctx, ("models",)))
    await run_case(context, "F010", "cli", lambda ctx: run_cli_case(ctx, ("llm-health",)))

    await run_case(context, "F011", "chat", case_chat)
    await run_case(context, "F012", "chat", case_chat_live_route)
    await run_case(context, "F013", "chat", case_chat_history)
    await run_case(context, "F014", "chat", case_conversation_list)
    await run_case(context, "F015", "trace", case_trace_message)
    await run_case(context, "F016", "trace", case_trace_conversation)

    await run_case(context, "F017", "stream", case_stream_capture)
    await run_case(context, "F018", "stream", case_stream_json)
    await run_case(context, "F019", "stream", case_stream_order)
    await run_case(context, "F020", "stream", case_stream_delta_done)
    await run_case(context, "F021", "stream", case_stream_history)

    await run_case(context, "F022", "concurrency", case_parallel_chats)
    await run_case(context, "F023", "concurrency", case_parallel_history)

    await run_case(context, "F024", "files", case_synthetic_fixtures)
    await run_case(context, "F025", "files", lambda ctx: upload_fixture(ctx, "acceptance.txt"))
    await run_case(context, "F026", "files", lambda ctx: upload_fixture(ctx, "acceptance.md"))
    await run_case(context, "F027", "files", case_file_metadata)
    await run_case(context, "F028", "files", case_file_download)
    await run_case(context, "F029", "files", case_directory_ingest)
    await run_case(context, "F030", "files", case_file_search)

    await run_case(context, "F031", "memory", case_memory_create)
    await run_case(context, "F032", "memory", case_memory_search)
    await run_case(context, "F033", "memory", case_memory_vault)

    await run_case(context, "F034", "approvals", case_approval_create)
    await run_case(context, "F035", "approvals", case_approval_list)
    await run_case(context, "F036", "approvals", case_approval_cancel)

    await run_case(context, "F037", "tools", case_tools_registry)
    await run_case(
        context,
        "F038",
        "tools",
        lambda ctx: run_read_only_tool(ctx, "runtime.status", {}),
    )
    await run_case(
        context,
        "F039",
        "tools",
        lambda ctx: run_read_only_tool(ctx, "environment.profile", {}),
    )
    await run_case(
        context,
        "F040",
        "tools",
        lambda ctx: run_read_only_tool(ctx, "memory.search", {"query": ctx.marker, "limit": 10}),
    )

    await run_case(context, "F041", "missions", case_mission_create)
    await run_case(context, "F042", "missions", case_mission_plan)
    await run_case(context, "F043", "missions", case_mission_bypass_rejected)
    await run_case(context, "F044", "missions", case_mission_report_closed)

    await run_case(context, "F045", "errors", case_invalid_chat)
    await run_case(context, "F046", "errors", case_missing_conversation)
    await run_case(context, "F047", "errors", case_missing_file)
    await run_case(context, "F048", "errors", case_unknown_tool)

    await run_case(context, "F049", "websocket", case_websocket_event)
    await run_case(context, "F050", "websocket", case_websocket_bad_origin)
    await run_case(
        context,
        "F051",
        "websocket",
        case_websocket_bad_token,
        required=False,
    )

    await run_case(
        context,
        "F052",
        "performance",
        lambda ctx: latency_case(ctx, "/health"),
    )
    await run_case(
        context,
        "F053",
        "performance",
        lambda ctx: latency_case(ctx, "/api/status"),
    )
    await run_case(
        context,
        "F054",
        "cleanup",
        case_cleanup_conversations,
        required=False,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a safe, evidence-producing functional campaign against local JARVIS."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.environ.get("JARVIS_API_TOKEN", ""))
    parser.add_argument("--ws-origin", default="http://localhost:3000")
    parser.add_argument("--campaign-prefix", default="jarvis-functional")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--ws-timeout", type=float, default=15.0)
    parser.add_argument("--cli-timeout", type=float, default=90.0)
    parser.add_argument("--parallel-chats", type=int, default=4)
    parser.add_argument("--read-concurrency", type=int, default=12)
    parser.add_argument("--read-latency-budget-ms", type=float, default=3000.0)
    parser.add_argument("--chat-latency-budget-ms", type=float, default=180000.0)
    parser.add_argument("--skip-cli", action="store_true")
    parser.add_argument("--keep-conversations", action="store_true")
    args = parser.parse_args(argv)
    args.base_url = args.base_url.rstrip("/")
    parsed = urlparse(args.base_url)
    if parsed.scheme not in {"http", "https"}:
        parser.error("--base-url must use http or https")
    host = (parsed.hostname or "").casefold()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("--base-url must target a loopback host")
    if not 2 <= args.parallel_chats <= 8:
        parser.error("--parallel-chats must be between 2 and 8")
    if not 2 <= args.read_concurrency <= 64:
        parser.error("--read-concurrency must be between 2 and 64")
    for name in (
        "timeout",
        "ws_timeout",
        "cli_timeout",
        "read_latency_budget_ms",
        "chat_latency_budget_ms",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


async def async_main(args: argparse.Namespace) -> int:
    campaign_id, namespace = build_campaign_identity(args.campaign_prefix)
    marker = f"{campaign_id}:{secrets.token_hex(4)}"
    recorder = EvidenceRecorder(campaign_id, namespace)
    repo_root = find_repo_root()
    started_at = utc_now()
    headers = {"Accept": "application/json"}
    if args.token:
        headers["X-Jarvis-Api-Token"] = args.token
    limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
    timeout = httpx.Timeout(args.timeout, connect=min(args.timeout, 15.0))
    metadata = {
        "schema": "jarvis.functional-campaign.v1",
        "started_at": started_at,
        "base_url": args.base_url,
        "token_present": bool(args.token),
        "repo_root": str(repo_root),
        "configuration": {
            "parallel_chats": args.parallel_chats,
            "read_concurrency": args.read_concurrency,
            "read_latency_budget_ms": args.read_latency_budget_ms,
            "chat_latency_budget_ms": args.chat_latency_budget_ms,
            "skip_cli": args.skip_cli,
            "keep_conversations": args.keep_conversations,
        },
        "safety": {
            "loopback_only": True,
            "approval_execute": False,
            "model_actions": False,
            "dispatcher_actions": False,
            "host_actions": False,
            "cleanup_endpoint": False,
            "arbitrary_tools": False,
        },
    }
    try:
        async with httpx.AsyncClient(
            base_url=args.base_url,
            headers=headers,
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            trust_env=False,
        ) as raw_client:
            context = CampaignContext(
                args=args,
                campaign_id=campaign_id,
                namespace=namespace,
                marker=marker,
                recorder=recorder,
                api=SafeApiClient(raw_client),
                repo_root=repo_root,
            )
            await run_campaign(context)
    except Exception as exc:  # noqa: BLE001 - preserve campaign-level failure evidence
        recorder.error(
            case_id="HARNESS",
            category="harness",
            required=True,
            started_at=started_at,
            started_perf=time.perf_counter(),
            error=f"{type(exc).__name__}: {exc}",
        )
    verdict, exit_code = recorder.finalize(metadata)
    print(
        compact_json(
            {
                "campaign_id": campaign_id,
                "namespace": namespace,
                "verdict": verdict,
                "jsonl": str(recorder.jsonl_path),
                "csv": str(recorder.csv_path),
                "manifest": str(recorder.manifest_path),
            }
        )
    )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
