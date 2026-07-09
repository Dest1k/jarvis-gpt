from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .agent import AgentRuntime
from .approval_executor import ApprovalExecutor
from .autonomy_executor import AutonomyExecutor
from .config import ensure_runtime_dirs, load_settings
from .diagnostics import run_diagnostics
from .dispatcher import DispatcherManager
from .event_bus import EventBus
from .experience import ExperienceManager
from .host_bridge import HostBridgeStatus
from .ingest import FileIngestor
from .learning import LearningEngine
from .llm import LLMRouter
from .model_catalog import ModelCatalog
from .model_hub import ModelHubManager
from .models import (
    ApprovalCreateRequest,
    ApprovalExecutionResponse,
    ApprovalItem,
    ApprovalUpdateRequest,
    AuditEntry,
    AutonomyJobCreateRequest,
    AutonomyJobResponse,
    AutonomyJobRunResponse,
    AutonomyJobUpdateRequest,
    AutonomyPolicyResponse,
    AutonomyPolicyUpdateRequest,
    AutonomyStatusResponse,
    BenchmarkResponse,
    BrowserPolicyResponse,
    BrowserPolicyUpdateRequest,
    ChatRequest,
    ChatResponse,
    CleanupRequest,
    ConversationItem,
    DailyBriefingResponse,
    DiagnosticsResponse,
    DirectoryIngestRequest,
    DirectoryIngestResponse,
    DispatcherActionResponse,
    DispatcherStatusResponse,
    DockerContainersResponse,
    DockerPolicyResponse,
    DockerPolicyUpdateRequest,
    FileChunkHit,
    FileIngestResponse,
    FileItem,
    HostBridgeResponse,
    LearningTickResponse,
    MemoryCreateRequest,
    MemoryHygieneResponse,
    MemoryItem,
    MemoryVaultResponse,
    MessageFeedbackRequest,
    MessageItem,
    Mission,
    MissionCreateRequest,
    MissionExecutionResponse,
    MissionRunResponse,
    MissionTask,
    MissionTaskUpdateRequest,
    ModelActivateRequest,
    ModelCatalogResponse,
    ModelDownloadRequest,
    ModelProfilesResponse,
    ModelSearchResponse,
    OperatorPersonaInsightRequest,
    OperatorPersonaResponse,
    OperatorPersonaUpdateRequest,
    OperatorQueueResponse,
    RoutineResponse,
    RoutineRunResponse,
    RuntimePreferencesResponse,
    RuntimePreferencesUpdateRequest,
    SelfHealResponse,
    StatusResponse,
    TelemetryResponse,
    ToolInfo,
    ToolRunRequest,
    ToolRunResponse,
)
from .operations import OperationsManager
from .operator_queue import (
    answer_quality_report,
    memory_hygiene_report,
    model_profile_plan,
    operator_queue_snapshot,
)
from .persona import PersonaManager
from .storage import JarvisStorage, utc_now
from .supervisor import RuntimeSupervisor
from .telemetry import TelemetryCollector


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = LLMRouter(settings)
    bus = EventBus()
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, bus=bus)
    ingestor = FileIngestor(settings=settings, storage=storage)
    models = ModelCatalog(settings, storage)
    model_hub = ModelHubManager(settings=settings, storage=storage)
    dispatcher = DispatcherManager(settings, storage=storage)
    telemetry = TelemetryCollector(settings)
    learning = LearningEngine(storage, llm=llm)
    host_bridge = HostBridgeStatus(settings)
    experience = ExperienceManager(settings=settings, storage=storage)
    persona = PersonaManager(settings=settings, storage=storage)
    operations = OperationsManager(settings=settings, storage=storage)
    autonomy_executor = AutonomyExecutor(
        settings=settings,
        storage=storage,
        operations=operations,
        agent=agent,
        experience=experience,
        llm=llm,
        telemetry=telemetry,
        dispatcher=dispatcher,
        learning=learning,
        bus=bus,
    )
    supervisor = RuntimeSupervisor(
        settings=settings,
        storage=storage,
        llm=llm,
        autonomy_executor=autonomy_executor,
    )
    approval_executor = ApprovalExecutor(
        storage=storage,
        llm=llm,
        dispatcher=dispatcher,
        tools=agent.tools,
        mission_resumer=agent.resume_mission_after_approval,
    )

    app.state.settings = settings
    app.state.storage = storage
    app.state.llm = llm
    app.state.bus = bus
    app.state.agent = agent
    app.state.ingestor = ingestor
    app.state.models = models
    app.state.model_hub = model_hub
    app.state.dispatcher = dispatcher
    app.state.telemetry = telemetry
    app.state.learning = learning
    app.state.host_bridge = host_bridge
    app.state.experience = experience
    app.state.persona = persona
    app.state.operations = operations
    app.state.autonomy_executor = autonomy_executor
    app.state.supervisor = supervisor
    app.state.approval_executor = approval_executor
    storage.add_event(kind="runtime.start", title="JARVIS GPT backend started")
    await supervisor.start()
    try:
        yield
    finally:
        await supervisor.stop()
        storage.add_event(kind="runtime.stop", title="JARVIS GPT backend stopped")
        storage.close()


