from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Any

from .config import JarvisSettings
from .storage import utc_now


class TelemetryCollector:
    def __init__(self, settings: JarvisSettings) -> None:
        self.settings = settings

    def snapshot(self) -> dict[str, Any]:
        return {
            "ts": utc_now(),
            "host": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "cpu_count": os.cpu_count() or 0,
            },
            "memory": _memory_snapshot(),
            "disks": self._disk_snapshot(),
            "gpu": _nvidia_snapshot(),
            "docker": _docker_snapshot(),
            "performance": self.performance_plan(),
        }

    def performance_plan(self) -> dict[str, Any]:
        profile = self.settings.profile
        return {
            "profile": profile.name,
            "model": profile.model_dir_name,
            "max_model_len": profile.max_model_len,
            "gpu_memory_utilization": profile.gpu_memory_utilization,
            "kv_cache_dtype": profile.kv_cache_dtype,
            "max_num_seqs": profile.max_num_seqs,
            "eager_mode": profile.eager_mode,
            "recommended_dispatcher": {
                "port": 8001,
                "image": os.environ.get("JARVIS_VLLM_IMAGE", "vllm/vllm-openai:nightly"),
                "vllm_use_v2_model_runner": os.environ.get("VLLM_USE_V2_MODEL_RUNNER", "0"),
                "vllm_weight_offloading_disable_uva": os.environ.get(
                    "VLLM_WEIGHT_OFFLOADING_DISABLE_UVA",
                    "1",
                ),
                "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES", "0"),
                "tokenizer_mode": "slow",
                "safetensors_load_strategy": "prefetch",
                "model_path": f"/models/{profile.model_dir_name}",
            },
            "resource_policy": {
                "backend": "keep lightweight; no model weights in process",
                "dispatcher": "own GPU/VRAM through vLLM profile",
                "storage": "SQLite WAL with FTS for local single-user latency",
                "frontend": "poll compact snapshots; heavy work remains in backend/dispatcher",
            },
        }

    def _disk_snapshot(self) -> list[dict[str, Any]]:
        paths = [
            self.settings.home,
            self.settings.data_dir,
            self.settings.cache_dir,
            self.settings.model_root,
        ]
        seen: set[str] = set()
        disks = []
        for path in paths:
            key = str(path.resolve(strict=False))
            if key in seen:
                continue
            seen.add(key)
            try:
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            disks.append(
                {
                    "path": str(path),
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "used_ratio": usage.used / usage.total if usage.total else 0,
                }
            )
        return disks


def _memory_snapshot() -> dict[str, Any]:
    if platform.system().lower() == "windows":
        return _windows_memory()
    return _linux_memory()


def _windows_memory() -> dict[str, Any]:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except Exception:  # noqa: BLE001
        return {"total": None, "available": None, "used_ratio": None}
    used = status.ullTotalPhys - status.ullAvailPhys
    return {
        "total": status.ullTotalPhys,
        "available": status.ullAvailPhys,
        "used": used,
        "used_ratio": used / status.ullTotalPhys if status.ullTotalPhys else 0,
    }


def _linux_memory() -> dict[str, Any]:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return {"total": None, "available": None, "used_ratio": None}
    values: dict[str, int] = {}
    for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        parts = raw.strip().split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    used = total - available if total is not None and available is not None else None
    return {
        "total": total,
        "available": available,
        "used": used,
        "used_ratio": used / total if used is not None and total else None,
    }


def _nvidia_snapshot() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,utilization.gpu,temperature.gpu,power.draw",
        "--format=csv,noheader,nounits",
    ]
    result = _run(command, timeout=6)
    if not result["ok"]:
        return {"available": False, "error": result["stderr"] or result["stdout"]}
    gpus = []
    for line in result["stdout"].splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        total = _float(parts[1])
        used = _float(parts[2])
        ratio = used / total if used is not None and total else None
        gpus.append(
            {
                "name": parts[0],
                "memory_total_mb": total,
                "memory_used_mb": used,
                "memory_used_ratio": ratio,
                "utilization_gpu": _float(parts[3]),
                "temperature_c": _float(parts[4]),
                "power_w": _float(parts[5]),
            }
        )
    return {"available": True, "gpus": gpus}


def _docker_snapshot() -> dict[str, Any]:
    result = _run(["docker", "ps", "--format", "{{json .}}"], timeout=8)
    if not result["ok"]:
        return {"available": False, "error": result["stderr"] or result["stdout"]}
    containers = []
    for line in result["stdout"].splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        containers.append(
            {
                "name": item.get("Names"),
                "image": item.get("Image"),
                "status": item.get("Status"),
                "ports": item.get("Ports"),
            }
        )
    return {"available": True, "containers": containers}


def _run(command: list[str], *, timeout: int) -> dict[str, Any]:
    if shutil.which(command[0]) is None:
        return {"ok": False, "stdout": "", "stderr": f"{command[0]} not found"}
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "stdout": "", "stderr": str(exc) or exc.__class__.__name__}
    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "returncode": result.returncode,
    }


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None
