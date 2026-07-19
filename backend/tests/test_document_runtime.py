from __future__ import annotations

import asyncio
import hashlib
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.document_runtime import (
    DocumentRuntimeError,
    _parse_xml,
    copy_document,
    edit_docx_document,
    edit_text_document,
    edit_workbook_xlsx,
    extract_document,
    file_sha256,
    read_workbook_grid,
    resolve_artifact_output_path,
    verify_document_artifact,
    write_exact_text_artifact,
    write_markdown_docx,
    write_workbook_xlsx,
)
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


def test_parse_xml_rejects_dtd_and_entity_expansion() -> None:
    payload = """<!DOCTYPE root [<!ENTITY x "expanded">]><root>&x;</root>"""

    with pytest.raises(DocumentRuntimeError, match="DTD and entity"):
        _parse_xml(payload, "test document")


def test_parse_xml_accepts_plain_office_xml() -> None:
    root = _parse_xml("<root><value>safe</value></root>", "test document")

    assert root.findtext("value") == "safe"


def test_resolve_artifact_output_path_honors_subdirectory(tmp_path: Path) -> None:
    root = tmp_path / "document-outputs"
    destination = resolve_artifact_output_path(
        root,
        output_name="functional-20260713/OP-0013-1.md",
        default_name="fallback.md",
    )

    assert destination == root / "functional-20260713" / "OP-0013-1.md"
    assert destination.parent.is_dir()


def test_resolve_artifact_output_path_collision_is_unique(tmp_path: Path) -> None:
    root = tmp_path / "document-outputs"
    first = resolve_artifact_output_path(root, output_name="report.md")
    first.write_text("A\n", encoding="utf-8")
    second = resolve_artifact_output_path(root, output_name="report.md")

    assert first.name == "report.md"
    assert second.name.startswith("report.")
    assert second.suffix == ".md"
    assert first != second


def test_resolve_artifact_exact_path_refuses_collision_without_overwrite(
    tmp_path: Path,
) -> None:
    root = tmp_path / "document-outputs"
    first = resolve_artifact_output_path(
        root, output_name="exact.md", collision_safe=False
    )
    first.write_text("A\n", encoding="utf-8")
    with pytest.raises(DocumentRuntimeError, match="overwrite"):
        resolve_artifact_output_path(
            root, output_name="exact.md", collision_safe=False, allow_overwrite=False
        )


def test_write_exact_text_artifact_has_no_generator_wrapper(tmp_path: Path) -> None:
    path = tmp_path / "document-outputs" / "functional-20260713" / "OP-0013-1.md"
    body = "# Итог\n\nmarker OP-0013-1\n"
    written = write_exact_text_artifact(path, body)

    text = Path(written["path"]).read_text(encoding="utf-8")
    assert text == body if body.endswith("\n") else body + "\n"
    assert "generator:" not in text
    assert "<!--" not in text
    assert written["verification"]["ok"] is True


def test_markdown_to_docx_has_heading_styles_and_tables(tmp_path: Path) -> None:
    source = tmp_path / "convert-1.md"
    source.write_text(
        "# Conversion Fixture\n\n| ColA | ColB |\n| --- | --- |\n| 1 | 2 |\n",
        encoding="utf-8",
    )
    source_hash = file_sha256(source)
    out = tmp_path / "document-outputs" / "convert-1.docx"
    written = write_markdown_docx(out, source.read_text(encoding="utf-8"), title="convert-1")

    assert file_sha256(source) == source_hash
    assert written["verification"]["ok"] is True
    assert written["table_count"] >= 1
    assert written["heading_count"] >= 1

    with zipfile.ZipFile(out) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
        assert 'w:val="Heading1"' in xml
        assert "<w:tbl>" in xml
        names = archive.namelist()
        assert len(names) == len(set(names))
        ET.fromstring(archive.read("word/document.xml"))

    extracted = extract_document(out)
    assert int((extracted.get("structure") or {}).get("table_count") or 0) >= 1


