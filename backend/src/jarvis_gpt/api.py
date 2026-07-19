from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import tempfile
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from starlette.routing import Match

from . import speech
from .admin_api import ADMIN_API_CAPABILITIES
from .admin_api import router as admin_router
from .agent import (
    AgentRuntime,
    ChatRequestConflictError,
    ChatRequestInProgressError,
    ChatUnavailableError,
)
from .approval_executor import ApprovalExecutor
from .authorization import (
    CORE_CAPABILITIES,
    LEGACY_OWNER_USER_ID,
    ActorContext,
    AuthorizationError,
    AuthorizationService,
    CapabilityDefinition,
    ResourceIsolationError,
    bind_actor,
    current_actor,
)
from .autonomy_executor import AutonomyExecutor
from .cognitive_memory import ExecutionPlaybookStore, HostProfileManager
from .config import ensure_runtime_dirs, load_settings
from .diagnostics import run_diagnostics
from .dispatcher import DispatcherManager
from .event_bus import EventBus
from .executive_runtime import ExecutiveCoordinator
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
    VoiceSpeakRequest,
)
from .operations import OperationsManager
from .operator_queue import (
    answer_quality_report,
    memory_hygiene_report,
    model_profile_plan,
    operator_queue_snapshot,
)
from .persona import PersonaManager
from .runtime_lease import PrimaryRuntimeLease
from .storage import JarvisStorage, utc_now
from .supervisor import RuntimeSupervisor
from .telemetry import TelemetryCollector
from .tools import ToolRegistry, browser_handoff_snapshot, internet_observability_snapshot
from .web_surfer_adapter import WebSurferAdapter

LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    configured_api_token = _api_token()
    if configured_api_token and len(configured_api_token) < 32:
        raise RuntimeError("JARVIS_API_TOKEN must contain at least 32 characters")
    if settings.api_require_token_on_loopback and not configured_api_token:
        raise RuntimeError(
            "JARVIS_API_TOKEN is required when loopback authentication is enabled"
        )
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    playbooks: ExecutionPlaybookStore | None = None
    web_surfer: WebSurferAdapter | None = None
    model_hub: ModelHubManager | None = None
    supervisor: RuntimeSupervisor | None = None
    primary_lease = PrimaryRuntimeLease(settings.state_dir / "primary-runtime.lock")
    runtime_started = False
    resources_closed = False
    resources_closing: asyncio.Task[None] | None = None

    async def run_lease_owned_thread(call, /, *args, **kwargs):
        """Do not release the primary lease while its worker thread still mutates state."""

        operation = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
        current = asyncio.current_task()
        cancellation_requested = False
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                cancellation_requested = True
                if current is not None:
                    while current.cancelling():
                        current.uncancel()
        if cancellation_requested:
            with suppress(BaseException):
                operation.result()
            raise asyncio.CancelledError
        return operation.result()

    async def close_resources_impl() -> None:
        nonlocal resources_closed
        if supervisor is not None:
            with suppress(Exception, asyncio.CancelledError):
                await supervisor.stop()
        if model_hub is not None:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.to_thread(model_hub.close)
        if web_surfer is not None:
            with suppress(Exception, asyncio.CancelledError):
                await web_surfer.aclose()
        if playbooks is not None:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.to_thread(playbooks.close)
        if runtime_started:
            with suppress(Exception):
                storage.add_event(kind="runtime.stop", title="Jarvis backend stopped")
        try:
            with suppress(Exception):
                storage.close()
        finally:
            with suppress(Exception):
                primary_lease.release()
            resources_closed = True

    async def close_resources() -> None:
        nonlocal resources_closing
        if resources_closed:
            return
        if resources_closing is None:
            resources_closing = asyncio.create_task(close_resources_impl())
        cleanup = resources_closing
        current = asyncio.current_task()
        cancellation_requested = False
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                if cleanup.cancelled():
                    raise
                cancellation_requested = True
                if current is not None:
                    while current.cancelling():
                        current.uncancel()
        cleanup.result()
        if cancellation_requested:
            raise asyncio.CancelledError

    try:
        primary_lease.acquire()
        storage.initialize()
        authorization = AuthorizationService(storage)
        authorization.sync_capabilities(CORE_CAPABILITIES, catalog_key="core.v1")
        authorization.sync_capabilities(
            ADMIN_API_CAPABILITIES,
            catalog_key="http.admin.v1",
        )
        authorization.sync_capabilities(
            HTTP_API_CAPABILITIES,
            catalog_key="http.api.v1",
        )
        authorization.prune_ephemeral_security_state(force=True)
        # This is the designated primary runtime.  Fail closed acquired approvals
        # before any mission recovery can consider their interrupted side effects.
        # It is inside the resource guard so a database failure still closes storage.
        storage.recover_interrupted_approval_executions()
        profile_manager = HostProfileManager(settings.home / "host_profile.json")
        try:
            host_profile = await run_lease_owned_thread(profile_manager.refresh)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            host_profile = await run_lease_owned_thread(
                profile_manager.load_verified,
                max_age_seconds=86_400,
            )
            if host_profile is None:
                raise RuntimeError(
                    "cold-start host fingerprint could not be collected or recovered"
                ) from exc
        storage.set_runtime_value("environment.host_profile", host_profile)
        playbooks = ExecutionPlaybookStore(
            settings.state_dir / "execution-playbooks.sqlite3"
        )
        llm = LLMRouter(settings)
        bus = EventBus()
        web_surfer = WebSurferAdapter()
        await web_surfer.start()
        executive = ExecutiveCoordinator(
            storage=storage,
            host_profile=host_profile,
            playbooks=playbooks,
            recover_interrupted=True,
        )
        tools = ToolRegistry(
            settings,
            storage,
            llm,
            playbooks=playbooks,
            web_surfer=web_surfer,
            executive=executive,
            recover_execution=True,
        )
        agent = AgentRuntime(
            settings=settings,
            storage=storage,
            llm=llm,
            bus=bus,
            tools=tools,
            playbooks=playbooks,
            host_profile=host_profile,
            executive=executive,
        )
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
            bus=bus,
            dispatcher=dispatcher,
        )
        approval_executor = ApprovalExecutor(
            storage=storage,
            llm=llm,
            dispatcher=dispatcher,
            tools=agent.tools,
            mission_resumer=agent.resume_mission_after_approval,
            mission_aborter=agent.abort_mission_after_approval,
        )
        await approval_executor.reconcile_interrupted_executions()

        app.state.settings = settings
        app.state.storage = storage
        app.state.authorization = authorization
        app.state.llm = llm
        app.state.bus = bus
        app.state.agent = agent
        app.state.host_profile = host_profile
        app.state.host_profile_manager = profile_manager
        app.state.playbooks = playbooks
        app.state.web_surfer = web_surfer
        app.state.executive = executive
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
        app.state.autonomy_background_tasks = set()
        storage.add_event(kind="runtime.start", title="Jarvis backend started")
        await supervisor.start()
        runtime_started = True
    except BaseException:
        await close_resources()
        raise
    try:
        yield
    finally:
        detached_tasks = [
            task
            for task in getattr(app.state, "autonomy_background_tasks", set())
            if not task.done()
        ]
        for task in detached_tasks:
            task.cancel()
        if detached_tasks:
            await asyncio.gather(*detached_tasks, return_exceptions=True)
        await close_resources()


