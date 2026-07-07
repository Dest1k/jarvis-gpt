from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

from .config import JarvisSettings


class HostBridgeStatus:
    def __init__(self, settings: JarvisSettings) -> None:
        self.settings = settings

    def snapshot(self) -> dict[str, Any]:
        token_paths = [
            Path.home() / ".jarvis" / "bridge.token",
            self.settings.home / ".jarvis" / "bridge.token",
            self.settings.home / "bridge.token",
        ]
        token = next((path for path in token_paths if path.exists()), None)
        return {
            "name": "windows_rpc_bridge",
            "host": "127.0.0.1",
            "port": 8765,
            "port_open": _port_open("127.0.0.1", 8765, timeout=0.5),
            "token_path": str(token) if token else None,
            "token_available": token is not None,
            "script_path": str(self.settings.home / "windows_rpc_bridge.py"),
            "script_available": (self.settings.home / "windows_rpc_bridge.py").exists(),
            "start_command": f"python {self.settings.home / 'windows_rpc_bridge.py'} --port 8765",
        }


def _port_open(host: str, port: int, *, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
