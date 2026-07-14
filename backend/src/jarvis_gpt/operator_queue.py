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

    autonomy_jobs = storage.get_runtime_value("operations.autonomy.jobs", [])
    if isinstance(autonomy_jobs, list):
        for job in autonomy_jobs[:20]:
            if not isinstance(job, dict):
                continue
            failures = _safe_int(job.get("consecutive_failures"), 0)
            status = str(job.get("status") or "")
            if failures <= 0 and status != "cancelled":
                continue
            items.append(
                {
                    "id": f"autonomy:{job.get('id')}",
                    "kind": "autonomy",
                    "status": status or "unknown",
                    "title": str(job.get("title") or job.get("kind") or "Autonomy job"),
                    "detail": str((job.get("last_result") or {}).get("summary") or ""),
                    "priority": "high" if failures >= 3 else "medium",
                    "action": "review_job",
                    "updated_at": job.get("updated_at") or job.get("last_run_at"),
                    "payload": {
                        "job_id": job.get("id"),
                        "consecutive_failures": failures,
                        "next_run_after": job.get("next_run_after"),
                    },
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
    profiles = []
    for name, profile in sorted(PROFILES.items()):
        if name == settings.profile.name:
            status = "active"
            role = "current-runtime"
        elif profile.interactive_certified:
            status = "available"
            role = "certified-interactive"
        elif profile.certification == "experimental":
            status = "available"
            role = "experimental-research-only"
        else:
            status = "available"
            role = "unsupported-interactive-research-only"
        profiles.append(
            {
                "id": name,
                "title": profile.title,
                "role": role,
                "status": status,
                "model_hint": profile.model_dir_name,
                "certification": profile.certification,
                "interactive_certified": profile.interactive_certified,
                "default_recommended": profile.default_recommended,
                "research_only": profile.research_only,
                "readiness_deadline_sec": profile.readiness_deadline_sec,
                "certification_reason": profile.certification_reason,
                "menu_visible": profile.menu_visible,
                "requires_experimental_opt_in": profile.requires_experimental_opt_in,
                "notes": [
                    profile.description,
                    f"certification={profile.certification}",
                    f"readiness_deadline_sec={profile.readiness_deadline_sec}",
                    f"context={profile.max_model_len}",
                    f"temperature={profile.temperature}",
                    profile.certification_reason,
                ],
            }
        )
    # PROFILE-RESEARCH backlog (not a release blocker).
    profiles.extend(
        [
            {
                "id": "PROFILE-RESEARCH",
                "title": "Profile research backlog",
                "role": "research-backlog",
                "status": "future",
                "model_hint": "future multi-profile quality work",
                "certification": "research",
                "interactive_certified": False,
                "default_recommended": False,
                "research_only": True,
                "readiness_deadline_sec": None,
                "certification_reason": (
                    "Backlog only: do not block release on multi-day 31B tuning."
                ),
                "menu_visible": False,
                "requires_experimental_opt_in": True,
                "notes": [
                    "PROFILE-RESEARCH backlog is not a release blocker.",
                    "Do not download alternate models or engines in remediation.",
                    "Keep gemma4-turbo as certified interactive default.",
                ],
            },
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
        "default_recommended_profile": next(
            (name for name, item in PROFILES.items() if item.default_recommended),
            "gemma4-turbo",
        ),
        "certified_interactive_profiles": [
            name
            for name, item in PROFILES.items()
            if item.interactive_certified and item.certification == "certified"
        ],
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


def _safe_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _normalize_memory_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())
