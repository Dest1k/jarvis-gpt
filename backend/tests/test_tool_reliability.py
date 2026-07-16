from __future__ import annotations

import asyncio

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.models import ToolRunResponse
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry, ToolSpec


def _registry(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return ToolRegistry(settings, storage, LLMRouter(settings)), storage


def test_policy_rejections_have_stable_machine_readable_decisions(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    tools.add(
        ToolSpec(
            name="test.review",
            description="review tool",
            category="test",
            input_schema={},
            handler=lambda _ctx, _args: ToolRunResponse(
                tool="test.review", ok=True, summary="unexpected"
            ),
            danger_level="review",
        )
    )

    missing = asyncio.run(tools.run("test.missing", {}))
    gated = asyncio.run(tools.run("test.review", {"value": 1}))

    missing_decision = missing.data["policy_decision"]
    assert missing_decision == {
        "protocol": "jarvis.policy-decision.v1",
        "effect": "deny",
        "code": "tool_not_registered",
        "source": "tool_registry",
        "reason": missing.summary,
        "remediation": "Choose a registered tool from the returned availability list.",
        "retryable": False,
        "outcome": "not_started",
    }
    gated_decision = gated.data["policy_decision"]
    assert gated_decision["effect"] == "require_approval"
    assert gated_decision["code"] == "approval_required"
    assert gated_decision["outcome"] == "not_started"
    assert gated_decision["retryable"] is False
    storage.close()


def test_handler_exception_is_ambiguous_and_never_blindly_retryable(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)

    def fail_after_possible_effect(_context, _arguments):
        raise RuntimeError("connection disappeared")

    tools.add(
        ToolSpec(
            name="test.mutation",
            description="mutation",
            category="test",
            input_schema={},
            handler=fail_after_possible_effect,
            danger_level="review",
        )
    )

    result = asyncio.run(tools.run("test.mutation", {}, allow_danger=True))

    assert result.ok is False
    assert result.data["failure"] == {
        "protocol": "jarvis.tool-failure.v1",
        "kind": "handler_exception",
        "outcome": "unknown",
        "outcome_known": False,
        "retryable": False,
        "requires_operator": True,
        "remediation": "Inspect the target state before deciding whether to retry.",
        "fallback": "Use a read-only inspection or reconciliation action.",
    }
    storage.close()


def test_audit_failure_cannot_turn_completed_tool_into_retryable_failure(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    effects: list[str] = []

    def completed(_context, _arguments):
        effects.append("completed")
        return ToolRunResponse(tool="test.completed", ok=True, summary="effect completed")

    tools.add(
        ToolSpec(
            name="test.completed",
            description="completed effect",
            category="test",
            input_schema={},
            handler=completed,
        )
    )

    def audit_down(*_args, **_kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(storage, "record_tool_run", audit_down)
    monkeypatch.setattr(storage, "add_event", audit_down)

    result = asyncio.run(tools.run("test.completed", {}))

    assert result.ok is True
    assert result.summary == "effect completed"
    assert effects == ["completed"]
    assert result.data["audit_status"] == {
        "protocol": "jarvis.audit-status.v1",
        "persisted": False,
        "failed_sinks": ["tool_run", "runtime_event"],
        "outcome_known": True,
        "retryable": False,
    }
    storage.close()


def test_non_finite_arguments_return_policy_decision_instead_of_raising(
    monkeypatch,
    tmp_path,
):
    tools, storage = _registry(monkeypatch, tmp_path)
    tools.add(
        ToolSpec(
            name="test.review",
            description="review tool",
            category="test",
            input_schema={},
            handler=lambda _ctx, _args: ToolRunResponse(
                tool="test.review", ok=True, summary="unexpected"
            ),
            danger_level="review",
        )
    )

    result = asyncio.run(tools.run("test.review", {"value": float("nan")}))

    assert result.ok is False
    assert result.data["invalid_arguments"] is True
    assert result.data["policy_decision"]["code"] == "arguments_not_canonical_json"
    assert result.data["policy_decision"]["effect"] == "deny"
    assert result.data["policy_decision"]["outcome"] == "not_started"
    storage.close()
