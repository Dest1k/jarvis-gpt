from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass, field, replace
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


def load_local_env_file(path: str | Path | None = None) -> list[str]:
    """Populate ``os.environ`` from a gitignored ``KEY=VALUE`` secrets file, if present.

    The backend reads every secret from the process environment (no dotenv). To let
    the owner persist secrets — search API keys, tokens — in a file instead of
    exporting them on every launch, the CLI reads a simple ``.env``-style file at
    startup. Rules are deliberately minimal and safe:

    - default path ``backend/.env.local`` (already gitignored via ``.env.*``),
      overridable with ``JARVIS_ENV_FILE``;
    - ``KEY=VALUE`` lines; ``#`` comments and blank lines skipped; an optional
      ``export`` prefix and surrounding single/double quotes on the value are stripped;
    - an already-set environment variable is **never** overridden, so an explicit
      shell ``export`` / ``$env:`` always wins over the file.

    Returns the names (never the values) of the keys it applied, for optional logging.
    """

    if path is not None:
        target = Path(path)
    elif os.environ.get("JARVIS_ENV_FILE"):
        target = Path(os.environ["JARVIS_ENV_FILE"])
    else:
        target = Path(__file__).resolve().parents[2] / ".env.local"
    if not target.is_file():
        return []
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return []
    applied: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        key, sep, value = stripped.partition("=")
        key = key.strip()
        if not sep or not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value
        applied.append(key)
    return applied


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
    # Reasoning / tool-call parsing (needed by newer Qwen chat models). Left unset by
    # default so a profile serves cleanly even if a given vLLM build lacks a parser;
    # enabling an unsupported parser makes vLLM fail to start, so these are opt-in.
    reasoning_parser: str | None = None
    tool_call_parser: str | None = None
    enable_auto_tool_choice: bool = False
    # Multimodal bounds for vision-language models, e.g. "image=2,video=1".
    limit_mm_per_prompt: str | None = None
    trust_remote_code: bool = False
    # Speculative decoding, e.g. MTP. Compact JSON with NO spaces (survives the compose
    # command word-split); emitted single-quoted so the inner double quotes are preserved.
    # Example: '{"method":"mtp","num_speculative_tokens":2}'.
    speculative_config: str | None = None
    # Async scheduling for lower per-step latency (vLLM >= 0.25). Harmless if the build
    # already enables it by default.
    async_scheduling: bool = False


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
    # vLLM tokenizer mode. Gemma needs the slow tokenizer (its fast tokenizer had a
    # correctness bug on this stack), but a fast-only checkpoint (e.g. Qwen ships a
    # self-contained tokenizer.json with NO merges.txt, so the slow BPE cannot be
    # built) MUST use "auto" or the server fails to start.
    tokenizer_mode: str = "slow"
    # vLLM serving image for this profile. The certified Gemma stack stays pinned to
    # v0.23.0; the Qwen3.5-VL NVFP4 checkpoint needs vLLM >= 0.25 for the fast NVFP4
    # kernels (cutlass / flashinfer, not the 2x-slower Marlin fallback), so its profile
    # selects a newer image. An explicit JARVIS_VLLM_IMAGE env override always wins.
    vllm_image: str = "vllm/vllm-openai:v0.23.0"
    # True when the served model is a vision-language model that understands image
    # pixels. The chat pipeline only forwards image attachments as real vision content
    # parts when this is set; a text-only brain keeps treating images as file metadata.
    vision_capable: bool = False
    # True when the model's "thinking" mode emits an unparseable chain-of-thought into
    # the answer body (Qwen3.5 dumps a free-form "Here's a thinking process:" trace with
    # no <think> delimiters and no reasoning_content, so it cannot be stripped). For such
    # models the LLM router forces enable_thinking=False so chat answers stay clean and
    # fast; Jarvis supplies its own reasoning at the agent/orchestrator layer.
    suppress_model_thinking: bool = False
    # Product certification on the current certified host (not a performance claim).
    certification: str = "unsupported"  # certified | experimental | unsupported
    interactive_certified: bool = False
    default_recommended: bool = False
    research_only: bool = True
    readiness_deadline_sec: float = 300.0
    certification_reason: str = ""
    menu_visible: bool = False
    requires_experimental_opt_in: bool = True


