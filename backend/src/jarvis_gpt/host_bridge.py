from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import httpx

from .config import JarvisSettings

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765


class HostBridgeStatus:
    def __init__(self, settings: JarvisSettings) -> None:
        self.settings = settings

    def snapshot(self) -> dict[str, Any]:
        token = bridge_token_path(self.settings)
        deployed_script_path = self.settings.home / "windows_rpc_bridge.py"
        bundled_script_path = (
            Path(__file__).resolve().parents[3] / "scripts" / "windows_rpc_bridge.py"
        )
        script_path = bundled_script_path if bundled_script_path.exists() else deployed_script_path
        return {
            "name": "windows_rpc_bridge",
            "host": BRIDGE_HOST,
            "port": BRIDGE_PORT,
            "port_open": _port_open(BRIDGE_HOST, BRIDGE_PORT, timeout=0.5),
            "token_path": str(token) if token else None,
            "token_available": token is not None,
            "script_path": str(script_path),
            "deployed_script_path": str(deployed_script_path),
            "bundled_script_path": str(bundled_script_path),
            "script_available": script_path.exists(),
            "start_command": f"python {script_path} --port {BRIDGE_PORT}",
            "native_capabilities": [
                "wmi.query",
                "process.start",
                "window.list",
                "window.focus",
                "keyboard.send",
                "app.open_and_type",
            ],
        }


class HostBridgeClient:
    def __init__(self, settings: JarvisSettings) -> None:
        self.settings = settings

    async def execute(
        self,
        *,
        command: str,
        cwd: str | None = None,
        timeout_sec: int = 30,
    ) -> dict[str, Any]:
        command = command.strip()
        if not command:
            return {"ok": False, "summary": "Host bridge command is required."}

        token = read_bridge_token(self.settings)
        if token is None:
            return {
                "ok": False,
                "summary": "Host bridge token is missing.",
                "status": HostBridgeStatus(self.settings).snapshot(),
            }

        timeout_sec = max(1, min(120, int(timeout_sec)))
        payload: dict[str, Any] = {
            "command": command,
            "timeout_sec": timeout_sec,
        }
        if cwd:
            payload["cwd"] = cwd

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_sec + 5.0),
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/execute",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "summary": f"Host bridge request failed: {exc.__class__.__name__}",
                "error": str(exc),
                "status": HostBridgeStatus(self.settings).snapshot(),
            }

        try:
            data = response.json()
        except ValueError:
            data = {"raw": response.text}
        return {
            "ok": response.is_success and bool(data.get("ok", True)),
            "summary": str(data.get("summary") or f"Host bridge returned {response.status_code}."),
            "status_code": response.status_code,
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
    if path is None:
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def _port_open(host: str, port: int, *, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
