from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import Any

import uvicorn

from .agent import AgentRuntime
from .approval_executor import ApprovalExecutor
from .cognitive_memory import ExecutionPlaybookStore, HostProfileManager
from .config import PROFILES, ensure_runtime_dirs, load_settings
from .diagnostics import run_diagnostics
from .dispatcher import DispatcherManager
from .event_bus import EventBus
from .executive_runtime import ExecutiveCoordinator
from .host_bridge import HostBridgeClient, HostBridgeStatus
from .ingest import FileIngestor
from .learning import LearningEngine
from .llm import LLMRouter
from .model_catalog import ModelCatalog
from .persona import PersonaManager
from .runtime_lease import PrimaryRuntimeLease, RuntimeLeaseError
from .storage import JarvisStorage
from .supervisor import RuntimeSupervisor
from .telemetry import TelemetryCollector
from .tools import ToolRegistry


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _runtime(profile: str | None = None) -> tuple[Any, JarvisStorage, LLMRouter, AgentRuntime]:
    settings = load_settings(profile)
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    try:
        storage.open_readonly()
    except (FileNotFoundError, sqlite3.Error) as exc:
        raise SystemExit("Jarvis storage is not initialized; run `jarvis init` first.") from exc
    llm = LLMRouter(settings)
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, bus=EventBus())
    return settings, storage, llm, agent


@contextmanager
def _runtime_context(
    profile: str | None = None,
) -> Iterator[tuple[Any, JarvisStorage, LLMRouter, AgentRuntime]]:
    runtime = _runtime(profile)
    try:
        yield runtime
    finally:
        runtime[1].close()


@contextmanager
def _primary_runtime(
    profile: str | None = None,
) -> Iterator[tuple[Any, JarvisStorage, LLMRouter, AgentRuntime]]:
    """Serialize CLI executive mutations with the long-running API owner."""

    settings = load_settings(profile)
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    lease = PrimaryRuntimeLease(settings.state_dir / "primary-runtime.lock")
    playbooks: ExecutionPlaybookStore | None = None
    agent: AgentRuntime | None = None
    try:
        try:
            lease.acquire()
        except RuntimeLeaseError as exc:
            raise SystemExit(
                "Jarvis API currently owns executive state; use its API or stop it "
                "before running a mutating CLI command."
            ) from exc
        storage.initialize()
        storage.recover_interrupted_approval_executions()
        profile_manager = HostProfileManager(settings.home / "host_profile.json")
        try:
            host_profile = profile_manager.refresh()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            host_profile = profile_manager.load_verified(max_age_seconds=86_400)
            if host_profile is None:
                raise RuntimeError(
                    "cold-start host fingerprint could not be collected or recovered"
                ) from exc
        storage.set_runtime_value("environment.host_profile", host_profile)
        playbooks = ExecutionPlaybookStore(
            settings.state_dir / "execution-playbooks.sqlite3"
        )
        executive = ExecutiveCoordinator(
            storage=storage,
            host_profile=host_profile,
            playbooks=playbooks,
            recover_interrupted=True,
        )
        llm = LLMRouter(settings)
        tools = ToolRegistry(
            settings,
            storage,
            llm,
            playbooks=playbooks,
            executive=executive,
            recover_execution=True,
        )
        agent = AgentRuntime(
            settings=settings,
            storage=storage,
            llm=llm,
            bus=EventBus(),
            tools=tools,
            playbooks=playbooks,
            host_profile=host_profile,
            executive=executive,
        )
        yield settings, storage, llm, agent
    finally:
        if agent is not None:
            with suppress(Exception):
                agent.tools.web_surfer.close()
        if playbooks is not None:
            with suppress(Exception):
                playbooks.close()
        with suppress(Exception):
            lease.release()
        storage.close()


@asynccontextmanager
async def _async_primary_runtime(profile: str | None = None):
    with _primary_runtime(profile) as runtime:
        try:
            await runtime[3].tools.web_surfer.start()
            runtime[3].tools.refresh_web_surfer_registration()
            yield runtime
        finally:
            await runtime[3].tools.web_surfer.aclose()


