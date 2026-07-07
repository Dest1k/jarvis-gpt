from __future__ import annotations

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.dispatcher import DispatcherManager


def test_dispatcher_manager_builds_compose_environment(monkeypatch, tmp_path):
    model_root = tmp_path / "models"
    (model_root / "gemma4-31b-it-nvfp4").mkdir(parents=True)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_MODEL_ROOT", str(model_root))

    settings = load_settings("gemma4-mono")
    ensure_runtime_dirs(settings)
    manager = DispatcherManager(settings, repo_root=tmp_path)
    env = manager.compose_env()
    status = manager.status()

    assert env["JARVIS_QWEN_MODEL_PATH"] == "/models/gemma4-31b-it-nvfp4"
    assert env["JARVIS_QWEN_MODEL_NAME"] == "dispatcher"
    assert manager.compose_command("up")[-2:] == ["-d", "dispatcher"]
    assert status["active_model"]["id"] == "gemma4-31b-it-nvfp4"
