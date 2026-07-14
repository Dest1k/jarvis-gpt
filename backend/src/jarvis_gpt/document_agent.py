"""High-level document agent facade over :class:`JarvisDocumentSurfer`.

Keeps a stable agent-facing API while the real capability surface lives in
``document_surfer`` (the document analogue of ``web_surfer``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .document_runtime import (
    resolve_artifact_output_path,
    write_exact_text_artifact,
    write_markdown_docx,
)
from .document_surfer import (
    DocumentGenerationError,
    DocumentSurferConfig,
    DocumentSurferError,
    JarvisDocumentSurfer,
)


@dataclass
class DocumentGenerationRequest:
    task: str
    source_files: list[str] = field(default_factory=list)
    web_research_ids: list[str] = field(default_factory=list)
    memory_query: str | None = None
    output_format: str = "docx"
    body: str | None = None
    title: str | None = None
    output_dir: str | Path | None = None
    output_path: str | Path | None = None
    output_name: str | None = None
    exact_body: bool = True
    sections: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GeneratedDocument:
    output_path: str
    format: str
    summary: str
    key_sections: list[str]
    citations: list[str] = field(default_factory=list)
    size: int = 0
    sha256: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_path": self.output_path,
            "format": self.format,
            "summary": self.summary,
            "key_sections": list(self.key_sections),
            "citations": list(self.citations),
            "size": self.size,
            "sha256": self.sha256,
            "warnings": list(self.warnings),
        }


class DocumentAgent:
    """Operator-facing document workflows built on ``JarvisDocumentSurfer``."""

    def __init__(
        self,
        *,
        output_dir: str | Path | None = None,
        config: DocumentSurferConfig | None = None,
    ) -> None:
        cfg = config or DocumentSurferConfig()
        if output_dir is not None:
            cfg.output_dir = Path(output_dir)
        self.surfer = JarvisDocumentSurfer(cfg)

    def generate(self, request: DocumentGenerationRequest) -> GeneratedDocument:
        title = (request.title or request.task or "Document").strip() or "Document"
        body_parts: list[str] = []
        if request.body:
            body_parts.append(str(request.body))
        else:
            body_parts.append(f"Task: {request.task}")
            if request.source_files:
                try:
                    corpus = self.surfer.summarize_corpus(request.source_files)
                    body_parts.append(
                        corpus.get("combined_outline") or corpus.get("markdown") or ""
                    )
                    body_parts.append(
                        "Source files:\n"
                        + "\n".join(f"- {path}" for path in request.source_files[:40])
                    )
                except DocumentSurferError:
                    body_parts.append(
                        "Source files:\n"
                        + "\n".join(f"- {path}" for path in request.source_files[:40])
                    )
            if request.memory_query:
                body_parts.append(f"Memory focus: {request.memory_query}")
            if request.web_research_ids:
                body_parts.append(
                    "Research ids:\n"
                    + "\n".join(f"- {item}" for item in request.web_research_ids[:40])
                )
            if request.sections:
                for section in request.sections:
                    heading = str(section.get("heading") or section.get("title") or "").strip()
                    text = str(section.get("body") or section.get("text") or "").strip()
                    if heading:
                        body_parts.append(heading)
                    if text:
                        body_parts.append(text)

        output_dir = request.output_dir or self.surfer.config.output_dir
        if output_dir is not None:
            self.surfer.config.output_dir = Path(output_dir)

        body_text = "\n\n".join(part for part in body_parts if part).strip() or title
        fmt = str(request.output_format or "md").strip().lower()
        if fmt == "markdown":
            fmt = "md"
        base_dir = Path(output_dir) if output_dir is not None else (
            self.surfer.config.output_dir or Path.cwd() / "document-outputs"
        )
        destination = resolve_artifact_output_path(
            base_dir,
            output_path=request.output_path,
            output_name=request.output_name,
            default_name=f"document.{fmt}",
            collision_safe=True,
        )

        try:
            if fmt in {"md", "txt"} and request.exact_body and request.body:
                written = write_exact_text_artifact(destination, str(request.body))
                return GeneratedDocument(
                    output_path=str(written["path"]),
                    format=fmt,
                    summary=f"Generated exact {fmt} for: {request.task}. Path: {written['path']}"[
                        :850
                    ],
                    key_sections=[title],
                    citations=list(request.web_research_ids),
                    size=int(written["size"]),
                    sha256=written.get("sha256"),
                    warnings=[],
                )
            if fmt == "docx" and ("#" in body_text or "|" in body_text):
                written = write_markdown_docx(destination, body_text, title=title)
                structure = written.get("structure") or {}
                headings = list(structure.get("headings") or []) or [title]
                return GeneratedDocument(
                    output_path=str(written["path"]),
                    format="docx",
                    summary=f"Generated docx for: {request.task}. Path: {written['path']}"[:850],
                    key_sections=headings[:12],
                    citations=list(request.web_research_ids),
                    size=int(written["size"]),
                    sha256=written.get("sha256"),
                    warnings=[],
                )
            result = self.surfer.generate(
                title=title,
                body=body_text,
                output_format=fmt,
                output_path=destination,
                sections=request.sections or None,
                metadata={
                    "task": request.task,
                    "source_files": list(request.source_files),
                    "web_research_ids": list(request.web_research_ids),
                    "memory_query": request.memory_query,
                },
            )
        except (DocumentGenerationError, DocumentSurferError) as exc:
            raise DocumentSurferError(str(exc)) from exc

        output = result.get("output") or {}
        structure = result.get("structure") or {}
        headings = list(structure.get("headings") or [])
        if not headings:
            headings = ["Summary", "Analysis", "Sources"]
        summary = (
            f"Generated {output.get('format')} for: {request.task}. "
            f"Path: {output.get('path')}"
        )
        return GeneratedDocument(
            output_path=str(output.get("path") or ""),
            format=str(output.get("format") or request.output_format),
            summary=summary[:850],
            key_sections=headings[:12],
            citations=list(request.web_research_ids),
            size=int(output.get("size") or 0),
            sha256=output.get("sha256"),
            warnings=list(result.get("warnings") or []),
        )

    def summarize_corpus(
        self,
        file_paths: Sequence[str],
        focus: str | None = None,
    ) -> dict[str, Any]:
        return self.surfer.summarize_corpus(file_paths, focus=focus)

    def build_knowledge_graph(self, file_paths: Sequence[str]) -> dict[str, Any]:
        """Lightweight co-occurrence graph from extractive entity signals."""

        corpus = self.surfer.summarize_corpus(file_paths)
        entity_counts = list((corpus.get("summary") or {}).get("entity_counts") or [])
        nodes = [
            {"id": item["label"], "label": item["label"], "weight": item["count"]}
            for item in entity_counts[:80]
        ]
        edges: list[dict[str, Any]] = []
        labels = [node["id"] for node in nodes]
        # Pair consecutive frequent entities as soft edges (extractive, not ML).
        for left, right in zip(labels, labels[1:], strict=False):
            edges.append({"source": left, "target": right, "weight": 1})
        return {
            "nodes": len(nodes),
            "edges": len(edges),
            "summary": "Extractive entity co-occurrence graph from document corpus",
            "graph": {"nodes": nodes, "edges": edges},
            "files": corpus.get("files") or [],
            "errors": corpus.get("errors") or [],
        }

    def inspect(self, path: str | Path) -> dict[str, Any]:
        return self.surfer.inspect(path)

    def analyze(self, path: str | Path, instruction: str = "") -> dict[str, Any]:
        return self.surfer.analyze(path, instruction=instruction)


def get_document_agent_tools(output_dir: str | Path | None = None) -> dict[str, Any]:
    """Return callable map used by ideal registration helpers / diagnostics."""

    agent = DocumentAgent(output_dir=output_dir)
    return {
        "documents.generate": agent.generate,
        "documents.summarize_corpus": agent.summarize_corpus,
        "documents.build_knowledge_graph": agent.build_knowledge_graph,
        "documents.inspect": agent.inspect,
        "documents.analyze": agent.analyze,
    }
