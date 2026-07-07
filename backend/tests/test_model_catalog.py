from __future__ import annotations

import json

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.model_catalog import ModelCatalog


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
