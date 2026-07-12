from __future__ import annotations

import json

import pytest
from jarvis_gpt.config import PROFILES, ensure_runtime_dirs, load_settings
from jarvis_gpt.model_catalog import MODEL_OVERRIDE_KEY, ModelCatalog
from jarvis_gpt.storage import JarvisStorage


def test_builtin_profile_model_identities_are_exclusive() -> None:
    assert PROFILES["gemma4-mono"].model_dir_name == "gemma4-31b-it-nvfp4"
    assert PROFILES["gemma4-mono-perf"].model_dir_name == "gemma4-31b-it-nvfp4"
    assert PROFILES["gemma4-turbo"].model_dir_name == "gemma4-26b-a4b-nvfp4"


def test_mono_perf_dispatcher_args_are_exact_and_keep_fractional_offload(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-mono-perf")

    env = ModelCatalog(settings).dispatcher_config()["env"]

    assert env["JARVIS_QWEN_CPU_OFFLOAD_ARGS"] == "--cpu-offload-gb 2.5"
    assert env["JARVIS_QWEN_EXTRA_ARGS"] == (
        "--language-model-only --skip-mm-profiling --mm-processor-cache-gb 0 "
        "--max-num-batched-tokens 512"
    )


def test_model_catalog_uses_configured_root_and_active_profile(monkeypatch, tmp_path):
    model_root = tmp_path / "models"
    active = model_root / "gemma4-31b-it-nvfp4"
    active.mkdir(parents=True)
    (active / "config.json").write_text(
        json.dumps(
            {
                "model_type": "gemma4",
                "architectures": ["Gemma4ForConditionalGeneration"],
                "dtype": "bfloat16",
            }
        ),
        encoding="utf-8",
    )
    (active / "hf_quant_config.json").write_text(
        json.dumps({"quantization": {"quant_algo": "NVFP4"}}),
        encoding="utf-8",
    )
    (active / "model-00001-of-00001.safetensors").write_bytes(b"fake-weights")
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(model_root))

    settings = load_settings("gemma4-mono")
    ensure_runtime_dirs(settings)
    catalog = ModelCatalog(settings).response()

    assert catalog["root"] == str(model_root)
    assert catalog["active_model"]["id"] == "gemma4-31b-it-nvfp4"
    assert catalog["active_model"]["exists"] is True
    assert catalog["active_model"]["quantization"] == "NVFP4"
    assert catalog["dispatcher"]["env"]["JARVIS_QWEN_MODEL_PATH"].endswith(
        "/gemma4-31b-it-nvfp4"
    )


def test_model_catalog_honors_active_model_override(monkeypatch, tmp_path):
    model_root = tmp_path / "models"
    (model_root / "gemma4-31b-it-nvfp4").mkdir(parents=True)
    custom = model_root / "owner__custom-7b-q4"
    custom.mkdir(parents=True)
    (custom / "config.json").write_text(
        json.dumps({"model_type": "llama", "hidden_size": 4096, "num_hidden_layers": 32}),
        encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(model_root))

    settings = load_settings("gemma4-mono")
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.set_runtime_value(MODEL_OVERRIDE_KEY, "owner__custom-7b-q4")

    catalog = ModelCatalog(settings, storage).response()

    assert catalog["active_model"]["id"] == "owner__custom-7b-q4"
    assert catalog["dispatcher"]["env"]["JARVIS_QWEN_MODEL_PATH"] == "/models/owner__custom-7b-q4"
    storage.close()


@pytest.mark.parametrize(
    ("profile_name", "override", "expected"),
    [
        ("gemma4-mono", "gemma4-26b-a4b-nvfp4", "gemma4-31b-it-nvfp4"),
        ("gemma4-mono-perf", "gemma4-26b-a4b-nvfp4", "gemma4-31b-it-nvfp4"),
        ("gemma4-turbo", "gemma4-31b-it-nvfp4", "gemma4-26b-a4b-nvfp4"),
    ],
)
def test_model_catalog_ignores_cross_profile_builtin_override(
    monkeypatch,
    tmp_path,
    profile_name,
    override,
    expected,
):
    model_root = tmp_path / "models"
    (model_root / "gemma4-26b-a4b-nvfp4").mkdir(parents=True)
    (model_root / "gemma4-31b-it-nvfp4").mkdir(parents=True)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(model_root))
    settings = load_settings(profile_name)
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    storage.set_runtime_value(MODEL_OVERRIDE_KEY, override)

    catalog = ModelCatalog(settings, storage)

    assert catalog.active_model_dir_name() == expected
    assert catalog.dispatcher_config()["env"]["JARVIS_QWEN_MODEL_PATH"] == (
        f"/models/{expected}"
    )
    storage.close()
