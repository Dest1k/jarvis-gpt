from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import jarvis_gpt.cli as cli
import pytest
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.dispatcher import (
    DispatcherManager,
    _dispatcher_operation_lock,
    _launcher_state_lock,
    _runtime_from_command,
    _runtime_from_env,
    _runtime_mismatches,
)
from jarvis_gpt.models import DispatcherStatusResponse

OLD_CONTAINER_ID = "1" * 64
NEW_CONTAINER_ID = "2" * 64
FOREIGN_CONTAINER_ID = "3" * 64
OPERATION_NONCE = "a" * 32


def _write_launcher_owned_state(
    settings,
    container_id: str,
    operation_nonce: str = OPERATION_NONCE,
) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    (settings.state_dir / "launcher-state.json").write_text(
        json.dumps(
            {
                "profile": "qwen36-vl",
                "services": {
                    "dispatcher": {
                        "profile": "qwen36-vl",
                        "started_by_launcher": True,
                        "container_id": container_id,
                        "operation_nonce": operation_nonce,
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_dispatcher_operation_lock_rejects_concurrent_mutator(tmp_path):
    lock_path = tmp_path / "dispatcher-operation.lock"

    with (
        _dispatcher_operation_lock(lock_path, timeout_seconds=1.0),
        pytest.raises(TimeoutError),
        _dispatcher_operation_lock(lock_path, timeout_seconds=0.05),
    ):
        pytest.fail("concurrent mutator acquired the dispatcher lock")


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


def test_container_status_exposes_docker_health_start_time_and_full_id(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    manager = DispatcherManager(settings, repo_root=tmp_path)
    full_id = "a" * 64
    image_id = "sha256:" + ("b" * 64)
    started_at = "2026-07-19T05:41:04.123456789Z"

    def fake_run(command, **_kwargs):
        if command[1] == "ps":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "jarvis-gpt-dispatcher\tUp 2 minutes (health: starting)\t"
                    "127.0.0.1:8001->8001/tcp\tjarvis/vllm-openai:test\n"
                ),
                stderr="",
            )
        if ".State" in command[-1]:
            state = {"StartedAt": started_at, "Health": {"Status": "starting"}}
            labels = {
                "com.jarvis-gpt.dispatcher.operation-nonce": OPERATION_NONCE
            }
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"{full_id}\t{image_id}\t{json.dumps(state)}\t"
                    f"{json.dumps(labels)}\n"
                ),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout='["--model","/models/qwen3.6-35b-a3b-nvfp4"]\n',
            stderr="",
        )

    monkeypatch.setattr("jarvis_gpt.dispatcher.subprocess.run", fake_run)

    container = manager._container_status("docker.exe")

    assert container is not None
    assert container["inspect_ok"] is True
    assert container["id"] == full_id
    assert container["image_id"] == image_id
    assert container["health"] == "starting"
    assert container["started_at"] == started_at
    assert container["operation_nonce"] == OPERATION_NONCE


def test_self_heal_recreate_synchronizes_launcher_container_id(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    old_id = "1" * 64
    new_id = "2" * 64
    state_path = settings.state_dir / "launcher-state.json"
    launcher_state = {
        "services": {
            "dispatcher": {
                "profile": "qwen36-vl",
                "started_by_launcher": True,
                "container_id": old_id,
                "operation_nonce": OPERATION_NONCE,
                "phase": "warming",
            },
            "backend": {"pid": 1234},
        }
    }
    state_path.write_bytes(
        b"\xef\xbb\xbf" + json.dumps(launcher_state, ensure_ascii=False).encode("utf-8")
    )
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "runtime_matches_desired": True,
            "container_status": {
                "ok": True,
                "exists": True,
                "status": "Up 3 seconds (health: starting)",
                "id": new_id,
                "operation_nonce": OPERATION_NONCE,
            },
        },
    )

    result = manager.sync_launcher_state_container_id(
        expected_previous_id=old_id,
        expected_previous_operation_nonce=OPERATION_NONCE,
        expected_current_id=new_id,
        operation_nonce=OPERATION_NONCE,
    )

    assert result == {
        "ok": True,
        "updated": True,
        "reason": "docker-id-synchronized",
        "previous_id": old_id,
        "container_id": new_id,
    }
    updated_bytes = state_path.read_bytes()
    assert updated_bytes.startswith(b"\xef\xbb\xbf")
    updated = json.loads(updated_bytes.decode("utf-8-sig"))
    assert updated["services"]["dispatcher"]["container_id"] == new_id
    assert updated["services"]["dispatcher"]["started_by_launcher"] is True
    assert updated["services"]["backend"] == {"pid": 1234}


def test_launcher_state_sync_fails_closed_while_launcher_writer_holds_lock(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    old_id = "1" * 64
    new_id = "2" * 64
    state_path = settings.state_dir / "launcher-state.json"
    state_path.write_text(
        json.dumps(
            {
                "services": {
                    "dispatcher": {
                        "profile": "qwen36-vl",
                        "started_by_launcher": True,
                        "container_id": old_id,
                        "operation_nonce": OPERATION_NONCE,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "container_status": {
                "ok": True,
                "exists": True,
                    "status": "Up 3 seconds (health: starting)",
                    "id": new_id,
                    "operation_nonce": OPERATION_NONCE,
                }
            },
    )
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.LAUNCHER_STATE_LOCK_TIMEOUT_SECONDS",
        0.05,
    )

    lock_path = state_path.with_name("launcher-state.lock")
    with _launcher_state_lock(lock_path, timeout_seconds=1.0):
        result = manager.sync_launcher_state_container_id(
            expected_previous_id=old_id,
            expected_previous_operation_nonce=OPERATION_NONCE,
            expected_current_id=new_id,
            operation_nonce=OPERATION_NONCE,
        )

    assert result == {
        "ok": False,
        "updated": False,
        "reason": "launcher-state-lock-timeout",
        "container_id": new_id,
    }
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["services"]["dispatcher"]["container_id"] == old_id


def test_launcher_state_sync_rechecks_docker_id_after_lock_acquisition(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    recorded_id = "1" * 64
    observed_before_lock = "2" * 64
    observed_under_lock = "3" * 64
    state_path = settings.state_dir / "launcher-state.json"
    state_path.write_text(
        json.dumps(
            {
                "services": {
                    "dispatcher": {
                        "profile": "qwen36-vl",
                        "started_by_launcher": True,
                        "container_id": recorded_id,
                        "operation_nonce": OPERATION_NONCE,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    observed_ids = iter((observed_before_lock, observed_under_lock))

    def status():
        return {
            "container_status": {
                "ok": True,
                "exists": True,
                "status": "Up 3 seconds (health: starting)",
                "id": next(observed_ids),
                "operation_nonce": OPERATION_NONCE,
            }
        }

    monkeypatch.setattr(manager, "status", status)

    result = manager.sync_launcher_state_container_id(
        expected_previous_id=recorded_id,
        expected_previous_operation_nonce=OPERATION_NONCE,
        expected_current_id=observed_before_lock,
        operation_nonce=OPERATION_NONCE,
    )

    assert result["ok"] is False
    assert result["reason"] == "docker-container-id-changed-or-unavailable"
    assert result["container_id"] == observed_under_lock
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["services"]["dispatcher"]["container_id"] == recorded_id


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
    assert env["JARVIS_QWEN_GPU_UTIL"] == "0.93"
    assert env["JARVIS_QWEN_MAX_LEN"] == "4096"
    assert env["JARVIS_QWEN_MAX_NUM_SEQS"] == "1"
    assert env["JARVIS_QWEN_CPU_OFFLOAD_ARGS"] == "--cpu-offload-gb 2.5"
    assert env["JARVIS_QWEN_KV_OFFLOAD_ARGS"] == ""
    assert env["JARVIS_QWEN_EXTRA_ARGS"] == (
        "--language-model-only --skip-mm-profiling --mm-processor-cache-gb 0 "
        "--max-num-batched-tokens 512"
    )
    assert status["desired_runtime"]["enforce_eager"] is True
    assert status["desired_runtime"]["cpu_offload_gb"] == 2.5
    assert status["desired_runtime"]["language_model_only"] is True
    assert status["desired_runtime"]["skip_mm_profiling"] is True
    assert status["desired_runtime"]["mm_processor_cache_gb"] == 0
    assert status["desired_runtime"]["max_num_batched_tokens"] == 512
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
        "2.5",
        "--kv-offloading-size",
        "8",
        "--kv-offloading-backend",
        "native",
        "--tokenizer-mode",
        "slow",
        "--safetensors-load-strategy",
        "prefetch",
        "--enable-prefix-caching",
        "--language-model-only",
        "--skip-mm-profiling",
        "--mm-processor-cache-gb",
        "0",
        "--max-num-batched-tokens",
        "512",
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
    assert runtime["cpu_offload_gb"] == 2.5
    assert runtime["kv_offloading_gb"] == 8
    assert runtime["kv_offloading_backend"] == "native"
    assert runtime["tokenizer_mode"] == "slow"
    assert runtime["safetensors_load_strategy"] == "prefetch"
    assert runtime["prefix_caching"] is True
    assert runtime["language_model_only"] is True
    assert runtime["skip_mm_profiling"] is True
    assert runtime["mm_processor_cache_gb"] == 0
    assert runtime["max_num_batched_tokens"] == 512
    assert runtime["host"] == "0.0.0.0"
    assert runtime["port"] == 8001
    assert runtime_from_string == runtime


def test_dispatcher_verification_rejects_matching_runtime_during_warmup(
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

    assert verification["ok"] is False
    assert verification["container_running"] is True
    assert verification["port_open"] is False
    assert verification["runtime_matches_desired"] is True
    assert verification["live_completion"]["skipped"] is True


def test_dispatcher_verification_requires_exact_live_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "port": 8001,
            "port_open": True,
            "runtime_matches_desired": True,
            "runtime_mismatches": {},
            "container_status": {"ok": True, "exists": True, "status": "Up"},
        },
    )
    monkeypatch.setattr(
        manager,
        "_live_completion_probe",
        lambda **_kwargs: {
            "ok": False,
            "terminal": True,
            "error": "unexpected answer",
        },
    )

    verification = manager.verify_state(running=True, timeout_seconds=30)

    assert verification["ok"] is False
    assert verification["container_running"] is True
    assert verification["runtime_matches_desired"] is True
    assert verification["live_completion"]["terminal"] is True


def test_live_completion_probe_accepts_only_exact_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)
    responses = iter(
        (
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": " 4\n"}}]},
                request=httpx.Request("POST", "http://127.0.0.1:8001/v1/chat/completions"),
            ),
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": "5"}}]},
                request=httpx.Request("POST", "http://127.0.0.1:8001/v1/chat/completions"),
            ),
        )
    )
    payloads = []

    class FakeClient:
        def __init__(self, *, trust_env):
            assert trust_env is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, *, json, timeout):
            payloads.append((url, json, timeout))
            return next(responses)

    monkeypatch.setattr("jarvis_gpt.dispatcher.httpx.Client", FakeClient)

    accepted = manager._live_completion_probe(timeout_seconds=5)
    rejected = manager._live_completion_probe(timeout_seconds=5)

    assert accepted["ok"] is True
    assert rejected["ok"] is False
    assert rejected["terminal"] is True
    assert payloads[0][1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert payloads[0][1]["messages"][0]["content"].endswith("2+2?")


@pytest.mark.parametrize("status_code", [408, 425, 429])
def test_live_completion_probe_retries_transient_http_status(
    monkeypatch,
    tmp_path,
    status_code,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)

    class FakeClient:
        def __init__(self, *, trust_env):
            assert trust_env is False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return httpx.Response(status_code)

    monkeypatch.setattr("jarvis_gpt.dispatcher.httpx.Client", FakeClient)

    result = manager._live_completion_probe(timeout_seconds=1)

    assert result["ok"] is False
    assert result["terminal"] is False


def test_dispatcher_compose_success_is_rejected_when_runtime_disagrees(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-turbo")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "_run_compose_with_env",
        lambda _action, _env: {"ok": True, "summary": "compose returned zero"},
    )
    monkeypatch.setattr(
        manager,
        "_ensure_desired_image_available",
        lambda *_args: {"ok": True, "image_id": "sha256:test"},
    )
    monkeypatch.setattr(
        manager,
        "_record_candidate_container_id",
        lambda candidate, _nonce: candidate.setdefault(
            "candidate_container_id", NEW_CONTAINER_ID
        ),
    )
    monkeypatch.setattr(manager, "_candidate_matches_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        manager,
        "_replace_mismatched_container",
        lambda **_kwargs: {
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
            "container_status": {
                "ok": True,
                "exists": True,
                "status": "Up 4 seconds",
                "id": OLD_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
            },
        },
    )
    clock = {"now": 0.0}

    def _monotonic() -> float:
        clock["now"] += 0.05
        return clock["now"]

    monkeypatch.setattr("jarvis_gpt.dispatcher.time.monotonic", _monotonic)
    monkeypatch.setattr("jarvis_gpt.dispatcher.time.sleep", lambda _seconds: None)

    result = manager.run_compose_verified("up", timeout_seconds=0.1)

    assert result["ok"] is False
    verification = result["verification"]
    assert verification.get("ok") is False
    assert verification.get("runtime_matches_desired") is False


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


def test_runtime_match_rejects_stale_mono_perf_without_text_only_flag(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-mono-perf")
    manager = DispatcherManager(settings, repo_root=tmp_path)
    desired = _runtime_from_env(manager.compose_env())
    stale = dict(desired)
    stale["skip_mm_profiling"] = False

    assert _runtime_mismatches(stale, desired) == {
        "skip_mm_profiling": {"actual": False, "desired": True}
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
    image_id = "sha256:" + ("d" * 64)
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {
            "ok": True,
            "exists": True,
            "status": "Up 4 seconds",
            "image": image,
            "image_id": image_id,
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
    monkeypatch.setattr(
        manager,
        "_inspect_local_image",
        lambda _docker, _image: {"ok": True, "image_id": image_id},
    )

    status = manager.status()

    assert status["runtime_matches_desired"] is matches
    assert ("image" in status["runtime_mismatches"]) is not matches


def test_dispatcher_reuse_rejects_rebuilt_image_under_same_tag(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-mono")
    manager = DispatcherManager(settings, repo_root=tmp_path)
    desired_image = manager.compose_env()["JARVIS_VLLM_IMAGE"]
    desired_runtime = _runtime_from_env(manager.compose_env())
    command = [
        "--model",
        str(desired_runtime["model_path"]),
        "--served-model-name",
        str(desired_runtime["served_model_name"]),
        "--dtype",
        str(desired_runtime["dtype"]),
        "--enforce-eager",
        "--max-model-len",
        str(desired_runtime["max_model_len"]),
        "--gpu-memory-utilization",
        str(desired_runtime["gpu_memory_utilization"]),
        "--kv-cache-dtype",
        str(desired_runtime["kv_cache_dtype"]),
        "--max-num-seqs",
        str(desired_runtime["max_num_seqs"]),
        "--cpu-offload-gb",
        str(desired_runtime["cpu_offload_gb"]),
        "--kv-offloading-size",
        str(desired_runtime["kv_offloading_gb"]),
        "--kv-offloading-backend",
        str(desired_runtime["kv_offloading_backend"]),
        "--tokenizer-mode",
        str(desired_runtime["tokenizer_mode"]),
        "--safetensors-load-strategy",
        str(desired_runtime["safetensors_load_strategy"]),
        "--enable-prefix-caching",
        "--host",
        "0.0.0.0",
        "--port",
        "8001",
    ]
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {
            "ok": True,
            "exists": True,
            "status": "Up 1 hour",
            "image": desired_image,
            "image_id": "sha256:old",
            "command": command,
        },
    )
    monkeypatch.setattr(
        manager,
        "_inspect_local_image",
        lambda _docker, _image: {"ok": True, "image_id": "sha256:rebuilt"},
    )

    status = manager.status()

    assert status["runtime_matches_desired"] is False
    assert status["runtime_mismatches"]["image_id"] == {
        "actual": "sha256:old",
        "desired": "sha256:rebuilt",
    }


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
    monkeypatch.setattr(
        manager,
        "_cutover_preflight",
        lambda _snapshot, _docker: {
            "ok": True,
            "rollback_image_tag": "rollback:test",
            "previous_container_id": OLD_CONTAINER_ID,
        },
    )
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {
            "ok": True,
            "exists": True,
            "id": OLD_CONTAINER_ID,
        },
    )
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="jarvis-gpt-dispatcher", stderr="")

    monkeypatch.setattr("jarvis_gpt.dispatcher.subprocess.run", fake_run)

    replacement = manager._replace_mismatched_container()

    assert replacement["ok"] is True
    assert replacement["removed"] is True
    assert commands == [["docker.exe", "rm", "-f", OLD_CONTAINER_ID]]


def test_mismatched_dispatcher_pulls_missing_image_before_removal(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-mono")
    manager = DispatcherManager(settings, repo_root=tmp_path)
    desired_image = manager.compose_env()["JARVIS_VLLM_IMAGE"]
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "container_status": {"ok": True, "exists": True, "status": "Up"},
            "runtime_matches_desired": False,
            "runtime_mismatches": {"image": {"actual": "old", "desired": desired_image}},
            "desired_image": desired_image,
        },
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[1] == "pull":
            return SimpleNamespace(returncode=0, stdout="pulled", stderr="")
        if len(commands) == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")
        return SimpleNamespace(
            returncode=0,
            stdout="sha256:downloaded\n",
            stderr="",
        )

    monkeypatch.setattr("jarvis_gpt.dispatcher.subprocess.run", fake_run)

    replacement = manager._ensure_desired_image_available("docker.exe", desired_image)

    assert replacement["ok"] is True
    assert replacement["pulled"] is True
    assert commands == [
        ["docker.exe", "image", "inspect", "--format", "{{.Id}}", desired_image],
        ["docker.exe", "pull", desired_image],
        ["docker.exe", "image", "inspect", "--format", "{{.Id}}", desired_image],
    ]


def test_mismatched_dispatcher_keeps_old_container_when_image_pull_fails(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-mono")
    manager = DispatcherManager(settings, repo_root=tmp_path)
    desired_image = manager.compose_env()["JARVIS_VLLM_IMAGE"]
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "container_status": {"ok": True, "exists": True, "status": "Up"},
            "runtime_matches_desired": False,
            "runtime_mismatches": {"image": {"actual": "old", "desired": desired_image}},
            "desired_image": desired_image,
        },
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=1, stdout="", stderr="registry unavailable")

    monkeypatch.setattr("jarvis_gpt.dispatcher.subprocess.run", fake_run)

    replacement = manager._ensure_desired_image_available("docker.exe", desired_image)

    assert replacement["ok"] is False
    assert replacement["available"] is False
    assert commands == [
        ["docker.exe", "image", "inspect", "--format", "{{.Id}}", desired_image],
        ["docker.exe", "pull", desired_image],
    ]


def test_missing_local_qwen_derivative_is_built_without_registry_pull(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)
    desired_image = manager.compose_env()["JARVIS_VLLM_IMAGE"]
    dockerfile = tmp_path / "docker" / "vllm-asyncio" / "Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM scratch\n", encoding="utf-8")
    commands = []
    desired_inspects = 0

    def fake_run(command, **_kwargs):
        nonlocal desired_inspects
        commands.append(command)
        if command[1:3] == ["image", "inspect"] and command[-1] == desired_image:
            desired_inspects += 1
            if desired_inspects == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="missing")
            return SimpleNamespace(returncode=0, stdout="sha256:derived\n", stderr="")
        if command[1:3] == ["image", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="sha256:base\n", stderr="")
        if command[1] == "build":
            return SimpleNamespace(returncode=0, stdout="built", stderr="")
        raise AssertionError(command)

    monkeypatch.setattr("jarvis_gpt.dispatcher.subprocess.run", fake_run)

    result = manager._ensure_desired_image_available("docker.exe", desired_image)

    assert result["ok"] is True
    assert result["built"] is True
    assert result["pulled"] is False
    assert not any(command[1] == "pull" for command in commands)
    assert commands[-2] == [
        "docker.exe",
        "build",
        "--pull=false",
        "-f",
        str(dockerfile),
        "-t",
        desired_image,
        str(tmp_path),
    ]


def test_cold_dispatcher_start_preflights_image_before_compose(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "_replace_mismatched_container",
        lambda: {"required": False, "ok": True, "removed": False, "reused": False},
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    monkeypatch.setattr(
        manager,
        "_ensure_desired_image_available",
        lambda _docker, _image: {
            "ok": False,
            "returncode": None,
            "stderr": "local derivative and base are unavailable",
            "command": ["docker.exe", "build"],
        },
    )
    monkeypatch.setattr(
        manager,
        "run_compose",
        lambda _action: pytest.fail("compose must not run before image preflight"),
    )

    result = manager.run_compose_verified("up")

    assert result["ok"] is False
    assert result["verification"]["skipped"] is True
    assert "startup was not attempted" in result["summary"]


def test_model_artifact_validation_rejects_missing_indexed_shard(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)
    model_root = tmp_path / "models"
    model = model_root / "indexed-model"
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "model-00001-of-00002.safetensors").write_bytes(b"first shard")
    (model / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "weight_map": {
                    "layer.0": "model-00001-of-00002.safetensors",
                    "layer.1": "model-00002-of-00002.safetensors",
                }
            }
        ),
        encoding="utf-8",
    )

    missing = manager._validate_model_artifacts(
        str(model_root), "/models/indexed-model"
    )
    (model / "model-00002-of-00002.safetensors").write_bytes(b"second shard")
    complete = manager._validate_model_artifacts(
        str(model_root), "/models/indexed-model"
    )

    assert missing["ok"] is False
    assert "model-00002-of-00002.safetensors" in missing["summary"]
    assert complete["ok"] is True
    assert complete["weight_files"] == 2


def test_dispatcher_cutover_preflight_failure_never_removes_old_container(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("gemma4-mono"), repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "container_status": {"ok": True, "exists": True, "status": "Up"},
            "runtime_matches_desired": False,
            "runtime_mismatches": {"model_id": {"actual": "old", "desired": "new"}},
        },
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    monkeypatch.setattr(
        manager,
        "_cutover_preflight",
        lambda _snapshot, _docker: {"ok": False, "summary": "rollback not provable"},
    )
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("old container must not be touched"),
    )

    replacement = manager._replace_mismatched_container()

    assert replacement["ok"] is False
    assert replacement["removed"] is False
    assert replacement["preflight"]["summary"] == "rollback not provable"


def test_dispatcher_cutover_refuses_changed_container_after_preflight(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("gemma4-mono"), repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "container_status": {"ok": True, "exists": True, "status": "Up"},
            "runtime_matches_desired": False,
            "runtime_mismatches": {"model_id": {"actual": "old", "desired": "new"}},
        },
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    monkeypatch.setattr(
        manager,
        "_cutover_preflight",
        lambda _snapshot, _docker: {
            "ok": True,
            "rollback_image_tag": "rollback:test",
            "previous_container_id": OLD_CONTAINER_ID,
        },
    )
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {"ok": True, "exists": True, "id": NEW_CONTAINER_ID},
    )
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("replacement container must never be removed"),
    )

    replacement = manager._replace_mismatched_container()

    assert replacement["ok"] is False
    assert replacement["cutover_started"] is False
    assert replacement["expected_container_id"] == OLD_CONTAINER_ID
    assert replacement["current_container_id"] == NEW_CONTAINER_ID


