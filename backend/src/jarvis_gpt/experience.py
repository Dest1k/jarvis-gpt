from __future__ import annotations

import asyncio
import time
from typing import Any

from . import persona as persona_module
from .config import JarvisSettings
from .models import DiagnosticCheck
from .storage import JarvisStorage, utc_now

DEFAULT_PREFERENCES: dict[str, Any] = {
    "operator_name": "Admin",
    "communication_style": "concise",
    "daily_briefing": True,
    "voice_reply": False,
    "preferred_profile": "gemma4-turbo",
    "quiet_hours": "",
    "working_roots": [r"D:\jarvis", r"D:\jarvis-gpt"],
}

DEFAULT_AUTONOMY_POLICY: dict[str, Any] = {
    "mode": "balanced",
    "allow_safe_tools": True,
    "allow_review_tools": False,
    "allow_danger_tools": False,
    "allow_background_learning": True,
    "allow_self_healing_suggestions": True,
    "approval_required_for": [
        "execution.apply",
        "execution.transaction",
        "execution.cancel",
        "filesystem.write_text",
        "dispatcher.start",
        "dispatcher.stop",
    ],
    "max_autonomous_steps": 3,
    "resource_guard": {
        "max_memory_ratio": 0.92,
        "max_gpu_memory_ratio": 0.9,
    },
}

POLICY_PRESETS: dict[str, dict[str, Any]] = {
    "safe": {
        "allow_safe_tools": True,
        "allow_review_tools": False,
        "allow_danger_tools": False,
        "allow_background_learning": True,
        "allow_self_healing_suggestions": True,
        "max_autonomous_steps": 1,
        "resource_guard": {"max_memory_ratio": 0.86, "max_gpu_memory_ratio": 0.84},
    },
    "balanced": {
        "allow_safe_tools": True,
        "allow_review_tools": False,
        "allow_danger_tools": False,
        "allow_background_learning": True,
        "allow_self_healing_suggestions": True,
        "max_autonomous_steps": 3,
        "resource_guard": {"max_memory_ratio": 0.92, "max_gpu_memory_ratio": 0.9},
    },
    "operator": {
        "allow_safe_tools": True,
        "allow_review_tools": True,
        "allow_danger_tools": False,
        "allow_background_learning": True,
        "allow_self_healing_suggestions": True,
        "max_autonomous_steps": 6,
        "resource_guard": {"max_memory_ratio": 0.95, "max_gpu_memory_ratio": 0.94},
    },
}

PREFERENCES_KEY = "experience.preferences"
AUTONOMY_POLICY_KEY = "experience.autonomy_policy"
BENCHMARK_LATEST_KEY = "performance.benchmark.latest"
BENCHMARK_HISTORY_KEY = "performance.benchmark.history"


