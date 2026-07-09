from __future__ import annotations

import difflib
import html
import mimetypes
import re
import shutil
import zipfile
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


class DocumentRuntimeError(ValueError):
    pass


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
        },
        "warnings": [],
    }


def _extract_pdf(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    warnings: list[str] = []
    text = ""
    pages = len(re.findall(rb"/Type\s*/Page\b", data))
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        reader = PdfReader(str(path))
        pages = len(reader.pages)
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:  # noqa: BLE001
        text = _extract_pdf_text_basic(data)
        warnings.append("Used basic PDF extraction; scanned/compressed PDFs may need OCR.")
    return {
        "kind": "pdf",
        "text": text.strip(),
        "structure": {"page_count": pages},
        "warnings": warnings,
    }


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
