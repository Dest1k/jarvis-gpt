from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from .config import PROFILES, JarvisSettings
from .storage import JarvisStorage, utc_now


def operator_queue_snapshot(settings: JarvisSettings, storage: JarvisStorage) -> dict[str, Any]:
    memory_hygiene = memory_hygiene_report(storage)
    model_profiles = model_profile_plan(settings)
    items: list[dict[str, Any]] = []

    for approval in storage.list_approvals(limit=30):
        status = str(approval.get("status") or "")
        if status not in {"pending", "approved"}:
            continue
        payload = approval.get("payload") if isinstance(approval.get("payload"), dict) else {}
        mission_id = payload.get("mission_id") if isinstance(payload, dict) else None
        task_id = payload.get("task_id") if isinstance(payload, dict) else None
        items.append(
            {
                "id": f"approval:{approval['id']}",
                "kind": "approval",
                "status": status,
                "title": str(approval.get("title") or "Approval"),
                "detail": str(approval.get("description") or ""),
                "priority": "high" if approval.get("risk") == "danger" else "medium",
                "action": "approve" if status == "pending" else "execute",
                "updated_at": approval.get("updated_at"),
                "payload": {
                    "approval_id": approval["id"],
                    "risk": approval.get("risk"),
                    "requested_action": approval.get("requested_action"),
                    "mission_id": mission_id,
                    "task_id": task_id,
                },
            }
        )

    for mission in storage.list_missions(limit=20):
        for task in mission.get("tasks", []):
            status = str(task.get("status") or "")
            if status == "blocked":
                items.append(
                    {
                        "id": f"mission:{mission['id']}:{task['id']}",
                        "kind": "mission",
                        "status": status,
                        "title": str(task.get("title") or mission.get("title") or "Mission task"),
                        "detail": str(mission.get("title") or mission.get("goal") or ""),
                        "priority": "high",
                        "action": "resolve_blocker",
                        "updated_at": task.get("updated_at") or mission.get("updated_at"),
                        "payload": {"mission_id": mission["id"], "task_id": task["id"]},
                    }
                )
            elif status == "running":
                items.append(
                    {
                        "id": f"mission:{mission['id']}:{task['id']}",
                        "kind": "mission",
                        "status": status,
                        "title": str(task.get("title") or mission.get("title") or "Mission task"),
                        "detail": str(mission.get("title") or mission.get("goal") or ""),
                        "priority": "medium",
                        "action": "watch",
                        "updated_at": task.get("updated_at") or mission.get("updated_at"),
                        "payload": {"mission_id": mission["id"], "task_id": task["id"]},
                    }
                )
                break

    for check in storage.latest_health(limit=20):
        if check.get("status") not in {"warn", "error"}:
            continue
        component = str(check.get("component") or check.get("name") or "health")
        items.append(
            {
                "id": f"health:{component}",
                "kind": "health",
                "status": str(check.get("status") or "warn"),
                "title": component,
                "detail": str(check.get("message") or ""),
                "priority": "high" if check.get("status") == "error" else "medium",
                "action": "diagnose",
                "updated_at": check.get("ts"),
                "payload": {"details": check.get("details") or {}},
            }
        )

    for event in storage.list_events(limit=30):
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event.get("kind") == "agent.assistant_done" and payload.get("finish_reason") == "length":
            items.append(
                {
                    "id": f"generation:{event['id']}",
                    "kind": "generation",
                    "status": "truncated",
                    "title": "Generation stopped by token limit",
                    "detail": "Auto-continuation did not fully finish this answer.",
                    "priority": "medium",
                    "action": "increase_tokens",
                    "updated_at": event.get("ts"),
                    "payload": payload,
                }
            )
            break

    quality = answer_quality_report(storage)
    if quality["negative_feedback"]:
        latest = quality["negative_feedback"][0]
        payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
        comment = str(payload.get("comment") or "").strip()
        items.append(
            {
                "id": "quality:feedback",
                "kind": "quality",
                "status": "operator-flagged",
                "title": (
                    f"Оператор отметил {len(quality['negative_feedback'])} ответ(а) "
                    "как неудачные"
                ),
                "detail": comment or str(latest.get("content") or "")[:180],
                "priority": "high",
                "action": "review_lessons",
                "updated_at": latest.get("ts"),
                "payload": {
                    "count": len(quality["negative_feedback"]),
                    "message_id": payload.get("message_id"),
                },
            }
        )
    if len(quality["revises"]) >= 3:
        items.append(
            {
                "id": "quality:self-check",
                "kind": "quality",
                "status": "recurring-gaps",
                "title": (
                    f"Самопроверка нашла пробелы в {len(quality['revises'])} недавних ответах"
                ),
                "detail": "; ".join(quality["top_gaps"][:3]),
                "priority": "medium",
                "action": "learning_tick",
                "updated_at": quality["revises"][0].get("ts"),
                "payload": {"count": len(quality["revises"]), "gaps": quality["top_gaps"]},
            }
        )

    if int(memory_hygiene["stats"].get("duplicate_groups", 0)) > 0:
        items.append(
            {
                "id": "memory:hygiene",
                "kind": "memory",
                "status": "review",
                "title": "Memory hygiene review",
                "detail": "; ".join(memory_hygiene["recommendations"][:2]),
                "priority": "low",
                "action": "learning_tick",
                "updated_at": utc_now(),
                "payload": {"stats": memory_hygiene["stats"]},
            }
        )

    if any(item["status"] == "future" for item in model_profiles["profiles"]):
        items.append(
            {
                "id": "model:future-profiles",
                "kind": "model",
                "status": "future",
                "title": "Model role profiles are scaffolded",
                "detail": (
                    "Planner/reviewer roles are documented but inactive until "
                    "stronger hardware is available."
                ),
                "priority": "low",
                "action": "roadmap",
                "updated_at": utc_now(),
                "payload": {"profiles": model_profiles["profiles"]},
            }
        )

    items.sort(key=_queue_sort_key)
    summary = Counter(str(item["kind"]) for item in items)
    summary["total"] = len(items)
    return {
        "summary": dict(summary),
        "context": operator_context(settings, storage),
        "items": items[:25],
        "memory_hygiene": memory_hygiene,
        "model_profiles": model_profiles,
    }