_WS_TOKEN_PROTOCOL_PREFIX = "jarvis.token."


def _normalize_origin(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return ""
    scheme = parsed.scheme.lower()
    host = parsed.hostname.rstrip(".").lower()
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 80 if scheme == "http" else 443
    rendered_port = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{rendered_host}{rendered_port}"


def _cors_origins() -> list[str]:
    raw = os.environ.get("JARVIS_CORS_ORIGINS", "")
    origins = {_normalize_origin(item) for item in raw.split(",")}
    return sorted(origin for origin in origins if origin)


def _api_token() -> str:
    return os.environ.get("JARVIS_API_TOKEN", "").strip()


def _authenticated_user_rate_limit() -> int:
    try:
        value = int(os.environ.get("JARVIS_API_USER_RATE_LIMIT_PER_MINUTE", "240"))
    except ValueError:
        value = 240
    return max(1, min(value, 100_000))


def _bridge_secret() -> str:
    return os.environ.get("JARVIS_TELEGRAM_BRIDGE_SECRET", "").strip()


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


def _origin_allowed(origin: str) -> bool:
    normalized = _normalize_origin(origin)
    if not normalized:
        return False
    hostname = urlsplit(normalized).hostname
    return _is_loopback_host(hostname) or normalized in _cors_origins()


@lru_cache(maxsize=1)
def _local_interface_addresses() -> frozenset[str]:
    addresses: set[str] = {"127.0.0.1", "::1"}
    hostnames = {socket.gethostname(), socket.getfqdn()}
    for hostname in {item for item in hostnames if item}:
        try:
            for result in socket.getaddrinfo(hostname, None):
                address = str(result[4][0]).split("%", 1)[0].lower()
                if address:
                    addresses.add(address)
        except OSError:
            continue
    return frozenset(addresses)


def _is_local_machine_host(host: str | None) -> bool:
    if _is_loopback_host(host):
        return True
    if not host:
        return False
    normalized = host.strip().strip("[]").split("%", 1)[0].lower()
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return str(address).lower() in _local_interface_addresses()


def _header_token(headers: Any) -> str:
    auth = str(headers.get("authorization") or "")
    if auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return str(headers.get("x-jarvis-api-token") or "").strip()


def _user_session_token(headers: Any) -> str:
    token = str(headers.get("x-jarvis-user-session") or "").strip()
    return token if len(token) <= 1024 else ""


def _websocket_protocol_token(headers: Any) -> str:
    protocols = str(headers.get("sec-websocket-protocol") or "")
    for raw_protocol in protocols.split(","):
        protocol = raw_protocol.strip()
        if not protocol.startswith(_WS_TOKEN_PROTOCOL_PREFIX):
            continue
        encoded = protocol[len(_WS_TOKEN_PROTOCOL_PREFIX) :]
        if not encoded or len(encoded) > 1024:
            continue
        padding = "=" * (-len(encoded) % 4)
        try:
            token = base64.urlsafe_b64decode(f"{encoded}{padding}").decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if token:
            return token
    return ""


def _token_allowed(token: str) -> bool:
    expected = _api_token()
    return bool(expected and token and secrets.compare_digest(token, expected))


def _track_detached_autonomy_task(task: asyncio.Task[Any], job_id: str) -> None:
    tasks = getattr(app.state, "autonomy_background_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.autonomy_background_tasks = tasks
    tasks.add(task)

    def _discard(done: asyncio.Task[Any]) -> None:
        tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            try:
                app.state.storage.add_event(
                    kind="autonomy.job.detached_error",
                    title=f"Detached autonomy job failed: {job_id}",
                    level="error",
                    payload={"job_id": job_id, "error": repr(exc)},
                )
            except Exception:
                return

    task.add_done_callback(_discard)


def _strict_loopback_token_required() -> bool:
    return bool(app.state.settings.api_require_token_on_loopback)


app = FastAPI(title="Jarvis", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(admin_router)


_TELEGRAM_SESSION_PATHS = {
    "/api/integrations/telegram/session",
    "/api/integrations/telegram/register-session",
}


_HTTP_GUEST_ENDPOINTS = frozenset(
    {
        "chat",
        "chat_stream",
        "interrupted_stream",
        "list_conversations",
        "list_conversation_messages",
        "set_message_feedback",
    }
)

# These handlers operate on resources which are scoped to the authenticated actor.
# Tool execution still passes through the tool-level PEP; this grant only permits the
# HTTP transport to reach that second, more specific authorization decision.
_HTTP_PERSONAL_ENDPOINTS = frozenset(
    {
        "agent_trace",
        "agent_message_trace",
        "preferences",
        "update_preferences",
        "persona",
        "update_persona",
        "add_persona_insight",
        "voice_status",
        "voice_speak",
        "delete_conversation",
        "executive_plan",
        "web_surfer_capabilities",
        "list_missions",
        "create_mission",
        "get_mission",
        "execute_next_mission_step",
        "run_mission",
        "get_mission_report",
        "update_mission_task",
        "search_memory",
        "add_memory",
        "memory_vault",
        "memory_hygiene",
        "memory_consolidate",
        "list_files",
        "upload_file",
        "search_files",
        "download_file",
        "get_file",
        "list_audit",
        "list_approvals",
        "create_approval",
        "update_approval",
        "execute_approval",
        "list_tools",
        "run_tool",
        "list_tool_runs",
    }
)

# Read-only operational state is useful to administrators, but mutations of the
# runtime, model inventory, dispatcher, diagnostics and recovery remain owner-only.
_HTTP_ADMIN_READ_ENDPOINTS = frozenset(
    {
        "status",
        "runtime_security",
        "operator_queue",
        "operator_quality",
        "model_profiles",
        "models",
        "model_hub_search",
        "model_downloads",
        "dispatcher",
        "telemetry",
        "telemetry_live",
        "learning_journal",
        "host_bridge",
        "autonomy",
        "autonomy_policy",
        "browser_policy",
        "browser_handoff",
        "internet_observability",
        "docker_policy",
        "docker_containers",
        "autonomy_jobs",
        "autonomy_job_runs",
        "routines",
        "briefing",
        "environment_profile",
        "execution_playbooks",
    }
)

_HTTP_HIGH_RISK_ENDPOINTS = frozenset(
    {
        "runtime_backup",
        "model_download",
        "model_download_cancel",
        "model_activate",
        "model_delete",
        "dispatcher_action",
        "learning_tick",
        "update_autonomy_policy",
        "update_browser_policy",
        "update_docker_policy",
        "cleanup_runtime",
        "create_autonomy_job",
        "update_autonomy_job",
        "cancel_autonomy_job",
        "run_autonomy_job",
        "start_autonomy_job",
        "run_routine",
        "ingest_directory",
        "delete_conversation",
        "memory_consolidate",
        "diagnostics",
        "self_heal",
        "benchmark",
    }
)
_ROUTE_APPROVAL_TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class _HttpRouteCapability:
    route: APIRoute
    method: str
    path_template: str
    security_id: str


HTTP_API_CAPABILITIES: tuple[CapabilityDefinition, ...] = ()
_HTTP_ROUTE_CAPABILITIES: tuple[_HttpRouteCapability, ...] = ()


def _route_security_id(method: str, path_template: str) -> str:
    segments: list[str] = []
    for raw_segment in path_template.strip("/").split("/"):
        parameter = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]+)?\}", raw_segment)
        if parameter:
            segment = f"by_{parameter.group(1).lower()}"
        else:
            segment = re.sub(r"[^a-z0-9_]+", "_", raw_segment.lower()).strip("_")
        if not segment:
            raise RuntimeError(f"Cannot derive security_id from HTTP path {path_template!r}")
        segments.append(segment)
    return ".".join(("http", method.lower(), *segments))


