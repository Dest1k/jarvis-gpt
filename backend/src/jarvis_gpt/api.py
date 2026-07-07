from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .agent import AgentRuntime
from .config import ensure_runtime_dirs, load_settings
from .diagnostics import run_diagnostics
from .event_bus import EventBus
from .llm import LLMRouter
from .models import (
    ChatRequest,
    ChatResponse,
    DiagnosticsResponse,
    MemoryCreateRequest,
    MemoryItem,
    Mission,
    MissionCreateRequest,
    StatusResponse,
)
from .storage import JarvisStorage


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = LLMRouter(settings)
    bus = EventBus()
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, bus=bus)

    app.state.settings = settings
    app.state.storage = storage
    app.state.llm = llm
    app.state.bus = bus
    app.state.agent = agent
    storage.add_event(kind="runtime.start", title="JARVIS GPT backend started")
    try:
        yield
    finally:
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


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    return await app.state.agent.chat(
        request.message,
        conversation_id=request.conversation_id,
        mode=request.mode,
    )


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
