from __future__ import annotations

import os
import platform
from dataclasses import dataclass, field
from pathlib import Path


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def default_home() -> Path:
    raw = os.environ.get("JARVIS_HOME")
    if raw:
        return Path(raw)
    if platform.system().lower() == "windows":
        return Path(r"D:\jarvis")
    if Path("/mnt/d").exists():
        return Path("/mnt/d/jarvis")
    return Path.home() / ".jarvis"


@dataclass(frozen=True)
class VllmExtraArgs:
    """Typed optional vLLM flags that are part of a profile's runtime identity."""

    language_model_only: bool = False
    skip_mm_profiling: bool = False
    mm_processor_cache_gb: float | None = None
    max_num_batched_tokens: int | None = None


@dataclass(frozen=True)
class RuntimeProfile:
    name: str
    title: str
    description: str
    model_dir_name: str
    eager_mode: bool
    max_steps: int
    temperature: float
    max_model_len: int
    gpu_memory_utilization: float
    kv_cache_dtype: str
    max_num_seqs: int
    cpu_offload_gb: float
    kv_offloading_gb: int
    vllm_extra_args: VllmExtraArgs = field(default_factory=VllmExtraArgs)


PROFILES: dict[str, RuntimeProfile] = {
    # Experimental Docker/WSL 31B stability path. CPU weight streaming is very slow.
    "gemma4-mono": RuntimeProfile(
        name="gemma4-mono",
        title="Gemma 4 Mono (Offload)",
        description=(
            "Experimental Docker/WSL Gemma 4 31B IT NVFP4 stability profile. "
            "Partial CPU weight offload + native KV offload, eager mode, single-seq. "
            "Measured decode is below 1 tok/s on RTX 5090; use only for long-context "
            "quality checks, never interactive chat. Prefer gemma4-turbo."
        ),
        model_dir_name="gemma4-31b-it-nvfp4",
        eager_mode=True,
        max_steps=12,
        temperature=0.15,
        max_model_len=16384,
        gpu_memory_utilization=0.85,
        kv_cache_dtype="fp8",
        # One request at a time so background cognition cannot half the already-slow
        # PCIe-offload decode path.
        max_num_seqs=1,
        cpu_offload_gb=24,
        kv_offloading_gb=16,
    ),
    # Live-certified text-only 31B quality path. A small CPU spill leaves room for
    # activations and KV while keeping almost all of the checkpoint GPU-resident.
    "gemma4-mono-perf": RuntimeProfile(
        name="gemma4-mono-perf",
        title="Gemma 4 Mono Perf",
        description=(
            "Experimental Docker/WSL Gemma 4 31B IT NVFP4 quality profile. "
            "Minimal CPU weight offload (2.5GB), eager mode, 4k context and "
            "text-only execution. Measured around 2.45 tok/s on RTX 5090: much "
            "faster than the old mono profile, but still not an interactive runtime. "
            "Use gemma4-turbo for Command Center chat."
        ),
        model_dir_name="gemma4-31b-it-nvfp4",
        eager_mode=True,
        max_steps=16,
        temperature=0.15,
        max_model_len=4096,
        # Keep util below free VRAM at process start (host often leaves ~1–2 GiB busy).
        gpu_memory_utilization=0.93,
        kv_cache_dtype="fp8",
        # One operator request owns decode; autonomy waits instead of splitting tok/s.
        max_num_seqs=1,
        cpu_offload_gb=2.5,
        kv_offloading_gb=0,
        vllm_extra_args=VllmExtraArgs(
            language_model_only=True,
            skip_mm_profiling=True,
            mm_processor_cache_gb=0,
            max_num_batched_tokens=512,
        ),
    ),
    "gemma4-turbo": RuntimeProfile(
        name="gemma4-turbo",
        title="Gemma 4 Turbo",
        description=(
            "Recommended interactive Gemma 4 26B A4B NVFP4 profile for RTX 5090. "
            "It fits GPU memory without CPU weight offload."
        ),
        model_dir_name="gemma4-26b-a4b-nvfp4",
        eager_mode=False,
        max_steps=24,
        temperature=0.25,
        max_model_len=32768,
        gpu_memory_utilization=0.82,
        kv_cache_dtype="fp8",
        max_num_seqs=16,
        cpu_offload_gb=0,
        kv_offloading_gb=0,
    ),
}