def test_copy_document_preserves_source_hash(tmp_path: Path) -> None:
    source = tmp_path / "source-copy-1.docx"
    # Minimal valid-ish zip is enough for copy contract.
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    source_hash = file_sha256(source)
    out = tmp_path / "document-outputs" / "source-copy-1_STATUS_NEW.docx"
    copied = copy_document(source, out)

    assert file_sha256(source) == source_hash
    assert Path(copied["path"]).exists()
    assert Path(copied["path"]).read_bytes() == source.read_bytes()


def test_corrupt_pdf_fails_actionably_then_valid_retry_is_clean(tmp_path: Path) -> None:
    """SPARK-0008: corrupt-to-valid retry has no false success or stale content."""

    from jarvis_gpt.document_runtime import extract_document_safe

    corrupt = tmp_path / "corrupt-1.pdf"
    corrupt.write_bytes(
        b"%PDF-1.7\n% intentionally truncated functional fixture\n1 0 obj\n"
    )
    failed = extract_document_safe(corrupt)
    assert failed["ok"] is False
    assert failed["status"] == "failed"
    assert failed["actionable"] is True
    assert failed["partial_result"] is None
    assert failed["stale_content"] is False
    assert "retry" in str(failed["error"]).casefold()

    valid = tmp_path / "valid-replacement.pdf"
    # Minimal complete-looking PDF with EOF and a page marker + extractable text.
    valid.write_bytes(
        b"%PDF-1.7\n"
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>endobj\n"
        b"4 0 obj<< /Length 44 >>stream\n"
        b"BT /F1 12 Tf 100 100 Td (AUDIT_OK_MARKER) Tj ET\n"
        b"endstream\nendobj\n"
        b"xref\n0 5\n"
        b"trailer<< /Root 1 0 R >>\n"
        b"startxref\n0\n"
        b"%%EOF\n"
    )
    # Parser may use basic extraction for synthetic content; ensure no raise/false fail.
    recovered = extract_document_safe(valid)
    assert recovered["ok"] is True
    assert recovered["partial_result"] is None
    assert recovered["stale_content"] is False
    assert recovered["document"]["kind"] == "pdf"
    # No stale content from previous corrupt attempt.
    assert recovered["document"]["name"] == "valid-replacement.pdf"
    text = str(recovered["document"].get("text") or "")
    assert "intentionally truncated" not in text


def test_documents_generate_exact_path_and_collision_tools(monkeypatch, tmp_path: Path) -> None:
    """SPARK-0003 user contract via documents.generate / convert tools."""

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    body = "# Итог\n\nmarker OP-0013-1\n"
    generated = asyncio.run(
        tools.run(
            "documents.generate",
            {
                "title": "OP-0013",
                "body": body,
                "output_format": "md",
                "output_name": "functional-20260713/OP-0013-1.md",
                "exact_body": True,
            },
        )
    )
    assert generated.ok is True
    path = Path(generated.data["output"]["path"])
    assert path == settings.data_dir / "document-outputs" / "functional-20260713" / "OP-0013-1.md"
    assert path.read_text(encoding="utf-8") == body
    assert "generator" not in path.read_text(encoding="utf-8")

    # Exact-name collision must not overwrite and must not claim false success.
    second = asyncio.run(
        tools.run(
            "documents.generate",
            {
                "body": "# Итог\n\nmarker OP-0034-B-1\n",
                "output_format": "md",
                "output_name": "functional-20260713/OP-0013-1.md",
            },
        )
    )
    assert second.ok is False
    assert "overwrite" in second.summary.casefold() or "exist" in second.summary.casefold()
    assert path.read_text(encoding="utf-8") == body
    assert path.exists()

    # Explicit collision_safe opt-in may allocate a unique non-overwriting path.
    third = asyncio.run(
        tools.run(
            "documents.generate",
            {
                "body": "# Итог\n\nmarker OP-0034-B-1\n",
                "output_format": "md",
                "output_name": "functional-20260713/OP-0013-1.md",
                "collision_safe": True,
                "require_exact_path": False,
            },
        )
    )
    assert third.ok is True
    third_path = Path(third.data["output"]["path"])
    assert third_path != path
    assert path.read_text(encoding="utf-8") == body
    assert "OP-0034-B-1" in third_path.read_text(encoding="utf-8")

    md = tmp_path / "convert-source.md"
    md.write_text(
        "# Conversion Fixture\n\n| A | B |\n| --- | --- |\n| x | y |\n",
        encoding="utf-8",
    )
    before = hashlib.sha256(md.read_bytes()).hexdigest()
    converted = asyncio.run(
        tools.run(
            "documents.convert",
            {
                "path": str(md),
                "output_format": "docx",
                "output_name": "functional-20260713/convert-1.docx",
            },
        )
    )
    assert converted.ok is True
    assert converted.data["source_unchanged"] is True
    assert hashlib.sha256(md.read_bytes()).hexdigest() == before
    docx_path = Path(converted.data["output"]["path"])
    assert docx_path.exists()
    verification = verify_document_artifact(docx_path, expected_format="docx")
    assert verification["ok"] is True
    with zipfile.ZipFile(docx_path) as archive:
        assert 'w:val="Heading1"' in archive.read("word/document.xml").decode("utf-8")
        assert "<w:tbl>" in archive.read("word/document.xml").decode("utf-8")

    storage.close()