def test_dispatcher_rollback_refuses_foreign_replacement(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("gemma4-mono"), repo_root=tmp_path)
    replacement = {
        "candidate_container_id": NEW_CONTAINER_ID,
        "candidate_operation_nonce": OPERATION_NONCE,
        "preflight": {
            "_rollback_env": {"JARVIS_VLLM_IMAGE": "rollback:test"},
            "previous_runtime": {"model_id": "previous"},
            "previous_image_id": "sha256:previous",
        },
    }
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {
            "ok": True,
            "exists": True,
            "id": FOREIGN_CONTAINER_ID,
            "operation_nonce": "b" * 32,
        },
    )
    monkeypatch.setattr(
        manager,
        "_run_compose_with_env",
        lambda *_args, **_kwargs: pytest.fail("foreign replacement must be preserved"),
    )
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("foreign replacement must not be removed"),
    )

    rollback = manager._rollback_replacement(replacement, timeout_seconds=30)

    assert rollback["ok"] is False
    assert rollback["candidate_container_id"] == NEW_CONTAINER_ID
    assert rollback["current_container_id"] == FOREIGN_CONTAINER_ID


def test_dispatcher_candidate_requires_matching_unique_nonce(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)
    replacement = {
        "preflight": {"previous_container_id": OLD_CONTAINER_ID},
    }
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {
            "ok": True,
            "exists": True,
            "id": NEW_CONTAINER_ID,
            "operation_nonce": "b" * 32,
        },
    )

    candidate_id = manager._record_candidate_container_id(
        replacement,
        OPERATION_NONCE,
    )

    assert candidate_id == ""
    assert replacement["candidate_provenance"]["ok"] is False
    assert "candidate_container_id" not in replacement


