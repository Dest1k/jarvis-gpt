"""Experimental knowledge-graph helpers backed by document corpus signals."""

from __future__ import annotations

from typing import Any

from .document_agent import DocumentAgent


class KnowledgeGraphService:
    def __init__(self) -> None:
        self._agent = DocumentAgent()

    def build_from_files(self, file_paths: list[str]) -> dict[str, Any]:
        return self._agent.build_knowledge_graph(file_paths)

    def status(self) -> dict[str, Any]:
        return {
            "engine": "extractive_entity_cooccurrence",
            "llm_required": False,
            "source": "document_surfer.summarize_corpus",
        }


def get_knowledge_graph_tools() -> dict[str, Any]:
    service = KnowledgeGraphService()
    return {
        "knowledge_graph.build": service.build_from_files,
        "knowledge_graph.status": service.status,
    }
