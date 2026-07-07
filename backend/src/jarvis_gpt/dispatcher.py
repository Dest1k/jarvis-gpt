from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from .config import JarvisSettings
from .model_catalog import ModelCatalog

DISPATCHER_SERVICE = "dispatcher"
DISPATCHER_CONTAINER = "jarvis-gpt-dispatcher"


class DispatcherManager:
    def __init__(self, settings: JarvisSettings, repo_root: Path | None = None) -> None:
        self.settings = settings
        self.repo_root = repo_root or Path.cwd()

    def status(self) -> dict[str, Any]:
        catalog = ModelCatalog(self.settings).response()
        docker = shutil.which("docker")
        container = self._container_status(docker)
        return {
            "service": DISPATCHER_SERVICE,
            "container": DISPATCHER_CONTAINER,
            "docker_available": docker is not None,
            "docker_path": docker,
            "port": 8001,
            "port_open": _port_open("127.0.0.1", 8001, timeout=0.5),
            "base_url": self.settings.llm_base_url,
            "model": self.settings.llm_model,
            "active_model": catalog["active_model"],
            "compose": self.compose_command("up"),
            "container_status": container,
            "env": self.compose_env(),
        }

    def compose_env(self) -> dict[str, str]:
        dispatcher = ModelCatalog(self.settings).dispatcher_config()
        env = {
            "JARVIS_HOST_HOME": str(self.settings.home),
            "JARVIS_MODEL_ROOT": str(self.settings.model_root),
            "JARVIS_VLLM_IMAGE": os.environ.get("JARVIS_VLLM_IMAGE", "vllm/vllm-openai:nightly"),
            "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES", "0"),
            "CUDA_DEVICE_ORDER": os.environ.get("CUDA_DEVICE_ORDER", "PCI_BUS_ID"),
            "CUDA_DISABLE_P2P": os.environ.get("CUDA_DISABLE_P2P", "1"),
            "NCCL_P2P_DISABLE": os.environ.get("NCCL_P2P_DISABLE", "1"),
            "HF_TOKEN": os.environ.get("HF_TOKEN", ""),
        }
        env.update({key: str(value) for key, value in dispatcher["env"].items()})
        return env

    def compose_command(self, action: str = "up") -> list[str]:
        base = ["docker", "compose", "--profile", "llm"]
        if action == "up":
            return [*base, "up", "-d", DISPATCHER_SERVICE]
        if action == "down":
            return [*base, "stop", DISPATCHER_SERVICE]
        if action == "logs":
            return [*base, "logs", "--tail", "80", DISPATCHER_SERVICE]
        return [*base, "ps", DISPATCHER_SERVICE]

    def run_compose(self, action: str) -> dict[str, Any]:
        docker = shutil.which("docker")
        if docker is None:
            return {"ok": False, "summary": "Docker is not available in PATH.", "returncode": None}
        command = self.compose_command(action)
        env = {**os.environ, **self.compose_env()}
        result = subprocess.run(
            command,
            cwd=self.repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "summary": f"Dispatcher compose {action} exited with {result.returncode}.",
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "command": command,
        }

    def _container_status(self, docker: str | None) -> dict[str, Any] | None:
        if docker is None:
            return None
        result = subprocess.run(
            [
                docker,
                "ps",
                "-a",
                "--filter",
                f"name={DISPATCHER_CONTAINER}",
                "--format",
                "{{.Names}}\t{{.Status}}\t{{.Ports}}",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()}
        line = result.stdout.strip()
        if not line:
            return {"ok": True, "exists": False}
        parts = line.split("\t")
        return {
            "ok": True,
            "exists": True,
            "name": parts[0] if len(parts) > 0 else DISPATCHER_CONTAINER,
            "status": parts[1] if len(parts) > 1 else "",
            "ports": parts[2] if len(parts) > 2 else "",
        }


def _port_open(host: str, port: int, *, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