def test_dispatcher_stop_rejects_short_expected_id_without_docker_mutation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {"ok": True, "exists": True, "id": OLD_CONTAINER_ID},
    )
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("short ID must never reach docker rm"),
    )

    result = manager.run_compose_verified(
        "down",
        expected_container_id=OLD_CONTAINER_ID[:12],
    )

    assert result["ok"] is False
    assert result["command"] == []


def test_dispatcher_down_snapshots_full_id_and_removes_exact_id(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    states = iter(
        (
            {
                "ok": True,
                "exists": True,
                "id": OLD_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
            },
            {"ok": True, "exists": False},
        )
    )
    monkeypatch.setattr(manager, "_container_status", lambda _docker: next(states))
    commands = []
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda command, **_kwargs: (
            commands.append(command)
            or SimpleNamespace(returncode=0, stdout=OLD_CONTAINER_ID, stderr="")
        ),
    )

    result = manager.run_compose_verified("down")

    assert result["ok"] is True
    assert commands == [["docker.exe", "rm", "-f", OLD_CONTAINER_ID]]
    assert "compose" not in commands[0]


def test_dispatcher_down_retains_launcher_journal_until_absence_is_proven(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    _write_launcher_owned_state(settings, OLD_CONTAINER_ID)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {
            "ok": True,
            "exists": True,
            "id": OLD_CONTAINER_ID,
            "operation_nonce": OPERATION_NONCE,
        },
    )
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="synthetic rm failure",
        ),
    )

    result = manager.run_compose_verified("down", expected_container_id=OLD_CONTAINER_ID)

    assert result["ok"] is False
    assert result["ownership_journal"]["reason"] == "stop-unconfirmed-journal-retained"
    journal = json.loads(
        (settings.state_dir / "dispatcher-ownership-journal.json").read_text(
            encoding="utf-8"
        )
    )
    assert journal["phase"] == "stop-intent"
    assert journal["container_id"] == OLD_CONTAINER_ID
    assert journal["operation_nonce"] == OPERATION_NONCE


