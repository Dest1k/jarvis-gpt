from __future__ import annotations

import asyncio

from jarvis_gpt.approval_executor import ApprovalExecutor
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.dispatcher import DispatcherManager
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


def test_approval_executor_runs_memory_save(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Save lesson",
        description="Persist operator-approved lesson.",
        requested_action="memory.save",
        risk="review",
        payload={
            "content": "Approved actions must execute only through the gated executor.",
            "namespace": "learning",
            "tags": ["approval", "executor"],
        },
    )
    storage.update_approval(approval["id"], status="approved", result={"operator": "test"})

    result = asyncio.run(executor.execute(approval["id"]))
    updated = storage.get_approval(approval["id"])
    hits = storage.search_memory("gated executor", limit=5)

    assert result.ok is True
    assert result.approval is not None
    assert result.approval["status"] == "executed"
    assert updated is not None
    assert updated["result"]["ok"] is True
    assert hits
    storage.close()


def test_approval_executor_requires_approved_status(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Pending action",
        description="Still pending.",
        requested_action="memory.save",
        payload={"content": "not yet"},
    )

    result = asyncio.run(executor.execute(approval["id"]))

    assert result.ok is False
    assert result.status_code == 409
    assert storage.get_approval(approval["id"])["status"] == "pending"
    storage.close()


def test_approval_executor_rejects_unknown_action(monkeypatch, tmp_path):
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Unknown action",
        description="Should not execute.",
        requested_action="host.shell",
        risk="danger",
        payload={"command": "whoami"},
    )
    storage.update_approval(approval["id"], status="approved", result={"operator": "test"})

    result = asyncio.run(executor.execute(approval["id"]))

    assert result.ok is False
    assert result.status_code == 400
    assert result.finalize is False
    assert storage.get_approval(approval["id"])["status"] == "approved"
    storage.close()


def test_approval_executor_can_run_danger_tool_after_approval(monkeypatch, tmp_path):
    async def fake_execute(self, *, command, cwd=None, timeout_sec=30):
        return {
            "ok": True,
            "summary": f"fake bridge: {command}",
            "data": {"stdout": "approved\n", "cwd": cwd, "timeout_sec": timeout_sec},
        }

    monkeypatch.setattr("jarvis_gpt.host_bridge.HostBridgeClient.execute", fake_execute)
    executor, storage = _runtime(monkeypatch, tmp_path)
    approval = storage.create_approval(
        title="Run host command",
        description="Danger tool requires an approved gate.",
        requested_action="tool.run",
        risk="danger",
        payload={
            "tool": "host.bridge.execute",
            "arguments": {"command": "Write-Output approved"},
        },
    )
    storage.update_approval(approval["id"], status="approved", result={"operator": "test"})

    result = asyncio.run(executor.execute(approval["id"]))

    assert result.ok is True
    assert result.approval is not None
    assert result.approval["status"] == "executed"
    assert result.data["tool_run"]["tool"] == "host.bridge.execute"
    assert result.data["tool_run"]["ok"] is True
    storage.close()


def _runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    llm = LLMRouter(settings)
    tools = ToolRegistry(settings, storage, llm)
    executor = ApprovalExecutor(
        storage=storage,
        llm=llm,
        dispatcher=DispatcherManager(settings, repo_root=tmp_path),
        tools=tools,
    )
    return executor, storage
