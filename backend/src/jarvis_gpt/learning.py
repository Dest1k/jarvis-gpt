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
        lessons = self._derive_lessons(audit=audit, tool_runs=tool_runs, approvals=approvals)
        saved = []
        for lesson in lessons:
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
            payload={"saved": len(saved), "examined": len(audit) + len(tool_runs) + len(approvals)},
        )
        return {
            "saved": saved,
            "lesson_count": len(saved),
            "examined": {
                "audit": len(audit),
                "tool_runs": len(tool_runs),
                "approvals": len(approvals),
            },
        }

    def _derive_lessons(
        self,
        *,
        audit: list[dict[str, Any]],
        tool_runs: list[dict[str, Any]],
        approvals: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        lessons: list[dict[str, Any]] = []
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
