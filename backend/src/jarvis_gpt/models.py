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
