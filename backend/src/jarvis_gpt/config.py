from __future__ import annotations

import os
import platform
from dataclasses import dataclass
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
    cpu_offload_gb: int
    swap_space_gb: int


PROFILES: dict[str, RuntimeProfile] = {
    "gemma4-mono": RuntimeProfile(
        name="gemma4-mono",
        title="Gemma 4 Mono",
        description=(
            "Quality Gemma 4 31B IT NVFP4 profile with CPU offload "
            "and conservative concurrency."
        ),
        model_dir_name="gemma4-31b-it-nvfp4",
        eager_mode=True,
        max_steps=12,
        temperature=0.15,
        max_model_len=16384,
        gpu_memory_utilization=0.94,
        kv_cache_dtype="fp8",
        max_num_seqs=4,
        cpu_offload_gb=8,
        swap_space_gb=8,
    ),
    "gemma4-turbo": RuntimeProfile(
        name="gemma4-turbo",
        title="Gemma 4 Turbo",
        description="Fast Gemma 4 26B A4B NVFP4 profile for warmed runtime and throughput.",
        model_dir_name="gemma4-26b-a4b-nvfp4",
        eager_mode=False,
        max_steps=24,
        temperature=0.25,
        max_model_len=32768,
        gpu_memory_utilization=0.82,
        kv_cache_dtype="fp8",
        max_num_seqs=16,
        cpu_offload_gb=0,
        swap_space_gb=0,
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
    autonomy_enabled: bool
    telemetry_interval_sec: int
    health_interval_sec: int
    learning_interval_sec: int
    api_host: str
    api_port: int

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
                "swap_space_gb": self.profile.swap_space_gb,
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
            },
            "llm": {
                "enabled": self.llm_enabled,
                "base_url": self.llm_base_url,
                "model": self.llm_model,
                "timeout_sec": self.llm_timeout_sec,
                "max_tokens": self.llm_max_tokens,
            },
            "autonomy": {
                "enabled": self.autonomy_enabled,
                "telemetry_interval_sec": self.telemetry_interval_sec,
                "health_interval_sec": self.health_interval_sec,
                "learning_interval_sec": self.learning_interval_sec,
            },
            "api": {"host": self.api_host, "port": self.api_port},
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
        llm_max_tokens=_int_env("JARVIS_LLM_MAX_TOKENS", 512),
        autonomy_enabled=_bool_env("JARVIS_AUTONOMY_ENABLED", True),
        telemetry_interval_sec=_int_env("JARVIS_TELEMETRY_INTERVAL_SEC", 120),
        health_interval_sec=_int_env("JARVIS_HEALTH_INTERVAL_SEC", 300),
        learning_interval_sec=_int_env("JARVIS_LEARNING_INTERVAL_SEC", 600),
        api_host=os.environ.get("JARVIS_API_HOST", "0.0.0.0"),
        api_port=_int_env("JARVIS_API_PORT", 8000),
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
