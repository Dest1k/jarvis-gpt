from __future__ import annotations

import difflib
import hashlib
import html
import mimetypes
import re
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

DOCUMENT_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".htm",
    ".html",
    ".json",
    ".log",
    ".md",
    ".pdf",
    ".tsv",
    ".txt",
    ".xls",
    ".xlsm",
    ".xlsx",
    ".xml",
}
DOCUMENT_MIME_TYPES = {
    "application/json",
    "application/pdf",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-excel.sheet.macroenabled.12",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/xml",
    "text/csv",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/tab-separated-values",
}
DOCUMENT_EXTENSION_MIME_TYPES = {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
    ".pdf": "application/pdf",
}
MAX_DOCUMENT_BYTES = 50_000_000
MAX_ZIP_MEMBER_BYTES = 2_000_000

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_UNSAFE_XML_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class DocumentRuntimeError(ValueError):
    """Raised when a document is unsupported, unsafe, malformed, or oversized."""


def document_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return (
        mimetypes.guess_type(path.name)[0]
        or DOCUMENT_EXTENSION_MIME_TYPES.get(suffix)
        or "application/octet-stream"
    )


def is_supported_document(path_or_name: str | Path, mime_type: str = "") -> bool:
    path = Path(path_or_name)
    mime = (mime_type or document_mime_type(path)).lower()
    return path.suffix.lower() in DOCUMENT_EXTENSIONS or mime in DOCUMENT_MIME_TYPES


def extract_document(path: Path, *, max_chars: int = 60_000) -> dict[str, Any]:
    path = path.resolve(strict=False)
    if not path.exists() or not path.is_file():
        raise DocumentRuntimeError(f"Document does not exist: {path}")
    size = path.stat().st_size
    if size > MAX_DOCUMENT_BYTES:
        raise DocumentRuntimeError(
            f"Document is too large for safe parsing ({size} > {MAX_DOCUMENT_BYTES} bytes)."
        )
    suffix = path.suffix.lower()
    mime_type = document_mime_type(path)
    warnings: list[str] = []
    if suffix in {".doc", ".xls"}:
        raise DocumentRuntimeError(
            "Legacy binary Office files are recognized but require conversion to DOCX/XLSX "
            "before text extraction or editing."
        )
    if suffix == ".docx":
        payload = _extract_docx(path)
    elif suffix in {".xlsx", ".xlsm"}:
        payload = _extract_xlsx(path)
    elif suffix == ".pdf" or mime_type == "application/pdf":
        payload = _extract_pdf(path)
    elif _looks_textual(suffix, mime_type):
        payload = _extract_textual(path)
    else:
        raise DocumentRuntimeError(f"Unsupported document type: {suffix or mime_type}")

    text = " ".join(str(payload.get("text") or "").split()) if payload["kind"] == "xlsx" else str(
        payload.get("text") or ""
    )
    text = text.strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip()
        warnings.append("Text was truncated to the requested max_chars.")
    payload.update(
        {
            "path": str(path),
            "name": path.name,
            "mime_type": mime_type,
            "size": size,
            "text": text,
            "truncated": truncated,
            "warnings": [*payload.get("warnings", []), *warnings],
        }
    )
    return payload


def compare_documents(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    max_diffs: int = 120,
) -> dict[str, Any]:
    left_lines = _comparison_lines(str(left.get("text") or ""))
    right_lines = _comparison_lines(str(right.get("text") or ""))
    diff = list(
        difflib.unified_diff(
            left_lines,
            right_lines,
            fromfile=str(left.get("name") or "left"),
            tofile=str(right.get("name") or "right"),
            lineterm="",
            n=2,
        )
    )
    additions = [line[1:] for line in diff if line.startswith("+") and not line.startswith("+++")]
    deletions = [line[1:] for line in diff if line.startswith("-") and not line.startswith("---")]
    return {
        "left": _document_summary(left),
        "right": _document_summary(right),
        "diff": diff[:max_diffs],
        "truncated": len(diff) > max_diffs,
        "additions": additions[:30],
        "deletions": deletions[:30],
        "stats": {
            "left_lines": len(left_lines),
            "right_lines": len(right_lines),
            "diff_lines": len(diff),
            "additions": len(additions),
            "deletions": len(deletions),
        },
    }