def cmd_init(args: argparse.Namespace) -> None:
    with _primary_runtime(args.profile) as (settings, storage, _llm, _agent):
        storage.add_event(kind="runtime.init", title="Runtime directories initialized")
        _print_json(settings.public_dict())


def cmd_profiles(_args: argparse.Namespace) -> None:
    _print_json(
        {
            name: {
                "title": profile.title,
                "description": profile.description,
                "model_dir_name": profile.model_dir_name,
                "eager_mode": profile.eager_mode,
                "max_steps": profile.max_steps,
            }
            for name, profile in PROFILES.items()
        }
    )


def cmd_status(args: argparse.Namespace) -> None:
    settings, storage, _llm, _agent = _runtime(args.profile)
    _print_json(
        {
            "settings": settings.public_dict(),
            "counters": storage.counters(),
            "health": storage.latest_health(),
            "recent_events": storage.list_events(limit=10),
        }
    )
    storage.close()


def cmd_backup(args: argparse.Namespace) -> None:
    with _primary_runtime(args.profile) as (_settings, storage, _llm, _agent):
        _print_json(storage.backup_database(args.output_dir))


def cmd_models(args: argparse.Namespace) -> None:
    settings, storage, _llm, _agent = _runtime(args.profile)
    catalog = ModelCatalog(settings).response()
    if args.env:
        _print_json(catalog["dispatcher"]["env"])
    else:
        _print_json(catalog)
    storage.close()


def cmd_diag(args: argparse.Namespace) -> None:
    async def run() -> None:
        async with _async_primary_runtime(args.profile) as (
            settings,
            storage,
            llm,
            _agent,
        ):
            result = await run_diagnostics(settings=settings, storage=storage, llm=llm)
            _print_json(result.model_dump())

    asyncio.run(run())


def cmd_chat(args: argparse.Namespace) -> None:
    async def run() -> None:
        async with _async_primary_runtime(args.profile) as (
            _settings,
            _storage,
            _llm,
            agent,
        ):
            response = await agent.chat(args.message, mode=args.mode)
            _print_json(response.model_dump())

    asyncio.run(run())


def cmd_tools(args: argparse.Namespace) -> None:
    async def run() -> None:
        _settings, storage, _llm, agent = _runtime(args.profile)
        try:
            await agent.tools.web_surfer.start()
            agent.tools.refresh_web_surfer_registration()
            _print_json([tool.model_dump() for tool in agent.tools.list()])
        finally:
            await agent.tools.web_surfer.aclose()
            storage.close()

    asyncio.run(run())


def cmd_llm_health(args: argparse.Namespace) -> None:
    async def run() -> None:
        _settings, storage, llm, _agent = _runtime(args.profile)
        _print_json(await llm.health())
        storage.close()

    asyncio.run(run())


def cmd_dispatcher_status(args: argparse.Namespace) -> None:
    settings, storage, _llm, _agent = _runtime(args.profile)
    _print_json(DispatcherManager(settings, storage=storage).status())
    storage.close()


def cmd_dispatcher_compose(args: argparse.Namespace) -> None:
    settings, storage, _llm, _agent = _runtime(args.profile)
    manager = DispatcherManager(settings, storage=storage)
    if args.env:
        _print_json(manager.compose_env())
    else:
        _print_json(
            {
                "up": manager.compose_command("up"),
                "down": manager.compose_command("down"),
                "logs": manager.compose_command("logs"),
                "env": manager.compose_env(),
            }
        )
    storage.close()


def cmd_dispatcher_up(args: argparse.Namespace) -> None:
    with _primary_runtime(args.profile) as (settings, storage, _llm, _agent):
        result = DispatcherManager(settings, storage=storage).run_compose_verified("up")
        _print_json(result)
        if not result.get("ok"):
            raise SystemExit(1)


