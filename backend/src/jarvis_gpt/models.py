from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ApiEnvelope(BaseModel):
    ok: bool = True
    state: Literal["loading", "processing", "success", "error"] = "success"
    data: dict[str, Any] | list[Any] | None = None
    error: str | None = None


class ChatAttachment(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=500)
    mime_type: str | None = Field(default=None, max_length=200)
    size: int | None = Field(default=None, ge=0)
    url: str | None = Field(default=None, max_length=1000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    conversation_id: str | None = None
    mode: Literal["auto", "chat", "mission"] = "auto"
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    attachments: list[ChatAttachment] = Field(default_factory=list, max_length=8)
    thinking_enabled: bool = True


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
    duration_ms: int | None = None


class ConversationItem(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


class MessageItem(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class MessageFeedbackRequest(BaseModel):
    rating: Literal["up", "down"]
    comment: str = Field(default="", max_length=600)


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
    relevance: float | None = None
    snippet: str | None = None
    matched_terms: list[str] = Field(default_factory=list)


class MemoryGraphNode(BaseModel):
    id: str
    label: str
    kind: str
    path: str | None = None
    namespace: str | None = None
    tags: list[str] = Field(default_factory=list)
    importance: float | None = None
    updated_at: str | None = None
    degree: int | None = None


class MemoryGraphEdge(BaseModel):
    source: str
    target: str
    kind: str


class MemoryVaultNote(BaseModel):
    id: str | None = None
    title: str
    path: str
    namespace: str | None = None
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    importance: float | None = None
    updated_at: str | None = None
    content: str = ""


class MemoryVaultResponse(BaseModel):
    root: str
    notes: list[MemoryVaultNote]
    nodes: list[MemoryGraphNode]
    edges: list[MemoryGraphEdge]
    backlinks: dict[str, list[str]] = Field(default_factory=dict)
    top_nodes: list[MemoryGraphNode] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)


class MemoryHygieneResponse(BaseModel):
    stats: dict[str, int | float] = Field(default_factory=dict)
    recommendations: list[str] = Field(default_factory=list)
    duplicate_groups: list[dict[str, Any]] = Field(default_factory=list)


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
    relevance: float | None = None
    snippet: str | None = None
    matched_terms: list[str] = Field(default_factory=list)


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
    fit: dict[str, Any] = Field(default_factory=dict)


class ModelCatalogResponse(BaseModel):
    root: str
    active_profile: str
    active_model: ModelArtifact
    models: list[ModelArtifact]
    dispatcher: dict[str, Any]
    vram: dict[str, Any] = Field(default_factory=dict)
    downloads: list[dict[str, Any]] = Field(default_factory=list)


class ModelSearchResponse(BaseModel):
    query: str
    items: list[dict[str, Any]]
    vram: dict[str, Any] = Field(default_factory=dict)
    token_available: bool = False


class ModelProfilePlan(BaseModel):
    id: str
    title: str
    role: str
    status: Literal["active", "available", "future"]
    model_hint: str
    notes: list[str] = Field(default_factory=list)


class ModelProfilesResponse(BaseModel):
    active_profile: str
    active_model: str
    profiles: list[ModelProfilePlan] = Field(default_factory=list)


class ModelDownloadRequest(BaseModel):
    repo_id: str = Field(min_length=3, max_length=240)
    revision: str = Field(default="main", max_length=120)
    workers: int = Field(default=3, ge=1, le=6)


class ModelActivateRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=240)


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
    desired_model: dict[str, Any] | None = None
    runtime: dict[str, Any] | None = None
    desired_runtime: dict[str, Any] = Field(default_factory=dict)
    actual_image: str = ""
    desired_image: str = ""
    runtime_matches_desired: bool = False
    runtime_mismatches: dict[str, Any] = Field(default_factory=dict)
    compose: list[str]
    container_status: dict[str, Any] | None = None
    env: dict[str, str]


class DispatcherActionResponse(BaseModel):
    ok: bool
    summary: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    command: list[str] = Field(default_factory=list)
    verification: dict[str, Any] | None = None
    status: DispatcherStatusResponse


class TelemetryResponse(BaseModel):
    ts: str
    host: dict[str, Any]
    memory: dict[str, Any]
    disks: list[dict[str, Any]]
    gpu: dict[str, Any]
    docker: dict[str, Any]
    performance: dict[str, Any]


class LearningTickResponse(BaseModel):
    saved: list[MemoryItem]
    lesson_count: int
    skipped_duplicates: int = 0
    consolidated: dict[str, int] = Field(default_factory=dict)
    examined: dict[str, int]


class HostBridgeResponse(BaseModel):
    name: str
    host: str
    port: int
    port_open: bool
    token_path: str | None = None
    token_available: bool
    script_path: str
    deployed_script_path: str | None = None
    bundled_script_path: str | None = None
    script_available: bool
    start_command: str
    native_capabilities: list[str] = Field(default_factory=list)


class AutonomyStatusResponse(BaseModel):
    enabled: bool
    started_at: str | None = None
    running_tasks: list[str] = Field(default_factory=list)
    telemetry_interval_sec: int
    health_interval_sec: int
    learning_interval_sec: int
    cognition_enabled: bool = True
    cognition_interval_sec: int = 300
    cognition_max_tokens: int = 512
    mission_interval_sec: int = 120
    last_telemetry_at: str | None = None
    last_health_at: str | None = None
    last_learning_at: str | None = None
    last_cognition_at: str | None = None
    last_cognition_error: str | None = None
    last_background_job_at: str | None = None
    last_error: str | None = None
    capabilities: list[str] = Field(default_factory=list)


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
    status: Literal["approved", "rejected", "cancelled"]
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


class ApprovalExecutionResponse(BaseModel):
    approval: ApprovalItem
    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class ToolInfo(BaseModel):
    name: str
    description: str
    category: str
    input_schema: dict[str, Any]
    danger_level: Literal["safe", "review", "danger"] = "safe"


class ToolRunRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    allow_danger: bool = False


class ToolRunResponse(BaseModel):
    tool: str
    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class MissionExecutionResponse(BaseModel):
    mission: Mission
    task: MissionTask | None = None
    result: ToolRunResponse


class MissionStepOutcome(BaseModel):
    task: MissionTask | None = None
    result: ToolRunResponse


class MissionRunResponse(BaseModel):
    mission: Mission
    steps: list[MissionStepOutcome] = Field(default_factory=list)
    completed: bool = False
    stopped_reason: Literal["completed", "blocked", "budget", "busy", "empty"] = "completed"
    executed_steps: int = 0
    final_report: str | None = None


class OperatorQueueItem(BaseModel):
    id: str
    kind: Literal[
        "approval",
        "mission",
        "health",
        "generation",
        "memory",
        "model",
        "quality",
        "autonomy",
    ]
    status: str
    title: str
    detail: str = ""
    priority: Literal["high", "medium", "low"] = "medium"
    action: str | None = None
    updated_at: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class OperatorQueueResponse(BaseModel):
    summary: dict[str, int] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    items: list[OperatorQueueItem] = Field(default_factory=list)
    memory_hygiene: MemoryHygieneResponse
    model_profiles: ModelProfilesResponse


class DiagnosticCheck(BaseModel):
    name: str
    status: Literal["ok", "warn", "error"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DiagnosticsResponse(BaseModel):
    ok: bool
    checks: list[DiagnosticCheck]


class RuntimePreferencesResponse(BaseModel):
    operator_name: str
    communication_style: Literal["concise", "balanced", "detailed"]
    daily_briefing: bool
    voice_reply: bool
    preferred_profile: Literal["gemma4-turbo", "gemma4-mono"]
    quiet_hours: str
    working_roots: list[str] = Field(default_factory=list)


class RuntimePreferencesUpdateRequest(BaseModel):
    operator_name: str | None = Field(default=None, max_length=80)
    communication_style: Literal["concise", "balanced", "detailed"] | None = None
    daily_briefing: bool | None = None
    voice_reply: bool | None = None
    preferred_profile: Literal["gemma4-turbo", "gemma4-mono"] | None = None
    quiet_hours: str | None = Field(default=None, max_length=80)
    working_roots: list[str] | None = None


class AutonomyPolicyResponse(BaseModel):
    mode: Literal["safe", "balanced", "operator"]
    allow_safe_tools: bool
    allow_review_tools: bool
    allow_danger_tools: bool
    allow_background_learning: bool
    allow_self_healing_suggestions: bool
    approval_required_for: list[str] = Field(default_factory=list)
    max_autonomous_steps: int
    resource_guard: dict[str, float] = Field(default_factory=dict)


class AutonomyPolicyUpdateRequest(BaseModel):
    mode: Literal["safe", "balanced", "operator"] | None = None
    allow_safe_tools: bool | None = None
    allow_review_tools: bool | None = None
    allow_danger_tools: bool | None = None
    allow_background_learning: bool | None = None
    allow_self_healing_suggestions: bool | None = None
    approval_required_for: list[str] | None = None
    max_autonomous_steps: int | None = Field(default=None, ge=1, le=24)
    resource_guard: dict[str, float] | None = None


class DailyBriefingResponse(BaseModel):
    ts: str
    operator_name: str
    profile: str
    home: str
    headline: str
    focus: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    pending_approvals: int
    policy_mode: str
    counters: dict[str, int] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict)
    recent_events: list[dict[str, Any]] = Field(default_factory=list)


class SelfHealIssue(BaseModel):
    check: str
    status: Literal["warn", "error"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class SelfHealAction(BaseModel):
    id: str
    label: str
    kind: Literal["safe", "approval"]
    risk: Literal["safe", "review", "danger"]
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SelfHealResponse(BaseModel):
    ts: str
    ok: bool
    summary: str
    issues: list[SelfHealIssue] = Field(default_factory=list)
    actions: list[SelfHealAction] = Field(default_factory=list)
    checks: list[DiagnosticCheck] = Field(default_factory=list)


class BenchmarkResponse(BaseModel):
    ts: str
    profile: str
    summary: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    telemetry: dict[str, Any] = Field(default_factory=dict)
    dispatcher: dict[str, Any] = Field(default_factory=dict)
    llm: dict[str, Any] = Field(default_factory=dict)
    recommendations: list[str] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)


class BrowserPolicyResponse(BaseModel):
    mode: Literal["open", "approval-only", "local-safe", "locked"]
    allow_localhost: bool
    allowed_hosts: list[str] = Field(default_factory=list)
    blocked_schemes: list[str] = Field(default_factory=list)
    require_approval_for_external: bool
    max_urls_per_action: int


class BrowserPolicyUpdateRequest(BaseModel):
    mode: Literal["open", "approval-only", "local-safe", "locked"] | None = None
    allow_localhost: bool | None = None
    allowed_hosts: list[str] | None = None
    blocked_schemes: list[str] | None = None
    require_approval_for_external: bool | None = None
    max_urls_per_action: int | None = Field(default=None, ge=1, le=20)


class DockerPolicyResponse(BaseModel):
    allowed_prefixes: list[str] = Field(default_factory=list)
    allowed_containers: list[str] = Field(default_factory=list)
    max_log_tail: int
    include_stopped: bool


class DockerPolicyUpdateRequest(BaseModel):
    allowed_prefixes: list[str] | None = None
    allowed_containers: list[str] | None = None
    max_log_tail: int | None = Field(default=None, ge=10, le=1000)
    include_stopped: bool | None = None


class DockerContainersResponse(BaseModel):
    ok: bool
    summary: str
    policy: DockerPolicyResponse
    containers: list[dict[str, Any]] = Field(default_factory=list)
    command: list[str] = Field(default_factory=list)
    error: str | None = None


class CleanupRequest(BaseModel):
    aggressive: bool = False


class AutonomyJobCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    kind: Literal[
        "diagnostics",
        "learning.tick",
        "self_heal",
        "benchmark",
        "mission",
    ] = "diagnostics"
    cadence: str = Field(default="manual", max_length=80)
    budget: dict[str, int] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=0, le=100)
    deadline_at: str | None = Field(default=None, max_length=80)


class AutonomyJobUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    status: Literal["enabled", "paused", "done", "cancelled"] | None = None
    cadence: str | None = Field(default=None, max_length=80)
    budget: dict[str, int] | None = None
    payload: dict[str, Any] | None = None
    priority: int | None = Field(default=None, ge=0, le=100)
    deadline_at: str | None = Field(default=None, max_length=80)


class AutonomyJobResponse(BaseModel):
    id: str
    title: str
    kind: str
    status: str
    cadence: str
    budget: dict[str, int] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    run_count: int
    priority: int = 0
    consecutive_failures: int = 0
    created_at: str
    updated_at: str
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_duration_ms: int | None = None
    last_run_at: str | None = None
    next_run_after: str | None = None
    running_lease_id: str | None = None
    running_started_at: str | None = None
    running_lease_until: str | None = None
    deadline_at: str | None = None
    cancelled_at: str | None = None
    last_result: dict[str, Any] = Field(default_factory=dict)


class AutonomyJobRunResponse(BaseModel):
    job: AutonomyJobResponse
    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class RoutineResponse(BaseModel):
    id: str
    title: str
    description: str
    steps: list[str] = Field(default_factory=list)


class RoutineRunResponse(BaseModel):
    routine: RoutineResponse
    ok: bool
    summary: str
    results: list[dict[str, Any]] = Field(default_factory=list)


class DirectoryIngestRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    max_files: int = Field(default=50, ge=1, le=500)


class DirectoryIngestResponse(BaseModel):
    root: str
    files_seen: int
    files_indexed: int
    files_failed: int
    results: list[FileIngestResponse] = Field(default_factory=list)
    errors: list[dict[str, str]] = Field(default_factory=list)


class StatusResponse(BaseModel):
    settings: dict[str, Any]
    counters: dict[str, int]
    health: list[DiagnosticCheck]
    recent_events: list[dict[str, Any]]


class OperatorPersonaResponse(BaseModel):
    display_name: str = ""
    headline: str = ""
    role: str = ""
    location: str = ""
    timezone: str = ""
    languages: list[str] = Field(default_factory=list)
    expertise: list[str] = Field(default_factory=list)
    tech_stack: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    current_focus: list[str] = Field(default_factory=list)
    standing_instructions: list[str] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    notes: str = ""


class OperatorPersonaUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    headline: str | None = Field(default=None, max_length=240)
    role: str | None = Field(default=None, max_length=160)
    location: str | None = Field(default=None, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    languages: list[str] | None = None
    expertise: list[str] | None = None
    tech_stack: list[str] | None = None
    interests: list[str] | None = None
    current_focus: list[str] | None = None
    standing_instructions: list[str] | None = None
    glossary: dict[str, str] | None = None
    notes: str | None = Field(default=None, max_length=800)


class OperatorPersonaInsightRequest(BaseModel):
    field: Literal[
        "languages",
        "expertise",
        "tech_stack",
        "interests",
        "current_focus",
        "standing_instructions",
    ]
    value: str = Field(min_length=1, max_length=160)
