from __future__ import annotations

import inspect
import ipaddress
import json
import math
import shutil
import socket
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from .config import JarvisSettings
from .diagnostics import run_diagnostics
from .dispatcher import DispatcherManager
from .host_bridge import HostBridgeClient, HostBridgeStatus
from .learning import LearningEngine
from .llm import LLMRouter
from .model_catalog import ModelCatalog
from .models import ToolInfo, ToolRunResponse
from .operations import OperationsManager, docker_container_allowed
from .storage import JarvisStorage
from .telemetry import TelemetryCollector

DangerLevel = Literal["safe", "review", "danger"]
ToolHandler = Callable[
    ["ToolContext", dict[str, Any]],
    ToolRunResponse | Awaitable[ToolRunResponse],
]


@dataclass(frozen=True)
class ToolContext:
    settings: JarvisSettings
    storage: JarvisStorage
    llm: LLMRouter
    mission_id: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    category: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    danger_level: DangerLevel = "safe"

    def info(self) -> ToolInfo:
        return ToolInfo(
            name=self.name,
            description=self.description,
            category=self.category,
            input_schema=self.input_schema,
            danger_level=self.danger_level,
        )


class ToolRegistry:
    def __init__(self, settings: JarvisSettings, storage: JarvisStorage, llm: LLMRouter) -> None:
        self.settings = settings
        self.storage = storage
        self.llm = llm
        self._tools: dict[str, ToolSpec] = {}
        self._register_defaults()

    def list(self) -> list[ToolInfo]:
        return [tool.info() for tool in sorted(self._tools.values(), key=lambda item: item.name)]

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def add(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    async def run(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        mission_id: str | None = None,
        task_id: str | None = None,
        allow_danger: bool = False,
    ) -> ToolRunResponse:
        spec = self.get(name)
        args = arguments or {}
        if spec is None:
            response = ToolRunResponse(
                tool=name,
                ok=False,
                summary=f"Tool {name!r} is not registered.",
                data={"available": [tool.name for tool in self.list()]},
            )
        elif spec.danger_level != "safe" and not allow_danger:
            response = ToolRunResponse(
                tool=name,
                ok=False,
                summary=(
                    f"Tool {name!r} requires approval or explicit danger override "
                    f"({spec.danger_level})."
                ),
                data={
                    "danger_level": spec.danger_level,
                    "approval_action": "tool.run",
                    "approval_payload": {"tool": name, "arguments": args},
                },
            )
        else:
            context = ToolContext(
                settings=self.settings,
                storage=self.storage,
                llm=self.llm,
                mission_id=mission_id,
                task_id=task_id,
            )
            try:
                raw = spec.handler(context, args)
                response = await raw if inspect.isawaitable(raw) else raw
            except Exception as exc:  # noqa: BLE001
                response = ToolRunResponse(
                    tool=name,
                    ok=False,
                    summary=f"Tool failed: {exc}",
                    data={"error": str(exc)},
                )

        self.storage.record_tool_run(
            tool=response.tool,
            ok=response.ok,
            summary=response.summary,
            arguments=args,
            data=response.data,
            mission_id=mission_id,
            task_id=task_id,
        )
        self.storage.add_event(
            kind="tool.run",
            title=response.summary,
            level="info" if response.ok else "warn",
            payload={"tool": response.tool, "mission_id": mission_id, "task_id": task_id},
        )
        return response

    def _register_defaults(self) -> None:
        self.add(
            ToolSpec(
                name="runtime.status",
                description="Return current runtime settings, counters and recent health snapshot.",
                category="runtime",
                input_schema={},
                handler=_runtime_status,
            )
        )
        self.add(
            ToolSpec(
                name="diagnostics.run",
                description=(
                    "Run local diagnostics for paths, SQLite, Git, Docker and LLM endpoint."
                ),
                category="runtime",
                input_schema={},
                handler=_diagnostics_run,
            )
        )
        self.add(
            ToolSpec(
                name="llm.health",
                description="Check the OpenAI-compatible LLM route and local model catalog.",
                category="runtime",
                input_schema={},
                handler=_llm_health,
            )
        )
        self.add(
            ToolSpec(
                name="models.list",
                description="List local model artifacts and active dispatcher configuration.",
                category="runtime",
                input_schema={},
                handler=_models_list,
            )
        )
        self.add(
            ToolSpec(
                name="telemetry.snapshot",
                description=(
                    "Collect CPU, memory, disk, GPU, Docker and performance policy telemetry."
                ),
                category="runtime",
                input_schema={"persist": "Store snapshot in SQLite"},
                handler=_telemetry_snapshot,
            )
        )
        self.add(
            ToolSpec(
                name="docker.ps",
                description="List Docker containers in a compact, read-only form.",
                category="docker",
                input_schema={"all": "Include stopped containers", "limit": "Maximum rows"},
                handler=_docker_ps,
            )
        )
        self.add(
            ToolSpec(
                name="docker.logs",
                description="Read a small tail from an allowed Jarvis Docker container.",
                category="docker",
                input_schema={"container": "Jarvis container name", "tail": "Maximum log lines"},
                handler=_docker_logs,
            )
        )
        self.add(
            ToolSpec(
                name="docker.policy",
                description="Return the current Jarvis Docker safety policy.",
                category="docker",
                input_schema={},
                handler=_docker_policy,
            )
        )
        self.add(
            ToolSpec(
                name="docker.containers",
                description="List Docker containers annotated by the Jarvis Docker policy.",
                category="docker",
                input_schema={},
                handler=_docker_containers,
            )
        )
        self.add(
            ToolSpec(
                name="dispatcher.status",
                description="Inspect the OpenAI-compatible model dispatcher and Docker container.",
                category="docker",
                input_schema={},
                handler=_dispatcher_status,
            )
        )
        self.add(
            ToolSpec(
                name="dispatcher.logs",
                description="Read a small Docker Compose log tail for the model dispatcher.",
                category="docker",
                input_schema={},
                handler=_dispatcher_logs,
            )
        )
        self.add(
            ToolSpec(
                name="dispatcher.start",
                description="Start the model dispatcher through Docker Compose after approval.",
                category="docker",
                input_schema={},
                handler=_dispatcher_start,
                danger_level="review",
            )
        )
        self.add(
            ToolSpec(
                name="dispatcher.stop",
                description="Stop the model dispatcher through Docker Compose after approval.",
                category="docker",
                input_schema={},
                handler=_dispatcher_stop,
                danger_level="review",
            )
        )
        self.add(
            ToolSpec(
                name="learning.tick",
                description=(
                    "Mine recent audit/tool/approval history into durable learning memories."
                ),
                category="learning",
                input_schema={"limit": "Maximum recent records to inspect"},
                handler=_learning_tick,
            )
        )
        self.add(
            ToolSpec(
                name="host.bridge.status",
                description="Inspect native host RPC bridge readiness, token and port state.",
                category="host",
                input_schema={},
                handler=_host_bridge_status,
            )
        )
        self.add(
            ToolSpec(
                name="host.bridge.execute",
                description=(
                    "Execute a token-authenticated PowerShell command through the "
                    "local host bridge."
                ),
                category="host",
                input_schema={
                    "command": "PowerShell command",
                    "cwd": "Optional working directory",
                    "timeout_sec": "1-120 second timeout",
                },
                handler=_host_bridge_execute,
                danger_level="danger",
            )
        )
        self.add(
            ToolSpec(
                name="browser.open",
                description="Open a validated HTTP(S) URL through the native host browser.",
                category="browser",
                input_schema={"url": "HTTP(S) URL to open"},
                handler=_browser_open,
                danger_level="review",
            )
        )
        self.add(
            ToolSpec(
                name="browser.policy",
                description="Return the current browser automation policy.",
                category="browser",
                input_schema={},
                handler=_browser_policy,
            )
        )
        self.add(
            ToolSpec(
                name="browser.open_many",
                description="Open multiple validated HTTP(S) URLs through the native host browser.",
                category="browser",
                input_schema={"urls": "List of HTTP(S) URLs to open"},
                handler=_browser_open_many,
                danger_level="review",
            )
        )
        self.add(
            ToolSpec(
                name="approval.request",
                description="Create a human approval gate for a risky or irreversible action.",
                category="safety",
                input_schema={
                    "title": "Short title",
                    "description": "Why approval is needed",
                    "requested_action": "Action identifier",
                    "risk": "review or danger",
                    "payload": "Structured action details",
                },
                handler=_approval_request,
                danger_level="review",
            )
        )
        self.add(
            ToolSpec(
                name="memory.search",
                description="Search long-term memory using FTS when available.",
                category="memory",
                input_schema={"query": "Text query", "limit": "Maximum results"},
                handler=_memory_search,
            )
        )
        self.add(
            ToolSpec(
                name="memory.save",
                description="Store a durable memory item with namespace, tags and importance.",
                category="memory",
                input_schema={
                    "content": "Memory content",
                    "namespace": "Memory namespace",
                    "tags": "List of tags",
                    "importance": "0.0 to 1.0",
                },
                handler=_memory_save,
            )
        )
        self.add(
            ToolSpec(
                name="files.list",
                description="List files that were uploaded or indexed into local runtime storage.",
                category="memory",
                input_schema={"limit": "Maximum results"},
                handler=_files_list,
            )
        )
        self.add(
            ToolSpec(
                name="files.search",
                description="Search indexed file chunks for local project context.",
                category="memory",
                input_schema={"query": "Text query", "limit": "Maximum chunk hits"},
                handler=_files_search,
            )
        )
        self.add(
            ToolSpec(
                name="web.fetch",
                description=(
                    "Fetch text from a public HTTP(S) URL with private-network SSRF guards."
                ),
                category="web",
                input_schema={"url": "Public http(s) URL", "max_chars": "Maximum text characters"},
                handler=_web_fetch,
            )
        )
        self.add(
            ToolSpec(
                name="mission.brief",
                description="Produce a deterministic execution brief for a mission task.",
                category="mission",
                input_schema={"goal": "Mission goal", "task_title": "Current task title"},
                handler=_mission_brief,
            )
        )
        self.add(
            ToolSpec(
                name="filesystem.list",
                description="List files below the repository or JARVIS_HOME.",
                category="filesystem",
                input_schema={"path": "Path to list", "limit": "Maximum entries"},
                handler=_filesystem_list,
            )
        )
        self.add(
            ToolSpec(
                name="filesystem.read_text",
                description="Read a small text file below the repository or JARVIS_HOME.",
                category="filesystem",
                input_schema={"path": "Path to read", "max_chars": "Maximum characters"},
                handler=_filesystem_read_text,
            )
        )
        self.add(
            ToolSpec(
                name="filesystem.write_text",
                description=(
                    "Write or append text below the repository or JARVIS_HOME after approval."
                ),
                category="filesystem",
                input_schema={
                    "path": "Path to write",
                    "content": "UTF-8 text content",
                    "mode": "overwrite or append",
                },
                handler=_filesystem_write_text,
                danger_level="review",
            )
        )


def _runtime_status(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    data = {
        "settings": ctx.settings.public_dict(),
        "counters": ctx.storage.counters(),
        "health": ctx.storage.latest_health(limit=20),
        "recent_events": ctx.storage.list_events(limit=10),
    }
    return ToolRunResponse(
        tool="runtime.status",
        ok=True,
        summary="Runtime status collected.",
        data=data,
    )


async def _diagnostics_run(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    diagnostics = await run_diagnostics(settings=ctx.settings, storage=ctx.storage, llm=ctx.llm)
    warn_count = sum(1 for check in diagnostics.checks if check.status == "warn")
    error_count = sum(1 for check in diagnostics.checks if check.status == "error")
    return ToolRunResponse(
        tool="diagnostics.run",
        ok=diagnostics.ok,
        summary=f"Diagnostics finished: {error_count} errors, {warn_count} warnings.",
        data=diagnostics.model_dump(),
    )


async def _llm_health(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    health = await ctx.llm.health()
    return ToolRunResponse(
        tool="llm.health",
        ok=bool(health.get("ok")),
        summary="LLM endpoint is responding."
        if health.get("ok")
        else "LLM endpoint is unavailable.",
        data=health,
    )


def _models_list(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    catalog = ModelCatalog(ctx.settings).response()
    return ToolRunResponse(
        tool="models.list",
        ok=bool(catalog["active_model"]["exists"]),
        summary=(
            f"Model catalog contains {len(catalog['models'])} model(s); "
            f"active profile is {catalog['active_profile']}."
        ),
        data=catalog,
    )


def _telemetry_snapshot(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    snapshot = TelemetryCollector(ctx.settings).snapshot()
    if bool(args.get("persist")):
        ctx.storage.record_telemetry(snapshot)
    gpu = snapshot.get("gpu", {})
    gpu_count = len(gpu.get("gpus") or []) if gpu.get("available") else 0
    return ToolRunResponse(
        tool="telemetry.snapshot",
        ok=True,
        summary=f"Telemetry collected: {gpu_count} GPU(s) visible.",
        data=snapshot,
    )


def _docker_ps(_ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    limit = _int_arg(args.get("limit"), default=30, minimum=1, maximum=200)
    include_all = bool(args.get("all", True))
    command = ["ps", "--format", "{{json .}}"]
    if include_all:
        command.insert(1, "-a")
    result = _run_docker(command, timeout=10)
    if not result["ok"]:
        return ToolRunResponse(
            tool="docker.ps",
            ok=False,
            summary=result["summary"],
            data=result,
        )
    containers = _parse_docker_ps(result["stdout"])[:limit]
    return ToolRunResponse(
        tool="docker.ps",
        ok=True,
        summary=f"Listed {len(containers)} Docker container(s).",
        data={
            "containers": containers,
            "limit": limit,
            "all": include_all,
            "command": result["command"],
        },
    )


def _docker_logs(_ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    container = str(args.get("container") or "jarvis-gpt-dispatcher").strip()
    policy = OperationsManager(settings=_ctx.settings, storage=_ctx.storage).docker_policy()
    if not _is_allowed_docker_container(container, policy):
        return ToolRunResponse(
            tool="docker.logs",
            ok=False,
            summary="Docker logs are restricted to Jarvis containers.",
            data={"container": container},
        )
    tail = _int_arg(
        args.get("tail"),
        default=80,
        minimum=1,
        maximum=int(policy["max_log_tail"]),
    )
    result = _run_docker(["logs", "--tail", str(tail), container], timeout=20)
    if not result["ok"]:
        return ToolRunResponse(
            tool="docker.logs",
            ok=False,
            summary=result["summary"],
            data=result,
        )
    return ToolRunResponse(
        tool="docker.logs",
        ok=True,
        summary=f"Read {tail} Docker log line(s) from {container}.",
        data={
            "container": container,
            "tail": tail,
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "command": result["command"],
        },
    )


def _docker_policy(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    policy = OperationsManager(settings=ctx.settings, storage=ctx.storage).docker_policy()
    return ToolRunResponse(
        tool="docker.policy",
        ok=True,
        summary="Docker policy loaded.",
        data={"policy": policy},
    )


def _docker_containers(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    data = OperationsManager(settings=ctx.settings, storage=ctx.storage).docker_containers()
    return ToolRunResponse(
        tool="docker.containers",
        ok=bool(data["ok"]),
        summary=str(data["summary"]),
        data=data,
    )


def _dispatcher_status(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    status = DispatcherManager(ctx.settings).status()
    return ToolRunResponse(
        tool="dispatcher.status",
        ok=bool(status.get("docker_available")),
        summary="Dispatcher status collected."
        if status.get("docker_available")
        else "Docker is not available in PATH.",
        data=status,
    )


def _dispatcher_logs(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    result = DispatcherManager(ctx.settings).run_compose("logs")
    return ToolRunResponse(
        tool="dispatcher.logs",
        ok=bool(result.get("ok")),
        summary=str(result.get("summary") or "Dispatcher logs collected."),
        data=result,
    )


def _dispatcher_start(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    result = DispatcherManager(ctx.settings).run_compose("up")
    return ToolRunResponse(
        tool="dispatcher.start",
        ok=bool(result.get("ok")),
        summary=str(result.get("summary") or "Dispatcher start requested."),
        data=result,
    )


def _dispatcher_stop(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    result = DispatcherManager(ctx.settings).run_compose("down")
    return ToolRunResponse(
        tool="dispatcher.stop",
        ok=bool(result.get("ok")),
        summary=str(result.get("summary") or "Dispatcher stop requested."),
        data=result,
    )


def _learning_tick(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    limit = _int_arg(args.get("limit"), default=20, minimum=5, maximum=100)
    result = LearningEngine(ctx.storage).tick(limit=limit)
    return ToolRunResponse(
        tool="learning.tick",
        ok=True,
        summary=f"Learning tick saved {result['lesson_count']} lesson(s).",
        data=result,
    )


def _host_bridge_status(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    status = HostBridgeStatus(ctx.settings).snapshot()
    return ToolRunResponse(
        tool="host.bridge.status",
        ok=bool(status["script_available"]),
        summary="Host bridge is listening."
        if status["port_open"]
        else "Host bridge script found but port is offline."
        if status["script_available"]
        else "Host bridge script is missing.",
        data=status,
    )


async def _host_bridge_execute(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    timeout_sec = _int_arg(args.get("timeout_sec"), default=30, minimum=1, maximum=120)
    result = await HostBridgeClient(ctx.settings).execute(
        command=str(args.get("command") or ""),
        cwd=str(args["cwd"]) if args.get("cwd") else None,
        timeout_sec=timeout_sec,
    )
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    return ToolRunResponse(
        tool="host.bridge.execute",
        ok=bool(result.get("ok")),
        summary=str(result.get("summary") or "Host bridge command finished."),
        data=data,
    )


async def _browser_open(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    policy = OperationsManager(settings=ctx.settings, storage=ctx.storage).browser_policy()
    try:
        url = _validate_browser_url(str(args.get("url") or ""), policy=policy)
    except ValueError as exc:
        return ToolRunResponse(tool="browser.open", ok=False, summary=str(exc))
    command = f"Start-Process -FilePath {_powershell_quote(url)}"
    result = await HostBridgeClient(ctx.settings).execute(command=command, timeout_sec=10)
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    return ToolRunResponse(
        tool="browser.open",
        ok=bool(result.get("ok")),
        summary=str(result.get("summary") or "Browser open requested."),
        data={"url": url, "bridge": data},
    )


def _browser_policy(ctx: ToolContext, _args: dict[str, Any]) -> ToolRunResponse:
    policy = OperationsManager(settings=ctx.settings, storage=ctx.storage).browser_policy()
    return ToolRunResponse(
        tool="browser.policy",
        ok=True,
        summary="Browser automation policy loaded.",
        data={"policy": policy},
    )


async def _browser_open_many(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    policy = OperationsManager(settings=ctx.settings, storage=ctx.storage).browser_policy()
    raw_urls = args.get("urls")
    if isinstance(raw_urls, str):
        raw_urls = [item.strip() for item in raw_urls.splitlines() if item.strip()]
    if not isinstance(raw_urls, list):
        return ToolRunResponse(
            tool="browser.open_many",
            ok=False,
            summary="A list of URLs is required.",
        )
    limit = int(policy["max_urls_per_action"])
    urls = []
    for item in raw_urls[:limit]:
        try:
            urls.append(_validate_browser_url(str(item), policy=policy))
        except ValueError as exc:
            return ToolRunResponse(
                tool="browser.open_many",
                ok=False,
                summary=str(exc),
                data={"url": str(item)},
            )
    if not urls:
        return ToolRunResponse(tool="browser.open_many", ok=False, summary="No URLs provided.")
    commands = "; ".join(
        f"Start-Process -FilePath {_powershell_quote(url)}" for url in urls
    )
    result = await HostBridgeClient(ctx.settings).execute(command=commands, timeout_sec=20)
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    return ToolRunResponse(
        tool="browser.open_many",
        ok=bool(result.get("ok")),
        summary=str(result.get("summary") or f"Requested {len(urls)} browser tab(s)."),
        data={"urls": urls, "bridge": data},
    )


def _approval_request(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    title = str(args.get("title") or "").strip()
    description = str(args.get("description") or "").strip()
    requested_action = str(args.get("requested_action") or "manual.review").strip()
    risk = str(args.get("risk") or "review").strip()
    if risk not in {"review", "danger"}:
        risk = "review"
    if not title or not description:
        return ToolRunResponse(
            tool="approval.request",
            ok=False,
            summary="Approval title and description are required.",
        )
    payload = args.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    approval = ctx.storage.create_approval(
        title=title,
        description=description,
        requested_action=requested_action,
        risk=risk,
        payload=payload,
    )
    return ToolRunResponse(
        tool="approval.request",
        ok=True,
        summary=f"Approval requested: {approval['title']}",
        data={"approval": approval},
    )


def _memory_search(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    query = str(args.get("query") or "")
    limit = _int_arg(args.get("limit"), default=10, minimum=1, maximum=50)
    items = ctx.storage.search_memory(query or None, limit=limit)
    return ToolRunResponse(
        tool="memory.search",
        ok=True,
        summary=f"Memory search returned {len(items)} item(s).",
        data={"items": items, "query": query, "limit": limit},
    )


def _memory_save(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    content = str(args.get("content") or "").strip()
    if not content:
        return ToolRunResponse(
            tool="memory.save",
            ok=False,
            summary="Memory content is required.",
        )
    namespace = str(args.get("namespace") or "core")[:80]
    tags = args.get("tags") or []
    if isinstance(tags, str):
        tags = [item.strip() for item in tags.split(",") if item.strip()]
    if not isinstance(tags, list):
        tags = []
    importance = _float_arg(args.get("importance"), default=0.5, minimum=0.0, maximum=1.0)
    item = ctx.storage.add_memory(
        content=content,
        namespace=namespace,
        tags=[str(tag)[:80] for tag in tags[:12]],
        importance=importance,
    )
    return ToolRunResponse(
        tool="memory.save",
        ok=True,
        summary="Memory item saved.",
        data={"item": item},
    )


def _files_list(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    limit = _int_arg(args.get("limit"), default=10, minimum=1, maximum=50)
    files = ctx.storage.list_files(limit=limit)
    return ToolRunResponse(
        tool="files.list",
        ok=True,
        summary=f"Listed {len(files)} file(s).",
        data={"files": files, "limit": limit},
    )


def _files_search(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolRunResponse(
            tool="files.search",
            ok=False,
            summary="File search query is required.",
        )
    limit = _int_arg(args.get("limit"), default=8, minimum=1, maximum=30)
    hits = ctx.storage.search_file_chunks(query, limit=limit)
    return ToolRunResponse(
        tool="files.search",
        ok=True,
        summary=f"File search returned {len(hits)} chunk(s).",
        data={"hits": hits, "query": query, "limit": limit},
    )


async def _web_fetch(_ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    raw_url = str(args.get("url") or "").strip()
    max_chars = _int_arg(args.get("max_chars"), default=6000, minimum=256, maximum=20000)
    try:
        current_url = _validate_public_http_url(raw_url)
    except ValueError as exc:
        return ToolRunResponse(tool="web.fetch", ok=False, summary=str(exc))

    redirects: list[dict[str, Any]] = []
    headers = {"User-Agent": "JARVIS-GPT/0.1"}
    timeout = httpx.Timeout(20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            for _ in range(6):
                async with client.stream(
                    "GET",
                    current_url,
                    headers=headers,
                    follow_redirects=False,
                ) as response:
                    location = response.headers.get("location")
                    if response.status_code in {301, 302, 303, 307, 308} and location:
                        next_url = str(httpx.URL(current_url).join(location))
                        redirects.append(
                            {
                                "from": current_url,
                                "to": next_url,
                                "status_code": response.status_code,
                            }
                        )
                        try:
                            current_url = _validate_public_http_url(next_url)
                        except ValueError as exc:
                            return ToolRunResponse(
                                tool="web.fetch",
                                ok=False,
                                summary=f"Blocked redirect target: {exc}",
                                data={"redirects": redirects},
                            )
                        continue

                    text, truncated = await _read_limited_response_text(response, max_chars)
                    return ToolRunResponse(
                        tool="web.fetch",
                        ok=True,
                        summary=f"Fetched URL with HTTP {response.status_code}.",
                        data={
                            "url": current_url,
                            "status_code": response.status_code,
                            "content_type": response.headers.get("content-type"),
                            "text": text,
                            "truncated": truncated,
                            "redirects": redirects,
                        },
                    )
    except httpx.HTTPError as exc:
        return ToolRunResponse(
            tool="web.fetch",
            ok=False,
            summary=f"HTTP request failed: {exc}",
            data={"url": current_url, "redirects": redirects},
        )

    return ToolRunResponse(
        tool="web.fetch",
        ok=False,
        summary="Too many redirects.",
        data={"url": current_url, "redirects": redirects},
    )


def _mission_brief(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    goal = str(args.get("goal") or "").strip()
    task_title = str(args.get("task_title") or "").strip()
    memory = ctx.storage.search_memory(f"{goal} {task_title}".strip(), limit=5)
    file_hits = ctx.storage.search_file_chunks(f"{goal} {task_title}".strip(), limit=5)
    brief = {
        "goal": goal,
        "task_title": task_title,
        "recommended_action": _recommended_action(task_title),
        "acceptance_check": (
            "Record the observable result, mark the task done or blocked, "
            "then refresh mission progress."
        ),
        "memory_hits": memory,
        "file_hits": file_hits,
    }
    return ToolRunResponse(
        tool="mission.brief",
        ok=True,
        summary="Mission step brief prepared.",
        data=brief,
    )


def _filesystem_list(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    try:
        path = _resolve_allowed_path(ctx.settings, str(args.get("path") or "."))
    except ValueError as exc:
        return ToolRunResponse(tool="filesystem.list", ok=False, summary=str(exc))
    limit = _int_arg(args.get("limit"), default=100, minimum=1, maximum=500)
    if not path.exists():
        return ToolRunResponse(
            tool="filesystem.list",
            ok=False,
            summary=f"Path does not exist: {path}",
            data={"path": str(path)},
        )
    if not path.is_dir():
        return ToolRunResponse(
            tool="filesystem.list",
            ok=False,
            summary=f"Path is not a directory: {path}",
            data={"path": str(path)},
        )
    entries = []
    children = sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    for child in children[:limit]:
        entries.append(
            {
                "name": child.name,
                "path": str(child),
                "type": "directory" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else None,
            }
        )
    return ToolRunResponse(
        tool="filesystem.list",
        ok=True,
        summary=f"Listed {len(entries)} item(s).",
        data={"path": str(path), "entries": entries},
    )


def _filesystem_read_text(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    try:
        path = _resolve_allowed_path(ctx.settings, str(args.get("path") or ""))
    except ValueError as exc:
        return ToolRunResponse(tool="filesystem.read_text", ok=False, summary=str(exc))
    max_chars = _int_arg(args.get("max_chars"), default=8000, minimum=1, maximum=50000)
    if not path.exists() or not path.is_file():
        return ToolRunResponse(
            tool="filesystem.read_text",
            ok=False,
            summary=f"File does not exist: {path}",
            data={"path": str(path)},
        )
    content = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    return ToolRunResponse(
        tool="filesystem.read_text",
        ok=True,
        summary=f"Read {len(content)} character(s).",
        data={"path": str(path), "content": content, "truncated": path.stat().st_size > max_chars},
    )


def _filesystem_write_text(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    try:
        path = _resolve_allowed_path(ctx.settings, str(args.get("path") or ""))
    except ValueError as exc:
        return ToolRunResponse(tool="filesystem.write_text", ok=False, summary=str(exc))
    content = str(args.get("content") or "")
    if not content:
        return ToolRunResponse(
            tool="filesystem.write_text",
            ok=False,
            summary="Content is required.",
            data={"path": str(path)},
        )
    if len(content) > 200_000:
        return ToolRunResponse(
            tool="filesystem.write_text",
            ok=False,
            summary="Content is too large for the sandboxed write tool.",
            data={"path": str(path), "chars": len(content), "max_chars": 200_000},
        )
    mode = str(args.get("mode") or "overwrite").strip().lower()
    if mode not in {"overwrite", "append"}:
        return ToolRunResponse(
            tool="filesystem.write_text",
            ok=False,
            summary="Mode must be 'overwrite' or 'append'.",
            data={"path": str(path), "mode": mode},
        )
    if path.exists() and path.is_dir():
        return ToolRunResponse(
            tool="filesystem.write_text",
            ok=False,
            summary=f"Path is a directory: {path}",
            data={"path": str(path)},
        )

    previous_size = path.stat().st_size if path.exists() else 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append":
        with path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(content)
    else:
        path.write_text(content, encoding="utf-8", newline="")
    return ToolRunResponse(
        tool="filesystem.write_text",
        ok=True,
        summary=f"Wrote {len(content)} character(s) to sandboxed path.",
        data={
            "path": str(path),
            "mode": mode,
            "chars": len(content),
            "previous_size": previous_size,
            "size": path.stat().st_size,
        },
    )


def _resolve_allowed_path(settings: JarvisSettings, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("Path is required.")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve(strict=False)
    roots = [Path.cwd().resolve(strict=False), settings.home.resolve(strict=False)]
    for root in roots:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    roots_text = ", ".join(str(root) for root in roots)
    raise ValueError(f"Path is outside allowed roots: {roots_text}")


def _run_docker(args: list[str], *, timeout: int) -> dict[str, Any]:
    docker = shutil.which("docker")
    if docker is None:
        return {
            "ok": False,
            "summary": "Docker is not available in PATH.",
            "stdout": "",
            "stderr": "docker not found",
            "command": ["docker", *args],
            "returncode": None,
        }
    command = [docker, *args]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "summary": f"Docker command failed: {exc}",
            "stdout": "",
            "stderr": str(exc),
            "command": command,
            "returncode": None,
        }
    return {
        "ok": result.returncode == 0,
        "summary": f"Docker exited with {result.returncode}.",
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "command": command,
        "returncode": result.returncode,
    }


def _parse_docker_ps(stdout: str) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(
            {
                "id": item.get("ID"),
                "name": item.get("Names"),
                "image": item.get("Image"),
                "status": item.get("Status"),
                "state": item.get("State"),
                "ports": item.get("Ports"),
                "created_at": item.get("CreatedAt"),
            }
        )
    return containers


def _is_allowed_docker_container(container: str, policy: dict[str, Any] | None = None) -> bool:
    if policy is not None:
        return docker_container_allowed(policy, container)
    lowered = container.lower()
    return bool(container) and (
        lowered == "jarvis-gpt-dispatcher"
        or lowered.startswith("jarvis-")
        or lowered.startswith("jarvis_")
        or "jarvis-gpt" in lowered
    )


def _validate_browser_url(raw_url: str, policy: dict[str, Any] | None = None) -> str:
    raw_url = raw_url.strip()
    if not raw_url:
        raise ValueError("URL is required.")
    if any(char in raw_url for char in ("\r", "\n", "\0")):
        raise ValueError("URL contains invalid control characters.")
    if len(raw_url) > 2048:
        raise ValueError("URL is too long.")
    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only http and https URLs can be opened.")
    blocked_schemes = set(policy.get("blocked_schemes", [])) if policy else set()
    if parsed.scheme.lower() in blocked_schemes:
        raise ValueError(f"URL scheme is blocked by browser policy: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("URL host is required.")
    if policy:
        if policy.get("mode") == "locked":
            raise ValueError("Browser automation policy is locked.")
        host = parsed.hostname.lower()
        allowed_hosts = {str(item).lower() for item in policy.get("allowed_hosts", [])}
        is_local = host in {"localhost", "127.0.0.1", "::1"}
        if policy.get("mode") == "local-safe" and host not in allowed_hosts:
            raise ValueError("Browser policy only allows configured local hosts.")
        if is_local and not policy.get("allow_localhost", True):
            raise ValueError("Browser policy blocks localhost URLs.")
    return parsed.geturl()


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _validate_public_http_url(raw_url: str) -> str:
    if not raw_url:
        raise ValueError("URL is required.")
    parsed = urlparse(raw_url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Only http and https URLs are supported.")
    if not parsed.hostname:
        raise ValueError("URL host is required.")
    if _hostname_is_private(parsed.hostname):
        raise ValueError("URL host must resolve only to public addresses.")
    return parsed.geturl()


def _hostname_is_private(hostname: str) -> bool:
    try:
        resolved = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"Could not resolve URL host: {hostname}") from exc
    if not resolved:
        raise ValueError(f"Could not resolve URL host: {hostname}")

    for item in resolved:
        address = item[4][0]
        ip = ipaddress.ip_address(address)
        if (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


async def _read_limited_response_text(
    response: httpx.Response,
    max_chars: int,
) -> tuple[str, bool]:
    byte_limit = max(4096, min(1_000_000, max_chars * 4))
    content = bytearray()
    truncated = False
    async for chunk in response.aiter_bytes():
        remaining = byte_limit - len(content)
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            content.extend(chunk[:remaining])
            truncated = True
            break
        content.extend(chunk)

    encoding = _charset_from_content_type(response.headers.get("content-type")) or "utf-8"
    text = bytes(content).decode(encoding, errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return text, truncated


def _charset_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    for item in content_type.split(";"):
        key, separator, value = item.strip().partition("=")
        if separator and key.lower() == "charset" and value:
            return value.strip("\"' ")
    return None


def _recommended_action(task_title: str) -> str:
    lowered = task_title.lower()
    if any(word in lowered for word in ("контекст", "context", "код", "окружение")):
        return "Inspect repository files, runtime paths and diagnostics before changing behavior."
    if any(word in lowered for word in ("реализ", "implement", "срез", "runtime")):
        return "Make a narrow code change, run tests, then record the exact verification result."
    if any(word in lowered for word in ("провер", "verify", "health", "диагност")):
        return "Run diagnostics and tests, then attach the health summary to task notes."
    return "Break the task into a concrete action, execute the smallest safe step, then verify it."


def _int_arg(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _float_arg(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    if math.isnan(parsed):
        parsed = default
    return max(minimum, min(maximum, parsed))
