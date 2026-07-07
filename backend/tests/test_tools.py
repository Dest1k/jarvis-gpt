from __future__ import annotations

import asyncio

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


def test_tool_registry_runs_memory_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    save_result = asyncio.run(
        tools.run(
            "memory.save",
            {
                "content": "Jarvis tools can persist memories.",
                "namespace": "tests",
                "tags": ["tools"],
                "importance": 0.7,
            },
        )
    )
    search_result = asyncio.run(tools.run("memory.search", {"query": "persist memories"}))

    assert save_result.ok is True
    assert search_result.ok is True
    assert search_result.data["items"]
    assert storage.counters()["tool_runs"] == 2
    storage.close()


def test_filesystem_tool_stays_inside_allowed_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    denied = asyncio.run(tools.run("filesystem.read_text", {"path": "C:/Windows/win.ini"}))

    assert denied.ok is False
    assert "outside allowed roots" in denied.summary
    storage.close()