def test_documents_generate_exact_path_mismatch_is_failure(monkeypatch, tmp_path: Path) -> None:
    """RB-3 C: if the tool would bind a different path, success is forbidden."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    # Occupy the exact name so require_exact path resolution refuses silently rewriting.
    occupied = settings.data_dir / "document-outputs" / "must-be-exact.md"
    occupied.parent.mkdir(parents=True, exist_ok=True)
    occupied.write_text("occupied\n", encoding="utf-8")

    result = asyncio.run(
        tools.run(
            "documents.generate",
            {
                "body": "# Should fail\n",
                "output_format": "md",
                "output_name": "must-be-exact.md",
                "require_exact_path": True,
                "exact_body": True,
            },
        )
    )
    assert result.ok is False
    assert "overwrite" in result.summary.casefold() or "exist" in result.summary.casefold()
    assert occupied.read_text(encoding="utf-8") == "occupied\n"
    storage.close()


def test_documents_generate_preserves_source_hash(monkeypatch, tmp_path: Path) -> None:
    """RB-3 H: source files remain unchanged after generate-from-source."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    source = tmp_path / "source-transform.md"
    source.write_text("# Source\n\noriginal body\n", encoding="utf-8")
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    generated = asyncio.run(
        tools.run(
            "documents.generate",
            {
                "title": "Transform out",
                "output_format": "md",
                "output_name": "transform-out.md",
                "source_paths": [str(source)],
                "require_exact_path": True,
            },
        )
    )
    assert generated.ok is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    out = Path(generated.data["output"]["path"])
    assert out.name == "transform-out.md"
    assert out.is_file()
    storage.close()


# ----------------------------------------------------------------- XLSX editing


def _sample_budget_xlsx(path: Path) -> None:
    write_workbook_xlsx(
        path,
        [
            {
                "name": "Budget",
                "rows": [
                    ["Item", "Amount", "Active"],
                    ["Rent", 30000, True],
                    ["Marketing", 10000, False],
                    ["Total", "=SUM(B2:B3)", ""],
                ],
            }
        ],
        title="Budget",
    )


def test_read_workbook_grid_preserves_types_and_formulas(tmp_path: Path) -> None:
    src = tmp_path / "budget.xlsx"
    _sample_budget_xlsx(src)
    grid = read_workbook_grid(src)
    assert len(grid) == 1
    rows = grid[0]["rows"]
    assert rows[0] == ["Item", "Amount", "Active"]
    # numbers stay numeric, not stringified
    assert rows[1] == ["Rent", 30000, True]
    assert isinstance(rows[1][1], int)
    assert rows[2][2] is False
    # a formula round-trips as a leading-"=" string
    assert rows[3][1] == "=SUM(B2:B3)"