def answer_quality_report(storage: JarvisStorage, *, limit: int = 40) -> dict[str, Any]:
    """Aggregate recent quality signals: operator feedback and failed self-checks."""

    observations = storage.list_learning_observations(limit=limit)
    negative_feedback = [
        item
        for item in observations
        if item.get("kind") == "operator.feedback"
        and isinstance(item.get("payload"), dict)
        and item["payload"].get("rating") == "down"
    ]
    revises = [item for item in observations if item.get("kind") == "verification.revise"]
    gaps: list[str] = []
    for item in revises:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        missing = payload.get("missing")
        if isinstance(missing, list):
            gaps.extend(str(gap)[:120] for gap in missing[:2] if str(gap).strip())
    top_gaps = [gap for gap, _count in Counter(gaps).most_common(5)]
    return {
        "negative_feedback": negative_feedback,
        "revises": revises,
        "top_gaps": top_gaps,
    }


def operator_context(settings: JarvisSettings, storage: JarvisStorage) -> dict[str, Any]:
    preferences = storage.get_runtime_value("experience.preferences", {})
    if not isinstance(preferences, dict):
        preferences = {}
    persona = storage.get_runtime_value("experience.persona", {})
    if not isinstance(persona, dict):
        persona = {}
    missions = storage.list_missions(limit=10)
    pending_approvals = [
        item
        for item in storage.list_approvals(limit=50, status="pending")
        if item.get("status") == "pending"
    ]
    working_roots = preferences.get("working_roots")
    if not isinstance(working_roots, list):
        working_roots = []
    return {
        "now": datetime.now().astimezone().isoformat(timespec="seconds"),
        "jarvis_home": str(settings.home),
        "active_profile": settings.profile.name,
        "llm_model": settings.llm_model,
        "llm_base_url": settings.llm_base_url,
        "operator_name": preferences.get("operator_name") or "Admin",
        "home_location": persona.get("location") or "",
        "working_roots": working_roots,
        "active_missions": sum(
            1 for mission in missions if mission.get("status") in {"active", "running"}
        ),
        "pending_approvals": len(pending_approvals),
    }


