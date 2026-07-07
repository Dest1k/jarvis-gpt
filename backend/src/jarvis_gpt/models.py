from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ApiEnvelope(BaseModel):
    ok: bool = True
    state: Literal["loading", "processing", "success", "error"] = "success"
    data: dict[str, Any] | list[Any] | None = None
    error: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: str | None = None
    mode: Literal["auto", "chat", "mission"] = "auto"


class ChatEvent(BaseModel):
    type: str
    title: str
    content: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    conversation_id: str
    message_id: str
    answer: str
    events: list[ChatEvent]
    mission_id: str | None = None


class MissionCreateRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=20000)
    title: str | None = Field(default=None, max_length=240)


class MissionTaskUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    status: Literal["pending", "running", "done", "blocked", "skipped"] | None = None
    notes: str | None = Field(default=None, max_length=20000)


class MissionTask(BaseModel):
    id: str
    mission_id: str
    title: str
    status: str
    notes: str | None = None
    position: int
    created_at: str
    updated_at: str


class Mission(BaseModel):
    id: str
    title: str
    goal: str
    status: str
    progress: float
    created_at: str
    updated_at: str
    tasks: list[MissionTask] = Field(default_factory=list)


class MemoryCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    namespace: str = Field(default="core", max_length=80)
    tags: list[str] = Field(default_factory=list)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


class MemoryItem(BaseModel):
    id: str
    namespace: str
    content: str
    tags: list[str]
    importance: float
    created_at: str
    updated_at: str
    rank: float | None = None


class FileItem(BaseModel):
    id: str
    name: str
    source_path: str | None = None
    stored_path: str
    mime_type: str
    size: int
    sha256: str
    status: str
    error: str | None = None
    chunk_count: int
    created_at: str
    updated_at: str


class FileChunkHit(BaseModel):
    file_id: str
    file_name: str
    chunk_id: str
    position: int
    content: str
    created_at: str
    rank: float | None = None


class FileIngestResponse(BaseModel):
    file: FileItem
    chunks_indexed: int


class ModelArtifact(BaseModel):
    id: str
    path: str
    exists: bool
    active: bool
    size_bytes: int
    shard_count: int
    modified_at: str | None = None
    model_type: str | None = None
    architectures: list[str] = Field(default_factory=list)
    dtype: str | None = None
    quantization: str | None = None
    metadata: dict[str, bool] = Field(default_factory=dict)
    generation: dict[str, Any] = Field(default_factory=dict)


class ModelCatalogResponse(BaseModel):
    root: str
    active_profile: str
    active_model: ModelArtifact
    models: list[ModelArtifact]
    dispatcher: dict[str, Any]


class DispatcherStatusResponse(BaseModel):
    service: str
    container: str
    docker_available: bool
    docker_path: str | None = None
    port: int
    port_open: bool
    base_url: str
    model: str
    active_model: dict[str, Any]
    compose: list[str]
    container_status: dict[str, Any] | None = None
    env: dict[str, str]


class AuditEntry(BaseModel):
    id: str
    ts: str
    actor: str
    action: str
    target_type: str
    target_id: str | None = None
    summary: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)


class ApprovalCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1, max_length=20000)
    requested_action: str = Field(min_length=1, max_length=120)
    risk: Literal["review", "danger"] = "review"
    payload: dict[str, Any] = Field(default_factory=dict)


class ApprovalUpdateRequest(BaseModel):
    status: Literal["approved", "rejected", "executed", "cancelled"]
    result: dict[str, Any] = Field(default_factory=dict)


class ApprovalItem(BaseModel):
    id: str
    created_at: str
    updated_at: str
    status: str
    risk: str
    title: str
    description: str
    requested_action: str
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)


class ToolInfo(BaseModel):
    name: str
    description: str
    category: str
    input_schema: dict[str, Any]
    danger_level: Literal["safe", "review", "danger"] = "safe"


class ToolRunRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolRunResponse(BaseModel):
    tool: str
    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class MissionExecutionResponse(BaseModel):
    mission: Mission
    task: MissionTask | None = None
    result: ToolRunResponse


class DiagnosticCheck(BaseModel):
    name: str
    status: Literal["ok", "warn", "error"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DiagnosticsResponse(BaseModel):
    ok: bool
    checks: list[DiagnosticCheck]


class StatusResponse(BaseModel):
    settings: dict[str, Any]
    counters: dict[str, int]
    health: list[DiagnosticCheck]
    recent_events: list[dict[str, Any]]