_HTTP_METHOD_RU = {
    "GET": "Чтение",
    "POST": "Создание/действие",
    "PUT": "Полная замена",
    "PATCH": "Частичное обновление",
    "DELETE": "Удаление",
}


def _http_capability_description(
    method: str, path_template: str, endpoint_name: str
) -> str:
    """Human-readable Russian description for an auto-derived HTTP security_id."""

    verb = _HTTP_METHOD_RU.get(method.upper(), method.upper())
    # Prefer the FastAPI endpoint name when it is descriptive; fall back to path.
    name = (endpoint_name or "").replace("_", " ").strip()
    path = path_template
    if name and name not in {"", path}:
        return (
            f"{verb} через API `{path}` "
            f"(эндпоинт `{endpoint_name}`). Нужен для авторизации этого HTTP-маршрута."
        )
    return (
        f"{verb} через API `{path}`. "
        f"Право на вызов этого HTTP-маршрута (security_id автогенерируется из метода и пути)."
    )


def _http_default_presets(endpoint_name: str) -> tuple[str, ...]:
    if endpoint_name in _HTTP_GUEST_ENDPOINTS:
        return ("guest", "user", "moderator", "admin")
    if endpoint_name in _HTTP_PERSONAL_ENDPOINTS:
        return ("user", "moderator", "admin")
    if endpoint_name in _HTTP_ADMIN_READ_ENDPOINTS:
        return ("admin",)
    return ()


def _http_risk_level(endpoint_name: str, method: str) -> int:
    if endpoint_name in _HTTP_HIGH_RISK_ENDPOINTS:
        return 4
    if method == "DELETE":
        return 3
    if method in {"POST", "PUT", "PATCH"}:
        return 2
    if endpoint_name in _HTTP_ADMIN_READ_ENDPOINTS:
        return 1
    return 0


def _uses_separate_route_authorization(path: str) -> bool:
    return (
        path == "/api/admin"
        or path.startswith("/api/admin/")
        or path in _TELEGRAM_SESSION_PATHS
    )


def _build_http_api_catalog(
    application: FastAPI,
) -> tuple[tuple[CapabilityDefinition, ...], tuple[_HttpRouteCapability, ...]]:
    definitions: list[CapabilityDefinition] = []
    policies: list[_HttpRouteCapability] = []
    security_ids: set[str] = set()
    route_names: set[str] = set()
    for route in application.routes:
        if not isinstance(route, APIRoute):
            continue
        path_template = str(route.path)
        if not path_template.startswith("/api/") or _uses_separate_route_authorization(
            path_template
        ):
            continue
        route_names.add(route.name)
        for method in sorted(set(route.methods or ()) - {"HEAD", "OPTIONS"}):
            security_id = _route_security_id(method, path_template)
            if security_id in security_ids:
                raise RuntimeError(f"Duplicate HTTP security_id: {security_id}")
            security_ids.add(security_id)
            risk_level = _http_risk_level(route.name, method)
            category_segment = path_template.split("/", 3)[2]
            definition = CapabilityDefinition(
                security_id=security_id,
                description=_http_capability_description(
                    method, path_template, route.name
                ),
                category=f"http.{category_segment}",
                risk_level=risk_level,
                default_requires_hitl=route.name in _HTTP_HIGH_RISK_ENDPOINTS,
                source="http_api",
                default_presets=_http_default_presets(route.name),
            )
            definitions.append(definition)
            policies.append(
                _HttpRouteCapability(
                    route=route,
                    method=method,
                    path_template=path_template,
                    security_id=security_id,
                )
            )

    configured_names = (
        _HTTP_GUEST_ENDPOINTS
        | _HTTP_PERSONAL_ENDPOINTS
        | _HTTP_ADMIN_READ_ENDPOINTS
        | _HTTP_HIGH_RISK_ENDPOINTS
    )
    missing_routes = configured_names - route_names
    if missing_routes:
        raise RuntimeError(
            "HTTP authorization policy references unknown route names: "
            + ", ".join(sorted(missing_routes))
        )
    overlapping_defaults = (
        (_HTTP_GUEST_ENDPOINTS & _HTTP_PERSONAL_ENDPOINTS)
        | (_HTTP_GUEST_ENDPOINTS & _HTTP_ADMIN_READ_ENDPOINTS)
        | (_HTTP_PERSONAL_ENDPOINTS & _HTTP_ADMIN_READ_ENDPOINTS)
    )
    if overlapping_defaults:
        raise RuntimeError(
            "HTTP routes have ambiguous built-in grants: "
            + ", ".join(sorted(overlapping_defaults))
        )
    return tuple(definitions), tuple(policies)


def _resolve_http_route_capability(request: Request) -> _HttpRouteCapability | None:
    method = request.method.upper()
    for policy in _HTTP_ROUTE_CAPABILITIES:
        if policy.method != method:
            continue
        match, _ = policy.route.matches(request.scope)
        if match is Match.FULL:
            return policy
    return None


