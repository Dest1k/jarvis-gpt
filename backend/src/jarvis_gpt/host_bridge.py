from __future__ import annotations

import asyncio
import hashlib
import math
import os
import socket
from pathlib import Path
from typing import Any

import httpx

from .config import JarvisSettings

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765
BRIDGE_CONTRACT = "action.v1"
BRIDGE_POLICY_REVISION = "native-app-v3"
BRIDGE_ACTIONS = (
    "app.open_and_type",
    "browser.open_guarded",
    "capabilities",
    "chrome.attest_guarded",
    "chrome.launch_guarded",
    "console.show_processes",
    "hardware.gpu",
    "keyboard.send",
    "process.start",
    "process.top",
    "screen.capture",
    "window.focus",
    "window.list",
    "wmi.query",
)
BRIDGE_READ_ONLY_ACTIONS = frozenset(
    {
        "capabilities",
        "hardware.gpu",
        "process.top",
        "window.list",
        "wmi.query",
    }
)
_BRIDGE_READ_MAX_ATTEMPTS = 3
_BRIDGE_RETRY_BASE_DELAY_SEC = 0.1
_BRIDGE_RETRY_MAX_DELAY_SEC = 1.0


class HostBridgeStatus:
    def __init__(self, settings: JarvisSettings) -> None:
        self.settings = settings

    def snapshot(self) -> dict[str, Any]:
        token = bridge_token_path(self.settings)
        health = _bridge_health()
        token_value = _read_token_file(token)
        capabilities = _bridge_capabilities(token_value)
        deployed_script_path = self.settings.home / "windows_rpc_bridge.py"
        packaged_script_path = (
            Path(__file__).resolve().parent / "bundled" / "windows_rpc_bridge.py"
        )
        repository_script_path = (
            Path(__file__).resolve().parents[3] / "scripts" / "windows_rpc_bridge.py"
        )
        bundled_script_path = (
            packaged_script_path if packaged_script_path.exists() else repository_script_path
        )
        script_path = bundled_script_path if bundled_script_path.exists() else deployed_script_path
        return {
            "name": "windows_rpc_bridge",
            "host": BRIDGE_HOST,
            "port": BRIDGE_PORT,
            "port_open": _port_open(BRIDGE_HOST, BRIDGE_PORT, timeout=0.5),
            "health": health,
            "capabilities_probe": capabilities,
            "action_v1_ready": bool(
                health.get("ok")
                and health.get("contract") == BRIDGE_CONTRACT
                and capabilities.get("ok")
                and capabilities.get("contract") == BRIDGE_CONTRACT
                and capabilities.get("raw_command_execution") is False
                and capabilities.get("policy_revision") == BRIDGE_POLICY_REVISION
                and _browser_network_guard_ready(capabilities)
                and capabilities.get("app_paths_sha256")
                == _expected_app_paths_sha256()
            ),
            "token_path": str(token) if token else None,
            "token_available": token is not None,
            "script_path": str(script_path),
            "deployed_script_path": str(deployed_script_path),
            "bundled_script_path": str(bundled_script_path),
            "script_available": script_path.exists(),
            "start_command": f"python {script_path} --port {BRIDGE_PORT}",
            "contract": BRIDGE_CONTRACT,
            "policy_revision": BRIDGE_POLICY_REVISION,
            "action_endpoint": "/action",
            "raw_execute_available": False,
            "browser_network_guard": capabilities.get("browser_network_guard"),
            "native_capabilities": list(BRIDGE_ACTIONS),
        }


