from __future__ import annotations

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.dispatcher import DispatcherManager, _runtime_from_command


def test_dispatcher_manager_builds_compose_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_VLLM_IMAGE", raising=False)
    model_root = tmp_path / "models"
    (model_root / "gemma4-31b-it-nvfp4").mkdir(parents=True)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(model_root))
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: None)

    settings = load_settings("gemma4-mono")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    env = manager.compose_env()
    status = manager.status()

    assert env["JARVIS_QWEN_MODEL_PATH"] == "/models/gemma4-31b-it-nvfp4"
    assert env["JARVIS_QWEN_MODEL_NAME"] == "dispatcher"
    assert env["VLLM_USE_V2_MODEL_RUNNER"] == "0"
    assert env["JARVIS_VLLM_IMAGE"] == "vllm/vllm-openai:v0.23.0"
    assert env["VLLM_WEIGHT_OFFLOADING_DISABLE_UVA"] == "1"
    assert env["JARVIS_QWEN_TOKENIZER_MODE"] == "slow"
    assert env["JARVIS_QWEN_SAFETENSORS_LOAD_STRATEGY"] == "prefetch"
    assert env["JARVIS_QWEN_MAX_LEN"] == "16384"
    assert env["JARVIS_QWEN_GPU_UTIL"] == "0.94"
    assert env["JARVIS_QWEN_MAX_NUM_SEQS"] == "4"
    assert env["JARVIS_QWEN_ENFORCE_EAGER"] == "--enforce-eager"
    assert env["JARVIS_QWEN_CPU_OFFLOAD_ARGS"] == "--cpu-offload-gb 8"
    assert env["JARVIS_QWEN_SWAP_SPACE_ARGS"] == "--swap-space 8"
    assert manager.compose_command("up")[-2:] == ["-d", "dispatcher"]
    assert status["active_model"]["id"] == "gemma4-31b-it-nvfp4"
    assert status["runtime"] is None
    assert status["desired_runtime"]["enforce_eager"] is True
    assert status["desired_runtime"]["cpu_offload_gb"] == 8


def test_dispatcher_status_redacts_hugging_face_token(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.setenv("HF_TOKEN", "hf_super_secret")
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: None)

    settings = load_settings("gemma4-mono")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)

    assert manager.compose_env()["HF_TOKEN"] == "hf_super_secret"
    assert manager.status()["env"]["HF_TOKEN"] == "[configured]"


def test_dispatcher_turbo_profile_keeps_cuda_graph_path(monkeypatch, tmp_path):
    model_root = tmp_path / "models"
    (model_root / "gemma4-26b-a4b-nvfp4").mkdir(parents=True)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(model_root))
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: None)

    settings = load_settings("gemma4-turbo")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    env = manager.compose_env()

    assert env["JARVIS_QWEN_MODEL_PATH"] == "/models/gemma4-26b-a4b-nvfp4"
    assert env["JARVIS_QWEN_ENFORCE_EAGER"] == ""
    assert env["JARVIS_QWEN_GPU_UTIL"] == "0.82"
    assert env["JARVIS_QWEN_MAX_LEN"] == "32768"
    assert env["JARVIS_QWEN_CPU_OFFLOAD_ARGS"] == ""
    assert env["JARVIS_QWEN_SWAP_SPACE_ARGS"] == ""


def test_dispatcher_parses_actual_container_runtime_command():
    command = [
        "--model",
        "/models/gemma4-31b-it-nvfp4",
        "--served-model-name",
        "dispatcher",
        "--enforce-eager",
        "--max-model-len",
        "32768",
        "--gpu-memory-utilization",
        "0.86",
        "--kv-cache-dtype",
        "fp8",
        "--max-num-seqs",
        "16",
        "--cpu-offload-gb",
        "8",
        "--swap-space",
        "8",
    ]
    runtime = _runtime_from_command(command)
    runtime_from_string = _runtime_from_command([" ".join(command)])

    assert runtime["model_id"] == "gemma4-31b-it-nvfp4"
    assert runtime["served_model_name"] == "dispatcher"
    assert runtime["enforce_eager"] is True
    assert runtime["max_model_len"] == 32768
    assert runtime["gpu_memory_utilization"] == 0.86
    assert runtime["kv_cache_dtype"] == "fp8"
    assert runtime["max_num_seqs"] == 16
    assert runtime["cpu_offload_gb"] == 8
    assert runtime["swap_space_gb"] == 8
    assert runtime_from_string == runtime
