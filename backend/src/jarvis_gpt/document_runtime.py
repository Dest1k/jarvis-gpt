from __future__ import annotations

import csv
import difflib
import hashlib
import html
import math
import mimetypes
import os
import re
import shutil
import struct
import textwrap
import zipfile
import zlib
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
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A_DRAW = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PML = "application/vnd.openxmlformats-officedocument.presentationml"
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


def edit_docx_document(
    source_path: Path,
    operations: list[dict[str, Any]],
    output_path: Path,
) -> dict[str, Any]:
    """Edit an existing DOCX and write a new version, preserving the source package
    byte-for-byte except ``word/document.xml``. Supported ops (the ``op`` key):

    - ``replace`` {old, new}                      exact visible-text replacement
    - ``replacements`` {items:[{old,new}, ...]}   several replacements at once
    - ``append_section`` {title?, level?, body}   heading + Markdown body at the end
    - ``append_paragraph`` / ``append`` {text}    Markdown appended at the end
    - ``append_markdown`` {markdown}              raw Markdown appended at the end

    Appended content is self-contained (no new hyperlink/numbering parts): links become
    ``text (url)`` and list items get a bullet/number prefix. Replacements match only
    within a single run (Word may split a phrase across runs)."""

    src = Path(source_path).resolve(strict=False)
    if src.suffix.lower() != ".docx":
        raise DocumentRuntimeError(f"Not a DOCX document: {src.suffix or src.name}")
    if not zipfile.is_zipfile(src):
        raise DocumentRuntimeError("DOCX is not a valid ZIP package.")
    with zipfile.ZipFile(src) as archive:
        members = [(info, archive.read(info.filename)) for info in archive.infolist()]
    document_bytes = next(
        (data for info, data in members if info.filename == "word/document.xml"), None
    )
    if document_bytes is None:
        raise DocumentRuntimeError("DOCX has no word/document.xml.")
    document_xml = document_bytes.decode("utf-8")
    changes: list[str] = []
    replacements: list[dict[str, str]] = []
    append_blocks: list[dict[str, Any]] = []
    for raw_op in operations or []:
        if not isinstance(raw_op, dict):
            continue
        op = str(raw_op.get("op") or raw_op.get("action") or "").strip().casefold()
        if op in {"replace", "substitute"}:
            old = str(raw_op.get("old") or raw_op.get("find") or "")
            new = str(raw_op.get("new") or raw_op.get("value") or raw_op.get("replace") or "")
            if old:
                replacements.append({"old": old, "new": new})
        elif op in {"replacements", "replace_all"}:
            for item in raw_op.get("items") or raw_op.get("replacements") or []:
                if isinstance(item, dict) and item.get("old"):
                    replacements.append(
                        {"old": str(item["old"]), "new": str(item.get("new") or "")}
                    )
        elif op in {"append_section", "add_section"}:
            title = str(raw_op.get("title") or raw_op.get("heading") or "").strip()
            body = str(raw_op.get("body") or raw_op.get("content") or raw_op.get("text") or "")
            if title:
                level = max(1, min(6, int(raw_op.get("level") or 2)))
                append_blocks.append({"type": "heading", "level": level, "text": title})
            append_blocks.extend(_parse_markdown_blocks(body))
            changes.append(f"appended section {title!r}" if title else "appended content")
        elif op in {"append_paragraph", "append_text", "append", "add_paragraph"}:
            text = str(raw_op.get("text") or raw_op.get("value") or raw_op.get("body") or "")
            if text.strip():
                append_blocks.extend(_parse_markdown_blocks(text))
                changes.append("appended a paragraph")
        elif op in {"append_markdown", "append_md"}:
            markdown = str(raw_op.get("markdown") or raw_op.get("text") or "")
            if markdown.strip():
                append_blocks.extend(_parse_markdown_blocks(markdown))
                changes.append("appended Markdown content")
        else:
            raise DocumentRuntimeError(f"unsupported docx edit op: {op or '(missing op)'}")
    if replacements:
        document_xml, changed = _replace_xml_text(document_xml, replacements)
        if changed == 0:
            raise DocumentRuntimeError(
                "None of the replacement text was found in the document (Word may split a "
                "phrase across runs; try a shorter, contiguous snippet)."
            )
        changes.append(f"replaced {changed} occurrence(s)")
    if append_blocks:
        document_xml = _docx_insert_body(document_xml, _docx_append_blocks_xml(append_blocks))
    if not changes:
        raise DocumentRuntimeError("No document edit operation changed anything.")
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in members:
            if info.filename == "word/document.xml":
                out.writestr(info, document_xml.encode("utf-8"))
            else:
                out.writestr(info, data)
    verification = verify_document_artifact(dest, expected_format="docx")
    extracted = extract_document(dest)
    return {
        "path": str(dest),
        "name": dest.name,
        "size": dest.stat().st_size,
        "sha256": file_sha256(dest),
        "format": "docx",
        "changes": changes,
        "structure": dict(extracted.get("structure") or {}),
        "verification": verification,
    }


def edit_text_document(
    source_path: Path,
    operations: list[dict[str, Any]],
    output_path: Path,
) -> dict[str, Any]:
    """Edit a plain-text / Markdown / CSV-like document and write a new version. Ops:
    ``append``/``prepend`` {text}, ``replace`` {old,new}, ``insert_after`` {anchor,text},
    ``set_text`` {text}."""

    src = Path(source_path).resolve(strict=False)
    text = src.read_text(encoding="utf-8", errors="replace")
    changes: list[str] = []
    for raw_op in operations or []:
        if not isinstance(raw_op, dict):
            continue
        op = str(raw_op.get("op") or raw_op.get("action") or "").strip().casefold()
        addition = str(raw_op.get("text") or raw_op.get("value") or "")
        if op in {"append", "add", "append_text"}:
            if addition:
                text = text + ("" if not text or text.endswith("\n") else "\n") + addition
                changes.append("appended text")
        elif op in {"prepend", "prepend_text"}:
            if addition:
                text = addition + ("" if addition.endswith("\n") else "\n") + text
                changes.append("prepended text")
        elif op in {"replace", "substitute"}:
            old = str(raw_op.get("old") or raw_op.get("find") or "")
            new = str(raw_op.get("new") or raw_op.get("replace") or raw_op.get("value") or "")
            if old and old in text:
                text = text.replace(old, new)
                changes.append(f"replaced {old!r}")
            elif old:
                raise DocumentRuntimeError(f"text to replace was not found: {old!r}")
        elif op in {"insert_after"}:
            anchor = str(raw_op.get("anchor") or "")
            index = text.find(anchor) if anchor else -1
            if anchor and index >= 0:
                cut = index + len(anchor)
                lead = "" if addition.startswith("\n") else "\n"
                text = text[:cut] + lead + addition + text[cut:]
                changes.append(f"inserted after {anchor!r}")
            elif anchor:
                raise DocumentRuntimeError(f"anchor was not found: {anchor!r}")
        elif op in {"set_text", "replace_all_text", "set"}:
            text = addition
            changes.append("replaced the full text")
        else:
            raise DocumentRuntimeError(f"unsupported text edit op: {op or '(missing op)'}")
    if not changes:
        raise DocumentRuntimeError("No text edit operation changed anything.")
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    dest.write_text(text, encoding="utf-8", newline="\n")
    verification = verify_document_artifact(dest)
    return {
        "path": str(dest),
        "name": dest.name,
        "size": dest.stat().st_size,
        "sha256": file_sha256(dest),
        "changes": changes,
        "verification": verification,
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
    allow_overwrite: bool = False,
) -> Path:
    """Bind an operator-requested destination under the document-outputs root.

    Accepts absolute paths inside the root, root-relative paths with
    subdirectories (for example ``functional-20260713/report.md``), or a
    simple basename. Never rewrites sources; only allocates under
    ``output_root``.

    When ``collision_safe`` is True (default), an existing file is not
    overwritten — a timestamp suffix is allocated instead. When the operator
    requires an *exact* destination (``collision_safe=False``), a collision
    raises unless ``allow_overwrite`` is explicitly True. Timestamp fallback
    must never silently replace an exact requested path.
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
    if destination.exists() and collision_safe and not allow_overwrite:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        destination = destination.with_name(
            f"{destination.stem}.{stamp}{destination.suffix}"
        )
    elif destination.exists() and not allow_overwrite:
        raise DocumentRuntimeError(
            f"refusing to overwrite existing artifact without explicit permission: "
            f"{destination}"
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


def write_workbook_xlsx(
    output_path: Path,
    sheets: list[dict[str, Any]],
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Write a structurally valid multi-sheet XLSX workbook.

    ``sheets`` is a list of ``{"name": str, "rows": [[cell, ...], ...]}``. Cells are
    numbers, booleans or strings; a string whose text starts with ``=`` becomes a
    formula. The first row of each sheet is a bold, shaded, frozen header with an
    auto-filter, and columns are sized to their content — so the output opens as a
    real, usable spreadsheet rather than a flat dump.
    """

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_workbook_sheets(sheets)
    _write_workbook_xlsx(destination, normalized, title=title or "Workbook")
    verification = verify_document_artifact(destination, expected_format="xlsx")
    extracted = extract_document(destination)
    structure = dict(extracted.get("structure") or {})
    return {
        "path": str(destination),
        "name": destination.name,
        "size": destination.stat().st_size,
        "sha256": file_sha256(destination),
        "format": "xlsx",
        "structure": structure,
        "verification": verification,
        "sheet_count": len(normalized),
        "row_count": sum(len(sheet["rows"]) for sheet in normalized),
    }


# --------------------------------------------------------------------------- #
# PPTX — hand-rolled OpenXML presentation (mirrors the DOCX/XLSX writers).
# --------------------------------------------------------------------------- #

_PPTX_MASTER_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<p:sldMaster xmlns:a="{_A_DRAW}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}">'
    "<p:cSld><p:spTree>"
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
    '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
    "</p:spTree></p:cSld>"
    '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" '
    'accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" '
    'accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
    '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>'
    "<p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles>"
    "</p:sldMaster>"
)

_PPTX_LAYOUT_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<p:sldLayout xmlns:a="{_A_DRAW}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}" '
    'type="blank" preserve="1">'
    '<p:cSld name="Blank"><p:spTree>'
    '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
    '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
    '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
    "</p:spTree></p:cSld>"
    "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>"
    "</p:sldLayout>"
)

