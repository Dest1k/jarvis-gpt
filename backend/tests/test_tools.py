from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry, _windows_native_command


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
    denied_backslash = asyncio.run(
        tools.run("filesystem.read_text", {"path": r"C:\Windows\win.ini"})
    )

    assert denied.ok is False
    assert "outside allowed roots" in denied.summary
    assert denied_backslash.ok is False
    assert "outside allowed roots" in denied_backslash.summary
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


def test_windows_native_is_gated_and_uses_winapi_wmi(monkeypatch, tmp_path):
    class FakeBridgeClient:
        def __init__(self, _settings):
            pass

        async def execute(self, *, command, cwd=None, timeout_sec=30):
            assert cwd is None
            assert timeout_sec == 30
            assert "SetForegroundWindow" in command
            assert "Get-CimInstance" in command
            assert "Win32_Process" in command
            return {
                "ok": True,
                "summary": "native ok",
                "data": {
                    "stdout": (
                        '{"ok":true,"summary":"WMI/CIM query returned 1 item(s).",'
                        '"data":{"items":[{"Name":"python.exe"}]}}'
                    )
                },
            }

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient", FakeBridgeClient)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    info = {tool.name: tool for tool in tools.list()}
    blocked = asyncio.run(
        tools.run(
            "windows.native",
            {
                "action": "wmi.query",
                "payload": {
                    "namespace": "root\\cimv2",
                    "class_name": "Win32_Process",
                    "properties": ["Name"],
                },
            },
        )
    )
    queried = asyncio.run(
        tools.run(
            "windows.native",
            {
                "action": "wmi.query",
                "payload": {
                    "namespace": "root\\cimv2",
                    "class_name": "Win32_Process",
                    "properties": ["Name"],
                },
            },
            allow_danger=True,
        )
    )

    assert info["windows.native"].danger_level == "danger"
    assert blocked.ok is False
    assert "requires approval" in blocked.summary
    assert queried.ok is True
    assert queried.summary == "WMI/CIM query returned 1 item(s)."
    storage.close()


def test_windows_native_process_start_omits_empty_argument_list():
    command = _windows_native_command("process.start", {"executable": "calc.exe"})

    assert "[Console]::OutputEncoding=$utf8" in command
    assert "$OutputEncoding=$utf8" in command
    assert "function StartNativeProcess" in command
    assert "function Focus($TargetPid" in command
    assert "function ForegroundPid" in command
    assert "function TryActivate" in command
    assert "AttachThreadInput" in command
    assert "BringWindowToTop" in command
    assert "function SplitTargets" in command
    assert "function HasExplicitTarget" in command
    assert "function Focus($Pid" not in command
    assert "WScript.Shell" in command
    assert "Target window was not focused; native input was not sent." in command
    assert "Start-Process @parameters" in command
    assert "-ArgumentList @($Payload.arguments)" not in command


def test_windows_native_process_start_preserves_nonempty_arguments():
    command = _windows_native_command(
        "process.start",
        {
            "executable": "powershell.exe",
            "arguments": '-NoExit -Command "Get-Process | Select-Object -First 10"',
        },
    )

    assert "powershell.exe" in command
    assert "Get-Process | Select-Object -First 10" in command
    assert "$parameters.ArgumentList = [string]$Arguments" in command


def test_windows_native_screen_capture_command_is_structured(tmp_path):
    path = tmp_path / "screen.png"
    command = _windows_native_command("screen.capture", {"path": str(path), "limit": 8})

    assert "screen.capture" in command
    assert "System.Drawing" in command
    assert "CopyFromScreen" in command
    assert "Screen captured." in command
    assert "screen.png" in command
    assert "test_windows_native_screen_cap" in command
    assert "VisibleWindows $Limit" in command


def test_browser_open_is_validated_and_gated(monkeypatch, tmp_path):
    class FakeBridgeClient:
        def __init__(self, _settings):
            pass

        async def execute(self, *, command, cwd=None, timeout_sec=30):
            assert cwd is None
            assert timeout_sec == 10
            assert command == "Start-Process -FilePath 'https://example.com/path?q=1'"
            return {"ok": True, "summary": "opened", "data": {"command": command}}

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient", FakeBridgeClient)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    blocked = asyncio.run(tools.run("browser.open", {"url": "https://example.com/path?q=1"}))
    invalid = asyncio.run(
        tools.run(
            "browser.open",
            {"url": "file:///C:/Windows/win.ini"},
            allow_danger=True,
        )
    )
    opened = asyncio.run(
        tools.run(
            "browser.open",
            {"url": "https://example.com/path?q=1"},
            allow_danger=True,
        )
    )

    assert blocked.ok is False
    assert "requires approval" in blocked.summary
    assert invalid.ok is False
    assert "http and https" in invalid.summary
    assert opened.ok is True
    assert opened.data["url"] == "https://example.com/path?q=1"
    storage.close()


