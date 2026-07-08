from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
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


def test_filesystem_write_text_is_sandboxed_and_gated(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))
    target = settings.home / "notes" / "approved.txt"

    blocked = asyncio.run(
        tools.run(
            "filesystem.write_text",
            {"path": str(target), "content": "hello"},
        )
    )
    written = asyncio.run(
        tools.run(
            "filesystem.write_text",
            {"path": str(target), "content": "hello"},
            allow_danger=True,
        )
    )
    denied = asyncio.run(
        tools.run(
            "filesystem.write_text",
            {"path": "C:/Windows/jarvis-denied.txt", "content": "nope"},
            allow_danger=True,
        )
    )

    assert blocked.ok is False
    assert "requires approval" in blocked.summary
    assert written.ok is True
    assert target.read_text(encoding="utf-8") == "hello"
    assert denied.ok is False
    assert "outside allowed roots" in denied.summary
    storage.close()


def test_host_bridge_execute_requires_token(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr("jarvis_gpt.host_bridge.bridge_token_path", lambda _settings: None)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    info = {tool.name: tool for tool in tools.list()}
    blocked = asyncio.run(tools.run("host.bridge.execute", {"command": "Write-Output ok"}))
    result = asyncio.run(
        tools.run(
            "host.bridge.execute",
            {"command": "Write-Output ok"},
            allow_danger=True,
        )
    )

    assert info["host.bridge.execute"].danger_level == "danger"
    assert blocked.ok is False
    assert "requires approval" in blocked.summary
    assert result.ok is False
    assert "token is missing" in result.summary
    storage.close()


def test_web_fetch_blocks_private_addresses(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    info = {tool.name: tool for tool in tools.list()}
    result = asyncio.run(tools.run("web.fetch", {"url": "http://127.0.0.1:8000/"}))

    assert info["web.fetch"].danger_level == "safe"
    assert result.ok is False
    assert "public addresses" in result.summary
    storage.close()


def test_web_fetch_reads_public_text(monkeypatch, tmp_path):
    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/plain; charset=utf-8"}

        async def aiter_bytes(self):
            yield b"hello "
            yield b"world"

    class FakeStream:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        def stream(self, method, url, *, headers, follow_redirects):
            assert method == "GET"
            assert url == "https://example.com/"
            assert headers["User-Agent"] == "JARVIS-GPT/0.1"
            assert follow_redirects is False
            assert self.kwargs["trust_env"] is False
            return FakeStream()

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr("jarvis_gpt.tools._hostname_is_private", lambda _hostname: False)
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    result = asyncio.run(tools.run("web.fetch", {"url": "https://example.com/"}))

    assert result.ok is True
    assert result.data["status_code"] == 200
    assert result.data["text"] == "hello world"
    assert result.data["truncated"] is False
    storage.close()


def test_docker_ps_parses_compact_container_list(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr(
        "jarvis_gpt.tools.shutil.which",
        lambda command: "docker.exe" if command == "docker" else None,
    )

    def fake_run(command, **kwargs):
        assert command[:3] == ["docker.exe", "ps", "-a"]
        assert kwargs["timeout"] == 10
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"ID":"abc","Names":"jarvis-gpt-dispatcher","Image":"vllm",'
                '"Status":"Up 2 minutes","State":"running","Ports":"127.0.0.1:8001"}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr("jarvis_gpt.tools.subprocess.run", fake_run)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    result = asyncio.run(tools.run("docker.ps", {}))

    assert result.ok is True
    assert result.data["containers"][0]["name"] == "jarvis-gpt-dispatcher"
    assert result.data["containers"][0]["state"] == "running"
    storage.close()


def test_docker_logs_restricts_non_jarvis_containers(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    result = asyncio.run(tools.run("docker.logs", {"container": "postgres"}))

    assert result.ok is False
    assert "restricted" in result.summary
    storage.close()
