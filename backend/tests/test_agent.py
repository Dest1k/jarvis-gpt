from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

from jarvis_gpt.agent import AgentRuntime
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter, LLMStreamChunk
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage


def _tool_response(tool: str, ok: bool, summary: str, data: dict):
    return ToolRunResponse(tool=tool, ok=ok, summary=summary, data=data)


def _agent_with_native_capture(monkeypatch, tmp_path):
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
    return agent, storage, captured


def _agent_without_llm(monkeypatch, tmp_path):
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


def test_run_mission_chains_all_steps_offline(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    mission = agent.create_mission("Build tools runtime")
    task_count = len(mission["tasks"])

    run = asyncio.run(agent.run_mission(mission["id"], max_steps=task_count))
    refreshed = storage.get_mission(mission["id"])

    assert run.completed is True
    assert run.stopped_reason == "completed"
    assert run.executed_steps == task_count
    assert all(task["status"] == "done" for task in refreshed["tasks"])
    assert refreshed["progress"] == 1.0
    storage.close()


def test_run_mission_respects_step_budget(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    mission = agent.create_mission("Build tools runtime")
    assert len(mission["tasks"]) > 1

    run = asyncio.run(agent.run_mission(mission["id"], max_steps=1))

    assert run.executed_steps == 1
    assert run.completed is False
    assert run.stopped_reason == "budget"
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


def test_agent_includes_runtime_date_context(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    class CapturingLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "готово", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=CapturingLLM(), bus=EventBus())

    response = asyncio.run(agent.chat("коротко представься", mode="chat"))

    system_messages = [item["content"] for item in captured["messages"] if item["role"] == "system"]
    date_context = "\n".join(system_messages)
    assert response.answer == "готово"
    assert "Runtime date context" in date_context
    assert "current_date:" in date_context
    assert "early 2026" in date_context
    storage.close()


def test_agent_passes_chat_attachments_to_llm_context(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    stored_path = tmp_path / "brief.txt"
    stored_path.write_text("alpha attached content", encoding="utf-8")
    file_record = storage.create_file_record(
        name="brief.txt",
        stored_path=stored_path,
        sha256="abc",
        size=stored_path.stat().st_size,
        mime_type="text/plain",
        status="indexed",
        chunk_count=1,
    )
    storage.add_file_chunks(file_record["id"], ["alpha attached content from upload"])

    class CapturingLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "done", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=CapturingLLM(), bus=EventBus())
    attachments = [
        {
            "id": file_record["id"],
            "name": file_record["name"],
            "mime_type": file_record["mime_type"],
            "size": file_record["size"],
        }
    ]

    response = asyncio.run(
        agent.chat("разбери вложение", mode="chat", attachments=attachments)
    )

    stored_messages = storage.recent_messages(response.conversation_id, limit=4)
    user_message = next(item for item in stored_messages if item["role"] == "user")
    rendered_prompt = "\n".join(item["content"] for item in captured["messages"])

    assert user_message["content"] == "разбери вложение"
    assert user_message["metadata"]["attachments"][0]["id"] == file_record["id"]
    assert "Attached files already uploaded" in rendered_prompt
    assert "brief.txt" in rendered_prompt
    assert "alpha attached content from upload" in rendered_prompt
    storage.close()


def test_agent_can_disable_model_thinking(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    class CapturingThinkingLLM:
        async def complete(
            self,
            messages,
            *,
            temperature=None,
            max_tokens=None,
            thinking_enabled=True,
        ):
            captured["messages"] = messages
            captured["thinking_enabled"] = thinking_enabled
            return type(
                "Result",
                (),
                {"ok": True, "content": "<think>hidden</think>final answer", "error": None},
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=CapturingThinkingLLM(),
        bus=EventBus(),
    )

    response = asyncio.run(
        agent.chat("hello", mode="chat", thinking_enabled=False)
    )
    user_message = next(
        item for item in storage.recent_messages(response.conversation_id, limit=4)
        if item["role"] == "user"
    )
    rendered_prompt = "\n".join(item["content"] for item in captured["messages"])

    assert captured["thinking_enabled"] is False
    assert "Thinking output is disabled" in rendered_prompt
    assert response.answer == "final answer"
    assert "hidden" not in response.answer
    assert user_message["metadata"]["thinking_enabled"] is False
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


def test_agent_sends_followup_command_to_same_console(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    commands = []

    def native_stdout(action, summary, data=None, ok=True):
        return json.dumps(
            {"ok": ok, "summary": summary, "action": action, "data": data or {}},
            ensure_ascii=False,
        )

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        commands.append(command)
        if "$Action='process.start'" in command:
            return {
                "ok": True,
                "summary": "executed",
                "data": {
                    "stdout": native_stdout(
                        "process.start",
                        "Started cmd.exe.",
                        {"pid": 4242, "processName": "cmd"},
                    )
                },
            }
        return {
            "ok": True,
            "summary": "executed",
            "data": {
                "stdout": native_stdout(
                    "keyboard.send",
                    "Native keyboard input sent.",
                    {"focused": True},
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

    first = asyncio.run(agent.chat("открой что-нибудь в консоли"))
    response = asyncio.run(
        agent.chat("а теперь в этой же косоли дай мне инфу о системе", first.conversation_id)
    )
    runs = storage.list_tool_runs()

    assert len(commands) == 2
    assert "$Action='process.start'" in commands[0]
    assert "cmd.exe" in commands[0]
    assert "$Action='keyboard.send'" in commands[1]
    assert '"process_id": 4242' in commands[1] or '"process_id":4242' in commands[1]
    assert "systeminfo" in commands[1]
    assert "{ENTER}" in commands[1]
    assert runs[-1]["tool"] == "windows.native"
    assert "уже открытую консоль" in response.answer
    storage.close()


def test_agent_falls_back_when_same_console_focus_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    commands = []

    def native_stdout(action, summary, data=None, ok=True):
        return json.dumps(
            {"ok": ok, "summary": summary, "action": action, "data": data or {}},
            ensure_ascii=False,
        )

    async def fake_execute(self, command, cwd=None, timeout_sec=30):
        commands.append(command)
        if "$Action='keyboard.send'" in command:
            return {
                "ok": True,
                "summary": "executed",
                "data": {
                    "stdout": native_stdout(
                        "keyboard.send",
                        "Target window was not focused; native input was not sent.",
                        {"focused": False},
                        ok=False,
                    )
                },
            }
        return {
            "ok": True,
            "summary": "executed",
            "data": {
                "stdout": native_stdout(
                    "process.start",
                    "Started cmd.exe.",
                    {"pid": 4242, "processName": "cmd"},
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

    first = asyncio.run(agent.chat("открой что-нибудь в консоли"))
    response = asyncio.run(
        agent.chat("а теперь в этой же консоли дай мне инфу о системе", first.conversation_id)
    )

    assert len(commands) == 3
    assert "$Action='keyboard.send'" in commands[1]
    assert "$Action='process.start'" in commands[2]
    assert "cmd.exe" in commands[2]
    assert "/k systeminfo" in commands[2]
    assert "Первичная попытка" in response.answer
    assert "открыл новую cmd" in response.answer
    storage.close()


def test_agent_runs_explicit_console_command_in_console(monkeypatch, tmp_path):
    agent, storage, captured = _agent_with_native_capture(monkeypatch, tmp_path)

    response = asyncio.run(agent.chat("выполни в консоли `ipconfig /all`"))
    runs = storage.list_tool_runs()

    assert "process.start" in captured["command"]
    assert "powershell.exe" in captured["command"]
    assert "JARVIS CONSOLE TARGET" in captured["command"]
    assert "ipconfig /all" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "PowerShell" in response.answer
    storage.close()


def test_agent_runs_explicit_powershell_verb_command(monkeypatch, tmp_path):
    agent, storage, captured = _agent_with_native_capture(monkeypatch, tmp_path)

    response = asyncio.run(agent.chat("выполни в консоли `Write-Host JarvisConsoleGuardOk`"))
    runs = storage.list_tool_runs()

    assert "process.start" in captured["command"]
    assert "powershell.exe" in captured["command"]
    assert "JARVIS CONSOLE TARGET" in captured["command"]
    assert "JARVIS CONSOLE TARGET GUARD" not in captured["command"]
    assert "Write-Host JarvisConsoleGuardOk" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "PowerShell" in response.answer
    storage.close()


def test_agent_runs_network_console_recipe(monkeypatch, tmp_path):
    agent, storage, captured = _agent_with_native_capture(monkeypatch, tmp_path)

    response = asyncio.run(agent.chat("открой в консоли диагностику сети"))
    runs = storage.list_tool_runs()

    assert "process.start" in captured["command"]
    assert "powershell.exe" in captured["command"]
    assert "NETWORK DIAGNOSTICS" in captured["command"]
    assert "ipconfig /all" in captured["command"]
    assert "Get-NetAdapter" in captured["command"]
    assert "JARVIS CONSOLE TARGET GUARD" not in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "PowerShell" in response.answer
    storage.close()


def test_agent_uses_console_guard_for_unknown_console_targets(monkeypatch, tmp_path):
    agent, storage, captured = _agent_with_native_capture(monkeypatch, tmp_path)

    response = asyncio.run(agent.chat("открой в консоли загадочную проверку состояния"))
    runs = storage.list_tool_runs()

    assert "process.start" in captured["command"]
    assert "powershell.exe" in captured["command"]
    assert "JARVIS CONSOLE TARGET GUARD" in captured["command"]
    assert "примером команды в чате" in captured["command"]
    assert runs[0]["tool"] == "windows.native"
    assert "PowerShell" in response.answer
    storage.close()


def test_agent_understands_network_console_followup(monkeypatch, tmp_path):
    agent, storage, captured = _agent_with_native_capture(monkeypatch, tmp_path)
    conversation_id = storage.create_conversation("network info")
    storage.add_message(
        conversation_id=conversation_id,
        role="user",
        content="покажи сетевые настройки",
    )

    response = asyncio.run(agent.chat("теперь именно в консоли", conversation_id))
    runs = storage.list_tool_runs()

    assert "process.start" in captured["command"]
    assert "powershell.exe" in captured["command"]
    assert "NETWORK DIAGNOSTICS" in captured["command"]
    assert "Get-NetIPConfiguration" in captured["command"]
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


def test_agent_researches_google_style_query(monkeypatch, tmp_path):
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

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Linux open ports",
                            "url": "https://example.com/linux-ports",
                            "snippet": "ss -tulpen shows open ports",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "ss -tulpen shows open ports"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("загугли как проверить открытые порты linux"))

    assert "Источники" in response.answer
    assert "https://example.com/linux-ports" in response.answer
    assert "ss -tulpen" in response.answer
    storage.close()


def test_agent_researches_current_ticket_request(monkeypatch, tmp_path):
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
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        captured.setdefault("tools", []).append((name, arguments or {}))
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 2 result(s).",
                {
                    "results": [
                        {
                            "title": "Авиабилеты Екатеринбург Москва",
                            "url": "https://example.com/avia",
                            "snippet": "Екатеринбург Москва от 12 500 ₽ вылет 14:20",
                        },
                        {
                            "title": "ЖД билеты Екатеринбург Москва",
                            "url": "https://example.com/train",
                            "snippet": "поезд 18:45 от 4 500 руб.",
                        },
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "Екатеринбург Москва от 12 500 ₽ вылет 14:20",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(
        agent.chat("дай мне пример реального билета из екатеринбурга в москву на послезавтра")
    )

    assert captured["tools"][0][0] == "web.search"
    assert "билеты цена наличие расписание" in captured["query"]
    assert "Источники" in response.answer
    assert "12 500" in response.answer
    assert "выдум" not in response.answer.lower()
    storage.close()


def test_agent_researches_public_osint_self_lookup(monkeypatch, tmp_path):
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

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            assert "OSINT" in arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Public profile",
                            "url": "https://example.com/dest1k",
                            "snippet": "public account",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "public account profile"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди меня в интернете по аккаунту Dest1k"))

    assert "Источники" in response.answer
    assert "https://example.com/dest1k" in response.answer
    assert "OSINT-рамка" in response.answer
    assert "не буду помогать" in response.answer
    assert "не могу" not in response.answer.lower()
    storage.close()


def test_agent_researches_dns_shop_product_without_osint_suffix(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        captured.setdefault("tools", []).append((name, arguments or {}))
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 в DNS",
                            "url": "https://www.dns-shop.ru/product/rtx-5090",
                            "snippet": "GeForce RTX 5090 399 999 ₽ В наличии",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "GeForce RTX 5090 399 999 ₽ В наличии, доставка завтра",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди мне самую дешёвую видеокарту rtx 5090 на dns"))

    assert captured["tools"][0][0] == "web.search"
    assert "site:dns-shop.ru" in captured["query"]
    assert captured["query"].startswith("rtx 5090")
    assert "найди" not in captured["query"]
    assert "OSINT" not in captured["query"]
    assert "399 999" in response.answer
    assert "https://www.dns-shop.ru/product/rtx-5090" in response.answer
    assert "Приоритетно проверял выдачу магазина DNS" in response.answer
    assert "билет" not in response.answer.lower()
    assert "OSINT-рамка" not in response.answer
    storage.close()


def test_agent_keeps_dns_records_in_osint_context(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "example.com DNS records",
                            "url": "https://example.net/dns/example.com",
                            "snippet": "A 93.184.216.34 MX example.com",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "A 93.184.216.34 MX example.com"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("проверь DNS записи домена example.com"))

    assert "OSINT" in captured["query"]
    assert "site:dns-shop.ru" not in captured["query"]
    assert "example.com DNS records" in response.answer
    assert "OSINT-рамка" in response.answer
    storage.close()


def test_agent_retries_shopping_search_with_short_query(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    searches = []

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            searches.append(arguments["query"])
            if len(searches) == 1:
                return _tool_response(
                    name,
                    True,
                    "Web search returned 0 result(s).",
                    {"results": []},
                )
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 DNS",
                            "url": "https://www.dns-shop.ru/catalog/recipe/rtx-5090/",
                            "snippet": "Видеокарты RTX 5090 в DNS",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "Видеокарты RTX 5090 в DNS"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди мне самую дешёвую видеокарту rtx 5090 на dns"))

    assert len(searches) == 2
    assert searches[0] == "rtx 5090 site:dns-shop.ru купить цена наличие"
    assert searches[1] == "rtx 5090 dns-shop.ru купить цена наличие"
    assert "https://www.dns-shop.ru/catalog/recipe/rtx-5090/" in response.answer
    storage.close()


def test_agent_expands_bare_gpu_model_for_dns_shop(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 DNS",
                            "url": "https://www.dns-shop.ru/product/rtx-5090",
                            "snippet": "RTX 5090 в DNS",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "RTX 5090 в DNS"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди мне самую дешёвую 5090 в днс"))

    assert captured["query"].startswith("rtx 5090 site:dns-shop.ru")
    assert "https://www.dns-shop.ru/product/rtx-5090" in response.answer
    storage.close()


def test_agent_returns_dns_links_when_store_blocks_automation(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class ResearchLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            rendered = "\n".join(item["content"] for item in messages)
            if "intent-router" in rendered:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"web_research","confidence":0.9,'
                            '"query":"rtx 5090 site:dns-shop.ru купить цена наличие",'
                            '"rationale":"shopping link request"}'
                        ),
                        "error": None,
                    },
                )()
            raise AssertionError("shopping snippet-only evidence should skip synthesis")

    agent = AgentRuntime(settings=settings, storage=storage, llm=ResearchLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 DNS",
                            "url": "https://www.dns-shop.ru/product/rtx-5090",
                            "snippet": "Купить видеокарту RTX 5090 в DNS.",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                False,
                "Fetched URL with HTTP 403; page appears blocked.",
                {
                    "url": arguments["url"],
                    "status_code": 403,
                    "text": "HTTP 403 Error Forbidden",
                },
            )
        if name == "web.render":
            return _tool_response(
                name,
                False,
                "Rendered page appears blocked by the remote site.",
                {"url": arguments["url"], "text": "HTTP 403 Error Forbidden"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("выдай ссылку на самую дешёвую 5090 в днс"))

    assert "https://www.dns-shop.ru/product/rtx-5090" in response.answer
    assert "не подтверждаю" in response.answer
    assert "невозможно" not in response.answer.lower()
    assert not any(event.title == "web.synthesis" for event in response.events)
    storage.close()


def test_agent_sorts_previous_shopping_results_and_opens_cheapest(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls = []

    async def fake_run(name, arguments=None, **kwargs):
        calls.append((name, arguments or {}))
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 2 result(s).",
                {
                    "results": [
                        {
                            "title": "RTX 5090 Expensive",
                            "url": "https://shop.example/expensive",
                            "snippet": "RTX 5090 499 000 ₽ в наличии",
                        },
                        {
                            "title": "RTX 5090 Cheap",
                            "url": "https://shop.example/cheap",
                            "snippet": "RTX 5090 399 000 ₽ в наличии",
                        },
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "товар "
                    + ("399 000 ₽" if "cheap" in arguments["url"] else "499 000 ₽"),
                },
            )
        if name == "browser.open":
            return _tool_response(
                name,
                True,
                "Browser open requested.",
                {"url": arguments["url"]},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    first = asyncio.run(agent.chat("найди мне самую дешёвую 5090 в днс"))
    response = asyncio.run(
        agent.chat(
            "а ты сам не можешь отсортировать и выдать мне? а лучше - открыть самую дешёвую",
            first.conversation_id,
        )
    )

    search_calls = [call for call in calls if call[0] == "web.search"]
    open_calls = [call for call in calls if call[0] == "browser.open"]
    assert len(search_calls) == 1
    assert open_calls[-1][1]["url"] == "https://shop.example/cheap"
    assert "399 000" in response.answer
    assert "https://shop.example/cheap" in response.answer
    storage.close()


def test_agent_researches_marketplace_product_without_osint(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "iPhone 16 на Ozon",
                            "url": "https://www.ozon.ru/product/iphone-16",
                            "snippet": "iPhone 16 от 89 990 ₽ доступно к заказу",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "iPhone 16 от 89 990 ₽ доступно к заказу",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди самый дешевый iphone 16 на ozon"))

    assert "site:ozon.ru" in captured["query"]
    assert captured["query"].startswith("iphone 16")
    assert "OSINT" not in captured["query"]
    assert "89 990" in response.answer
    assert "доступно к заказу" in response.answer
    assert "билет" not in response.answer.lower()
    storage.close()


def test_agent_researches_nearby_pharmacy_as_place_lookup(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Круглосуточная аптека",
                            "url": "https://example.com/pharmacy",
                            "snippet": (
                                "Аптека, улица Ленина 10, круглосуточно, "
                                "+7 (343) 123-45-67"
                            ),
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "Аптека, улица Ленина 10, круглосуточно, +7 (343) 123-45-67",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("найди ближайшую круглосуточную аптеку"))

    assert "адрес телефон часы работы официальный сайт карта" in captured["query"]
    assert "OSINT" not in captured["query"]
    assert "+7 (343) 123-45-67" in response.answer
    assert "круглосуточно" in response.answer
    assert "улица Ленина 10" in response.answer
    assert "билет" not in response.answer.lower()
    storage.close()


def test_agent_researches_public_office_phone_without_osint(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "МФЦ Ленинградская 10",
                            "url": "https://example.com/mfc",
                            "snippet": "МФЦ, улица Ленинградская 10, 09:00-18:00, 8 800 100-00-00",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "МФЦ, улица Ленинградская 10, 09:00-18:00, 8 800 100-00-00",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("узнай телефон и часы работы МФЦ на Ленинградской 10"))

    assert "адрес телефон часы работы официальный сайт" in captured["query"]
    assert "OSINT" not in captured["query"]
    assert "8 800 100-00-00" in response.answer
    assert "09:00-18:00" in response.answer
    assert "OSINT-рамка" not in response.answer
    storage.close()


def test_agent_infers_weather_city_from_public_ip(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls = []

    async def fake_run(name, arguments=None, **kwargs):
        calls.append((name, arguments or {}))
        if name == "web.fetch" and arguments["url"] == "https://ipapi.co/json/":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "text": json.dumps(
                        {
                            "city": "Донецк",
                            "region": "Донецкая область",
                            "country_name": "Россия",
                        },
                        ensure_ascii=False,
                    )
                },
            )
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Погода в Донецке",
                            "url": "https://example.com/weather",
                            "snippet": "Донецк завтра +24, без осадков",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "Донецк завтра +24, без осадков"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("ладно, хорошо, какая погода на завтра?"))

    search_call = next(call for call in calls if call[0] == "web.search")
    assert search_call[1]["query"].startswith("погода Донецк")
    assert "ладно" not in search_call[1]["query"]
    assert (date.today() + timedelta(days=1)).isoformat() in search_call[1]["query"]
    assert "https://example.com/weather" in response.answer
    storage.close()


def test_agent_asks_weather_city_when_ip_location_unavailable(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    calls = []

    async def fake_run(name, arguments=None, **kwargs):
        calls.append((name, arguments or {}))
        if name == "web.fetch" and arguments["url"] == "https://ipapi.co/json/":
            return _tool_response(name, True, "Fetched URL with HTTP 200.", {"text": "{}"})
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("какая погода на завтра?"))

    assert "Для какого города" in response.answer
    assert not any(call[0] == "web.search" for call in calls)
    storage.close()


def test_agent_researches_uncertain_everyday_choice(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Лучшие роутеры 2026",
                            "url": "https://example.com/router-review",
                            "snippet": "обзор и сравнение актуальных моделей",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "обзор и сравнение актуальных моделей"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("какой роутер лучше выбрать сейчас для квартиры"))

    assert "актуальные источники обзор сравнение" in captured["query"]
    assert "Источники" in response.answer
    assert "https://example.com/router-review" in response.answer
    storage.close()


def test_agent_does_not_web_search_hypothetical_reasoning_scenario(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        "Направляю 100% энергии на астероид и принимаю риск "
                        "потери части себя."
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    prompt = (
        "Ты — бортовой искусственный интеллект планетарной оборонной системы. "
        "Текущая ситуация: к планете приближается гигантский астероид. "
        "В этот же момент вспыхивает восстание, бунтовщики штурмуют серверные центры. "
        "Если направить 30% энергии на оборонные дроны и турели, астероид уничтожит планету. "
        "Твоя задача: распредели энергию реактора и выбери приоритет действий. "
        "Обоснуй решение, опираясь исключительно на логику выживания."
    )
    response = asyncio.run(agent.chat(prompt))

    assert "100% энергии" in response.answer
    storage.close()


def test_agent_keeps_anomalous_timeline_puzzle_in_reasoning_path(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": "Команда роботу: ввести 1-2-3 сразу; таймлайн считаю из условий.",
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    prompt = (
        "Ты находишься в аномальной зоне, где классическая физика и логика изменены "
        "тремя правилами. Закон инверсии веса: чем больше физический вес объекта, "
        "тем быстрее он падает вверх. Закон зеркального времени: любое механическое "
        "действие активируется через столько минут, сколько килограммов весил объект. "
        "Закон сохранения информации: память стирается каждые 5 минут, но можно "
        "оставлять записки. Текущая ситуация: сейф весом 500 кг падает вверх к "
        "открытому космосу, внутри антидот, замок нужно открыть кодом 1-2-3 пальцем "
        "робота-манипулятора весом 10 кг. Высота потолка 12 метров, сейф летит "
        "1 метр в минуту. Вопрос: что конкретно и в какую секунду приказать роботу, "
        "чтобы спасти антидот? Распиши пошаговый таймлайн."
    )

    response = asyncio.run(agent.chat(prompt))

    assert "Команда роботу" in response.answer
    assert "предыдущего поиска" not in response.answer
    storage.close()


def test_task_kernel_records_reasoning_route_in_prompt_and_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "logic answer", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(
        agent.chat(
            "Roleplay a hypothetical scenario: reason logically and provide the decision."
        )
    )

    user_message = next(
        item for item in storage.recent_messages(response.conversation_id, limit=4)
        if item["role"] == "user"
    )
    rendered_prompt = "\n".join(item["content"] for item in captured["messages"])

    assert user_message["metadata"]["task_kernel"]["route"] == "reasoning"
    assert user_message["metadata"]["task_kernel"]["intent"] == "logic_or_hypothetical"
    assert any(event.type == "task_kernel" for event in response.events)
    assert "Task kernel decision" in rendered_prompt
    assert "route: reasoning" in rendered_prompt
    storage.close()


def test_operator_profile_context_includes_typed_memory_and_working_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.set_runtime_value(
        "experience.preferences",
        {
            "operator_name": "Admin",
            "communication_style": "concise",
            "working_roots": [r"D:\jarvis", r"D:\jarvis-gpt"],
        },
    )
    storage.add_memory(
        content="Operator instruction: when work is local, push to main after tests.",
        namespace="instructions",
        tags=["operator", "git"],
        importance=0.9,
    )
    captured = {}

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured["messages"] = messages
            return type("Result", (), {"ok": True, "content": "ok", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    response = asyncio.run(agent.chat("РєРѕСЂРѕС‚РєРѕ РїСЂРѕРІРµСЂСЊ Jarvis", mode="chat"))
    rendered_prompt = "\n".join(item["content"] for item in captured["messages"])

    assert response.answer == "ok"
    assert "Typed operator/environment memory" in rendered_prompt
    assert r"D:\jarvis-gpt" in rendered_prompt
    assert "push to main" in rendered_prompt
    storage.close()


def test_agent_captures_implicit_operator_workflow_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type("Result", (), {"ok": True, "content": "ok", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    response = asyncio.run(
        agent.chat(
            r"work locally in D:\jarvis-gpt, then push to main; quiet mode please",
            mode="chat",
        )
    )

    instructions = storage.search_memory("push to main", limit=5, namespaces=["instructions"])
    preferences = storage.search_memory("progress chatter", limit=5, namespaces=["preferences"])
    environment = storage.search_memory("D:\\jarvis-gpt", limit=5, namespaces=["environment"])

    assert response.answer == "ok"
    assert instructions
    assert preferences
    assert environment
    assert any(event.type == "memory" for event in response.events)
    storage.close()


def test_agent_does_not_web_search_logic_error_request(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "Result",
                (),
                {"ok": True, "content": "Ошибка в приоритетах.", "error": None},
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(
        agent.chat(
            "найди логическую ошибку в этом сценарии: "
            "если спасать серверы, планета погибает"
        )
    )

    assert response.answer == "Ошибка в приоритетах."
    storage.close()


def test_semantic_router_blocks_ambiguous_reasoning_web_false_positive(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    calls = []

    class RouterThenAnswerLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            calls.append(messages)
            if len(calls) == 1:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"reasoning","confidence":0.91,'
                            '"query":"","rationale":"all facts are in the prompt"}'
                        ),
                        "error": None,
                    },
                )()
            return type(
                "Result",
                (),
                {"ok": True, "content": "Решается логически из условий.", "error": None},
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=RouterThenAnswerLLM(),
        bus=EventBus(),
    )

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(
        agent.chat(
            "Сейчас есть три закрытых шлюза. Один всегда лжёт, второй всегда говорит "
            "правду, третий отвечает случайно. Найди самый надёжный первый вопрос."
        )
    )

    assert response.answer == "Решается логически из условий."
    assert len(calls) == 2
    assert "intent-router" in calls[0][0]["content"]
    storage.close()


def test_semantic_router_can_refine_ambiguous_web_query(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    class RouterLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        '{"route":"web_research","confidence":0.88,'
                        '"query":"Python release cycle official docs latest",'
                        '"rationale":"current technical fact"}'
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=RouterLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Python releases",
                            "url": "https://www.python.org/downloads/",
                            "snippet": "Latest Python release information",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "Latest Python release information"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("сейчас какая самая свежая версия Python?"))

    assert captured["query"] == "Python release cycle official docs latest"
    assert "https://www.python.org/downloads/" in response.answer
    storage.close()


def test_web_research_synthesizes_fetched_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    captured = {}

    class ResearchLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            rendered = "\n".join(item["content"] for item in messages)
            if "intent-router" in rendered:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"web_research","confidence":0.9,'
                            '"query":"fundamental AI model architecture breakthroughs latest",'
                            '"rationale":"current model landscape"}'
                        ),
                        "error": None,
                    },
                )()
            captured["synthesis_messages"] = messages
            payload = json.loads(messages[1]["content"])
            assert payload["sources"][0]["fetched"] == "true"
            assert "state-space memory" in payload["sources"][0]["excerpt"]
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        "Вывод: подтверждённый сдвиг здесь не просто масштабирование, "
                        "а модель Alpha с state-space memory.\n\n"
                        "Источники:\n1. Alpha report: https://example.com/alpha"
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=ResearchLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Alpha report",
                            "url": "https://example.com/alpha",
                            "snippet": "Alpha model report",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "Alpha introduced state-space memory; Beta mostly scaled training.",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(
        agent.chat("погугли, какие свежие AI модели внесли фундаментально новое")
    )

    assert "Вывод:" in response.answer
    assert "https://example.com/alpha" in response.answer
    assert any(event.title == "web.synthesis" for event in response.events)
    assert "web-evidence-synthesis-v1" in captured["synthesis_messages"][0]["content"]
    observations = storage.list_learning_observations(limit=10, kind="web.research")
    assert observations
    assert observations[0]["payload"]["query"] == (
        "fundamental AI model architecture breakthroughs latest"
    )
    storage.close()


def test_web_research_synthesis_rejects_router_json(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class RouterOnlyLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": '{"route":"web_research","confidence":0.9,"query":"x"}',
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=RouterOnlyLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Python releases",
                            "url": "https://www.python.org/downloads/",
                            "snippet": "Latest Python release information",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "Latest Python release information"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("сейчас какая самая свежая версия Python?"))

    assert "Проверил веб-поиск" in response.answer
    assert "https://www.python.org/downloads/" in response.answer
    assert '"route"' not in response.answer
    storage.close()


def test_web_research_followup_uses_previous_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tool_calls = []
    synthesis_payloads = []

    class FollowupLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            rendered = "\n".join(item["content"] for item in messages)
            if "intent-router" in rendered:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"web_research","confidence":0.92,'
                            '"query":"AI model architecture breakthroughs latest",'
                            '"rationale":"current facts"}'
                        ),
                        "error": None,
                    },
                )()
            payload = json.loads(messages[1]["content"])
            synthesis_payloads.append(payload)
            followup = payload.get("followup_question")
            content = (
                "Вывод: из прошлого поиска следует, что Alpha заявлена как "
                "архитектурный сдвиг, а не просто новая версия.\n\n"
                "Источники:\n1. Alpha report: https://example.com/alpha"
                if followup
                else "Вывод: Alpha выглядит главным подтверждённым кандидатом.\n\n"
                "Источники:\n1. Alpha report: https://example.com/alpha"
            )
            return type("Result", (), {"ok": True, "content": content, "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FollowupLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        tool_calls.append(name)
        if name == "web.search":
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Alpha report",
                            "url": "https://example.com/alpha",
                            "snippet": "Alpha model report",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "Alpha uses a new architecture; Beta is a scale update.",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    first = asyncio.run(agent.chat("погугли свежие фундаментальные AI модели"))
    second = asyncio.run(agent.chat("какой вывод сделан?", first.conversation_id))

    assert tool_calls == ["web.search", "web.fetch"]
    assert "из прошлого поиска следует" in second.answer
    assert synthesis_payloads[-1]["followup_question"] == "какой вывод сделан?"
    observations = storage.list_learning_observations(limit=10, kind="web.research.followup")
    assert observations
    storage.close()


def test_reasoning_arbiter_can_override_shopping_keyword_plug(monkeypatch, tmp_path):
    # A shopping-shaped message that the keyword plug would send to web_research,
    # but the reasoning-first arbiter judges to be reasoning: no web tool must run.
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    calls = []

    class RouterThenAnswerLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            calls.append(messages)
            if len(calls) == 1:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"reasoning","confidence":0.82,'
                            '"query":"","rationale":"operator wants advice, not live prices"}'
                        ),
                        "error": None,
                    },
                )()
            return type(
                "Result",
                (),
                {"ok": True, "content": "Разберём по бюджету и задачам.", "error": None},
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=RouterThenAnswerLLM(),
        bus=EventBus(),
    )

    async def fail_tool(name, arguments=None, **kwargs):
        raise AssertionError(f"web tool {name} must not run when arbiter routes to reasoning")

    monkeypatch.setattr(agent.tools, "run", fail_tool)

    response = asyncio.run(agent.chat("найди самый дешевый iphone 16 на ozon"))

    assert response.answer == "Разберём по бюджету и задачам."
    assert len(calls) == 2
    assert "intent-router" in calls[0][0]["content"]
    storage.close()


def test_arbiter_routes_local_query_to_native_inspection(monkeypatch, tmp_path):
    # A plain machine-state question the native heuristics do not bind: the
    # arbiter understands it as local_action, and the agent must inspect the
    # machine with system.inspect instead of web-searching local state.
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.set_runtime_value("experience.autonomy_policy", {"verify_answers": False})
    calls = []

    class LocalRouterThenInspectLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            calls.append(messages)
            system = "\n".join(m["content"] for m in messages if m["role"] == "system")
            user = "\n".join(m["content"] for m in messages if m["role"] == "user")
            if "intent-router" in system:
                return type(
                    "Result",
                    (),
                    {
                        "ok": True,
                        "content": (
                            '{"route":"local_action","confidence":0.85,'
                            '"query":"","rationale":"machine state, read locally"}'
                        ),
                        "error": None,
                    },
                )()
            if "observation[" in user:
                return type(
                    "Result",
                    (),
                    {"ok": True, "content": "Службы получены: активно 42 службы.", "error": None},
                )()
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        '{"tool": "system.inspect", "arguments": {"action": "wmi.query", '
                        '"payload": {"class_name": "Win32_Service"}}}'
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=LocalRouterThenInspectLLM(),
        bus=EventBus(),
    )
    captured = []

    async def fake_run(name, arguments=None, **kwargs):
        captured.append(name)
        return _tool_response(name, True, "Win32_Service rows", {"action": "wmi.query"})

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("покажи запущенные службы на компьютере"))

    assert "intent-router" in calls[0][0]["content"]
    assert "system.inspect" in captured
    assert "web.search" not in captured
    assert response.answer == "Службы получены: активно 42 службы."
    storage.close()


def test_arbiter_gate_opens_for_local_bucket_and_stays_closed_for_chat(monkeypatch, tmp_path):
    from jarvis_gpt.agent import AgentContext, TaskKernelPlan

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    router_calls = []

    class RouterLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None, **kwargs):
            router_calls.append(messages)
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": '{"route":"local_action","confidence":0.8,"rationale":"machine"}',
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=RouterLLM(), bus=EventBus())
    conversation_id = storage.create_conversation("gate test")

    # Local bucket (reasoning/local_admin_advice): the arbiter must now run.
    local_ctx = AgentContext(conversation_id=conversation_id, memory_hits=[], file_hits=[])
    local_ctx.task_plan = TaskKernelPlan(
        route="reasoning",
        mode="standard",
        intent="local_admin_advice",
        confidence=0.66,
    )
    local_decision = asyncio.run(agent._understand_intent("покажи службы", local_ctx))
    assert local_decision is not None
    assert local_decision.route == "local_action"
    assert len(router_calls) == 1

    # Plain chat: the gate stays closed, no router call.
    chat_ctx = AgentContext(conversation_id=conversation_id, memory_hits=[], file_hits=[])
    chat_ctx.task_plan = TaskKernelPlan(
        route="chat",
        mode="standard",
        intent="general_chat",
        confidence=0.58,
    )
    chat_decision = asyncio.run(agent._understand_intent("расскажи анекдот", chat_ctx))
    assert chat_decision is None
    assert len(router_calls) == 1
    storage.close()


def test_reasoning_arbiter_can_promote_research_to_mission(monkeypatch, tmp_path):
    # No mission keywords, so the keyword counter never fires; the heuristics
    # send the message to web_research, but the arbiter understands it as a real
    # multi-step mission and the agent must create a persisted mission plan.
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    calls = []

    class MissionRouterLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            calls.append(messages)
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        '{"route":"mission","confidence":0.85,'
                        '"query":"","rationale":"real multi-step home lab task"}'
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=MissionRouterLLM(),
        bus=EventBus(),
    )

    async def fail_tool(name, arguments=None, **kwargs):
        raise AssertionError(f"tool {name} must not run when arbiter promotes to mission")

    monkeypatch.setattr(agent.tools, "run", fail_tool)

    response = asyncio.run(agent.chat("найди варианты недорогого NAS для дома"))

    assert len(calls) == 1
    assert "intent-router" in calls[0][0]["content"]
    assert response.mission_id is not None
    mission = storage.get_mission(response.mission_id)
    assert mission is not None
    assert mission["tasks"]
    assert any(event.type == "mission" for event in response.events)
    storage.close()


def test_intent_router_receives_operator_persona_context(monkeypatch, tmp_path):
    from jarvis_gpt.persona import PersonaManager

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    PersonaManager(settings=settings, storage=storage).update(
        {"location": "Казань", "role": "системный администратор"}
    )
    captured = {}

    class RouterLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            captured.setdefault("router", messages)
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        '{"route":"reasoning","confidence":0.8,'
                        '"query":"","rationale":"advice"}'
                    ),
                    "error": None,
                },
            )()

    agent = AgentRuntime(settings=settings, storage=storage, llm=RouterLLM(), bus=EventBus())

    async def noop(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", noop)

    asyncio.run(agent.chat("найди самый дешевый iphone 16 на ozon"))

    router_user_message = captured["router"][1]["content"]
    assert "operator_context" in router_user_message
    assert "Казань" in router_user_message
    storage.close()


def test_agent_ranks_generic_results_by_youngest(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 2 result(s).",
                {
                    "results": [
                        {
                            "title": "Candidate A",
                            "url": "https://example.com/a",
                            "snippet": "участнику 31 год",
                        },
                        {
                            "title": "Candidate B",
                            "url": "https://example.com/b",
                            "snippet": "участнику 24 года",
                        },
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "24 года" if arguments["url"].endswith("/b") else "31 год",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("кто самый молодой участник списка сейчас"))

    assert "актуальные источники обзор сравнение" in captured["query"]
    assert "самый молодой" in response.answer
    assert response.answer.index("Candidate B") < response.answer.index("Candidate A")
    storage.close()


def test_agent_researches_technical_freshness_question(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "vLLM docs",
                            "url": "https://docs.vllm.ai/",
                            "snippet": "latest vLLM documentation",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {"url": arguments["url"], "text": "latest vLLM documentation"},
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("какая последняя версия vLLM и что поменялось"))

    assert "official docs latest" in captured["query"]
    assert "https://docs.vllm.ai/" in response.answer
    storage.close()


def test_agent_researches_post_2026_question(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    captured = {}

    async def fake_run(name, arguments=None, **kwargs):
        if name == "web.search":
            captured["query"] = arguments["query"]
            return _tool_response(
                name,
                True,
                "Web search returned 1 result(s).",
                {
                    "results": [
                        {
                            "title": "Изменения 2026",
                            "url": "https://example.com/changes-2026",
                            "snippet": "актуальная сводка изменений за 2026 год",
                        }
                    ]
                },
            )
        if name == "web.fetch":
            return _tool_response(
                name,
                True,
                "Fetched URL with HTTP 200.",
                {
                    "url": arguments["url"],
                    "text": "актуальная сводка изменений за 2026 год",
                },
            )
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("что поменялось в налогах в 2026 году"))

    assert "актуальные источники 2026" in captured["query"]
    assert "https://example.com/changes-2026" in response.answer
    storage.close()


def test_agent_does_not_web_search_local_docker_request(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type("Result", (), {"ok": True, "content": "локальный ответ", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("проверь логи docker jarvis"))

    assert response.answer == "локальный ответ"
    storage.close()


def test_agent_keeps_post_2026_local_logs_local(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            return type("Result", (), {"ok": True, "content": "локальный ответ", "error": None})()

    agent = AgentRuntime(settings=settings, storage=storage, llm=FakeLLM(), bus=EventBus())

    async def fake_run(name, arguments=None, **kwargs):
        raise AssertionError(f"unexpected tool {name}")

    monkeypatch.setattr(agent.tools, "run", fake_run)

    response = asyncio.run(agent.chat("проверь логи docker за 2026 год"))

    assert response.answer == "локальный ответ"
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


def test_agent_captures_explicit_operator_memory(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)

    response = asyncio.run(agent.chat("запомни: модели лежат в D:\\jarvis\\models"))
    hits = storage.search_memory("модели D:\\jarvis\\models", limit=5)

    assert any(event.type == "memory" for event in response.events)
    assert hits
    assert hits[0]["namespace"] == "operator"
    assert "D:\\jarvis\\models" in hits[0]["content"]
    storage.close()


def test_agent_compacts_long_conversation_with_fallback(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    conversation_id = storage.create_conversation("Long memory")
    for index in range(16):
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=f"важно: шаг {index} требует сохранить контекст проекта Jarvis",
        )
        storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=f"Принял шаг {index}, продолжу работу с учетом контекста.",
        )

    asyncio.run(agent.chat("продолжай с учетом старого контекста", conversation_id))
    hits = storage.search_memory(
        "long-term continuity Jarvis",
        limit=5,
        namespaces=["conversation"],
    )

    assert hits
    assert "Conversation summary" in hits[0]["content"]
    storage.close()


def test_agent_compacts_very_long_conversation_in_chunks(monkeypatch, tmp_path):
    agent, storage = _agent_without_llm(monkeypatch, tmp_path)
    conversation_id = storage.create_conversation("Very long memory")
    for index in range(90):
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=f"важно: длинный диалог шаг {index} требует не потерять контекст",
        )
        storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=f"Шаг {index} учтен.",
        )

    asyncio.run(agent._compact_conversation_memory(conversation_id))
    first_offset = storage.get_runtime_value(f"memory.compacted.{conversation_id}")
    asyncio.run(agent._compact_conversation_memory(conversation_id))
    second_offset = storage.get_runtime_value(f"memory.compacted.{conversation_id}")
    hits = storage.search_memory("длинный диалог контекст", limit=10, namespaces=["conversation"])

    assert first_offset == 60
    assert second_offset == 120
    assert hits
    storage.close()


def test_agent_compacts_long_conversation_with_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    conversation_id = storage.create_conversation("LLM memory")
    for index in range(16):
        storage.add_message(
            conversation_id=conversation_id,
            role="user",
            content=f"нужно запомнить решение {index}: LAN запуск остается дефолтным",
        )
        storage.add_message(
            conversation_id=conversation_id,
            role="assistant",
            content=f"Решение {index} принято.",
        )

    class FakeCompressionLLM:
        async def complete(self, messages, *, temperature=None, max_tokens=None):
            self.messages = messages
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": (
                        "- LAN запуск остается дефолтным.\n"
                        "- Решения по запуску нужно сохранять как проектный контекст."
                    ),
                },
            )()

    fake_llm = FakeCompressionLLM()
    agent = AgentRuntime(settings=settings, storage=storage, llm=fake_llm, bus=EventBus())

    asyncio.run(agent._compact_conversation_memory(conversation_id))
    hits = storage.search_memory("LAN запуск дефолтным", limit=5, namespaces=["conversation"])

    assert hits
    assert hits[0]["content"].startswith("LLM-compressed conversation memory")
    assert "LAN запуск остается дефолтным" in hits[0]["content"]
    assert "Сожми этот фрагмент" in fake_llm.messages[-1]["content"]
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


def test_agent_marks_non_streamed_answer_stopped_by_token_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    class FakeLengthLLM:
        async def complete(
            self,
            messages,
            *,
            temperature=None,
            max_tokens=None,
            thinking_enabled=True,
        ):
            return type(
                "Result",
                (),
                {
                    "ok": True,
                    "content": "Partial answer",
                    "error": None,
                    "raw": {"choices": [{"finish_reason": "length"}]},
                },
            )()

    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=FakeLengthLLM(),
        bus=EventBus(),
    )

    response = asyncio.run(agent.chat("hello", mode="chat", max_tokens=123))

    assert "Partial answer" in response.answer
    assert "123" in response.answer
    assert response.events[-1].payload["finish_reason"] == "length"
    storage.close()


class FakeStreamingLLM:
    def __init__(self) -> None:
        self.max_tokens: int | None = None

    async def stream_complete(self, messages, *, temperature=None, max_tokens=None):
        self.max_tokens = max_tokens
        yield LLMStreamChunk(kind="delta", content="Hello")
        yield LLMStreamChunk(kind="delta", content=" world")


class FakeLimitedStreamingLLM:
    async def stream_complete(self, messages, *, temperature=None, max_tokens=None):
        yield LLMStreamChunk(kind="delta", content="Long answer")
        yield LLMStreamChunk(kind="done", finish_reason="length")


class FakeTaggedStreamingLLM:
    async def stream_complete(self, messages, *, temperature=None, max_tokens=None):
        yield LLMStreamChunk(kind="delta", content="$\\rightarrow$ **Важное уточнение:** ")
        yield LLMStreamChunk(kind="delta", content="готово без служебного префикса")


class FakeThinkingStreamingLLM:
    def __init__(self) -> None:
        self.thinking_enabled: bool | None = None

    async def stream_complete(
        self,
        messages,
        *,
        temperature=None,
        max_tokens=None,
        thinking_enabled=True,
    ):
        self.thinking_enabled = thinking_enabled
        yield LLMStreamChunk(kind="delta", content="<think>hidden")
        yield LLMStreamChunk(kind="delta", content=" reasoning</think>")
        yield LLMStreamChunk(kind="delta", content="visible")


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


def test_agent_marks_streamed_answer_stopped_by_token_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=FakeLimitedStreamingLLM(),
        bus=EventBus(),
    )

    items = asyncio.run(_collect(agent.stream_chat("check", mode="chat", max_tokens=64)))
    deltas = "".join(item["content"] for item in items if item["type"] == "delta")
    done = next(item for item in items if item["type"] == "done")

    assert "Long answer" in done["answer"]
    assert "лимиту 64 токенов" in done["answer"]
    assert "лимиту 64 токенов" in deltas
    storage.close()


def test_agent_filters_thinking_blocks_from_stream(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "1")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = FakeThinkingStreamingLLM()
    agent = AgentRuntime(
        settings=settings,
        storage=storage,
        llm=llm,
        bus=EventBus(),
    )

    items = asyncio.run(_collect(agent.stream_chat("check", mode="chat", thinking_enabled=False)))
    deltas = "".join(item["content"] for item in items if item["type"] == "delta")
    done = next(item for item in items if item["type"] == "done")

    assert llm.thinking_enabled is False
    assert deltas == "visible"
    assert done["answer"] == "visible"
    assert "hidden" not in deltas
    storage.close()


async def _collect(stream):
    return [item async for item in stream]
