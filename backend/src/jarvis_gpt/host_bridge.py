from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path
from typing import Any

import httpx

from .config import JarvisSettings

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765
BRIDGE_CONTRACT = "action.v1"
BRIDGE_POLICY_REVISION = "native-app-v2"
BRIDGE_ACTIONS = (
    "app.open_and_type",
    "capabilities",
    "chrome.launch",
    "console.show_processes",
    "keyboard.send",
    "process.start",
    "process.top",
    "screen.capture",
    "url.open",
    "window.focus",
    "window.list",
    "wmi.query",
)


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
            }
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "summary": "Host bridge payload must be an object.",
                "contract": BRIDGE_CONTRACT,
            }
        if isinstance(timeout_sec, bool) or not isinstance(timeout_sec, int):
            return {
                "ok": False,
                "summary": "Host bridge timeout_sec must be an integer.",
                "contract": BRIDGE_CONTRACT,
            }

        token = read_bridge_token(self.settings)
        if token is None:
            return {
                "ok": False,
                "summary": "Host bridge token is missing.",
                "contract": BRIDGE_CONTRACT,
                "status": HostBridgeStatus(self.settings).snapshot(),
            }

        timeout_sec = max(1, min(120, timeout_sec))
        request_payload: dict[str, Any] = {
            "action": action,
            "payload": payload,
            "timeout_sec": timeout_sec,
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_sec + 5.0),
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/action",
                    headers={"Authorization": f"Bearer {token}"},
                    json=request_payload,
                )
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "summary": f"Host bridge request failed: {exc.__class__.__name__}",
                "error": str(exc),
                "contract": BRIDGE_CONTRACT,
                "status": HostBridgeStatus(self.settings).snapshot(),
            }

        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text}
        if not isinstance(data, dict):
            data = {"raw": data}
        stale_contract = response.status_code in {404, 410}
        summary = str(data.get("summary") or f"Host bridge returned {response.status_code}.")
        if stale_contract:
            summary = (
                "Running host bridge does not support action.v1; restart Jarvis so the "
                "updated structured bridge can be loaded."
            )
        return {
            "ok": response.is_success and bool(data.get("ok", False)),
            "summary": summary,
            "status_code": response.status_code,
            "contract": BRIDGE_CONTRACT,
            "data": data,
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
            "app_paths_sha256": "",
        }


def _expected_app_paths_sha256() -> str:
    raw = os.environ.get("JARVIS_BRIDGE_APP_PATHS_JSON", "")
    return hashlib.sha256(raw.encode("utf-8", errors="strict")).hexdigest()
