"""The Qwen3.5-VL migration profile and the vLLM-arg plumbing that serves it."""

from __future__ import annotations

from jarvis_gpt.config import PROFILES, VllmExtraArgs, load_settings, profile_public_dict
from jarvis_gpt.model_catalog import PROFILE_MODEL_DIR_NAMES, ModelCatalog, _vllm_extra_args


def test_qwen_vl_profile_is_registered_and_fits_vram():
    profile = PROFILES["qwen36-vl"]
    assert profile.model_dir_name == "qwen3.6-35b-a3b-nvfp4"
    # Fully resident on the 5090 — no CPU/KV offload — with fp8 KV.
    assert profile.cpu_offload_gb == 0 and profile.kv_offloading_gb == 0
    assert profile.kv_cache_dtype == "fp8"
    assert profile.max_model_len == 32768
    # Qwen ships only a fast tokenizer.json (no merges.txt): the slow BPE cannot be
    # built, so this profile MUST serve with tokenizer-mode auto (gemma stays slow).
    assert profile.tokenizer_mode == "auto"
    # The model dir name is auto-registered so identity binding accepts it.
    assert "qwen3.6-35b-a3b-nvfp4" in PROFILE_MODEL_DIR_NAMES


def test_qwen_vl_profile_starts_without_risky_parser_flags():
    # Startup-safe defaults: no reasoning/tool parser flags (an unsupported parser makes
    # vLLM fail to start), just the bounded multimodal knobs.
    args = _vllm_extra_args(PROFILES["qwen36-vl"])
    assert "--reasoning-parser" not in args
    assert "--tool-call-parser" not in args
    assert "--skip-mm-profiling" in args
    assert "--mm-processor-cache-gb" in args


def test_qwen_vl_max_num_batched_tokens_satisfies_mamba_block_size():
    # Qwen3.5's hybrid Mamba/GDN layers force the attention block size to 2096; vLLM
    # asserts block_size <= max_num_batched_tokens, so the default 2048 crashes engine
    # init. The profile must raise it above that block size.
    profile = PROFILES["qwen36-vl"]
    assert profile.vllm_extra_args.max_num_batched_tokens is not None
    assert profile.vllm_extra_args.max_num_batched_tokens >= 2096
    assert "--max-num-batched-tokens 4096" in _vllm_extra_args(profile)


def test_vllm_extra_args_emits_advanced_flags_when_enabled():
    options = VllmExtraArgs(
        reasoning_parser="qwen3",
        tool_call_parser="hermes",
        enable_auto_tool_choice=True,
        limit_mm_per_prompt="image=2,video=1",
        trust_remote_code=True,
    )
    profile = PROFILES["qwen36-vl"]
    from dataclasses import replace

    args = _vllm_extra_args(replace(profile, vllm_extra_args=options))
    assert "--reasoning-parser qwen3" in args
    assert "--tool-call-parser hermes" in args
    assert "--enable-auto-tool-choice" in args
    assert "--limit-mm-per-prompt image=2,video=1" in args
    assert "--trust-remote-code" in args


def test_enable_auto_tool_choice_requires_a_tool_parser():
    # --enable-auto-tool-choice is meaningless without a tool parser and must not be emitted.
    options = VllmExtraArgs(enable_auto_tool_choice=True)
    profile = PROFILES["qwen36-vl"]
    from dataclasses import replace

    args = _vllm_extra_args(replace(profile, vllm_extra_args=options))
    assert "--enable-auto-tool-choice" not in args


def test_dispatcher_config_mounts_qwen_model(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_PROFILE", "qwen36-vl")
    settings = load_settings()
    cfg = ModelCatalog(settings=settings).dispatcher_config()
    assert cfg["docker_model_path"] == "/models/qwen3.6-35b-a3b-nvfp4"
    env = cfg["env"]
    assert env["JARVIS_QWEN_MODEL_PATH"] == "/models/qwen3.6-35b-a3b-nvfp4"
    assert env["JARVIS_QWEN_GPU_UTIL"] == "0.90"
    assert env["JARVIS_QWEN_MAX_LEN"] == "32768"
    assert env["JARVIS_QWEN_KV_DTYPE"] == "fp8"
    assert env["JARVIS_QWEN_TOKENIZER_MODE"] == "auto"


def test_qwen_vl_profile_selects_nvfp4_capable_vllm_image(monkeypatch, tmp_path):
    # NVFP4 needs vLLM >= 0.25 for the fast kernels; the profile drives the image so a
    # bare `--profile qwen36-vl dispatcher-up` serves on the right runtime. gemma stays
    # pinned to v0.23.0. An explicit JARVIS_VLLM_IMAGE env override still wins.
    from jarvis_gpt.dispatcher import DispatcherManager

    monkeypatch.delenv("JARVIS_VLLM_IMAGE", raising=False)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_PROFILE", "qwen36-vl")
    settings = load_settings()
    assert settings.profile.vllm_image == "vllm/vllm-openai:v0.25.1"
    env = DispatcherManager(settings, repo_root=tmp_path).compose_env()
    assert env["JARVIS_VLLM_IMAGE"] == "vllm/vllm-openai:v0.25.1"
    assert PROFILES["gemma4-turbo"].vllm_image == "vllm/vllm-openai:v0.23.0"


def test_profile_public_dict_exposes_new_vllm_fields():
    public = profile_public_dict(PROFILES["qwen36-vl"])
    extra = public["vllm_extra_args"]
    for key in (
        "reasoning_parser",
        "tool_call_parser",
        "enable_auto_tool_choice",
        "limit_mm_per_prompt",
        "trust_remote_code",
    ):
        assert key in extra