@dataclass(frozen=True)
class JarvisSettings:
    home: Path
    profile: RuntimeProfile
    data_dir: Path
    cache_dir: Path
    log_dir: Path
    model_root: Path
    model_dir: Path
    docker_dir: Path
    state_dir: Path
    database_path: Path
    llm_base_url: str
    llm_model: str
    llm_enabled: bool
    llm_timeout_sec: float
    llm_max_tokens: int
    verify_answers: bool
    embeddings_enabled: bool
    embeddings_base_url: str
    embeddings_model: str
    autonomy_enabled: bool
    telemetry_interval_sec: int
    health_interval_sec: int
    learning_interval_sec: int
    cognition_enabled: bool
    cognition_interval_sec: int
    cognition_max_tokens: int
    autonomy_mission_interval_sec: int
    api_host: str
    api_port: int
    api_require_token_on_loopback: bool

    def public_dict(self) -> dict[str, object]:
        return {
            "home": str(self.home),
            "profile": {
                "name": self.profile.name,
                "title": self.profile.title,
                "description": self.profile.description,
                "eager_mode": self.profile.eager_mode,
                "max_steps": self.profile.max_steps,
                "temperature": self.profile.temperature,
                "max_model_len": self.profile.max_model_len,
                "gpu_memory_utilization": self.profile.gpu_memory_utilization,
                "kv_cache_dtype": self.profile.kv_cache_dtype,
                "max_num_seqs": self.profile.max_num_seqs,
                "cpu_offload_gb": self.profile.cpu_offload_gb,
                "kv_offloading_gb": self.profile.kv_offloading_gb,
                "vllm_extra_args": {
                    "language_model_only": self.profile.vllm_extra_args.language_model_only,
                    "skip_mm_profiling": self.profile.vllm_extra_args.skip_mm_profiling,
                    "mm_processor_cache_gb": (
                        self.profile.vllm_extra_args.mm_processor_cache_gb
                    ),
                    "max_num_batched_tokens": (
                        self.profile.vllm_extra_args.max_num_batched_tokens
                    ),
                },
            },
            "paths": {
                "data": str(self.data_dir),
                "files": str(self.data_dir / "files"),
                "cache": str(self.cache_dir),
                "logs": str(self.log_dir),
                "models": str(self.model_root),
                "active_model": str(self.model_dir),
                "docker": str(self.docker_dir),
                "state": str(self.state_dir),
                "database": str(self.database_path),
                "memory_vault": str(self.data_dir / "memory-vault"),
                "host_profile": str(self.home / "host_profile.json"),
                "execution_playbooks": str(self.state_dir / "execution-playbooks.sqlite3"),
            },
            "llm": {
                "enabled": self.llm_enabled,
                "base_url": self.llm_base_url,
                "model": self.llm_model,
                "timeout_sec": self.llm_timeout_sec,
                "max_tokens": self.llm_max_tokens,
                "verify_answers": self.verify_answers,
            },
            "embeddings": {
                "enabled": self.embeddings_enabled,
                "base_url": self.embeddings_base_url,
                "model": self.embeddings_model,
            },
            "autonomy": {
                "enabled": self.autonomy_enabled,
                "telemetry_interval_sec": self.telemetry_interval_sec,
                "health_interval_sec": self.health_interval_sec,
                "learning_interval_sec": self.learning_interval_sec,
                "cognition_enabled": self.cognition_enabled,
                "cognition_interval_sec": self.cognition_interval_sec,
                "cognition_max_tokens": self.cognition_max_tokens,
                "mission_interval_sec": self.autonomy_mission_interval_sec,
            },
            "api": {
                "host": self.api_host,
                "port": self.api_port,
                "require_token_on_loopback": self.api_require_token_on_loopback,
            },
        }