def cmd_dispatcher_down(args: argparse.Namespace) -> None:
    with _primary_runtime(args.profile) as (settings, storage, _llm, _agent):
        result = DispatcherManager(settings, storage=storage).run_compose_verified("down")
        _print_json(result)
        if not result.get("ok"):
            raise SystemExit(1)


def cmd_telemetry(args: argparse.Namespace) -> None:
    runtime = _primary_runtime if args.persist else _runtime_context
    with runtime(args.profile) as (settings, storage, _llm, _agent):
        snapshot = TelemetryCollector(settings).snapshot()
        if args.persist:
            storage.record_telemetry(snapshot)
        _print_json(snapshot)


def cmd_learning_tick(args: argparse.Namespace) -> None:
    with _primary_runtime(args.profile) as (_settings, storage, llm, _agent):
        _print_json(asyncio.run(LearningEngine(storage, llm=llm).tick_async(limit=args.limit)))


def cmd_host_bridge(args: argparse.Namespace) -> None:
    settings, storage, _llm, _agent = _runtime(args.profile)
    _print_json(HostBridgeStatus(settings).snapshot())
    storage.close()


def cmd_host_bridge_action(args: argparse.Namespace) -> None:
    async def run() -> None:
        async with _async_primary_runtime(args.profile) as (
            settings,
            _storage,
            _llm,
            _agent,
        ):
            payload = _json_argument(args.payload_json, option="--payload-json")
            if args.payload_file:
                path = Path(args.payload_file).expanduser().resolve(strict=True)
                if not path.is_file() or path.stat().st_size > 1024 * 1024:
                    raise SystemExit(
                        "--payload-file must be a JSON file no larger than 1 MiB"
                    )
                payload = _json_argument(
                    path.read_text(encoding="utf-8"), option="--payload-file"
                )
            payload.update(_set_arguments(args.sets))
            result = await HostBridgeClient(settings).action(
                action=args.action,
                payload=payload,
                timeout_sec=args.timeout,
            )
            _print_json(result)
            if not result.get("ok"):
                raise SystemExit(1)

    asyncio.run(run())


def cmd_autonomy(args: argparse.Namespace) -> None:
    settings, storage, _llm, _agent = _runtime(args.profile)
    _print_json(RuntimeSupervisor(settings=settings, storage=storage).status())
    storage.close()


def cmd_persona(args: argparse.Namespace) -> None:
    settings, storage, _llm, _agent = _runtime(args.profile)
    _print_json(PersonaManager(settings=settings, storage=storage).persona())
    storage.close()


def cmd_persona_set(args: argparse.Namespace) -> None:
    with _primary_runtime(args.profile) as (settings, storage, _llm, _agent):
        patch = _set_arguments(args.sets)
        updated = PersonaManager(settings=settings, storage=storage).update(patch)
        _print_json(updated)


def cmd_ingest(args: argparse.Namespace) -> None:
    with _primary_runtime(args.profile) as (settings, storage, _llm, _agent):
        result = FileIngestor(settings=settings, storage=storage).ingest_path(args.path)
        _print_json(result)


def cmd_files(args: argparse.Namespace) -> None:
    _settings, storage, _llm, _agent = _runtime(args.profile)
    _print_json(storage.list_files(limit=args.limit))
    storage.close()


def cmd_file_search(args: argparse.Namespace) -> None:
    _settings, storage, _llm, _agent = _runtime(args.profile)
    _print_json(storage.search_file_chunks(args.query, limit=args.limit))
    storage.close()


def cmd_audit(args: argparse.Namespace) -> None:
    _settings, storage, _llm, _agent = _runtime(args.profile)
    _print_json(
        storage.list_audit(
            limit=args.limit,
            target_type=args.target_type,
            target_id=args.target_id,
        )
    )
    storage.close()


def cmd_approvals(args: argparse.Namespace) -> None:
    _settings, storage, _llm, _agent = _runtime(args.profile)
    _print_json(storage.list_approvals(limit=args.limit, status=args.status))
    storage.close()


