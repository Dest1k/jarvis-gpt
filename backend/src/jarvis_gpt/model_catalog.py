from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import PROFILES, JarvisSettings, RuntimeProfile
from .storage import JarvisStorage

MODEL_OVERRIDE_KEY = "models.active_override"
PROFILE_MODEL_DIR_NAMES = frozenset(
    profile.model_dir_name for profile in PROFILES.values()
)

MODEL_METADATA_FILES = (
    "config.json",
    "generation_config.json",
    "hf_quant_config.json",
    "model.safetensors.index.json",
    "tokenizer_config.json",
)


class ModelCatalog:
    def __init__(self, settings: JarvisSettings, storage: JarvisStorage | None = None) -> None:
        self.settings = settings
        self.storage = storage

    def response(self) -> dict[str, Any]:
        models = self.list_models()
        active_name = self.active_model_dir_name()
        active_path = self.settings.model_root / active_name
        active = next((item for item in models if item["id"] == active_name), None)
        return {
            "root": str(self.settings.model_root),
            "active_profile": self.settings.profile.name,
            "active_model": active or self.describe_path(active_path),
            "models": models,
            "dispatcher": self.dispatcher_config(),
        }

    def active_model_dir_name(self) -> str:
        override = ""
        if self.storage is not None:
            override = str(self.storage.get_runtime_value(MODEL_OVERRIDE_KEY, "") or "").strip()
        if (
            override
            and model_allowed_for_profile(self.settings, override)
            and (self.settings.model_root / override).is_dir()
        ):
            return override
        return self.settings.profile.model_dir_name

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
        active_path = self.settings.model_root / self.active_model_dir_name()
        return {
            "id": path.name,
            "path": str(path),
            "exists": path.exists(),
            "active": path.resolve(strict=False) == active_path.resolve(strict=False),
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
        model_name = self.active_model_dir_name()
        return {
            "base_url": self.settings.llm_base_url,
            "served_model_name": self.settings.llm_model,
            "model_path": str(self.settings.model_root / model_name),
            "docker_model_path": f"/models/{model_name}",
            "engine": "vllm-openai",
            "env": {
                "JARVIS_QWEN_MODEL_NAME": self.settings.llm_model,
                "JARVIS_QWEN_MODEL_PATH": f"/models/{model_name}",
                "JARVIS_QWEN_DTYPE": "auto",
                "JARVIS_QWEN_GPU_UTIL": f"{float(profile.gpu_memory_utilization):.2f}",
                "JARVIS_QWEN_MAX_LEN": str(int(profile.max_model_len)),
                "JARVIS_QWEN_KV_DTYPE": profile.kv_cache_dtype,
                "JARVIS_QWEN_TOKENIZER_MODE": profile.tokenizer_mode,
                "JARVIS_QWEN_SAFETENSORS_LOAD_STRATEGY": "prefetch",
                "JARVIS_QWEN_MAX_NUM_SEQS": str(int(profile.max_num_seqs)),
                "JARVIS_QWEN_ENFORCE_EAGER": "--enforce-eager" if profile.eager_mode else "",
                "JARVIS_QWEN_CPU_OFFLOAD_ARGS": (
                    f"--cpu-offload-gb {_format_number(profile.cpu_offload_gb)}"
                    if profile.cpu_offload_gb > 0
                    else ""
                ),
                # vLLM 0.23 removed --swap-space; KV CPU spill uses
                # --kv-offloading-size / --kv-offloading-backend instead.
                "JARVIS_QWEN_KV_OFFLOAD_ARGS": (
                    (
                        f"--kv-offloading-size {int(profile.kv_offloading_gb)} "
                        f"--kv-offloading-backend native"
                    )
                    if profile.kv_offloading_gb > 0
                    else ""
                ),
                "JARVIS_QWEN_EXTRA_ARGS": _vllm_extra_args(profile),
                "JARVIS_ENABLE_UITARS": "0",
            },
        }


def model_allowed_for_profile(settings: JarvisSettings, model_dir_name: str) -> bool:
    """Keep built-in model identities bound to their named runtime profiles."""

    return (
        model_dir_name not in PROFILE_MODEL_DIR_NAMES
        or model_dir_name == settings.profile.model_dir_name
    )


def _vllm_extra_args(profile: RuntimeProfile) -> str:
    options = profile.vllm_extra_args
    args: list[str] = []
    if options.language_model_only:
        args.append("--language-model-only")
    if options.skip_mm_profiling:
        args.append("--skip-mm-profiling")
    if options.mm_processor_cache_gb is not None:
        args.extend(
            ["--mm-processor-cache-gb", _format_number(options.mm_processor_cache_gb)]
        )
    if options.max_num_batched_tokens is not None:
        args.extend(["--max-num-batched-tokens", str(options.max_num_batched_tokens)])
    if options.trust_remote_code:
        args.append("--trust-remote-code")
    if options.reasoning_parser:
        args.extend(["--reasoning-parser", options.reasoning_parser])
    if options.tool_call_parser:
        args.extend(["--tool-call-parser", options.tool_call_parser])
        if options.enable_auto_tool_choice:
            args.append("--enable-auto-tool-choice")
    if options.limit_mm_per_prompt:
        args.extend(["--limit-mm-per-prompt", options.limit_mm_per_prompt])
    return " ".join(args)


def _format_number(value: float | int) -> str:
    return f"{value:g}"


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