_PPTX_THEME_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<a:theme xmlns:a="{_A_DRAW}" name="Office Theme"><a:themeElements>'
    '<a:clrScheme name="Office">'
    '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="44546A"/></a:dk2>'
    '<a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>'
    '<a:accent1><a:srgbClr val="4472C4"/></a:accent1>'
    '<a:accent2><a:srgbClr val="ED7D31"/></a:accent2>'
    '<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3>'
    '<a:accent4><a:srgbClr val="FFC000"/></a:accent4>'
    '<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5>'
    '<a:accent6><a:srgbClr val="70AD47"/></a:accent6>'
    '<a:hlink><a:srgbClr val="0563C1"/></a:hlink>'
    '<a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
    "</a:clrScheme>"
    '<a:fontScheme name="Office">'
    '<a:majorFont><a:latin typeface="Calibri Light"/><a:ea typeface=""/>'
    '<a:cs typeface=""/></a:majorFont>'
    '<a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/>'
    '<a:cs typeface=""/></a:minorFont>'
    "</a:fontScheme>"
    '<a:fmtScheme name="Office">'
    "<a:fillStyleLst>"
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    "</a:fillStyleLst>"
    "<a:lnStyleLst>"
    '<a:ln w="6350" cap="flat" cmpd="sng" algn="ctr">'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:prstDash val="solid"/></a:ln>'
    '<a:ln w="12700" cap="flat" cmpd="sng" algn="ctr">'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:prstDash val="solid"/></a:ln>'
    '<a:ln w="19050" cap="flat" cmpd="sng" algn="ctr">'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:prstDash val="solid"/></a:ln>'
    "</a:lnStyleLst>"
    "<a:effectStyleLst>"
    "<a:effectStyle><a:effectLst/></a:effectStyle>"
    "<a:effectStyle><a:effectLst/></a:effectStyle>"
    "<a:effectStyle><a:effectLst/></a:effectStyle>"
    "</a:effectStyleLst>"
    "<a:bgFillStyleLst>"
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    "</a:bgFillStyleLst>"
    "</a:fmtScheme>"
    "</a:themeElements></a:theme>"
)


def build_slides_from_markdown(
    markdown_text: str, *, title: str | None = None
) -> list[dict[str, Any]]:
    """Split Markdown into slides: each heading starts a new slide whose title is the
    heading text; list items, paragraphs and table rows become bullet strings."""

    slides: list[dict[str, Any]] = []

    def _current() -> dict[str, Any]:
        if not slides:
            slides.append({"title": "", "bullets": []})
        return slides[-1]

    for block in _parse_markdown_blocks(markdown_text):
        kind = block.get("type")
        if kind == "heading":
            slides.append({"title": str(block.get("text") or ""), "bullets": []})
        elif kind == "list":
            _current()["bullets"].extend(
                str(item) for item in (block.get("items") or []) if str(item).strip()
            )
        elif kind == "table":
            _current()["bullets"].extend(
                " | ".join(str(cell) for cell in row) for row in (block.get("rows") or [])
            )
        elif kind == "paragraph" and str(block.get("text") or "").strip():
            _current()["bullets"].append(str(block["text"]))
    if not slides:
        slides = [{"title": title or "Presentation", "bullets": []}]
    elif title and not str(slides[0].get("title") or "").strip():
        slides[0]["title"] = title
    return _normalize_slides(slides, title=title)


def _normalize_slides(
    slides: Any, *, title: str | None = None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for slide in slides or []:
        if not isinstance(slide, dict):
            slide = {"title": str(slide), "bullets": []}
        bullets_src = slide.get("bullets")
        if bullets_src is None:
            bullets_src = str(slide.get("body") or "").splitlines()
        bullets = [str(b) for b in bullets_src if str(b).strip()]
        out.append({"title": str(slide.get("title") or "").strip(), "bullets": bullets})
    return out or [{"title": title or "Presentation", "bullets": []}]


def write_presentation_pptx(
    output_path: Path,
    slides: Any,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Write a structurally valid PPTX presentation: one title+bullets slide per entry.

    ``slides`` is a list of ``{"title": str, "bullets": [str, ...]}`` (a bare string
    becomes a title-only slide), or pass Markdown through
    :func:`build_slides_from_markdown` first. Every part is wired through the OOXML
    relationship graph so real parsers (PowerPoint / python-pptx) open and walk it.
    """

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_slides(slides, title=title)
    _write_presentation_pptx(destination, normalized, title=title or "Presentation")
    verification = verify_document_artifact(destination, expected_format="pptx")
    return {
        "path": str(destination),
        "name": destination.name,
        "size": destination.stat().st_size,
        "sha256": file_sha256(destination),
        "format": "pptx",
        "slide_count": len(normalized),
        "bullet_count": sum(len(slide["bullets"]) for slide in normalized),
        "verification": verification,
    }


def _pptx_slide_xml(slide: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value or ""), quote=False)

    title_p = (
        '<a:p><a:r><a:rPr lang="en-US" dirty="0"/>'
        f'<a:t>{esc(slide.get("title"))}</a:t></a:r></a:p>'
    )
    body_ps = "".join(
        f'<a:p><a:r><a:rPr lang="en-US" dirty="0"/><a:t>{esc(bullet)}</a:t></a:r></a:p>'
        for bullet in slide.get("bullets") or []
    ) or '<a:p><a:endParaRPr lang="en-US"/></a:p>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:sld xmlns:a="{_A_DRAW}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}">'
        "<p:cSld><p:spTree>"
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title 1"/>'
        '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        '<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="685800" y="457200"/>'
        '<a:ext cx="7772400" cy="1143000"/></a:xfrm></p:spPr>'
        f"<p:txBody><a:bodyPr/><a:lstStyle/>{title_p}</p:txBody></p:sp>"
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="Content 2"/>'
        '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        '<p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
        '<p:spPr><a:xfrm><a:off x="685800" y="1600200"/>'
        '<a:ext cx="7772400" cy="4351338"/></a:xfrm></p:spPr>'
        f"<p:txBody><a:bodyPr/><a:lstStyle/>{body_ps}</p:txBody></p:sp>"
        "</p:spTree></p:cSld>"
        "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"
    )


def _write_presentation_pptx(
    path: Path,
    slides: list[dict[str, Any]],
    *,
    title: str,
) -> None:
    n = len(slides)
    slide_overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="{_PML}.slide+xml"/>'
        for i in range(1, n + 1)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/ppt/presentation.xml" ContentType="{_PML}.presentation.main+xml"/>'
        f'<Override PartName="/ppt/slideMasters/slideMaster1.xml" '
        f'ContentType="{_PML}.slideMaster+xml"/>'
        f'<Override PartName="/ppt/slideLayouts/slideLayout1.xml" '
        f'ContentType="{_PML}.slideLayout+xml"/>'
        '<Override PartName="/ppt/theme/theme1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        f"{slide_overrides}</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="ppt/presentation.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/'
        'relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        "</Relationships>"
    )
    sld_ids = "".join(
        f'<p:sldId id="{256 + i - 1}" r:id="rId{i}"/>' for i in range(1, n + 1)
    )
    master_rid = f"rId{n + 1}"
    presentation = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:presentation xmlns:a="{_A_DRAW}" xmlns:r="{_R_NS}" xmlns:p="{_P_NS}">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" '
        f'r:id="{master_rid}"/></p:sldMasterIdLst>'
        f"<p:sldIdLst>{sld_ids}</p:sldIdLst>"
        '<p:sldSz cx="9144000" cy="6858000" type="screen4x3"/>'
        '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>'
    )
    pres_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        + "".join(
            f'<Relationship Id="rId{i}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/'
            f'2006/relationships/slide" Target="slides/slide{i}.xml"/>'
            for i in range(1, n + 1)
        )
        + f'<Relationship Id="{master_rid}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
        "</Relationships>"
    )
    master_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/theme" Target="../theme/theme1.xml"/>'
        "</Relationships>"
    )
    layout_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>'
        "</Relationships>"
    )
    slide_rel = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
        "</Relationships>"
    )
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<cp:coreProperties xmlns:cp="{_CP_NS}" xmlns:dc="{_DC_NS}">'
        f"<dc:title>{html.escape(title)}</dc:title>"
        "<dc:creator>jarvis</dc:creator>"
        "<cp:lastModifiedBy>jarvis</cp:lastModifiedBy></cp:coreProperties>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("ppt/presentation.xml", presentation)
        archive.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        archive.writestr("ppt/slideMasters/slideMaster1.xml", _PPTX_MASTER_XML)
        archive.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", master_rels)
        archive.writestr("ppt/slideLayouts/slideLayout1.xml", _PPTX_LAYOUT_XML)
        archive.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", layout_rels)
        archive.writestr("ppt/theme/theme1.xml", _PPTX_THEME_XML)
        for index, slide in enumerate(slides, start=1):
            archive.writestr(f"ppt/slides/slide{index}.xml", _pptx_slide_xml(slide))
            archive.writestr(f"ppt/slides/_rels/slide{index}.xml.rels", slide_rel)


# --------------------------------------------------------------------------- #
# PDF — minimal single-file writer with a byte-accurate classic xref table.
# --------------------------------------------------------------------------- #

_PDF_PAGE_W, _PDF_PAGE_H, _PDF_MARGIN = 612, 792, 72
_PDF_FONT_SIZE, _PDF_LEADING, _PDF_MAX_LINES = 11, 15, 46


def write_pdf(
    output_path: Path,
    body: Any,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Write a minimal but valid PDF (US-Letter) from Markdown flattened to plain lines.

    Pure-ASCII / Latin-1 text uses a built-in Helvetica font. As soon as the text
    contains characters outside Latin-1 (Cyrillic, ``№``, em dash, …) the writer
    locates a Unicode-capable TrueType font already installed on the host (see
    ``_find_unicode_font``; override with ``JARVIS_PDF_FONT``) and embeds it as a
    Type0/CIDFontType2 program with Identity-H encoding and a ``/ToUnicode`` CMap, so
    the glyphs render correctly and stay searchable/extractable. If no such font is
    found it falls back to the Latin-1 path (non-Latin characters degrade to ``?``)."""

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pages = _pdf_paginate(_pdf_flatten_lines(body, title=title))
    text_all = "".join(line for page in pages for line in page)
    warnings: list[str] = []
    font: _EmbeddedTrueTypeFont | None = None
    if not _pdf_text_is_latin1(text_all):
        font = _load_pdf_unicode_font()
        if font is None:
            warnings.append(
                "No Unicode TrueType font was found on this host (set JARVIS_PDF_FONT "
                "to a .ttf path); non-Latin-1 characters degraded to '?'."
            )
    if font is not None:
        payload = _render_pdf_bytes_unicode(pages, title=title or "Document", font=font)
    else:
        payload = _render_pdf_bytes(pages, title=title or "Document")
    destination.write_bytes(payload)
    verification = verify_document_artifact(destination, expected_format="pdf")
    result: dict[str, Any] = {
        "path": str(destination),
        "name": destination.name,
        "size": destination.stat().st_size,
        "sha256": file_sha256(destination),
        "format": "pdf",
        "page_count": len(pages),
        "verification": verification,
    }
    if font is not None:
        result["font"] = str(font.source_path)
    if warnings:
        result["warnings"] = warnings
    return result


def _pdf_flatten_lines(body: Any, *, title: str | None) -> list[str]:
    lines: list[str] = []
    if title:
        lines += [str(title), ""]
    for block in _parse_markdown_blocks(str(body or "")):
        kind = block.get("type")
        if kind == "heading":
            lines += ["", str(block.get("text") or ""), ""]
        elif kind == "list":
            ordered = bool(block.get("ordered"))
            for position, item in enumerate(block.get("items") or [], start=1):
                prefix = f"{position}. " if ordered else "- "
                wrapped = textwrap.wrap(prefix + str(item), width=95) or [prefix.rstrip()]
                lines += wrapped
        elif kind == "table":
            lines += [
                " | ".join(str(cell) for cell in row) for row in block.get("rows") or []
            ]
        elif kind == "empty":
            lines.append("")
        elif block.get("text"):
            lines += textwrap.wrap(str(block["text"]), width=95) or [""]
    return lines or [str(title or "")]


def _pdf_paginate(lines: list[str]) -> list[list[str]]:
    return [
        lines[i : i + _PDF_MAX_LINES]
        for i in range(0, max(1, len(lines)), _PDF_MAX_LINES)
    ]


def _pdf_escape(text: str) -> str:
    winansi = str(text).encode("latin-1", "replace").decode("latin-1")
    return winansi.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _pdf_content_stream(lines: list[str]) -> str:
    parts = [
        "BT",
        f"/F1 {_PDF_FONT_SIZE} Tf",
        f"{_PDF_LEADING} TL",
        f"{_PDF_MARGIN} {_PDF_PAGE_H - _PDF_MARGIN} Td",
    ]
    for index, line in enumerate(lines):
        if index:
            parts.append("T*")
        escaped = _pdf_escape(line)
        if escaped:
            parts.append(f"({escaped}) Tj")
    parts.append("ET")
    return "\n".join(parts) + "\n"


def _render_pdf_bytes(pages: list[list[str]], *, title: str) -> bytes:
    n = len(pages)
    font_id = 3
    page_ids: list[int] = []
    content_ids: list[int] = []
    next_id = 4
    for _ in pages:
        page_ids.append(next_id)
        content_ids.append(next_id + 1)
        next_id += 2
    total = next_id - 1
    objs: dict[int, bytes] = {}
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode("latin-1")
    objs[font_id] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )
    for index, page_lines in enumerate(pages):
        stream = _pdf_content_stream(page_lines).encode("latin-1", "replace")
        objs[content_ids[index]] = (
            b"<< /Length " + str(len(stream)).encode("latin-1") + b" >>\nstream\n"
            + stream + b"\nendstream"
        )
        objs[page_ids[index]] = (
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {_PDF_PAGE_W} {_PDF_PAGE_H}] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_ids[index]} 0 R >>"
        ).encode("latin-1")
    return _assemble_pdf(objs, total)


