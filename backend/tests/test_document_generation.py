"""Native DOCX + XLSX generation (hand-rolled OpenXML, no external libraries).

These round-trip every artifact through the project's own reader (`extract_document`)
and the structural verifier, so the suite proves the files are valid Office packages
without depending on python-docx / openpyxl being installed.
"""

from __future__ import annotations

import zipfile

from jarvis_gpt.document_runtime import (
    build_workbook_sheets,
    extract_document,
    write_markdown_docx,
    write_workbook_xlsx,
)

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
