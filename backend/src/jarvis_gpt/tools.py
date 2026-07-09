from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import math
import re
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from html import unescape
from pathlib import Path, PureWindowsPath
from typing import Any, Literal
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import SOCKET_OPTION, AsyncNetworkBackend, AsyncNetworkStream

from .browser_cdp import (
    DEFAULT_CHROME_DEBUG_URL,
    BrowserCdpError,
    chrome_debugger_status,
    normalize_debug_url,
    read_chrome_page,
)
from .config import JarvisSettings
from .diagnostics import run_diagnostics
from .dispatcher import DispatcherManager
from .host_bridge import HostBridgeClient, HostBridgeStatus
from .learning import LearningEngine
from .llm import LLMRouter
from .model_catalog import ModelCatalog
from .models import ToolInfo, ToolRunResponse
from .operations import OperationsManager, docker_container_allowed
from .persona import INSIGHT_FIELDS, PersonaManager, load_persona
from .storage import JarvisStorage
from .telemetry import TelemetryCollector

DangerLevel = Literal["safe", "review", "danger"]
ToolHandler = Callable[
    ["ToolContext", dict[str, Any]],
    ToolRunResponse | Awaitable[ToolRunResponse],
]

WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
WEB_HEADERS = {
    "User-Agent": WEB_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


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
                name="windows.native",
                description=(
                    "Perform native Windows actions through WMI/CIM, WinAPI window focus, "
                    "process launch and GUI text/key input via the local host bridge."
                ),
                category="host",
                input_schema={
                    "action": (
                        "capabilities, process.start, app.open_and_type, window.focus, "
                        "screen.capture, window.list, keyboard.send or wmi.query"
                    ),
                    "payload": "Structured action payload",
                    "timeout_sec": "1-120 second timeout",
                },
                handler=_windows_native,
                danger_level="danger",
            )
        )
        self.add(
            ToolSpec(
                name="system.inspect",
                description=(
                    "Read-only inspection of the operator's Windows machine through WMI/CIM "
                    "(plus visible-window list and screen capture to Jarvis cache). YOU choose "
                    "the Win32_* class and properties from your own knowledge, e.g. "
                    "Win32_OperatingSystem, Win32_Processor, Win32_PhysicalMemory, "
                    "Win32_LogicalDisk, Win32_Battery, Win32_VideoController, Win32_Service, "
                    "Win32_Process, Win32_StartupCommand, Win32_Printer, "
                    "Win32_NetworkAdapterConfiguration, Win32_PnPEntity. Use it for everyday "
                    "questions about hardware, OS, disks, memory, battery, services, startup, "
                    "printers or network. Non-mutating and safe to run autonomously; runs through "
                    "the local host bridge and degrades honestly if the bridge is offline."
                ),
                category="host",
                input_schema={
                    "action": "wmi.query (default), window.list, screen.capture, or capabilities",
                    "payload": (
                        "wmi.query: {class_name, properties[], filter?, limit?}; "
                        "window.list: {limit}; screen.capture: {path?, limit?, ocr?}"
                    ),
                    "timeout_sec": "1-120 second timeout",
                },
                handler=_system_inspect,
            )
        )
        self.add(
            ToolSpec(
                name="browser.open",
                description="Open a validated HTTP(S) URL through the native host browser.",
                category="browser",
                input_schema={"url": "HTTP(S) URL to open"},
                handler=_browser_open,
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
                name="browser.chrome.status",
                description="Check the local Chrome DevTools endpoint used for browser reading.",
                category="browser",
                input_schema={"debug_url": "Local Chrome DevTools URL, default 127.0.0.1:9222"},
                handler=_browser_chrome_status,
            )
        )
        self.add(
            ToolSpec(
                name="browser.chrome.launch",
                description=(
                    "Launch Chrome with a dedicated Jarvis profile and local DevTools endpoint."
                ),
                category="browser",
                input_schema={
                    "debug_port": "Local DevTools port, default 9222",
                    "profile_dir": "Optional profile directory under allowed roots",
                    "start_url": "Optional HTTP(S) URL to open",
                },
                handler=_browser_chrome_launch,
                danger_level="review",
            )
        )
        self.add(
            ToolSpec(
                name="browser.read",
                description=(
                    "Read visible text from an HTTP(S) page through a local Chrome DevTools "
                    "session, preserving browser-side cookies without exporting them."
                ),
                category="browser",
                input_schema={
                    "url": "HTTP(S) URL to open and read",
                    "max_chars": "Maximum text characters",
                    "wait_ms": "Milliseconds to wait for page load",
                    "debug_url": "Local Chrome DevTools URL, default 127.0.0.1:9222",
                },
                handler=_browser_read,
                danger_level="review",
            )
        )
        self.add(
            ToolSpec(
                name="browser.open_many",
                description="Open multiple validated HTTP(S) URLs through the native host browser.",
                category="browser",
                input_schema={"urls": "List of HTTP(S) URLs to open"},
                handler=_browser_open_many,
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
                name="persona.get",
                description=(
                    "Read the durable operator persona: role, home location, languages, "
                    "tech stack, interests, current focus, standing instructions, glossary."
                ),
                category="operator",
                input_schema={},
                handler=_persona_get,
            )
        )
        self.add(
            ToolSpec(
                name="persona.insight",
                description=(
                    "Append ONE durable fact the operator just revealed about themselves "
                    "to the persona. Use sparingly for stable facts only (new tech stack, "
                    "interest, current focus, standing 'always/never' rule), never for "
                    "transient or speculative details. Deduplicated, capped and audited."
                ),
                category="operator",
                input_schema={
                    "field": (
                        "One of: languages, expertise, tech_stack, interests, "
                        "current_focus, standing_instructions"
                    ),
                    "value": "The single learned fact, short and durable",
                },
                handler=_persona_insight,
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
                name="web.search",
                description=(
                    "Search the public web and return result titles, URLs and snippets."
                ),
                category="web",
                input_schema={"query": "Search query", "limit": "Maximum results"},
                handler=_web_search,
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
                name="web.render",
                description=(
                    "Render a public HTTP(S) page in an isolated headless Chrome/Edge process "
                    "and return the visible DOM text. This is for JS-heavy pages and never opens "
                    "the operator's real browser window."
                ),
                category="web",
                input_schema={
                    "url": "Public http(s) URL",
                    "max_chars": "Maximum visible text characters",
                    "wait_ms": "Virtual time budget for page scripts",
                },
                handler=_web_render,
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


async def _learning_tick(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    limit = _int_arg(args.get("limit"), default=20, minimum=5, maximum=100)
    result = await LearningEngine(ctx.storage, llm=ctx.llm).tick_async(limit=limit)
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


# Read-only native actions that never change desktop/host state, so the agentic
# loop may drive them without approval. wmi.query is a WMI SELECT, window.list
# enumerates visible windows, screen.capture writes a PNG into Jarvis cache, and
# capabilities reports what the bridge supports.
SAFE_INSPECT_ACTIONS = frozenset({"capabilities", "screen.capture", "window.list", "wmi.query"})


async def _run_native_bridge_command(
    ctx: ToolContext,
    action: str,
    payload: dict[str, Any],
    timeout_sec: int,
) -> tuple[dict[str, Any], bool, str, Any]:
    """Build, run and parse one native host-bridge command. May raise ValueError."""

    command = _windows_native_command(action, payload)
    result = await HostBridgeClient(ctx.settings).execute(
        command=command,
        timeout_sec=timeout_sec,
    )
    bridge_data = result.get("data") if isinstance(result.get("data"), dict) else result
    stdout = bridge_data.get("stdout", "") if isinstance(bridge_data, dict) else ""
    native = _parse_native_stdout(stdout)
    ok = bool(result.get("ok")) and bool(native.get("ok", True))
    summary = str(
        native.get("summary")
        or result.get("summary")
        or f"Native action {action} finished."
    )
    return native, ok, summary, bridge_data


async def _windows_native(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    action = str(args.get("action") or "capabilities").strip().lower()
    payload = args.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    timeout_sec = _int_arg(args.get("timeout_sec"), default=30, minimum=1, maximum=120)
    try:
        native, ok, summary, bridge_data = await _run_native_bridge_command(
            ctx, action, payload, timeout_sec
        )
    except ValueError as exc:
        return ToolRunResponse(tool="windows.native", ok=False, summary=str(exc))
    return ToolRunResponse(
        tool="windows.native",
        ok=ok,
        summary=summary,
        data={
            "action": action,
            "payload": _redact_native_payload(payload),
            "native": native,
            "bridge": bridge_data,
        },
    )


async def _system_inspect(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    """Read-only inspection of the operator's machine (WMI/CIM, visible windows).

    This is the safe, autonomous counterpart of windows.native: the model picks
    the WMI class and properties from its own knowledge and reads system/hardware
    state without an approval gate, because a WMI SELECT changes nothing. It may
    also list windows or capture the current screen to Jarvis cache. Actions
    that touch the desktop stay on the approval-gated windows.native tool.
    """

    action = str(args.get("action") or "wmi.query").strip().lower()
    if action not in SAFE_INSPECT_ACTIONS:
        allowed = ", ".join(sorted(SAFE_INSPECT_ACTIONS))
        return ToolRunResponse(
            tool="system.inspect",
            ok=False,
            summary=(
                f"system.inspect is read-only; '{action}' is not allowed here "
                f"(use one of: {allowed}). Desktop-changing actions such as "
                "process.start, app.open_and_type, keyboard.send or window.focus "
                "must go through the approval-gated windows.native tool."
            ),
        )
    payload = args.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    if action == "screen.capture" and not payload.get("path"):
        screen_dir = ctx.settings.cache_dir / "screens"
        payload = {
            **payload,
            "path": str(screen_dir / f"screen-{_timestamp_slug()}.png"),
        }
    if action == "screen.capture":
        payload = {**payload, "ocr": bool(payload.get("ocr", True))}
    timeout_sec = _int_arg(args.get("timeout_sec"), default=30, minimum=1, maximum=120)
    try:
        native, ok, summary, bridge_data = await _run_native_bridge_command(
            ctx, action, payload, timeout_sec
        )
    except ValueError as exc:
        return ToolRunResponse(tool="system.inspect", ok=False, summary=str(exc))
    return ToolRunResponse(
        tool="system.inspect",
        ok=ok,
        summary=summary,
        data={"action": action, "native": native, "bridge": bridge_data},
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


async def _browser_chrome_status(_ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    try:
        debug_url = normalize_debug_url(str(args.get("debug_url") or DEFAULT_CHROME_DEBUG_URL))
        status = await chrome_debugger_status(debug_url)
    except BrowserCdpError as exc:
        return ToolRunResponse(tool="browser.chrome.status", ok=False, summary=str(exc))
    return ToolRunResponse(
        tool="browser.chrome.status",
        ok=bool(status.get("ok")),
        summary=str(status.get("summary") or "Chrome DevTools status collected."),
        data=status,
    )


async def _browser_chrome_launch(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    policy = OperationsManager(settings=ctx.settings, storage=ctx.storage).browser_policy()
    debug_port = _int_arg(args.get("debug_port"), default=9222, minimum=1024, maximum=65535)
    debug_url = f"http://127.0.0.1:{debug_port}"
    raw_start_url = str(args.get("start_url") or "").strip()
    if raw_start_url:
        try:
            start_url = _validate_browser_url(raw_start_url, policy=policy)
        except ValueError as exc:
            return ToolRunResponse(tool="browser.chrome.launch", ok=False, summary=str(exc))
    else:
        start_url = "about:blank"

    raw_profile_dir = str(args.get("profile_dir") or "").strip()
    try:
        profile_dir = (
            _resolve_allowed_path(ctx.settings, raw_profile_dir)
            if raw_profile_dir
            else ctx.settings.cache_dir / "chrome-profile"
        )
    except ValueError as exc:
        return ToolRunResponse(tool="browser.chrome.launch", ok=False, summary=str(exc))

    args_list = [
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ]
    ps_args = ", ".join(_powershell_quote(item) for item in args_list)
    command = (
        "$candidates = @("
        "\"$env:ProgramFiles\\Google\\Chrome\\Application\\chrome.exe\", "
        "\"${env:ProgramFiles(x86)}\\Google\\Chrome\\Application\\chrome.exe\", "
        "\"$env:LOCALAPPDATA\\Google\\Chrome\\Application\\chrome.exe\""
        "); "
        "$chrome = $candidates | Where-Object { $_ -and (Test-Path $_) } | "
        "Select-Object -First 1; "
        "if (-not $chrome) { throw 'Chrome executable was not found.' }; "
        f"New-Item -ItemType Directory -Force -Path {_powershell_quote(str(profile_dir))} "
        "| Out-Null; "
        f"Start-Process -FilePath $chrome -ArgumentList @({ps_args})"
    )
    result = await HostBridgeClient(ctx.settings).execute(command=command, timeout_sec=15)
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    return ToolRunResponse(
        tool="browser.chrome.launch",
        ok=bool(result.get("ok")),
        summary=str(result.get("summary") or "Chrome launch requested."),
        data={
            "debug_url": debug_url,
            "profile_dir": str(profile_dir),
            "start_url": start_url,
            "bridge": data,
        },
    )


async def _browser_read(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    policy = OperationsManager(settings=ctx.settings, storage=ctx.storage).browser_policy()
    try:
        url = _validate_browser_url(str(args.get("url") or ""), policy=policy)
        debug_url = normalize_debug_url(str(args.get("debug_url") or DEFAULT_CHROME_DEBUG_URL))
    except (BrowserCdpError, ValueError) as exc:
        return ToolRunResponse(tool="browser.read", ok=False, summary=str(exc))

    max_chars = _int_arg(args.get("max_chars"), default=6000, minimum=256, maximum=30000)
    wait_ms = _int_arg(args.get("wait_ms"), default=5000, minimum=1000, maximum=30000)
    try:
        snapshot = await read_chrome_page(
            url=url,
            max_chars=max_chars,
            wait_ms=wait_ms,
            debug_url=debug_url,
        )
    except BrowserCdpError as exc:
        return ToolRunResponse(
            tool="browser.read",
            ok=False,
            summary=(
                f"{exc}. Start Chrome with browser.chrome.launch or manually expose "
                "a local DevTools endpoint."
            ),
            data={"url": url, "debug_url": debug_url},
        )

    ok = bool(snapshot.text.strip()) and not snapshot.needs_human_verification
    if snapshot.needs_human_verification:
        summary = "Page appears to require human verification; complete it in Chrome and retry."
    elif snapshot.text.strip():
        summary = f"Read browser page: {snapshot.title or snapshot.url}"
    else:
        summary = "Browser page loaded but no visible text was found."
    return ToolRunResponse(
        tool="browser.read",
        ok=ok,
        summary=summary,
        data={
            "url": snapshot.url,
            "requested_url": url,
            "title": snapshot.title,
            "ready_state": snapshot.ready_state,
            "text": snapshot.text,
            "truncated": snapshot.truncated,
            "needs_human_verification": snapshot.needs_human_verification,
            "debug_url": debug_url,
        },
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


def _persona_get(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    persona = load_persona(ctx.storage)
    return ToolRunResponse(
        tool="persona.get",
        ok=True,
        summary="Operator persona loaded.",
        data={"persona": persona, "insight_fields": sorted(INSIGHT_FIELDS)},
    )


def _persona_insight(ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    field = str(args.get("field") or "").strip()
    value = str(args.get("value") or "").strip()
    if field not in INSIGHT_FIELDS:
        return ToolRunResponse(
            tool="persona.insight",
            ok=False,
            summary=(
                f"Persona field {field!r} does not accept insights. "
                f"Allowed: {', '.join(sorted(INSIGHT_FIELDS))}."
            ),
        )
    if not value:
        return ToolRunResponse(
            tool="persona.insight",
            ok=False,
            summary="Persona insight value is required.",
        )
    manager = PersonaManager(settings=ctx.settings, storage=ctx.storage)
    before = list(manager.persona().get(field) or [])
    persona = manager.add_insight(field, value, actor="agent")
    after = list(persona.get(field) or [])
    learned = after != before
    summary = (
        f"Persona {field} learned: {value[:120]}"
        if learned
        else f"Persona {field} already knows: {value[:120]}"
    )
    return ToolRunResponse(
        tool="persona.insight",
        ok=True,
        summary=summary,
        data={"field": field, "value": value, "learned": learned, "items": after},
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


async def _web_search(_ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    query = " ".join(str(args.get("query") or "").split())
    limit = _int_arg(args.get("limit"), default=6, minimum=1, maximum=12)
    if not query:
        return ToolRunResponse(tool="web.search", ok=False, summary="Search query is required.")
    if len(query) > 300:
        query = query[:300].rstrip()

    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    headers = WEB_HEADERS
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            follow_redirects=True,
            trust_env=False,
            transport=_PublicOnlyAsyncHTTPTransport(),
        ) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return ToolRunResponse(
            tool="web.search",
            ok=False,
            summary=f"Search request failed: {exc}",
            data={"query": query, "url": url},
        )

    results = _parse_duckduckgo_results(_decode_response_text(response), limit=limit)
    return ToolRunResponse(
        tool="web.search",
        ok=True,
        summary=f"Web search returned {len(results)} result(s).",
        data={"query": query, "results": results, "source": "duckduckgo_html"},
    )


async def _web_fetch(_ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    raw_url = str(args.get("url") or "").strip()
    max_chars = _int_arg(args.get("max_chars"), default=6000, minimum=256, maximum=20000)
    try:
        current_url = _validate_public_http_url(raw_url)
    except ValueError as exc:
        return ToolRunResponse(tool="web.fetch", ok=False, summary=str(exc))

    redirects: list[dict[str, Any]] = []
    headers = WEB_HEADERS
    timeout = httpx.Timeout(20.0)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            transport=_PublicOnlyAsyncHTTPTransport(),
        ) as client:
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
                    blocked = _web_response_blocked(response.status_code, text)
                    summary = f"Fetched URL with HTTP {response.status_code}."
                    if blocked:
                        summary = (
                            f"Fetched URL with HTTP {response.status_code}; "
                            "page appears blocked."
                        )
                    return ToolRunResponse(
                        tool="web.fetch",
                        ok=response.status_code < 400 and not blocked,
                        summary=summary,
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


async def _web_render(_ctx: ToolContext, args: dict[str, Any]) -> ToolRunResponse:
    raw_url = str(args.get("url") or "").strip()
    max_chars = _int_arg(args.get("max_chars"), default=8000, minimum=512, maximum=30000)
    wait_ms = _int_arg(args.get("wait_ms"), default=2500, minimum=250, maximum=10000)
    timeout_sec = _int_arg(args.get("timeout_sec"), default=25, minimum=5, maximum=60)
    try:
        url = _validate_public_http_url(raw_url)
        parsed = urlparse(url)
        addresses = _public_resolved_addresses(str(parsed.hostname or ""))
    except ValueError as exc:
        return ToolRunResponse(tool="web.render", ok=False, summary=str(exc))

    browser = _find_headless_browser()
    if browser is None:
        return ToolRunResponse(
            tool="web.render",
            ok=False,
            summary="No headless Chrome/Edge executable was found.",
            data={"url": url},
        )

    result = await asyncio.to_thread(
        _run_headless_dump_dom,
        browser,
        url,
        addresses=addresses,
        wait_ms=wait_ms,
        timeout_sec=timeout_sec,
    )
    if not result["ok"]:
        return ToolRunResponse(
            tool="web.render",
            ok=False,
            summary=str(result["summary"]),
            data={"url": url, **result},
        )
    html = str(result.get("html") or "")
    text = _html_to_text(html)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip()
    if _web_response_blocked(200, text):
        return ToolRunResponse(
            tool="web.render",
            ok=False,
            summary="Rendered page appears blocked by the remote site.",
            data={
                "url": url,
                "browser": str(browser),
                "text": text,
                "html_chars": len(html),
                "truncated": truncated,
                "pinned_addresses": [str(item) for item in addresses],
                "stderr": str(result.get("stderr") or "")[:1000],
            },
        )
    return ToolRunResponse(
        tool="web.render",
        ok=True,
        summary="Rendered public URL in isolated headless browser.",
        data={
            "url": url,
            "browser": str(browser),
            "text": text,
            "html_chars": len(html),
            "truncated": truncated,
            "pinned_addresses": [str(item) for item in addresses],
            "stderr": str(result.get("stderr") or "")[:1000],
        },
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


WINDOWS_NATIVE_ACTIONS = {
    "capabilities",
    "process.start",
    "app.open_and_type",
    "screen.capture",
    "window.focus",
    "window.list",
    "keyboard.send",
    "wmi.query",
}

WINDOWS_NATIVE_SCRIPT = r"""
function Out($Ok, $Summary, $Data) {
  [pscustomobject]@{
    ok = $Ok
    summary = $Summary
    action = $Action
    data = $Data
  } | ConvertTo-Json -Depth 8 -Compress
}

try {
  if ($Action -eq 'capabilities') {
    Out $true 'Native Windows layer is available.' @{
      wmi = $true
      cim = $true
      winapi = $true
      windowFocus = $true
      keyboard = $true
      clipboard = $true
      process = $true
      screenshot = $true
    }
    exit 0
  }

  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class JWin {
  [DllImport("user32.dll")]
  public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")]
  public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")]
  public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")]
  public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("kernel32.dll")]
  public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")]
  public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
  [DllImport("user32.dll")]
  public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")]
  public static extern IntPtr SetFocus(IntPtr hWnd);
}
'@

  function Val($Value, $Default) {
    if ($null -eq $Value -or $Value -eq '') {
      return $Default
    }
    return $Value
  }

  function SplitTargets($Value) {
    if ($null -eq $Value -or [string]$Value -eq '') {
      return @()
    }
    return @(([string]$Value -split '\|') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
  }

  function ForegroundPid() {
    $foreground = [JWin]::GetForegroundWindow()
    if ($foreground -eq [IntPtr]::Zero) {
      return 0
    }
    $foregroundPid = 0
    [void][JWin]::GetWindowThreadProcessId($foreground, [ref]$foregroundPid)
    return [int]$foregroundPid
  }

  function IsForeground($Process) {
    if (-not $Process) {
      return $false
    }
    return (ForegroundPid) -eq [int]$Process.Id
  }

  function TryActivate($Process, $Title) {
    if (-not $Process -or $Process.MainWindowHandle -eq 0) {
      return $false
    }
    $targetPid = 0
    $targetThread = [JWin]::GetWindowThreadProcessId($Process.MainWindowHandle, [ref]$targetPid)
    $foregroundPid = 0
    $foregroundThread = [JWin]::GetWindowThreadProcessId(
      [JWin]::GetForegroundWindow(),
      [ref]$foregroundPid
    )
    $currentThread = [JWin]::GetCurrentThreadId()
    if ($foregroundThread -ne 0) {
      [void][JWin]::AttachThreadInput($currentThread, $foregroundThread, $true)
    }
    if ($targetThread -ne 0) {
      [void][JWin]::AttachThreadInput($currentThread, $targetThread, $true)
    }
    try {
      [void][JWin]::ShowWindowAsync($Process.MainWindowHandle, 9)
      Start-Sleep -Milliseconds 120
      [void][JWin]::BringWindowToTop($Process.MainWindowHandle)
      [void][JWin]::SetForegroundWindow($Process.MainWindowHandle)
      [void][JWin]::SetFocus($Process.MainWindowHandle)
    } finally {
      if ($targetThread -ne 0) {
        [void][JWin]::AttachThreadInput($currentThread, $targetThread, $false)
      }
      if ($foregroundThread -ne 0) {
        [void][JWin]::AttachThreadInput($currentThread, $foregroundThread, $false)
      }
    }
    Start-Sleep -Milliseconds 220
    if (IsForeground $Process) {
      return $true
    }
    try {
      $shell = New-Object -ComObject WScript.Shell
      [void]$shell.AppActivate([int]$Process.Id)
      Start-Sleep -Milliseconds 160
      if (IsForeground $Process) {
        return $true
      }
      foreach ($targetTitle in (SplitTargets $Title)) {
        [void]$shell.AppActivate($targetTitle)
        Start-Sleep -Milliseconds 160
        if (IsForeground $Process) {
          return $true
        }
      }
    } catch {
      return $false
    }
    return $false
  }

  function Focus($TargetPid, $Name, $Title) {
    $p = $null
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
      if ($TargetPid) {
        $candidate = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
        if ($candidate -and $candidate.MainWindowHandle -ne 0) {
          $p = $candidate
        }
      }
      if (-not $p) {
        foreach ($targetTitle in (SplitTargets $Title)) {
          $p = Get-Process -ErrorAction SilentlyContinue |
            Where-Object {
              $_.MainWindowTitle -like "*$targetTitle*" -and $_.MainWindowHandle -ne 0
            } |
            Select-Object -First 1
          if ($p) {
            break
          }
        }
      }
      if (-not $p) {
        foreach ($targetName in (SplitTargets $Name)) {
          $p = Get-Process -Name $targetName -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne 0 } |
            Select-Object -First 1
          if ($p) {
            break
          }
        }
      }
      if ($p -and $p.MainWindowHandle -ne 0) {
        break
      }
      Start-Sleep -Milliseconds 250
    }
    return (TryActivate $p $Title)
  }

  function SendInput($Keys, $Text) {
    if ($Text) {
      Set-Clipboard -Value ([string]$Text)
      Start-Sleep -Milliseconds 80
      [System.Windows.Forms.SendKeys]::SendWait('^v')
      Start-Sleep -Milliseconds 80
    }
    if ($Keys) {
      [System.Windows.Forms.SendKeys]::SendWait([string]$Keys)
    }
  }

  function HasExplicitTarget($Payload) {
    return [bool](
      $Payload.process_id -or
      $Payload.process_name -or
      $Payload.window_title
    )
  }

  function StartNativeProcess($Executable, $Arguments) {
    $parameters = @{
      FilePath = $Executable
      PassThru = $true
    }
    if ($null -ne $Arguments -and [string]$Arguments -ne '') {
      $parameters.ArgumentList = [string]$Arguments
    }
    return Start-Process @parameters
  }

  function VisibleWindows($Limit) {
    return Get-Process -ErrorAction SilentlyContinue |
      Where-Object { $_.MainWindowHandle -ne 0 } |
      Select-Object -First ([int](Val $Limit 30)) Id, ProcessName, MainWindowTitle
  }

  function ReadScreenOcr($OutputPath) {
    $tesseract = Get-Command tesseract -ErrorAction SilentlyContinue
    if (-not $tesseract) {
      return @{ available = $false; text = ''; error = 'tesseract not found' }
    }
    try {
      $text = & $tesseract.Source $OutputPath stdout --psm 6 2>$null | Out-String
      return @{ available = $true; text = ([string]$text).Trim(); error = '' }
    } catch {
      return @{ available = $false; text = ''; error = $_.Exception.Message }
    }
  }

  function CaptureScreen($OutputPath, $Limit, $Ocr) {
    $directory = Split-Path -Parent $OutputPath
    if ($directory) {
      New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
      $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
      $bitmap.Save($OutputPath, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
      $graphics.Dispose()
      $bitmap.Dispose()
    }
    $foreground = [JWin]::GetForegroundWindow()
    $foregroundPid = 0
    if ($foreground -ne [IntPtr]::Zero) {
      [void][JWin]::GetWindowThreadProcessId($foreground, [ref]$foregroundPid)
    }
    $active = $null
    if ($foregroundPid -gt 0) {
      $active = Get-Process -Id $foregroundPid -ErrorAction SilentlyContinue |
        Select-Object -First 1 Id, ProcessName, MainWindowTitle
    }
    $ocrResult = @{ available = $false; text = ''; error = '' }
    if ([bool]$Ocr) {
      $ocrResult = ReadScreenOcr $OutputPath
    }
    Out $true "Screen captured." @{
      path = $OutputPath
      width = $bounds.Width
      height = $bounds.Height
      left = $bounds.Left
      top = $bounds.Top
      activeWindow = $active
      ocrRequested = [bool]$Ocr
      ocrAvailable = [bool]$ocrResult.available
      ocrText = $ocrResult.text
      ocrError = $ocrResult.error
      windows = @(VisibleWindows $Limit)
    }
  }

  switch ($Action) {
    'process.start' {
      $p = StartNativeProcess $Payload.executable $Payload.arguments
      Out $true "Started $($Payload.executable)." @{
        pid = $p.Id
        processName = $p.ProcessName
      }
      break
    }
    'app.open_and_type' {
      $p = StartNativeProcess $Payload.executable $Payload.arguments
      Start-Sleep -Milliseconds ([int](Val $Payload.wait_ms 700))
      $focused = Focus $p.Id $Payload.process_name $Payload.window_title
      if (-not $focused) {
        Out $false "Target window was not focused; native input was not sent." @{
          pid = $p.Id
          focused = $focused
        }
        break
      }
      SendInput $Payload.keys $Payload.text
      Out $true "Opened $($Payload.executable) and sent native input." @{
        pid = $p.Id
        focused = $focused
      }
      break
    }
    'screen.capture' {
      CaptureScreen $Payload.path $Payload.limit $Payload.ocr
      break
    }
    'window.focus' {
      $focused = Focus `
        $Payload.process_id `
        $Payload.process_name `
        $Payload.window_title
      if ($focused) {
        $summary = 'Window focused through WinAPI.'
      } else {
        $summary = 'Window was not found.'
      }
      Out $focused $summary @{ focused = $focused }
      break
    }
    'keyboard.send' {
      $hasTarget = HasExplicitTarget $Payload
      $focused = $false
      if ($hasTarget) {
        $focused = Focus `
          $Payload.process_id `
          $Payload.process_name `
          $Payload.window_title
        if (-not $focused) {
          Out $false 'Target window was not focused; native input was not sent.' @{
            focused = $focused
          }
          break
        }
      }
      SendInput $Payload.keys $Payload.text
      Out $true 'Native keyboard input sent.' @{ focused = $focused }
      break
    }
    'window.list' {
      $items = VisibleWindows $Payload.limit
      Out $true "Listed $(@($items).Count) visible window(s)." @{
        windows = $items
      }
      break
    }
    'wmi.query' {
      $limit = [int](Val $Payload.limit 20)
      $props = @($Payload.properties)
      $base = @{
        Namespace = $Payload.namespace
        ClassName = $Payload.class_name
      }
      if ($Payload.filter) {
        $base.Filter = $Payload.filter
      }
      $q = Get-CimInstance @base | Select-Object -First $limit
      if ($props.Count -gt 0) {
        $q = $q | Select-Object -Property $props
      }
      Out $true "WMI/CIM query returned $(@($q).Count) item(s)." @{
        items = $q
        className = $Payload.class_name
        namespace = $Payload.namespace
      }
      break
    }
    default {
      throw "Unsupported native action: $Action"
    }
  }
} catch {
  Out $false $_.Exception.Message @{ error = $_.Exception.Message }
  exit 1
}
"""


def _windows_native_command(action: str, payload: dict[str, Any]) -> str:
    if action not in WINDOWS_NATIVE_ACTIONS:
        allowed = ", ".join(sorted(WINDOWS_NATIVE_ACTIONS))
        raise ValueError(f"Unsupported native action: {action}. Allowed: {allowed}.")
    clean_payload = _validate_native_payload(action, payload)
    payload_json = json.dumps(clean_payload, ensure_ascii=False, default=str)
    return "\n".join(
        [
            "$utf8=New-Object System.Text.UTF8Encoding -ArgumentList $false",
            "[Console]::OutputEncoding=$utf8",
            "$OutputEncoding=$utf8",
            "$ErrorActionPreference='Stop'",
            f"$Action={_powershell_quote(action)}",
            f"$Payload={_powershell_quote(payload_json)} | ConvertFrom-Json",
            WINDOWS_NATIVE_SCRIPT.strip(),
        ]
    )


def _validate_native_payload(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    clean = dict(payload)
    if action in {"process.start", "app.open_and_type"}:
        executable = _native_string(clean.get("executable"), "executable", max_length=260)
        clean["executable"] = executable
        clean["arguments"] = _native_string(
            clean.get("arguments", ""),
            "arguments",
            max_length=4000,
        )
    if action == "app.open_and_type":
        clean["keys"] = _native_string(clean.get("keys", ""), "keys", max_length=1000)
        clean["text"] = _native_string(clean.get("text", ""), "text", max_length=4000)
        clean["process_name"] = _native_string(
            clean.get("process_name", ""),
            "process_name",
            max_length=120,
        )
        clean["window_title"] = _native_string(
            clean.get("window_title", ""),
            "window_title",
            max_length=200,
        )
        clean["wait_ms"] = _int_arg(clean.get("wait_ms"), default=700, minimum=0, maximum=5000)
    if action in {"window.focus", "keyboard.send"}:
        clean["process_id"] = _int_arg(
            clean.get("process_id"),
            default=0,
            minimum=0,
            maximum=999999,
        )
        clean["process_name"] = _native_string(
            clean.get("process_name", ""),
            "process_name",
            max_length=120,
        )
        clean["window_title"] = _native_string(
            clean.get("window_title", ""),
            "window_title",
            max_length=200,
        )
    if action == "keyboard.send":
        clean["keys"] = _native_string(clean.get("keys", ""), "keys", max_length=1000)
        clean["text"] = _native_string(clean.get("text", ""), "text", max_length=4000)
        if not clean["keys"] and not clean["text"]:
            raise ValueError("keyboard.send requires keys or text.")
    if action == "window.list":
        clean["limit"] = _int_arg(clean.get("limit"), default=50, minimum=1, maximum=200)
    if action == "screen.capture":
        clean["path"] = _native_string(clean.get("path"), "path", max_length=500)
        clean["limit"] = _int_arg(clean.get("limit"), default=30, minimum=1, maximum=100)
        clean["ocr"] = bool(clean.get("ocr", False))
    if action == "wmi.query":
        clean = _validate_wmi_payload(clean)
    return clean


def _validate_wmi_payload(payload: dict[str, Any]) -> dict[str, Any]:
    namespace = _native_string(payload.get("namespace", "root\\cimv2"), "namespace", max_length=120)
    class_name = _native_string(payload.get("class_name"), "class_name", max_length=120)
    if not re.fullmatch(r"[A-Za-z0-9_\\]+", namespace):
        raise ValueError("WMI namespace contains unsupported characters.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", class_name):
        raise ValueError("WMI class name contains unsupported characters.")
    raw_properties = payload.get("properties") or []
    if isinstance(raw_properties, str):
        raw_properties = [item.strip() for item in raw_properties.split(",") if item.strip()]
    if not isinstance(raw_properties, list):
        raise ValueError("WMI properties must be a list or comma-separated string.")
    properties = []
    for item in raw_properties[:40]:
        prop = _native_string(item, "property", max_length=80)
        if prop and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", prop):
            raise ValueError("WMI property contains unsupported characters.")
        if prop:
            properties.append(prop)
    filter_text = _native_string(payload.get("filter", ""), "filter", max_length=500)
    return {
        "namespace": namespace,
        "class_name": class_name,
        "properties": properties,
        "filter": filter_text,
        "limit": _int_arg(payload.get("limit"), default=20, minimum=1, maximum=200),
    }


def _native_string(value: Any, field: str, *, max_length: int) -> str:
    text = str(value or "").strip()
    if any(char in text for char in ("\r", "\n", "\0")):
        raise ValueError(f"{field} contains unsupported control characters.")
    if len(text) > max_length:
        raise ValueError(f"{field} is too long.")
    return text


def _parse_native_stdout(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _redact_native_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(payload)
    for key in ("text", "keys"):
        value = redacted.get(key)
        if isinstance(value, str) and len(value) > 120:
            redacted[key] = f"{value[:120]}..."
    return redacted


def _resolve_allowed_path(settings: JarvisSettings, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("Path is required.")
    candidate = Path(raw_path)
    windows_candidate = PureWindowsPath(raw_path)
    if not candidate.is_absolute() and windows_candidate.drive:
        raise ValueError("Windows-style absolute paths are outside allowed roots.")
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
        if (
            policy.get("mode") == "approval-only"
            and policy.get("require_approval_for_external", True)
            and not is_local
        ):
            raise ValueError("Browser policy requires approval for external URLs.")
        if policy.get("mode") == "local-safe" and host not in allowed_hosts:
            raise ValueError("Browser policy only allows configured local hosts.")
        if is_local and not policy.get("allow_localhost", True):
            raise ValueError("Browser policy blocks localhost URLs.")
    return parsed.geturl()


class _PublicOnlyAsyncNetworkBackend(AsyncNetworkBackend):
    """httpcore network backend that pins DNS to public IPs resolved by Jarvis."""

    def __init__(self) -> None:
        self._backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: list[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        addresses = _public_resolved_addresses(host)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if last_error is not None:
            raise last_error
        raise OSError(f"Could not connect to public host: {host}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: list[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        return await self._backend.connect_unix_socket(
            path,
            timeout=timeout,
            socket_options=socket_options,
        )

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PublicOnlyAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    """httpx transport that avoids the second resolver hop inside httpcore."""

    def __init__(self) -> None:
        super().__init__(trust_env=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            http1=True,
            http2=False,
            retries=0,
            network_backend=_PublicOnlyAsyncNetworkBackend(),
        )


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
    _public_resolved_addresses(parsed.hostname)
    return parsed.geturl()


def _parse_duckduckgo_results(html: str, *, limit: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'<a[^>]+class="[^"]*\bresult__a\b[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>'
        r"(?P<title>.*?)</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html):
        url = _unwrap_duckduckgo_url(unescape(match.group("href")))
        title = _html_to_text(match.group("title"))
        if not url or not title or url in seen:
            continue
        if urlparse(url).scheme not in {"http", "https"}:
            continue
        snippet = _snippet_after_result(html, match.end())
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": snippet,
                "rank": len(results) + 1,
            }
        )
        seen.add(url)
        if len(results) >= limit:
            break
    return results


def _unwrap_duckduckgo_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target)
    return raw_url


def _snippet_after_result(html: str, offset: int) -> str:
    tail = html[offset : offset + 3000]
    match = re.search(
        r'<a[^>]+class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(?P<snippet>.*?)</a>',
        tail,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        match = re.search(
            r'<div[^>]+class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(?P<snippet>.*?)</div>',
            tail,
            flags=re.IGNORECASE | re.DOTALL,
        )
    return _html_to_text(match.group("snippet")) if match else ""


def _html_to_text(value: str) -> str:
    without_scripts = re.sub(
        r"(?is)<(script|style|noscript)[^>]*>.*?</\1>",
        " ",
        value,
    )
    without_tags = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
    return _repair_mojibake(" ".join(unescape(without_tags).split()))


def _decode_response_text(response: httpx.Response) -> str:
    encoding = _charset_from_content_type(response.headers.get("content-type")) or "utf-8"
    return _repair_mojibake(response.content.decode(encoding, errors="replace"))


def _repair_mojibake(text: str) -> str:
    if not text or not _looks_like_mojibake(text):
        return text

    candidates = [text]
    for encoding in ("cp1252", "latin1"):
        try:
            candidates.append(text.encode(encoding).decode("utf-8"))
        except UnicodeError:
            continue
    return min(candidates, key=_mojibake_score)


def _looks_like_mojibake(text: str) -> bool:
    return any(marker in text for marker in ("Ð", "Ñ", "Â", "â€"))


def _mojibake_score(text: str) -> tuple[int, int]:
    marker_score = sum(text.count(marker) for marker in ("Ð", "Ñ", "Â", "â€", "�"))
    cyrillic_bonus = sum(1 for char in text if "а" <= char.lower() <= "я" or char.lower() == "ё")
    return marker_score, -cyrillic_bonus


def _web_response_blocked(status_code: int, text: str) -> bool:
    if status_code in {401, 403, 429}:
        return True
    normalized = _repair_mojibake(text).lower()
    blocked_markers = (
        "403 error forbidden",
        "access denied",
        "forbidden",
        "доступ к сайту",
        "доступ запрещ",
        "captcha",
        "проверка браузера",
        "request id",
        "guru meditation",
    )
    return any(marker in normalized for marker in blocked_markers)


def _hostname_is_private(hostname: str) -> bool:
    return bool(_private_resolved_addresses(hostname))


def _public_resolved_addresses(
    hostname: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    addresses = _resolved_ip_addresses(hostname)
    private_addresses = [address for address in addresses if _address_is_private(address)]
    if private_addresses:
        blocked = ", ".join(str(item) for item in private_addresses[:4])
        raise ValueError(f"URL host must resolve only to public addresses; blocked: {blocked}")
    return addresses


def _private_resolved_addresses(
    hostname: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    return [address for address in _resolved_ip_addresses(hostname) if _address_is_private(address)]


def _resolved_ip_addresses(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        resolved = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"Could not resolve URL host: {hostname}") from exc
    if not resolved:
        raise ValueError(f"Could not resolve URL host: {hostname}")

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for item in resolved:
        address = item[4][0]
        parsed = ipaddress.ip_address(address)
        if parsed not in addresses:
            addresses.append(parsed)
    return addresses


def _address_is_private(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        not address.is_global
        or address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _find_headless_browser() -> Path | None:
    names = ("chrome.exe", "chrome", "msedge.exe", "msedge", "chromium.exe", "chromium")
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    candidates = [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _run_headless_dump_dom(
    browser: Path,
    url: str,
    *,
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
    wait_ms: int,
    timeout_sec: int,
) -> dict[str, Any]:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    resolver_rules = _headless_host_resolver_rules(host, addresses)
    with tempfile.TemporaryDirectory(prefix="jarvis-headless-") as profile_dir:
        command = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-proxy-server",
            "--proxy-server=direct://",
            "--proxy-bypass-list=*",
            f"--user-data-dir={profile_dir}",
            f"--virtual-time-budget={wait_ms}",
            f"--host-resolver-rules={resolver_rules}",
            "--dump-dom",
            url,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
                check=False,
                creationflags=_hidden_process_flags(),
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "summary": f"Headless browser timed out after {timeout_sec}s.",
                "browser": str(browser),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "summary": f"Headless browser failed: {exc}",
                "browser": str(browser),
            }
    ok = result.returncode == 0 and bool(result.stdout.strip())
    return {
        "ok": ok,
        "summary": (
            "Headless browser rendered DOM."
            if ok
            else f"Headless browser exited with {result.returncode}."
        ),
        "browser": str(browser),
        "returncode": result.returncode,
        "html": result.stdout,
        "stderr": result.stderr.strip(),
        "resolver_rules": resolver_rules,
    }


def _headless_host_resolver_rules(
    host: str,
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> str:
    if not host or not addresses:
        return "EXCLUDE localhost"
    address = next((item for item in addresses if item.version == 4), addresses[0])
    # Keep SNI/Host as the original hostname while making Chrome use the public
    # IP Jarvis already validated. The wildcard sink reduces background lookups.
    return f"MAP {host} {address}, MAP * 0.0.0.0, EXCLUDE localhost"


def _hidden_process_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _timestamp_slug() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


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
    text = _repair_mojibake(bytes(content).decode(encoding, errors="replace"))
    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type or "<html" in text[:500].lower():
        text = _html_to_text(text)
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
