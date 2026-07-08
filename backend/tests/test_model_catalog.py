from __future__ import annotations

import json

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.model_catalog import MODEL_OVERRIDE_KEY, ModelCatalog
from jarvis_gpt.storage import JarvisStorage


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
