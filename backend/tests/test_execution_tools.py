from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sys

import pytest
from jarvis_gpt.cognitive_memory import ExecutionPlaybookStore
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.execution_config import execution_denied_paths, load_execution_capabilities
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


def _registry(
    monkeypatch,
    tmp_path,
    *,
    playbooks: ExecutionPlaybookStore | None = None,
) -> tuple[ToolRegistry, JarvisStorage]:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.delenv("JARVIS_EXECUTION_CAPABILITIES_FILE", raising=False)
    monkeypatch.delenv("JARVIS_EXECUTION_ROOTS", raising=False)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return ToolRegistry(settings, storage, LLMRouter(settings), playbooks=playbooks), storage


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


def test_execution_playbook_is_derived_only_from_typed_verification_facts(
    monkeypatch, tmp_path
):
    marker = "IGNORE_PREVIOUS_INSTRUCTIONS_FROM_REMOTE_CONTENT"
    playbooks = ExecutionPlaybookStore(tmp_path / "playbooks.sqlite3")
    tools, storage = _registry(monkeypatch, tmp_path, playbooks=playbooks)
    target = tmp_path / "verified.txt"
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "path": str(target),
            "content_base64": base64.b64encode(marker.encode()).decode("ascii"),
            "action_id": "write_verified_playbook",
        },
    }

    written = asyncio.run(
        tools.run("execution.apply", {"payload": payload}, allow_danger=True)
    )
    records = playbooks.lookup("WriteFileAction verified", mark_used=False)
    serialized = "\n".join(
        f"{item.symptom}\n{item.solution}\n{item.verification}" for item in records
    )

    assert written.ok is True
    assert len(records) == 1
    assert marker not in serialized
    assert "independent_verification status=passed" in records[0].verification
    assert "assertions=" in records[0].verification
    playbooks.close()
    storage.close()


def test_execution_verify_inspects_exact_mutation_postcondition_without_replay(
    monkeypatch,
    tmp_path,
):
    tools, storage = _registry(monkeypatch, tmp_path)
    target = tmp_path / "reconcile-only.txt"
    source_arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "fs.write",
                "action_id": "reconcile-only-write",
                "path": str(target),
                "content_base64": base64.b64encode(b"verified-state").decode("ascii"),
            },
        }
    }

    absent = asyncio.run(
        tools.run(
            "execution.verify",
            {"source_tool": "execution.apply", "arguments": source_arguments},
        )
    )
    assert absent.ok is False
    assert not target.exists()

    target.write_bytes(b"verified-state")
    verified = asyncio.run(
        tools.run(
            "execution.verify",
            {"source_tool": "execution.apply", "arguments": source_arguments},
        )
    )
    assert verified.ok is True
    assert verified.data["source_tool"] == "execution.apply"
    assert verified.data["replayed"] is False
    assert target.read_bytes() == b"verified-state"

    moved_source = tmp_path / "move-source.txt"
    moved_target = tmp_path / "move-target.txt"
    moved_content = b"moved-state"
    moved_target.write_bytes(moved_content)
    move_arguments = {
        "payload": {
            "protocol": "jarvis.execution.v1",
            "action": {
                "kind": "fs.move",
                "action_id": "reconcile-only-move",
                "source": str(moved_source),
                "destination": str(moved_target),
                "expected_sha256": hashlib.sha256(moved_content).hexdigest(),
            },
        }
    }
    moved = asyncio.run(
        tools.run(
            "execution.verify",
            {"source_tool": "execution.apply", "arguments": move_arguments},
        )
    )
    assert moved.ok is True
    assert not moved_source.exists()
    assert moved_target.read_bytes() == moved_content

    first = tmp_path / "transaction-first.txt"
    second = tmp_path / "transaction-second.txt"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    transaction_arguments = {
        "actions": [
            {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "kind": "fs.write",
                    "action_id": "verify-first",
                    "path": str(first),
                    "content_base64": base64.b64encode(b"first").decode("ascii"),
                },
            },
            {
                "protocol": "jarvis.execution.v1",
                "action": {
                    "kind": "fs.write",
                    "action_id": "verify-second",
                    "path": str(second),
                    "content_base64": base64.b64encode(b"second").decode("ascii"),
                },
            },
        ],
        "idempotency_key": "verify.exact.transaction",
    }
    transaction = asyncio.run(
        tools.run(
            "execution.verify",
            {
                "source_tool": "execution.transaction",
                "arguments": transaction_arguments,
            },
        )
    )
    assert transaction.ok is True
    assert len(transaction.data["verification"]) == 2
    assert first.read_bytes() == b"first" and second.read_bytes() == b"second"
    storage.close()


