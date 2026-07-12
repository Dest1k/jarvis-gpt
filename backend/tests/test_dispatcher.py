from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import jarvis_gpt.cli as cli
import pytest
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.dispatcher import (
    DispatcherManager,
    _runtime_from_command,
    _runtime_from_env,
    _runtime_mismatches,
)
from jarvis_gpt.models import DispatcherStatusResponse


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
    assert env["JARVIS_QWEN_GPU_UTIL"] == "0.85"
    assert env["JARVIS_QWEN_MAX_NUM_SEQS"] == "1"
    assert env["JARVIS_QWEN_ENFORCE_EAGER"] == "--enforce-eager"
    assert env["JARVIS_QWEN_CPU_OFFLOAD_ARGS"] == "--cpu-offload-gb 24"
    assert env["JARVIS_QWEN_KV_OFFLOAD_ARGS"] == (
        "--kv-offloading-size 16 --kv-offloading-backend native"
    )
    assert manager.compose_command("up")[-2:] == ["-d", "dispatcher"]
    assert status["active_model"]["id"] == "gemma4-31b-it-nvfp4"
    assert status["runtime"] is None
    assert status["desired_runtime"]["enforce_eager"] is True
    assert status["desired_runtime"]["cpu_offload_gb"] == 24
    assert status["desired_runtime"]["kv_offloading_gb"] == 16
    assert status["desired_runtime"]["kv_offloading_backend"] == "native"


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


def test_dispatcher_status_schema_preserves_runtime_diagnostics(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: None)
    settings = load_settings("gemma4-mono")
    manager = DispatcherManager(settings, repo_root=tmp_path)

    response = DispatcherStatusResponse.model_validate(manager.status()).model_dump()

    assert response["actual_image"] == ""
    assert response["desired_image"] == "vllm/vllm-openai:v0.23.0"
    assert response["runtime_matches_desired"] is False
    assert response["runtime_mismatches"]["model_id"]["desired"] == (
        "gemma4-31b-it-nvfp4"
    )


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
    assert env["JARVIS_QWEN_KV_OFFLOAD_ARGS"] == ""


def test_dispatcher_mono_perf_profile_is_gpu_first(monkeypatch, tmp_path):
    model_root = tmp_path / "models"
    (model_root / "gemma4-31b-it-nvfp4").mkdir(parents=True)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(model_root))
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: None)

    settings = load_settings("gemma4-mono-perf")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    env = manager.compose_env()
    status = manager.status()

    assert env["JARVIS_QWEN_MODEL_PATH"] == "/models/gemma4-31b-it-nvfp4"
    assert env["JARVIS_QWEN_ENFORCE_EAGER"] == "--enforce-eager"
    assert env["JARVIS_QWEN_GPU_UTIL"] == "0.92"
    assert env["JARVIS_QWEN_MAX_LEN"] == "8192"
    assert env["JARVIS_QWEN_MAX_NUM_SEQS"] == "1"
    assert env["JARVIS_QWEN_CPU_OFFLOAD_ARGS"] == "--cpu-offload-gb 12"
    assert env["JARVIS_QWEN_KV_OFFLOAD_ARGS"] == ""
    assert status["desired_runtime"]["enforce_eager"] is True
    assert status["desired_runtime"]["cpu_offload_gb"] == 12
    assert not status["desired_runtime"].get("kv_offloading_gb")
    assert not status["desired_runtime"].get("kv_offloading_backend")


