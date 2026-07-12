#!/usr/bin/env python3
"""
Document Agent - Continued dense iteration

Improved structure.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Any, Optional


class DocumentGenerationRequest:
    def __init__(self, task, source_files=None, web_research_ids=None, memory_query=None, output_format="docx"):
        self.task = task
        self.source_files = source_files or []
        self.web_research_ids = web_research_ids or []
        self.memory_query = memory_query
        self.output_format = output_format


class GeneratedDocument:
    def __init__(self, output_path, format, summary, key_sections, citations=None):
        self.output_path = output_path
        self.format = format
        self.summary = summary
        self.key_sections = key_sections
        self.citations = citations or []


@dataclass
class DocumentAgentConfig:
    enable_vision: bool = True


class DocumentAgent:
    def __init__(self, config=None):
        self.config = config or DocumentAgentConfig()

    def generate(self, req: DocumentGenerationRequest) -> GeneratedDocument:
        ctx = f"Task: {req.task}\nFiles: {len(req.source_files)}\nWeb: {len(req.web_research_ids)}"
        summary = f"Generated {req.output_format} for {req.task}\nContext: {ctx}\n[Real LLM + render would go here]"

        out_dir = Path("D:/jarvis/data/document-outputs")
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = req.task[:40].replace(" ", "_") + f".{req.output_format}"
        out_path = str(out_dir / fname)
        Path(out_path).write_text(summary, encoding="utf-8")

        return GeneratedDocument(out_path, req.output_format, summary[:300], ["Summary", "Analysis"], req.web_research_ids)

    def summarize_corpus(self, files, focus=None):
        return {"files": len(files), "summary": f"Focus: {focus}", "entities": 12}

    def build_knowledge_graph(self, files):
        return {"nodes": len(files)*4, "edges": len(files)*2}


def get_document_agent_tools():
    a = DocumentAgent()
    return {
        "documents.generate": a.generate,
        "documents.summarize_corpus": a.summarize_corpus,
        "documents.build_knowledge_graph": a.build_knowledge_graph,
    }

print("[document_agent.py] Updated.")