# Product decision RESOLVED_BY_PRODUCT_DECISION (SPARK-0013):
# 31B profiles are not made "fast"; they are labeled experimental/unsupported
# interactive on the current certified host. gemma4-turbo remains the only
# certified interactive default.
PROFILES: dict[str, RuntimeProfile] = {
    # Unsupported interactive / research-only on the current certified host.
    "gemma4-mono": RuntimeProfile(
        name="gemma4-mono",
        title="Gemma 4 Mono (Offload)",
        description=(
            "UNSUPPORTED interactive on the current certified host. "
            "Research-only Docker/WSL Gemma 4 31B IT NVFP4 path with heavy offload. "
            "Measured decode is below 1 tok/s; never present as ready interactive chat. "
            "Prefer gemma4-turbo. RESOLVED_BY_PRODUCT_DECISION for SPARK-0013."
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
        certification="unsupported",
        interactive_certified=False,
        default_recommended=False,
        research_only=True,
        readiness_deadline_sec=1200.0,
        certification_reason=(
            "31B offload path is research-only on this host; cyclic/slow decode "
            "makes interactive use unsupported (RESOLVED_BY_PRODUCT_DECISION)."
        ),
        menu_visible=False,
        requires_experimental_opt_in=True,
    ),
    # Experimental / research-only on the current certified host.
    "gemma4-mono-perf": RuntimeProfile(
        name="gemma4-mono-perf",
        title="Gemma 4 Mono Perf",
        description=(
            "EXPERIMENTAL / research-only on the current certified host. "
            "Gemma 4 31B IT NVFP4 quality path (~2.45 tok/s measured) is not certified "
            "interactive. Prefer gemma4-turbo. RESOLVED_BY_PRODUCT_DECISION for SPARK-0013."
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
        certification="experimental",
        interactive_certified=False,
        default_recommended=False,
        research_only=True,
        readiness_deadline_sec=900.0,
        certification_reason=(
            "31B mono-perf is experimental/research-only on this host; not certified "
            "for interactive Command Center use (RESOLVED_BY_PRODUCT_DECISION)."
        ),
        menu_visible=False,
        requires_experimental_opt_in=True,
    ),
    "gemma4-turbo": RuntimeProfile(
        name="gemma4-turbo",
        title="Gemma 4 Turbo",
        description=(
            "Gemma 4 26B A4B NVFP4 profile — secondary certified model "
            "for the current host. Qwen3.5-VL is now the recommended default."
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
        certification="certified",
        interactive_certified=True,
        default_recommended=False,
        research_only=False,
        readiness_deadline_sec=180.0,
        certification_reason=(
            "Secondary certified model; Qwen3.5-VL is the recommended default."
        ),
        menu_visible=True,
        requires_experimental_opt_in=False,
    ),
    # Qwen3.5-MoE vision-language model — certified primary brain of Jarvis GPT.
    # 35B total / ~3B active fits the 32 GB 5090 in NVFP4 with fp8 KV, no CPU offload.
    # Vision+video capable. Default recommended profile.
    "qwen36-vl": RuntimeProfile(
        name="qwen36-vl",
        title="Qwen3.5-VL 35B-A3B NVFP4",
        description=(
            "Qwen3.5-MoE vision-language model — certified primary brain of Jarvis GPT. "
            "(35B total / ~3B active) in NVFP4 + fp8 KV, fully resident on the 5090. "
            "Vision-capable with image/video understanding."
        ),
        model_dir_name="qwen3.6-35b-a3b-nvfp4",
        eager_mode=False,
        max_steps=24,
        temperature=0.25,
        max_model_len=32768,
        gpu_memory_utilization=0.90,
        kv_cache_dtype="fp8",
        max_num_seqs=16,
        cpu_offload_gb=0,
        kv_offloading_gb=0,
        # Qwen ships only a fast tokenizer.json (no merges.txt) -> slow mode cannot load.
        tokenizer_mode="auto",
        # NVFP4 needs vLLM >= 0.25 for the fast cutlass/flashinfer kernels. The local
        # derivative keeps that exact runtime and replaces only the unstable uvloop
        # top-level runner with the standard asyncio loop.
        vllm_image="jarvis/vllm-openai:v0.25.1-asyncio-e4f88a8",
        # Qwen3.5-VL sees images/video: forward chat image attachments as vision input.
        vision_capable=True,
        # Qwen dumps a free-form thinking trace into the answer with no <think> tags to
        # strip; run it non-thinking for clean, fast chat (Jarvis reasons at its own layer).
        suppress_model_thinking=True,
        vllm_extra_args=VllmExtraArgs(
            skip_mm_profiling=True,
            mm_processor_cache_gb=4.0,
            # Qwen3.5's hybrid Mamba/Gated-DeltaNet layers force the attention block
            # size up to 2096 (to align with the mamba page size); vLLM then asserts
            # block_size <= max_num_batched_tokens, whose default 2048 is too small and
            # crashes EngineCore init. Raise it so the constraint holds (also a better
            # prefill chunk).
            max_num_batched_tokens=4096,
            # Accept several images (and a video) per prompt so multi-image vision works;
            # vLLM 0.25 parses this as JSON. The VLM default is 1 image/prompt.
            limit_mm_per_prompt='{"image":4,"video":1}',
            # MTP self-speculation was measured LIVE on this box (5090 + vLLM 0.25.1
            # FLASHINFER_CUTLASS NVFP4) and was a NET LOSS: ~150 vs ~203 tok/s pure decode
            # (400 forced tokens, apples-to-apples) — the already memory-bound NVFP4 decode
            # doesn't benefit from the extra draft forward passes (num_speculative_tokens>1
            # also lowers acceptance). Left OFF; the field stays available for hardware
            # where it wins: speculative_config='{"method":"mtp","num_speculative_tokens":2}'.
            # Enable these live once base serving is confirmed (values may vary by vLLM
            # build): reasoning_parser="qwen3", tool_call_parser="hermes",
            # enable_auto_tool_choice=True.
        ),
        certification="certified",
        interactive_certified=True,
        default_recommended=True,
        research_only=False,
        readiness_deadline_sec=900.0,
        certification_reason=(
            "Qwen3.5-VL is now the primary model for Jarvis GPT."
        ),
        menu_visible=True,
        requires_experimental_opt_in=False,
    ),
}

# Keep the uncensored checkpoint on the exact same runtime contract as the certified
# Qwen profile. Only model identity and pre-certification product metadata differ; using
# dataclasses.replace prevents serving knobs from drifting between the two profiles.
PROFILES["qwen36-vl-uncensored"] = replace(
    PROFILES["qwen36-vl"],
    name="qwen36-vl-uncensored",
    title="Qwen3.6-VL 35B-A3B Uncensored NVFP4",
    description=(
        "EXPERIMENTAL / research-only uncensored Qwen3.6 35B-A3B NVFP4 checkpoint "
        "from NeuralNet-Hub. Runtime parameters mirror qwen36-vl."
    ),
    model_dir_name="qwen3.6-35b-a3b-uncensored-nvfp4",
    certification="experimental",
    interactive_certified=False,
    default_recommended=False,
    research_only=True,
    certification_reason=(
        "Live text, vision, and Jarvis API smoke passed on this host; remains "
        "research-only because it is a third-party uncensored checkpoint."
    ),
    requires_experimental_opt_in=True,
)


def profile_public_dict(profile: RuntimeProfile) -> dict[str, object]:
    return {
        "name": profile.name,
        "title": profile.title,
        "description": profile.description,
        "model_dir_name": profile.model_dir_name,
        "eager_mode": profile.eager_mode,
        "max_steps": profile.max_steps,
        "temperature": profile.temperature,
        "max_model_len": profile.max_model_len,
        "gpu_memory_utilization": profile.gpu_memory_utilization,
        "kv_cache_dtype": profile.kv_cache_dtype,
        "max_num_seqs": profile.max_num_seqs,
        "cpu_offload_gb": profile.cpu_offload_gb,
        "kv_offloading_gb": profile.kv_offloading_gb,
        "tokenizer_mode": profile.tokenizer_mode,
        "vllm_image": profile.vllm_image,
        "vision_capable": profile.vision_capable,
        "suppress_model_thinking": profile.suppress_model_thinking,
        "certification": profile.certification,
        "interactive_certified": profile.interactive_certified,
        "default_recommended": profile.default_recommended,
        "research_only": profile.research_only,
        "readiness_deadline_sec": profile.readiness_deadline_sec,
        "certification_reason": profile.certification_reason,
        "menu_visible": profile.menu_visible,
        "requires_experimental_opt_in": profile.requires_experimental_opt_in,
        "vllm_extra_args": {
            "language_model_only": profile.vllm_extra_args.language_model_only,
            "skip_mm_profiling": profile.vllm_extra_args.skip_mm_profiling,
            "mm_processor_cache_gb": profile.vllm_extra_args.mm_processor_cache_gb,
            "max_num_batched_tokens": profile.vllm_extra_args.max_num_batched_tokens,
            "reasoning_parser": profile.vllm_extra_args.reasoning_parser,
            "tool_call_parser": profile.vllm_extra_args.tool_call_parser,
            "enable_auto_tool_choice": profile.vllm_extra_args.enable_auto_tool_choice,
            "limit_mm_per_prompt": profile.vllm_extra_args.limit_mm_per_prompt,
            "trust_remote_code": profile.vllm_extra_args.trust_remote_code,
        },
    }


def certified_interactive_profiles() -> list[str]:
    return [
        name
        for name, profile in PROFILES.items()
        if profile.interactive_certified and profile.certification == "certified"
    ]


def detect_repeated_token_degeneration(text: str, *, min_repeats: int = 12) -> bool:
    """True when output collapses into cyclic/repeated-token degeneration."""

    cleaned = " ".join(str(text or "").split()).strip()
    if len(cleaned) < max(24, min_repeats):
        return False
    # Character-run collapse: aaaaa...
    if re.search(r"(.)\1{" + str(max(8, min_repeats - 1)) + r",}", cleaned):
        return True
    tokens = cleaned.split()
    if len(tokens) >= min_repeats and len(set(tokens[-min_repeats:])) == 1:
        return True
    # Short cycle: ab ab ab ab ...
    if len(tokens) >= min_repeats:
        window = tokens[-min_repeats:]
        for cycle in (1, 2, 3, 4):
            if min_repeats % cycle != 0:
                continue
            unit = window[:cycle]
            if unit * (min_repeats // cycle) == window:
                return True
    return False


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
    operator_full_autonomy: bool
    autonomy_enabled: bool
    telemetry_interval_sec: int
    health_interval_sec: int
    learning_interval_sec: int
    cognition_enabled: bool
    cognition_interval_sec: int
    cognition_max_tokens: int
    autonomy_mission_interval_sec: int
    reminder_interval_sec: int
    reminder_tz: str
    # ---- Scheduled agent tasks --------------------------------------------------
    # A recurring "do X and report" reminder (payload.kind == "agent_task") fires a
    # full agent turn on its wall-clock schedule and delivers the answer to Telegram.
    # Off flips such reminders back to a passive text nudge.
    scheduled_tasks_enabled: bool
    # ---- Proactive screen watching --------------------------------------------
    # A screen watch is a bounded interval reminder whose poll captures the desktop and
    # asks the active vision model whether an operator-supplied condition is visible.
    # Limits protect GPU time and keep accidental/unbounded monitoring impossible.
    screen_watch_enabled: bool
    screen_watch_min_interval_sec: int
    screen_watch_default_duration_sec: int
    screen_watch_max_duration_sec: int
    screen_watch_max_active: int
    # ---- Code/data sandbox ------------------------------------------------------
    # code.run executes operator Python in an isolated child (wall-clock timeout +
    # memory ceiling + kill-on-close job + curated secret-free env). Resource-isolated,
    # NOT security-isolated (see sandbox.py). Off disables the code.run tool.
    sandbox_enabled: bool
    sandbox_timeout_sec: int
    sandbox_max_output_bytes: int
    sandbox_mem_limit_mb: int
    # ---- Proactive health alerts -----------------------------------------------
    # The supervisor watches each telemetry snapshot and, on an ok→bad transition,
    # emits a runtime event, pushes it to the UI bus, and (if Telegram is wired)
    # to the owner's phone. Edge-triggered — one alert per breach, one recovery.
    health_alerts_enabled: bool
    health_alert_gpu_temp_c: float
    health_alert_gpu_vram_ratio: float
    health_alert_disk_ratio: float
    health_alert_memory_ratio: float
    # ---- Self-healing runtime ---------------------------------------------------
    # The supervisor probes the local model dispatcher; when its container has
    # crashed/OOM-killed or hung (running but not serving) it auto-restarts it and
    # alerts the owner. It never auto-starts a dispatcher the owner never launched
    # (no container) nor one they stopped cleanly (Exited 0). A restart budget +
    # consecutive-failure confirmation prevent restart storms; exhausting the budget
    # escalates to the owner instead of looping.
    self_healing_enabled: bool
    self_healing_interval_sec: int
    self_healing_min_failures: int
    self_healing_max_restarts: int
    self_healing_window_sec: int
    self_healing_grace_sec: int
    # ---- Self-replanning missions -----------------------------------------------
    # After an autonomously-run mission stops on the step budget, the runtime keeps
    # continuing it (bounded by max_rounds) until it finishes. A mission that stays
    # genuinely blocked (needs intervention, not just the verification gate) is
    # escalated to the owner on Telegram so they can step in while away.
    mission_self_replan_enabled: bool
    mission_self_replan_max_rounds: int
    api_host: str
    api_port: int
    api_require_token_on_loopback: bool
    # ---- Hybrid brain (SCAFFOLD — INACTIVE by default) --------------------------
    # A prepared-but-off delegation of hard reasoning/synthesis to a frontier model
    # reached through the owner's *logged-in Claude Code CLI subscription* (NOT a
    # billed API key). Nothing routes here while `hybrid_brain_enabled` is False.
    # See frontier_brain.py for the whole story and how to activate it.
    hybrid_brain_enabled: bool
    frontier_cli_path: str
    frontier_model: str
    frontier_effort: str
    frontier_timeout_sec: float

    def public_dict(self) -> dict[str, object]:
        return {
            "home": str(self.home),
            "profile": profile_public_dict(self.profile),
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
            "permissions": {
                "operator_full_autonomy": self.operator_full_autonomy,
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
                "mission_self_replan_enabled": self.mission_self_replan_enabled,
                "mission_self_replan_max_rounds": self.mission_self_replan_max_rounds,
            },
            "reminders": {
                "interval_sec": self.reminder_interval_sec,
                "timezone": self.reminder_tz,
            },
            "screen_watch": {
                "enabled": self.screen_watch_enabled,
                "min_interval_sec": self.screen_watch_min_interval_sec,
                "default_duration_sec": self.screen_watch_default_duration_sec,
                "max_duration_sec": self.screen_watch_max_duration_sec,
                "max_active": self.screen_watch_max_active,
            },
            "health_alerts": {
                "enabled": self.health_alerts_enabled,
                "gpu_temp_c": self.health_alert_gpu_temp_c,
                "gpu_vram_ratio": self.health_alert_gpu_vram_ratio,
                "disk_ratio": self.health_alert_disk_ratio,
                "memory_ratio": self.health_alert_memory_ratio,
            },
            "self_healing": {
                "enabled": self.self_healing_enabled,
                "interval_sec": self.self_healing_interval_sec,
                "min_failures": self.self_healing_min_failures,
                "max_restarts": self.self_healing_max_restarts,
                "window_sec": self.self_healing_window_sec,
                "grace_sec": self.self_healing_grace_sec,
            },
            "api": {
                "host": self.api_host,
                "port": self.api_port,
                "require_token_on_loopback": self.api_require_token_on_loopback,
            },
            # Hybrid brain is a dormant scaffold; surfaced here so its status is
            # always visible in the settings dump even while inactive.
            "hybrid_brain": {
                "enabled": self.hybrid_brain_enabled,
                "status": "active" if self.hybrid_brain_enabled else "scaffold (inactive)",
                "backend": "claude-code-cli-subscription",
                "cli_path": self.frontier_cli_path,
                "model": self.frontier_model,
                "effort": self.frontier_effort,
            },
        }


def load_settings(profile_name: str | None = None) -> JarvisSettings:
    home = default_home()
    selected_name = profile_name or os.environ.get("JARVIS_PROFILE", "qwen36-vl")
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
        # Owner full-autonomy posture (default on). When enabled, the single
        # operator is the system administrator: their own chat turn authorizes the
        # work it asks for, so the runtime acts without clarification round-trips or
        # approval gates and keeps the chat to request/analysis/action/result. Set to
        # 0 to fall back to the gated posture (clarify-first, approval-gated tools).
        # Reliability guarantees (atomic effect keys, verified writes, executive
        # contracts) hold in both modes. See agent.py `_owner_autonomy_active`.
        operator_full_autonomy=_bool_env("JARVIS_OPERATOR_FULL_AUTONOMY", True),
        autonomy_enabled=_bool_env("JARVIS_AUTONOMY_ENABLED", True),
        telemetry_interval_sec=_int_env("JARVIS_TELEMETRY_INTERVAL_SEC", 120),
        health_interval_sec=_int_env("JARVIS_HEALTH_INTERVAL_SEC", 300),
        learning_interval_sec=_int_env("JARVIS_LEARNING_INTERVAL_SEC", 120),
        cognition_enabled=_bool_env("JARVIS_COGNITION_ENABLED", True),
        cognition_interval_sec=_int_env("JARVIS_COGNITION_INTERVAL_SEC", 300),
        cognition_max_tokens=_int_env("JARVIS_COGNITION_MAX_TOKENS", 512),
        autonomy_mission_interval_sec=_int_env("JARVIS_AUTONOMY_MISSION_INTERVAL_SEC", 120),
        reminder_interval_sec=_int_env("JARVIS_REMINDER_INTERVAL_SEC", 30),
        reminder_tz=os.environ.get("JARVIS_REMINDER_TZ", "Europe/Moscow"),
        scheduled_tasks_enabled=_bool_env("JARVIS_SCHEDULED_TASKS_ENABLED", True),
        screen_watch_enabled=_bool_env("JARVIS_SCREEN_WATCH_ENABLED", True),
        screen_watch_min_interval_sec=_int_env("JARVIS_SCREEN_WATCH_MIN_INTERVAL_SEC", 120),
        screen_watch_default_duration_sec=_int_env(
            "JARVIS_SCREEN_WATCH_DEFAULT_DURATION_SEC", 7200
        ),
        screen_watch_max_duration_sec=_int_env(
            "JARVIS_SCREEN_WATCH_MAX_DURATION_SEC", 21600
        ),
        screen_watch_max_active=_int_env("JARVIS_SCREEN_WATCH_MAX_ACTIVE", 3),
        sandbox_enabled=_bool_env("JARVIS_SANDBOX_ENABLED", True),
        sandbox_timeout_sec=_int_env("JARVIS_SANDBOX_TIMEOUT_SEC", 30),
        sandbox_max_output_bytes=_int_env("JARVIS_SANDBOX_MAX_OUTPUT_BYTES", 1_000_000),
        sandbox_mem_limit_mb=_int_env("JARVIS_SANDBOX_MEM_LIMIT_MB", 2048),
        health_alerts_enabled=_bool_env("JARVIS_HEALTH_ALERTS_ENABLED", True),
        health_alert_gpu_temp_c=_float_env("JARVIS_HEALTH_ALERT_GPU_TEMP_C", 85.0),
        health_alert_gpu_vram_ratio=_float_env("JARVIS_HEALTH_ALERT_GPU_VRAM_RATIO", 0.97),
        health_alert_disk_ratio=_float_env("JARVIS_HEALTH_ALERT_DISK_RATIO", 0.95),
        health_alert_memory_ratio=_float_env("JARVIS_HEALTH_ALERT_MEMORY_RATIO", 0.95),
        self_healing_enabled=_bool_env("JARVIS_SELF_HEALING_ENABLED", True),
        self_healing_interval_sec=_int_env("JARVIS_SELF_HEALING_INTERVAL_SEC", 90),
        self_healing_min_failures=_int_env("JARVIS_SELF_HEALING_MIN_FAILURES", 2),
        self_healing_max_restarts=_int_env("JARVIS_SELF_HEALING_MAX_RESTARTS", 3),
        self_healing_window_sec=_int_env("JARVIS_SELF_HEALING_WINDOW_SEC", 1800),
        self_healing_grace_sec=_int_env("JARVIS_SELF_HEALING_GRACE_SEC", 300),
        mission_self_replan_enabled=_bool_env("JARVIS_MISSION_SELF_REPLAN_ENABLED", True),
        mission_self_replan_max_rounds=_int_env("JARVIS_MISSION_SELF_REPLAN_MAX_ROUNDS", 2),
        api_host=os.environ.get("JARVIS_API_HOST", "0.0.0.0"),
        api_port=_int_env("JARVIS_API_PORT", 8000),
        api_require_token_on_loopback=_bool_env("JARVIS_API_REQUIRE_TOKEN_ON_LOOPBACK", True),
        # Hybrid brain: SCAFFOLD ONLY. Off unless the owner explicitly opts in. When
        # enabled it delegates to the logged-in `claude` CLI (subscription, no key).
        hybrid_brain_enabled=_bool_env("JARVIS_ENABLE_HYBRID_BRAIN", False),
        frontier_cli_path=os.environ.get("JARVIS_FRONTIER_CLI", "claude"),
        # Owner requirement: the frontier brain is Opus 4.8 at medium effort.
        frontier_model=os.environ.get("JARVIS_FRONTIER_MODEL", "claude-opus-4-8"),
        frontier_effort=os.environ.get("JARVIS_FRONTIER_EFFORT", "medium"),
        frontier_timeout_sec=_float_env("JARVIS_FRONTIER_TIMEOUT_SEC", 180.0),
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