class ExperienceManager:
    def __init__(self, *, settings: JarvisSettings, storage: JarvisStorage) -> None:
        self.settings = settings
        self.storage = storage

    def preferences(self) -> dict[str, Any]:
        stored = self.storage.get_runtime_value(PREFERENCES_KEY, {})
        return _normalize_preferences({**DEFAULT_PREFERENCES, **_dict(stored)})

    def update_preferences(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.preferences()
        allowed = {key: value for key, value in patch.items() if key in DEFAULT_PREFERENCES}
        updated = _normalize_preferences({**current, **allowed})
        self.storage.set_runtime_value(PREFERENCES_KEY, updated)
        self.storage.record_audit(
            actor="operator",
            action="preferences.update",
            target_type="runtime",
            target_id=PREFERENCES_KEY,
            summary="Operator preferences updated",
            before=current,
            after=updated,
        )
        return updated

    def autonomy_policy(self) -> dict[str, Any]:
        stored = self.storage.get_runtime_value(AUTONOMY_POLICY_KEY, {})
        merged = _merge_policy(DEFAULT_AUTONOMY_POLICY, _dict(stored))
        return _normalize_policy(merged)

    def update_autonomy_policy(self, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.autonomy_policy()
        allowed = {key: value for key, value in patch.items() if key in DEFAULT_AUTONOMY_POLICY}
        if "mode" in allowed:
            current = _merge_policy(
                current,
                {"mode": allowed["mode"], **_mode_preset(allowed["mode"])},
            )
        updated = _normalize_policy(_merge_policy(current, allowed))
        self.storage.set_runtime_value(AUTONOMY_POLICY_KEY, updated)
        self.storage.record_audit(
            actor="operator",
            action="autonomy.policy.update",
            target_type="runtime",
            target_id=AUTONOMY_POLICY_KEY,
            summary=f"Autonomy policy set to {updated['mode']}",
            before=current,
            after=updated,
        )
        return updated

    def daily_briefing(self, dispatcher_status: dict[str, Any] | None = None) -> dict[str, Any]:
        preferences = self.preferences()
        policy = self.autonomy_policy()
        health = self.storage.latest_health(limit=16)
        issues = [item for item in health if item.get("status") in {"warn", "error"}]
        pending_approvals = self.storage.list_approvals(limit=50, status="pending")
        latest_telemetry = _latest_telemetry_snapshot(self.storage)
        resources = _resource_summary(latest_telemetry)
        counters = self.storage.counters()
        persona = persona_module.load_persona(self.storage)
        focus = [
            f"Focus: {item}" for item in list(persona.get("current_focus") or [])[:2]
        ]
        focus.extend(
            [
                f"Profile {self.settings.profile.name} on {self.settings.llm_base_url}",
                f"{counters.get('missions', 0)} missions, {counters.get('memories', 0)} "
                f"memories, {counters.get('files', 0)} files",
            ]
        )
        if dispatcher_status is not None:
            focus.append(
                "Dispatcher online"
                if dispatcher_status.get("port_open")
                else "Dispatcher port is offline"
            )
        if latest_telemetry:
            focus.append(
                "RAM "
                f"{_ratio_label(resources.get('memory_used_ratio'))}, GPU "
                f"{_ratio_label(resources.get('gpu_memory_used_ratio'))}"
            )

        risks = [
            f"{item.get('name') or item.get('component')}: {item.get('message')}"
            for item in issues[:4]
        ]
        suggestions = _briefing_suggestions(
            pending_approvals=len(pending_approvals),
            issues=issues,
            resources=resources,
            policy=policy,
            dispatcher_status=dispatcher_status,
        )
        headline = (
            "Runtime needs attention"
            if issues or pending_approvals
            else "Runtime is stable"
        )
        return {
            "ts": utc_now(),
            "operator_name": preferences["operator_name"],
            "profile": self.settings.profile.name,
            "home": str(self.settings.home),
            "headline": headline,
            "focus": focus[:6],
            "risks": risks,
            "suggestions": suggestions,
            "pending_approvals": len(pending_approvals),
            "policy_mode": policy["mode"],
            "counters": counters,
            "resources": resources,
            "recent_events": self.storage.list_events(limit=5),
        }

    def self_heal_report(
        self,
        *,
        checks: list[DiagnosticCheck],
        telemetry_snapshot: dict[str, Any] | None = None,
        dispatcher_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        check_items = [check.model_dump() for check in checks]
        issues = [
            {
                "check": item["name"],
                "status": item["status"],
                "message": item["message"],
                "details": item.get("details", {}),
            }
            for item in check_items
            if item.get("status") in {"warn", "error"}
        ]
        resources = _resource_summary(telemetry_snapshot)
        actions = _dedupe_actions(
            [
                *(_actions_for_issue(issue) for issue in issues),
                *_actions_for_resources(resources, self.autonomy_policy()),
                *_actions_for_dispatcher(dispatcher_status),
            ]
        )
        ok = not issues and not actions
        report = {
            "ts": utc_now(),
            "ok": ok,
            "summary": (
                "No repair action is needed."
                if ok
                else f"{len(issues)} issue(s), {len(actions)} suggested action(s)."
            ),
            "issues": issues,
            "actions": actions,
            "checks": check_items,
        }
        self.storage.set_runtime_value("experience.self_heal.latest", report)
        self.storage.add_event(
            kind="self_heal.scan",
            title=report["summary"],
            level="info" if ok else "warn",
            payload={"issues": len(issues), "actions": len(actions)},
        )
        return report

    async def run_benchmark(
        self,
        *,
        llm: Any,
        telemetry: Any,
        dispatcher: Any,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        metrics: dict[str, Any] = {}

        lap = time.perf_counter()
        self.storage.ping()
        metrics["storage_ping_ms"] = _elapsed_ms(lap)

        lap = time.perf_counter()
        counters = self.storage.counters()
        metrics["counters_ms"] = _elapsed_ms(lap)

        lap = time.perf_counter()
        telemetry_snapshot = await asyncio.to_thread(telemetry.snapshot)
        self.storage.record_telemetry(telemetry_snapshot)
        metrics["telemetry_ms"] = _elapsed_ms(lap)

        lap = time.perf_counter()
        dispatcher_status = await asyncio.to_thread(dispatcher.status)
        metrics["dispatcher_status_ms"] = _elapsed_ms(lap)

        lap = time.perf_counter()
        llm_health = await llm.health()
        metrics["llm_health_ms"] = _elapsed_ms(lap)

        lap = time.perf_counter()
        benchmark_inference = getattr(llm, "benchmark_inference", None)
        if callable(benchmark_inference) and llm_health.get("ok"):
            try:
                inference = await benchmark_inference(
                    runs=3,
                    max_tokens=64,
                    timeout_sec=30.0,
                )
            except Exception as exc:  # noqa: BLE001 - benchmark reports failure structurally
                inference = {
                    "ok": False,
                    "requested_runs": 3,
                    "successful_runs": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "runs": [],
                    "aggregate": {},
                }
        else:
            inference = {
                "ok": False,
                "available": False,
                "error": "Inference benchmark is unavailable or LLM health failed.",
                "runs": [],
                "aggregate": {},
            }
        metrics["inference_ms"] = _elapsed_ms(lap)
        metrics["total_ms"] = _elapsed_ms(started)
        metrics["runtime_records"] = sum(int(value) for value in counters.values())

        compact_telemetry = _resource_summary(telemetry_snapshot)
        compact_dispatcher = _compact_dispatcher(dispatcher_status)
        compact_llm = _compact_llm_health(llm_health)
        report = {
            "ts": utc_now(),
            "profile": self.settings.profile.name,
            "summary": _benchmark_summary(
                compact_dispatcher,
                compact_llm,
                compact_telemetry,
                inference,
            ),
            "metrics": metrics,
            "telemetry": compact_telemetry,
            "dispatcher": compact_dispatcher,
            "llm": compact_llm,
            "inference": inference,
            "recommendations": _benchmark_recommendations(
                telemetry=compact_telemetry,
                dispatcher=compact_dispatcher,
                llm=compact_llm,
                inference=inference,
                policy=self.autonomy_policy(),
            ),
        }
        history = self.storage.get_runtime_value(BENCHMARK_HISTORY_KEY, [])
        compact = _compact_benchmark(report)
        updated_history = [compact, *_list(history)][:20]
        self.storage.set_runtime_value(BENCHMARK_LATEST_KEY, report)
        self.storage.set_runtime_value(BENCHMARK_HISTORY_KEY, updated_history)
        self.storage.add_event(
            kind="performance.benchmark",
            title=report["summary"],
            payload={"metrics": metrics, "recommendations": len(report["recommendations"])},
        )
        return {**report, "history": updated_history}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _normalize_preferences(value: dict[str, Any]) -> dict[str, Any]:
    style = value.get("communication_style")
    if style not in {"concise", "balanced", "detailed"}:
        style = DEFAULT_PREFERENCES["communication_style"]
    profile = value.get("preferred_profile")
    if profile not in {
        "gemma4-turbo",
        "gemma4-mono",
        "gemma4-mono-perf",
    }:
        profile = DEFAULT_PREFERENCES["preferred_profile"]
    return {
        "operator_name": str(value.get("operator_name") or "Admin")[:80],
        "communication_style": style,
        "daily_briefing": bool(value.get("daily_briefing", True)),
        "voice_reply": bool(value.get("voice_reply", False)),
        "preferred_profile": profile,
        "quiet_hours": str(value.get("quiet_hours") or "")[:80],
        "working_roots": _clean_string_list(value.get("working_roots"), r"D:\jarvis"),
    }


def _normalize_policy(value: dict[str, Any]) -> dict[str, Any]:
    mode = value.get("mode")
    if mode not in POLICY_PRESETS:
        mode = DEFAULT_AUTONOMY_POLICY["mode"]
    resource_guard = _dict(value.get("resource_guard"))
    return {
        "mode": mode,
        "allow_safe_tools": bool(value.get("allow_safe_tools", True)),
        "allow_review_tools": bool(value.get("allow_review_tools", False)),
        "allow_danger_tools": bool(value.get("allow_danger_tools", False)),
        "allow_background_learning": bool(value.get("allow_background_learning", True)),
        "allow_self_healing_suggestions": bool(
            value.get("allow_self_healing_suggestions", True)
        ),
        "approval_required_for": _clean_string_list(
            value.get("approval_required_for"),
            "execution.apply",
            limit=20,
        ),
        "max_autonomous_steps": _bounded_int(value.get("max_autonomous_steps"), 1, 24, 3),
        "resource_guard": {
            "max_memory_ratio": _bounded_float(
                resource_guard.get("max_memory_ratio"),
                0.5,
                0.99,
                0.92,
            ),
            "max_gpu_memory_ratio": _bounded_float(
                resource_guard.get("max_gpu_memory_ratio"),
                0.5,
                0.99,
                0.9,
            ),
        },
    }


def _merge_policy(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = {**base}
    for key, value in patch.items():
        if key == "resource_guard" and isinstance(value, dict):
            merged[key] = {**_dict(merged.get(key)), **value}
        else:
            merged[key] = value
    return merged


def _mode_preset(mode: Any) -> dict[str, Any]:
    return POLICY_PRESETS.get(str(mode), {})


def _clean_string_list(value: Any, fallback: str, *, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return [fallback]
    cleaned: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text[:240])
        if len(cleaned) >= limit:
            break
    return cleaned or [fallback]


def _bounded_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def _bounded_float(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, number))


def _latest_telemetry_snapshot(storage: JarvisStorage) -> dict[str, Any] | None:
    rows = storage.list_telemetry(limit=1)
    if not rows:
        return None
    snapshot = rows[0].get("snapshot")
    return snapshot if isinstance(snapshot, dict) else None


def _resource_summary(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {}
    memory = _dict(snapshot.get("memory"))
    gpu = _dict(snapshot.get("gpu"))
    gpus = _list(gpu.get("gpus"))
    first_gpu = _dict(gpus[0]) if gpus else {}
    disks = _list(snapshot.get("disks"))
    tightest_disk = max(
        (_dict(item) for item in disks),
        key=lambda item: float(item.get("used_ratio") or 0),
        default={},
    )
    return {
        "memory_used_ratio": memory.get("used_ratio"),
        "memory_available": memory.get("available"),
        "gpu_available": bool(gpu.get("available")),
        "gpu_name": first_gpu.get("name"),
        "gpu_memory_used_ratio": first_gpu.get("memory_used_ratio"),
        "gpu_utilization": first_gpu.get("utilization_gpu"),
        "disk_path": tightest_disk.get("path"),
        "disk_used_ratio": tightest_disk.get("used_ratio"),
    }


def _briefing_suggestions(
    *,
    pending_approvals: int,
    issues: list[dict[str, Any]],
    resources: dict[str, Any],
    policy: dict[str, Any],
    dispatcher_status: dict[str, Any] | None,
) -> list[str]:
    suggestions: list[str] = []
    if pending_approvals:
        suggestions.append(f"Review {pending_approvals} pending approval gate(s).")
    if issues:
        suggestions.append("Run self-heal scan before autonomous execution.")
    if dispatcher_status is not None and not dispatcher_status.get("port_open"):
        suggestions.append("Start or inspect dispatcher before LLM-heavy work.")
    if _ratio(resources.get("memory_used_ratio")) > _ratio(
        policy["resource_guard"]["max_memory_ratio"]
    ):
        suggestions.append("Memory pressure is above policy guard; reduce background load.")
    if not suggestions:
        suggestions.append("Continue with balanced autonomy and periodic learning.")
    return suggestions[:5]


def _actions_for_issue(issue: dict[str, Any]) -> dict[str, Any]:
    name = str(issue.get("check") or "")
    if name == "llm.router":
        return _action(
            "dispatcher.inspect",
            "Inspect dispatcher logs",
            "review",
            "LLM router is not healthy.",
            {"tool": "dispatcher.logs"},
        )
    if name == "docker":
        return _action(
            "docker.check",
            "Check Docker Desktop",
            "review",
            "Docker is required for the local LLM dispatcher.",
            {"command": "docker version"},
        )
    if name == "models.profile":
        return _action(
            "models.verify",
            "Verify model directory",
            "review",
            "The active profile model was not found or is incomplete.",
            {"path": "D:/jarvis/data/models"},
        )
    if name == "storage.sqlite":
        return _action(
            "storage.backup",
            "Back up SQLite state",
            "review",
            "Storage reported an error and should be copied before repair.",
            {"path": "D:/jarvis/data/jarvis-gpt/state/jarvis.sqlite3"},
        )
    return _action(
        f"diagnostics.{name or 'check'}",
        "Review diagnostic check",
        "safe",
        str(issue.get("message") or "Diagnostic check needs attention."),
        {"check": name},
    )


def _actions_for_resources(
    resources: dict[str, Any],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    guard = _dict(policy.get("resource_guard"))
    if _ratio(resources.get("memory_used_ratio")) > _ratio(guard.get("max_memory_ratio")):
        actions.append(
            _action(
                "resources.memory",
                "Reduce memory pressure",
                "review",
                "RAM usage is above the autonomy policy guard.",
                {"memory_used_ratio": resources.get("memory_used_ratio")},
            )
        )
    if _ratio(resources.get("gpu_memory_used_ratio")) > _ratio(guard.get("max_gpu_memory_ratio")):
        actions.append(
            _action(
                "resources.gpu",
                "Reduce GPU memory pressure",
                "review",
                "GPU memory usage is above the autonomy policy guard.",
                {"gpu_memory_used_ratio": resources.get("gpu_memory_used_ratio")},
            )
        )
    return actions


def _actions_for_dispatcher(dispatcher_status: dict[str, Any] | None) -> list[dict[str, Any]]:
    if dispatcher_status is None or dispatcher_status.get("port_open"):
        return []
    return [
        _action(
            "dispatcher.start",
            "Start dispatcher",
            "review",
            "Dispatcher port is offline.",
            {"approval_action": "dispatcher.start"},
        )
    ]


def _action(
    action_id: str,
    label: str,
    risk: str,
    reason: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": action_id,
        "label": label,
        "kind": "approval" if risk != "safe" else "safe",
        "risk": risk,
        "reason": reason,
        "payload": payload,
    }


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for action in actions:
        action_id = str(action.get("id") or "")
        if action_id in seen:
            continue
        seen.add(action_id)
        deduped.append(action)
    return deduped


def _compact_dispatcher(status: dict[str, Any]) -> dict[str, Any]:
    container = _dict(status.get("container_status"))
    return {
        "port_open": bool(status.get("port_open")),
        "base_url": status.get("base_url"),
        "model": status.get("model"),
        "container_exists": bool(container.get("exists")),
        "container_status": container.get("status"),
    }


def _compact_llm_health(health: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(health.get("ok")),
        "disabled": bool(health.get("disabled")),
        "status_code": health.get("status_code"),
        "served_models": _list(health.get("served_models"))[:8],
        "configured_model": health.get("configured_model"),
        "error": health.get("error") or health.get("message"),
    }


def _benchmark_summary(
    dispatcher: dict[str, Any],
    llm: dict[str, Any],
    telemetry: dict[str, Any],
    inference: dict[str, Any],
) -> str:
    if not dispatcher.get("port_open"):
        return "Benchmark completed with dispatcher offline."
    if not llm.get("ok"):
        return "Benchmark completed with LLM warning."
    if not inference.get("ok"):
        return "Runtime health passed, but bounded inference benchmark failed."
    if not telemetry:
        return "Benchmark completed without telemetry."
    aggregate = _dict(inference.get("aggregate"))
    ttft = aggregate.get("ttft_ms_p50")
    decode = aggregate.get("decode_tokens_per_sec_p50")
    if ttft is not None and decode is not None:
        return f"Inference p50: TTFT {ttft} ms, decode {decode} tok/s."
    return "Runtime and bounded inference benchmark are healthy."


def _benchmark_recommendations(
    *,
    telemetry: dict[str, Any],
    dispatcher: dict[str, Any],
    llm: dict[str, Any],
    inference: dict[str, Any],
    policy: dict[str, Any],
) -> list[str]:
    recommendations: list[str] = []
    if not dispatcher.get("port_open"):
        recommendations.append("Start dispatcher before LLM latency checks.")
    if not llm.get("ok"):
        recommendations.append("Inspect LLM health and dispatcher logs.")
    elif not inference.get("ok"):
        recommendations.append(
            "Inspect per-run inference errors; endpoint health alone does not prove usable latency."
        )
    guard = _dict(policy.get("resource_guard"))
    if _ratio(telemetry.get("memory_used_ratio")) > _ratio(guard.get("max_memory_ratio")):
        recommendations.append("Lower background load or switch to the safe profile.")
    if _ratio(telemetry.get("gpu_memory_used_ratio")) > _ratio(guard.get("max_gpu_memory_ratio")):
        recommendations.append("Reduce GPU memory pressure before long missions.")
    if not recommendations:
        recommendations.append("Current profile is inside the configured resource guard.")
    return recommendations[:5]


def _compact_benchmark(report: dict[str, Any]) -> dict[str, Any]:
    metrics = _dict(report.get("metrics"))
    inference = _dict(report.get("inference"))
    aggregate = _dict(inference.get("aggregate"))
    return {
        "ts": report.get("ts"),
        "profile": report.get("profile"),
        "summary": report.get("summary"),
        "total_ms": metrics.get("total_ms"),
        "llm_health_ms": metrics.get("llm_health_ms"),
        "inference_ms": metrics.get("inference_ms"),
        "ttft_ms_p50": aggregate.get("ttft_ms_p50"),
        "decode_tokens_per_sec_p50": aggregate.get("decode_tokens_per_sec_p50"),
        "dispatcher_online": _dict(report.get("dispatcher")).get("port_open"),
        "llm_ok": _dict(report.get("llm")).get("ok"),
        "inference_ok": inference.get("ok"),
    }


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _ratio(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ratio_label(value: Any) -> str:
    return f"{round(_ratio(value) * 100)}%"