def load_settings(profile_name: str | None = None) -> JarvisSettings:
    home = default_home()
    selected_name = profile_name or os.environ.get("JARVIS_PROFILE", "gemma4-turbo")
    profile = PROFILES.get(selected_name)
    if profile is None:
        valid = ", ".join(sorted(PROFILES))
        raise ValueError(f"Unknown JARVIS_PROFILE={selected_name!r}. Valid profiles: {valid}")

    data_dir = home / "data" / "jarvis-gpt"
    cache_dir = home / "cache" / "jarvis-gpt"
    log_dir = home / "logs" / "jarvis-gpt"
    model_root = _model_root(home)
    docker_dir = home / "docker" / "jarvis-gpt"
    state_dir = data_dir / "state"
    database_path = state_dir / "jarvis.sqlite3"

    return JarvisSettings(
        home=home,
        profile=profile,
        data_dir=data_dir,
        cache_dir=cache_dir,
        log_dir=log_dir,
        model_root=model_root,
        model_dir=model_root / profile.model_dir_name,
        docker_dir=docker_dir,
        state_dir=state_dir,
        database_path=database_path,
        llm_base_url=os.environ.get("JARVIS_LLM_BASE_URL", "http://localhost:8001/v1").rstrip("/"),
        llm_model=os.environ.get("JARVIS_LLM_MODEL", "dispatcher"),
        llm_enabled=_bool_env("JARVIS_LLM_ENABLED", True),
        llm_timeout_sec=_float_env("JARVIS_LLM_TIMEOUT_SEC", 240.0),
        llm_max_tokens=_int_env("JARVIS_LLM_MAX_TOKENS", 2048),
        verify_answers=_bool_env("JARVIS_VERIFY_ANSWERS", True),
        embeddings_enabled=_bool_env("JARVIS_EMBEDDINGS_ENABLED", False),
        embeddings_base_url=os.environ.get(
            "JARVIS_EMBEDDINGS_BASE_URL",
            os.environ.get("JARVIS_LLM_BASE_URL", "http://localhost:8001/v1"),
        ).rstrip("/"),
        embeddings_model=os.environ.get("JARVIS_EMBEDDINGS_MODEL", ""),
        autonomy_enabled=_bool_env("JARVIS_AUTONOMY_ENABLED", True),
        telemetry_interval_sec=_int_env("JARVIS_TELEMETRY_INTERVAL_SEC", 120),
        health_interval_sec=_int_env("JARVIS_HEALTH_INTERVAL_SEC", 300),
        learning_interval_sec=_int_env("JARVIS_LEARNING_INTERVAL_SEC", 120),
        cognition_enabled=_bool_env("JARVIS_COGNITION_ENABLED", True),
        cognition_interval_sec=_int_env("JARVIS_COGNITION_INTERVAL_SEC", 300),
        cognition_max_tokens=_int_env("JARVIS_COGNITION_MAX_TOKENS", 512),
        autonomy_mission_interval_sec=_int_env("JARVIS_AUTONOMY_MISSION_INTERVAL_SEC", 120),
        api_host=os.environ.get("JARVIS_API_HOST", "0.0.0.0"),
        api_port=_int_env("JARVIS_API_PORT", 8000),
        api_require_token_on_loopback=_bool_env("JARVIS_API_REQUIRE_TOKEN_ON_LOOPBACK", False),
    )


def ensure_runtime_dirs(settings: JarvisSettings) -> list[Path]:
    paths = [
        settings.home,
        settings.data_dir,
        settings.data_dir / "files",
        settings.cache_dir,
        settings.log_dir,
        settings.model_root,
        settings.docker_dir,
        settings.state_dir,
    ]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _model_root(home: Path) -> Path:
    raw = os.environ.get("JARVIS_MODEL_ROOT")
    if raw:
        return Path(raw)
    data_models = home / "data" / "models"
    if data_models.exists():
        return data_models
    return home / "models"