def cmd_approval_request(args: argparse.Namespace) -> None:
    with _primary_runtime(args.profile) as (_settings, storage, _llm, _agent):
        payload = _json_argument(args.payload)
        approval = storage.create_approval(
            title=args.title,
            description=args.description,
            requested_action=args.action,
            risk=args.risk,
            payload=payload,
        )
        _print_json(approval)


def cmd_approval_update(args: argparse.Namespace) -> None:
    with _primary_runtime(args.profile) as (settings, storage, llm, agent):
        result = _json_argument(args.result)
        try:
            updated = storage.update_approval(args.id, status=args.status, result=result)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if updated is None:
            raise SystemExit(f"Approval not found: {args.id}")
        if args.status in {"rejected", "cancelled"}:
            executor = ApprovalExecutor(
                storage=storage,
                llm=llm,
                dispatcher=DispatcherManager(settings),
                tools=agent.tools,
                mission_resumer=agent.resume_mission_after_approval,
                mission_aborter=agent.abort_mission_after_approval,
            )
            asyncio.run(
                executor.reconcile_pending_approvals(approval_id=args.id)
            )
            updated = storage.get_approval(args.id) or updated
        _print_json(updated)


def cmd_approval_execute(args: argparse.Namespace) -> None:
    async def run() -> None:
        async with _async_primary_runtime(args.profile) as (
            settings,
            storage,
            llm,
            agent,
        ):
            executor = ApprovalExecutor(
                storage=storage,
                llm=llm,
                dispatcher=DispatcherManager(settings),
                tools=agent.tools,
                mission_resumer=agent.resume_mission_after_approval,
                mission_aborter=agent.abort_mission_after_approval,
            )
            result = await executor.execute(args.id)
            _print_json(
                {
                    "ok": result.ok,
                    "summary": result.summary,
                    "data": result.data,
                    "approval": result.approval,
                    "status_code": result.status_code,
                }
            )
            if result.status_code >= 400:
                raise SystemExit(1)

    asyncio.run(run())


def cmd_tool_run(args: argparse.Namespace) -> None:
    async def run() -> None:
        async with _async_primary_runtime(args.profile) as (
            _settings,
            _storage,
            _llm,
            agent,
        ):
            arguments = _json_argument(args.arguments)
            arguments.update(_set_arguments(args.sets))
            response = await agent.tools.run(
                args.name, arguments, allow_danger=args.allow_danger
            )
            _print_json(response.model_dump())

    asyncio.run(run())


def cmd_mission_next(args: argparse.Namespace) -> None:
    async def run() -> None:
        async with _async_primary_runtime(args.profile) as (
            _settings,
            _storage,
            _llm,
            agent,
        ):
            response = await agent.execute_next_mission_step(args.mission_id)
            _print_json(response.model_dump())

    asyncio.run(run())


def cmd_mission_run(args: argparse.Namespace) -> None:
    async def run() -> None:
        async with _async_primary_runtime(args.profile) as (
            _settings,
            _storage,
            _llm,
            agent,
        ):
            response = await agent.run_mission(args.mission_id, max_steps=args.max_steps)
            _print_json(response.model_dump())

    asyncio.run(run())