def memory_hygiene_report(storage: JarvisStorage, *, limit: int = 1000) -> dict[str, Any]:
    memories = storage.search_memory(None, limit=limit)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    missing_source = 0
    low_confidence = 0
    for item in memories:
        tags = {str(tag).lower() for tag in item.get("tags", [])}
        has_source_tag = any(
            tag.startswith("source:")
            or tag in {"operator", "learning", "mission", "approval"}
            for tag in tags
        )
        if not has_source_tag:
            missing_source += 1
        importance = float(item.get("importance") or 0)
        if any(tag in {"uncertain", "draft", "stale"} for tag in tags) or importance < 0.35:
            low_confidence += 1
        key = (
            str(item.get("namespace") or ""),
            _normalize_memory_text(str(item.get("content") or "")),
        )
        if key[1]:
            groups[key].append(item)
    duplicates = [items for items in groups.values() if len(items) > 1]
    duplicate_groups = [
        {
            "namespace": items[0].get("namespace"),
            "count": len(items),
            "sample": str(items[0].get("content") or "")[:180],
            "ids": [item.get("id") for item in items[:8]],
        }
        for items in duplicates[:8]
    ]
    recommendations: list[str] = []
    if duplicates:
        recommendations.append("Run learning tick or memory consolidation to merge duplicates.")
    if missing_source:
        recommendations.append("Prefer source/confidence tags for durable memories.")
    if low_confidence:
        recommendations.append("Review low-confidence or stale memories before relying on them.")
    if not recommendations:
        recommendations.append("Memory hygiene looks clean.")
    return {
        "stats": {
            "total": len(memories),
            "duplicate_groups": len(duplicates),
            "missing_source": missing_source,
            "low_confidence": low_confidence,
        },
        "recommendations": recommendations,
        "duplicate_groups": duplicate_groups,
    }


def model_profile_plan(settings: JarvisSettings) -> dict[str, Any]:
    profiles = [
        {
            "id": name,
            "title": profile.title,
            "role": "current-runtime" if name == settings.profile.name else "local-alternative",
            "status": "active" if name == settings.profile.name else "available",
            "model_hint": profile.model_dir_name,
            "notes": [
                profile.description,
                f"context={profile.max_model_len}",
                f"temperature={profile.temperature}",
            ],
        }
        for name, profile in sorted(PROFILES.items())
    ]
    profiles.extend(
        [
            {
                "id": "planner-70b-80b",
                "title": "Future Planner",
                "role": "planning, code review, difficult intent arbitration",
                "status": "future",
                "model_hint": "70B/80B instruct model when VRAM/RAM allows",
                "notes": [
                    "Use as slow high-quality planner/reviewer.",
                    "Keep current Gemma profile as executor for routine safe tools.",
                ],
            },
            {
                "id": "executor-fast",
                "title": "Future Fast Executor",
                "role": "short tool loops and UI replies",
                "status": "future",
                "model_hint": "small/fast local instruct model",
                "notes": [
                    "Reserved for routing once multiple live model endpoints are available.",
                ],
            },
        ]
    )
    return {
        "active_profile": settings.profile.name,
        "active_model": settings.profile.model_dir_name,
        "profiles": profiles,
    }


def _queue_sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
    priority_rank = {"high": 0, "medium": 1, "low": 2}
    return (
        priority_rank.get(str(item.get("priority")), 3),
        -_timestamp_value(item.get("updated_at")),
        str(item.get("id") or ""),
    )


def _timestamp_value(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _normalize_memory_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())
