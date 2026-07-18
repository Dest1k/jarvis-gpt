from __future__ import annotations

import asyncio
import threading

import httpx
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
    assert result["outcome_known"] is True
    assert result["retryable"] is False
    assert result["attempts"] == 1
    assert result["data"]["raw_command_execution"] is False
    assert result["data"]["policy_revision"] == "native-app-v3"
    assert result["data"]["browser_network_guard"]["version"] == "public-proxy-v1"
    assert status["action_v1_ready"] is True
    assert status["capabilities_probe"]["raw_command_execution"] is False
    assert status["capabilities_probe"]["policy_revision"] == "native-app-v3"
    assert status["browser_network_guard"]["fail_closed"] is True
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


def test_read_only_host_action_retries_transport_and_5xx(monkeypatch, tmp_path):
    import jarvis_gpt.host_bridge as host_bridge

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr(host_bridge, "read_bridge_token", lambda _settings: "token")
    monkeypatch.setattr(host_bridge, "_BRIDGE_RETRY_BASE_DELAY_SEC", 0.0)
    request = httpx.Request("POST", "http://bridge.test/action")
    outcomes: list[Exception | httpx.Response] = [
        httpx.ConnectError("bridge starting", request=request),
        httpx.Response(
            503,
            request=request,
            json={"ok": False, "summary": "temporarily unavailable"},
        ),
        httpx.Response(
            200,
            request=request,
            json={"ok": True, "summary": "ready"},
        ),
    ]
    calls = 0

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(host_bridge.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(
        HostBridgeClient(load_settings()).action(
            action="window.list",
            payload={"limit": 10},
        )
    )

    assert result["ok"] is True
    assert result["outcome_known"] is True
    assert result["retryable"] is False
    assert result["attempts"] == 3
    assert calls == 3


def test_mutating_host_action_never_retries_transport_failure(monkeypatch, tmp_path):
    import jarvis_gpt.host_bridge as host_bridge

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr(host_bridge, "read_bridge_token", lambda _settings: "token")
    monkeypatch.setattr(
        host_bridge.HostBridgeStatus,
        "snapshot",
        lambda _self: {"ok": False},
    )
    request = httpx.Request("POST", "http://bridge.test/action")
    calls = 0

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("uncertain", request=request)

    monkeypatch.setattr(host_bridge.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(
        HostBridgeClient(load_settings()).action(
            action="keyboard.send",
            payload={"keys": "ENTER"},
        )
    )

    assert result["ok"] is False
    assert result["outcome_known"] is False
    assert result["retryable"] is False
    assert result["attempts"] == 1
    assert calls == 1


def test_mutating_host_action_never_retries_5xx(monkeypatch, tmp_path):
    import jarvis_gpt.host_bridge as host_bridge

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr(host_bridge, "read_bridge_token", lambda _settings: "token")
    request = httpx.Request("POST", "http://bridge.test/action")
    calls = 0

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                503,
                request=request,
                json={"ok": False, "summary": "bridge failed"},
            )

    monkeypatch.setattr(host_bridge.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(
        HostBridgeClient(load_settings()).action(
            action="process.start",
            payload={"executable": "notepad.exe"},
        )
    )

    assert result["ok"] is False
    assert result["outcome_known"] is False
    assert result["retryable"] is False
    assert result["attempts"] == 1
    assert calls == 1


def test_malformed_success_body_is_never_truthy_success(monkeypatch, tmp_path):
    import jarvis_gpt.host_bridge as host_bridge

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr(host_bridge, "read_bridge_token", lambda _settings: "token")
    request = httpx.Request("POST", "http://bridge.test/action")

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs):
            return httpx.Response(
                200,
                request=request,
                json={"ok": "false", "summary": "malformed"},
            )

    monkeypatch.setattr(host_bridge.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(
        HostBridgeClient(load_settings()).action(
            action="keyboard.send",
            payload={"keys": "ENTER"},
        )
    )

    assert result["ok"] is False
    assert result["outcome_known"] is False
    assert result["retryable"] is False
    assert result["attempts"] == 1


def test_screen_capture_is_not_retried_after_transport_uncertainty(monkeypatch, tmp_path):
    import jarvis_gpt.host_bridge as host_bridge

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr(host_bridge, "read_bridge_token", lambda _settings: "token")
    request = httpx.Request("POST", "http://bridge.test/action")
    calls = 0

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("capture outcome unknown", request=request)

    monkeypatch.setattr(host_bridge.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(
        HostBridgeClient(load_settings()).action(
            action="screen.capture",
            payload={"path": str(tmp_path / "capture.png")},
        )
    )

    assert result["ok"] is False
    assert result["outcome_known"] is False
    assert result["retryable"] is False
    assert result["attempts"] == 1
    assert calls == 1


def test_read_only_host_action_exhaustion_remains_retryable(monkeypatch, tmp_path):
    import jarvis_gpt.host_bridge as host_bridge

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr(host_bridge, "read_bridge_token", lambda _settings: "token")
    monkeypatch.setattr(host_bridge, "_BRIDGE_RETRY_BASE_DELAY_SEC", 0.0)
    request = httpx.Request("POST", "http://bridge.test/action")
    calls = 0

    class FakeClient:
        def __init__(self, *_args, **_kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def post(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                500,
                request=request,
                json={"ok": False, "summary": "temporary read failure"},
            )

    monkeypatch.setattr(host_bridge.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(
        HostBridgeClient(load_settings()).action(action="window.list", payload={})
    )

    assert result["ok"] is False
    assert result["outcome_known"] is False
    assert result["retryable"] is True
    assert result["attempts"] == 3
    assert calls == 3


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


def test_clipboard_actions_classified_read_vs_write():
    from jarvis_gpt.host_bridge import BRIDGE_ACTIONS, BRIDGE_READ_ONLY_ACTIONS

    assert "clipboard.read" in BRIDGE_ACTIONS
    assert "clipboard.write" in BRIDGE_ACTIONS
    # clipboard.read is read-only (auto-retried); clipboard.write is a mutation and
    # must never be retried, so it is excluded from the read-only set.
    assert "clipboard.read" in BRIDGE_READ_ONLY_ACTIONS
    assert "clipboard.write" not in BRIDGE_READ_ONLY_ACTIONS