def test_browser_policy_and_open_many(monkeypatch, tmp_path):
    class FakeBridgeClient:
        def __init__(self, _settings):
            pass

        async def execute(self, *, command, cwd=None, timeout_sec=30):
            assert cwd is None
            assert timeout_sec == 20
            assert "https://example.com/a" in command
            assert "https://example.com/b" in command
            return {"ok": True, "summary": "opened many", "data": {"command": command}}

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient", FakeBridgeClient)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    policy = asyncio.run(tools.run("browser.policy", {}))
    blocked = asyncio.run(
        tools.run("browser.open_many", {"urls": ["https://example.com/a"]})
    )
    opened = asyncio.run(
        tools.run(
            "browser.open_many",
            {"urls": ["https://example.com/a", "https://example.com/b"]},
            allow_danger=True,
        )
    )

    assert policy.ok is True
    assert blocked.ok is False
    assert "requires approval" in blocked.summary
    assert opened.ok is True
    assert opened.data["urls"] == ["https://example.com/a", "https://example.com/b"]
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


def test_web_search_parses_public_results(monkeypatch, tmp_path):
    class FakeResponse:
        text = """
        <html>
          <a
            class="result__a"
            href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fprofile"
          >Example Profile</a>
          <a class="result__snippet">Public profile snippet</a>
          <a class="result__a" href="https://example.org/news">Example News</a>
          <div class="result__snippet">News snippet</div>
        </html>
        """

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return None

        async def get(self, url, *, headers):
            assert "duckduckgo.com/html/" in url
            assert "JARVIS-GPT" in headers["User-Agent"]
            assert self.kwargs["trust_env"] is False
            return FakeResponse()

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    result = asyncio.run(tools.run("web.search", {"query": "Dest1k OSINT", "limit": 2}))

    assert result.ok is True
    assert result.data["results"][0]["url"] == "https://example.com/profile"
    assert result.data["results"][0]["title"] == "Example Profile"
    assert result.data["results"][0]["snippet"] == "Public profile snippet"
    assert result.data["results"][1]["url"] == "https://example.org/news"
    storage.close()


def test_docker_ps_parses_compact_container_list(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr(
        "jarvis_gpt.tools.shutil.which",
        lambda command: "docker.exe" if command == "docker" else None,
    )
    monkeypatch.setattr(
        "jarvis_gpt.operations.shutil.which",
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
    monkeypatch.setattr("jarvis_gpt.operations.subprocess.run", fake_run)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    result = asyncio.run(tools.run("docker.ps", {}))
    fleet = asyncio.run(tools.run("docker.containers", {}))
    policy = asyncio.run(tools.run("docker.policy", {}))

    assert result.ok is True
    assert result.data["containers"][0]["name"] == "jarvis-gpt-dispatcher"
    assert result.data["containers"][0]["state"] == "running"
    assert fleet.ok is True
    assert fleet.data["containers"][0]["allowed"] is True
    assert policy.data["policy"]["max_log_tail"] >= 10
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


def test_dispatcher_tools_are_safe_or_gated(monkeypatch, tmp_path):
    class FakeDispatcher:
        def __init__(self, _settings):
            pass

        def status(self):
            return {"docker_available": True, "port_open": True}

        def run_compose(self, action):
            return {
                "ok": True,
                "summary": f"dispatcher {action}",
                "stdout": "ok",
                "stderr": "",
                "command": ["docker", "compose", action],
            }

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setattr("jarvis_gpt.tools.DispatcherManager", FakeDispatcher)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    info = {tool.name: tool for tool in tools.list()}
    status = asyncio.run(tools.run("dispatcher.status", {}))
    logs = asyncio.run(tools.run("dispatcher.logs", {}))
    blocked = asyncio.run(tools.run("dispatcher.start", {}))
    started = asyncio.run(tools.run("dispatcher.start", {}, allow_danger=True))

    assert info["dispatcher.start"].danger_level == "review"
    assert status.ok is True
    assert logs.ok is True
    assert blocked.ok is False
    assert "requires approval" in blocked.summary
    assert started.ok is True
    assert started.summary == "dispatcher up"
    storage.close()