def test_dispatcher_parses_actual_container_runtime_command():
    command = [
        "--model",
        "/models/gemma4-31b-it-nvfp4",
        "--served-model-name",
        "dispatcher",
        "--dtype",
        "auto",
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
        "--kv-offloading-size",
        "8",
        "--kv-offloading-backend",
        "native",
        "--tokenizer-mode",
        "slow",
        "--safetensors-load-strategy",
        "prefetch",
        "--enable-prefix-caching",
        "--host",
        "0.0.0.0",
        "--port",
        "8001",
    ]
    runtime = _runtime_from_command(command)
    runtime_from_string = _runtime_from_command([" ".join(command)])

    assert runtime["model_id"] == "gemma4-31b-it-nvfp4"
    assert runtime["served_model_name"] == "dispatcher"
    assert runtime["dtype"] == "auto"
    assert runtime["enforce_eager"] is True
    assert runtime["max_model_len"] == 32768
    assert runtime["gpu_memory_utilization"] == 0.86
    assert runtime["kv_cache_dtype"] == "fp8"
    assert runtime["max_num_seqs"] == 16
    assert runtime["cpu_offload_gb"] == 8
    assert runtime["kv_offloading_gb"] == 8
    assert runtime["kv_offloading_backend"] == "native"
    assert runtime["tokenizer_mode"] == "slow"
    assert runtime["safetensors_load_strategy"] == "prefetch"
    assert runtime["prefix_caching"] is True
    assert runtime["host"] == "0.0.0.0"
    assert runtime["port"] == 8001
    assert runtime_from_string == runtime


def test_dispatcher_verification_accepts_matching_runtime_during_warmup(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-turbo")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "port": 8001,
            "port_open": False,
            "runtime_matches_desired": True,
            "runtime_mismatches": {},
            "container_status": {"ok": True, "exists": True, "status": "Up 4 seconds"},
        },
    )

    verification = manager.verify_state(running=True, timeout_seconds=0.1)

    assert verification["ok"] is True
    assert verification["container_running"] is True
    assert verification["port_open"] is False
    assert verification["runtime_matches_desired"] is True


def test_dispatcher_compose_success_is_rejected_when_runtime_disagrees(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-turbo")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "run_compose",
        lambda _action: {"ok": True, "summary": "compose returned zero"},
    )
    monkeypatch.setattr(
        manager,
        "_replace_mismatched_container",
        lambda: {
            "required": False,
            "ok": True,
            "removed": False,
            "reused": False,
        },
    )
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "port": 8001,
            "port_open": True,
            "runtime_matches_desired": False,
            "runtime_mismatches": {
                "model_id": {"actual": "gemma4-26b-a4b-nvfp4", "desired": "gemma4-31b-it-nvfp4"}
            },
            "container_status": {"ok": True, "exists": True, "status": "Up 4 seconds"},
        },
    )
    ticks = iter((0.0, 1.0))
    monkeypatch.setattr("jarvis_gpt.dispatcher.time.monotonic", lambda: next(ticks))

    result = manager.run_compose_verified("up", timeout_seconds=0.1)

    assert result["ok"] is False
    assert result["verification"]["container_running"] is True
    assert result["verification"]["port_open"] is True
    assert result["verification"]["runtime_matches_desired"] is False


def test_runtime_match_requires_model_and_every_profile_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-mono")
    manager = DispatcherManager(settings, repo_root=tmp_path)
    desired = _runtime_from_env(manager.compose_env())
    mono_perf = _runtime_from_command(
        [
            "--model",
            "/models/gemma4-31b-it-nvfp4",
            "--served-model-name",
            "dispatcher",
            "--dtype",
            "auto",
            "--enforce-eager",
            "--max-model-len",
            "8192",
            "--gpu-memory-utilization",
            "0.92",
            "--kv-cache-dtype",
            "fp8",
            "--max-num-seqs",
            "1",
            "--cpu-offload-gb",
            "12",
            "--tokenizer-mode",
            "slow",
            "--safetensors-load-strategy",
            "prefetch",
            "--enable-prefix-caching",
            "--host",
            "0.0.0.0",
            "--port",
            "8001",
        ]
    )

    mismatches = _runtime_mismatches(mono_perf, desired)

    assert "model_id" not in mismatches
    assert set(mismatches) == {
        "max_model_len",
        "gpu_memory_utilization",
        "cpu_offload_gb",
        "kv_offloading_gb",
        "kv_offloading_backend",
    }