def test_dispatcher_mutation_refuses_invalid_ownership_journal(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    (settings.state_dir / "dispatcher-ownership-journal.json").write_text(
        "{truncated",
        encoding="utf-8",
    )
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: pytest.fail("invalid journal must block before Docker mutation"),
    )

    result = manager.run_compose_verified("up")

    assert result["ok"] is False
    assert result["ownership_reconciliation"]["reason"] == "ownership-journal-invalid"


def test_raw_compose_up_delegates_to_verified_mutation(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)
    calls = []
    monkeypatch.setattr(
        manager,
        "run_compose_verified",
        lambda action: calls.append(action) or {"ok": True},
    )

    result = manager.run_compose("up")

    assert result["ok"] is True
    assert calls == ["up"]


def test_dispatcher_cutover_spawn_failure_preserves_old_container_and_drops_pin(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("gemma4-mono"), repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "container_status": {
                "ok": True,
                "exists": True,
                "status": "Up",
                "id": OLD_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
            },
            "runtime_matches_desired": False,
            "runtime_mismatches": {"model_id": {"actual": "old", "desired": "new"}},
        },
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    monkeypatch.setattr(
        manager,
        "_cutover_preflight",
        lambda _snapshot, _docker: {
            "ok": True,
            "rollback_image_tag": "rollback:test",
            "previous_container_id": OLD_CONTAINER_ID,
        },
    )
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {
            "ok": True,
            "exists": True,
            "id": OLD_CONTAINER_ID,
            "operation_nonce": OPERATION_NONCE,
        },
    )
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
    )
    cleanup_calls = []
    monkeypatch.setattr(
        manager,
        "_cleanup_rollback_image",
        lambda replacement: (
            cleanup_calls.append(replacement) or {"ok": True, "removed": True}
        ),
    )
    monkeypatch.setattr(
        manager,
        "_rollback_replacement",
        lambda *_args, **_kwargs: pytest.fail("preserved old container must not be removed"),
    )

    result = manager.run_compose_verified("up")

    assert result["ok"] is False
    assert result["replacement"]["cutover_started"] is False
    assert result["rollback"] is None
    assert result["rollback_image_cleanup"]["removed"] is True
    assert len(cleanup_calls) == 1