def apply_document_replacements(
    path: Path,
    replacements: list[dict[str, str]],
    output_path: Path,
) -> dict[str, Any]:
    suffix = path.suffix.lower()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".docx":
        changed = _replace_in_zip_xml(path, output_path, replacements, ["word/document.xml"])
    elif suffix in {".xlsx", ".xlsm"}:
        changed = _replace_in_zip_xml(
            path,
            output_path,
            replacements,
            ["xl/sharedStrings.xml"],
            prefix_matches=("xl/worksheets/",),
        )
    elif _looks_textual(suffix, document_mime_type(path)):
        text = path.read_text(encoding="utf-8", errors="replace")
        text, changed = _replace_plain_text(text, replacements)
        output_path.write_text(text, encoding="utf-8", newline="")
    else:
        raise DocumentRuntimeError(
            "Only DOCX, XLSX/XLSM, and text-like files support replacements."
        )
    if changed == 0:
        output_path.unlink(missing_ok=True)
        raise DocumentRuntimeError(
            "No replacement text was found; original document was unchanged."
        )
    return {
        "path": str(output_path),
        "changed": changed,
        "kind": suffix.lstrip(".") or "text",
    }


def copy_document(path: Path, output_path: Path) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, output_path)
    return {"path": str(output_path), "size": output_path.stat().st_size}


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_artifact_output_path(
    output_root: Path,
    *,
    output_path: str | Path | None = None,
    output_name: str | None = None,
    default_name: str = "artifact.md",
    collision_safe: bool = True,
) -> Path:
    """Bind an operator-requested destination under the document-outputs root.

    Accepts absolute paths inside the root, root-relative paths with
    subdirectories (for example ``functional-20260713/report.md``), or a
    simple basename. Never rewrites sources; only allocates under
    ``output_root``.
    """

    root = Path(output_root).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    destination: Path | None = None
    raw_path = str(output_path or "").strip()
    raw_name = str(output_name or "").strip()
    if raw_path:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            destination = candidate.resolve(strict=False)
        else:
            destination = (root / candidate).resolve(strict=False)
    elif raw_name:
        # Preserve relative subdirectories from output_name when present.
        name_path = Path(raw_name.replace("\\", "/"))
        safe_parts = [
            re.sub(r"[^\w.\- ()\[\]]+", "_", part).strip(" .")
            for part in name_path.parts
            if part not in {"", ".", ".."}
        ]
        if not safe_parts:
            safe_parts = [_safe_artifact_filename(default_name)]
        else:
            safe_parts[-1] = _safe_artifact_filename(safe_parts[-1]) or safe_parts[-1]
        destination = (root.joinpath(*safe_parts)).resolve(strict=False)
    else:
        destination = (root / _safe_artifact_filename(default_name)).resolve(strict=False)

    try:
        destination.relative_to(root)
    except ValueError as exc:
        raise DocumentRuntimeError(
            f"output path escapes document-outputs root: {destination}"
        ) from exc
    if destination.exists() and destination.is_dir():
        raise DocumentRuntimeError(f"output_path is a directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and collision_safe:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        destination = destination.with_name(
            f"{destination.stem}.{stamp}{destination.suffix}"
        )
    return destination


def write_exact_text_artifact(
    output_path: Path,
    body: str,
    *,
    encoding: str = "utf-8",
) -> dict[str, Any]:
    """Write operator body bytes-for-bytes (UTF-8 text) without generator wrappers."""

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = str(body if body is not None else "")
    if not text.endswith("\n"):
        text = text + "\n"
    destination.write_text(text, encoding=encoding, newline="\n")
    verification = verify_document_artifact(destination, expected_format="text")
    return {
        "path": str(destination),
        "name": destination.name,
        "size": destination.stat().st_size,
        "sha256": file_sha256(destination),
        "exact_body": True,
        "verification": verification,
    }


