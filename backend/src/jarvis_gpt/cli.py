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

    serve_parser = sub.add_parser("serve", help="Start FastAPI backend")
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.add_argument("--reload", action="store_true")
    serve_parser.set_defaults(func=cmd_serve)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)
