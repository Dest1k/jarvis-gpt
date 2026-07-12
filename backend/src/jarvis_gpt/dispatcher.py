from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import JarvisSettings
from .model_catalog import ModelCatalog
from .storage import JarvisStorage

DISPATCHER_SERVICE = "dispatcher"
DISPATCHER_CONTAINER = "jarvis-gpt-dispatcher"
RUNTIME_COMPATIBILITY_FIELDS = (
    "model_path",
    "model_id",
    "served_model_name",
    "dtype",
    "enforce_eager",
    "max_model_len",
    "gpu_memory_utilization",
    "kv_cache_dtype",
    "max_num_seqs",
    "cpu_offload_gb",
    "kv_offloading_gb",
    "kv_offloading_backend",
    "tokenizer_mode",
    "safetensors_load_strategy",
    "prefix_caching",
    "language_model_only",
    "skip_mm_profiling",
    "mm_processor_cache_gb",
    "max_num_batched_tokens",
    "host",
    "port",
)


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
        runtime_mismatches = _runtime_mismatches(runtime, desired_runtime)
        actual_image = str(container.get("image") or "") if isinstance(container, dict) else ""
        desired_image = desired_env["JARVIS_VLLM_IMAGE"]
        if container and container.get("exists") and actual_image != desired_image:
            runtime_mismatches["image"] = {
                "actual": actual_image,
                "desired": desired_image,
            }
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
            "actual_image": actual_image,
            "desired_image": desired_image,
            "runtime_matches_desired": runtime is not None and not runtime_mismatches,
            "runtime_mismatches": runtime_mismatches,
            "compose": self.compose_command("up"),
            "container_status": container,
            "env": _public_env(desired_env),
        }

    def compose_env(self) -> dict[str, str]:
        dispatcher = ModelCatalog(self.settings, self.storage).dispatcher_config()
        env = {
            "JARVIS_HOST_HOME": str(self.settings.home),
            "JARVIS_MODEL_ROOT": str(self.settings.model_root),
            "JARVIS_VLLM_IMAGE": os.environ.get(
                "JARVIS_VLLM_IMAGE",
                "vllm/vllm-openai:v0.23.0",
            ),
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

    def run_compose_verified(
        self,
        action: str,
        *,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        replacement: dict[str, Any] | None = None
        if action == "up":
            replacement = self._replace_mismatched_container()
            if replacement.get("required") and not replacement.get("ok"):
                return {
                    "ok": False,
                    "summary": "Existing dispatcher container could not be replaced safely.",
                    "returncode": replacement.get("returncode"),
                    "stdout": replacement.get("stdout", ""),
                    "stderr": replacement.get("stderr", ""),
                    "command": replacement.get("command", []),
                    "replacement": replacement,
                    "verification": {"ok": False, "skipped": True},
                }
            if replacement.get("reused"):
                verification = self.verify_state(
                    running=True,
                    timeout_seconds=timeout_seconds,
                )
                return {
                    "ok": verification["ok"],
                    "summary": (
                        "Dispatcher already runs the exact desired runtime."
                        if verification["ok"]
                        else "Reusable dispatcher failed independent state verification."
                    ),
                    "returncode": 0 if verification["ok"] else None,
                    "stdout": "",
                    "stderr": "",
                    "command": [],
                    "replacement": replacement,
                    "verification": verification,
                }
        result = self.run_compose(action)
        if not result.get("ok"):
            return {
                **result,
                "replacement": replacement,
                "verification": {"ok": False, "skipped": True},
            }
        expected_running = action == "up"
        verification = self.verify_state(
            running=expected_running,
            timeout_seconds=timeout_seconds,
        )
        return {
            **result,
            "ok": bool(result.get("ok") and verification["ok"]),
            "replacement": replacement,
            "summary": (
                f"Dispatcher compose {action} completed and independent state checks passed."
                if verification["ok"]
                else f"Dispatcher compose {action} completed but state verification failed."
            ),
            "verification": verification,
        }

    def _replace_mismatched_container(self) -> dict[str, Any]:
        snapshot = self.status()
        container = snapshot.get("container_status")
        if not isinstance(container, dict) or container.get("exists") is not True:
            return {
                "required": False,
                "ok": True,
                "removed": False,
                "reused": False,
            }
        container_running = str(container.get("status") or "").casefold().startswith("up")
        if container_running and snapshot.get("runtime_matches_desired") is True:
            return {
                "required": False,
                "ok": True,
                "removed": False,
                "reused": True,
            }

        docker = shutil.which("docker")
        if docker is None:
            return {
                "required": True,
                "ok": False,
                "removed": False,
                "reused": False,
                "returncode": None,
                "stderr": "Docker is not available in PATH.",
                "mismatches": snapshot.get("runtime_mismatches", {}),
            }
        command = [docker, "rm", "-f", DISPATCHER_CONTAINER]
        result = subprocess.run(
            command,
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return {
            "required": True,
            "ok": result.returncode == 0,
            "removed": result.returncode == 0,
            "reused": False,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "command": command,
            "mismatches": snapshot.get("runtime_mismatches", {}),
        }

    def verify_state(
        self,
        *,
        running: bool,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.1, min(float(timeout_seconds), 120.0))
        snapshot: dict[str, Any] = {}
        while True:
            snapshot = self.status()
            container = snapshot.get("container_status")
            container_status = (
                str(container.get("status") or "") if isinstance(container, dict) else ""
            )
            container_known = bool(
                isinstance(container, dict)
                and container.get("ok") is True
                and isinstance(container.get("exists"), bool)
            )
            container_running = bool(
                container_known
                and container.get("exists")
                and container_status.casefold().startswith("up")
            )
            port_open = bool(snapshot.get("port_open"))
            runtime_matches_desired = snapshot.get("runtime_matches_desired") is True
            verified = (
                container_known and container_running and runtime_matches_desired
                if running
                else container_known and not container_running and not port_open
            )
            if verified or time.monotonic() >= deadline:
                return {
                    "ok": verified,
                    "expected_running": running,
                    "container_known": container_known,
                    "container_running": container_running,
                    "port_open": port_open,
                    "runtime_matches_desired": runtime_matches_desired,
                    "runtime_mismatches": snapshot.get("runtime_mismatches", {}),
                    "port": int(snapshot.get("port") or 8001),
                    "container_status": container,
                }
            time.sleep(0.25)

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
                "{{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Image}}",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip()}
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        exact = next(
            (
                line
                for line in lines
                if line.split("\t", 1)[0].strip() == DISPATCHER_CONTAINER
            ),
            None,
        )
        if exact is None:
            return {"ok": True, "exists": False}
        parts = exact.split("\t")
        command = self._container_command(docker)
        return {
            "ok": True,
            "exists": True,
            "name": parts[0] if len(parts) > 0 else DISPATCHER_CONTAINER,
            "status": parts[1] if len(parts) > 1 else "",
            "ports": parts[2] if len(parts) > 2 else "",
            "image": parts[3] if len(parts) > 3 else "",
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


def _public_env(env: dict[str, str]) -> dict[str, str]:
    """Return dispatcher configuration without exposing credential values."""
    secret_keys = {"HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"}
    return {
        key: "[configured]" if key in secret_keys and value else value
        for key, value in env.items()
    }


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
        "dtype": env.get("JARVIS_QWEN_DTYPE", ""),
        "enforce_eager": bool(env.get("JARVIS_QWEN_ENFORCE_EAGER", "").strip()),
        "max_model_len": _int_or_none(env.get("JARVIS_QWEN_MAX_LEN")),
        "gpu_memory_utilization": _float_or_none(env.get("JARVIS_QWEN_GPU_UTIL")),
        "kv_cache_dtype": env.get("JARVIS_QWEN_KV_DTYPE", ""),
        "max_num_seqs": _int_or_none(env.get("JARVIS_QWEN_MAX_NUM_SEQS")),
        "cpu_offload_gb": _float_arg_value_from_string(
            env.get("JARVIS_QWEN_CPU_OFFLOAD_ARGS"),
            "cpu-offload-gb",
        ),
        "kv_offloading_gb": _int_arg_value_from_string(
            env.get("JARVIS_QWEN_KV_OFFLOAD_ARGS"),
            "kv-offloading-size",
        ),
        "kv_offloading_backend": _flag_value_from_string(
            env.get("JARVIS_QWEN_KV_OFFLOAD_ARGS"),
            "kv-offloading-backend",
        ),
        "tokenizer_mode": env.get("JARVIS_QWEN_TOKENIZER_MODE", ""),
        "safetensors_load_strategy": env.get(
            "JARVIS_QWEN_SAFETENSORS_LOAD_STRATEGY", ""
        ),
        "prefix_caching": True,
        "language_model_only": _bool_flag_from_string(
            env.get("JARVIS_QWEN_EXTRA_ARGS"), "language-model-only"
        ),
        "skip_mm_profiling": _bool_flag_from_string(
            env.get("JARVIS_QWEN_EXTRA_ARGS"), "skip-mm-profiling"
        ),
        "mm_processor_cache_gb": _float_arg_value_from_string(
            env.get("JARVIS_QWEN_EXTRA_ARGS"), "mm-processor-cache-gb"
        ),
        "max_num_batched_tokens": _int_arg_value_from_string(
            env.get("JARVIS_QWEN_EXTRA_ARGS"), "max-num-batched-tokens"
        ),
        "host": "0.0.0.0",
        "port": 8001,
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
        "dtype": str(flags.get("dtype") or ""),
        "enforce_eager": "enforce-eager" in flags,
        "max_model_len": _int_or_none(flags.get("max-model-len")),
        "gpu_memory_utilization": _float_or_none(flags.get("gpu-memory-utilization")),
        "kv_cache_dtype": str(flags.get("kv-cache-dtype") or ""),
        "max_num_seqs": _int_or_none(flags.get("max-num-seqs")),
        "cpu_offload_gb": _float_or_none(flags.get("cpu-offload-gb")),
        "kv_offloading_gb": _int_or_none(flags.get("kv-offloading-size")),
        "kv_offloading_backend": (
            str(flags.get("kv-offloading-backend") or "") or None
        ),
        "tokenizer_mode": str(flags.get("tokenizer-mode") or ""),
        "safetensors_load_strategy": str(
            flags.get("safetensors-load-strategy") or ""
        ),
        "prefix_caching": "enable-prefix-caching" in flags,
        "language_model_only": "language-model-only" in flags,
        "skip_mm_profiling": "skip-mm-profiling" in flags,
        "mm_processor_cache_gb": _float_or_none(flags.get("mm-processor-cache-gb")),
        "max_num_batched_tokens": _int_or_none(flags.get("max-num-batched-tokens")),
        "host": str(flags.get("host") or ""),
        "port": _int_or_none(flags.get("port")),
    }


def _runtime_mismatches(
    actual: dict[str, Any] | None,
    desired: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    mismatches: dict[str, dict[str, Any]] = {}
    for field in RUNTIME_COMPATIBILITY_FIELDS:
        actual_value = actual.get(field) if actual is not None else None
        desired_value = desired.get(field)
        if field == "model_path":
            actual_value = _normalized_model_path(actual_value)
            desired_value = _normalized_model_path(desired_value)
        if isinstance(actual_value, float) and isinstance(desired_value, float):
            matches = abs(actual_value - desired_value) <= 1e-9
        else:
            matches = actual_value == desired_value
        if not matches:
            mismatches[field] = {"actual": actual_value, "desired": desired_value}
    return mismatches


def _normalized_model_path(value: object) -> str:
    return str(value or "").replace("\\", "/").rstrip("/")


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


def _int_arg_value_from_string(raw: str | None, key: str) -> int | None:
    if not raw:
        return None
    flags = _parse_flags([item for item in raw.split() if item])
    return _int_or_none(flags.get(key))


def _float_arg_value_from_string(raw: str | None, key: str) -> float | None:
    if not raw:
        return None
    flags = _parse_flags([item for item in raw.split() if item])
    return _float_or_none(flags.get(key))


def _bool_flag_from_string(raw: str | None, key: str) -> bool:
    if not raw:
        return False
    flags = _parse_flags([item for item in raw.split() if item])
    return key in flags


def _flag_value_from_string(raw: str | None, key: str) -> str | None:
    if not raw:
        return None
    flags = _parse_flags([item for item in raw.split() if item])
    value = flags.get(key)
    if value is True or value is None:
        return None
    text = str(value).strip()
    return text or None