def test_verified_dispatcher_cutover_rolls_back_failed_new_compose(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("gemma4-mono"), repo_root=tmp_path)
    replacement = {
        "required": True,
        "ok": True,
        "removed": True,
        "reused": False,
        "cutover_started": True,
        "preflight": {
            "rollback_image_tag": "rollback:test",
            "_rollback_env": {"SECRET": "must-not-leak"},
        },
    }
    monkeypatch.setattr(manager, "_replace_mismatched_container", lambda: replacement)
    monkeypatch.setattr(
        manager,
        "_run_compose_with_env",
        lambda _action, _env: {
            "ok": False,
            "summary": "new compose failed",
            "returncode": 1,
            "stdout": "",
            "stderr": "boom",
            "command": ["docker", "compose", "up"],
        },
    )
    rollback_calls = []

    def rollback(candidate, *, timeout_seconds):
        rollback_calls.append((candidate, timeout_seconds))
        return {"ok": True, "summary": "old runtime restored"}

    monkeypatch.setattr(manager, "_rollback_replacement", rollback)

    result = manager.run_compose_verified("up", timeout_seconds=7)

    assert result["ok"] is False
    assert result["rollback"]["ok"] is True
    assert rollback_calls == [(replacement, 7)]
    assert "_rollback_env" not in result["replacement"]["preflight"]
    assert "Previous runtime restored" in result["summary"]


def test_verified_dispatcher_cutover_rolls_back_failed_verification(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("gemma4-mono"), repo_root=tmp_path)
    replacement = {
        "required": True,
        "ok": True,
        "removed": True,
        "reused": False,
        "cutover_started": True,
        "preflight": {"rollback_image_tag": "rollback:test"},
    }
    monkeypatch.setattr(manager, "_replace_mismatched_container", lambda: replacement)
    monkeypatch.setattr(
        manager,
        "_run_compose_with_env",
        lambda _action, _env: {
            "ok": True,
            "summary": "new compose started",
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "command": ["docker", "compose", "up"],
        },
    )
    monkeypatch.setattr(
        manager,
        "_record_candidate_container_id",
        lambda candidate, _nonce: candidate.setdefault(
            "candidate_container_id", NEW_CONTAINER_ID
        ),
    )
    monkeypatch.setattr(
        manager,
        "verify_state",
        lambda **_kwargs: {"ok": False, "runtime_matches_desired": False},
    )
    monkeypatch.setattr(
        manager,
        "_rollback_replacement",
        lambda _candidate, *, timeout_seconds: {
            "ok": timeout_seconds == 9,
            "summary": "old runtime restored",
        },
    )

    result = manager.run_compose_verified("up", timeout_seconds=9)

    assert result["ok"] is False
    assert result["rollback"]["ok"] is True
    assert result["rollback_image_cleanup"] is None
    assert "previous runtime restored" in result["summary"].casefold()


def test_verified_dispatcher_cutover_commits_only_after_live_completion(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)
    replacement = {
        "required": True,
        "ok": True,
        "removed": True,
        "reused": False,
        "cutover_started": True,
        "preflight": {"rollback_image_tag": "rollback:test"},
    }
    monkeypatch.setattr(manager, "_replace_mismatched_container", lambda: replacement)
    monkeypatch.setattr(
        manager,
        "_run_compose_with_env",
        lambda _action, _env: {
            "ok": True,
            "returncode": 0,
            "summary": "candidate started",
        },
    )
    monkeypatch.setattr(
        manager,
        "_record_candidate_container_id",
        lambda candidate, _nonce: candidate.setdefault(
            "candidate_container_id", NEW_CONTAINER_ID
        ),
    )
    monkeypatch.setattr(manager, "_candidate_matches_current", lambda *_args, **_kwargs: True)
    captured_timeouts = []
    monkeypatch.setattr(
        manager,
        "verify_state",
        lambda *, running, timeout_seconds: (
            captured_timeouts.append((running, timeout_seconds))
            or {
                "ok": True,
                "live_completion": {"ok": True, "normalized_content": "4"},
            }
        ),
    )
    cleanup_calls = []
    monkeypatch.setattr(
        manager,
        "_cleanup_rollback_image",
        lambda candidate: (
            cleanup_calls.append(candidate) or {"ok": True, "removed": True}
        ),
    )
    monkeypatch.setattr(
        manager,
        "_rollback_replacement",
        lambda *_args, **_kwargs: pytest.fail("ready candidate must not roll back"),
    )

    result = manager.run_compose_verified("up")

    assert result["ok"] is True
    assert result["verification"]["live_completion"]["ok"] is True
    assert result["rollback_image_cleanup"]["removed"] is True
    assert cleanup_calls == [replacement]
    assert captured_timeouts == [(True, 900.0)]