async def _http_authorization_denied(
    request: Request,
    actor: ActorContext,
) -> JSONResponse | None:
    path = request.url.path
    if not path.startswith("/api/") or _uses_separate_route_authorization(path):
        return None
    service = getattr(request.app.state, "authorization", None)
    if not isinstance(service, AuthorizationService):
        return JSONResponse(
            {"detail": "Authorization service is unavailable."},
            status_code=503,
        )
    policy = _resolve_http_route_capability(request)
    security_id = policy.security_id if policy is not None else "http.unmapped"
    decision = service.authorize(
        actor.user_id,
        security_id,
        identity_id=actor.identity_id,
        request_id=request.state.request_id,
        resource_type="http_route",
        resource_ref=path,
        context={
            "method": request.method.upper(),
            "path_template": policy.path_template if policy is not None else None,
        },
    )
    request.state.security_id = security_id
    request.state.authorization_decision = decision
    if not decision.allowed:
        return JSONResponse(
            {
                "detail": {
                    "message": "Permission denied.",
                    "security_id": security_id,
                    "decision_id": decision.decision_id,
                    "reason": decision.reason_code,
                }
            },
            status_code=403,
        )
    if policy is None or policy.route.name not in _HTTP_HIGH_RISK_ENDPOINTS:
        return None

    body_sha256 = hashlib.sha256(await request.body()).hexdigest()
    fingerprint = hashlib.sha256(
        "\n".join(
            (
                request.method.upper(),
                policy.path_template,
                request.url.path,
                request.url.query,
                body_sha256,
            )
        ).encode("utf-8")
    ).hexdigest()
    supplied_approval_id = str(
        request.headers.get("x-jarvis-approval-id") or ""
    ).strip()
    storage: JarvisStorage = request.app.state.storage
    if supplied_approval_id:
        approval = storage.get_approval(supplied_approval_id)
        payload = approval.get("payload") if isinstance(approval, dict) else None
        valid = bool(
            isinstance(approval, dict)
            and approval.get("status") == "approved"
            and approval.get("requested_action") == "http.route.authorize"
            and isinstance(payload, dict)
            and payload.get("protocol") == "jarvis.http-route-approval.v1"
            and payload.get("security_id") == security_id
            and payload.get("request_fingerprint") == fingerprint
            and int(payload.get("policy_epoch") or -1) == decision.policy_epoch
            and _route_approval_is_fresh(approval, payload)
        )
        if valid:
            claimed = storage.claim_approval_execution(supplied_approval_id)
            if claimed is not None:
                request.state.http_approval_id = supplied_approval_id
                return None
        return JSONResponse(
            {
                "detail": {
                    "message": "The route approval is invalid, stale, or already used.",
                    "security_id": security_id,
                }
            },
            status_code=409,
        )

    approval = storage.create_approval(
        title=f"Confirm {request.method.upper()} {policy.path_template}",
        description=(
            "A high-risk HTTP operation requires a separate, one-use human approval."
        ),
        requested_action="http.route.authorize",
        risk="danger",
        payload={
            "protocol": "jarvis.http-route-approval.v1",
            "security_id": security_id,
            "method": request.method.upper(),
            "path_template": policy.path_template,
            "request_fingerprint": fingerprint,
            "body_sha256": body_sha256,
            "policy_epoch": decision.policy_epoch,
            "expires_at": (datetime.now(UTC) + _ROUTE_APPROVAL_TTL).isoformat(
                timespec="seconds"
            ),
        },
    )
    return JSONResponse(
        {
            "detail": {
                "message": "Human approval is required before this operation.",
                "error": "approval_required",
                "approval_id": approval["id"],
                "security_id": security_id,
            }
        },
        status_code=428,
    )


def _route_approval_is_fresh(approval: dict[str, Any], payload: dict[str, Any]) -> bool:
    try:
        created_at = datetime.fromisoformat(str(approval["created_at"]))
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    if created_at.tzinfo is None or expires_at.tzinfo is None:
        return False
    now = datetime.now(UTC)
    return created_at <= now <= expires_at and expires_at - created_at <= _ROUTE_APPROVAL_TTL


async def _call_as_actor(
    request: Request,
    call_next: Any,
    actor: ActorContext,
) -> Response:
    request.state.actor = actor
    with bind_actor(actor):
        denied = await _http_authorization_denied(request, actor)
        if denied is not None:
            denied.headers["X-Jarvis-Request-Id"] = request.state.request_id
            return denied
        try:
            response = await call_next(request)
        except BaseException as exc:
            approval_id = getattr(request.state, "http_approval_id", None)
            if approval_id:
                request.app.state.storage.finalize_approval_execution(
                    approval_id,
                    status="failed",
                    result={"ok": False, "error": type(exc).__name__},
                )
            raise
        approval_id = getattr(request.state, "http_approval_id", None)
        if approval_id:
            final_status = "executed" if response.status_code < 400 else "failed"
            finalized = request.app.state.storage.finalize_approval_execution(
                approval_id,
                status=final_status,
                result={"ok": final_status == "executed", "status_code": response.status_code},
            )
            if finalized is None:
                return JSONResponse(
                    {"detail": "The operation completed but approval finalization failed."},
                    status_code=500,
                )
    response.headers["X-Jarvis-Request-Id"] = request.state.request_id
    return response


@app.middleware("http")
async def local_api_guard(request: Request, call_next):
    request.state.request_id = secrets.token_hex(16)
    if request.method == "OPTIONS" or request.url.path in {"/", "/health"}:
        return await call_next(request)
    host = request.client.host if request.client else ""
    token_ok = _token_allowed(_header_token(request.headers))
    origin = str(request.headers.get("origin") or "").strip()
    if origin and not _origin_allowed(origin):
        return JSONResponse(
            {"detail": "Request Origin is not allowed for the Jarvis API."},
            status_code=403,
        )
    service = getattr(request.app.state, "authorization", None)
    if not isinstance(service, AuthorizationService):
        return JSONResponse(
            {"detail": "Authorization service is unavailable."}, status_code=503
        )
    if request.url.path in _TELEGRAM_SESSION_PATHS:
        expected = _bridge_secret()
        supplied = str(request.headers.get("x-jarvis-bridge-secret") or "").strip()
        reused_secrets = (
            _api_token(),
            os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        )
        if (
            len(expected) < 32
            or any(
                item and secrets.compare_digest(expected, item)
                for item in reused_secrets
            )
        ):
            return JSONResponse(
                {"detail": "Telegram bridge authentication is not configured safely."},
                status_code=503,
            )
        if not supplied or not secrets.compare_digest(supplied, expected):
            return JSONResponse(
                {"detail": "A valid Telegram bridge credential is required."},
                status_code=401,
            )
        integration_actor = service.actor_for_authorized_owner(
            "integration.telegram.session.create",
            source="telegram-bridge",
        )
        if integration_actor is None:
            return JSONResponse(
                {"detail": "No active owner can authorize Telegram registration."},
                status_code=503,
            )
        return await _call_as_actor(request, call_next, integration_actor)

    actor: ActorContext | None = None
    raw_session = str(request.headers.get("x-jarvis-user-session") or "").strip()
    if raw_session:
        session_token = _user_session_token(request.headers)
        if not session_token:
            return JSONResponse({"detail": "Invalid user session."}, status_code=401)
        actor = service.authenticate_session(session_token)
        if actor is None:
            return JSONResponse({"detail": "Invalid or expired user session."}, status_code=401)
    fetch_site = str(request.headers.get("sec-fetch-site") or "").strip().lower()
    if fetch_site == "cross-site" and not (token_ok or actor is not None):
        return JSONResponse(
            {"detail": "Cross-site browser requests require API authentication."},
            status_code=403,
        )
    if actor is not None:
        budget = service.consume_rate_limit(
            scope="api.user",
            subject=actor.user_id,
            limit=_authenticated_user_rate_limit(),
        )
        if not bool(budget["allowed"]):
            retry_after = str(int(budget["retry_after"]))
            return JSONResponse(
                {"detail": "Authenticated user rate limit exceeded."},
                status_code=429,
                headers={"Retry-After": retry_after},
            )
        return await _call_as_actor(request, call_next, actor)
    if token_ok:
        token_actor = service.actor_for_user(
            LEGACY_OWNER_USER_ID, source="api-token"
        )
        if token_actor is None:
            return JSONResponse(
                {"detail": "API token principal is inactive."}, status_code=403
            )
        return await _call_as_actor(request, call_next, token_actor)
    if _is_local_machine_host(host) and not _strict_loopback_token_required():
        local_actor = service.actor_for_user(
            LEGACY_OWNER_USER_ID, source="legacy-local"
        )
        if local_actor is None:
            return JSONResponse(
                {"detail": "Local API principal is inactive."}, status_code=403
            )
        return await _call_as_actor(request, call_next, local_actor)
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
    return {"name": "Jarvis", "status": "online"}