def cmd_serve(args: argparse.Namespace) -> None:
    settings = load_settings(args.profile)
    uvicorn.run(
        "jarvis_gpt.main:app",
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        reload=args.reload,
        factory=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis runtime CLI")
    parser.add_argument("--profile", choices=sorted(PROFILES), default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="Create D:\\jarvis runtime folders and SQLite state")
    init_parser.set_defaults(func=cmd_init)

    profiles_parser = sub.add_parser("profiles", help="List available runtime profiles")
    profiles_parser.set_defaults(func=cmd_profiles)

    status_parser = sub.add_parser("status", help="Show local runtime status")
    status_parser.set_defaults(func=cmd_status)

    backup_parser = sub.add_parser("backup", help="Create a consistent SQLite runtime backup")
    backup_parser.add_argument("--output-dir", default=None)
    backup_parser.set_defaults(func=cmd_backup)

    models_parser = sub.add_parser("models", help="Show local model catalog and dispatcher config")
    models_parser.add_argument("--env", action="store_true", help="Print vLLM dispatcher env only")
    models_parser.set_defaults(func=cmd_models)

    diag_parser = sub.add_parser("diag", help="Run diagnostics")
    diag_parser.set_defaults(func=cmd_diag)

    chat_parser = sub.add_parser("chat", help="Run one local agent turn")
    chat_parser.add_argument("message")
    chat_parser.add_argument("--mode", choices=["auto", "chat", "mission"], default="auto")
    chat_parser.set_defaults(func=cmd_chat)

    tools_parser = sub.add_parser("tools", help="List registered safe tools")
    tools_parser.set_defaults(func=cmd_tools)

    llm_health_parser = sub.add_parser("llm-health", help="Check OpenAI-compatible LLM route")
    llm_health_parser.set_defaults(func=cmd_llm_health)

    dispatcher_status_parser = sub.add_parser("dispatcher-status", help="Show dispatcher status")
    dispatcher_status_parser.set_defaults(func=cmd_dispatcher_status)

    dispatcher_compose_parser = sub.add_parser(
        "dispatcher-compose",
        help="Show dispatcher docker compose commands and env",
    )
    dispatcher_compose_parser.add_argument("--env", action="store_true")
    dispatcher_compose_parser.set_defaults(func=cmd_dispatcher_compose)

    dispatcher_up_parser = sub.add_parser("dispatcher-up", help="Start vLLM dispatcher service")
    dispatcher_up_parser.set_defaults(func=cmd_dispatcher_up)

    dispatcher_down_parser = sub.add_parser("dispatcher-down", help="Stop vLLM dispatcher service")
    dispatcher_down_parser.set_defaults(func=cmd_dispatcher_down)

    telemetry_parser = sub.add_parser("telemetry", help="Collect host/GPU/Docker telemetry")
    telemetry_parser.add_argument("--persist", action="store_true")
    telemetry_parser.set_defaults(func=cmd_telemetry)

    learning_tick_parser = sub.add_parser(
        "learning-tick",
        help="Mine audit/tool history into memory",
    )
    learning_tick_parser.add_argument("--limit", type=int, default=20)
    learning_tick_parser.set_defaults(func=cmd_learning_tick)

    host_bridge_parser = sub.add_parser("host-bridge", help="Show native host bridge status")
    host_bridge_parser.set_defaults(func=cmd_host_bridge)

    host_bridge_action_parser = sub.add_parser(
        "host-bridge-action",
        help="Run a typed action.v1 request through the native host bridge",
    )
    host_bridge_action_parser.add_argument("action")
    host_bridge_action_parser.add_argument("--payload-json", default="{}")
    host_bridge_action_parser.add_argument("--payload-file", default=None)
    host_bridge_action_parser.add_argument(
        "--set",
        dest="sets",
        action="append",
        default=[],
        help="Set one payload field as key=value. Can be repeated.",
    )
    host_bridge_action_parser.add_argument("--timeout", type=int, default=30)
    host_bridge_action_parser.set_defaults(func=cmd_host_bridge_action)

    autonomy_parser = sub.add_parser("autonomy", help="Show autonomous supervisor settings")
    autonomy_parser.set_defaults(func=cmd_autonomy)

    persona_parser = sub.add_parser("persona", help="Show the durable operator persona")
    persona_parser.set_defaults(func=cmd_persona)

    persona_set_parser = sub.add_parser(
        "persona-set",
        help="Update persona fields, e.g. --set location=Kazan --set tech_stack=Proxmox,Debian",
    )
    persona_set_parser.add_argument(
        "--set",
        dest="sets",
        action="append",
        default=[],
        help="Set one persona field as key=value (comma-separated for lists). Repeatable.",
    )
    persona_set_parser.set_defaults(func=cmd_persona_set)

    ingest_parser = sub.add_parser("ingest", help="Copy and index a local text file")
    ingest_parser.add_argument("path")
    ingest_parser.set_defaults(func=cmd_ingest)

    files_parser = sub.add_parser("files", help="List indexed or stored files")
    files_parser.add_argument("--limit", type=int, default=25)
    files_parser.set_defaults(func=cmd_files)

    file_search_parser = sub.add_parser("file-search", help="Search indexed file chunks")
    file_search_parser.add_argument("query")
    file_search_parser.add_argument("--limit", type=int, default=12)
    file_search_parser.set_defaults(func=cmd_file_search)

    audit_parser = sub.add_parser("audit", help="Show audit trail")
    audit_parser.add_argument("--limit", type=int, default=25)
    audit_parser.add_argument("--target-type", default=None)
    audit_parser.add_argument("--target-id", default=None)
    audit_parser.set_defaults(func=cmd_audit)

    approvals_parser = sub.add_parser("approvals", help="List human approval gates")
    approvals_parser.add_argument("--limit", type=int, default=25)
    approvals_parser.add_argument("--status", default=None)
    approvals_parser.set_defaults(func=cmd_approvals)

    approval_request_parser = sub.add_parser("approval-request", help="Create a HITL gate")
    approval_request_parser.add_argument("title")
    approval_request_parser.add_argument("description")
    approval_request_parser.add_argument("--action", default="manual.review")
    approval_request_parser.add_argument("--risk", choices=["review", "danger"], default="review")
    approval_request_parser.add_argument("--payload", default="{}")
    approval_request_parser.set_defaults(func=cmd_approval_request)

    approval_update_parser = sub.add_parser("approval-update", help="Update a HITL gate")
    approval_update_parser.add_argument("id")
    approval_update_parser.add_argument(
        "--status",
        choices=["approved", "rejected", "cancelled"],
        required=True,
    )
    approval_update_parser.add_argument("--result", default="{}")
    approval_update_parser.set_defaults(func=cmd_approval_update)

    approval_execute_parser = sub.add_parser(
        "approval-execute",
        help="Execute an approved HITL gate through the gated executor",
    )
    approval_execute_parser.add_argument("id")
    approval_execute_parser.set_defaults(func=cmd_approval_execute)

    tool_run_parser = sub.add_parser("tool-run", help="Run a registered safe tool")
    tool_run_parser.add_argument("name")
    tool_run_parser.add_argument(
        "--arguments",
        default="{}",
        help="JSON object with tool arguments",
    )
    tool_run_parser.add_argument(
        "--set",
        dest="sets",
        action="append",
        default=[],
        help="Set one argument as key=value. Can be repeated.",
    )
    tool_run_parser.add_argument("--allow-danger", action="store_true")
    tool_run_parser.set_defaults(func=cmd_tool_run)

    mission_next_parser = sub.add_parser("mission-next", help="Execute next pending mission task")
    mission_next_parser.add_argument("mission_id")
    mission_next_parser.set_defaults(func=cmd_mission_next)

    mission_run_parser = sub.add_parser(
        "mission-run",
        help="Auto-chain mission steps until completion, a blocked step, or the budget",
    )
    mission_run_parser.add_argument("mission_id")
    mission_run_parser.add_argument("--max-steps", type=int, default=None)
    mission_run_parser.set_defaults(func=cmd_mission_run)

    serve_parser = sub.add_parser("serve", help="Start FastAPI backend")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.add_argument("--reload", action="store_true")
    serve_parser.set_defaults(func=cmd_serve)

    return parser


def _json_argument(raw: str | None, *, option: str = "--arguments") -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON for {option}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{option} must be a JSON object")
    return data


def _set_arguments(items: list[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--set expects key=value, got: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit("--set key cannot be empty")
        parsed[key] = _parse_set_value(value)
    return parsed


def _parse_set_value(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        decoded = None
    else:
        return decoded
    if "," in value:
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