def test_dispatcher_rollback_removes_candidate_and_verifies_exact_previous_runtime(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("gemma4-mono"), repo_root=tmp_path)
    previous_runtime = {"model_id": "previous"}
    rollback_env = {"JARVIS_VLLM_IMAGE": "rollback:test", "PRIVATE": "value"}
    replacement = {
        "candidate_container_id": NEW_CONTAINER_ID,
        "candidate_operation_nonce": OPERATION_NONCE,
        "preflight": {
            "_rollback_env": rollback_env,
            "previous_runtime": previous_runtime,
            "previous_image_id": "sha256:old",
            "rollback_image_tag": "rollback:test",
            "previous_container_id": OLD_CONTAINER_ID,
        }
    }
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    rollback_nonce = "b" * 32
    monkeypatch.setattr("jarvis_gpt.dispatcher.secrets.token_hex", lambda _size: rollback_nonce)
    container_states = iter(
        (
            {
                "ok": True,
                "exists": True,
                "id": NEW_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
            },
            {"ok": True, "exists": False},
            {
                "ok": True,
                "exists": True,
                "id": FOREIGN_CONTAINER_ID,
                "operation_nonce": rollback_nonce,
            },
            {
                "ok": True,
                "exists": True,
                "id": FOREIGN_CONTAINER_ID,
                "operation_nonce": rollback_nonce,
            },
        )
    )
    monkeypatch.setattr(manager, "_container_status", lambda _docker: next(container_states))
    commands = []
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda command, **_kwargs: (
            commands.append(command)
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    compose_calls = []
    monkeypatch.setattr(
        manager,
        "_run_compose_with_env",
        lambda action, env: (
            compose_calls.append((action, env))
            or {"ok": True, "returncode": 0}
        ),
    )
    verify_calls = []
    monkeypatch.setattr(
        manager,
        "_verify_runtime",
        lambda runtime, image, *, timeout_seconds: (
            verify_calls.append((runtime, image, timeout_seconds)) or {"ok": True}
        ),
    )
    monkeypatch.setattr(
        manager,
        "_cleanup_rollback_image",
        lambda _replacement: {"ok": True, "removed": True},
    )

    result = manager._rollback_replacement(replacement, timeout_seconds=11)

    assert result["ok"] is True
    assert commands == [["docker.exe", "rm", "-f", NEW_CONTAINER_ID]]
    assert compose_calls == [
        ("up", {**rollback_env, "JARVIS_DISPATCHER_OPERATION_NONCE": rollback_nonce})
    ]
    assert verify_calls == [(previous_runtime, "sha256:old", 11)]
    assert result["rollback_image_cleanup"]["removed"] is True


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
        lambda **_kwargs: {
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
        "_run_compose_with_env",
        lambda *_args, **_kwargs: pytest.fail("compose must not run for an exact runtime"),
    )
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "port": 8001,
            "port_open": True,
            "runtime_matches_desired": True,
            "runtime_mismatches": {},
            "container_status": {
                "ok": True,
                "exists": True,
                "status": "Up",
                "id": OLD_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
            },
        },
    )
    monkeypatch.setattr(
        manager,
        "verify_state",
        lambda **_kwargs: {
            "ok": True,
            "container_status": {
                "ok": True,
                "exists": True,
                "status": "Up",
                "id": OLD_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
            },
            "runtime_matches_desired": True,
            "port_open": True,
        },
    )
    monkeypatch.setattr(
        manager,
        "_live_completion_probe",
        lambda **_kwargs: {"ok": True, "terminal": False, "normalized_content": "4"},
    )

    result = manager.run_compose_verified("up", timeout_seconds=0.1)

    assert result["ok"] is True
    assert result["replacement"]["reused"] is True
    assert result["command"] == []


def test_dispatcher_stop_rejects_unknown_docker_state(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
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

        def run_compose_verified(self, requested_action, **_kwargs):
            assert requested_action == action
            return {"ok": False, "summary": "verification failed"}

    monkeypatch.setattr(cli, "_primary_runtime", fake_runtime)
    monkeypatch.setattr(cli, "DispatcherManager", FakeDispatcherManager)

    with pytest.raises(SystemExit) as exc_info:
        getattr(cli, command_name)(SimpleNamespace(profile="qwen36-vl"))

    assert exc_info.value.code == 1


def test_dispatcher_down_cli_keeps_safe_manager_snapshot_contract(monkeypatch):
    calls = []

    @contextmanager
    def fake_runtime(_profile):
        yield (SimpleNamespace(), None, None, None)

    class FakeDispatcherManager:
        def __init__(self, _settings, *, storage):
            assert storage is None

        def run_compose_verified(self, action):
            calls.append(action)
            return {"ok": True, "summary": "exact-ID stop verified"}

    monkeypatch.setattr(cli, "_primary_runtime", fake_runtime)
    monkeypatch.setattr(cli, "DispatcherManager", FakeDispatcherManager)
    monkeypatch.setattr(cli, "_print_json", lambda _value: None)

    cli.cmd_dispatcher_down(SimpleNamespace(profile="qwen36-vl"))

    assert calls == ["down"]


def test_launcher_candidate_journal_precedes_qwen_live_warmup(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker")
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "container_status": {"ok": True, "exists": False},
            "runtime_matches_desired": False,
        },
    )
    monkeypatch.setattr(
        manager,
        "_replace_mismatched_container",
        lambda **_kwargs: {
            "required": False,
            "ok": True,
            "removed": False,
            "reused": False,
        },
    )
    monkeypatch.setattr(
        manager,
        "_ensure_desired_image_available",
        lambda *_args, **_kwargs: {"ok": True},
    )

    def compose_with_intent(_action, compose_env):
        journal = manager._read_ownership_journal()
        assert journal is not None
        assert journal["phase"] == "intent"
        assert journal["launcher_owned"] is True
        assert journal["container_id"] == ""
        assert journal["operation_nonce"] == compose_env["JARVIS_DISPATCHER_OPERATION_NONCE"]
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "command": []}

    def record_candidate(replacement, nonce):
        replacement["candidate_container_id"] = NEW_CONTAINER_ID
        replacement["candidate_operation_nonce"] = nonce
        return NEW_CONTAINER_ID

    def verify_warmup_boundary(**_kwargs):
        journal = manager._read_ownership_journal()
        assert journal is not None
        assert journal["phase"] == "candidate"
        assert journal["container_id"] == NEW_CONTAINER_ID
        return {
            "ok": True,
            "container_status": {
                "ok": True,
                "exists": True,
                "status": "Up",
                "id": NEW_CONTAINER_ID,
                "operation_nonce": journal["operation_nonce"],
            },
        }

    monkeypatch.setattr(manager, "_run_compose_with_env", compose_with_intent)
    monkeypatch.setattr(manager, "_record_candidate_container_id", record_candidate)
    monkeypatch.setattr(manager, "verify_state", verify_warmup_boundary)
    monkeypatch.setattr(manager, "_candidate_matches_current", lambda *_args, **_kwargs: True)

    result = manager.run_compose_verified("up", ownership_intent="launcher")

    assert result["ok"] is True
    assert result["ownership_commit"]["pending_launcher_commit"] is True
    persisted = manager._read_ownership_journal()
    assert persisted is not None
    assert persisted["container_id"] == NEW_CONTAINER_ID