class HostBridgeClient:
    def __init__(self, settings: JarvisSettings) -> None:
        self.settings = settings

    async def action(
        self,
        *,
        action: str,
        payload: dict[str, Any] | None = None,
        timeout_sec: int = 30,
    ) -> dict[str, Any]:
        if action not in BRIDGE_ACTIONS:
            return {
                "ok": False,
                "summary": (
                    f"Unsupported host bridge action. Allowed: {', '.join(BRIDGE_ACTIONS)}."
                ),
                "contract": BRIDGE_CONTRACT,
                "outcome_known": True,
                "retryable": False,
                "attempts": 0,
            }
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "summary": "Host bridge payload must be an object.",
                "contract": BRIDGE_CONTRACT,
                "outcome_known": True,
                "retryable": False,
                "attempts": 0,
            }
        if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, int):
            return {
                "ok": False,
                "summary": "Host bridge timeout_sec must be an integer.",
                "contract": BRIDGE_CONTRACT,
                "outcome_known": True,
                "retryable": False,
                "attempts": 0,
            }

        token = read_bridge_token(self.settings)
        if token is None:
            return {
                "ok": False,
                "summary": "Host bridge token is missing.",
                "contract": BRIDGE_CONTRACT,
                "status": HostBridgeStatus(self.settings).snapshot(),
                "outcome_known": True,
                "retryable": False,
                "attempts": 0,
            }

        timeout_sec = max(1, min(120, timeout_sec))
        request_payload: dict[str, Any] = {
            "action": action,
            "payload": payload,
            "timeout_sec": timeout_sec,
        }
        read_only = action in BRIDGE_READ_ONLY_ACTIONS
        max_attempts = _BRIDGE_READ_MAX_ATTEMPTS if read_only else 1
        attempts = 0
        response: httpx.Response | None = None
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec + 5.0),
            trust_env=False,
        ) as client:
            while attempts < max_attempts:
                attempts += 1
                try:
                    response = await client.post(
                        f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/action",
                        headers={"Authorization": f"Bearer {token}"},
                        json=request_payload,
                    )
                except httpx.HTTPError as exc:
                    transport_failure = isinstance(exc, httpx.TransportError)
                    if read_only and transport_failure and attempts < max_attempts:
                        await asyncio.sleep(_bridge_retry_delay(attempts))
                        continue
                    return {
                        "ok": False,
                        "summary": f"Host bridge request failed: {exc.__class__.__name__}",
                        "error": str(exc),
                        "contract": BRIDGE_CONTRACT,
                        "status": HostBridgeStatus(self.settings).snapshot(),
                        "outcome_known": False,
                        "retryable": bool(read_only and transport_failure),
                        "attempts": attempts,
                    }
                if 500 <= response.status_code <= 599 and read_only and attempts < max_attempts:
                    await asyncio.sleep(_bridge_retry_delay(attempts, response=response))
                    continue
                break

        assert response is not None

        try:
            decoded = response.json()
        except ValueError:
            decoded = None
        response_has_outcome = isinstance(decoded, dict) and isinstance(
            decoded.get("ok"), bool
        )
        response_ok = response_has_outcome and decoded.get("ok") is True
        data = decoded if isinstance(decoded, dict) else {"raw": response.text or decoded}
        stale_contract = response.status_code in {404, 410}
        summary = str(data.get("summary") or f"Host bridge returned {response.status_code}.")
        if stale_contract:
            summary = (
                "Running host bridge does not support action.v1; restart Jarvis so the "
                "updated structured bridge can be loaded."
            )
        uncertain_response = (
            response.status_code == 408
            or 500 <= response.status_code <= 599
            or response.is_success
            and not response_has_outcome
        )
        return {
            "ok": response.is_success and response_ok,
            "summary": summary,
            "status_code": response.status_code,
            "contract": BRIDGE_CONTRACT,
            "data": data,
            "outcome_known": not uncertain_response,
            "retryable": bool(read_only and 500 <= response.status_code <= 599),
            "attempts": attempts,
        }


def bridge_token_path(settings: JarvisSettings) -> Path | None:
    return next((path for path in bridge_token_paths(settings) if path.exists()), None)


def bridge_token_paths(settings: JarvisSettings) -> list[Path]:
    return [
        settings.home / ".jarvis" / "bridge.token",
        settings.home / "bridge.token",
        Path.home() / ".jarvis" / "bridge.token",
    ]


