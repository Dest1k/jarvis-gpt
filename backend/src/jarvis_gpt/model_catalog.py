from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import JarvisSettings

MODEL_METADATA_FILES = (
    "config.json",
    "generation_config.json",
    "hf_quant_config.json",
    "model.safetensors.index.json",
    "tokenizer_config.json",
)


class ModelCatalog:
    def __init__(self, settings: JarvisSettings) -> None:
        self.settings = settings

    def response(self) -> dict[str, Any]:
        models = self.list_models()
        active = next(
            (item for item in models if item["id"] == self.settings.profile.model_dir_name),
            None,
        )
        return {
            "root": str(self.settings.model_root),
            "active_profile": self.settings.profile.name,
            "active_model": active or self.describe_path(self.settings.model_dir),
            "models": models,
            "dispatcher": self.dispatcher_config(),
        }

    def list_models(self) -> list[dict[str, Any]]:
        root = self.settings.model_root
        if not root.exists():
            return []
        items = []
        for path in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if path.is_dir():
                items.append(self.describe_path(path))
        return items

    def describe_path(self, path: Path) -> dict[str, Any]:
        files = list(path.iterdir()) if path.exists() and path.is_dir() else []
        shards = [item for item in files if item.suffix == ".safetensors"]
        size_bytes = sum(item.stat().st_size for item in files if item.is_file())
        config = _read_json(path / "config.json")
        quant = _read_json(path / "hf_quant_config.json")
        generation = _read_json(path / "generation_config.json")
        metadata = {
            name: (path / name).exists()
            for name in MODEL_METADATA_FILES
        }
        return {
            "id": path.name,
            "path": str(path),
            "exists": path.exists(),
            "active": path.resolve(strict=False) == self.settings.model_dir.resolve(strict=False),
            "size_bytes": size_bytes,
            "shard_count": len(shards),
            "modified_at": _modified_at(path),
            "model_type": config.get("model_type"),
            "architectures": config.get("architectures") or [],
            "dtype": config.get("dtype") or config.get("torch_dtype"),
            "quantization": _quantization_name(quant),
            "metadata": metadata,
            "generation": {
                "temperature": generation.get("temperature"),
                "top_p": generation.get("top_p"),
                "max_length": generation.get("max_length"),
            },
        }

    def dispatcher_config(self) -> dict[str, Any]:
        profile = self.settings.profile
        return {
            "base_url": self.settings.llm_base_url,
            "served_model_name": self.settings.llm_model,
            "model_path": str(self.settings.model_dir),
            "docker_model_path": f"/models/{profile.model_dir_name}",
            "engine": "vllm-openai",
            "env": {
                "JARVIS_QWEN_MODEL_NAME": self.settings.llm_model,
                "JARVIS_QWEN_MODEL_PATH": f"/models/{profile.model_dir_name}",
                "JARVIS_QWEN_DTYPE": "auto",
                "JARVIS_QWEN_GPU_UTIL": str(profile.gpu_memory_utilization),
                "JARVIS_QWEN_MAX_LEN": str(profile.max_model_len),
                "JARVIS_QWEN_KV_DTYPE": profile.kv_cache_dtype,
                "JARVIS_QWEN_TOKENIZER_MODE": "slow",
                "JARVIS_QWEN_SAFETENSORS_LOAD_STRATEGY": "prefetch",
                "JARVIS_QWEN_MAX_NUM_SEQS": str(profile.max_num_seqs),
                "JARVIS_QWEN_ENFORCE_EAGER": "--enforce-eager" if profile.eager_mode else "",
                "JARVIS_ENABLE_UITARS": "0",
            },
        }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _quantization_name(data: dict[str, Any]) -> str | None:
    quant = data.get("quantization")
    if isinstance(quant, dict):
        return quant.get("quant_algo") or quant.get("quant_method")
    return None


def _modified_at(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds")
    except OSError:
        return None