def test_qwen_replacement_rolls_back_when_launcher_state_sync_fails(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    _write_launcher_owned_state(settings, OLD_CONTAINER_ID)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker")
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "container_status": {
                "ok": True,
                "exists": True,
                "status": "Up",
                "id": OLD_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
            },
            "runtime_matches_desired": False,
        },
    )
    monkeypatch.setattr(
        manager,
        "_replace_mismatched_container",
        lambda **_kwargs: {
            "required": True,
            "ok": True,
            "removed": True,
            "reused": False,
            "cutover_started": True,
            "preflight": {"previous_container_id": OLD_CONTAINER_ID},
        },
    )

    def record_candidate(replacement, nonce):
        replacement["candidate_container_id"] = NEW_CONTAINER_ID
        replacement["candidate_operation_nonce"] = nonce
        return NEW_CONTAINER_ID

    monkeypatch.setattr(
        manager,
        "_run_compose_with_env",
        lambda *_args, **_kwargs: {
            "ok": True,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "command": [],
        },
    )
    monkeypatch.setattr(manager, "_record_candidate_container_id", record_candidate)
    monkeypatch.setattr(
        manager,
        "verify_state",
        lambda **_kwargs: {
            "ok": True,
            "container_status": {
                "id": NEW_CONTAINER_ID,
                "operation_nonce": "f" * 32,
            },
        },
    )
    monkeypatch.setattr(manager, "_candidate_matches_current", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        manager,
        "sync_launcher_state_container_id",
        lambda **_proof: {
            "ok": False,
            "updated": False,
            "reason": "launcher-state-write-failed:OSError",
        },
    )
    rollbacks = []
    monkeypatch.setattr(
        manager,
        "_rollback_replacement",
        lambda *_args, **_kwargs: rollbacks.append(True) or {"ok": True},
    )

    result = manager.run_compose_verified("up")

    assert result["ok"] is False
    assert result["ownership_commit"]["ok"] is False
    assert result["verification"]["ownership_commit"]["ok"] is False
    assert rollbacks == [True]


