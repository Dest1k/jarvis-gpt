from __future__ import annotations

import asyncio

from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter, LLMStreamChunk
from jarvis_gpt.storage import JarvisStorage


def test_agent_creates_mission_from_large_goal(monkeypatch, tmp_path):
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

    response = asyncio.run(
        agent.chat(
            "Сделай проект с нуля: полностью переосмысли архитектуру, реализуй runtime, "
            "память, диагностику, web интерфейс и mission plan для локального Jarvis.",
            mode="auto",
        )
    )

    assert response.mission_id is not None
    assert "mission plan" in response.answer
    assert storage.counters()["mission_tasks"] >= 4
    mission = storage.get_mission(response.mission_id)
    task_titles = [task["title"] for task in mission["tasks"]]
    assert any("Command Center" in title for title in task_titles)
    storage.close()


def test_agent_executes_next_mission_step(monkeypatch, tmp_path):
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
    mission = agent.create_mission("Build tools runtime")

    result = asyncio.run(agent.execute_next_mission_step(mission["id"]))
    refreshed = storage.get_mission(mission["id"])
    runs = storage.list_tool_runs()

    assert result.result.ok is True
    assert result.task is not None
    assert result.task.status == "done"
    assert refreshed is not None
    assert refreshed["progress"] > 0
    assert runs[0]["tool"] == "mission.brief"
    storage.close()


def test_agent_streams_chat_response(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = FakeStreamingLLM()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=llm,
        bus=EventBus(),
    )

    items = asyncio.run(_collect(agent.stream_chat("hello", mode="chat", max_tokens=32)))
    deltas = [item["content"] for item in items if item["type"] == "delta"]
    done = next(item for item in items if item["type"] == "done")
    messages = storage.recent_messages(done["conversation_id"], limit=5)

    assert deltas == ["Hello", " world"]
    assert done["answer"] == "Hello world"
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "Hello world"
    assert llm.max_tokens == 32
    storage.close()