@pytest.mark.parametrize(
    ("image", "matches"),
    [
        ("vllm/vllm-openai:v0.23.0", True),
        ("vllm/vllm-openai:nightly", False),
    ],
)
def test_dispatcher_reuse_requires_configured_image(
    monkeypatch,
    tmp_path,
    image,
    matches,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-mono")
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {
            "ok": True,
            "exists": True,
            "status": "Up 4 seconds",
            "image": image,
            "command": [
                "--model",
                "/models/gemma4-31b-it-nvfp4",
                "--served-model-name",
                "dispatcher",
                "--dtype",
                "auto",
                "--enforce-eager",
                "--max-model-len",
                "16384",
                "--gpu-memory-utilization",
                "0.85",
                "--kv-cache-dtype",
                "fp8",
                "--max-num-seqs",
                "1",
                "--cpu-offload-gb",
                "24",
                "--kv-offloading-size",
                "16",
                "--kv-offloading-backend",
                "native",
                "--tokenizer-mode",
                "slow",
                "--safetensors-load-strategy",
                "prefetch",
                "--enable-prefix-caching",
                "--host",
                "0.0.0.0",
                "--port",
                "8001",
            ],
        },
    )

    status = manager.status()

    assert status["runtime_matches_desired"] is matches
    assert ("image" in status["runtime_mismatches"]) is not matches


def test_mismatched_dispatcher_container_is_removed_before_start(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-mono")
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "container_status": {"ok": True, "exists": True, "status": "Up"},
            "runtime_matches_desired": False,
            "runtime_mismatches": {
                "model_id": {"actual": "gemma4-26b-a4b-nvfp4", "desired": "gemma4-31b-it-nvfp4"}
            },
        },
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(returncode=0, stdout="jarvis-gpt-dispatcher", stderr="")

    monkeypatch.setattr("jarvis_gpt.dispatcher.subprocess.run", fake_run)

    replacement = manager._replace_mismatched_container()

    assert replacement["ok"] is True
    assert replacement["removed"] is True
    assert captured["command"][-3:] == ["rm", "-f", "jarvis-gpt-dispatcher"]


def test_matching_dispatcher_container_is_reused_without_removal(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-mono")
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "container_status": {"ok": True, "exists": True, "status": "Up"},
            "runtime_matches_desired": True,
            "runtime_mismatches": {},
        },
    )

    replacement = manager._replace_mismatched_container()

    assert replacement == {
        "required": False,
        "ok": True,
        "removed": False,
        "reused": True,
    }


def test_verified_start_skips_compose_for_exact_running_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-mono")
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "_replace_mismatched_container",
        lambda: {
            "required": False,
            "ok": True,
            "removed": False,
            "reused": True,
        },
    )
    monkeypatch.setattr(
        manager,
        "run_compose",
        lambda _action: pytest.fail("compose must not run for an exact runtime"),
    )
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "port": 8001,
            "port_open": False,
            "runtime_matches_desired": True,
            "runtime_mismatches": {},
            "container_status": {"ok": True, "exists": True, "status": "Up"},
        },
    )

    result = manager.run_compose_verified("up", timeout_seconds=0.1)

    assert result["ok"] is True
    assert result["replacement"]["reused"] is True
    assert result["command"] == []


def test_dispatcher_stop_rejects_unknown_docker_state(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-turbo")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "port": 8001,
            "port_open": False,
            "container_status": {"ok": False, "error": "docker unavailable"},
        },
    )

    verification = manager.verify_state(running=False, timeout_seconds=0.1)

    assert verification["ok"] is False
    assert verification["container_known"] is False


@pytest.mark.parametrize(
    ("command_name", "action"),
    [("cmd_dispatcher_up", "up"), ("cmd_dispatcher_down", "down")],
)
def test_dispatcher_cli_exits_nonzero_when_verified_action_fails(
    monkeypatch,
    command_name,
    action,
):
    @contextmanager
    def fake_runtime(_profile):
        yield (SimpleNamespace(), None, None, None)

    class FakeDispatcherManager:
        def __init__(self, _settings, *, storage):
            assert storage is None
            pass

        def run_compose_verified(self, requested_action):
            assert requested_action == action
            return {"ok": False, "summary": "verification failed"}

    monkeypatch.setattr(cli, "_primary_runtime", fake_runtime)
    monkeypatch.setattr(cli, "DispatcherManager", FakeDispatcherManager)

    with pytest.raises(SystemExit) as exc_info:
        getattr(cli, command_name)(SimpleNamespace(profile="gemma4-mono"))

    assert exc_info.value.code == 1
