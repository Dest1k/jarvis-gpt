from __future__ import annotations

from typing import Any

from .storage import JarvisStorage


class LearningEngine:
    def __init__(self, storage: JarvisStorage) -> None:
        self.storage = storage

    def tick(self, *, limit: int = 20) -> dict[str, Any]:
        audit = self.storage.list_audit(limit=limit)
        tool_runs = self.storage.list_tool_runs(limit=limit)
        approvals = self.storage.list_approvals(limit=limit)
        observations = self.storage.list_learning_observations(limit=max(50, limit * 4))
        lessons = self._derive_lessons(
            audit=audit,
            tool_runs=tool_runs,
            approvals=approvals,
            observations=observations,
        )
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
                "examined": len(audit) + len(tool_runs) + len(approvals) + len(observations),
            },
        )
        return {
            "saved": saved,
            "lesson_count": len(saved),
            "skipped_duplicates": skipped_duplicates,
            "consolidated": consolidation,
            "examined": {
                "audit": len(audit),
                "tool_runs": len(tool_runs),
                "approvals": len(approvals),
                "learning_observations": len(observations),
            },
        }

    def _lesson_exists(self, content: str) -> bool:
        existing = self.storage.search_memory(content[:180], limit=20)
        return any(
            item.get("namespace") == "learning" and item.get("content") == content
            for item in existing
        )

    def _derive_lessons(
        self,
        *,
        audit: list[dict[str, Any]],
        tool_runs: list[dict[str, Any]],
        approvals: list[dict[str, Any]],
        observations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        lessons: list[dict[str, Any]] = []
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
        return lessons[:5]


def _compact_text(value: str, max_chars: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}..."