def test_qwen_rollback_commits_restored_full_id_and_nonce(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    _write_launcher_owned_state(settings, OLD_CONTAINER_ID)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    restored_id = "4" * 64
    replacement = {
        "candidate_container_id": NEW_CONTAINER_ID,
        "candidate_operation_nonce": OPERATION_NONCE,
        "_ownership_context": {
            "launcher_owned": True,
            "requires_state_sync": True,
            "state_expected_container_id": OLD_CONTAINER_ID,
            "state_expected_operation_nonce": OPERATION_NONCE,
            "source": "launcher-state",
        },
        "preflight": {
            "_rollback_env": {"JARVIS_VLLM_IMAGE": "rollback:test"},
            "previous_runtime": {"model_path": "/models/qwen"},
            "previous_image_id": "sha256:old",
            "previous_container_id": OLD_CONTAINER_ID,
            "rollback_image_tag": "rollback:test",
        },
    }
    statuses = iter(
        (
            {
                "ok": True,
                "exists": True,
                "id": NEW_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
            },
            {"ok": True, "exists": False},
        )
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker")
    monkeypatch.setattr(manager, "_container_status", lambda _docker: next(statuses))
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        manager,
        "_run_compose_with_env",
        lambda *_args, **_kwargs: {"ok": True, "returncode": 0},
    )

    def record_restored(restoration, nonce):
        restoration["candidate_container_id"] = restored_id
        restoration["candidate_operation_nonce"] = nonce
        return restored_id

    monkeypatch.setattr(manager, "_record_candidate_container_id", record_restored)
    monkeypatch.setattr(manager, "_verify_runtime", lambda *_args, **_kwargs: {"ok": True})
    monkeypatch.setattr(manager, "_candidate_matches_current", lambda *_args, **_kwargs: True)
    sync_proofs = []

    def sync_state(**proof):
        sync_proofs.append(proof)
        return {
            "ok": True,
            "updated": True,
            "reason": "docker-id-synchronized",
            "container_id": restored_id,
        }

    monkeypatch.setattr(manager, "sync_launcher_state_container_id", sync_state)

    result = manager._rollback_replacement(replacement, timeout_seconds=1)

    assert result["ok"] is True
    assert result["container_id"] == restored_id
    assert result["operation_nonce"] == sync_proofs[0]["operation_nonce"]
    assert sync_proofs[0]["expected_previous_id"] == OLD_CONTAINER_ID
    assert sync_proofs[0]["expected_previous_operation_nonce"] == OPERATION_NONCE
    assert sync_proofs[0]["expected_current_id"] == restored_id
    assert manager._read_ownership_journal() is None


def test_qwen_launcher_ownership_requires_exact_state_id_and_nonce(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    different_nonce = "b" * 32
    _write_launcher_owned_state(settings, OLD_CONTAINER_ID, different_nonce)

    mismatched = manager._launcher_ownership_context(
        previous_container_id=OLD_CONTAINER_ID,
        previous_operation_nonce=OPERATION_NONCE,
        explicit_launcher_intent=False,
    )
    legacy_state = json.loads(
        (settings.state_dir / "launcher-state.json").read_text(encoding="utf-8")
    )
    del legacy_state["services"]["dispatcher"]["operation_nonce"]
    (settings.state_dir / "launcher-state.json").write_text(
        json.dumps(legacy_state),
        encoding="utf-8",
    )
    missing = manager._launcher_ownership_context(
        previous_container_id=OLD_CONTAINER_ID,
        previous_operation_nonce=OPERATION_NONCE,
        explicit_launcher_intent=False,
    )

    assert mismatched["launcher_owned"] is False
    assert mismatched["requires_state_sync"] is False
    assert missing["launcher_owned"] is False
    assert missing["requires_state_sync"] is False


def test_qwen_launcher_state_sync_rejects_old_nonce_cas_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    state_nonce = "b" * 32
    _write_launcher_owned_state(settings, OLD_CONTAINER_ID, state_nonce)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    monkeypatch.setattr(
        manager,
        "status",
        lambda: {
            "container_status": {
                "ok": True,
                "exists": True,
                "status": "Up",
                "id": NEW_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
            }
        },
    )

    result = manager.sync_launcher_state_container_id(
        expected_previous_id=OLD_CONTAINER_ID,
        expected_previous_operation_nonce="c" * 32,
        expected_current_id=NEW_CONTAINER_ID,
        operation_nonce=OPERATION_NONCE,
    )

    assert result["ok"] is False
    assert result["reason"] == "launcher-container-id-or-nonce-cas-mismatch"
    persisted = json.loads(
        (settings.state_dir / "launcher-state.json").read_text(encoding="utf-8")
    )
    assert persisted["services"]["dispatcher"]["container_id"] == OLD_CONTAINER_ID
    assert persisted["services"]["dispatcher"]["operation_nonce"] == state_nonce


def test_qwen_rollback_intent_precedes_restore_and_recovers_post_compose_crash(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    _write_launcher_owned_state(settings, OLD_CONTAINER_ID)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    rollback_nonce = "b" * 32
    restored_id = "4" * 64
    replacement = {
        "candidate_container_id": NEW_CONTAINER_ID,
        "candidate_operation_nonce": OPERATION_NONCE,
        "_ownership_context": {
            "launcher_owned": True,
            "requires_state_sync": True,
            "state_expected_container_id": OLD_CONTAINER_ID,
            "state_expected_operation_nonce": OPERATION_NONCE,
            "source": "launcher-state",
        },
        "preflight": {
            "_rollback_env": {"JARVIS_VLLM_IMAGE": "rollback:test"},
            "previous_runtime": {"model_path": "/models/qwen"},
            "previous_image_id": "sha256:old",
            "previous_container_id": OLD_CONTAINER_ID,
        },
    }
    states = iter(
        (
            {
                "ok": True,
                "exists": True,
                "id": NEW_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
            },
            {"ok": True, "exists": False},
        )
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker")
    monkeypatch.setattr(manager, "_container_status", lambda _docker: next(states))
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.secrets.token_hex", lambda _size: rollback_nonce)

    def restore_compose(_action, env):
        journal = manager._read_ownership_journal()
        assert journal is not None
        assert journal["phase"] == "rollback-intent"
        assert journal["operation_nonce"] == rollback_nonce
        assert env["JARVIS_DISPATCHER_OPERATION_NONCE"] == rollback_nonce
        return {"ok": True, "returncode": 0}

    monkeypatch.setattr(manager, "_run_compose_with_env", restore_compose)
    monkeypatch.setattr(
        manager,
        "_record_candidate_container_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("power-loss")),
    )

    with pytest.raises(SystemExit, match="power-loss"):
        manager._rollback_replacement(replacement, timeout_seconds=1)

    pending = manager._read_ownership_journal()
    assert pending is not None
    assert pending["phase"] == "rollback-intent"
    current = {
        "ok": True,
        "exists": True,
        "status": "Up",
        "id": restored_id,
        "operation_nonce": rollback_nonce,
    }
    monkeypatch.setattr(manager, "_container_status", lambda _docker: current)
    monkeypatch.setattr(manager, "status", lambda: {"container_status": current})

    reconciled = manager._reconcile_ownership_journal_locked()

    assert reconciled["ok"] is True
    assert reconciled["reason"] == "launcher-state-synchronized"
    assert manager._read_ownership_journal() is None
    persisted = json.loads(
        (settings.state_dir / "launcher-state.json").read_text(encoding="utf-8")
    )
    assert persisted["services"]["dispatcher"]["container_id"] == restored_id
    assert persisted["services"]["dispatcher"]["operation_nonce"] == rollback_nonce


def test_qwen_rollback_intent_before_compose_crash_clears_as_abandoned(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    rollback_nonce = "b" * 32
    replacement = {
        "candidate_container_id": NEW_CONTAINER_ID,
        "candidate_operation_nonce": OPERATION_NONCE,
        "_ownership_context": {
            "launcher_owned": False,
            "requires_state_sync": False,
            "state_expected_container_id": "",
            "state_expected_operation_nonce": "",
            "source": "unowned",
        },
        "preflight": {
            "_rollback_env": {"JARVIS_VLLM_IMAGE": "rollback:test"},
            "previous_runtime": {"model_path": "/models/qwen"},
            "previous_image_id": "sha256:old",
            "previous_container_id": OLD_CONTAINER_ID,
        },
    }
    states = iter(
        (
            {
                "ok": True,
                "exists": True,
                "id": NEW_CONTAINER_ID,
                "operation_nonce": OPERATION_NONCE,
            },
            {"ok": True, "exists": False},
        )
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker")
    monkeypatch.setattr(manager, "_container_status", lambda _docker: next(states))
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("jarvis_gpt.dispatcher.secrets.token_hex", lambda _size: rollback_nonce)

    def crash_before_restore(_action, _env):
        assert manager._read_ownership_journal()["phase"] == "rollback-intent"
        raise SystemExit("power-loss-before-restore")

    monkeypatch.setattr(manager, "_run_compose_with_env", crash_before_restore)

    with pytest.raises(SystemExit, match="power-loss-before-restore"):
        manager._rollback_replacement(replacement, timeout_seconds=1)

    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {"ok": True, "exists": False},
    )
    reconciled = manager._reconcile_ownership_journal_locked()
    assert reconciled["ok"] is True
    assert reconciled["reason"] == "abandoned-operation-journal-cleared"
    assert manager._read_ownership_journal() is None


def test_qwen_dispatcher_stop_refuses_same_id_wrong_nonce(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    manager = DispatcherManager(load_settings("qwen36-vl"), repo_root=tmp_path)
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker.exe")
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {
            "ok": True,
            "exists": True,
            "id": OLD_CONTAINER_ID,
            "operation_nonce": OPERATION_NONCE,
        },
    )
    monkeypatch.setattr(
        "jarvis_gpt.dispatcher.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("nonce mismatch must never reach docker rm"),
    )

    result = manager.run_compose_verified(
        "down",
        expected_container_id=OLD_CONTAINER_ID,
        expected_operation_nonce="d" * 32,
    )

    assert result["ok"] is False
    assert result["command"] == []
    assert result.get("current_operation_nonce") == OPERATION_NONCE


def test_qwen_stopped_tombstone_clears_when_container_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    journal = manager._new_ownership_journal(
        phase="stopped",
        operation_nonce=OPERATION_NONCE,
        previous_container_id=OLD_CONTAINER_ID,
        container_id="",
        context={
            "launcher_owned": True,
            "requires_state_sync": False,
            "state_expected_container_id": OLD_CONTAINER_ID,
            "state_expected_operation_nonce": OPERATION_NONCE,
        },
    )
    assert manager._write_ownership_journal(journal)["ok"] is True
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker")
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {"ok": True, "exists": False},
    )

    reconciled = manager._reconcile_ownership_journal_locked()
    assert reconciled["ok"] is True
    assert reconciled["reason"] == "completed-stop-tombstone-cleared"
    assert manager._read_ownership_journal() is None


def test_qwen_stopped_tombstone_clears_when_foreign_container_appears(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    journal = manager._new_ownership_journal(
        phase="stopped",
        operation_nonce=OPERATION_NONCE,
        previous_container_id=OLD_CONTAINER_ID,
        container_id="",
        context={
            "launcher_owned": True,
            "requires_state_sync": False,
            "state_expected_container_id": OLD_CONTAINER_ID,
            "state_expected_operation_nonce": OPERATION_NONCE,
        },
    )
    assert manager._write_ownership_journal(journal)["ok"] is True
    foreign_id = "b" * 64
    monkeypatch.setattr("jarvis_gpt.dispatcher.shutil.which", lambda _name: "docker")
    monkeypatch.setattr(
        manager,
        "_container_status",
        lambda _docker: {
            "ok": True,
            "exists": True,
            "id": foreign_id,
            "operation_nonce": "e" * 32,
        },
    )

    reconciled = manager._reconcile_ownership_journal_locked()
    assert reconciled["ok"] is True
    assert reconciled["reason"] == "stop-tombstone-superseded-cleared"
    assert manager._read_ownership_journal() is None
