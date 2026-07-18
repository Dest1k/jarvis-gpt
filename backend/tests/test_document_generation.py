"""Native DOCX + XLSX generation (hand-rolled OpenXML, no external libraries).

These round-trip every artifact through the project's own reader (`extract_document`)
and the structural verifier, so the suite proves the files are valid Office packages
without depending on python-docx / openpyxl being installed.
"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.document_runtime import (
    build_chart_spec,
    build_slides_from_markdown,
    build_workbook_sheets,
    extract_document,
    write_chart_svg,
    write_markdown_docx,
    write_pdf,
    write_presentation_pptx,
    write_workbook_xlsx,
)
from jarvis_gpt.document_surfer import (
    DocumentSurferConfig,
    JarvisDocumentSurfer,
    _extract_pptx,
)
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry

# --------------------------------------------------------------------------- XLSX


def test_workbook_xlsx_multisheet_with_numbers_and_formulas(tmp_path):
    out = tmp_path / "budget.xlsx"
    sheets = [
        {
            "name": "Budget",
            "rows": [
                ["Item", "Q1", "Q2", "Total"],
                ["Rent", 1000, 1000, "=B2+C2"],
                ["Sum", "=SUM(B2:B2)", "=SUM(C2:C2)", "=SUM(D2:D2)"],
            ],
        },
        {"name": "Notes", "rows": [["Note"], ["Prepared by Jarvis"]]},
    ]
    result = write_workbook_xlsx(out, sheets, title="Budget 2026")
    assert result["format"] == "xlsx"
    assert result["sheet_count"] == 2
    assert result["verification"]["ok"] is True
    assert result["verification"]["worksheet_count"] == 2

    structure = extract_document(out)["structure"]
    assert structure["sheet_count"] == 2
    assert structure["formula_count"] >= 4
    first = structure["sheets"][0]
    assert first["name"] == "Budget"
    assert first["preview_rows"][0] == ["Item", "Q1", "Q2", "Total"]
    # Numbers round-trip as numeric strings (reader stringifies), formula parts captured.
    assert any("B2+C2" in formula for formula in first["formulas"])


def test_workbook_xlsx_from_markdown_table(tmp_path):
    md = "| Name | Score |\n| --- | --- |\n| Alice | 91 |\n| Bob | 88 |\n"
    sheets = build_workbook_sheets(body=md, default_name="Scores")
    assert sheets[0]["rows"][1] == ["Alice", 91]  # numeric coercion
    out = tmp_path / "scores.xlsx"
    result = write_workbook_xlsx(out, sheets, title="Scores")
    assert result["verification"]["ok"] is True
    structure = extract_document(out)["structure"]
    assert structure["sheets"][0]["preview_rows"][0] == ["Name", "Score"]


def test_workbook_xlsx_from_csv(tmp_path):
    sheets = build_workbook_sheets(body="a,b,c\n1,2,3\n4,5,6\n", default_name="Data")
    assert sheets[0]["rows"][1] == [1, 2, 3]
    out = tmp_path / "data.xlsx"
    assert write_workbook_xlsx(out, sheets)["verification"]["ok"] is True


def test_workbook_multiple_markdown_tables_become_sheets(tmp_path):
    body = "| A |\n| --- |\n| 1 |\n\n| B |\n| --- |\n| 2 |\n"
    sheets = build_workbook_sheets(body=body, default_name="Report")
    assert len(sheets) == 2
    out = tmp_path / "multi.xlsx"
    result = write_workbook_xlsx(out, sheets)
    assert result["sheet_count"] == 2
    assert result["verification"]["worksheet_count"] == 2


def test_workbook_sheet_names_sanitized_and_unique(tmp_path):
    sheets = [
        {"name": "A/B:C*?" + "x" * 40, "rows": [["v"]]},
        {"name": "Dup", "rows": [["v"]]},
        {"name": "Dup", "rows": [["v"]]},
    ]
    out = tmp_path / "names.xlsx"
    write_workbook_xlsx(out, sheets)
    names = [s["name"] for s in extract_document(out)["structure"]["sheets"]]
    assert all(len(name) <= 31 for name in names)
    assert len(set(names)) == len(names)  # de-duplicated
    assert not any(ch in name for name in names for ch in "[]:*?/\\")


def test_workbook_preserves_leading_zero_codes(tmp_path):
    sheets = build_workbook_sheets(body="code,qty\n007,5\n", default_name="Codes")
    # Leading-zero code stays a string; plain integer coerces.
    assert sheets[0]["rows"][1] == ["007", 5]


# --------------------------------------------------------------------------- DOCX


def test_docx_headings_lists_table_and_inline(tmp_path):
    md = (
        "# Title\n\n"
        "Para with **bold**, *italic*, `code`, and [link](https://example.com/p).\n\n"
        "## Section\n\n"
        "- bullet one\n- bullet two\n\n"
        "1. step one\n2. step two\n\n"
        "| Metric | Value |\n| --- | --- |\n| Accuracy | 0.91 |\n"
    )
    out = tmp_path / "report.docx"
    result = write_markdown_docx(out, md, title="Title")
    assert result["verification"]["ok"] is True
    assert result["heading_count"] == 2
    assert result["table_count"] == 1

    text = extract_document(out)["text"]
    assert "Title" in text and "bullet one" in text and "Accuracy" in text

    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        assert "word/numbering.xml" in names
        assert "word/styles.xml" in names
        document = archive.read("word/document.xml").decode("utf-8")
        rels = archive.read("word/_rels/document.xml.rels").decode("utf-8")
    # Inline styling emitted as real runs.
    assert "<w:b/>" in document and "<w:i/>" in document
    # Lists reference numbering definitions; bullet uses numId 1.
    assert "<w:numPr>" in document and 'w:numId w:val="1"' in document
    # Hyperlink relationship recorded as external target.
    assert "example.com/p" in rels and 'TargetMode="External"' in rels
    # Heading styles carry real formatting (font size present).
    styles = None
    with zipfile.ZipFile(out) as archive:
        styles = archive.read("word/styles.xml").decode("utf-8")
    assert 'w:styleId="Heading1"' in styles and "<w:sz " in styles


def test_docx_plain_paragraph_still_valid(tmp_path):
    out = tmp_path / "plain.docx"
    result = write_markdown_docx(out, "Just a single plain paragraph.", title="Plain")
    assert result["verification"]["ok"] is True
    assert "single plain paragraph" in extract_document(out)["text"]


def test_docx_ordered_lists_restart_numbering(tmp_path):
    md = "1. a\n2. b\n\nSome text.\n\n1. x\n2. y\n"
    out = tmp_path / "lists.docx"
    write_markdown_docx(out, md, title="Lists")
    with zipfile.ZipFile(out) as archive:
        numbering = archive.read("word/numbering.xml").decode("utf-8")
        document = archive.read("word/document.xml").decode("utf-8")
    # Two ordered lists => two distinct decimal num definitions (numId 2 and 3).
    assert 'w:numId w:val="2"' in document
    assert 'w:numId w:val="3"' in document
    assert numbering.count("<w:num ") >= 3  # bullet(1) + two ordered


def test_workbook_accepts_cells_format(tmp_path):
    # The natural format a model emits: sparse {row, col, value} cells.
    sheets = [
        {
            "name": "Бюджет",
            "cells": [
                {"row": 1, "col": 1, "value": "Статья"},
                {"row": 1, "col": 2, "value": "Сумма"},
                {"row": 2, "col": 1, "value": "Аренда"},
                {"row": 2, "col": 2, "value": 30000},
                {"row": 3, "col": 1, "value": "Итого"},
                {"row": 3, "col": 2, "value": "=SUM(B2:B2)"},
            ],
        }
    ]
    built = build_workbook_sheets(sheets=sheets, body="", default_name="Бюджет")
    out = tmp_path / "budget.xlsx"
    result = write_workbook_xlsx(out, built, title="Бюджет")
    assert result["verification"]["ok"] is True
    structure = extract_document(out)["structure"]
    first = structure["sheets"][0]
    assert first["rows"] == 3  # not an empty sheet
    assert first["preview_rows"][1] == ["Аренда", "30000"]
    assert any("SUM" in formula for formula in first["formulas"])


def test_workbook_falls_back_to_body_when_sheets_have_no_data(tmp_path):
    built = build_workbook_sheets(
        sheets=[{"name": "X", "cells": []}],
        body="Аренда,30000\nЕда,20000\nИтого,=SUM(B1:B2)",
    )
    out = tmp_path / "fallback.xlsx"
    write_workbook_xlsx(out, built)
    structure = extract_document(out)["structure"]
    assert structure["sheets"][0]["rows"] == 3
    assert structure["sheets"][0]["preview_rows"][0] == ["Аренда", "30000"]


# --------------------------------------------------------------------------- PPTX


def test_presentation_pptx_roundtrip(tmp_path):
    out = tmp_path / "deck.pptx"
    result = write_presentation_pptx(
        out,
        [
            {"title": "Intro", "bullets": ["Point A", "Point B"]},
            {"title": "Data", "bullets": ["42 units"]},
        ],
        title="Deck",
    )
    assert result["format"] == "pptx"
    assert result["slide_count"] == 2
    assert result["verification"]["ok"] is True
    assert result["verification"]["slide_count"] == 2

    # Round-trip through the project's own PPTX reader (reads DrawingML a:t nodes).
    text = _extract_pptx(out)["text"]
    assert "Point A" in text
    assert "Data" in text
    assert "42 units" in text


def test_presentation_pptx_opens_in_python_pptx(tmp_path):
    pptx = pytest.importorskip("pptx")
    out = tmp_path / "deck.pptx"
    write_presentation_pptx(
        out,
        [
            {"title": "Intro", "bullets": ["Point A", "Point B"]},
            {"title": "Data", "bullets": ["42 units"]},
        ],
        title="Deck",
    )
    prs = pptx.Presentation(str(out))
    slides = list(prs.slides)
    assert len(slides) == 2
    collected = []
    for slide in slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                collected.append(shape.text_frame.text)
    joined = "\n".join(collected)
    assert "Point A" in joined
    assert "42 units" in joined


def test_build_slides_from_markdown_sections(tmp_path):
    slides = build_slides_from_markdown(
        "# First\n\n- alpha\n- beta\n\n# Second\n\nplain paragraph"
    )
    assert [s["title"] for s in slides] == ["First", "Second"]
    assert slides[0]["bullets"] == ["alpha", "beta"]
    assert slides[1]["bullets"] == ["plain paragraph"]
    out = tmp_path / "md-deck.pptx"
    result = write_presentation_pptx(out, slides, title="Deck")
    assert result["verification"]["ok"] is True
    assert result["slide_count"] == 2


# --------------------------------------------------------------------------- PDF


def test_pdf_roundtrip(tmp_path):
    out = tmp_path / "note.pdf"
    result = write_pdf(
        out,
        "# Report\n\nHello world line one.\n\n- bullet alpha\n- bullet beta",
        title="Report",
    )
    assert result["format"] == "pdf"
    assert result["page_count"] >= 1
    assert result["verification"]["ok"] is True

    raw = out.read_bytes()
    assert raw.startswith(b"%PDF")
    assert raw.rstrip().endswith(b"%%EOF")
    assert b"xref" in raw

    text = extract_document(out)["text"]
    assert "Hello world" in text
    assert "bullet alpha" in text


def test_pdf_opens_in_pypdf(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    out = tmp_path / "note.pdf"
    write_pdf(out, "# Report\n\nHello world line one.", title="Report")
    reader = pypdf.PdfReader(str(out))
    assert not reader.is_encrypted
    assert len(reader.pages) >= 1
    assert "Hello world" in reader.pages[0].extract_text()


def test_pdf_multipage(tmp_path):
    body = "\n\n".join(f"Line number {i} of the report body." for i in range(200))
    out = tmp_path / "big.pdf"
    result = write_pdf(out, body, title="Big Report")
    assert result["page_count"] > 1
    assert result["verification"]["ok"] is True
    text = extract_document(out)["text"]
    assert "Line number 199" in text


# --------------------------------------------------------------------------- SVG


def test_chart_svg_bar(tmp_path):
    out = tmp_path / "bar.svg"
    result = write_chart_svg(
        out,
        {
            "type": "bar",
            "title": "Sales",
            "categories": ["Q1", "Q2", "Q3"],
            "series": [{"name": "2026", "data": [10, 20, 15]}],
        },
    )
    assert result["format"] == "svg"
    assert result["chart_type"] == "bar"
    assert result["verification"]["ok"] is True
    content = out.read_text(encoding="utf-8")
    root = ET.fromstring(content)
    assert root.tag.rsplit("}", 1)[-1] == "svg"
    assert "<rect" in content
    assert "Sales" in content


def test_chart_svg_line(tmp_path):
    out = tmp_path / "line.svg"
    write_chart_svg(
        out,
        {
            "type": "line",
            "title": "Trend",
            "categories": ["Jan", "Feb", "Mar"],
            "series": [{"name": "A", "data": [1, 5, 3]}],
        },
    )
    content = out.read_text(encoding="utf-8")
    assert ET.fromstring(content).tag.rsplit("}", 1)[-1] == "svg"
    assert "<polyline" in content


def test_chart_svg_pie(tmp_path):
    out = tmp_path / "pie.svg"
    write_chart_svg(
        out,
        {
            "type": "pie",
            "title": "Share",
            "categories": ["North", "South", "East"],
            "series": [{"name": "share", "data": [30, 45, 25]}],
        },
    )
    content = out.read_text(encoding="utf-8")
    assert ET.fromstring(content).tag.rsplit("}", 1)[-1] == "svg"
    assert "<path" in content


def test_chart_from_markdown_table(tmp_path):
    spec = build_chart_spec(
        "| Region | Sales |\n|---|---|\n| North | 30 |\n| South | 45 |", None
    )
    assert spec["categories"] == ["North", "South"]
    assert len(spec["series"]) == 1
    assert spec["series"][0]["data"] == [30.0, 45.0]
    out = tmp_path / "from-table.svg"
    result = write_chart_svg(out, spec)
    assert result["verification"]["ok"] is True
    assert ET.fromstring(out.read_text(encoding="utf-8")).tag.rsplit("}", 1)[-1] == "svg"


def test_chart_svg_raw_passthrough(tmp_path):
    raw = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect/></svg>'
    out = tmp_path / "raw.svg"
    result = write_chart_svg(out, raw)
    assert result["verification"]["ok"] is True
    assert "<rect/>" in out.read_text(encoding="utf-8")


# ------------------------------------------------------ tool-tier dispatch wiring


def test_generate_dispatch_new_formats_via_surfer(tmp_path):
    surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=tmp_path / "out"))

    deck = surfer.generate(
        title="Quarterly",
        body="# Overview\n\n- growth up\n\n# Numbers\n\n- 42 sold",
        output_format="pptx",
    )
    assert deck["output"]["format"] == "pptx"
    assert Path(deck["output"]["path"]).exists()
    assert "42 sold" in _extract_pptx(Path(deck["output"]["path"]))["text"]

    pdf = surfer.generate(title="Memo", body="Hello memo body.", output_format="pdf")
    assert pdf["output"]["format"] == "pdf"
    assert "Hello memo" in extract_document(Path(pdf["output"]["path"]))["text"]

    svg = surfer.generate(
        title="Bars",
        body="| K | V |\n|---|---|\n| a | 3 |\n| b | 7 |",
        output_format="svg",
    )
    assert svg["output"]["format"] == "svg"
    svg_text = Path(svg["output"]["path"]).read_text(encoding="utf-8")
    assert ET.fromstring(svg_text).tag.rsplit("}", 1)[-1] == "svg"


def test_generate_dispatch_new_formats_via_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))
    outputs = settings.data_dir / "document-outputs"

    # PPTX via the 'presentation' alias + structured slides.
    deck = asyncio.run(
        tools.run(
            "documents.generate",
            {
                "title": "Deck",
                "output_format": "presentation",
                "slides": [{"title": "Intro", "bullets": ["hard-dispatch point"]}],
                "output_name": "deck.pptx",
            },
        )
    )
    assert deck.ok is True
    assert deck.data["output"]["format"] == "pptx"
    deck_path = outputs / "deck.pptx"
    assert deck_path.exists()
    assert deck.data["verification"]["ok"] is True
    assert "hard-dispatch point" in _extract_pptx(deck_path)["text"]

    # PDF hard-dispatch.
    pdf = asyncio.run(
        tools.run(
            "documents.generate",
            {
                "title": "Note",
                "body": "# Note\n\nPdf dispatch marker line.",
                "output_format": "pdf",
                "output_name": "note.pdf",
            },
        )
    )
    assert pdf.ok is True
    assert pdf.data["output"]["format"] == "pdf"
    pdf_path = outputs / "note.pdf"
    assert pdf_path.exists()
    assert pdf.data["verification"]["ok"] is True
    assert "Pdf dispatch marker" in extract_document(pdf_path)["text"]

    # SVG via the 'chart' alias + structured chart spec.
    svg = asyncio.run(
        tools.run(
            "documents.generate",
            {
                "title": "Chart",
                "output_format": "chart",
                "chart": {
                    "type": "bar",
                    "title": "Dispatch",
                    "categories": ["x", "y"],
                    "series": [{"name": "s", "data": [1, 2]}],
                },
                "output_name": "chart.svg",
            },
        )
    )
    assert svg.ok is True
    assert svg.data["output"]["format"] == "svg"
    svg_path = outputs / "chart.svg"
    assert svg_path.exists()
    assert svg.data["verification"]["ok"] is True
    assert ET.fromstring(svg_path.read_text(encoding="utf-8")).tag.rsplit("}", 1)[-1] == "svg"
