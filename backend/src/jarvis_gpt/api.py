from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .agent import AgentRuntime
from .approval_executor import ApprovalExecutor
from .config import ensure_runtime_dirs, load_settings
from .diagnostics import run_diagnostics
from .dispatcher import DispatcherManager
from .event_bus import EventBus
from .host_bridge import HostBridgeStatus
from .ingest import FileIngestor
from .learning import LearningEngine
from .llm import LLMRouter
from .model_catalog import ModelCatalog
from .models import (
    ApprovalCreateRequest,
    ApprovalExecutionResponse,
    ApprovalItem,
    ApprovalUpdateRequest,
    AuditEntry,
    AutonomyStatusResponse,
    ChatRequest,
    ChatResponse,
    ConversationItem,
    DiagnosticsResponse,
    DispatcherActionResponse,
    DispatcherStatusResponse,
    FileChunkHit,
    FileIngestResponse,
    FileItem,
    HostBridgeResponse,
    LearningTickResponse,
    MemoryCreateRequest,
    MemoryItem,
    MessageItem,
    Mission,
    MissionCreateRequest,
    MissionExecutionResponse,
    MissionTask,
    MissionTaskUpdateRequest,
    ModelCatalogResponse,
    StatusResponse,
    TelemetryResponse,
    ToolInfo,
    ToolRunRequest,
    ToolRunResponse,
)
from .storage import JarvisStorage
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
    models = ModelCatalog(settings)
    dispatcher = DispatcherManager(settings)
    telemetry = TelemetryCollector(settings)
    learning = LearningEngine(storage)
    host_bridge = HostBridgeStatus(settings)
    supervisor = RuntimeSupervisor(settings=settings, storage=storage, llm=llm)
    approval_executor = ApprovalExecutor(
        storage=storage,
        llm=llm,
        dispatcher=dispatcher,
        tools=agent.tools,
    )

    app.state.settings = settings
    app.state.storage = storage
    app.state.llm = llm
    app.state.bus = bus
    app.state.agent = agent
    app.state.ingestor = ingestor
    app.state.models = models
    app.state.dispatcher = dispatcher
    app.state.telemetry = telemetry
    app.state.learning = learning
    app.state.host_bridge = host_bridge
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


app = FastAPI(title="JARVIS GPT", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.get("/api/models", response_model=ModelCatalogResponse)
async def models() -> ModelCatalogResponse:
    return app.state.models.response()


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
    snapshot = app.state.telemetry.snapshot()
    app.state.storage.record_telemetry(snapshot)
    return snapshot


@app.post("/api/learning/tick", response_model=LearningTickResponse)
async def learning_tick() -> LearningTickResponse:
    result = app.state.learning.tick()
    await app.state.bus.publish({"channel": "learning", "lesson_count": result["lesson_count"]})
    return result


@app.get("/api/host-bridge", response_model=HostBridgeResponse)
async def host_bridge() -> HostBridgeResponse:
    return app.state.host_bridge.snapshot()


@app.get("/api/autonomy", response_model=AutonomyStatusResponse)
async def autonomy() -> AutonomyStatusResponse:
    return app.state.supervisor.status()


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    return await app.state.agent.chat(
        request.message,
        conversation_id=request.conversation_id,
        mode=request.mode,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    async def lines():
        async for item in app.state.agent.stream_chat(
            request.message,
            conversation_id=request.conversation_id,
            mode=request.mode,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        ):
            yield f"{json.dumps(item, ensure_ascii=False)}\n".encode()

    return StreamingResponse(lines(), media_type="application/x-ndjson")


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
        title=request.title,
        status=request.status,
        notes=request.notes,
    )
    if updated is None or updated["mission_id"] != mission_id:
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


@app.get("/api/files/search", response_model=list[FileChunkHit])
async def search_files(
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=12, ge=1, le=50),
) -> list[FileChunkHit]:
    return app.state.storage.search_file_chunks(q, limit=limit)


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
    return await app.state.agent.tools.run(
        tool_name,
        request.arguments,
        allow_danger=request.allow_danger,
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


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    bus: EventBus = app.state.bus
    await bus.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await bus.disconnect(websocket)
