from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from .config import JarvisSettings
from .model_catalog import ModelCatalog
from .storage import JarvisStorage

DISPATCHER_SERVICE = "dispatcher"
DISPATCHER_CONTAINER = "jarvis-gpt-dispatcher"


class DispatcherManager:
    def __init__(
        self,
        settings: JarvisSettings,
        repo_root: Path | None = None,
        storage: JarvisStorage | None = None,
    ) -> None:
        self.settings = settings
        self.repo_root = repo_root or Path.cwd()
        self.storage = storage

    def status(self) -> dict[str, Any]:
        catalog = ModelCatalog(self.settings, self.storage).response()
        docker = shutil.which("docker")
        container = self._container_status(docker)
        desired_env = self.compose_env()
        desired_runtime = _runtime_from_env(desired_env)
        runtime = _runtime_from_container(container)
        active_model = _model_for_runtime(catalog, runtime) or catalog["active_model"]
        return {
            "service": DISPATCHER_SERVICE,
            "container": DISPATCHER_CONTAINER,
            "docker_available": docker is not None,
            "docker_path": docker,
            "port": 8001,
            "port_open": _port_open("127.0.0.1", 8001, timeout=0.5),
            "base_url": self.settings.llm_base_url,
            "model": self.settings.llm_model,
            "active_model": active_model,
            "desired_model": catalog["active_model"],
            "runtime": runtime,
            "desired_runtime": desired_runtime,
            "compose": self.compose_command("up"),
            "container_status": container,
            "env": desired_env,
        }

    def compose_env(self) -> dict[str, str]:
        dispatcher = ModelCatalog(self.settings, self.storage).dispatcher_config()
        env = {
            "JARVIS_HOST_HOME": str(self.settings.home),
            "JARVIS_MODEL_ROOT": str(self.settings.model_root),
            "JARVIS_VLLM_IMAGE": os.environ.get("JARVIS_VLLM_IMAGE", "vllm/vllm-openai:nightly"),
            "VLLM_USE_V2_MODEL_RUNNER": os.environ.get("VLLM_USE_V2_MODEL_RUNNER", "0"),
            "VLLM_WEIGHT_OFFLOADING_DISABLE_UVA": os.environ.get(
                "VLLM_WEIGHT_OFFLOADING_DISABLE_UVA",
                "1",
            ),
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
        command = self._container_command(docker)
        return {
            "ok": True,
            "exists": True,
            "name": parts[0] if len(parts) > 0 else DISPATCHER_CONTAINER,
            "status": parts[1] if len(parts) > 1 else "",
            "ports": parts[2] if len(parts) > 2 else "",
            "command": command,
        }

    def _container_command(self, docker: str) -> list[str]:
        result = subprocess.run(
            [
                docker,
                "inspect",
                DISPATCHER_CONTAINER,
                "--format",
                "{{json .Config.Cmd}}",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode != 0:
            return []
        try:
            value = json.loads(result.stdout.strip())
        except json.JSONDecodeError:
            return []
        return [str(item) for item in value] if isinstance(value, list) else []


def _port_open(host: str, port: int, *, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _runtime_from_container(container: dict[str, Any] | None) -> dict[str, Any] | None:
    if not container or not container.get("exists"):
        return None
    command = container.get("command")
    if not isinstance(command, list):
        return None
    runtime = _runtime_from_command([str(item) for item in command])
    return runtime if runtime.get("model_path") else None


def _runtime_from_env(env: dict[str, str]) -> dict[str, Any]:
    return {
        "source": "desired-env",
        "model_path": env.get("JARVIS_QWEN_MODEL_PATH", ""),
        "model_id": _model_id(env.get("JARVIS_QWEN_MODEL_PATH", "")),
        "served_model_name": env.get("JARVIS_QWEN_MODEL_NAME", ""),
        "enforce_eager": bool(env.get("JARVIS_QWEN_ENFORCE_EAGER", "").strip()),
        "max_model_len": _int_or_none(env.get("JARVIS_QWEN_MAX_LEN")),
        "gpu_memory_utilization": _float_or_none(env.get("JARVIS_QWEN_GPU_UTIL")),
        "kv_cache_dtype": env.get("JARVIS_QWEN_KV_DTYPE", ""),
        "max_num_seqs": _int_or_none(env.get("JARVIS_QWEN_MAX_NUM_SEQS")),
        "cpu_offload_gb": _arg_value_from_string(
            env.get("JARVIS_QWEN_CPU_OFFLOAD_ARGS"),
            "cpu-offload-gb",
        ),
        "swap_space_gb": _arg_value_from_string(
            env.get("JARVIS_QWEN_SWAP_SPACE_ARGS"),
            "swap-space",
        ),
    }


def _runtime_from_command(command: list[str]) -> dict[str, Any]:
    if len(command) == 1 and " --" in command[0]:
        command = [item for item in command[0].split() if item]
    flags = _parse_flags(command)
    model_path = str(flags.get("model") or "")
    return {
        "source": "container-command",
        "model_path": model_path,
        "model_id": _model_id(model_path),
        "served_model_name": str(flags.get("served-model-name") or ""),
        "enforce_eager": "enforce-eager" in flags,
        "max_model_len": _int_or_none(flags.get("max-model-len")),
        "gpu_memory_utilization": _float_or_none(flags.get("gpu-memory-utilization")),
        "kv_cache_dtype": str(flags.get("kv-cache-dtype") or ""),
        "max_num_seqs": _int_or_none(flags.get("max-num-seqs")),
        "cpu_offload_gb": _int_or_none(flags.get("cpu-offload-gb")),
        "swap_space_gb": _int_or_none(flags.get("swap-space")),
    }


def _parse_flags(command: list[str]) -> dict[str, str | bool]:
    flags: dict[str, str | bool] = {}
    index = 0
    while index < len(command):
        token = command[index]
        if not token.startswith("--"):
            index += 1
            continue
        key = token[2:]
        if "=" in key:
            key, value = key.split("=", 1)
            flags[key] = value
            index += 1
            continue
        if index + 1 < len(command) and not command[index + 1].startswith("--"):
            flags[key] = command[index + 1]
            index += 2
            continue
        flags[key] = True
        index += 1
    return flags


def _model_for_runtime(
    catalog: dict[str, Any],
    runtime: dict[str, Any] | None,
) -> dict[str, Any] | None:
    model_id = runtime.get("model_id") if runtime else None
    if not model_id:
        return None
    for model in catalog.get("models", []):
        if isinstance(model, dict) and model.get("id") == model_id:
            return model
    return None


def _model_id(path: str | None) -> str:
    if not path:
        return ""
    normalized = path.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1]


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _float_or_none(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _arg_value_from_string(raw: str | None, key: str) -> int | None:
    if not raw:
        return None
    flags = _parse_flags([item for item in raw.split() if item])
    return _int_or_none(flags.get(key))
