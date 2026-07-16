"""Hybrid frontier-brain scaffold — prepared but INACTIVE by default.

The owner wants the hybrid brain to reach the frontier model through the logged-in
Claude Code CLI (subscription, not a billed API key), and to stay a dormant
scaffold until they explicitly enable it. These tests pin exactly that: inert when
off, constructible when on, and never raising into the caller — and they never
invoke a real CLI.
"""

from __future__ import annotations

import asyncio

from jarvis_gpt.config import load_settings
from jarvis_gpt.frontier_brain import (
    FrontierBrain,
    build_frontier_brain,
    select_brain,
)
from jarvis_gpt.llm import LLMRouter


def test_scaffold_is_inert_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_ENABLE_HYBRID_BRAIN", raising=False)
    settings = load_settings()
    assert settings.hybrid_brain_enabled is False
    assert build_frontier_brain(settings) is None
    assert select_brain(settings) == "local"
    # The router holds no frontier object, so nothing can delegate.
    router = LLMRouter(settings)
    assert router.frontier is None


def test_activation_flag_builds_brain(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_ENABLE_HYBRID_BRAIN", "1")
    monkeypatch.setenv("JARVIS_FRONTIER_MODEL", "sonnet")
    settings = load_settings()
    assert settings.hybrid_brain_enabled is True
    brain = build_frontier_brain(settings)
    assert isinstance(brain, FrontierBrain)
    assert brain.model == "sonnet"
    assert select_brain(settings) == "frontier"
    # The router now carries a live frontier delegate configured from settings.
    router_brain = LLMRouter(settings).frontier
    assert isinstance(router_brain, FrontierBrain)
    assert router_brain.model == "sonnet"


def test_split_messages_separates_system_and_conversation():
    system, user = FrontierBrain._split_messages(
        [
            {"role": "system", "content": "Ты редактор."},
            {"role": "user", "content": "Составь план."},
        ]
    )
    assert system == "Ты редактор."
    assert user == "Составь план."  # single user turn is passed verbatim

    system2, convo = FrontierBrain._split_messages(
        [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "next"},
        ]
    )
    assert system2 == "S"
    assert "USER: hi" in convo and "ASSISTANT: hello" in convo and "USER: next" in convo


def test_build_command_uses_print_mode_model_and_effort():
    brain = FrontierBrain(cli_path="claude", model="claude-opus-4-8", effort="medium")
    command = brain._build_command("SYS", "ASK")
    assert command[0] == "claude"
    assert "-p" in command and "ASK" in command
    assert command[command.index("--model") + 1] == "claude-opus-4-8"
    assert command[command.index("--effort") + 1] == "medium"
    assert command[command.index("--output-format") + 1] == "text"
    assert command[command.index("--system-prompt") + 1] == "SYS"


def test_defaults_are_opus_48_medium_effort(monkeypatch, tmp_path):
    # Owner requirement: Opus 4.8 at medium effort, via the subscription CLI.
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_ENABLE_HYBRID_BRAIN", "1")
    monkeypatch.delenv("JARVIS_FRONTIER_MODEL", raising=False)
    monkeypatch.delenv("JARVIS_FRONTIER_EFFORT", raising=False)
    brain = build_frontier_brain(load_settings())
    assert brain is not None
    assert brain.model == "claude-opus-4-8"
    assert brain.effort == "medium"


def test_complete_falls_back_when_cli_missing():
    # A bogus CLI path must yield a not-ok result (never raise, never block), so the
    # caller transparently keeps using the local brain. No real CLI is invoked.
    brain = FrontierBrain(cli_path="jarvis-nonexistent-cli-xyz", model="opus")
    assert brain.is_available() is False
    result = asyncio.run(brain.complete([{"role": "user", "content": "ping"}]))
    assert result.ok is False
    assert "not found" in (result.error or "").lower()


def test_describe_reports_status_without_secrets():
    info = FrontierBrain(cli_path="jarvis-nonexistent-cli-xyz").describe()
    assert info["backend"] == "claude-code-cli-subscription"
    assert info["available"] is False
    assert "api_key" not in info
