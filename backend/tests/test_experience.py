from __future__ import annotations

import asyncio
from typing import Any

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.experience import ExperienceManager, parse_response_preference
from jarvis_gpt.models import DiagnosticCheck
from jarvis_gpt.storage import JarvisStorage


class FakeLLM:
    async def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "status_code": 200,
            "served_models": ["dispatcher"],
            "configured_model": "dispatcher",
        }

    async def benchmark_inference(self, **kwargs) -> dict[str, Any]:
        assert kwargs == {"runs": 3, "max_tokens": 64, "timeout_sec": 30.0}
        return {
            "ok": True,
            "requested_runs": 3,
            "successful_runs": 3,
            "runs": [],
            "aggregate": {
                "ttft_ms_p50": 125.0,
                "decode_tokens_per_sec_p50": 42.5,
            },
        }


class FakeTelemetry:
    def snapshot(self) -> dict[str, Any]:
        return {
            "ts": "2026-07-08T00:00:00+00:00",
            "memory": {"used_ratio": 0.41, "available": 1024},
            "gpu": {
                "available": True,
                "gpus": [
                    {
                        "name": "RTX",
                        "memory_used_ratio": 0.5,
                        "utilization_gpu": 35,
                    }
                ],
            },
            "disks": [{"path": "D:/jarvis", "used_ratio": 0.2}],
        }


class FakeDispatcher:
    def status(self) -> dict[str, Any]:
        return {
            "port_open": True,
            "base_url": "http://127.0.0.1:8001/v1",
            "model": "dispatcher",
            "container_status": {"exists": True, "status": "running"},
        }


def _manager(monkeypatch, tmp_path) -> tuple[ExperienceManager, JarvisStorage]:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings("gemma4-turbo")
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return ExperienceManager(settings=settings, storage=storage), storage


def test_preferences_and_policy_are_persistent(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)

    preferences = manager.update_preferences(
        {
            "operator_name": "Operator",
            "communication_style": "detailed",
            "voice_input_reply_mode": "text",
            "preferred_profile": "gemma4-mono-perf",
            "working_roots": ["D:/jarvis", "C:/work"],
        }
    )
    policy = manager.update_autonomy_policy({"mode": "safe"})
    reloaded = ExperienceManager(settings=manager.settings, storage=storage)

    assert preferences["operator_name"] == "Operator"
    assert reloaded.preferences()["communication_style"] == "detailed"
    assert reloaded.preferences()["voice_input_reply_mode"] == "text"
    assert reloaded.preferences()["preferred_profile"] == "gemma4-mono-perf"
    assert policy["mode"] == "safe"
    assert policy["max_autonomous_steps"] == 1
    assert reloaded.autonomy_policy()["resource_guard"]["max_gpu_memory_ratio"] == 0.84
    storage.close()


def test_response_preference_parser_accepts_durable_choices_only():
    voice_text = parse_response_preference(
        "Джарвис, запомни: отвечай мне на голосовые сообщения текстом!"
    )
    voice_auto = parse_response_preference("/voice auto")
    detailed = parse_response_preference("Пожалуйста, отвечай мне подробно")
    concise = parse_response_preference("Запомни это: отвечай кратко")

    assert voice_text is not None
    assert voice_text.patch == {"voice_input_reply_mode": "text"}
    assert voice_auto is not None
    assert voice_auto.patch == {"voice_input_reply_mode": "auto"}
    assert detailed is not None
    assert detailed.patch == {"communication_style": "detailed"}
    assert concise is not None
    assert concise.patch == {"communication_style": "concise"}
    assert parse_response_preference("Ответь на это голосовое текстом") is None
    assert parse_response_preference("Он просил отвечать на голосовые текстом") is None


def test_daily_briefing_summarizes_risk_and_pending_approval(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    storage.record_health(
        component="llm.router",
        status="warn",
        message="LLM endpoint is unavailable",
    )
    storage.create_approval(
        title="Host command",
        description="Needs operator review",
        requested_action="tool.run",
        risk="danger",
    )

    briefing = manager.daily_briefing(dispatcher_status={"port_open": False})

    assert briefing["headline"] == "Runtime needs attention"
    assert briefing["pending_approvals"] == 1
    assert any("self-heal" in item for item in briefing["suggestions"])
    assert any("Dispatcher" in item for item in briefing["focus"])
    storage.close()


def test_self_heal_report_suggests_non_destructive_actions(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)
    checks = [
        DiagnosticCheck(
            name="llm.router",
            status="warn",
            message="LLM endpoint is unavailable",
        )
    ]

    report = manager.self_heal_report(
        checks=checks,
        telemetry_snapshot=FakeTelemetry().snapshot(),
        dispatcher_status={"port_open": False},
    )

    assert report["ok"] is False
    assert {action["id"] for action in report["actions"]} >= {
        "dispatcher.inspect",
        "dispatcher.start",
    }
    assert all(action["kind"] in {"safe", "approval"} for action in report["actions"])
    storage.close()


def test_benchmark_records_history(monkeypatch, tmp_path):
    manager, storage = _manager(monkeypatch, tmp_path)

    report = asyncio.run(
        manager.run_benchmark(
            llm=FakeLLM(),
            telemetry=FakeTelemetry(),
            dispatcher=FakeDispatcher(),
        )
    )

    assert report["llm"]["ok"] is True
    assert report["inference"]["aggregate"]["ttft_ms_p50"] == 125.0
    assert report["history"][0]["decode_tokens_per_sec_p50"] == 42.5
    assert report["dispatcher"]["port_open"] is True
    assert report["history"][0]["profile"] == "gemma4-turbo"
    latest = storage.get_runtime_value("performance.benchmark.latest", {})
    assert latest["profile"] == "gemma4-turbo"
    storage.close()
