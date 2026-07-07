from __future__ import annotations

import inspect
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .config import JarvisSettings
from .diagnostics import run_diagnostics
from .host_bridge import HostBridgeStatus
from .learning import LearningEngine
from .llm import LLMRouter
from .model_catalog import ModelCatalog
from .models import ToolInfo, ToolRunResponse
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