def test_edit_workbook_append_update_delete(tmp_path: Path) -> None:
    src = tmp_path / "budget.xlsx"
    _sample_budget_xlsx(src)
    out = tmp_path / "budget.edited.xlsx"
    result = edit_workbook_xlsx(
        src,
        [
            {"op": "append_row", "values": ["Ads", 15000, True]},
            # resolve the column by its header name, not a letter
            {"op": "update_row_where", "match_col": "Item", "match_value": "Marketing",
             "set_col": "Amount", "value": 12000},
            {"op": "delete_row", "match_col": "Item", "match_value": "Rent"},
        ],
        out,
    )
    assert out.exists()
    assert result["verification"]["ok"] is True
    assert len(result["changes"]) == 3
    rows = read_workbook_grid(out)[0]["rows"]
    names = [row[0] for row in rows]
    assert "Ads" in names and "Rent" not in names
    marketing = next(row for row in rows if row[0] == "Marketing")
    assert marketing[1] == 12000 and isinstance(marketing[1], int)


def test_edit_workbook_set_cell_a1_expands_grid(tmp_path: Path) -> None:
    src = tmp_path / "budget.xlsx"
    _sample_budget_xlsx(src)
    out = tmp_path / "grown.xlsx"
    edit_workbook_xlsx(
        src,
        [
            {"op": "set_cell", "cell": "E7", "value": 42},
            {"op": "set_cell", "row": 2, "col": "Active", "value": False},
        ],
        out,
    )
    rows = read_workbook_grid(out)[0]["rows"]
    # E7 => row 7, col 5
    assert rows[6][4] == 42
    assert rows[1][2] is False


def test_edit_workbook_add_and_rename_sheet(tmp_path: Path) -> None:
    src = tmp_path / "budget.xlsx"
    _sample_budget_xlsx(src)
    out = tmp_path / "multi.xlsx"
    edit_workbook_xlsx(
        src,
        [
            {"op": "rename_sheet", "sheet": "Budget", "name": "Plan 2026"},
            {"op": "add_sheet", "name": "Notes", "rows": [["Note"], ["reviewed"]]},
        ],
        out,
    )
    grid = read_workbook_grid(out)
    names = [sheet["name"] for sheet in grid]
    assert "Plan 2026" in names and "Notes" in names
    assert extract_document(out)["structure"]["sheet_count"] == 2


def test_edit_workbook_empty_ops_and_bad_match_raise(tmp_path: Path) -> None:
    src = tmp_path / "budget.xlsx"
    _sample_budget_xlsx(src)
    out = tmp_path / "nope.xlsx"
    with pytest.raises(DocumentRuntimeError):
        edit_workbook_xlsx(src, [], out)
    with pytest.raises(DocumentRuntimeError):
        edit_workbook_xlsx(
            src,
            [{"op": "delete_row", "match_col": "Item", "match_value": "Missing"}],
            out,
        )
    # the source is never touched and no partial output is left readable as valid
    assert read_workbook_grid(src)[0]["rows"][0] == ["Item", "Amount", "Active"]


# ------------------------------------------------------------- DOCX/text editing


