from __future__ import annotations

import asyncio
import threading

from jarvis_gpt.config import load_settings
from jarvis_gpt.host_bridge import HostBridgeClient, HostBridgeStatus


def test_host_bridge_client_posts_structured_action(monkeypatch, tmp_path):
    import jarvis_gpt.host_bridge as host_bridge

    bridge_script = _load_bridge_module()
    token = "client-test-token"
    server = bridge_script.BridgeServer(("127.0.0.1", 0), token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(host_bridge, "BRIDGE_PORT", server.server_address[1])
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    token_path = tmp_path / ".jarvis" / "bridge.token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text(token, encoding="utf-8")
    settings = load_settings()
    try:
        result = asyncio.run(
            HostBridgeClient(settings).action(
                action="capabilities",
                payload={},
                timeout_sec=5,
            )
        )
        status = HostBridgeStatus(settings).snapshot()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["contract"] == "action.v1"
    assert result["data"]["raw_command_execution"] is False
    assert result["data"]["policy_revision"] == "native-app-v2"
    assert status["action_v1_ready"] is True
    assert status["capabilities_probe"]["raw_command_execution"] is False
    assert status["capabilities_probe"]["policy_revision"] == "native-app-v2"
    assert status["capabilities_probe"]["app_paths_sha256"] == result["data"][
        "app_paths_sha256"
    ]


def test_host_bridge_client_rejects_unknown_action_without_network(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()

    result = asyncio.run(
        HostBridgeClient(settings).action(action="shell.execute", payload={})
    )

    assert result["ok"] is False
    assert "Unsupported" in result["summary"]


def test_host_bridge_status_requires_authenticated_capabilities(monkeypatch, tmp_path):
    import jarvis_gpt.host_bridge as host_bridge

    bridge_script = _load_bridge_module()
    server = bridge_script.BridgeServer(("127.0.0.1", 0), "server-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(host_bridge, "BRIDGE_PORT", server.server_address[1])
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    token_path = tmp_path / ".jarvis" / "bridge.token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("wrong-token", encoding="utf-8")
    settings = load_settings()
    try:
        status = HostBridgeStatus(settings).snapshot()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status["health"]["ok"] is True
    assert status["health"]["contract"] == "action.v1"
    assert status["capabilities_probe"]["ok"] is False
    assert status["capabilities_probe"]["status_code"] == 401
    assert status["action_v1_ready"] is False


def test_host_bridge_status_rejects_stale_process_policy(monkeypatch, tmp_path):
    import jarvis_gpt.host_bridge as host_bridge

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    token_path = tmp_path / ".jarvis" / "bridge.token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("token", encoding="utf-8")
    monkeypatch.setattr(
        host_bridge,
        "_bridge_health",
        lambda: {"ok": True, "contract": "action.v1"},
    )
    monkeypatch.setattr(
        host_bridge,
        "_bridge_capabilities",
        lambda _token: {
            "ok": True,
            "contract": "action.v1",
            "raw_command_execution": False,
            "policy_revision": "legacy-unrestricted-native",
        },
    )

    status = HostBridgeStatus(load_settings()).snapshot()

    assert status["action_v1_ready"] is False


def _load_bridge_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "windows_rpc_bridge.py"
    spec = importlib.util.spec_from_file_location("windows_rpc_bridge_client_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