def _assemble_pdf(objs: dict[int, bytes], total: int) -> bytes:
    """Serialize numbered PDF objects with a byte-accurate classic xref table."""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for oid in range(1, total + 1):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode("latin-1") + objs[oid] + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {total + 1}\n".encode("latin-1") + b"0000000000 65535 f \n"
    for oid in range(1, total + 1):
        out += f"{offsets[oid]:010d} 00000 n \n".encode("latin-1")
    out += b"trailer\n" + f"<< /Size {total + 1} /Root 1 0 R >>\n".encode("latin-1")
    out += f"startxref\n{xref_pos}\n".encode("latin-1") + b"%%EOF\n"
    return bytes(out)


# --------------------------------------------------------------------------- #
# PDF — Unicode/Cyrillic via an embedded Type0 (CIDFontType2) TrueType program.
# --------------------------------------------------------------------------- #

_PDF_FONT_CANDIDATES: tuple[str, ...] = (
    # Windows — the operator's own installed fonts (never bundled into the repo).
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
    r"C:\Windows\Fonts\verdana.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
    # Linux (CI / tests) — common DejaVu / Liberation / Noto locations.
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/LiberationSans-Regular.ttf",
    # macOS (best effort).
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


def _pdf_text_is_latin1(text: str) -> bool:
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def _find_unicode_font() -> Path | None:
    """Locate a Cyrillic-capable TrueType (.ttf) font already installed on the host.

    Reads the operator's own font at runtime — nothing is bundled into the repo. The
    search list is overridable with the ``JARVIS_PDF_FONT`` env var (an explicit .ttf
    path, tried first). ``.ttc`` collections and non-existent paths are skipped.
    Returns the first usable ``.ttf`` or ``None`` when none is found."""
    candidates: list[str] = []
    override = os.environ.get("JARVIS_PDF_FONT")
    if override:
        candidates.append(override)
    candidates.extend(_PDF_FONT_CANDIDATES)
    for name in candidates:
        if not name:
            continue
        try:
            path = Path(name)
        except (TypeError, ValueError):
            continue
        if path.suffix.lower() != ".ttf":
            continue
        if path.is_file():
            return path
    return None


def _load_pdf_unicode_font() -> _EmbeddedTrueTypeFont | None:
    """Return a parsed embeddable font, or ``None`` if none is found/parseable."""
    path = _find_unicode_font()
    if path is None:
        return None
    try:
        return _EmbeddedTrueTypeFont(path)
    except Exception:  # noqa: BLE001 — any parse failure degrades to Latin-1, never crashes
        return None


class _FontUnusableError(Exception):
    """Raised when a candidate font cannot be embedded (wrong outlines, no cmap, …)."""


class _EmbeddedTrueTypeFont:
    """Minimal TrueType reader — enough to embed the raw program as a PDF
    CIDFontType2 and map Unicode codepoints to glyph ids. Pure stdlib ``struct``
    parsing (``cmap`` formats 4 and 12, ``head``, ``hhea``, ``hmtx``, ``name``);
    no ``fontTools`` or any other third-party dependency."""

    def __init__(self, path: Path) -> None:
        self.source_path = Path(path)
        self.raw = self.source_path.read_bytes()
        self._tables: dict[str, tuple[int, int]] = {}
        self._read_table_directory()
        self.units_per_em = self._u16(self._tables["head"][0] + 18) or 1000
        self._num_h_metrics = self._u16(self._tables["hhea"][0] + 34)
        self._cmap_fmt, self._cmap = self._read_cmap()
        self.font_bbox, self.ascent, self.descent = self._read_metrics()
        self.ps_name = self._read_ps_name()

    # -- primitive readers -------------------------------------------------- #
    def _u16(self, off: int) -> int:
        return struct.unpack(">H", self.raw[off : off + 2])[0]

    def _s16(self, off: int) -> int:
        return struct.unpack(">h", self.raw[off : off + 2])[0]

    def _u32(self, off: int) -> int:
        return struct.unpack(">I", self.raw[off : off + 4])[0]

    # -- table directory ---------------------------------------------------- #
    def _read_table_directory(self) -> None:
        if len(self.raw) < 12:
            raise _FontUnusableError("truncated font file")
        sfnt = self._u32(0)
        if sfnt not in (0x00010000, 0x74727565):  # 'true' — TrueType outlines only
            raise _FontUnusableError("not a TrueType (glyf) font")
        num_tables = self._u16(4)
        off = 12
        for _ in range(num_tables):
            tag = self.raw[off : off + 4].decode("latin-1", "replace")
            self._tables[tag] = (self._u32(off + 8), self._u32(off + 12))
            off += 16
        for required in ("cmap", "head", "hhea", "hmtx", "glyf"):
            if required not in self._tables:
                raise _FontUnusableError(f"missing required table {required!r}")

    def _read_metrics(self) -> tuple[list[int], int, int]:
        head = self._tables["head"][0]
        scale = 1000.0 / self.units_per_em
        bbox = [
            round(self._s16(head + 36) * scale),
            round(self._s16(head + 38) * scale),
            round(self._s16(head + 40) * scale),
            round(self._s16(head + 42) * scale),
        ]
        hhea = self._tables["hhea"][0]
        return bbox, round(self._s16(hhea + 4) * scale), round(self._s16(hhea + 6) * scale)

    def _read_ps_name(self) -> str:
        table = self._tables.get("name")
        if table is None:
            return "EmbeddedFont"
        base = table[0]
        count = self._u16(base + 2)
        strings = base + self._u16(base + 4)
        chosen = ""
        for i in range(count):
            rec = base + 6 + i * 12
            platform = self._u16(rec)
            name_id = self._u16(rec + 6)
            length = self._u16(rec + 8)
            offset = self._u16(rec + 10)
            if name_id != 6:  # 6 = PostScript name
                continue
            raw = self.raw[strings + offset : strings + offset + length]
            if platform in (0, 3):
                candidate = raw.decode("utf-16-be", "ignore")
            else:
                candidate = raw.decode("latin-1", "ignore")
            candidate = re.sub(r"[^A-Za-z0-9._-]", "", candidate)
            if candidate:
                chosen = candidate
                if platform == 3:
                    break
        return chosen or "EmbeddedFont"

    # -- cmap --------------------------------------------------------------- #
    def _read_cmap(self) -> tuple[int, Any]:
        base = self._tables["cmap"][0]
        num = self._u16(base + 2)
        best: tuple[int, int, int] | None = None  # (priority, subtable_off, format)
        for i in range(num):
            rec = base + 4 + i * 8
            platform = self._u16(rec)
            encoding = self._u16(rec + 2)
            sub = base + self._u32(rec + 4)
            fmt = self._u16(sub)
            priority = -1
            if platform == 3 and encoding == 10 and fmt == 12:
                priority = 4
            elif platform == 0 and fmt == 12:
                priority = 3
            elif platform == 3 and encoding == 1 and fmt == 4:
                priority = 2
            elif platform == 0 and fmt == 4:
                priority = 1
            elif fmt in (4, 12):
                priority = 0
            if priority >= 0 and (best is None or priority > best[0]):
                best = (priority, sub, fmt)
        if best is None:
            raise _FontUnusableError("no usable Unicode cmap subtable")
        _, sub, fmt = best
        return (fmt, self._parse_format4(sub) if fmt == 4 else self._parse_format12(sub))

    def _parse_format4(self, sub: int) -> tuple[Any, ...]:
        seg_x2 = self._u16(sub + 6)
        seg_count = seg_x2 // 2
        pos = sub + 14
        end = struct.unpack(f">{seg_count}H", self.raw[pos : pos + seg_x2])
        pos += seg_x2 + 2  # skip reservedPad
        start = struct.unpack(f">{seg_count}H", self.raw[pos : pos + seg_x2])
        pos += seg_x2
        delta = struct.unpack(f">{seg_count}h", self.raw[pos : pos + seg_x2])
        pos += seg_x2
        range_off_pos = pos
        range_off = struct.unpack(f">{seg_count}H", self.raw[pos : pos + seg_x2])
        return (seg_count, end, start, delta, range_off, range_off_pos)

    def _parse_format12(self, sub: int) -> list[tuple[int, int, int]]:
        n_groups = self._u32(sub + 12)
        pos = sub + 16
        groups: list[tuple[int, int, int]] = []
        for _ in range(n_groups):
            groups.append((self._u32(pos), self._u32(pos + 4), self._u32(pos + 8)))
            pos += 12
        return groups

    def gid(self, codepoint: int) -> int:
        """Return the glyph id for a Unicode codepoint (0 = .notdef / no glyph)."""
        if self._cmap_fmt == 4:
            seg_count, end, start, delta, range_off, range_off_pos = self._cmap
            for i in range(seg_count):
                if codepoint > end[i]:
                    continue
                if codepoint < start[i]:
                    return 0
                if range_off[i] == 0:
                    return (codepoint + delta[i]) & 0xFFFF
                glyph_pos = range_off_pos + i * 2 + range_off[i] + (codepoint - start[i]) * 2
                if glyph_pos + 2 > len(self.raw):
                    return 0
                glyph = self._u16(glyph_pos)
                return (glyph + delta[i]) & 0xFFFF if glyph else 0
            return 0
        for first, last, first_gid in self._cmap:
            if first <= codepoint <= last:
                return first_gid + (codepoint - first)
        return 0

    def advance(self, glyph_id: int) -> int:
        """Advance width of a glyph in font units (last hmetric repeats past the table)."""
        off, length = self._tables["hmtx"]
        if self._num_h_metrics == 0:
            return self.units_per_em
        index = glyph_id if glyph_id < self._num_h_metrics else self._num_h_metrics - 1
        pos = off + index * 4
        if pos + 2 > off + length:
            return self.units_per_em
        return self._u16(pos)


def _utf16be_units(codepoint: int) -> list[int]:
    if codepoint <= 0xFFFF:
        return [codepoint]
    codepoint -= 0x10000
    return [0xD800 + (codepoint >> 10), 0xDC00 + (codepoint & 0x3FF)]


def _pdf_unicode_content_stream(lines: list[str], cp_to_gid: dict[int, int]) -> str:
    parts = [
        "BT",
        f"/F1 {_PDF_FONT_SIZE} Tf",
        f"{_PDF_LEADING} TL",
        f"{_PDF_MARGIN} {_PDF_PAGE_H - _PDF_MARGIN} Td",
    ]
    for index, line in enumerate(lines):
        if index:
            parts.append("T*")
        if line:
            glyphs = "".join(f"{cp_to_gid.get(ord(ch), 0):04X}" for ch in line)
            parts.append(f"<{glyphs}> Tj")
    parts.append("ET")
    return "\n".join(parts) + "\n"


def _pdf_cid_widths(font: _EmbeddedTrueTypeFont, used_gids: dict[int, int]) -> str:
    if not used_gids:
        return "[ ]"
    scale = 1000.0 / font.units_per_em
    widths = {gid: round(font.advance(gid) * scale) for gid in used_gids}
    gids = sorted(widths)
    runs: list[str] = []
    i = 0
    while i < len(gids):
        j = i + 1
        while j < len(gids) and gids[j] == gids[j - 1] + 1:
            j += 1
        run = " ".join(str(widths[g]) for g in gids[i:j])
        runs.append(f"{gids[i]} [ {run} ]")
        i = j
    return "[ " + " ".join(runs) + " ]"


def _pdf_tounicode_cmap(used_gids: dict[int, int]) -> str:
    items = sorted(used_gids.items())
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    for chunk_start in range(0, len(items), 100):
        chunk = items[chunk_start : chunk_start + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        for gid, codepoint in chunk:
            target = "".join(f"{unit:04X}" for unit in _utf16be_units(codepoint))
            lines.append(f"<{gid:04X}> <{target}>")
        lines.append("endbfchar")
    lines += ["endcmap", "CMapResource endresource end", "end", "end"]
    return "\n".join(lines) + "\n"


def _render_pdf_bytes_unicode(
    pages: list[list[str]], *, title: str, font: _EmbeddedTrueTypeFont
) -> bytes:
    cp_to_gid: dict[int, int] = {}
    for page in pages:
        for line in page:
            for ch in line:
                codepoint = ord(ch)
                if codepoint not in cp_to_gid:
                    cp_to_gid[codepoint] = font.gid(codepoint)
    used_gids: dict[int, int] = {}
    for codepoint, glyph_id in cp_to_gid.items():
        if glyph_id != 0:
            used_gids[glyph_id] = codepoint

    n = len(pages)
    catalog_id, pages_id, type0_id, cid_id, desc_id, file_id, tounicode_id = range(1, 8)
    next_id = 8
    page_ids: list[int] = []
    content_ids: list[int] = []
    for _ in pages:
        page_ids.append(next_id)
        content_ids.append(next_id + 1)
        next_id += 2
    total = next_id - 1

    base_font = font.ps_name
    objs: dict[int, bytes] = {}
    objs[catalog_id] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objs[pages_id] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode("latin-1")
    objs[type0_id] = (
        f"<< /Type /Font /Subtype /Type0 /BaseFont /{base_font} "
        f"/Encoding /Identity-H /DescendantFonts [{cid_id} 0 R] "
        f"/ToUnicode {tounicode_id} 0 R >>"
    ).encode("latin-1")
    objs[cid_id] = (
        f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{base_font} "
        f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        f"/FontDescriptor {desc_id} 0 R /CIDToGIDMap /Identity "
        f"/DW 1000 /W {_pdf_cid_widths(font, used_gids)} >>"
    ).encode("latin-1")
    bbox = " ".join(str(v) for v in font.font_bbox)
    objs[desc_id] = (
        f"<< /Type /FontDescriptor /FontName /{base_font} /Flags 4 "
        f"/FontBBox [{bbox}] /ItalicAngle 0 /Ascent {font.ascent} "
        f"/Descent {font.descent} /CapHeight {font.ascent} /StemV 80 "
        f"/FontFile2 {file_id} 0 R >>"
    ).encode("latin-1")
    compressed = zlib.compress(font.raw)
    objs[file_id] = (
        b"<< /Length " + str(len(compressed)).encode("latin-1")
        + b" /Length1 " + str(len(font.raw)).encode("latin-1")
        + b" /Filter /FlateDecode >>\nstream\n" + compressed + b"\nendstream"
    )
    tounicode = _pdf_tounicode_cmap(used_gids).encode("latin-1")
    objs[tounicode_id] = (
        b"<< /Length " + str(len(tounicode)).encode("latin-1") + b" >>\nstream\n"
        + tounicode + b"\nendstream"
    )
    for index, page_lines in enumerate(pages):
        stream = _pdf_unicode_content_stream(page_lines, cp_to_gid).encode("latin-1")
        objs[content_ids[index]] = (
            b"<< /Length " + str(len(stream)).encode("latin-1") + b" >>\nstream\n"
            + stream + b"\nendstream"
        )
        objs[page_ids[index]] = (
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {_PDF_PAGE_W} {_PDF_PAGE_H}] "
            f"/Resources << /Font << /F1 {type0_id} 0 R >> >> "
            f"/Contents {content_ids[index]} 0 R >>"
        ).encode("latin-1")
    return _assemble_pdf(objs, total)


# --------------------------------------------------------------------------- #
# CHART / SVG — a well-formed standalone SVG bar / line / pie chart.
# --------------------------------------------------------------------------- #

_CHART_COLORS = ["#4E79A7", "#F28E2B", "#59A14F", "#E15759", "#B07AA1", "#76B7B2"]


def _chart_number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    coerced = _coerce_scalar(str(value))
    if isinstance(coerced, int | float) and not isinstance(coerced, bool):
        return float(coerced)
    match = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(match.group(0)) if match else 0.0


def _normalize_chart(chart: dict[str, Any], *, title: str | None = None) -> dict[str, Any]:
    ctype = str(chart.get("type") or "bar").lower()
    if ctype not in ("bar", "line", "pie"):
        ctype = "bar"
    series = chart.get("series")
    if not series and chart.get("data") is not None:
        series = [{"name": "Series 1", "data": chart["data"]}]
    norm: list[dict[str, Any]] = []
    for entry in series or []:
        if not isinstance(entry, dict):
            entry = {"name": f"Series {len(norm) + 1}", "data": entry}
        norm.append(
            {
                "name": str(entry.get("name") or f"Series {len(norm) + 1}"),
                "data": [_chart_number(x) for x in (entry.get("data") or [])],
            }
        )
    return {
        "type": ctype,
        "title": str(title or chart.get("title") or "Chart"),
        "categories": [str(c) for c in (chart.get("categories") or [])],
        "series": norm,
    }


def build_chart_spec(
    body: Any,
    chart: Any = None,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Build a normalized chart spec from a structured ``chart`` dict
    (``{type, title, categories, series:[{name,data}]}``), a raw ``<svg>`` string, or a
    Markdown table in ``body`` (first column = categories, remaining columns = series)."""

    if isinstance(chart, dict):
        if chart.get("raw"):
            return {"raw": str(chart["raw"])}
        if any(chart.get(key) for key in ("series", "data", "categories", "type")):
            return _normalize_chart(chart, title=title or chart.get("title"))
    text = str(body or "").lstrip()
    if text.startswith("<svg") or text.startswith("<?xml"):
        return {"raw": text}
    categories: list[str] = []
    series: list[dict[str, Any]] = []
    for block in _parse_markdown_blocks(text):
        if block.get("type") == "table" and block.get("rows"):
            rows = block["rows"]
            head = rows[0]
            body_rows = rows[1:]
            categories = [str(row[0]) for row in body_rows if row]
            for col in range(1, len(head)):
                series.append(
                    {
                        "name": str(head[col]),
                        "data": [
                            _chart_number(row[col]) if col < len(row) else 0.0
                            for row in body_rows
                        ],
                    }
                )
            break
    return _normalize_chart(
        {"type": "bar", "categories": categories, "series": series}, title=title
    )


def write_chart_svg(
    output_path: Path,
    chart: Any = None,
    *,
    title: str | None = None,
    body: Any = None,
) -> dict[str, Any]:
    """Write a standalone, well-formed SVG chart (bar/line/pie) from a structured chart
    spec or a Markdown table. A raw ``<svg>`` string is passed through unchanged."""

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(chart, dict):
        spec = build_chart_spec(body if body is not None else "", chart, title=title)
    else:
        source = chart if isinstance(chart, str) else (body if body is not None else "")
        spec = build_chart_spec(source, None, title=title)
    svg = spec["raw"] if spec.get("raw") else _render_chart_svg(spec)
    if not svg.endswith("\n"):
        svg += "\n"
    destination.write_text(svg, encoding="utf-8", newline="\n")
    verification = verify_document_artifact(destination, expected_format="svg")
    return {
        "path": str(destination),
        "name": destination.name,
        "size": destination.stat().st_size,
        "sha256": file_sha256(destination),
        "format": "svg",
        "chart_type": spec.get("type", "custom"),
        "series_count": len(spec.get("series", [])),
        "verification": verification,
    }


def _svg_num(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _svg_text(x: float, y: float, text: Any, *, size: int, anchor: str = "start",
              color: str = "#333333", weight: str = "normal") -> str:
    return (
        f'<text x="{_svg_num(x)}" y="{_svg_num(y)}" font-family="Arial, sans-serif" '
        f'font-size="{size}" fill="{color}" text-anchor="{anchor}" '
        f'font-weight="{weight}">{html.escape(str(text), quote=False)}</text>'
    )


def _render_chart_svg(spec: dict[str, Any]) -> str:
    width, height = 800, 480
    ctype = spec.get("type", "bar")
    title = spec.get("title") or "Chart"
    series = spec.get("series") or []
    categories = spec.get("categories") or []
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>',
        _svg_text(width / 2, 30, title, size=20, anchor="middle",
                  color="#1F3864", weight="bold"),
    ]
    if ctype == "pie":
        parts.append(_render_pie_body(spec, width, height))
    else:
        parts.append(_render_axes_body(spec, ctype, series, categories, width, height))
    parts.append(_render_chart_legend(ctype, spec, series, categories, width, height))
    parts.append("</svg>")
    return "".join(parts)


def _render_axes_body(
    spec: dict[str, Any],
    ctype: str,
    series: list[dict[str, Any]],
    categories: list[str],
    width: int,
    height: int,
) -> str:
    left, right, top, bottom = 60, width - 40, 50, height - 90
    plot_w = right - left
    plot_h = bottom - top
    values = [v for s in series for v in s.get("data") or []]
    max_val = max([*values, 0.0]) or 1.0
    min_val = min([*values, 0.0])
    span = (max_val - min_val) or 1.0
    zero_y = bottom - ((0.0 - min_val) / span) * plot_h
    out: list[str] = ['<g>']
    # gridlines + y labels
    for step in range(5):
        val = min_val + span * step / 4
        y = bottom - (val - min_val) / span * plot_h
        out.append(
            f'<line x1="{left}" y1="{_svg_num(y)}" x2="{right}" y2="{_svg_num(y)}" '
            'stroke="#E0E0E0" stroke-width="1"/>'
        )
        out.append(_svg_text(left - 8, y + 4, _svg_num(val), size=11, anchor="end",
                             color="#666666"))
    # axes
    out.append(
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" '
        'stroke="#888888" stroke-width="1.5"/>'
    )
    out.append(
        f'<line x1="{left}" y1="{_svg_num(zero_y)}" x2="{right}" y2="{_svg_num(zero_y)}" '
        'stroke="#888888" stroke-width="1.5"/>'
    )
    n_cats = max(len(categories), max((len(s.get("data") or []) for s in series), default=0), 1)
    slot = plot_w / n_cats
    if ctype == "line":
        for si, s in enumerate(series):
            color = _CHART_COLORS[si % len(_CHART_COLORS)]
            points = []
            for ci, val in enumerate(s.get("data") or []):
                x = left + slot * (ci + 0.5)
                y = bottom - (val - min_val) / span * plot_h
                points.append(f"{_svg_num(x)},{_svg_num(y)}")
            if points:
                out.append(
                    f'<polyline fill="none" stroke="{color}" stroke-width="2.5" '
                    f'points="{" ".join(points)}"/>'
                )
                for pt in points:
                    px, py = pt.split(",")
                    out.append(
                        f'<circle cx="{px}" cy="{py}" r="3" fill="{color}"/>'
                    )
    else:  # bar
        group_w = slot * 0.8
        n_series = max(len(series), 1)
        bar_w = group_w / n_series
        for si, s in enumerate(series):
            color = _CHART_COLORS[si % len(_CHART_COLORS)]
            for ci, val in enumerate(s.get("data") or []):
                x = left + slot * ci + (slot - group_w) / 2 + bar_w * si
                y = bottom - (max(val, 0.0) - min_val) / span * plot_h
                bar_h = abs((val - 0.0) / span * plot_h)
                bar_top = min(y, zero_y)
                out.append(
                    f'<rect x="{_svg_num(x)}" y="{_svg_num(bar_top)}" '
                    f'width="{_svg_num(bar_w)}" height="{_svg_num(bar_h)}" '
                    f'fill="{color}"/>'
                )
    # category labels
    for ci in range(n_cats):
        label = categories[ci] if ci < len(categories) else str(ci + 1)
        x = left + slot * (ci + 0.5)
        out.append(_svg_text(x, bottom + 18, label, size=11, anchor="middle",
                             color="#444444"))
    out.append("</g>")
    return "".join(out)


def _render_pie_body(spec: dict[str, Any], width: int, height: int) -> str:
    series = spec.get("series") or []
    categories = spec.get("categories") or []
    data = series[0].get("data") if series else []
    data = [abs(v) for v in (data or [])]
    total = sum(data) or 1.0
    cx, cy, r = width / 2, height / 2 + 10, 150
    out: list[str] = ['<g>']
    angle = -math.pi / 2
    for index, value in enumerate(data):
        frac = value / total
        end = angle + frac * 2 * math.pi
        color = _CHART_COLORS[index % len(_CHART_COLORS)]
        if frac >= 0.9999:
            out.append(f'<circle cx="{_svg_num(cx)}" cy="{_svg_num(cy)}" r="{r}" '
                       f'fill="{color}"/>')
        else:
            x1 = cx + r * math.cos(angle)
            y1 = cy + r * math.sin(angle)
            x2 = cx + r * math.cos(end)
            y2 = cy + r * math.sin(end)
            large = 1 if (end - angle) > math.pi else 0
            out.append(
                f'<path d="M {_svg_num(cx)} {_svg_num(cy)} L {_svg_num(x1)} {_svg_num(y1)} '
                f'A {r} {r} 0 {large} 1 {_svg_num(x2)} {_svg_num(y2)} Z" '
                f'fill="{color}"/>'
            )
        mid = (angle + end) / 2
        lx = cx + (r + 20) * math.cos(mid)
        ly = cy + (r + 20) * math.sin(mid)
        label = categories[index] if index < len(categories) else f"{value:g}"
        anchor = "start" if math.cos(mid) >= 0 else "end"
        out.append(_svg_text(lx, ly, label, size=11, anchor=anchor, color="#444444"))
        angle = end
    out.append("</g>")
    return "".join(out)


def _render_chart_legend(
    ctype: str,
    spec: dict[str, Any],
    series: list[dict[str, Any]],
    categories: list[str],
    width: int,
    height: int,
) -> str:
    if ctype == "pie":
        labels = list(categories)
    else:
        labels = [s.get("name", f"Series {i + 1}") for i, s in enumerate(series)]
    if not labels:
        return ""
    out: list[str] = ['<g>']
    y = height - 30
    x = 60
    for index, label in enumerate(labels):
        color = _CHART_COLORS[index % len(_CHART_COLORS)]
        out.append(
            f'<rect x="{_svg_num(x)}" y="{y - 10}" width="12" height="12" fill="{color}"/>'
        )
        out.append(_svg_text(x + 16, y, label, size=11, anchor="start", color="#444444"))
        x += 16 + 10 + max(40, len(str(label)) * 7)
    out.append("</g>")
    return "".join(out)


def read_workbook_grid(
    path: Path,
    *,
    max_rows: int = 4096,
    max_cols: int = 256,
) -> list[dict[str, Any]]:
    """Read every sheet of an XLSX/XLSM into a dense, TYPED row grid suitable for
    round-trip editing and rewriting via :func:`write_workbook_xlsx`.

    Unlike the bounded preview in :func:`_extract_xlsx`, this preserves cell TYPES
    (numbers stay numbers, booleans stay booleans) and formulas (returned as a
    leading-``=`` string so the writer re-emits them as formulas) across the full
    used range of each sheet. Trailing blank cells are trimmed per row.
    """

    src = Path(path).resolve(strict=False)
    if not src.exists() or not src.is_file():
        raise DocumentRuntimeError(f"Workbook does not exist: {src}")
    if src.suffix.lower() not in {".xlsx", ".xlsm"}:
        raise DocumentRuntimeError(f"Not an XLSX/XLSM workbook: {src.suffix or src.name}")
    if not zipfile.is_zipfile(src):
        raise DocumentRuntimeError("XLSX is not a valid ZIP package.")
    with zipfile.ZipFile(src) as archive:
        shared_strings = _xlsx_shared_strings(archive)
        sheet_map = _xlsx_sheet_map(archive)
        sheets: list[dict[str, Any]] = []
        for sheet_name, member_name in sheet_map[:64]:
            xml = _read_zip_text_member(archive, member_name)
            if not xml:
                sheets.append({"name": sheet_name, "rows": []})
                continue
            sheets.append(
                {
                    "name": sheet_name,
                    "rows": _xlsx_typed_rows(
                        xml, shared_strings, max_rows=max_rows, max_cols=max_cols
                    ),
                }
            )
    if not sheets:
        raise DocumentRuntimeError("Workbook has no readable sheets.")
    return sheets


def _xlsx_typed_rows(
    xml: str,
    shared_strings: list[str],
    *,
    max_rows: int,
    max_cols: int,
) -> list[list[Any]]:
    root = _parse_xml(xml, "XLSX sheet")
    grid: dict[int, dict[int, Any]] = {}
    max_row_seen = 0
    for order, row in enumerate(root.findall(f".//{{{_A_NS}}}row")):
        if order >= max_rows:
            break
        attr_r = row.attrib.get("r")
        row_number = int(attr_r) if attr_r and attr_r.isdigit() else order + 1
        cells: dict[int, Any] = {}
        for position, cell in enumerate(row.findall(f"./{{{_A_NS}}}c"), start=1):
            ref = str(cell.attrib.get("r") or "")
            col = _xlsx_col_index(ref) if ref else position
            if col > max_cols:
                continue
            value = _xlsx_typed_cell_value(cell, shared_strings)
            if value == "":
                continue
            cells[col] = value
        if cells:
            grid[row_number] = cells
            max_row_seen = max(max_row_seen, row_number)
    rows: list[list[Any]] = []
    for row_number in range(1, max_row_seen + 1):
        cells = grid.get(row_number, {})
        width = max(cells) if cells else 0
        rows.append([cells.get(col, "") for col in range(1, width + 1)])
    return rows


def _xlsx_typed_cell_value(cell: ET.Element, shared_strings: list[str]) -> Any:
    formula = cell.find(f"./{{{_A_NS}}}f")
    if formula is not None and (formula.text or "").strip():
        return "=" + formula.text.strip()
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(f".//{{{_A_NS}}}t"))
    value_node = cell.find(f"./{{{_A_NS}}}v")
    raw = value_node.text if value_node is not None and value_node.text is not None else ""
    if raw == "":
        return ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return raw
    if cell_type == "b":
        return raw.strip() in {"1", "true", "True"}
    if cell_type == "str":
        return raw
    return _coerce_scalar(raw)


def edit_workbook_xlsx(
    source_path: Path,
    operations: list[dict[str, Any]],
    output_path: Path,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Apply structured edit operations to an existing workbook and write the result.

    Reads the full typed grid of ``source_path``, applies ``operations`` in order,
    then rewrites a real spreadsheet to ``output_path`` (typed cells, formulas kept,
    bold frozen header). Supported ops (the ``op`` key):

    - ``append_row``       {sheet?, values: [...]}                a row at the end
    - ``set_cell``         {sheet?, cell:"B5" | row, col, value}
    - ``update_row_where`` {sheet?, match_col, match_value, set_col, value}
    - ``delete_row``       {sheet?, row | match_col, match_value}
    - ``set_rows``         {sheet?, rows: [[...]]}                 replace the grid
    - ``add_sheet``        {name, rows: [[...]]}
    - ``rename_sheet``     {sheet, name}

    Column selectors accept an A1 letter, a 1-based index, or a header-cell name
    (matched against the sheet's first row). Rows are 1-based.
    """

    src = Path(source_path).resolve(strict=False)
    sheets = read_workbook_grid(src)
    changes: list[str] = []
    for raw_op in operations or []:
        if not isinstance(raw_op, dict):
            continue
        op = str(raw_op.get("op") or raw_op.get("action") or "").strip().casefold()
        if not op:
            continue
        _apply_workbook_operation(sheets, op, raw_op, changes)
    if not changes:
        raise DocumentRuntimeError(
            "No workbook edit operation changed anything; the source was left untouched."
        )
    result = write_workbook_xlsx(Path(output_path), sheets, title=title)
    result["changes"] = changes
    result["source"] = str(src)
    return result


def _apply_workbook_operation(
    sheets: list[dict[str, Any]],
    op: str,
    spec: dict[str, Any],
    changes: list[str],
) -> None:
    if op in {"add_sheet", "new_sheet"}:
        name = str(spec.get("name") or spec.get("sheet") or f"Sheet{len(sheets) + 1}").strip()
        rows = _workbook_rows_from_arg(spec.get("rows"))
        sheets.append({"name": name or f"Sheet{len(sheets) + 1}", "rows": rows})
        changes.append(f"added sheet '{name}' with {len(rows)} row(s)")
        return
    sheet = _workbook_select_sheet(sheets, spec.get("sheet"))
    rows = sheet["rows"]
    if op in {"append_row", "add_row"}:
        raw = spec.get("values") if spec.get("values") is not None else spec.get("row")
        values = _workbook_row_values(raw)
        rows.append(values)
        changes.append(f"appended a row to '{sheet['name']}'")
    elif op in {"set_cell", "update_cell", "set"}:
        if spec.get("cell"):
            row_no, col_no = _workbook_parse_a1(spec.get("cell"))
        else:
            row_no = int(spec.get("row"))
            col_spec = spec.get("col") if spec.get("col") is not None else spec.get("column")
            col_no = _workbook_resolve_column(sheet, col_spec)
        _workbook_ensure_cell(rows, row_no, col_no)
        rows[row_no - 1][col_no - 1] = _workbook_edit_value(spec.get("value"))
        changes.append(f"set {sheet['name']}!R{row_no}C{col_no}")
    elif op in {"update_row_where", "update_where", "set_where"}:
        match_col = spec.get("match_col") or spec.get("where_col") or spec.get("key_col")
        col = _workbook_resolve_column(sheet, match_col)
        match_value = spec.get("match_value") if "match_value" in spec else spec.get("where_value")
        target = _workbook_resolve_row_by_match(rows, col, match_value)
        set_spec = spec.get("set_col") or spec.get("col") or spec.get("column")
        set_col = _workbook_resolve_column(sheet, set_spec)
        _workbook_ensure_cell(rows, target, set_col)
        rows[target - 1][set_col - 1] = _workbook_edit_value(spec.get("value"))
        changes.append(f"updated row {target} in '{sheet['name']}'")
    elif op in {"delete_row", "remove_row"}:
        target = _workbook_resolve_row(sheet, spec)
        if not 1 <= target <= len(rows):
            raise DocumentRuntimeError(f"row out of range: {target}")
        rows.pop(target - 1)
        changes.append(f"deleted row {target} from '{sheet['name']}'")
    elif op in {"set_rows", "replace_rows", "set_grid"}:
        sheet["rows"] = _workbook_rows_from_arg(spec.get("rows"))
        changes.append(f"replaced the grid of '{sheet['name']}' ({len(sheet['rows'])} row(s))")
    elif op in {"rename_sheet", "rename"}:
        new_name = str(spec.get("name") or spec.get("to") or "").strip()
        if not new_name:
            raise DocumentRuntimeError("rename_sheet requires a new name")
        old = sheet["name"]
        sheet["name"] = new_name
        changes.append(f"renamed sheet '{old}' -> '{new_name}'")
    else:
        raise DocumentRuntimeError(f"unsupported workbook edit op: {op}")


def _workbook_rows_from_arg(raw_rows: Any) -> list[list[Any]]:
    return [_workbook_row_values(row) for row in (raw_rows or [])]


def _workbook_row_values(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list | tuple):
        return [_workbook_edit_value(cell) for cell in raw]
    return [_workbook_edit_value(raw)]


def _workbook_edit_value(value: Any) -> Any:
    if isinstance(value, str):
        return _coerce_scalar(value)
    return _normalize_cell(value)


def _workbook_select_sheet(sheets: list[dict[str, Any]], spec: Any) -> dict[str, Any]:
    if not sheets:
        raise DocumentRuntimeError("workbook has no sheets")
    if spec is None or isinstance(spec, bool) or (isinstance(spec, str) and not spec.strip()):
        return sheets[0]
    if isinstance(spec, int):
        if 0 <= spec < len(sheets):
            return sheets[spec]
        raise DocumentRuntimeError(f"sheet index out of range: {spec}")
    text = str(spec).strip()
    for sheet in sheets:
        if str(sheet["name"]).casefold() == text.casefold():
            return sheet
    for sheet in sheets:
        if text.casefold() in str(sheet["name"]).casefold():
            return sheet
    if text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(sheets):
            return sheets[idx]
    raise DocumentRuntimeError(f"sheet not found: {spec}")


def _workbook_resolve_column(sheet: dict[str, Any], spec: Any) -> int:
    if spec is None or isinstance(spec, bool) or (isinstance(spec, str) and not spec.strip()):
        raise DocumentRuntimeError("a column selector is required")
    if isinstance(spec, int):
        if spec < 1:
            raise DocumentRuntimeError(f"invalid column index: {spec}")
        return spec
    text = str(spec).strip()
    header = sheet["rows"][0] if sheet.get("rows") else []
    for index, cell in enumerate(header, start=1):
        if str(cell).strip().casefold() == text.casefold():
            return index
    if re.fullmatch(r"[A-Za-z]{1,3}", text):
        return _xlsx_col_index(text.upper())
    if text.isdigit():
        return int(text)
    raise DocumentRuntimeError(f"column not found: {spec!r}")


def _workbook_resolve_row(sheet: dict[str, Any], spec: dict[str, Any]) -> int:
    if spec.get("row") is not None:
        row = int(spec["row"])
        if row < 1:
            raise DocumentRuntimeError(f"invalid row: {row}")
        return row
    match_col = spec.get("match_col") or spec.get("where_col") or spec.get("key_col")
    if match_col is not None:
        col = _workbook_resolve_column(sheet, match_col)
        value = spec.get("match_value") if "match_value" in spec else spec.get("where_value")
        return _workbook_resolve_row_by_match(sheet["rows"], col, value)
    raise DocumentRuntimeError("delete_row needs a row number or match_col+match_value")


def _workbook_resolve_row_by_match(rows: list[list[Any]], col: int, value: Any) -> int:
    for index, row in enumerate(rows, start=1):
        if col - 1 < len(row) and _workbook_cells_equal(row[col - 1], value):
            return index
    raise DocumentRuntimeError(f"no row where column {col} == {value!r}")


def _workbook_cells_equal(cell: Any, value: Any) -> bool:
    if cell == value:
        return True
    return str(cell).strip().casefold() == str(value).strip().casefold()


def _workbook_parse_a1(ref: Any) -> tuple[int, int]:
    match = re.fullmatch(r"\s*([A-Za-z]{1,3})(\d+)\s*", str(ref or ""))
    if not match:
        raise DocumentRuntimeError(f"invalid A1 cell reference: {ref!r}")
    return int(match.group(2)), _xlsx_col_index(match.group(1).upper())


def _workbook_ensure_cell(rows: list[list[Any]], row: int, col: int) -> None:
    while len(rows) < row:
        rows.append([])
    target = rows[row - 1]
    while len(target) < col:
        target.append("")


def _normalize_cell(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if _is_finite_number(value) else str(value)
    if value is None:
        return ""
    return str(value)


def _is_finite_number(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _safe_sheet_name(name: str, used: set[str]) -> str:
    cleaned = re.sub(r"[\[\]:\*\?/\\]", " ", str(name or "")).strip()
    cleaned = " ".join(cleaned.split())[:31] or "Sheet"
    candidate = cleaned
    suffix = 2
    while candidate.casefold() in used:
        tail = f" ({suffix})"
        candidate = f"{cleaned[: 31 - len(tail)]}{tail}"
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def _rows_from_cells(cells: Any) -> list[list[Any]]:
    """Build a dense row grid from a sparse ``[{"row": r, "col": c, "value": v}]``
    list — the natural cell format a model tends to emit for a spreadsheet."""

    grid: dict[tuple[int, int], Any] = {}
    max_row = max_col = 0
    for cell in cells or []:
        if not isinstance(cell, dict):
            continue
        try:
            row = int(cell.get("row"))
            col = int(cell.get("col") if cell.get("col") is not None else cell.get("column"))
        except (TypeError, ValueError):
            continue
        if row < 1 or col < 1:
            continue
        grid[(row, col)] = _normalize_cell(cell.get("value"))
        max_row = max(max_row, row)
        max_col = max(max_col, col)
    return [
        [grid.get((row, col), "") for col in range(1, max_col + 1)]
        for row in range(1, max_row + 1)
    ]


def _sheet_has_data(sheet: Any) -> bool:
    if not isinstance(sheet, dict):
        return False
    rows = sheet.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list | tuple):
                if any(cell not in (None, "") for cell in row):
                    return True
            elif row not in (None, "", []):
                return True
    cells = sheet.get("cells")
    if isinstance(cells, list):
        for cell in cells:
            if isinstance(cell, dict) and str(cell.get("value") or "").strip():
                return True
    return False


def _normalize_workbook_sheets(sheets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for index, sheet in enumerate(sheets or []):
        if isinstance(sheet, dict) and not sheet.get("rows") and sheet.get("cells"):
            rows = _rows_from_cells(sheet.get("cells"))
        else:
            raw_rows = sheet.get("rows") if isinstance(sheet, dict) else sheet
            rows = []
            for row in raw_rows or []:
                if isinstance(row, list | tuple):
                    rows.append([_normalize_cell(cell) for cell in row])
                else:
                    rows.append([_normalize_cell(row)])
        raw_name = sheet.get("name") if isinstance(sheet, dict) else None
        name = _safe_sheet_name(raw_name or f"Sheet{index + 1}", used_names)
        result.append({"name": name, "rows": rows})
    if not result:
        result.append({"name": "Sheet1", "rows": []})
    return result


def _xlsx_col_letter(index: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""

    letters = ""
    number = max(1, int(index))
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _xlsx_cell_xml(ref: str, value: Any, style: int) -> str:
    if value is None or value == "":
        return ""
    style_attr = f' s="{style}"' if style else ""
    if isinstance(value, bool):
        return f'<c r="{ref}"{style_attr} t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, int | float):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    text = str(value)
    if text.startswith("=") and len(text) > 1:
        return f'<c r="{ref}"{style_attr}><f>{html.escape(text[1:], quote=False)}</f></c>'
    escaped = html.escape(text, quote=False)
    return (
        f'<c r="{ref}"{style_attr} t="inlineStr">'
        f'<is><t xml:space="preserve">{escaped}</t></is></c>'
    )


def _xlsx_column_widths(rows: list[list[Any]]) -> list[float]:
    widths: dict[int, int] = {}
    for row in rows:
        for col_index, cell in enumerate(row, start=1):
            length = len(str(cell)) if cell not in (None, "") else 0
            widths[col_index] = max(widths.get(col_index, 0), length)
    if not widths:
        return []
    return [
        min(60.0, max(8.0, widths.get(col, 0) + 2))
        for col in range(1, max(widths) + 1)
    ]


def _xlsx_sheet_xml(sheet: dict[str, Any]) -> str:
    rows = sheet["rows"]
    row_count = len(rows)
    col_count = max((len(row) for row in rows), default=0)
    has_header = row_count >= 1 and col_count >= 1
    body_rows: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        style = 1 if (row_index == 1 and has_header) else 2
        cells = [
            _xlsx_cell_xml(f"{_xlsx_col_letter(col)}{row_index}", value, style)
            for col, value in enumerate(row, start=1)
        ]
        body_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    widths = _xlsx_column_widths(rows)
    cols_xml = ""
    if widths:
        col_entries = "".join(
            f'<col min="{i}" max="{i}" width="{w:.2f}" customWidth="1"/>'
            for i, w in enumerate(widths, start=1)
        )
        cols_xml = f"<cols>{col_entries}</cols>"
    dimension = "A1"
    if row_count and col_count:
        dimension = f"A1:{_xlsx_col_letter(col_count)}{row_count}"
    pane = ""
    autofilter = ""
    if has_header:
        pane = (
            '<sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
            'activePane="bottomLeft" state="frozen"/>'
            '<selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView>'
        )
        autofilter = f'<autoFilter ref="A1:{_xlsx_col_letter(col_count)}1"/>'
    else:
        pane = '<sheetView workbookViewId="0"/>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_A_NS}" xmlns:r="{_R_NS}">'
        f'<dimension ref="{dimension}"/>'
        f"<sheetViews>{pane}</sheetViews>"
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"{cols_xml}"
        f'<sheetData>{"".join(body_rows)}</sheetData>'
        f"{autofilter}"
        "</worksheet>"
    )


def _write_workbook_xlsx(
    path: Path,
    sheets: list[dict[str, Any]],
    *,
    title: str,
) -> None:
    sheet_overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.sheet.main+xml"/>'
        f"{sheet_overrides}"
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/'
        'relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        "</Relationships>"
    )
    sheet_tags = "".join(
        f'<sheet name="{html.escape(sheet["name"], quote=True)}" '
        f'sheetId="{i}" r:id="rId{i}"/>'
        for i, sheet in enumerate(sheets, start=1)
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_A_NS}" xmlns:r="{_R_NS}">'
        f"<sheets>{sheet_tags}</sheets>"
        '<calcPr fullCalcOnLoad="1"/>'
        "</workbook>"
    )
    styles_rid = len(sheets) + 1
    sheet_rels = "".join(
        f'<Relationship Id="rId{i}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        f"{sheet_rels}"
        f'<Relationship Id="rId{styles_rid}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<styleSheet xmlns="{_A_NS}">'
        '<fonts count="2">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FF1F3864"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="3">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid">'
        '<fgColor rgb="FFDDE6F0"/><bgColor indexed="64"/></patternFill></fill>'
        "</fills>"
        '<borders count="2">'
        "<border><left/><right/><top/><bottom/><diagonal/></border>"
        '<border><left style="thin"><color rgb="FFBFBFBF"/></left>'
        '<right style="thin"><color rgb="FFBFBFBF"/></right>'
        '<top style="thin"><color rgb="FFBFBFBF"/></top>'
        '<bottom style="thin"><color rgb="FFBFBFBF"/></bottom>'
        "<diagonal/></border>"
        "</borders>"
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/>'
        "</cellStyleXfs>"
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" '
        'applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="left" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"/>'
        "</cellXfs>"
        '<cellStyles count="1">'
        '<cellStyle name="Normal" xfId="0" builtinId="0"/>'
        "</cellStyles>"
        "</styleSheet>"
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
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("docProps/core.xml", core)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        for index, sheet in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _xlsx_sheet_xml(sheet))


def _coerce_scalar(text: Any) -> Any:
    """Turn a numeric-looking string into an int/float so spreadsheets compute and
    sort correctly, while preserving codes/ids (leading zeros) and formulas as text."""

    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped or stripped.startswith("="):
        return stripped
    if re.fullmatch(r"[-+]?(?:0|[1-9]\d*)", stripped):
        return int(stripped)
    if re.fullmatch(r"[-+]?(?:0|[1-9]\d*)?\.\d+", stripped):
        try:
            return float(stripped)
        except ValueError:
            return stripped
    return stripped


def _rows_from_delimited(text: str) -> list[list[Any]]:
    sample = text.strip("\n")
    delimiter = "\t" if "\t" in sample.splitlines()[0] else "," if "," in sample else None
    if delimiter is None:
        return [[_coerce_scalar(line)] for line in sample.splitlines() if line.strip()]
    rows: list[list[Any]] = []
    for parsed in csv.reader(sample.splitlines(), delimiter=delimiter):
        if any(cell.strip() for cell in parsed):
            rows.append([_coerce_scalar(cell) for cell in parsed])
    return rows


def build_workbook_sheets(
    *,
    sheets: Any = None,
    body: str | None = None,
    default_name: str = "Sheet1",
) -> list[dict[str, Any]]:
    """Assemble workbook sheets from a structured ``sheets`` argument (``rows`` or
    ``cells``), or by parsing Markdown tables (each becomes a sheet) or CSV/TSV out
    of ``body``. Structured sheets carrying no usable data fall back to the body."""

    structured = (
        [sheet for sheet in sheets if isinstance(sheet, dict)]
        if isinstance(sheets, list)
        else []
    )
    if structured and any(_sheet_has_data(sheet) for sheet in structured):
        return structured
    text = str(body or "").strip()
    if text:
        blocks = _parse_markdown_blocks(text)
        tables = [
            block for block in blocks if block.get("type") == "table" and block.get("rows")
        ]
        if tables:
            coerced = [
                [[_coerce_scalar(cell) for cell in row] for row in table["rows"]]
                for table in tables
            ]
            if len(coerced) == 1:
                return [{"name": default_name, "rows": coerced[0]}]
            return [
                {
                    "name": f"{default_name} {index}" if index > 1 else default_name,
                    "rows": rows,
                }
                for index, rows in enumerate(coerced, start=1)
            ]
        return [{"name": default_name, "rows": _rows_from_delimited(text)}]
    return structured or [{"name": default_name, "rows": []}]


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
    elif fmt in {"xlsx", "xlsm"}:
        if not zipfile.is_zipfile(target):
            raise DocumentRuntimeError(f"XLSX is not a valid ZIP package: {target}")
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise DocumentRuntimeError(f"XLSX has duplicate ZIP members: {target}")
            required = {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml"}
            missing = sorted(required - set(names))
            if missing:
                raise DocumentRuntimeError(f"XLSX missing required members {missing}: {target}")
            worksheets = [n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml")]
            if not worksheets:
                raise DocumentRuntimeError(f"XLSX has no worksheet parts: {target}")
            try:
                ET.fromstring(archive.read("xl/workbook.xml"))
                for member in worksheets:
                    ET.fromstring(archive.read(member))
            except ET.ParseError as exc:
                raise DocumentRuntimeError(f"XLSX XML is not well-formed: {exc}") from exc
            result["zip_members"] = len(names)
            result["worksheet_count"] = len(worksheets)
    elif fmt == "pptx":
        if not zipfile.is_zipfile(target):
            raise DocumentRuntimeError(f"PPTX is not a valid ZIP package: {target}")
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise DocumentRuntimeError(f"PPTX has duplicate ZIP members: {target}")
            required = {"[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml"}
            missing = sorted(required - set(names))
            if missing:
                raise DocumentRuntimeError(f"PPTX missing required members {missing}: {target}")
            slides = [
                n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            ]
            if not slides:
                raise DocumentRuntimeError(f"PPTX has no slide parts: {target}")
            try:
                ET.fromstring(archive.read("ppt/presentation.xml"))
                for member in slides:
                    ET.fromstring(archive.read(member))
            except ET.ParseError as exc:
                raise DocumentRuntimeError(f"PPTX XML is not well-formed: {exc}") from exc
            result["zip_members"] = len(names)
            result["slide_count"] = len(slides)
    elif fmt == "pdf":
        data = target.read_bytes()
        if not data.lstrip().startswith(b"%PDF"):
            raise DocumentRuntimeError(f"PDF missing %PDF header: {target}")
        if b"%%EOF" not in data:
            raise DocumentRuntimeError(f"PDF missing %%EOF trailer: {target}")
        if b"/Catalog" not in data or b"xref" not in data:
            raise DocumentRuntimeError(f"PDF missing catalog/xref: {target}")
        result["has_eof"] = True
    elif fmt == "svg":
        text = target.read_text(encoding="utf-8")
        root = _parse_xml(text, "SVG")
        if root.tag.rsplit("}", 1)[-1] != "svg":
            raise DocumentRuntimeError(f"not an SVG document (root={root.tag}): {target}")
        result["utf8"] = True
        result["svg_root"] = True
    elif fmt in {"md", "txt", "text", "csv", "json", "html", "htm"}:
        # Strict UTF-8 decode proves text artifacts are not binary garbage.
        target.read_text(encoding="utf-8")
        result["utf8"] = True
    return result


def _safe_artifact_filename(value: str) -> str:
    cleaned = re.sub(r"[^\w.\- ()\[\]]+", "_", Path(str(value or "")).name).strip(" .")
    return cleaned[:180] or "artifact.bin"


_BULLET_ITEM_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_ITEM_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")


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
        ordered = _ORDERED_ITEM_RE.match(line)
        bullet = None if ordered else _BULLET_ITEM_RE.match(line)
        if ordered or bullet:
            is_ordered = bool(ordered)
            items: list[str] = []
            while index < len(lines):
                nxt = lines[index]
                nxt_ordered = _ORDERED_ITEM_RE.match(nxt)
                nxt_bullet = None if nxt_ordered else _BULLET_ITEM_RE.match(nxt)
                if is_ordered and nxt_ordered:
                    items.append(nxt_ordered.group(1).strip())
                elif not is_ordered and nxt_bullet:
                    items.append(nxt_bullet.group(1).strip())
                else:
                    break
                index += 1
            blocks.append({"type": "list", "ordered": is_ordered, "items": items})
            continue
        if not line.strip():
            blocks.append({"type": "empty"})
            index += 1
            continue
        # Paragraph: accumulate until blank/heading/table/list.
        paragraph_lines = [line.rstrip()]
        index += 1
        while index < len(lines):
            nxt = lines[index]
            if (
                not nxt.strip()
                or re.match(r"^(#{1,6})\s+", nxt)
                or _ORDERED_ITEM_RE.match(nxt)
                or _BULLET_ITEM_RE.match(nxt)
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


_INLINE_MD_RE = re.compile(
    r"(?P<link>\[(?P<ltext>[^\]]+)\]\((?P<lurl>[^)\s]+)\))"
    r"|(?P<code>`(?P<ctext>[^`]+)`)"
    r"|(?P<bi>\*\*\*(?P<bitext>[^*]+)\*\*\*)"
    r"|(?P<bold>\*\*(?P<btext>[^*]+)\*\*)"
    r"|(?P<bold2>__(?P<btext2>[^_]+)__)"
    r"|(?P<italic>\*(?P<itext>[^*]+)\*)"
    r"|(?P<italic2>_(?P<itext2>[^_]+)_)"
)

_DOCX_HEADING_STYLES: dict[int, dict[str, Any]] = {
    1: {"sz": "36", "color": "1F3864", "bold": True, "italic": False,
        "before": "280", "after": "140"},
    2: {"sz": "30", "color": "2E5496", "bold": True, "italic": False,
        "before": "240", "after": "120"},
    3: {"sz": "26", "color": "1F3864", "bold": True, "italic": False,
        "before": "200", "after": "100"},
    4: {"sz": "24", "color": "2E5496", "bold": True, "italic": True,
        "before": "180", "after": "80"},
    5: {"sz": "23", "color": "404040", "bold": True, "italic": False,
        "before": "160", "after": "80"},
    6: {"sz": "22", "color": "404040", "bold": False, "italic": True,
        "before": "160", "after": "80"},
}


def _parse_inline_runs(text: str) -> list[dict[str, Any]]:
    """Split Markdown inline markup into styled runs (bold/italic/code/links)."""

    source = str(text or "")
    runs: list[dict[str, Any]] = []
    pos = 0
    for match in _INLINE_MD_RE.finditer(source):
        if match.start() > pos:
            runs.append({"text": source[pos:match.start()]})
        if match.group("link"):
            runs.append({"text": match.group("ltext"), "href": match.group("lurl")})
        elif match.group("code"):
            runs.append({"text": match.group("ctext"), "code": True})
        elif match.group("bi"):
            runs.append({"text": match.group("bitext"), "bold": True, "italic": True})
        elif match.group("bold"):
            runs.append({"text": match.group("btext"), "bold": True})
        elif match.group("bold2"):
            runs.append({"text": match.group("btext2"), "bold": True})
        elif match.group("italic"):
            runs.append({"text": match.group("itext"), "italic": True})
        elif match.group("italic2"):
            runs.append({"text": match.group("itext2"), "italic": True})
        pos = match.end()
    if pos < len(source):
        runs.append({"text": source[pos:]})
    return runs or [{"text": source}]


def _docx_run_props(
    run: dict[str, Any], *, force_bold: bool = False, hyperlink: bool = False
) -> str:
    props: list[str] = []
    if run.get("bold") or force_bold:
        props.append("<w:b/>")
    if run.get("italic"):
        props.append("<w:i/>")
    if run.get("code"):
        props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>')
        props.append('<w:color w:val="A31515"/>')
    if hyperlink:
        props.append('<w:color w:val="0563C1"/>')
        props.append('<w:u w:val="single"/>')
    return f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""


def _docx_runs_xml(
    runs: list[dict[str, Any]],
    hyperlinks: list[str],
    *,
    force_bold: bool = False,
) -> str:
    parts: list[str] = []
    for run in runs:
        text = html.escape(str(run.get("text") or ""), quote=False)
        href = run.get("href")
        rpr = _docx_run_props(run, force_bold=force_bold, hyperlink=bool(href))
        run_xml = f'<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'
        if href:
            # rId1=styles, rId2=numbering, rId3+ = external hyperlinks.
            rid = len(hyperlinks) + 3
            hyperlinks.append(str(href))
            parts.append(f'<w:hyperlink r:id="rId{rid}">{run_xml}</w:hyperlink>')
        else:
            parts.append(run_xml)
    return "".join(parts)


def _docx_styles_xml() -> str:
    heading_styles = ""
    for level, spec in _DOCX_HEADING_STYLES.items():
        rpr = "<w:b/>" if spec["bold"] else ""
        rpr += "<w:i/>" if spec["italic"] else ""
        rpr += (
            f'<w:color w:val="{spec["color"]}"/>'
            f'<w:sz w:val="{spec["sz"]}"/><w:szCs w:val="{spec["sz"]}"/>'
        )
        heading_styles += (
            f'<w:style w:type="paragraph" w:styleId="Heading{level}">'
            f'<w:name w:val="heading {level}"/><w:basedOn w:val="Normal"/>'
            f'<w:next w:val="Normal"/><w:uiPriority w:val="{level}"/>'
            f'<w:pPr><w:keepNext/>'
            f'<w:spacing w:before="{spec["before"]}" w:after="{spec["after"]}"/>'
            f'<w:outlineLvl w:val="{level - 1}"/></w:pPr>'
            f"<w:rPr>{rpr}</w:rPr></w:style>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:styles xmlns:w="{_W_NS}">'
        '<w:docDefaults><w:rPrDefault><w:rPr>'
        '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
        '<w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/>'
        '<w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr>'
        '<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/></w:rPr>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="ListParagraph">'
        '<w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>'
        '<w:uiPriority w:val="34"/>'
        '<w:pPr><w:ind w:left="720"/><w:contextualSpacing/></w:pPr></w:style>'
        f"{heading_styles}"
        "</w:styles>"
    )


def _docx_numbering_xml(ordered_num_ids: list[int]) -> str:
    ordered_nums = "".join(
        f'<w:num w:numId="{num_id}"><w:abstractNumId w:val="1"/>'
        '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>'
        for num_id in ordered_num_ids
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:numbering xmlns:w="{_W_NS}">'
        '<w:abstractNum w:abstractNumId="0">'
        '<w:multiLevelType w:val="hybridMultilevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
        '<w:lvlText w:val="&#8226;"/><w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>'
        "</w:abstractNum>"
        '<w:abstractNum w:abstractNumId="1">'
        '<w:multiLevelType w:val="hybridMultilevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%1."/><w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>'
        "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        f"{ordered_nums}"
        "</w:numbering>"
    )


def _docx_plain_runs_xml(runs: list[dict[str, Any]], *, force_bold: bool = False) -> str:
    """Render inline runs WITHOUT hyperlink relationships — a link becomes styled
    ``text (url)`` text. Used for appended content so the edit needs no rels/numbering
    surgery and the source package stays otherwise byte-identical."""

    parts: list[str] = []
    for run in runs:
        href = run.get("href")
        text = str(run.get("text") or "")
        if href:
            text = f"{text} ({href})" if text else str(href)
        escaped = html.escape(text, quote=False)
        rpr = _docx_run_props(run, force_bold=force_bold, hyperlink=bool(href))
        parts.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escaped}</w:t></w:r>')
    return "".join(parts)


def _docx_table_xml(rows: list[list[Any]], render_runs: Any) -> str:
    """Render a self-contained bordered table. ``render_runs(cell_text, is_header)``
    returns the run XML for a cell, so callers decide how links are handled."""

    if not rows:
        return ""
    col_count = max(len(row) for row in rows)
    row_xml: list[str] = []
    for row_index, row in enumerate(rows):
        is_header = row_index == 0
        cell_xml: list[str] = []
        for col in range(col_count):
            cell = row[col] if col < len(row) else ""
            runs = render_runs(str(cell), is_header)
            shading = (
                '<w:shd w:val="clear" w:color="auto" w:fill="DDE6F0"/>' if is_header else ""
            )
            cell_xml.append(
                f'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/>{shading}</w:tcPr>'
                f"<w:p>{runs}</w:p></w:tc>"
            )
        row_xml.append(f"<w:tr>{''.join(cell_xml)}</w:tr>")
    grid = "".join("<w:gridCol/>" for _ in range(col_count))
    borders = (
        "<w:tblBorders>"
        '<w:top w:val="single" w:sz="4" w:color="BFBFBF"/>'
        '<w:left w:val="single" w:sz="4" w:color="BFBFBF"/>'
        '<w:bottom w:val="single" w:sz="4" w:color="BFBFBF"/>'
        '<w:right w:val="single" w:sz="4" w:color="BFBFBF"/>'
        '<w:insideH w:val="single" w:sz="4" w:color="BFBFBF"/>'
        '<w:insideV w:val="single" w:sz="4" w:color="BFBFBF"/>'
        "</w:tblBorders>"
    )
    return (
        '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>'
        f'<w:tblLook w:firstRow="1" w:val="0420"/>{borders}</w:tblPr>'
        f'<w:tblGrid>{grid}</w:tblGrid>{"".join(row_xml)}</w:tbl>'
    )


def _docx_append_blocks_xml(blocks: list[dict[str, Any]]) -> str:
    """Render Markdown blocks to self-contained DOCX body XML for appending: headings
    use style refs, lists get a text bullet/number prefix, tables are self-contained,
    and links render as ``text (url)`` — nothing that needs new package parts."""

    out: list[str] = []
    for block in blocks:
        kind = str(block.get("type") or "")
        if kind == "heading":
            level = max(1, min(6, int(block.get("level") or 1)))
            runs = _docx_plain_runs_xml(_parse_inline_runs(str(block.get("text") or "")))
            out.append(f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>{runs}</w:p>')
        elif kind == "list":
            ordered = bool(block.get("ordered"))
            for position, item in enumerate(block.get("items") or [], start=1):
                prefix = f"{position}. " if ordered else "• "
                runs = _docx_plain_runs_xml(_parse_inline_runs(prefix + str(item)))
                out.append(f"<w:p>{runs}</w:p>")
        elif kind == "table":
            out.append(
                _docx_table_xml(
                    list(block.get("rows") or []),
                    lambda text, is_header: _docx_plain_runs_xml(
                        _parse_inline_runs(text), force_bold=is_header
                    ),
                )
            )
        elif kind == "empty":
            out.append("<w:p/>")
        else:
            runs = _docx_plain_runs_xml(_parse_inline_runs(str(block.get("text") or "")))
            out.append(f"<w:p>{runs}</w:p>" if runs else "<w:p/>")
    return "".join(out)


def _docx_insert_body(document_xml: str, appended_xml: str) -> str:
    """Insert body XML before the final body-level ``<w:sectPr>`` (else before
    ``</w:body>``), so appended content lands in the document's last section."""

    if not appended_xml:
        return document_xml
    marker = document_xml.rfind("<w:sectPr")
    if marker == -1:
        marker = document_xml.rfind("</w:body>")
    if marker == -1:
        raise DocumentRuntimeError("DOCX body has no <w:sectPr> or </w:body> to append before.")
    return document_xml[:marker] + appended_xml + document_xml[marker:]


def _write_structured_docx(
    path: Path,
    blocks: list[dict[str, Any]],
    *,
    title: str,
) -> None:
    body_xml: list[str] = []
    hyperlinks: list[str] = []
    ordered_num_ids: list[int] = []
    next_ordered_num = 2
    for block in blocks:
        kind = str(block.get("type") or "")
        if kind == "heading":
            level = max(1, min(6, int(block.get("level") or 1)))
            runs = _docx_runs_xml(_parse_inline_runs(str(block.get("text") or "")), hyperlinks)
            body_xml.append(
                f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>{runs}</w:p>'
            )
        elif kind == "list":
            if block.get("ordered"):
                num_id = next_ordered_num
                next_ordered_num += 1
                ordered_num_ids.append(num_id)
            else:
                num_id = 1
            for item in block.get("items") or []:
                runs = _docx_runs_xml(_parse_inline_runs(str(item)), hyperlinks)
                body_xml.append(
                    '<w:p><w:pPr><w:pStyle w:val="ListParagraph"/>'
                    f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'
                    f"</w:pPr>{runs}</w:p>"
                )
        elif kind == "table":
            rows = list(block.get("rows") or [])
            if not rows:
                continue
            body_xml.append(
                _docx_table_xml(
                    rows,
                    lambda text, is_header: _docx_runs_xml(
                        _parse_inline_runs(str(text)), hyperlinks, force_bold=is_header
                    ),
                )
            )
        elif kind == "empty":
            body_xml.append("<w:p/>")
        else:
            runs = _docx_runs_xml(_parse_inline_runs(str(block.get("text") or "")), hyperlinks)
            body_xml.append(f"<w:p>{runs}</w:p>" if runs else "<w:p/>")
    if not body_xml:
        body_xml.append(
            f'<w:p><w:r><w:t xml:space="preserve">{html.escape(title)}</w:t></w:r></w:p>'
        )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}" xmlns:r="{_R_NS}"><w:body>'
        + "".join(body_xml)
        + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
        "</w:sectPr></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/numbering.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
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
    hyperlink_rels = "".join(
        f'<Relationship Id="rId{i + 3}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        f'Target="{html.escape(url, quote=True)}" TargetMode="External"/>'
        for i, url in enumerate(hyperlinks)
    )
    word_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" '
        'Target="numbering.xml"/>'
        f"{hyperlink_rels}"
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
        archive.writestr("word/styles.xml", _docx_styles_xml())
        archive.writestr("word/numbering.xml", _docx_numbering_xml(ordered_num_ids))
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


def normalize_document_parse_error(
    exc: BaseException, *, path: Path | None = None
) -> dict[str, Any]:
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