def test_edit_docx_replaces_and_appends_preserving_source(tmp_path: Path) -> None:
    src = tmp_path / "report.docx"
    write_markdown_docx(
        src,
        "# Report\n\nDeadline: 15 July. Owner: Ivanov.\n\n## Summary\n\n- one\n- two\n",
        title="Report",
    )
    with zipfile.ZipFile(src) as archive:
        original_members = set(archive.namelist())
        original_styles = archive.read("word/styles.xml")

    out = tmp_path / "report.edited.docx"
    result = edit_docx_document(
        src,
        [
            {"op": "replace", "old": "15 July", "new": "20 July"},
            {"op": "append_section", "title": "Risks", "level": 2,
             "body": "Key risks:\n\n- delays\n- budget\n\nSee [portal](https://example.com)."},
        ],
        out,
    )
    assert result["verification"]["ok"] is True
    assert any("replaced" in change for change in result["changes"])

    with zipfile.ZipFile(out) as archive:
        new_members = set(archive.namelist())
        new_styles = archive.read("word/styles.xml")
    # every original part survives and untouched parts are byte-identical
    assert original_members <= new_members
    assert new_styles == original_styles

    text = extract_document(out)["text"]
    assert "20 July" in text and "15 July" not in text
    assert "Risks" in text and "delays" in text
    # a link in appended content is preserved as visible text (no external rels)
    assert "example.com" in text


def test_edit_docx_missing_replacement_text_raises(tmp_path: Path) -> None:
    src = tmp_path / "doc.docx"
    write_markdown_docx(src, "# Doc\n\nAlpha content.\n", title="Doc")
    out = tmp_path / "doc.edited.docx"
    with pytest.raises(DocumentRuntimeError):
        edit_docx_document(src, [{"op": "replace", "old": "Zeta", "new": "Omega"}], out)


def test_edit_pdf_replace_and_append_regenerates(tmp_path: Path) -> None:
    from jarvis_gpt.document_runtime import edit_pdf_document, write_pdf

    src = tmp_path / "source.pdf"
    write_pdf(src, "Hello ALPHA world.\n\nSecond paragraph.", title="Source")
    out = tmp_path / "edited.pdf"
    result = edit_pdf_document(
        src,
        [
            {"op": "replace", "old": "ALPHA", "new": "BETA"},
            {"op": "append", "text": "\n\nFooter note."},
        ],
        out,
    )
    assert result["format"] == "pdf"
    assert result.get("regenerated") is True
    assert out.exists()
    text = extract_document(out)["text"]
    assert "BETA" in text
    assert "ALPHA" not in text
    assert "Footer note" in text


def test_edit_pdf_empty_text_fails_closed(tmp_path: Path) -> None:
    from jarvis_gpt.document_runtime import DocumentRuntimeError, edit_pdf_document

    # Minimal valid-looking PDF shell with no extractable text.
    src = tmp_path / "empty.pdf"
    src.write_bytes(
        b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\nstartxref\n0\n%%EOF\n"
    )
    with pytest.raises(DocumentRuntimeError, match="no extractable text|corrupt|unreadable"):
        edit_pdf_document(src, [{"op": "append", "text": "x"}], tmp_path / "out.pdf")


def test_edit_text_append_prepend_replace_insert(tmp_path: Path) -> None:
    src = tmp_path / "notes.md"
    src.write_text("# Notes\n\nFirst line.\n", encoding="utf-8")
    out = tmp_path / "notes.edited.md"
    result = edit_text_document(
        src,
        [
            {"op": "append", "text": "## Added\n\nmore."},
            {"op": "prepend", "text": "<!-- top -->"},
            {"op": "replace", "old": "First line.", "new": "First line (edited)."},
            {"op": "insert_after", "anchor": "# Notes", "text": "_subtitle_"},
        ],
        out,
    )
    body = out.read_text(encoding="utf-8")
    assert result["verification"]["ok"] is True
    assert body.startswith("<!-- top -->")
    assert "First line (edited)." in body
    assert "_subtitle_" in body and "## Added" in body


def test_edit_text_replace_missing_raises(tmp_path: Path) -> None:
    src = tmp_path / "t.txt"
    src.write_text("hello world\n", encoding="utf-8")
    out = tmp_path / "t.edited.txt"
    with pytest.raises(DocumentRuntimeError):
        edit_text_document(src, [{"op": "replace", "old": "absent", "new": "x"}], out)
