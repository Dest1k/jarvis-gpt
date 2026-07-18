"""Model control from chat: switch profile / restart-stop-start dispatcher / model status.

Deterministic NativeAction routing (agent._model_control_action) + the direct-action handlers,
so the weak local model never has to drive dispatcher control itself.
"""

from __future__ import annotations

import asyncio

from jarvis_gpt.agent import (
    AgentRuntime,
    NativeAction,
    _format_model_status,
    _model_control_action,
    _native_action_from_message,
)
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.event_bus import EventBus
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage


def _action(phrase: str) -> NativeAction | None:
    return _native_action_from_message(phrase, load_settings("qwen36-vl"))


# --------------------------------------------------------------------------- #
# Detection.
# --------------------------------------------------------------------------- #


def test_restart_phrases_route_to_dispatcher_restart():
    phrases = ("перезапусти модель", "перезапусти диспетчер", "перезагрузи мозг", "рестарт vllm")
    for phrase in phrases:
        action = _action(phrase)
        assert action is not None and action.action == "dispatcher.restart", phrase


def test_stop_and_start_phrases():
    assert _action("останови модель").action == "dispatcher.stop"
    assert _action("выключи диспетчер").action == "dispatcher.stop"
    assert _action("запусти диспетчер").action == "dispatcher.start"
    assert _action("подними мозг").action == "dispatcher.start"


def test_switch_profile_phrases_carry_target():
    gemma = _action("переключись на gemma")
    assert gemma.action == "model.switch_profile" and gemma.payload["target"] == "gemma4-turbo"
    qwen = _action("переключись на qwen")
    assert qwen.action == "model.switch_profile" and qwen.payload["target"] == "qwen36-vl"
    assert _action("запусти gemma").action == "model.switch_profile"


def test_status_phrases_route_to_model_status():
    for phrase in ("какая модель загружена", "статус диспетчера", "что за модель сейчас работает"):
        assert _action(phrase).action == "model.status", phrase


def test_vram_question_stays_on_gpu_telemetry_not_model_control():
    # "сколько модель ест VRAM" must go to nvidia-smi, not model control.
    assert _action("сколько модель ест vram").action == "hardware.gpu"


def test_device_spec_and_shopping_are_not_model_control():
    assert _model_control_action("какая модель телефона у тебя") is None
    assert _model_control_action("какую модель rtx 5090 купить дешевле") is None
    assert _model_control_action("расскажи про модель вселенной") is None


# --------------------------------------------------------------------------- #
# Report formatting.
# --------------------------------------------------------------------------- #


def test_format_model_status_combines_sources():
    settings = load_settings("qwen36-vl")
    disp = {"active_model": "qwen3.6-35b-a3b-nvfp4", "port_open": True, "port": 8001}
    health = {"served_models": ["dispatcher"]}
    gpus = [{"name": "RTX 5090", "memory_used_mib": 31000, "memory_total_mib": 32607,
             "memory_free_mib": 1607, "utilization_pct": 40}]
    report = _format_model_status(settings, disp, health, gpus)
    assert "Модель: dispatcher" in report
    assert "qwen36-vl" in report
    assert "работает" in report
    assert "VRAM 31000/32607" in report


# --------------------------------------------------------------------------- #
# Direct-action handlers.
# --------------------------------------------------------------------------- #


def _agent(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    monkeypatch.setenv("JARVIS_PROFILE", "qwen36-vl")
    settings = load_settings("qwen36-vl")
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return AgentRuntime(settings=settings, storage=storage, llm=LLMRouter(settings), bus=EventBus())


def test_dispatcher_control_runs_tool_with_allow_danger(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    calls: list[tuple[str, bool]] = []

    async def fake_run(name, args=None, allow_danger=False, **kwargs):
        calls.append((name, allow_danger))
        return ToolRunResponse(tool=name, ok=True, summary="перезапуск выполнен", data={})

    monkeypatch.setattr(agent.tools, "run", fake_run)
    action = NativeAction(
        action="dispatcher.restart", payload={}, answer="перезапуск диспетчера модели"
    )
    result = asyncio.run(agent._dispatcher_control_action(action))
    assert calls == [("dispatcher.restart", True)]
    assert "Готово" in result.answer
    assert "прогреется" in result.answer  # warmup note for a restart


def test_model_status_summary_reports(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)

    async def fake_run(name, args=None, allow_danger=False, **kwargs):
        if name == "dispatcher.status":
            return ToolRunResponse(
                tool=name, ok=True, summary="", data={"active_model": "qwen", "port_open": True}
            )
        if name == "llm.health":
            return ToolRunResponse(
                tool=name, ok=True, summary="", data={"served_models": ["dispatcher"]}
            )
        return ToolRunResponse(
            tool=name, ok=True, summary="", data={"native": {"result": {"data": {"gpus": []}}}}
        )

    monkeypatch.setattr(agent.tools, "run", fake_run)
    result = asyncio.run(agent._model_status_summary("проверил"))
    assert "Состояние модели" in result.answer
    assert "dispatcher" in result.answer


def test_switch_profile_reports_restart_instruction(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    # Current profile is qwen36-vl; switching to gemma needs a serve restart.
    result = agent._model_switch_profile_answer({"target": "gemma4-turbo"})
    assert "gemma4-turbo" in result.answer
    assert "--profile gemma4-turbo serve" in result.answer
    # Same-profile is a no-op message.
    same = agent._model_switch_profile_answer({"target": "qwen36-vl"})
    assert "Уже работает" in same.answer