def read_bridge_token(settings: JarvisSettings) -> str | None:
    path = bridge_token_path(settings)
    return _read_token_file(path)


def _read_token_file(path: Path | None) -> str | None:
    if path is None or path.is_symlink() or not path.is_file():
        return None
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def _port_open(host: str, port: int, *, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _bridge_health() -> dict[str, Any]:
    try:
        with httpx.Client(timeout=1.0, trust_env=False) as client:
            response = client.get(f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/health")
        payload = response.json()
        if not isinstance(payload, dict):
            payload = {}
        return {
            "ok": response.is_success and bool(payload.get("ok")),
            "status_code": response.status_code,
            "contract": str(payload.get("contract") or ""),
            "actions": payload.get("actions") if isinstance(payload.get("actions"), list) else [],
        }
    except (httpx.HTTPError, ValueError):
        return {"ok": False, "status_code": None, "contract": "", "actions": []}


def _bridge_capabilities(token: str | None) -> dict[str, Any]:
    if not token:
        return {
            "ok": False,
            "status_code": None,
            "contract": "",
            "actions": [],
            "raw_command_execution": None,
            "policy_revision": "",
            "process_policy": {},
            "browser_network_guard": {},
            "app_paths_sha256": "",
        }
    try:
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            response = client.post(
                f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/action",
                headers={"Authorization": f"Bearer {token}"},
                json={"action": "capabilities", "payload": {}, "timeout_sec": 2},
            )
        payload = response.json()
        if not isinstance(payload, dict):
            payload = {}
        raw_execution = payload.get("raw_command_execution")
        return {
            "ok": response.is_success and bool(payload.get("ok")),
            "status_code": response.status_code,
            "contract": str(payload.get("contract") or ""),
            "actions": payload.get("actions")
            if isinstance(payload.get("actions"), list)
            else [],
            "raw_command_execution": raw_execution
            if isinstance(raw_execution, bool)
            else None,
            "policy_revision": str(payload.get("policy_revision") or ""),
            "process_policy": payload.get("process_policy")
            if isinstance(payload.get("process_policy"), dict)
            else {},
            "browser_network_guard": payload.get("browser_network_guard")
            if isinstance(payload.get("browser_network_guard"), dict)
            else {},
            "app_paths_sha256": str(payload.get("app_paths_sha256") or ""),
        }
    except (httpx.HTTPError, ValueError):
        return {
            "ok": False,
            "status_code": None,
            "contract": "",
            "actions": [],
            "raw_command_execution": None,
            "policy_revision": "",
            "process_policy": {},
            "browser_network_guard": {},
            "app_paths_sha256": "",
        }


def _browser_network_guard_ready(capabilities: dict[str, Any]) -> bool:
    guard = capabilities.get("browser_network_guard")
    return bool(
        isinstance(guard, dict)
        and guard.get("version") == "public-proxy-v1"
        and guard.get("fail_closed") is True
        and guard.get("private_networks") == "blocked"
        and set(guard.get("required_actions") or ())
        == {
            "browser.open_guarded",
            "chrome.attest_guarded",
            "chrome.launch_guarded",
        }
    )


def _expected_app_paths_sha256() -> str:
    raw = os.environ.get("JARVIS_BRIDGE_APP_PATHS_JSON", "")
    return hashlib.sha256(raw.encode("utf-8", errors="strict")).hexdigest()


def _bridge_retry_delay(attempt: int, *, response: httpx.Response | None = None) -> float:
    exponential = _BRIDGE_RETRY_BASE_DELAY_SEC * (2 ** max(0, attempt - 1))
    retry_after = 0.0
    if response is not None:
        raw = response.headers.get("Retry-After", "").strip()
        try:
            parsed = float(raw)
        except ValueError:
            parsed = 0.0
        if math.isfinite(parsed):
            retry_after = max(0.0, parsed)
    return min(_BRIDGE_RETRY_MAX_DELAY_SEC, max(exponential, retry_after))