@app.get("/health", response_model=None)
async def health() -> dict[str, object] | JSONResponse:
    try:
        app.state.storage.ping()
        rows = app.state.storage.latest_complete_health(limit=20)
    except Exception as exc:  # noqa: BLE001 - health must report, never mask, storage failure
        payload = {
            "ok": False,
            "profile": app.state.settings.profile.name,
            "error": f"health storage unavailable: {type(exc).__name__}",
        }
        return JSONResponse(status_code=503, content=payload)
    readiness = _health_snapshot_readiness(app.state.settings, rows)
    supervisor = getattr(app.state, "supervisor", None)
    supervisor_status = supervisor.status() if supervisor is not None else {}
    latest_attempt_ok = supervisor_status.get("last_health_attempt_ok")
    latest_attempt_at = supervisor_status.get("last_health_attempt_at")
    if latest_attempt_ok is not True:
        readiness = {
            **readiness,
            "ok": False,
            "probe_failure": True,
        }
    else:
        readiness = {**readiness, "probe_failure": False}
    payload = {
        "ok": readiness["ok"],
        "profile": app.state.settings.profile.name,
        "unhealthy_components": readiness["unhealthy_components"],
        "missing_components": readiness["missing_components"],
        "stale_components": readiness["stale_components"],
        "max_snapshot_age_seconds": readiness["max_snapshot_age_seconds"],
        "health_components": len(rows),
        "latest_probe_ok": latest_attempt_ok,
        "latest_probe_at": latest_attempt_at,
        "probe_failure": readiness["probe_failure"],
    }
    if not readiness["ok"]:
        return JSONResponse(status_code=503, content=payload)
    return payload


