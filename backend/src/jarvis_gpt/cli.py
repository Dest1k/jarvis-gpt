from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import uvicorn

from .agent import AgentRuntime
from .config import PROFILES, ensure_runtime_dirs, load_settings
from .diagnostics import run_diagnostics
from .event_bus import EventBus
from .ingest import FileIngestor
from .llm import LLMRouter
from .storage import JarvisStorage


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _runtime(profile: str | None = None) -> tuple[Any, JarvisStorage, LLMRouter, AgentRuntime]:
    settings = load_settings(profile)
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = LLMRouter(settings)
    agent = AgentRuntime(settings=settings, storage=storage, llm=llm, bus=EventBus())
    return settings, storage, llm, agent


def cmd_init(args: argparse.Namespace) -> None:
    settings, storage, _llm, _agent = _runtime(args.profile)
    storage.add_event(kind="runtime.init", title="Runtime directories initialized")
    _print_json(settings.public_dict())
    storage.close()


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


def cmd_diag(args: argparse.Namespace) -> None:
    async def run() -> None:
        settings, storage, llm, _agent = _runtime(args.profile)
        result = await run_diagnostics(settings=settings, storage=storage, llm=llm)
        _print_json(result.model_dump())
        storage.close()

    asyncio.run(run())


def cmd_chat(args: argparse.Namespace) -> None:
    async def run() -> None:
        _settings, storage, _llm, agent = _runtime(args.profile)
        response = await agent.chat(args.message, mode=args.mode)
        _print_json(response.model_dump())
        storage.close()

    asyncio.run(run())


def cmd_tools(args: argparse.Namespace) -> None:
    settings, storage, _llm, agent = _runtime(args.profile)
    _print_json([tool.model_dump() for tool in agent.tools.list()])
    storage.close()


def cmd_ingest(args: argparse.Namespace) -> None:
    settings, storage, _llm, _agent = _runtime(args.profile)
    result = FileIngestor(settings=settings, storage=storage).ingest_path(args.path)
    _print_json(result)
    storage.close()


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


def cmd_tool_run(args: argparse.Namespace) -> None:
    async def run() -> None:
        _settings, storage, _llm, agent = _runtime(args.profile)
        arguments = _json_argument(args.arguments)
        arguments.update(_set_arguments(args.sets))
        response = await agent.tools.run(args.name, arguments)
        _print_json(response.model_dump())
        storage.close()

    asyncio.run(run())


def cmd_mission_next(args: argparse.Namespace) -> None:
    async def run() -> None:
        _settings, storage, _llm, agent = _runtime(args.profile)
        response = await agent.execute_next_mission_step(args.mission_id)
        _print_json(response.model_dump())
        storage.close()

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
    parser = argparse.ArgumentParser(prog="jarvis-gpt", description="JARVIS GPT runtime CLI")
    parser.add_argument("--profile", choices=sorted(PROFILES), default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="Create D:\\jarvis runtime folders and SQLite state")
    init_parser.set_defaults(func=cmd_init)

    profiles_parser = sub.add_parser("profiles", help="List available runtime profiles")
    profiles_parser.set_defaults(func=cmd_profiles)

    status_parser = sub.add_parser("status", help="Show local runtime status")
    status_parser.set_defaults(func=cmd_status)

    diag_parser = sub.add_parser("diag", help="Run diagnostics")
    diag_parser.set_defaults(func=cmd_diag)

    chat_parser = sub.add_parser("chat", help="Run one local agent turn")
    chat_parser.add_argument("message")
    chat_parser.add_argument("--mode", choices=["auto", "chat", "mission"], default="auto")
    chat_parser.set_defaults(func=cmd_chat)

    tools_parser = sub.add_parser("tools", help="List registered safe tools")
    tools_parser.set_defaults(func=cmd_tools)

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
    tool_run_parser.set_defaults(func=cmd_tool_run)

    mission_next_parser = sub.add_parser("mission-next", help="Execute next pending mission task")
    mission_next_parser.add_argument("mission_id")
    mission_next_parser.set_defaults(func=cmd_mission_next)

    serve_parser = sub.add_parser("serve", help="Start FastAPI backend")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.add_argument("--reload", action="store_true")
    serve_parser.set_defaults(func=cmd_serve)

    return parser


def _json_argument(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON for --arguments: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("--arguments must be a JSON object")
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
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    if "," in value:
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
