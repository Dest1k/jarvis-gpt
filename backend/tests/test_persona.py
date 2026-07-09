from __future__ import annotations

import asyncio

import pytest
from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.persona import (
    DEFAULT_PERSONA,
    PersonaManager,
    home_location,
    is_configured,
    normalize_persona,
    primary_language,
    render_system_block,
)
from jarvis_gpt.storage import JarvisStorage


def _storage(monkeypatch, tmp_path) -> tuple[PersonaManager, JarvisStorage]:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-turbo")
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return PersonaManager(settings=settings, storage=storage), storage


def _agent(monkeypatch, tmp_path) -> tuple[AgentRuntime, JarvisStorage]:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )
    return agent, storage


def test_normalize_fills_defaults_and_caps_lists():
    persona = normalize_persona(
        {
            "display_name": "  Ilya  ",
            "tech_stack": ["Proxmox", "proxmox", "Docker", "  ", "Docker"],
            "languages": [],
            "glossary": {"нода": "физический сервер в кластере"},
            "unknown_field": "ignored",
        }
    )

    assert persona["display_name"] == "Ilya"
    # Deduplicated case-insensitively, blanks dropped.
    assert persona["tech_stack"] == ["Proxmox", "Docker"]
    # Empty language list falls back to the default.
    assert persona["languages"] == DEFAULT_PERSONA["languages"]
    assert persona["glossary"]["нода"] == "физический сервер в кластере"
    assert "unknown_field" not in persona


def test_is_configured_and_accessors():
    assert is_configured(normalize_persona({})) is False
    persona = normalize_persona({"location": "Казань", "languages": ["ru", "en"]})
    assert is_configured(persona) is True
    assert home_location(persona) == "Казань"
    assert primary_language(persona) == "ru"
    assert home_location(normalize_persona({})) is None


def test_render_system_block_is_empty_for_defaults():
    assert render_system_block(normalize_persona({})) == ""


def test_render_system_block_includes_rich_fields():
    persona = normalize_persona(
        {
            "display_name": "Ilya",
            "role": "Системный администратор",
            "location": "Казань",
            "tech_stack": ["Proxmox", "Debian"],
            "interests": ["3D-печать"],
            "standing_instructions": ["Всегда показывай команды для Debian"],
        }
    )
    block = render_system_block(persona, preferences={"operator_name": "fallback"})

    assert "name: Ilya" in block
    assert "home_location: Казань" in block
    assert "Proxmox" in block
    assert "Standing operator instructions" in block
    assert "Всегда показывай команды для Debian" in block
    # Location drives the generic place fallback guidance.
    assert "Казань" in block


def test_persona_persists_and_audits(monkeypatch, tmp_path):
    manager, storage = _storage(monkeypatch, tmp_path)

    updated = manager.update({"location": "Казань", "tech_stack": ["Proxmox"]})
    reloaded = PersonaManager(settings=manager.settings, storage=storage)

    assert updated["location"] == "Казань"
    assert reloaded.persona()["tech_stack"] == ["Proxmox"]
    audit = storage.list_audit(limit=10)
    assert any(entry["action"] == "persona.update" for entry in audit)
    storage.close()


def test_add_insight_appends_and_dedupes(monkeypatch, tmp_path):
    manager, _ = _storage(monkeypatch, tmp_path)

    manager.add_insight("interests", "фотография")
    deduped = manager.add_insight("interests", "Фотография")  # case-insensitive dup
    final = manager.add_insight("interests", "походы")

    assert deduped["interests"] == ["фотография"]
    assert final["interests"] == ["фотография", "походы"]
    assert manager.persona()["interests"] == ["фотография", "походы"]
    with pytest.raises(ValueError):
        manager.add_insight("notes", "not a list field")


def test_agent_injects_persona_into_prompt(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)
    PersonaManager(settings=agent.settings, storage=storage).update(
        {"display_name": "Ilya", "tech_stack": ["Proxmox"], "location": "Казань"}
    )

    context = agent._prepare_context("hello", None)
    messages = agent._build_llm_messages(context, "hello")
    rendered = "\n".join(message["content"] for message in messages)

    assert "Operator persona" in rendered
    assert "Proxmox" in rendered
    assert "Казань" in rendered
    storage.close()


def test_agent_prompt_has_no_persona_block_by_default(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)

    context = agent._prepare_context("hello", None)
    messages = agent._build_llm_messages(context, "hello")
    rendered = "\n".join(message["content"] for message in messages)

    assert "Operator persona" not in rendered
    storage.close()


def test_weather_inference_prefers_persona_location(monkeypatch, tmp_path):
    agent, storage = _agent(monkeypatch, tmp_path)
    PersonaManager(settings=agent.settings, storage=storage).update({"location": "Казань"})

    async def fail_fetch(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("web.fetch should not run when persona has a home location")

    monkeypatch.setattr(agent.tools, "run", fail_fetch)

    location, events = asyncio.run(agent._infer_weather_location())

    assert location == "Казань"
    assert any(event.payload.get("source") == "persona" for event in events)
    storage.close()
