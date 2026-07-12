from __future__ import annotations

import json
from typing import Any

from .llm import LLMRouter, background_llm_priority
from .storage import JarvisStorage


class LearningEngine:
    def __init__(self, storage: JarvisStorage, llm: LLMRouter | None = None) -> None:
        self.storage = storage
        self.llm = llm

    def tick(self, *, limit: int = 20) -> dict[str, Any]:
        inputs = self._collect_inputs(limit=limit)
        lessons = self._derive_lessons(**inputs)
        return self._save_tick(lessons, inputs=inputs, limit=limit)

    async def tick_async(self, *, limit: int = 20) -> dict[str, Any]:
        inputs = self._collect_inputs(limit=limit)
        lessons = self._derive_lessons(**inputs)
        lessons.extend(await self._distill_lessons(inputs=inputs, deterministic=lessons))
        return self._save_tick(lessons, inputs=inputs, limit=limit)

    def _collect_inputs(self, *, limit: int) -> dict[str, list[dict[str, Any]]]:
        audit = self.storage.list_audit(limit=limit)
        tool_runs = self.storage.list_tool_runs(limit=limit)
        approvals = self.storage.list_approvals(limit=limit)
        observations = self.storage.list_learning_observations(limit=max(50, limit * 4))
        return {
            "audit": audit,
            "tool_runs": tool_runs,
            "approvals": approvals,
            "observations": observations,
        }

    def _save_tick(
        self,
        lessons: list[dict[str, Any]],
        *,
        inputs: dict[str, list[dict[str, Any]]],
        limit: int,
    ) -> dict[str, Any]:
        consolidation = self.storage.consolidate_memories(limit=max(200, limit * 20))
        saved = []
        skipped_duplicates = 0
        for lesson in lessons:
            if self._lesson_exists(lesson["content"]):
                skipped_duplicates += 1
                continue
            saved.append(
                self.storage.add_memory(
                    content=lesson["content"],
                    namespace="learning",
                    tags=lesson["tags"],
                    importance=lesson["importance"],
                )
            )
        self.storage.add_event(
            kind="learning.tick",
            title=f"Learning tick saved {len(saved)} lesson(s)",
            payload={
                "saved": len(saved),
                "skipped_duplicates": skipped_duplicates,
                "examined": sum(len(items) for items in inputs.values()),
            },
        )
        return {
            "saved": saved,
            "lesson_count": len(saved),
            "skipped_duplicates": skipped_duplicates,
            "consolidated": consolidation,
            "examined": {
                "audit": len(inputs["audit"]),
                "tool_runs": len(inputs["tool_runs"]),
                "approvals": len(inputs["approvals"]),
                "learning_observations": len(inputs["observations"]),
            },
        }

    def _lesson_exists(self, content: str) -> bool:
        existing = self.storage.search_memory(content[:180], limit=20)
        return any(
            item.get("namespace") == "learning" and item.get("content") == content
            for item in existing
        )

    async def _distill_lessons(
        self,
        *,
        inputs: dict[str, list[dict[str, Any]]],
        deterministic: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self.llm is None or not self.llm.settings.llm_enabled:
            return []
        signal_text = _learning_signal_text(inputs=inputs, deterministic=deterministic)
        if not signal_text:
            return []
        with background_llm_priority(self.llm):
            result = await self.llm.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            "You distill operator/runtime feedback into durable behavioral lessons "
                            "for a local assistant. Return strict JSON only: "
                            '{"lessons":[{"content":"...","tags":["learning","distilled"],'
                            '"importance":0.0}]}. Keep at most 2 lessons, each actionable, '
                            "short, non-secret, and grounded only in the provided signals."
                        ),
                    },
                    {"role": "user", "content": signal_text},
                ],
                temperature=0.1,
                max_tokens=700,
                thinking_enabled=False,
            )
        if not result.ok:
            return []
        try:
            parsed = json.loads(_json_object_text(result.content))
        except (json.JSONDecodeError, ValueError):
            return []
        raw_lessons = parsed.get("lessons") if isinstance(parsed, dict) else None
        if not isinstance(raw_lessons, list):
            return []
        lessons: list[dict[str, Any]] = []
        for raw in raw_lessons[:2]:
            if not isinstance(raw, dict):
                continue
            content = _compact_text(str(raw.get("content") or ""), 500)
            if len(content) < 24:
                continue
            tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
            clean_tags = ["learning", "distilled"]
            for tag in tags[:6]:
                text = str(tag).strip().lower()[:40]
                if text and text not in clean_tags:
                    clean_tags.append(text)
            lessons.append(
                {
                    "content": content,
                    "tags": clean_tags,
                    "importance": _bounded_float(raw.get("importance"), 0.7, 0.5, 0.92),
                }
            )
        return lessons

    def _derive_lessons(
        self,
        *,
        audit: list[dict[str, Any]],
        tool_runs: list[dict[str, Any]],
        approvals: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        lessons: list[dict[str, Any]] = []

        # Outcome signals come first: they carry the operator's own judgement
        # (feedback), the self-check's findings and explicit approval denials,
        # so they must survive the lesson cap ahead of generic activity notes.
        negative_feedback = [
            item
            for item in observations
            if item.get("kind") == "operator.feedback"
            and isinstance(item.get("payload"), dict)
            and item["payload"].get("rating") == "down"
        ]
        if negative_feedback:
            fragments = []
            for item in negative_feedback[:3]:
                payload = item["payload"]
                excerpt = _compact_text(str(item.get("content") or ""), 110)
                comment = _compact_text(str(payload.get("comment") or ""), 110)
                fragments.append(f"«{excerpt}»" + (f" — оператор: {comment}" if comment else ""))
            lessons.append(
                {
                    "content": (
                        "Оператор отметил ответы как неудачные; избегай повторения в похожих "
                        "задачах: " + " | ".join(fragments)
                    ),
                    "tags": ["learning", "feedback", "operator"],
                    "importance": 0.9,
                }
            )

        positive_feedback = [
            item
            for item in observations
            if item.get("kind") == "operator.feedback"
            and isinstance(item.get("payload"), dict)
            and item["payload"].get("rating") == "up"
            and str(item["payload"].get("comment") or "").strip()
        ]
        if positive_feedback:
            fragments = [
                _compact_text(str(item["payload"].get("comment") or ""), 130)
                for item in positive_feedback[:3]
            ]
            lessons.append(
                {
                    "content": (
                        "Оператор явно похвалил такие ответы — воспроизводи этот стиль/подход: "
                        + " | ".join(fragments)
                    ),
                    "tags": ["learning", "feedback", "operator"],
                    "importance": 0.68,
                }
            )

        revises = [
            item
            for item in observations
            if item.get("kind") == "verification.revise"
        ]
        if revises:
            gaps: list[str] = []
            for item in revises[:4]:
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                missing = payload.get("missing")
                if isinstance(missing, list) and missing:
                    gaps.extend(_compact_text(str(gap), 100) for gap in missing[:2])
                else:
                    gaps.append(_compact_text(str(item.get("summary") or ""), 100))
            gaps = list(dict.fromkeys(gap for gap in gaps if gap))[:5]
            if gaps:
                lessons.append(
                    {
                        "content": (
                            "Самопроверка регулярно находит одни и те же пробелы — закрывай их "
                            "сразу в черновике: " + " | ".join(gaps)
                        ),
                        "tags": ["learning", "verification", "quality"],
                        "importance": 0.74,
                    }
                )

        rejected = [item for item in approvals if item.get("status") == "rejected"]
        if rejected:
            titles = [
                _compact_text(str(item.get("title") or ""), 110)
                for item in rejected[:3]
                if str(item.get("title") or "").strip()
            ]
            if titles:
                lessons.append(
                    {
                        "content": (
                            "Оператор отклонил эти approval-гейты — не предлагай такие действия "
                            "повторно без новых оснований: " + " | ".join(titles)
                        ),
                        "tags": ["learning", "approval", "operator"],
                        "importance": 0.8,
                    }
                )

        dialogue = [
            item
            for item in observations
            if item.get("kind") == "conversation.message" and item.get("role") == "user"
        ]
        if dialogue:
            excerpts = [
                _compact_text(str(item.get("content") or ""), 120)
                for item in dialogue[:5]
                if str(item.get("content") or "").strip()
            ]
            if excerpts:
                lessons.append(
                    {
                        "content": (
                            "Recent operator dialogue themes to preserve beyond visible "
                            "chat history: "
                            + " | ".join(excerpts)
                        ),
                        "tags": ["learning", "dialogue", "operator"],
                        "importance": 0.62,
                    }
                )

        web_observations = [
            item
            for item in observations
            if str(item.get("kind") or "").startswith(("tool.web.", "tool.browser."))
        ]
        if web_observations:
            fragments = []
            for item in web_observations[:5]:
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                arguments = (
                    payload.get("arguments")
                    if isinstance(payload.get("arguments"), dict)
                    else {}
                )
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                target = (
                    arguments.get("query")
                    or arguments.get("url")
                    or data.get("url")
                    or item.get("summary")
                )
                if target:
                    fragments.append(_compact_text(str(target), 140))
            if fragments:
                lessons.append(
                    {
                        "content": (
                            "Recent web/browser activity useful for future context: "
                            + " | ".join(fragments)
                        ),
                        "tags": ["learning", "web", "browser"],
                        "importance": 0.64,
                    }
                )

        failed_tools = [run for run in tool_runs if not run.get("ok")]
        if failed_tools:
            names = sorted({str(run.get("tool")) for run in failed_tools})
            lessons.append(
                {
                    "content": (
                        "Recent failed tools should be checked before autonomous execution: "
                        + ", ".join(names)
                    ),
                    "tags": ["learning", "tools", "failure"],
                    "importance": 0.72,
                }
            )

        pending = [item for item in approvals if item.get("status") == "pending"]
        if pending:
            lessons.append(
                {
                    "content": (
                        f"There are {len(pending)} pending HITL approval gate(s); "
                        "do not execute their requested actions until operator decision."
                    ),
                    "tags": ["learning", "approval", "hitl"],
                    "importance": 0.85,
                }
            )

        ingests = [item for item in audit if item.get("action") == "file.ingest"]
        if ingests:
            latest = ingests[0]
            lessons.append(
                {
                    "content": (
                        "Latest ingested file context is available through files.search: "
                        f"{latest.get('summary')}"
                    ),
                    "tags": ["learning", "files", "rag"],
                    "importance": 0.66,
                }
            )

        if not lessons and audit:
            lessons.append(
                {
                    "content": (
                        "Recent runtime activity was stable; continue using diagnostics, "
                        "telemetry and audit before broad changes."
                    ),
                    "tags": ["learning", "stability"],
                    "importance": 0.55,
                }
            )
        return lessons[:6]


def _compact_text(value: str, max_chars: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}..."


def _learning_signal_text(
    *,
    inputs: dict[str, list[dict[str, Any]]],
    deterministic: list[dict[str, Any]],
) -> str:
    lines = ["Recent deterministic lessons:"]
    for item in deterministic[:6]:
        lines.append(f"- {_compact_text(str(item.get('content') or ''), 260)}")
    lines.append("Recent observations:")
    for item in inputs.get("observations", [])[:20]:
        kind = str(item.get("kind") or "")
        summary = _compact_text(str(item.get("summary") or item.get("content") or ""), 220)
        if summary:
            lines.append(f"- {kind}: {summary}")
    lines.append("Recent failed tools / approvals:")
    for item in inputs.get("tool_runs", [])[:12]:
        if not item.get("ok"):
            lines.append(
                f"- tool {item.get('tool')}: {_compact_text(str(item.get('summary') or ''), 180)}"
            )
    for item in inputs.get("approvals", [])[:12]:
        if item.get("status") in {"rejected", "pending"}:
            lines.append(
                f"- approval {item.get('status')}: "
                f"{_compact_text(str(item.get('title') or ''), 180)}"
            )
    text = "\n".join(line for line in lines if line.strip())
    return text[:5000]


def _json_object_text(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("No JSON object found")
    return text[start : end + 1]


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