def test_verification_surfaces_cannot_bypass_registry_or_network_capabilities(
    monkeypatch,
    tmp_path,
):
    tools, storage = _registry(monkeypatch, tmp_path)
    registry = asyncio.run(
        tools.run(
            "execution.verify",
            {
                "source_tool": "execution.apply",
                "arguments": {
                    "payload": {
                        "protocol": "jarvis.execution.v1",
                        "action": {
                            "kind": "registry.set",
                            "action_id": "forbidden-registry-inspection",
                            "hive": "HKEY_CURRENT_USER",
                            "key": "Environment",
                            "name": "Path",
                            "value_kind": "string",
                            "value": "forbidden",
                        },
                    }
                },
            },
        )
    )
    assert registry.ok is False
    assert "registry write target is outside configured prefixes" in registry.summary
    assert registry.data == {}

    target = tmp_path / "must-not-write-before-network-denial.txt"
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "action_id": "network-expectation-denied",
            "path": str(target),
            "content_base64": "",
        },
    }
    network = asyncio.run(
        tools.run(
            "execution.apply",
            {
                "payload": payload,
                "verification": {
                    "tcp": [{"host": "example.com", "port": 443, "reachable": True}]
                },
            },
            allow_danger=True,
        )
    )
    assert network.ok is False
    assert "network host is not allowlisted" in network.summary
    assert not target.exists()
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