def write_markdown_docx(
    output_path: Path,
    markdown_text: str,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Convert Markdown text into a structurally valid DOCX with headings/tables."""

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    blocks = _parse_markdown_blocks(markdown_text)
    if title and not any(block.get("type") == "heading" for block in blocks[:1]):
        blocks.insert(0, {"type": "heading", "level": 1, "text": title})
    _write_structured_docx(destination, blocks, title=title or "Document")
    verification = verify_document_artifact(destination, expected_format="docx")
    extracted = extract_document(destination)
    structure = dict(extracted.get("structure") or {})
    return {
        "path": str(destination),
        "name": destination.name,
        "size": destination.stat().st_size,
        "sha256": file_sha256(destination),
        "format": "docx",
        "structure": structure,
        "verification": verification,
        "heading_count": len(
            [block for block in blocks if block.get("type") == "heading"]
        ),
        "table_count": len([block for block in blocks if block.get("type") == "table"]),
    }


def verify_document_artifact(
    path: Path,
    *,
    expected_format: str | None = None,
) -> dict[str, Any]:
    """Post-write verification: existence, non-empty, and structural validity."""

    target = Path(path)
    if not target.exists() or not target.is_file():
        raise DocumentRuntimeError(f"claimed artifact missing: {target}")
    size = target.stat().st_size
    if size <= 0:
        raise DocumentRuntimeError(f"claimed artifact is empty: {target}")
    fmt = (expected_format or target.suffix.lstrip(".") or "bin").lower()
    if fmt == "markdown":
        fmt = "md"
    result: dict[str, Any] = {
        "path": str(target),
        "exists": True,
        "size": size,
        "sha256": file_sha256(target),
        "format": fmt,
        "ok": True,
    }
    if fmt == "docx":
        if not zipfile.is_zipfile(target):
            raise DocumentRuntimeError(f"DOCX is not a valid ZIP package: {target}")
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise DocumentRuntimeError(f"DOCX has duplicate ZIP members: {target}")
            required = {
                "[Content_Types].xml",
                "_rels/.rels",
                "word/document.xml",
            }
            missing = sorted(required - set(names))
            if missing:
                raise DocumentRuntimeError(
                    f"DOCX missing required members {missing}: {target}"
                )
            document_xml = archive.read("word/document.xml")
            try:
                ET.fromstring(document_xml)
            except ET.ParseError as exc:
                raise DocumentRuntimeError(
                    f"DOCX word/document.xml is not well-formed: {exc}"
                ) from exc
            result["zip_members"] = len(names)
            result["has_document_xml"] = True
    elif fmt in {"md", "txt", "text", "csv", "json", "html", "htm"}:
        # Strict UTF-8 decode proves text artifacts are not binary garbage.
        target.read_text(encoding="utf-8")
        result["utf8"] = True
    return result


def _safe_artifact_filename(value: str) -> str:
    cleaned = re.sub(r"[^\w.\- ()\[\]]+", "_", Path(str(value or "")).name).strip(" .")
    return cleaned[:180] or "artifact.bin"


def _parse_markdown_blocks(markdown_text: str) -> list[dict[str, Any]]:
    lines = str(markdown_text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            blocks.append(
                {
                    "type": "heading",
                    "level": len(heading.group(1)),
                    "text": heading.group(2).strip(),
                }
            )
            index += 1
            continue
        if "|" in line and index + 1 < len(lines) and re.match(
            r"^\s*\|?\s*:?-{3,}",
            lines[index + 1],
        ):
            table_lines = [line]
            index += 1
            while index < len(lines) and "|" in lines[index]:
                table_lines.append(lines[index])
                index += 1
            rows = _markdown_table_rows(table_lines)
            if rows:
                blocks.append({"type": "table", "rows": rows})
            continue
        if not line.strip():
            blocks.append({"type": "empty"})
            index += 1
            continue
        # Paragraph: accumulate until blank/heading/table.
        paragraph_lines = [line.rstrip()]
        index += 1
        while index < len(lines):
            nxt = lines[index]
            if (
                not nxt.strip()
                or re.match(r"^(#{1,6})\s+", nxt)
                or (
                    "|" in nxt
                    and index + 1 < len(lines)
                    and re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1])
                )
            ):
                break
            paragraph_lines.append(nxt.rstrip())
            index += 1
        blocks.append({"type": "paragraph", "text": " ".join(paragraph_lines).strip()})
    return blocks


def _markdown_table_rows(table_lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line_index, line in enumerate(table_lines):
        stripped = line.strip().strip("|")
        cells = [cell.strip() for cell in stripped.split("|")]
        if line_index == 1 and cells and all(re.match(r"^:?-{3,}:?$", cell) for cell in cells):
            continue
        if any(cells):
            rows.append(cells)
    return rows


def _write_structured_docx(
    path: Path,
    blocks: list[dict[str, Any]],
    *,
    title: str,
) -> None:
    body_xml: list[str] = []
    for block in blocks:
        kind = str(block.get("type") or "")
        if kind == "heading":
            level = max(1, min(6, int(block.get("level") or 1)))
            text = html.escape(str(block.get("text") or ""), quote=False)
            style = f"Heading{level}"
            body_xml.append(
                f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
                f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
            )
        elif kind == "table":
            rows = list(block.get("rows") or [])
            if not rows:
                continue
            row_xml: list[str] = []
            for row in rows:
                cells = list(row)
                cell_xml = []
                for cell in cells:
                    cell_text = html.escape(str(cell), quote=False)
                    cell_xml.append(
                        "<w:tc><w:tcPr><w:tcW w:w=\"2400\" w:type=\"dxa\"/></w:tcPr>"
                        f'<w:p><w:r><w:t xml:space="preserve">{cell_text}</w:t></w:r></w:p>'
                        "</w:tc>"
                    )
                row_xml.append(f"<w:tr>{''.join(cell_xml)}</w:tr>")
            col_count = max(len(row) for row in rows)
            grid = "".join('<w:gridCol w:w="2400"/>' for _ in range(col_count))
            body_xml.append(
                f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/></w:tblPr>'
                f"<w:tblGrid>{grid}</w:tblGrid>{''.join(row_xml)}</w:tbl>"
            )
        elif kind == "empty":
            body_xml.append("<w:p/>")
        else:
            text = html.escape(str(block.get("text") or ""), quote=False)
            if not text:
                body_xml.append("<w:p/>")
            else:
                body_xml.append(
                    f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
                )
    if not body_xml:
        body_xml.append(
            f'<w:p><w:r><w:t xml:space="preserve">{html.escape(title)}</w:t></w:r></w:p>'
        )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>'
        + "".join(body_xml)
        + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
        "</w:sectPr></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/'
        'relationships/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        "</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:styles xmlns:w="{_W_NS}">'
        '<w:style w:type="paragraph" w:styleId="Normal">'
        '<w:name w:val="Normal"/></w:style>'
        + "".join(
            f'<w:style w:type="paragraph" w:styleId="Heading{level}">'
            f'<w:name w:val="heading {level}"/>'
            f'<w:basedOn w:val="Normal"/>'
            f'<w:uiPriority w:val="{level}"/>'
            f"</w:style>"
            for level in range(1, 7)
        )
        + "</w:styles>"
    )
    word_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        "</Relationships>"
    )
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<cp:coreProperties xmlns:cp="{_CP_NS}" xmlns:dc="{_DC_NS}">'
        f"<dc:title>{html.escape(title)}</dc:title>"
        "<dc:creator>jarvis</dc:creator>"
        "<cp:lastModifiedBy>jarvis</cp:lastModifiedBy>"
        "</cp:coreProperties>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/_rels/document.xml.rels", word_rels)
        archive.writestr("docProps/core.xml", core)


def _extract_docx(path: Path) -> dict[str, Any]:
    if not zipfile.is_zipfile(path):
        raise DocumentRuntimeError("DOCX is not a valid ZIP package.")
    with zipfile.ZipFile(path) as archive:
        document_xml = _read_zip_text_member(archive, "word/document.xml")
        comments_xml = _read_zip_text_member(archive, "word/comments.xml")
        styles_xml = _read_zip_text_member(archive, "word/styles.xml")
    if not document_xml:
        raise DocumentRuntimeError("DOCX has no readable word/document.xml.")
    root = _parse_xml(document_xml, "DOCX document")
    body = root.find(f".//{{{_W_NS}}}body")
    paragraphs: list[str] = []
    tables: list[dict[str, Any]] = []
    if body is not None:
        for child in list(body):
            if child.tag == f"{{{_W_NS}}}p":
                text = _word_text(child)
                if text:
                    paragraphs.append(text)
            elif child.tag == f"{{{_W_NS}}}tbl":
                rows = _word_table_rows(child)
                if rows:
                    tables.append({"rows": rows[:12], "row_count": len(rows)})
    comments = _word_comments(comments_xml)
    style_names = _word_style_names(styles_xml)
    lines = [*paragraphs]
    for index, table in enumerate(tables, start=1):
        lines.append(f"Table {index}:")
        lines.extend(" | ".join(row) for row in table["rows"][:8])
    if comments:
        lines.append("Comments:")
        lines.extend(comments[:20])
    return {
        "kind": "docx",
        "text": "\n".join(lines),
        "structure": {
            "paragraph_count": len(paragraphs),
            "table_count": len(tables),
            "comment_count": len(comments),
            "headings": _docx_headings(paragraphs),
            "styles": style_names[:40],
            "tables": tables[:8],
        },
        "warnings": [],
    }


def _extract_xlsx(path: Path) -> dict[str, Any]:
    if not zipfile.is_zipfile(path):
        raise DocumentRuntimeError("XLSX is not a valid ZIP package.")
    with zipfile.ZipFile(path) as archive:
        shared_strings = _xlsx_shared_strings(archive)
        sheet_map = _xlsx_sheet_map(archive)
        style_count = _xlsx_style_count(archive)
        sheets: list[dict[str, Any]] = []
        for sheet_name, member_name in sheet_map[:20]:
            xml = _read_zip_text_member(archive, member_name)
            if not xml:
                continue
            sheets.append(_xlsx_sheet_payload(sheet_name, xml, shared_strings))
    lines: list[str] = []
    for sheet in sheets:
        lines.append(f"Sheet: {sheet['name']}")
        for row in sheet["preview_rows"][:30]:
            lines.append(" | ".join(str(cell) for cell in row))
        if sheet["formulas"]:
            lines.append("Formulas: " + "; ".join(sheet["formulas"][:20]))
    return {
        "kind": "xlsx",
        "text": "\n".join(lines),
        "structure": {
            "sheet_count": len(sheets),
            "sheets": sheets,
            "formula_count": sum(len(sheet["formulas"]) for sheet in sheets),
            "style_count": style_count,
        },
        "warnings": [],
    }


def _extract_pdf(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    _assert_pdf_not_corrupt(path, data)
    warnings: list[str] = []
    text = ""
    pages = len(re.findall(rb"/Type\s*/Page\b", data))
    parser_error: str | None = None
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        reader = PdfReader(str(path))
        if getattr(reader, "is_encrypted", False):
            raise DocumentRuntimeError(
                "PDF is encrypted and cannot be read. Provide an unlocked PDF and retry."
            )
        pages = len(reader.pages)
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except DocumentRuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        parser_error = str(exc)
        text = _extract_pdf_text_basic(data)
        warnings.append("Used basic PDF extraction; scanned/compressed PDFs may need OCR.")
    text = text.strip()
    if not text and (
        parser_error is not None or not _pdf_structure_looks_complete(data)
    ):
        detail = parser_error or "truncated or incomplete PDF structure"
        raise DocumentRuntimeError(
            "PDF is corrupt or unreadable "
            f"({detail}). Replace the file with a valid PDF and retry; "
            "do not treat empty extraction as success."
        )
    return {
        "kind": "pdf",
        "text": text,
        "structure": {"page_count": pages},
        "warnings": warnings,
    }


def _assert_pdf_not_corrupt(path: Path, data: bytes) -> None:
    if not data:
        raise DocumentRuntimeError(
            f"PDF is empty and unreadable: {path.name}. Upload a valid PDF and retry."
        )
    if not data.lstrip().startswith(b"%PDF"):
        raise DocumentRuntimeError(
            f"PDF is corrupt or not a PDF (missing %PDF header): {path.name}. "
            "Replace the file with a valid PDF and retry."
        )
    # Intentionally truncated fixtures and broken partial downloads end without a trailer.
    if not _pdf_structure_looks_complete(data):
        raise DocumentRuntimeError(
            f"PDF is corrupt or truncated (incomplete trailer/objects): {path.name}. "
            "Replace the file with a complete valid PDF and retry."
        )


def _pdf_structure_looks_complete(data: bytes) -> bool:
    if b"%%EOF" in data:
        return True
    # Very small payloads without EOF are treated as truncated by contract.
    if len(data) < 256:
        return False
    # Larger files without EOF may still be valid linearized PDFs; require page markers.
    return bool(re.search(rb"/Type\s*/Page\b", data)) and bool(
        re.search(rb"/Root\b", data) or re.search(rb"startxref", data)
    )


def normalize_document_parse_error(exc: BaseException, *, path: Path | None = None) -> dict[str, Any]:
    """Normalize parser failure into one actionable, non-success recovery payload."""

    name = path.name if path is not None else None
    message = str(exc).strip() or exc.__class__.__name__
    actionable = (
        message
        if "retry" in message.casefold()
        else f"{message.rstrip('.')} Replace the document with a valid file and retry."
    )
    return {
        "ok": False,
        "status": "failed",
        "error": actionable[:2000],
        "error_code": "document_parse_failed",
        "actionable": True,
        "retryable": True,
        "partial_result": None,
        "stale_content": False,
        "name": name,
        "path": str(path) if path is not None else None,
    }


def extract_document_safe(path: Path, *, max_chars: int = 60_000) -> dict[str, Any]:
    """Extract a document or return a normalized failed recovery result (never false success)."""

    target = Path(path)
    try:
        payload = extract_document(target, max_chars=max_chars)
        return {
            "ok": True,
            "status": "readable",
            "document": payload,
            "error": None,
            "partial_result": None,
            "stale_content": False,
        }
    except DocumentRuntimeError as exc:
        return normalize_document_parse_error(exc, path=target)


def _extract_textual(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    text = _decode_document_bytes(data)
    suffix = path.suffix.lower()
    kind = suffix.lstrip(".") or "text"
    if suffix in {".html", ".htm"} or "<html" in text[:1000].lower():
        text = _html_to_text(text)
        kind = "html"
    return {"kind": kind, "text": text, "structure": {}, "warnings": []}


def _read_zip_text_member(archive: zipfile.ZipFile, name: str) -> str:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return ""
    if info.file_size > MAX_ZIP_MEMBER_BYTES:
        return ""
    with archive.open(info) as member:
        data = member.read(MAX_ZIP_MEMBER_BYTES + 1)
    if len(data) > MAX_ZIP_MEMBER_BYTES:
        return ""
    return data.decode("utf-8", errors="replace")


def _parse_xml(xml: str, label: str) -> ET.Element:
    if _UNSAFE_XML_DECLARATION.search(xml):
        raise DocumentRuntimeError(
            f"Unsafe {label} XML: DTD and entity declarations are not allowed."
        )
    try:
        return ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DocumentRuntimeError(f"Invalid {label} XML: {exc}") from exc


def _word_text(element: ET.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        if node.tag == f"{{{_W_NS}}}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{{{_W_NS}}}tab":
            parts.append("\t")
        elif node.tag == f"{{{_W_NS}}}br":
            parts.append("\n")
    return "".join(parts).strip()


def _word_table_rows(table: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.findall(f".//{{{_W_NS}}}tr"):
        values = [_word_text(cell) for cell in row.findall(f"./{{{_W_NS}}}tc")]
        if any(values):
            rows.append(values)
    return rows


def _word_comments(xml: str) -> list[str]:
    if not xml:
        return []
    root = _parse_xml(xml, "DOCX comments")
    comments = []
    for item in root.findall(f".//{{{_W_NS}}}comment"):
        text = _word_text(item)
        if text:
            comments.append(text)
    return comments


def _word_style_names(xml: str) -> list[str]:
    if not xml:
        return []
    root = _parse_xml(xml, "DOCX styles")
    names: list[str] = []
    for style in root.findall(f".//{{{_W_NS}}}style"):
        name = style.find(f"./{{{_W_NS}}}name")
        value = name.attrib.get(f"{{{_W_NS}}}val") if name is not None else None
        if value:
            names.append(value)
    return names


def _docx_headings(paragraphs: list[str]) -> list[str]:
    return [item for item in paragraphs if len(item) <= 120][:20]


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    xml = _read_zip_text_member(archive, "xl/sharedStrings.xml")
    if not xml:
        return []
    root = _parse_xml(xml, "XLSX shared strings")
    strings: list[str] = []
    for item in root.findall(f".//{{{_A_NS}}}si"):
        parts = [node.text or "" for node in item.findall(f".//{{{_A_NS}}}t")]
        strings.append("".join(parts))
    return strings


def _xlsx_sheet_map(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook_xml = _read_zip_text_member(archive, "xl/workbook.xml")
    rels_xml = _read_zip_text_member(archive, "xl/_rels/workbook.xml.rels")
    if not workbook_xml:
        names = [
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/") and name.endswith(".xml")
        ]
        return [(Path(name).stem, name) for name in names]
    rels: dict[str, str] = {}
    if rels_xml:
        rel_root = _parse_xml(rels_xml, "XLSX workbook relationships")
        for rel in rel_root.findall(f".//{{{_REL_NS}}}Relationship"):
            rel_id = str(rel.attrib.get("Id") or "")
            target = str(rel.attrib.get("Target") or "")
            if target.startswith("/"):
                target = target.lstrip("/")
            elif not target.startswith("xl/"):
                target = f"xl/{target}"
            rels[rel_id] = target
    root = _parse_xml(workbook_xml, "XLSX workbook")
    result: list[tuple[str, str]] = []
    for sheet in root.findall(f".//{{{_A_NS}}}sheet"):
        name = str(sheet.attrib.get("name") or "Sheet")
        rel_id = str(sheet.attrib.get(f"{{{_R_NS}}}id") or "")
        member = rels.get(rel_id)
        if member:
            result.append((name, member))
    return result


def _xlsx_style_count(archive: zipfile.ZipFile) -> int:
    xml = _read_zip_text_member(archive, "xl/styles.xml")
    if not xml:
        return 0
    try:
        root = _parse_xml(xml, "XLSX styles")
    except DocumentRuntimeError:
        return 0
    return len(root.findall(f".//{{{_A_NS}}}cellXfs/{{{_A_NS}}}xf"))


def _xlsx_sheet_payload(
    sheet_name: str,
    xml: str,
    shared_strings: list[str],
) -> dict[str, Any]:
    root = _parse_xml(xml, f"XLSX sheet {sheet_name}")
    rows: list[list[str]] = []
    formulas: list[str] = []
    max_col = 0
    for row in root.findall(f".//{{{_A_NS}}}row")[:200]:
        cells: dict[int, str] = {}
        for cell in row.findall(f"./{{{_A_NS}}}c")[:80]:
            ref = str(cell.attrib.get("r") or "")
            col = _xlsx_col_index(ref)
            max_col = max(max_col, col)
            value = _xlsx_cell_value(cell, shared_strings)
            formula = cell.find(f"./{{{_A_NS}}}f")
            if formula is not None and formula.text:
                formulas.append(f"{ref}={formula.text}")
            if value:
                cells[col] = value
        if cells:
            row_values = [cells.get(index, "") for index in range(1, max(cells) + 1)]
            rows.append(row_values)
    return {
        "name": sheet_name,
        "rows": len(rows),
        "cols": max_col,
        "preview_rows": rows[:50],
        "formulas": formulas[:200],
    }


def _xlsx_cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        parts = [node.text or "" for node in cell.findall(f".//{{{_A_NS}}}t")]
        return "".join(parts)
    value = cell.find(f"./{{{_A_NS}}}v")
    raw = value.text if value is not None else ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return raw or ""
    return raw or ""


def _xlsx_col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref.upper())
    if not letters:
        return 1
    value = 0
    for char in letters.group(1):
        value = value * 26 + (ord(char) - ord("A") + 1)
    return max(1, value)


def _replace_in_zip_xml(
    path: Path,
    output_path: Path,
    replacements: list[dict[str, str]],
    exact_members: list[str],
    *,
    prefix_matches: tuple[str, ...] = (),
) -> int:
    changed = 0
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(output_path, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            should_patch = info.filename in exact_members or any(
                info.filename.startswith(prefix) for prefix in prefix_matches
            )
            if should_patch:
                text = data.decode("utf-8", errors="replace")
                text, count = _replace_xml_text(text, replacements)
                data = text.encode("utf-8")
                changed += count
            target.writestr(info, data)
    return changed


def _replace_xml_text(xml: str, replacements: list[dict[str, str]]) -> tuple[str, int]:
    changed = 0
    for item in replacements:
        old = str(item.get("old") or "")
        new = str(item.get("new") or "")
        if not old:
            continue
        escaped_old = html.escape(old, quote=False)
        escaped_new = html.escape(new, quote=False)
        xml, count = _replace_count(xml, escaped_old, escaped_new)
        changed += count
    return xml, changed


def _replace_plain_text(text: str, replacements: list[dict[str, str]]) -> tuple[str, int]:
    changed = 0
    for item in replacements:
        old = str(item.get("old") or "")
        new = str(item.get("new") or "")
        if not old:
            continue
        text, count = _replace_count(text, old, new)
        changed += count
    return text, changed


def _replace_count(value: str, old: str, new: str) -> tuple[str, int]:
    count = value.count(old)
    if count:
        value = value.replace(old, new)
    return value, count


def _comparison_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _document_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": payload.get("name"),
        "path": payload.get("path"),
        "kind": payload.get("kind"),
        "chars": len(str(payload.get("text") or "")),
        "structure": payload.get("structure") or {},
        "warnings": payload.get("warnings") or [],
    }


def _looks_textual(suffix: str, mime_type: str) -> bool:
    return (
        suffix in {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".html", ".htm", ".log"}
        or mime_type.startswith("text/")
        or mime_type in {"application/json", "application/xml"}
    )


def _decode_document_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "cp1251", "latin1"):
        try:
            return _repair_mojibake(data.decode(encoding))
        except UnicodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"[ \t]+", " ", value).strip()


def _repair_mojibake(value: str) -> str:
    markers = ("Р", "С", "Ð", "Ñ")
    if sum(value.count(marker) for marker in markers) < 3:
        return value
    try:
        repaired = value.encode("latin1").decode("utf-8")
    except UnicodeError:
        return value
    return repaired if repaired.count("�") <= value.count("�") else value


def _extract_pdf_text_basic(data: bytes) -> str:
    raw = data.decode("latin1", errors="ignore")
    parts: list[str] = []
    for match in re.finditer(r"\((?P<text>(?:\\.|[^\\()]){2,})\)\s*T[Jj]", raw):
        text = _pdf_unescape(match.group("text"))
        if _pdf_text_is_useful(text):
            parts.append(text)
    if not parts:
        for match in re.finditer(r"\((?P<text>(?:\\.|[^\\()]){4,})\)", raw):
            text = _pdf_unescape(match.group("text"))
            if _pdf_text_is_useful(text):
                parts.append(text)
            if len(parts) >= 400:
                break
    return _repair_mojibake(" ".join(parts))


def _pdf_unescape(value: str) -> str:
    value = re.sub(r"\\([()\\])", r"\1", value)
    value = re.sub(r"\\([0-7]{1,3})", lambda match: chr(int(match.group(1), 8)), value)
    return value.replace("\\n", "\n").replace("\\r", "\n").replace("\\t", "\t")


def _pdf_text_is_useful(value: str) -> bool:
    clean = " ".join(value.split())
    if len(clean) < 3:
        return False
    printable = sum(1 for char in clean if char.isprintable())
    return printable / max(1, len(clean)) > 0.85 and any(ch.isalnum() for ch in clean)