def _health_snapshot_readiness(
    settings: Any,
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Interpret persisted diagnostics as readiness without hiding warnings that matter."""

    required = {
        "runtime.home",
        "runtime.data",
        "runtime.cache",
        "runtime.logs",
        "storage.sqlite",
    }
    if bool(settings.llm_enabled):
        required.add("llm.router")
    by_component = {
        str(row.get("component") or ""): row
        for row in rows
        if str(row.get("component") or "")
    }
    missing = sorted(required - set(by_component))
    hard_failure_statuses = {"error", "failed", "critical", "unhealthy"}
    unhealthy = {
        component
        for component, row in by_component.items()
        if str(row.get("status") or "").casefold() in hard_failure_statuses
    }
    unhealthy.update(
        component
        for component in required
        if component in by_component
        and str(by_component[component].get("status") or "").casefold() != "ok"
    )
    max_age_seconds = max(180, max(60, int(settings.health_interval_sec)) * 2 + 30)
    stale: set[str] = set()
    current = (now or datetime.now(UTC)).astimezone(UTC)
    for component in required & set(by_component):
        raw_ts = str(by_component[component].get("ts") or "")
        try:
            timestamp = datetime.fromisoformat(raw_ts)
        except ValueError:
            stale.add(component)
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        age = (current - timestamp.astimezone(UTC)).total_seconds()
        if age < -300 or age > max_age_seconds:
            stale.add(component)
    return {
        "ok": not missing and not unhealthy and not stale,
        "unhealthy_components": sorted(unhealthy),
        "missing_components": missing,
        "stale_components": sorted(stale),
        "max_snapshot_age_seconds": max_age_seconds,
    }


@app.get("/api/status", response_model=StatusResponse)
async def status() -> StatusResponse:
    from .runtime_notices import collect_notices, get_service_mode

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
    state_dir = Path(app.state.settings.state_dir)
    return StatusResponse(
        settings=app.state.settings.public_dict(),
        counters=app.state.storage.counters(),
        health=health_checks,
        recent_events=app.state.storage.list_events(limit=25),
        notices=collect_notices(state_dir),
        service_mode=get_service_mode(state_dir),
    )


@app.get("/api/runtime/security")
async def runtime_security(request: Request) -> dict[str, Any]:
    host = request.client.host if request.client else ""
    return {
        "client_host": host,
        "loopback_client": _is_loopback_host(host),
        "local_machine_client": _is_local_machine_host(host),
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
    return await asyncio.to_thread(app.state.storage.backup_database)


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
    return await asyncio.to_thread(app.state.model_hub.inventory)


@app.get("/api/model-hub/search", response_model=ModelSearchResponse)
async def model_hub_search(
    query: str = Query(min_length=1, max_length=160),
    limit: int = Query(default=12, ge=1, le=30),
    context_tokens: int = Query(default=8192, ge=512, le=131072),
) -> ModelSearchResponse:
    return await asyncio.to_thread(
        app.state.model_hub.search,
        query,
        limit=limit,
        context_tokens=context_tokens,
    )


@app.get("/api/model-hub/downloads")
async def model_downloads() -> list[dict[str, Any]]:
    return await asyncio.to_thread(app.state.model_hub.download_jobs)


@app.post("/api/model-hub/download")
async def model_download(request: ModelDownloadRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            app.state.model_hub.start_download,
            request.repo_id,
            revision=request.revision,
            workers=request.workers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/model-hub/downloads/{job_id}/cancel")
async def model_download_cancel(job_id: str) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(app.state.model_hub.cancel_download, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await app.state.bus.publish({"channel": "models", "action": "download.cancel", **result})
    return result


@app.post("/api/models/activate")
async def model_activate(request: ModelActivateRequest) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(app.state.model_hub.activate_model, request.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await app.state.bus.publish({"channel": "models", "action": "activate", **result})
    return result


@app.delete("/api/models/local/{model_id}")
async def model_delete(model_id: str) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(app.state.model_hub.delete_model, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await app.state.bus.publish({"channel": "models", "action": "delete", **result})
    return result


@app.get("/api/dispatcher", response_model=DispatcherStatusResponse)
async def dispatcher() -> DispatcherStatusResponse:
    return await asyncio.to_thread(app.state.dispatcher.status)


@app.post("/api/dispatcher/{action}", response_model=DispatcherActionResponse)
async def dispatcher_action(action: str) -> DispatcherActionResponse:
    action_map = {"start": "up", "stop": "down", "logs": "logs"}
    compose_action = action_map.get(action)
    if compose_action is None:
        raise HTTPException(status_code=400, detail="Unsupported dispatcher action")
    runner = (
        app.state.dispatcher.run_compose_verified
        if compose_action in {"up", "down"}
        else app.state.dispatcher.run_compose
    )
    result = await asyncio.to_thread(runner, compose_action)
    status_snapshot = await asyncio.to_thread(app.state.dispatcher.status)
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
    return await asyncio.to_thread(app.state.host_bridge.snapshot)


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


@app.get("/api/browser/handoff")
async def browser_handoff() -> dict[str, Any] | None:
    return await asyncio.to_thread(browser_handoff_snapshot, app.state.storage)


@app.get("/api/internet/observability")
async def internet_observability(
    limit: int = Query(default=120, ge=10, le=300),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        internet_observability_snapshot,
        app.state.storage,
        limit=limit,
    )


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


@app.post("/api/autonomy/jobs/{job_id}/start", response_model=AutonomyJobResponse)
async def start_autonomy_job(job_id: str) -> AutonomyJobResponse:
    job = next((item for item in app.state.operations.list_jobs() if item["id"] == job_id), None)
    if job is None:
        raise HTTPException(status_code=404, detail="Autonomy job not found")
    task = asyncio.create_task(
        app.state.autonomy_executor.run_job(job),
        name=f"jarvis-autonomy-job-{job_id}",
    )
    _track_detached_autonomy_task(task, job_id)
    await app.state.bus.publish({"channel": "autonomy.jobs", "action": "started", "job_id": job_id})
    return job


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
    from .runtime_notices import blocking_notice, user_facing_reply

    notice = blocking_notice(Path(app.state.settings.state_dir))
    if notice and notice.get("kind") == "service_mode":
        # Maintenance fully blocks the agent turn for everyone.
        return ChatResponse(
            conversation_id=request.conversation_id or "service-mode",
            message_id=f"svc-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
            answer=user_facing_reply(notice),
            events=[],
            mission_id=None,
            duration_ms=0,
        )
    if (
        notice
        and notice.get("kind") == "model_overload"
        and not current_actor().is_owner
    ):
        # Guests/TG users get an immediate overload reply; the owner keeps
        # probing the model so the detector can clear when latency recovers.
        return ChatResponse(
            conversation_id=request.conversation_id or "model-overload",
            message_id=f"ovl-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
            answer=user_facing_reply(notice),
            events=[],
            mission_id=None,
            duration_ms=0,
        )
    try:
        return await app.state.agent.chat(
            request.message,
            conversation_id=request.conversation_id,
            mode=request.mode,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            attachments=[item.model_dump() for item in request.attachments],
            thinking_enabled=request.thinking_enabled,
            access_mode=request.access_mode,
            notification_chat_id=(
                request.notification_chat_id if current_actor().is_owner else None
            ),
            transport_request_id=request.request_id,
        )
    except (
        AuthorizationError,
        ChatRequestConflictError,
        ChatRequestInProgressError,
        ChatUnavailableError,
        ResourceIsolationError,
    ) as exc:
        raise _chat_http_exception(exc) from exc


def _chat_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, ResourceIsolationError):
        # A foreign id is indistinguishable from a missing conversation at the API.
        return HTTPException(status_code=404, detail="Conversation not found")
    if isinstance(exc, ChatUnavailableError):
        return HTTPException(
            status_code=503,
            detail="Language model is temporarily unavailable",
            headers=(
                {"X-Jarvis-Retry-Class": "llm-outage"}
                if exc.retry_scope == "service"
                else None
            ),
        )
    if isinstance(exc, ChatRequestInProgressError):
        return HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-Jarvis-Retry-Class": "chat-request-in-progress"},
        )
    if isinstance(exc, ChatRequestConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=403, detail="Chat permission denied")


@app.get("/api/voice/status")
async def voice_status() -> dict[str, Any]:
    return speech.voice_status()


@app.post("/api/voice/speak")
async def voice_speak(request: VoiceSpeakRequest) -> Response:
    """Synthesize text to the stylized 'Jarvis' voice and return WAV bytes.

    Deterministic (not model-driven) so both the web composer's speak button and the
    Telegram bridge can rely on it. Renders off the event loop; 503 when TTS is unavailable.
    """

    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    def _render() -> tuple[Any, bytes]:
        fd, tmp_name = tempfile.mkstemp(suffix=".wav", prefix="jarvis-tts-")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            result = speech.synthesize(
                text,
                tmp_path,
                voice=request.voice,
                style=request.style,
                engine=request.engine,
            )
            data = tmp_path.read_bytes() if result.ok and tmp_path.exists() else b""
        finally:
            with suppress(OSError):
                tmp_path.unlink()
        return result, data

    result, data = await asyncio.to_thread(_render)
    if not result.ok or not data:
        raise HTTPException(status_code=503, detail=result.error or "text-to-speech unavailable")
    headers = {
        "X-Voice-Engine": str(result.engine or ""),
        "X-Voice-Style": str(result.extra.get("style") or ""),
        "Cache-Control": "no-store",
    }
    return Response(content=data, media_type="audio/wav", headers=headers)


INTERRUPTED_STREAM_KEY_PREFIX = "agent.stream.interrupted."
INTERRUPTED_STREAM_REQUEST_KEY_PREFIX = f"{INTERRUPTED_STREAM_KEY_PREFIX}request."


def _persist_interrupted_stream(
    storage: JarvisStorage,
    *,
    conversation_id: str | None,
    partial: list[str],
    events: list[dict[str, Any]],
    request_id: str | None = None,
) -> dict[str, Any] | None:
    if not conversation_id:
        return None
    answer = "".join(partial).strip()
    if not answer:
        return None
    request_scope = str(request_id or "").strip()
    request_hash = hashlib.sha256(
        (
            request_scope
            if request_scope
            else f"{conversation_id}\x00{answer}"
        ).encode("utf-8")
    ).hexdigest()
    checkpoint_key = f"{INTERRUPTED_STREAM_REQUEST_KEY_PREFIX}{request_hash}"
    item = {
        "protocol": "jarvis.interrupted-stream.v2",
        "conversation_id": conversation_id,
        "checkpoint_id": f"stream_checkpoint_{request_hash[:16]}",
        "request_hash": request_hash,
        "answer": answer,
        "events": events,
        "terminal": False,
        "saved_at": utc_now(),
        "chars": len(answer),
    }
    index_key = f"{INTERRUPTED_STREAM_KEY_PREFIX}{conversation_id}"
    previous = storage.get_runtime_value(index_key, None)
    storage.set_runtime_value(checkpoint_key, item)
    storage.set_runtime_value(
        index_key,
        {
            "checkpoint_key": checkpoint_key,
            "conversation_id": conversation_id,
            "request_hash": request_hash,
        },
    )
    previous_key = (
        str(previous.get("checkpoint_key") or "")
        if isinstance(previous, dict)
        else ""
    )
    if previous_key and previous_key != checkpoint_key:
        storage.delete_runtime_value(previous_key)
    storage.add_event(
        kind="agent.stream.interrupted",
        title="Interrupted streaming answer persisted.",
        level="warn",
        payload={key: value for key, value in item.items() if key != "answer"},
    )
    return item


def _clear_interrupted_stream(
    storage: JarvisStorage,
    *,
    conversation_id: str | None,
    request_id: str | None,
) -> None:
    if not conversation_id:
        return
    index_key = f"{INTERRUPTED_STREAM_KEY_PREFIX}{conversation_id}"
    index = storage.get_runtime_value(index_key, None)
    if not isinstance(index, dict):
        return
    request_scope = str(request_id or "").strip()
    if request_scope:
        request_hash = hashlib.sha256(request_scope.encode("utf-8")).hexdigest()
        if index.get("request_hash") != request_hash:
            return
    checkpoint_key = str(index.get("checkpoint_key") or "")
    if checkpoint_key:
        storage.delete_runtime_value(checkpoint_key)
    storage.delete_runtime_value(index_key)


async def _close_chat_stream(stream: Any) -> None:
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - response cleanup cannot expose internals
        LOGGER.exception("Failed to close backend chat stream")


def _stream_error_envelope(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "error",
        "error": "Streaming response failed",
        "failure_scope": "request",
    }
    if isinstance(exc, ChatUnavailableError):
        payload["error"] = "Language model is temporarily unavailable"
        payload["failure_scope"] = exc.retry_scope
        if exc.retry_scope == "service":
            payload["retry_class"] = "llm-outage"
    elif isinstance(exc, ChatRequestInProgressError):
        payload["error"] = "Chat request is still being processed"
        payload["retry_class"] = "chat-request-in-progress"
    elif isinstance(exc, ChatRequestConflictError):
        payload["error"] = "Chat request conflicts with an existing request"
    elif isinstance(exc, ResourceIsolationError):
        payload["error"] = "Conversation is unavailable"
    elif isinstance(exc, AuthorizationError):
        payload["error"] = "Chat permission denied"
    return payload


@app.post("/api/chat/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    from .runtime_notices import blocking_notice, user_facing_reply

    if current_actor().preset_key == "guest":
        raise HTTPException(status_code=403, detail="Guest streaming is not available")

    notice = blocking_notice(Path(app.state.settings.state_dir))
    if notice and (
        notice.get("kind") == "service_mode"
        or (notice.get("kind") == "model_overload" and not current_actor().is_owner)
    ):
        answer = user_facing_reply(notice)
        kind = str(notice.get("kind") or "notice")
        conversation_id = request.conversation_id or kind
        message_id = f"{kind[:3]}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"

        async def _notice_mode_lines():
            yield (
                json.dumps(
                    {
                        "type": "meta",
                        "conversation_id": conversation_id,
                        "message_id": message_id,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            ).encode()
            yield (
                json.dumps({"type": "delta", "content": answer}, ensure_ascii=False) + "\n"
            ).encode()
            yield (
                json.dumps(
                    {
                        "type": "done",
                        "conversation_id": conversation_id,
                        "message_id": message_id,
                        "answer": answer,
                        "events": [],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            ).encode()

        return StreamingResponse(_notice_mode_lines(), media_type="application/x-ndjson")

    stream: Any = None
    try:
        stream = app.state.agent.stream_chat(
            request.message,
            conversation_id=request.conversation_id,
            mode=request.mode,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            attachments=[item.model_dump() for item in request.attachments],
            thinking_enabled=request.thinking_enabled,
            transport_request_id=request.request_id,
        )
        # Advance through authorization and the durable request claim before
        # Starlette commits a 200 response header.
        first_item = await anext(stream)
    except asyncio.CancelledError:
        await _close_chat_stream(stream)
        raise
    except (
        AuthorizationError,
        ChatRequestConflictError,
        ChatRequestInProgressError,
        ChatUnavailableError,
        ResourceIsolationError,
    ) as exc:
        await _close_chat_stream(stream)
        raise _chat_http_exception(exc) from exc
    except StopAsyncIteration as exc:
        await _close_chat_stream(stream)
        raise HTTPException(
            status_code=503,
            detail="Streaming response ended before output",
        ) from exc
    except Exception as exc:
        await _close_chat_stream(stream)
        raise HTTPException(
            status_code=503,
            detail="Streaming response is temporarily unavailable",
        ) from exc

    async def lines():
        conversation_id = request.conversation_id
        partial: list[str] = []
        events: list[dict[str, Any]] = []
        terminal_sent = False
        item = first_item
        try:
            while True:
                if item.get("type") == "meta":
                    conversation_id = str(
                        item.get("conversation_id") or conversation_id or ""
                    )
                elif item.get("type") == "delta":
                    partial.append(str(item.get("content") or ""))
                elif item.get("type") == "event" and isinstance(item.get("event"), dict):
                    events.append(item["event"])
                elif item.get("type") == "done":
                    _clear_interrupted_stream(
                        app.state.storage,
                        conversation_id=conversation_id,
                        request_id=request.request_id,
                    )
                elif item.get("type") == "error":
                    _persist_interrupted_stream(
                        app.state.storage,
                        conversation_id=conversation_id,
                        partial=partial,
                        events=events,
                        request_id=request.request_id,
                    )
                terminal_sent = item.get("type") in {"done", "error"}
                yield f"{json.dumps(item, ensure_ascii=False)}\n".encode()
                if terminal_sent:
                    return
                try:
                    item = await anext(stream)
                except StopAsyncIteration as exc:
                    item = _stream_error_envelope(exc)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    item = _stream_error_envelope(exc)
        except asyncio.CancelledError:
            if not terminal_sent:
                try:
                    _persist_interrupted_stream(
                        app.state.storage,
                        conversation_id=conversation_id,
                        partial=partial,
                        events=events,
                        request_id=request.request_id,
                    )
                except Exception:  # noqa: BLE001 - preserve cancellation semantics
                    LOGGER.exception("Failed to checkpoint cancelled chat stream")
            raise
        except Exception as exc:  # noqa: BLE001 - wire errors are sanitized
            LOGGER.exception("Backend chat stream failed after response start")
            if not terminal_sent:
                with suppress(Exception):
                    _persist_interrupted_stream(
                        app.state.storage,
                        conversation_id=conversation_id,
                        partial=partial,
                        events=events,
                        request_id=request.request_id,
                    )
                terminal_sent = True
                envelope = _stream_error_envelope(exc)
                yield f"{json.dumps(envelope, ensure_ascii=False)}\n".encode()
        finally:
            await _close_chat_stream(stream)

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.get("/api/chat/stream/interrupted/{conversation_id}")
async def interrupted_stream(conversation_id: str) -> dict[str, Any]:
    index = app.state.storage.get_runtime_value(
        f"{INTERRUPTED_STREAM_KEY_PREFIX}{conversation_id}", None
    )
    if not isinstance(index, dict):
        raise HTTPException(status_code=404, detail="Interrupted stream not found")
    checkpoint_key = str(index.get("checkpoint_key") or "")
    item = (
        app.state.storage.get_runtime_value(checkpoint_key, None)
        if checkpoint_key
        else index
    )
    if not isinstance(item, dict):
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


@app.get("/api/environment/profile")
async def environment_profile() -> dict[str, Any]:
    return {"profile": app.state.host_profile}


@app.get("/api/memory/playbooks")
async def execution_playbooks(
    query: str = Query(min_length=1, max_length=32768),
    limit: int = Query(default=5, ge=1, le=20),
) -> dict[str, Any]:
    records = await asyncio.to_thread(
        app.state.playbooks.lookup,
        query,
        limit=limit,
        mark_used=False,
    )
    return {
        "query": query,
        "playbooks": [item.to_dict() for item in records],
        "stats": await asyncio.to_thread(app.state.playbooks.stats),
    }


@app.get("/api/executive/plans/{mission_id}")
async def executive_plan(mission_id: str) -> dict[str, Any]:
    if app.state.storage.get_mission(mission_id) is None:
        # Do not disclose whether an in-memory plan exists for another tenant.
        raise HTTPException(status_code=404, detail="Executive plan not found")
    plan = app.state.executive.snapshot(mission_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Executive plan not found")
    return plan


@app.get("/api/internet/web-surfer")
async def web_surfer_capabilities() -> dict[str, Any]:
    return app.state.web_surfer.capabilities()


@app.get("/api/missions", response_model=list[Mission])
async def list_missions(limit: int = Query(default=50, ge=1, le=200)) -> list[Mission]:
    return app.state.storage.list_missions(limit=limit)


@app.post("/api/missions", response_model=Mission)
async def create_mission(request: MissionCreateRequest) -> Mission:
    return await app.state.agent.create_mission_planned(
        goal=request.goal,
        title=request.title,
    )


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
    try:
        app.state.authorization.require_current(
            "missions.write.own",
            resource_type="mission",
            resource_ref=mission_id,
            context={"operation": "update_task", "task_id": task_id},
        )
    except AuthorizationError as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "Permission denied.",
                "security_id": "missions.write.own",
            },
        ) from exc
    mission = app.state.storage.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="Mission not found")
    current = next(
        (item for item in mission.get("tasks", []) if item.get("id") == task_id),
        None,
    )
    if current is None:
        raise HTTPException(status_code=404, detail="Mission task not found")
    if (
        request.status is not None
        and request.status != current.get("status")
        and app.state.executive.snapshot(mission_id) is not None
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Task status is owned by the executive DAG; run/resume the mission "
                "instead of bypassing its state machine."
            ),
        )
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
    namespace = str(request.namespace or "").strip() or "core"
    # When content names an explicit campaign namespace, honor it over defaults.
    if namespace.casefold() in {"core", "operator", "default", "persona"}:
        match = re.search(
            r"(?:namespace\s*[:=]\s*|namespace\s+|в\s+namespace\s+)([A-Za-z0-9._\-]+)",
            request.content,
            flags=re.IGNORECASE,
        )
        if match:
            namespace = match.group(1).strip()[:80]
    return app.state.storage.add_memory(
        content=request.content,
        namespace=namespace,
        tags=request.tags,
        importance=request.importance,
    )


@app.get("/api/memory/vault", response_model=MemoryVaultResponse)
async def memory_vault() -> MemoryVaultResponse:
    return await asyncio.to_thread(app.state.storage.memory_graph)


@app.get("/api/memory/hygiene", response_model=MemoryHygieneResponse)
async def memory_hygiene() -> MemoryHygieneResponse:
    return memory_hygiene_report(app.state.storage)


@app.post("/api/memory/consolidate")
async def memory_consolidate() -> dict[str, int]:
    return await asyncio.to_thread(app.state.storage.consolidate_memories)


@app.get("/api/files", response_model=list[FileItem])
async def list_files(limit: int = Query(default=25, ge=1, le=200)) -> list[FileItem]:
    return app.state.storage.list_files(limit=limit)


@app.post("/api/files/upload", response_model=FileIngestResponse)
async def upload_file(file: UploadFile = File(...)) -> FileIngestResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename is required")
    try:
        result = await asyncio.to_thread(
            app.state.ingestor.ingest_upload,
            file.filename,
            file.file,
        )
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
    try:
        updated = app.state.storage.update_approval(
            approval_id,
            status=request.status,
            result=request.result,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if request.status in {"rejected", "cancelled"}:
        await app.state.approval_executor.reconcile_pending_approvals(
            approval_id=approval_id,
        )
        updated = app.state.storage.get_approval(approval_id) or updated
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
    origin = str(websocket.headers.get("origin") or "").strip()
    token = _header_token(websocket.headers) or _websocket_protocol_token(websocket.headers)
    origin_ok = not origin or _origin_allowed(origin)
    token_ok = _token_allowed(token)
    actor: ActorContext | None = None
    if token and not token_ok:
        service = getattr(app.state, "authorization", None)
        if isinstance(service, AuthorizationService):
            actor = service.authenticate_session(token)
        if actor is None:
            await websocket.close(code=1008)
            return
    elif token_ok:
        actor = app.state.authorization.actor_for_user(
            LEGACY_OWNER_USER_ID, source="api-token-websocket"
        )
    local_without_strict_token = (
        _is_local_machine_host(host) and not _strict_loopback_token_required()
    )
    if actor is None and local_without_strict_token:
        actor = app.state.authorization.actor_for_user(
            LEGACY_OWNER_USER_ID, source="legacy-local-websocket"
        )
    if not origin_ok or actor is None:
        await websocket.close(code=1008)
        return
    bus: EventBus = app.state.bus
    decision = app.state.authorization.authorize(
        actor.user_id,
        "events.subscribe",
        identity_id=actor.identity_id,
        context={"transport": "websocket"},
    )
    if not decision.allowed:
        await websocket.close(code=1008)
        return
    with bind_actor(actor):
        await bus.connect(websocket, user_id=actor.user_id)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return
        finally:
            await bus.disconnect(websocket)


# Route decorators have all executed at this point.  Freeze one audited catalog for
# startup synchronization and for pre-handler matching in the HTTP policy-enforcement
# point.  A later, unregistered /api route is denied as ``http.unmapped``.
HTTP_API_CAPABILITIES, _HTTP_ROUTE_CAPABILITIES = _build_http_api_catalog(app)