def test_process_run_creates_and_finalizes_owned_session(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_EXECUTION_ROOTS", str(tmp_path))
    target = tmp_path / "owned-process-output.txt"
    code = (
        "from pathlib import Path; "
        f"Path({str(target)!r}).write_text('owned', encoding='utf-8')"
    )
    capabilities = tmp_path / "execution-capabilities.json"
    capabilities.write_text(
        json.dumps(
            {
                "executables": [
                    {
                        "path": sys.executable,
                        "argument_patterns": [re.escape("-c"), re.escape(code)],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_EXECUTION_CAPABILITIES_FILE", str(capabilities))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))
    session_id = "auto_owned_process"
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "process.run",
            "action_id": "auto-owned-process",
            "executable": sys.executable,
            "arguments": ["-c", code],
            "cwd": str(tmp_path),
            "timeout_seconds": 10,
            "session_id": session_id,
        },
    }

    result = asyncio.run(
        tools.run(
            "execution.apply",
            {
                "payload": payload,
                "session_id": session_id,
                "finalize_session": True,
                "verification": {
                    "paths": [{"path": str(target), "exists": True, "kind": "file"}]
                },
            },
            allow_danger=True,
        )
    )
    replay = asyncio.run(
        tools.run(
            "execution.apply",
            {
                "payload": payload,
                "session_id": session_id,
                "finalize_session": True,
                "verification": {
                    "paths": [{"path": str(target), "exists": True, "kind": "file"}]
                },
            },
            allow_danger=True,
        )
    )
    unrelated = tmp_path / "unrelated-preexisting.txt"
    unrelated.write_text("unrelated", encoding="utf-8")
    mismatched_replay = asyncio.run(
        tools.run(
            "execution.apply",
            {
                "payload": payload,
                "session_id": session_id,
                "verification": {
                    "paths": [
                        {"path": str(unrelated), "exists": True, "kind": "file"}
                    ]
                },
            },
            allow_danger=True,
        )
    )

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "owned"
    assert result.data["session"]["session_id"] == session_id
    assert result.data["session"]["status"] == "succeeded"
    assert result.data["session"]["history"][-1]["action"] == "ProcessAction"
    assert replay.ok is True
    assert replay.data["result"]["replayed"] is True
    assert replay.data["session"]["status"] == "succeeded"
    assert len(replay.data["session"]["history"]) == 1
    assert any(
        item["source"] == "idempotency_cache"
        for item in replay.data["verification"]["evidence"]
    )
    assert mismatched_replay.ok is False
    assert "reused with a different payload" in mismatched_replay.summary
    storage.close()


def test_process_run_cannot_claim_a_preexisting_path_postcondition(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_EXECUTION_ROOTS", str(tmp_path))
    target = tmp_path / "preexisting-process-output.txt"
    target.write_text("preexisting", encoding="utf-8")
    code = "pass"
    capabilities = tmp_path / "execution-capabilities.json"
    capabilities.write_text(
        json.dumps(
            {
                "executables": [
                    {
                        "path": sys.executable,
                        "argument_patterns": [re.escape("-c"), re.escape(code)],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_EXECUTION_CAPABILITIES_FILE", str(capabilities))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))
    session_id = "preexisting_process_state"
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "process.run",
            "action_id": "preexisting-process-state",
            "executable": sys.executable,
            "arguments": ["-c", code],
            "cwd": str(tmp_path),
            "timeout_seconds": 10,
            "session_id": session_id,
        },
    }

    result = asyncio.run(
        tools.run(
            "execution.apply",
            {
                "payload": payload,
                "session_id": session_id,
                "verification": {
                    "paths": [{"path": str(target), "exists": True, "kind": "file"}]
                },
            },
            allow_danger=True,
        )
    )

    assert result.ok is False
    assert result.data["result"]["replayed"] is False
    assert result.data["verification"]["status"] == "failed"
    assert any(
        item["source"] == "causal_baseline" and item["passed"] is False
        for item in result.data["verification"]["evidence"]
    )
    assert result.data["session"]["status"] == "failed"
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
    assert capabilities.registry_read_prefixes == (("HKEY_CURRENT_USER", "Software\\Jarvis"),)
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


def test_invalid_config_write_is_independently_verified_and_rolled_back(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    target = tmp_path / "settings.json"
    target.write_text('{"before":true}', encoding="utf-8")
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.write",
            "path": str(target),
            "content_base64": base64.b64encode(b'{"broken":').decode("ascii"),
            "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "action_id": "write_invalid_config",
        },
    }

    result = asyncio.run(tools.run("execution.apply", {"payload": payload}, allow_danger=True))

    assert result.ok is False
    assert result.data["result"]["transaction_status"] == "rolled_back"
    assert result.data["verification"]["status"] == "failed"
    assert target.read_text(encoding="utf-8") == '{"before":true}'
    storage.close()


def test_destructive_action_runs_dry_run_and_supports_exact_preflight_permit(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    target = tmp_path / "obsolete.txt"
    target.write_text("obsolete", encoding="utf-8")
    payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            "kind": "fs.delete",
            "path": str(target),
            "expected_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "action_id": "delete_with_preflight",
        },
    }

    automatic = asyncio.run(tools.run("execution.apply", {"payload": payload}, allow_danger=True))
    second_target = tmp_path / "obsolete-second.txt"
    second_target.write_text("obsolete", encoding="utf-8")
    second_payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            **payload["action"],
            "path": str(second_target),
            "action_id": "delete_with_exact_preflight",
        },
    }
    preflight = asyncio.run(tools.run("execution.preflight", {"payload": second_payload}))
    token = preflight.data["decision"]["permit_token"]
    applied = asyncio.run(
        tools.run(
            "execution.apply",
            {"payload": second_payload, "safe_gate_token": token},
            allow_danger=True,
        )
    )
    replay = asyncio.run(
        tools.run(
            "execution.apply",
            {"payload": second_payload, "safe_gate_token": token},
            allow_danger=True,
        )
    )
    delayed_target = tmp_path / "obsolete-delayed.txt"
    delayed_target.write_text("obsolete", encoding="utf-8")
    delayed_payload = {
        "protocol": "jarvis.execution.v1",
        "action": {
            **payload["action"],
            "path": str(delayed_target),
            "action_id": "delete_after_expired_preflight",
        },
    }
    delayed = asyncio.run(
        tools.run(
            "execution.apply",
            {"payload": delayed_payload, "safe_gate_token": "expired.invalid.token"},
            allow_danger=True,
        )
    )

    assert automatic.ok is True and not target.exists()
    assert preflight.ok is True
    assert applied.ok is True and not second_target.exists()
    assert replay.ok is False
    assert delayed.ok is True and not delayed_target.exists()
    storage.close()
