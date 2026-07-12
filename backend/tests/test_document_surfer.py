from __future__ import annotations

from pathlib import Path

import pytest

from jarvis_gpt.document_agent import DocumentAgent, DocumentGenerationRequest
from jarvis_gpt.document_runtime import extract_document
from jarvis_gpt.document_surfer import (
    DocumentSurferConfig,
    DocumentSurferError,
    JarvisDocumentSurfer,
    document_surfer_capabilities,
    is_document_path_supported,
    supported_document_kinds,
)


def test_supported_kinds_cover_core_and_generation() -> None:
    kinds = supported_document_kinds()
    assert "docx" in kinds["extract_core"]
    assert "pdf" in kinds["extract_core"]
    assert "pptx" in kinds["extract_extended"]
    assert "md" in kinds["generate"]
    assert "docx" in kinds["generate"]


def test_capabilities_probe_is_stable() -> None:
    caps = document_surfer_capabilities()
    assert "formats" in caps
    assert "host_tools" in caps
    assert caps["mutation"]["never_overwrites_original"] is True


def test_inspect_and_search_text_document(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("Alpha contract 1000 RUB\nBeta clause\nContact a@b.com\n", encoding="utf-8")
    surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=tmp_path / "out"))
    inspected = surfer.inspect(path)
    assert inspected["ok"] is True
    assert inspected["document"]["kind"] == "txt"
    assert "capabilities" in inspected

    analyzed = surfer.analyze(path, instruction="find contacts")
    assert "a@b.com" in (analyzed.get("signals") or {}).get("emails", [])

    hits = surfer.search("contract", [path])
    assert hits["hit_count"] >= 1
    assert hits["hits"][0]["match"].lower() == "contract"


def test_compare_and_edit_plan(tmp_path: Path) -> None:
    left = tmp_path / "left.md"
    right = tmp_path / "right.md"
    left.write_text("# Title\nversion one\n", encoding="utf-8")
    right.write_text("# Title\nversion two\n", encoding="utf-8")
    surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=tmp_path / "out"))
    comparison = surfer.compare(left, right)
    assert comparison["comparison"]["stats"]["diff_lines"] > 0
    plan = surfer.edit_plan(left, "Align with reference", reference_path=right)
    assert plan["plan"]["instruction"].startswith("Align")
    assert "documents.generate" in plan["plan"]["tools"]


def test_apply_replacements_never_overwrites_source(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("hello world", encoding="utf-8")
    surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=tmp_path / "out"))
    result = surfer.apply_replacements(source, [{"old": "world", "new": "jarvis"}])
    output = Path(result["output"]["path"])
    assert output.exists()
    assert output != source.resolve()
    assert source.read_text(encoding="utf-8") == "hello world"
    assert "jarvis" in output.read_text(encoding="utf-8")


def test_generate_markdown_and_docx(tmp_path: Path) -> None:
    surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=tmp_path / "out"))
    md = surfer.generate(title="Report", body="Line one\nLine two", output_format="md")
    md_path = Path(md["output"]["path"])
    assert md_path.exists()
    assert "Report" in md_path.read_text(encoding="utf-8")

    docx = surfer.generate(
        title="Office Report",
        body="Paragraph A\nParagraph B",
        output_format="docx",
    )
    docx_path = Path(docx["output"]["path"])
    assert docx_path.exists()
    extracted = extract_document(docx_path, max_chars=5000)
    assert "Office Report" in extracted["text"]
    assert "Paragraph A" in extracted["text"]


def test_generate_xlsx_and_convert(tmp_path: Path) -> None:
    surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=tmp_path / "out"))
    xlsx = surfer.generate(title="Sheet Title", body="row-a\nrow-b", output_format="xlsx")
    xlsx_path = Path(xlsx["output"]["path"])
    assert xlsx_path.exists()
    extracted = extract_document(xlsx_path, max_chars=5000)
    assert extracted["kind"] == "xlsx"
    assert "row-a" in extracted["text"]

    source = tmp_path / "source.txt"
    source.write_text("Convert me please", encoding="utf-8")
    converted = surfer.convert(source, output_format="html")
    html_path = Path(converted["output"]["path"])
    assert html_path.exists()
    assert "Convert me please" in html_path.read_text(encoding="utf-8")


def test_summarize_corpus_and_package(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("Budget planning for Q1\nAlpha Team", encoding="utf-8")
    b.write_text("Budget risks and Alpha Team notes", encoding="utf-8")
    surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=tmp_path / "out"))
    corpus = surfer.summarize_corpus([a, b], focus="Budget")
    assert corpus["summary"]["files"] == 2
    assert corpus["summary"]["total_chars"] > 0

    package = surfer.package([a, b], output_name="bundle.zip")
    package_path = Path(package["output"]["path"])
    assert package_path.exists()
    assert package["output"]["count"] == 2


def test_document_agent_generate_uses_surfer(tmp_path: Path) -> None:
    agent = DocumentAgent(output_dir=tmp_path / "out")
    generated = agent.generate(
        DocumentGenerationRequest(
            task="Weekly summary",
            body="Status green",
            output_format="md",
            web_research_ids=["ev-1"],
        )
    )
    assert Path(generated.output_path).exists()
    assert generated.format == "md"
    assert "ev-1" in generated.citations


def test_empty_search_query_raises(tmp_path: Path) -> None:
    surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=tmp_path / "out"))
    with pytest.raises(DocumentSurferError, match="query"):
        surfer.search("  ", [tmp_path / "missing.txt"])


def test_is_document_path_supported_extended() -> None:
    assert is_document_path_supported("deck.pptx")
    assert is_document_path_supported("note.odt")
    assert is_document_path_supported("file.docx")