def _cors_origins() -> list[str]:
    raw = os.environ.get("JARVIS_CORS_ORIGINS", "")
    return [item.strip().rstrip("/") for item in raw.split(",") if item.strip()]


def _api_token() -> str:
    return os.environ.get("JARVIS_API_TOKEN", "").strip()


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _header_token(headers: Any) -> str:
    auth = str(headers.get("authorization") or "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return str(headers.get("x-jarvis-api-token") or "").strip()


def _token_allowed(token: str) -> bool:
    expected = _api_token()
    return bool(expected and token and secrets.compare_digest(token, expected))


def _strict_loopback_token_required() -> bool:
    return bool(app.state.settings.api_require_token_on_loopback)


app = FastAPI(title="JARVIS GPT", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def local_api_guard(request: Request, call_next):
    if request.method == "OPTIONS" or request.url.path in {"/", "/health"}:
        return await call_next(request)
    host = request.client.host if request.client else ""
    token_ok = _token_allowed(_header_token(request.headers))
    if token_ok or (_is_loopback_host(host) and not _strict_loopback_token_required()):
        return await call_next(request)
    status_code = 401 if _api_token() else 403
    detail = (
        "API access requires a valid bearer token."
        if _api_token()
        else "API access is locked until JARVIS_API_TOKEN is configured."
    )
    return JSONResponse({"detail": detail}, status_code=status_code)


def storage(request_app: FastAPI) -> JarvisStorage:
    return request_app.state.storage


@app.get("/")
async def index() -> dict[str, str]:
    return {"name": "JARVIS GPT", "status": "online"}


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "profile": app.state.settings.profile.name,
        "home": str(app.state.settings.home),
    }


@app.get("/api/status", response_model=StatusResponse)
async def status() -> StatusResponse:
    health_rows = app.state.storage.latest_health(limit=20)
    health_checks = [
        {
            "name": row["component"],
            "status": row["status"],
            "message": row["message"],
            "details": row["details"],
        }
        for row in health_rows
    ]
    return StatusResponse(
        settings=app.state.settings.public_dict(),
        counters=app.state.storage.counters(),
        health=health_checks,
        recent_events=app.state.storage.list_events(limit=25),
    )


@app.get("/api/runtime/security")
async def runtime_security(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else ""
    return {
        "client_host": host,
        "loopback_client": _is_loopback_host(host),
        "token_configured": bool(_api_token()),
        "loopback_requires_token": app.state.settings.api_require_token_on_loopback,
        "remote_requires_token": True,
        "cors": {
            "explicit_origins": _cors_origins(),
            "loopback_origins_allowed": True,
        },
    }


@app.post("/api/runtime/backup")
async def runtime_backup() -> dict[str, Any]:
    return app.state.storage.backup_database()


@app.get("/api/operator/queue", response_model=OperatorQueueResponse)
async def operator_queue() -> OperatorQueueResponse:
    return operator_queue_snapshot(app.state.settings, app.state.storage)


@app.get("/api/operator/quality")
async def operator_quality() -> dict[str, Any]:
    return answer_quality_report(app.state.storage)


@app.get("/api/model-profiles", response_model=ModelProfilesResponse)
async def model_profiles() -> ModelProfilesResponse:
    return model_profile_plan(app.state.settings)


@app.get("/api/agent/trace/{conversation_id}")
async def agent_trace(conversation_id: str) -> dict[str, Any]:
    conversation = app.state.storage.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = app.state.storage.recent_messages(conversation_id, limit=40)
    turns = []
    for item in messages:
        metadata = item.get("metadata") or {}
        events = metadata.get("events") if isinstance(metadata, dict) else None
        task_kernel = metadata.get("task_kernel") if isinstance(metadata, dict) else None
        turns.append(
            {
                "role": item.get("role"),
                "created_at": item.get("created_at"),
                "task_kernel": task_kernel if isinstance(task_kernel, dict) else None,
                "events": events if isinstance(events, list) else [],
                "duration_ms": metadata.get("duration_ms") if isinstance(metadata, dict) else None,
            }
        )
    recent_kernel_events = [
        event
        for event in app.state.storage.list_events(limit=60)
        if event.get("kind") == "agent.task_kernel"
        and (event.get("payload") or {}).get("conversation_id") in {None, conversation_id}
    ]
    return {
        "conversation": conversation,
        "turns": turns,
        "recent_task_kernel_events": recent_kernel_events[:10],
    }


@app.get("/api/agent/trace/message/{message_id}")
async def agent_message_trace(message_id: str) -> dict[str, Any]:
    message = app.state.storage.get_message(message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.get("role") != "assistant":
        raise HTTPException(status_code=400, detail="Only assistant messages have thought traces")
    conversation_id = str(message.get("conversation_id") or "")
    conversation = app.state.storage.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = app.state.storage.list_messages(conversation_id, limit=500)
    return _message_trace_payload(conversation=conversation, messages=messages, output=message)


def _message_trace_payload(
    *,
    conversation: dict[str, Any],
    messages: list[dict[str, Any]],
    output: dict[str, Any],
) -> dict[str, Any]:
    output_id = str(output.get("id") or "")
    output_index = next(
        (index for index, item in enumerate(messages) if item.get("id") == output_id),
        -1,
    )
    input_message = _previous_user_message(messages, before_index=output_index)
    metadata = output.get("metadata") if isinstance(output.get("metadata"), dict) else {}
    events = metadata.get("events") if isinstance(metadata, dict) else []
    if not isinstance(events, list):
        events = []
    nodes, edges = _trace_nodes_and_edges(
        input_message=input_message,
        output_message=output,
        events=events,
    )
    return {
        "conversation": conversation,
        "input": _trace_message_payload(input_message),
        "output": _trace_message_payload(output),
        "duration_ms": metadata.get("duration_ms") if isinstance(metadata, dict) else None,
        "events": events,
        "nodes": nodes,
        "edges": edges,
        "disclosure": (
            "Трасса показывает наблюдаемые стадии runtime, инструменты, маршрутизацию "
            "и сохранённую metadata; скрытая chain-of-thought не раскрывается."
        ),
    }


def _previous_user_message(
    messages: list[dict[str, Any]],
    *,
    before_index: int,
) -> dict[str, Any] | None:
    if before_index < 0:
        return None
    for item in reversed(messages[:before_index]):
        if item.get("role") == "user":
            return item
    return None


def _trace_message_payload(message: dict[str, Any] | None) -> dict[str, Any] | None:
    if message is None:
        return None
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    return {
        "id": message.get("id"),
        "conversation_id": message.get("conversation_id"),
        "role": message.get("role"),
        "content": message.get("content"),
        "created_at": message.get("created_at"),
        "metadata": metadata,
    }


def _trace_nodes_and_edges(
    *,
    input_message: dict[str, Any] | None,
    output_message: dict[str, Any],
    events: list[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    nodes: list[dict[str, Any]] = [
        {
            "id": "input",
            "kind": "input",
            "title": "Вход",
            "summary": _short_trace_text((input_message or {}).get("content")),
        }
    ]
    previous_id = "input"
    edges: list[dict[str, str]] = []
    for index, raw_event in enumerate(events):
        if not isinstance(raw_event, dict):
            continue
        node_id = f"event-{index}"
        payload = raw_event.get("payload") if isinstance(raw_event.get("payload"), dict) else {}
        nodes.append(
            {
                "id": node_id,
                "kind": str(raw_event.get("type") or "event"),
                "title": str(raw_event.get("title") or raw_event.get("type") or "Событие"),
                "summary": _short_trace_text(
                    raw_event.get("content") or _event_payload_summary(payload)
                ),
                "payload": payload,
            }
        )
        edges.append({"from": previous_id, "to": node_id})
        previous_id = node_id
    nodes.append(
        {
            "id": "output",
            "kind": "output",
            "title": "Выход",
            "summary": _short_trace_text(output_message.get("content")),
        }
    )
    edges.append({"from": previous_id, "to": "output"})
    return nodes, edges


def _event_payload_summary(payload: dict[str, Any]) -> str:
    if not payload:
        return ""
    preferred = []
    for key in (
        "route",
        "intent",
        "query",
        "tool",
        "ok",
        "source",
        "finish_reason",
        "tool_steps",
        "continuations",
    ):
        if key in payload:
            preferred.append(f"{key}={payload[key]}")
    if preferred:
        return "; ".join(preferred)
    return json.dumps(payload, ensure_ascii=False)[:500]


def _short_trace_text(value: Any, max_chars: int = 360) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


@app.get("/api/models", response_model=ModelCatalogResponse)
async def models() -> ModelCatalogResponse:
    return app.state.model_hub.inventory()


@app.get("/api/model-hub/search", response_model=ModelSearchResponse)
async def model_hub_search(
    query: str = Query(min_length=1, max_length=160),
    limit: int = Query(default=12, ge=1, le=30),
    context_tokens: int = Query(default=8192, ge=512, le=131072),
) -> ModelSearchResponse:
    return app.state.model_hub.search(query, limit=limit, context_tokens=context_tokens)


@app.get("/api/model-hub/downloads")
async def model_downloads() -> list[dict[str, Any]]:
    return app.state.model_hub.download_jobs()


@app.post("/api/model-hub/download")
async def model_download(request: ModelDownloadRequest) -> dict[str, Any]:
    try:
        return app.state.model_hub.start_download(
            request.repo_id,
            revision=request.revision,
            workers=request.workers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/models/activate")
async def model_activate(request: ModelActivateRequest) -> dict[str, Any]:
    try:
        result = app.state.model_hub.activate_model(request.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await app.state.bus.publish({"channel": "models", "action": "activate", **result})
    return result


@app.delete("/api/models/local/{model_id}")
async def model_delete(model_id: str) -> dict[str, Any]:
    try:
        result = app.state.model_hub.delete_model(model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await app.state.bus.publish({"channel": "models", "action": "delete", **result})
    return result


@app.get("/api/dispatcher", response_model=DispatcherStatusResponse)
async def dispatcher() -> DispatcherStatusResponse:
    return app.state.dispatcher.status()


@app.post("/api/dispatcher/{action}", response_model=DispatcherActionResponse)
async def dispatcher_action(action: str) -> DispatcherActionResponse:
    action_map = {"start": "up", "stop": "down", "logs": "logs"}
    compose_action = action_map.get(action)
    if compose_action is None:
        raise HTTPException(status_code=400, detail="Unsupported dispatcher action")
    result = app.state.dispatcher.run_compose(compose_action)
    status_snapshot = app.state.dispatcher.status()
    await app.state.bus.publish(
        {
            "channel": "dispatcher",
            "action": action,
            "ok": result["ok"],
            "status": status_snapshot["container_status"],
        }
    )
    return DispatcherActionResponse.model_validate({**result, "status": status_snapshot})


@app.get("/api/telemetry", response_model=TelemetryResponse)
async def telemetry() -> TelemetryResponse:
    snapshot = await asyncio.to_thread(app.state.telemetry.snapshot)
    app.state.storage.record_telemetry(snapshot)
    return snapshot


@app.get("/api/telemetry/live", response_model=TelemetryResponse)
async def telemetry_live() -> TelemetryResponse:
    return await asyncio.to_thread(app.state.telemetry.live_snapshot)


@app.post("/api/learning/tick", response_model=LearningTickResponse)
async def learning_tick() -> LearningTickResponse:
    result = await app.state.learning.tick_async()
    await app.state.bus.publish({"channel": "learning", "lesson_count": result["lesson_count"]})
    return result


@app.get("/api/learning/journal")
async def learning_journal(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    return app.state.storage.list_learning_observations(limit=limit)


@app.get("/api/host-bridge", response_model=HostBridgeResponse)
async def host_bridge() -> HostBridgeResponse:
    return app.state.host_bridge.snapshot()


@app.get("/api/autonomy", response_model=AutonomyStatusResponse)
async def autonomy() -> AutonomyStatusResponse:
    return app.state.supervisor.status()


@app.get("/api/preferences", response_model=RuntimePreferencesResponse)
async def preferences() -> RuntimePreferencesResponse:
    return app.state.experience.preferences()


@app.patch("/api/preferences", response_model=RuntimePreferencesResponse)
async def update_preferences(
    request: RuntimePreferencesUpdateRequest,
) -> RuntimePreferencesResponse:
    updated = app.state.experience.update_preferences(request.model_dump(exclude_none=True))
    await app.state.bus.publish({"channel": "preferences", "operator": updated["operator_name"]})
    return updated


@app.get("/api/persona", response_model=OperatorPersonaResponse)
async def persona() -> OperatorPersonaResponse:
    return app.state.persona.persona()


@app.patch("/api/persona", response_model=OperatorPersonaResponse)
async def update_persona(request: OperatorPersonaUpdateRequest) -> OperatorPersonaResponse:
    updated = app.state.persona.update(request.model_dump(exclude_none=True))
    await app.state.bus.publish({"channel": "persona", "role": updated.get("role")})
    return updated


@app.post("/api/persona/insight", response_model=OperatorPersonaResponse)
async def add_persona_insight(request: OperatorPersonaInsightRequest) -> OperatorPersonaResponse:
    updated = app.state.persona.add_insight(request.field, request.value, actor="operator")
    await app.state.bus.publish({"channel": "persona", "field": request.field})
    return updated


@app.get("/api/autonomy/policy", response_model=AutonomyPolicyResponse)
async def autonomy_policy() -> AutonomyPolicyResponse:
    return app.state.experience.autonomy_policy()


@app.patch("/api/autonomy/policy", response_model=AutonomyPolicyResponse)
async def update_autonomy_policy(
    request: AutonomyPolicyUpdateRequest,
) -> AutonomyPolicyResponse:
    updated = app.state.experience.update_autonomy_policy(request.model_dump(exclude_none=True))
    await app.state.bus.publish({"channel": "autonomy.policy", "mode": updated["mode"]})
    return updated


@app.get("/api/browser/policy", response_model=BrowserPolicyResponse)
async def browser_policy() -> BrowserPolicyResponse:
    return app.state.operations.browser_policy()


@app.patch("/api/browser/policy", response_model=BrowserPolicyResponse)
async def update_browser_policy(request: BrowserPolicyUpdateRequest) -> BrowserPolicyResponse:
    updated = app.state.operations.update_browser_policy(request.model_dump(exclude_none=True))
    await app.state.bus.publish({"channel": "browser.policy", "mode": updated["mode"]})
    return updated


@app.get("/api/docker/policy", response_model=DockerPolicyResponse)
async def docker_policy() -> DockerPolicyResponse:
    return app.state.operations.docker_policy()


@app.patch("/api/docker/policy", response_model=DockerPolicyResponse)
async def update_docker_policy(request: DockerPolicyUpdateRequest) -> DockerPolicyResponse:
    updated = app.state.operations.update_docker_policy(request.model_dump(exclude_none=True))
    await app.state.bus.publish(
        {"channel": "docker.policy", "max_log_tail": updated["max_log_tail"]}
    )
    return updated


@app.get("/api/docker/containers", response_model=DockerContainersResponse)
async def docker_containers() -> DockerContainersResponse:
    return await asyncio.to_thread(app.state.operations.docker_containers)


@app.post("/api/cleanup")
async def cleanup_runtime(request: CleanupRequest) -> dict[str, Any]:
    result = await asyncio.to_thread(
        app.state.operations.cleanup,
        aggressive=request.aggressive,
    )
    await app.state.bus.publish(
        {"channel": "cleanup", "ok": result["ok"], "aggressive": request.aggressive}
    )
    return result


@app.get("/api/autonomy/jobs", response_model=list[AutonomyJobResponse])
async def autonomy_jobs() -> list[AutonomyJobResponse]:
    return app.state.operations.list_jobs()


@app.get("/api/autonomy/job-runs")
async def autonomy_job_runs(
    limit: int = Query(default=50, ge=1, le=200),
    job_id: str | None = Query(default=None, max_length=120),
) -> list[dict[str, Any]]:
    return app.state.operations.list_job_runs(limit=limit, job_id=job_id)


@app.post("/api/autonomy/jobs", response_model=AutonomyJobResponse)
async def create_autonomy_job(request: AutonomyJobCreateRequest) -> AutonomyJobResponse:
    job = app.state.operations.create_job(request.model_dump())
    await app.state.bus.publish(
        {"channel": "autonomy.jobs", "action": "created", "job_id": job["id"]}
    )
    return job


@app.patch("/api/autonomy/jobs/{job_id}", response_model=AutonomyJobResponse)
async def update_autonomy_job(
    job_id: str,
    request: AutonomyJobUpdateRequest,
) -> AutonomyJobResponse:
    job = app.state.operations.update_job(job_id, request.model_dump(exclude_none=True))
    if job is None:
        raise HTTPException(status_code=404, detail="Autonomy job not found")
    await app.state.bus.publish({"channel": "autonomy.jobs", "action": "updated", "job_id": job_id})
    return job


@app.post("/api/autonomy/jobs/{job_id}/cancel", response_model=AutonomyJobResponse)
async def cancel_autonomy_job(job_id: str) -> AutonomyJobResponse:
    job = await app.state.autonomy_executor.cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Autonomy job not found")
    return job


@app.post("/api/autonomy/jobs/{job_id}/run", response_model=AutonomyJobRunResponse)
async def run_autonomy_job(job_id: str) -> AutonomyJobRunResponse:
    job = next((item for item in app.state.operations.list_jobs() if item["id"] == job_id), None)
    if job is None:
        raise HTTPException(status_code=404, detail="Autonomy job not found")
    return await app.state.autonomy_executor.run_job(job)


@app.get("/api/routines", response_model=list[RoutineResponse])
async def routines() -> list[RoutineResponse]:
    return app.state.operations.routines()


@app.post("/api/routines/{routine_id}/run", response_model=RoutineRunResponse)
async def run_routine(routine_id: str) -> RoutineRunResponse:
    routine = next(
        (item for item in app.state.operations.routines() if item["id"] == routine_id),
        None,
    )
    if routine is None:
        raise HTTPException(status_code=404, detail="Routine not found")
    results = [await app.state.autonomy_executor.run_kind(step, {}) for step in routine["steps"]]
    ok = all(item["ok"] for item in results)
    response = {
        "routine": routine,
        "ok": ok,
        "summary": f"Routine {routine['title']} finished with {len(results)} step(s).",
        "results": results,
    }
    app.state.operations.record_routine_run(routine, response)
    await app.state.bus.publish({"channel": "routines", "routine_id": routine_id, "ok": ok})
    return response


@app.get("/api/briefing", response_model=DailyBriefingResponse)
async def briefing() -> DailyBriefingResponse:
    dispatcher_status = await asyncio.to_thread(app.state.dispatcher.status)
    return app.state.experience.daily_briefing(dispatcher_status=dispatcher_status)


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    return await app.state.agent.chat(
        request.message,
        conversation_id=request.conversation_id,
        mode=request.mode,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        attachments=[item.model_dump() for item in request.attachments],
        thinking_enabled=request.thinking_enabled,
    )


INTERRUPTED_STREAM_KEY_PREFIX = "agent.stream.interrupted."


def _persist_interrupted_stream(
    storage: JarvisStorage,
    *,
    conversation_id: str | None,
    partial: list[str],
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not conversation_id:
        return None
    answer = "".join(partial).strip()
    if not answer:
        return None
    message_id = storage.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content=answer,
        metadata={
            "duration_ms": None,
            "events": events,
            "interrupted": True,
            "stream": True,
        },
    )
    item = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "saved_at": utc_now(),
        "chars": len(answer),
    }
    storage.set_runtime_value(f"{INTERRUPTED_STREAM_KEY_PREFIX}{conversation_id}", item)
    storage.add_event(
        kind="agent.stream.interrupted",
        title="Interrupted streaming answer persisted.",
        level="warn",
        payload=item,
    )
    return item


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    async def lines():
        conversation_id = request.conversation_id
        partial: list[str] = []
        events: list[dict[str, Any]] = []
        saved_done = False
        try:
            async for item in app.state.agent.stream_chat(
                request.message,
                conversation_id=request.conversation_id,
                mode=request.mode,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                attachments=[item.model_dump() for item in request.attachments],
                thinking_enabled=request.thinking_enabled,
            ):
                if item.get("type") == "meta":
                    conversation_id = str(item.get("conversation_id") or conversation_id or "")
                elif item.get("type") == "delta":
                    partial.append(str(item.get("content") or ""))
                elif item.get("type") == "event" and isinstance(item.get("event"), dict):
                    events.append(item["event"])
                elif item.get("type") == "done":
                    saved_done = True
                yield f"{json.dumps(item, ensure_ascii=False)}\n".encode()
        except asyncio.CancelledError:
            if not saved_done:
                _persist_interrupted_stream(
                    app.state.storage,
                    conversation_id=conversation_id,
                    partial=partial,
                    events=events,
                )
            raise

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.get("/api/chat/stream/interrupted/{conversation_id}")
async def interrupted_stream(conversation_id: str) -> dict[str, Any]:
    item = app.state.storage.get_runtime_value(
        f"{INTERRUPTED_STREAM_KEY_PREFIX}{conversation_id}",
        None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Interrupted stream not found")
    return item


@app.get("/api/conversations", response_model=list[ConversationItem])
async def list_conversations(
    limit: int = Query(default=25, ge=1, le=100),
) -> list[ConversationItem]:
    return app.state.storage.list_conversations(limit=limit)


@app.get("/api/conversations/{conversation_id}/messages", response_model=list[MessageItem])
async def list_conversation_messages(
    conversation_id: str,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[MessageItem]:
    messages = app.state.storage.list_messages(conversation_id, limit=limit)
    if not messages and app.state.storage.get_conversation(conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return messages


@app.post("/api/messages/{message_id}/feedback", response_model=MessageItem)
async def set_message_feedback(message_id: str, request: MessageFeedbackRequest) -> MessageItem:
    updated = app.state.storage.set_message_feedback(
        message_id,
        rating=request.rating,
        comment=request.comment,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if app.state.bus is not None:
        await app.state.bus.publish(
            {
                "channel": "agent",
                "type": "feedback",
                "title": "Оценка ответа получена",
                "payload": {"message_id": message_id, "rating": request.rating},
            }
        )
    return MessageItem.model_validate(updated)


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> dict[str, bool]:
    deleted = app.state.storage.delete_conversation(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await app.state.bus.publish({"channel": "conversations", "deleted": conversation_id})
    return {"ok": True}


@app.get("/api/missions", response_model=list[Mission])
async def list_missions(limit: int = Query(default=50, ge=1, le=200)) -> list[Mission]:
    return app.state.storage.list_missions(limit=limit)


@app.post("/api/missions", response_model=Mission)
async def create_mission(request: MissionCreateRequest) -> Mission:
    return app.state.agent.create_mission(goal=request.goal, title=request.title)


@app.get("/api/missions/{mission_id}", response_model=Mission)
async def get_mission(mission_id: str) -> Mission:
    mission = app.state.storage.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return mission


@app.post("/api/missions/{mission_id}/execute-next", response_model=MissionExecutionResponse)
async def execute_next_mission_step(mission_id: str) -> MissionExecutionResponse:
    if app.state.storage.get_mission(mission_id) is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return await app.state.agent.execute_next_mission_step(mission_id)


@app.post("/api/missions/{mission_id}/run", response_model=MissionRunResponse)
async def run_mission(
    mission_id: str,
    max_steps: int | None = Query(default=None, ge=1, le=24),
) -> MissionRunResponse:
    if app.state.storage.get_mission(mission_id) is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    return await app.state.agent.run_mission(mission_id, max_steps=max_steps)


@app.get("/api/missions/{mission_id}/report")
async def get_mission_report(mission_id: str) -> dict[str, Any]:
    if app.state.storage.get_mission(mission_id) is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    record = app.state.agent.mission_report(mission_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Mission report is not ready")
    return record


@app.patch("/api/missions/{mission_id}/tasks/{task_id}", response_model=MissionTask)
async def update_mission_task(
    mission_id: str,
    task_id: str,
    request: MissionTaskUpdateRequest,
) -> MissionTask:
    mission = app.state.storage.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    updated = app.state.storage.update_mission_task(
        task_id,
        mission_id=mission_id,
        title=request.title,
        status=request.status,
        notes=request.notes,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Mission task not found")
    return updated


@app.get("/api/memory", response_model=list[MemoryItem])
async def search_memory(
    q: str | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> list[MemoryItem]:
    return app.state.storage.search_memory(q, limit=limit)


@app.post("/api/memory", response_model=MemoryItem)
async def add_memory(request: MemoryCreateRequest) -> MemoryItem:
    return app.state.storage.add_memory(
        content=request.content,
        namespace=request.namespace,
        tags=request.tags,
        importance=request.importance,
    )


@app.get("/api/memory/vault", response_model=MemoryVaultResponse)
async def memory_vault() -> MemoryVaultResponse:
    return app.state.storage.memory_graph()


@app.get("/api/memory/hygiene", response_model=MemoryHygieneResponse)
async def memory_hygiene() -> MemoryHygieneResponse:
    return memory_hygiene_report(app.state.storage)


@app.post("/api/memory/consolidate")
async def memory_consolidate() -> dict[str, int]:
    return app.state.storage.consolidate_memories()


@app.get("/api/files", response_model=list[FileItem])
async def list_files(limit: int = Query(default=25, ge=1, le=200)) -> list[FileItem]:
    return app.state.storage.list_files(limit=limit)


@app.post("/api/files/upload", response_model=FileIngestResponse)
async def upload_file(file: UploadFile = File(...)) -> FileIngestResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")
    try:
        result = app.state.ingestor.ingest_upload(file.filename, file.file)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await file.close()
    await app.state.bus.publish(
        {
            "channel": "files",
            "event": "uploaded",
            "file_id": result["file"]["id"],
            "chunks_indexed": result["chunks_indexed"],
        }
    )
    return result


@app.post("/api/files/ingest-directory", response_model=DirectoryIngestResponse)
async def ingest_directory(request: DirectoryIngestRequest) -> DirectoryIngestResponse:
    try:
        result = await asyncio.to_thread(
            app.state.ingestor.ingest_directory,
            request.path,
            max_files=request.max_files,
        )
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await app.state.bus.publish(
        {
            "channel": "files",
            "event": "directory_ingested",
            "root": result["root"],
            "files_indexed": result["files_indexed"],
        }
    )
    return result


@app.get("/api/files/search", response_model=list[FileChunkHit])
async def search_files(
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=12, ge=1, le=50),
) -> list[FileChunkHit]:
    return app.state.storage.search_file_chunks(q, limit=limit)


@app.get("/api/files/{file_id}/download")
async def download_file(file_id: str) -> FileResponse:
    item = app.state.storage.get_file(file_id)
    if item is None:
        raise HTTPException(status_code=404, detail="File not found")
    path = Path(item["stored_path"])
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Stored file not found")
    return FileResponse(path, filename=item["name"], media_type=item["mime_type"])


@app.get("/api/files/{file_id}", response_model=FileItem)
async def get_file(file_id: str) -> FileItem:
    item = app.state.storage.get_file(file_id)
    if item is None:
        raise HTTPException(status_code=404, detail="File not found")
    return item


@app.get("/api/audit", response_model=list[AuditEntry])
async def list_audit(
    limit: int = Query(default=25, ge=1, le=200),
    target_type: str | None = Query(default=None, max_length=80),
    target_id: str | None = Query(default=None, max_length=120),
) -> list[AuditEntry]:
    return app.state.storage.list_audit(
        limit=limit,
        target_type=target_type,
        target_id=target_id,
    )


@app.get("/api/approvals", response_model=list[ApprovalItem])
async def list_approvals(
    limit: int = Query(default=25, ge=1, le=200),
    status: str | None = Query(default=None, max_length=40),
) -> list[ApprovalItem]:
    return app.state.storage.list_approvals(limit=limit, status=status)


@app.post("/api/approvals", response_model=ApprovalItem)
async def create_approval(request: ApprovalCreateRequest) -> ApprovalItem:
    return app.state.storage.create_approval(
        title=request.title,
        description=request.description,
        requested_action=request.requested_action,
        risk=request.risk,
        payload=request.payload,
    )


@app.patch("/api/approvals/{approval_id}", response_model=ApprovalItem)
async def update_approval(
    approval_id: str,
    request: ApprovalUpdateRequest,
) -> ApprovalItem:
    updated = app.state.storage.update_approval(
        approval_id,
        status=request.status,
        result=request.result,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    await app.state.bus.publish(
        {"channel": "approvals", "approval_id": approval_id, "status": request.status}
    )
    return updated


@app.post("/api/approvals/{approval_id}/execute", response_model=ApprovalExecutionResponse)
async def execute_approval(approval_id: str) -> ApprovalExecutionResponse:
    result = await app.state.approval_executor.execute(approval_id)
    if result.status_code == 404:
        raise HTTPException(status_code=404, detail=result.summary)
    if result.status_code == 409:
        raise HTTPException(status_code=409, detail=result.summary)
    if result.status_code == 400:
        raise HTTPException(status_code=400, detail=result.summary)
    if result.approval is None:
        raise HTTPException(status_code=500, detail="Approval execution did not return state")
    await app.state.bus.publish(
        {
            "channel": "approvals",
            "approval_id": approval_id,
            "status": result.approval["status"],
            "executed": True,
            "ok": result.ok,
        }
    )
    return ApprovalExecutionResponse(
        approval=ApprovalItem.model_validate(result.approval),
        ok=result.ok,
        summary=result.summary,
        data=result.data,
    )


@app.get("/api/tools", response_model=list[ToolInfo])
async def list_tools() -> list[ToolInfo]:
    return app.state.agent.tools.list()


@app.post("/api/tools/{tool_name}/run", response_model=ToolRunResponse)
async def run_tool(tool_name: str, request: ToolRunRequest) -> ToolRunResponse:
    # Public tool runs must not be an approval bypass. The dedicated
    # ApprovalExecutor is the only API path that may pass allow_danger=True.
    return await app.state.agent.tools.run(
        tool_name,
        request.arguments,
        allow_danger=False,
    )


@app.get("/api/tool-runs")
async def list_tool_runs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    return app.state.storage.list_tool_runs(limit=limit)


@app.post("/api/diagnostics", response_model=DiagnosticsResponse)
async def diagnostics() -> DiagnosticsResponse:
    result = await run_diagnostics(
        settings=app.state.settings,
        storage=app.state.storage,
        llm=app.state.llm,
    )
    await app.state.bus.publish({"channel": "diagnostics", "ok": result.ok})
    return result


@app.post("/api/self-heal", response_model=SelfHealResponse)
async def self_heal() -> SelfHealResponse:
    result = await run_diagnostics(
        settings=app.state.settings,
        storage=app.state.storage,
        llm=app.state.llm,
    )
    telemetry_snapshot = await asyncio.to_thread(app.state.telemetry.snapshot)
    app.state.storage.record_telemetry(telemetry_snapshot)
    dispatcher_status = await asyncio.to_thread(app.state.dispatcher.status)
    report = app.state.experience.self_heal_report(
        checks=result.checks,
        telemetry_snapshot=telemetry_snapshot,
        dispatcher_status=dispatcher_status,
    )
    await app.state.bus.publish(
        {"channel": "self-heal", "ok": report["ok"], "actions": len(report["actions"])}
    )
    return report


@app.post("/api/benchmark", response_model=BenchmarkResponse)
async def benchmark() -> BenchmarkResponse:
    report = await app.state.experience.run_benchmark(
        llm=app.state.llm,
        telemetry=app.state.telemetry,
        dispatcher=app.state.dispatcher,
    )
    await app.state.bus.publish(
        {"channel": "benchmark", "summary": report["summary"], "profile": report["profile"]}
    )
    return report


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    host = websocket.client.host if websocket.client else ""
    token = _header_token(websocket.headers) or str(websocket.query_params.get("token") or "")
    if not _is_loopback_host(host) and not _token_allowed(token):
        await websocket.close(code=1008)
        return
    bus: EventBus = app.state.bus
    await bus.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await bus.disconnect(websocket)