def test_agent_opens_wiki_without_false_refusal(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        return {"ok": True, "summary": "opened", "data": {"command": command}}

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
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

    response = asyncio.run(agent.chat("открой статью про Гитлера на вики в новой вкладке"))
    runs = storage.list_tool_runs()

    assert "ru.wikipedia.org" in response.answer
    assert "Адольф_Гитлер" in response.answer
    assert runs[0]["tool"] == "browser.open"
    assert runs[0]["ok"] is True
    storage.close()


def test_agent_opens_calculator_with_host_bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {"ok": True, "summary": "executed", "data": {"command": command}}

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
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

    response = asyncio.run(agent.chat("открой калькулятор и набери в нём что-нибудь"))
    runs = storage.list_tool_runs()

    assert "app.open_and_type" in captured["command"]
    assert "explorer.exe" in captured["command"]
    assert "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App" in captured["command"]
    assert "123{+}456=" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "Готово" in response.answer
    storage.close()


def test_agent_calculator_understands_russian_multiply_sign(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {"ok": True, "summary": "executed", "data": {"command": command}}

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
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

    response = asyncio.run(agent.chat("открой калькулятор и посчитай там 10х10"))
    runs = storage.list_tool_runs()

    assert "app.open_and_type" in captured["command"]
    assert "explorer.exe" in captured["command"]
    assert "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App" in captured["command"]
    assert "10{*}10=" in captured["command"]
    assert "Calculator|Калькулятор" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "Готово" in response.answer
    storage.close()


def test_agent_opens_console_with_top_processes(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {"ok": True, "summary": "executed", "data": {"command": command}}

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
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

    response = asyncio.run(agent.chat("открой мне консоль с топ 10 процессов"))
    runs = storage.list_tool_runs()

    assert "process.start" in captured["command"]
    assert "powershell.exe" in captured["command"]
    assert "Get-Process" in captured["command"]
    assert "Select-Object -First 10" in captured["command"]
    assert "Sort-Object CPU -Descending" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "Готово" in response.answer
    storage.close()


def test_agent_opens_system_info_in_console(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {"ok": True, "summary": "executed", "data": {"command": command}}

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
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

    response = asyncio.run(agent.chat("открой мне в консоли информацию о системе"))
    runs = storage.list_tool_runs()

    assert "process.start" in captured["command"]
    assert "powershell.exe" in captured["command"]
    assert "-NoExit" in captured["command"]
    assert "Get-ComputerInfo" in captured["command"]
    assert "Win32_Processor" in captured["command"]
    assert "Win32_LogicalDisk" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "Готово" in response.answer
    storage.close()


def test_agent_understands_system_info_console_followup(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {"ok": True, "summary": "executed", "data": {"command": command}}

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    conversation_id = storage.create_conversation("system info")
    storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="открой мне информацию о системе",
    )
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("так ты именно в консоли открой", conversation_id))
    runs = storage.list_tool_runs()

    assert "process.start" in captured["command"]
    assert "powershell.exe" in captured["command"]
    assert "Get-ComputerInfo" in captured["command"]
    assert "Win32_VideoController" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "PowerShell" in response.answer
    storage.close()


def test_agent_runs_largest_file_scan_in_console(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {"ok": True, "summary": "executed", "data": {"command": command}}

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
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

    response = asyncio.run(
        agent.chat("открой консоль и найди самый крупный файл на диске C:")
    )
    runs = storage.list_tool_runs()

    assert "process.start" in captured["command"]
    assert "powershell.exe" in captured["command"]
    assert "-NoExit" in captured["command"]
    assert "Get-ChildItem" in captured["command"]
    assert "Sort-Object" not in captured["command"]
    assert "Проверено файлов" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "Готово" in response.answer
    storage.close()


def test_agent_understands_largest_file_console_followup(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {"ok": True, "summary": "executed", "data": {"command": command}}

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    conversation_id = storage.create_conversation("scan")
    storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="какой самый крупный файл у меня сейчас на диске C: ?",
    )
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    response = asyncio.run(
        agent.chat("сделай это сканирование в консоли, и вывод там же", conversation_id)
    )
    runs = storage.list_tool_runs()

    assert "process.start" in captured["command"]
    assert "powershell.exe" in captured["command"]
    assert "Get-ChildItem" in captured["command"]
    assert "C:\\" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "PowerShell" in response.answer
    storage.close()


def test_agent_opens_named_programs_through_native_layer(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {"ok": True, "summary": "executed", "data": {"command": command}}

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
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

    response = asyncio.run(agent.chat("открой Microsoft Edge"))
    runs = storage.list_tool_runs()

    assert "process.start" in captured["command"]
    assert "msedge.exe" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "Готово" in response.answer
    storage.close()


def test_agent_captures_screen_when_asked_to_look(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {
            "ok": True,
            "summary": "captured",
            "data": {
                "stdout": (
                    '{"ok":true,"summary":"Screen captured.","action":"screen.capture",'
                    '"data":{"path":"C:/tmp/screen.png","width":1920,"height":1080,'
                    '"activeWindow":{"ProcessName":"chrome","MainWindowTitle":"Jarvis"},'
                    '"windows":[{"ProcessName":"chrome","MainWindowTitle":"Jarvis"}]}}'
                )
            },
        }

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
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

    response = asyncio.run(agent.chat("посмотри моими глазами, что сейчас на экране"))
    runs = storage.list_tool_runs()

    assert "screen.capture" in captured["command"]
    assert "screenshots" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "Визуальная проверка" in response.answer
    assert "C:/tmp/screen.png" in response.answer
    assert "chrome" in response.answer
    storage.close()


def test_agent_types_into_general_windows_app(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {"ok": True, "summary": "native input", "data": {"command": command}}

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
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

    response = asyncio.run(agent.chat("открой блокнот и напиши Jarvis online"))
    runs = storage.list_tool_runs()

    assert "app.open_and_type" in captured["command"]
    assert "notepad.exe" in captured["command"]
    assert "Jarvis online" in captured["command"]
    assert "scratch" in captured["command"]
    assert "notepad-" in captured["command"]
    assert ".txt" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "Готово" in response.answer
    storage.close()


def test_agent_routes_wmi_requests_to_native_layer(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {
            "ok": True,
            "summary": "wmi ok",
            "data": {
                "stdout": (
                    '{"ok":true,"summary":"WMI/CIM query returned 1 item(s).",'
                    '"data":{"items":[{"Name":"python.exe"}]}}'
                )
            },
        }

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
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

    response = asyncio.run(agent.chat("покажи процессы через WMI"))
    runs = storage.list_tool_runs()

    assert "wmi.query" in captured["command"]
    assert "Win32_Process" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "WMI/CIM query returned" in response.answer
    assert "python.exe" in response.answer
    storage.close()


def test_agent_opens_google_search(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    captured = {}

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        captured["command"] = command
        return {"ok": True, "summary": "opened", "data": {"command": command}}

    monkeypatch.setattr("jarvis_gpt.tools.HostBridgeClient.execute", fake_execute)
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

    response = asyncio.run(agent.chat("загугли как проверить открытые порты linux"))

    assert "google.com/search" in response.answer
    assert "google.com/search" in captured["command"]
    storage.close()


def test_agent_context_includes_relevance_snippets(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.add_memory(
        content="Runtime context should be clipped and scored before it reaches the model.",
        namespace="runtime",
        tags=["context"],
        importance=0.8,
    )
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    context = agent._prepare_context("runtime context", None)
    messages = agent._build_llm_messages(context, "runtime context")
    rendered = "\n".join(message["content"] for message in messages)

    assert "[0." in rendered or "[1." in rendered
    assert "Runtime context should be clipped" in rendered
    storage.close()


def test_agent_context_includes_operator_preferences(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.set_runtime_value(
        "experience.preferences",
        {
            "operator_name": "Alex",
            "communication_style": "detailed",
            "quiet_hours": "23:00-08:00",
        },
    )
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LLMRouter(settings),
        bus=EventBus(),
    )

    context = agent._prepare_context("hello", None)
    messages = agent._build_llm_messages(context, "hello")
    rendered = "\n".join(message["content"] for message in messages)

    assert "operator_name: Alex" in rendered
    assert "communication_style: detailed" in rendered
    assert "quiet_hours: 23:00-08:00" in rendered
    storage.close()


class FakeStreamingLLM:
    def __init__(self) -> None:
        self.max_tokens: int | None = None

    async def stream_complete(self, messages, *, temperature=None, max_tokens=None):
        self.max_tokens = max_tokens
        yield LLMStreamChunk(kind="delta", content="Hello")
        yield LLMStreamChunk(kind="delta", content=" world")


class FakeTaggedStreamingLLM:
    async def stream_complete(self, messages, *, temperature=None, max_tokens=None):
        yield LLMStreamChunk(kind="delta", content="$\\rightarrow$ **Важное уточнение:** ")
        yield LLMStreamChunk(kind="delta", content="готово без служебного префикса")


def test_agent_cleans_service_prefixes_from_streamed_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=FakeTaggedStreamingLLM(),
        bus=EventBus(),
    )

    items = asyncio.run(_collect(agent.stream_chat("проверка", mode="chat")))
    done = next(item for item in items if item["type"] == "done")

    assert "Важное уточнение" not in done["answer"]
    assert "$\\rightarrow$" not in done["answer"]
    assert done["answer"] == "готово без служебного префикса"


async def _collect(stream):
    return [item async for item in stream]
