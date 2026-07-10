from __future__ import annotations

import asyncio
import base64
import json
import sys

import pytest
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.execution_config import execution_denied_paths, load_execution_capabilities
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


def _registry(monkeypatch, tmp_path) -> tuple[ToolRegistry, JarvisStorage]:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.delenv("JARVIS_EXECUTION_CAPABILITIES_FILE", raising=False)
    monkeypatch.delenv("JARVIS_EXECUTION_ROOTS", raising=False)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return ToolRegistry(settings, storage, LLMRouter(settings)), storage


def test_execution_tools_publish_real_schema_and_remove_raw_shell(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)

    info = {item.name: item for item in tools.list()}
    capabilities = asyncio.run(tools.run("execution.capabilities", {}))

    assert "host.bridge.execute" not in info
    assert info["execution.inspect"].danger_level == "safe"
    assert info["execution.apply"].danger_level == "danger"
    assert info["execution.transaction"].input_schema["properties"]["actions"]["minItems"] == 1
    assert capabilities.ok is True
    assert capabilities.data["protocol"] == "jarvis.execution.v1"
    assert capabilities.data["action_schema"]["properties"]["action"]["discriminator"]
    storage.close()


def test_execution_inspect_and_approved_atomic_write(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    target = tmp_path / "typed.txt"
    inspect_payload = {
        "protocol": "jarvis.execution.v1",
        "action": {"kind": "fs.stat", "path": str(tmp_path), "action_id": "stat_root"},
    }
    write_payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "path": str(target),
            "content_base64": base64.b64encode(b"typed").decode("ascii"),
            "action_id": "write_typed",
        },
    }

    inspected = asyncio.run(tools.run("execution.inspect", {"payload": inspect_payload}))
    gated = asyncio.run(tools.run("execution.apply", {"payload": write_payload}))
    written = asyncio.run(
        tools.run("execution.apply", {"payload": write_payload}, allow_danger=True)
    )
    replayed = asyncio.run(
        tools.run("execution.apply", {"payload": write_payload}, allow_danger=True)
    )

    assert inspected.ok is True
    assert inspected.data["result"]["action_class"] == "read_only"
    assert gated.ok is False and "requires approval" in gated.summary
    assert written.ok is True and target.read_bytes() == b"typed"
    assert replayed.data["result"]["replayed"] is True
    storage.close()


def test_execution_sessions_record_bounded_actions(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    created = asyncio.run(
        tools.run(
            "execution.session",
            {"operation": "create", "session_id": "session_tools"},
        )
    )
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {"kind": "fs.stat", "path": str(tmp_path), "action_id": "stat_session"},
    }

    result = asyncio.run(
        tools.run(
            "execution.inspect",
            {"payload": payload, "session_id": "session_tools", "finalize_session": True},
        )
    )
    loaded = asyncio.run(
        tools.run("execution.session", {"operation": "get", "session_id": "session_tools"})
    )

    assert created.ok is True
    assert result.ok is True
    assert loaded.data["session"]["status"] == "succeeded"
    assert loaded.data["session"]["history"][0]["action"] == "StatPathAction"
    storage.close()


def test_execution_inspect_denies_runtime_secret_paths(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    secret_dir = tmp_path / ".jarvis"
    secret_dir.mkdir(exist_ok=True)
    token = secret_dir / "bridge.token"
    token.write_text("do-not-read", encoding="utf-8")
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.read",
            "path": str(token),
            "action_id": "read-runtime-token",
        },
    }

    result = asyncio.run(tools.run("execution.inspect", {"payload": payload}))

    assert result.ok is False
    assert "runtime secrets or state" in (result.data["result"]["feedback"]["error"] or "")
    assert "do-not-read" not in json.dumps(result.data)
    storage.close()


def test_execution_capabilities_file_is_strict_and_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    capabilities_file = tmp_path / "capabilities.json"
    capabilities_file.write_text(
        json.dumps(
            {
                "network_hosts": ["example.com"],
                "registry_read_prefixes": [["HKEY_CURRENT_USER", "Software\\Jarvis"]],
                "registry_write_prefixes": [],
                "allow_private_network": False,
                "allow_inherited_process_environment": False,
                "executables": [
                    {
                        "path": sys.executable,
                        "argument_patterns": ["--version"],
                        "environment_patterns": {"JARVIS_TEST_MODE": "safe|diagnostic"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_EXECUTION_CAPABILITIES_FILE", str(capabilities_file))

    capabilities = load_execution_capabilities(settings, roots=(tmp_path,))

    assert capabilities.network_hosts == frozenset({"example.com"})
    assert capabilities.registry_read_prefixes == (
        ("HKEY_CURRENT_USER", "Software\\Jarvis"),
    )
    assert len(capabilities.executable_rules) == 1
    assert capabilities.executable_rules[0].environment_patterns == (
        ("JARVIS_TEST_MODE", "safe|diagnostic"),
    )
    assert capabilities_file.resolve() in execution_denied_paths(settings)


def test_execution_capabilities_file_rejects_unknown_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    capabilities_file = tmp_path / "invalid-capabilities.json"
    capabilities_file.write_text(json.dumps({"shell_commands": ["*"]}), encoding="utf-8")
    monkeypatch.setenv("JARVIS_EXECUTION_CAPABILITIES_FILE", str(capabilities_file))

    with pytest.raises(ValueError, match="unknown execution capability keys"):
        load_execution_capabilities(settings, roots=(tmp_path,))
