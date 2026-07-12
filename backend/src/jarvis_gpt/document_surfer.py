"""Isolated, production-grade document-surfing module for the Jarvis agent.

``JarvisDocumentSurfer`` is an isolated document black box: it accepts high-level
commands over documents, archives, and generic files, extracts structure and text,
compares / searches / summarizes / edits / generates artifacts, and returns clean
structured results (dict / Markdown). Low-level parsing stays in
``document_runtime`` / ``archive_runtime`` / ``file_types``; this module is the
operator-facing capability surface, analogous to ``web_surfer``.

Layers:

1. Safety & detection — size limits, XXE-safe XML, archive zip-bomb guards,
   magic-byte + extension type recognition.
2. Documents — DOCX/XLSX/PDF/text/HTML + extended PPTX/ODT/RTF.
3. Archives — list/extract/read/create for zip/tar/gz/bz2/xz (+ optional 7z/rar).
4. Corpus ops & generation — search/summarize/compare/generate/convert/package.

Dependencies: stdlib + ``document_runtime``. Optional: pypdf, py7zr, rarfile,
tesseract/pdftoppm, LibreOffice.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import logging
import re
import shutil
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .archive_runtime import (
    ArchiveConfig,
    ArchiveError,
    archive_capabilities,
    is_archive_path,
)
from .archive_runtime import (
    create_archive as archive_create,
)
from .archive_runtime import (
    extract_archive as archive_extract,
)
from .archive_runtime import (
    list_archive as archive_list,
)
from .archive_runtime import (
    read_archive_member as archive_read_member,
)
from .document_runtime import (
    DOCUMENT_EXTENSIONS,
    DOCUMENT_MIME_TYPES,
    MAX_DOCUMENT_BYTES,
    DocumentRuntimeError,
    apply_document_replacements,
    compare_documents,
    document_mime_type,
    extract_document,
    is_supported_document,
)
from .file_types import (
    archive_kinds,
    document_kinds,
    identify_path,
    is_document_kind,
)

LOGGER = logging.getLogger("jarvis.document_surfer")

__all__ = [
    "DocumentSurferError",
    "DocumentUnsupportedError",
    "DocumentSafetyError",
    "DocumentGenerationError",
    "DocumentSurferConfig",
    "JarvisDocumentSurfer",
    "document_surfer_capabilities",
    "supported_document_kinds",
    "is_archive_path",
    "identify_path",
]

# --------------------------------------------------------------------------- #
# Extended formats (stdlib best-effort; document_runtime stays authoritative
# for the core OOXML / PDF / text set used by existing tools).
# --------------------------------------------------------------------------- #
_EXTENDED_EXTENSIONS = frozenset({".pptx", ".odt", ".rtf", ".ppt", ".ods"})
_GENERATABLE_FORMATS = frozenset(
    {"md", "markdown", "txt", "text", "csv", "json", "html", "htm", "docx", "xlsx"}
)
_TEXTUAL_OUTPUT = frozenset({"md", "markdown", "txt", "text", "csv", "json", "html", "htm"})

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A_DRAW = "http://schemas.openxmlformats.org/drawingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_ODF_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"

_UNSAFE_XML = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_ENTITY_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9]{1,30}|[А-ЯЁ][А-Яа-яё0-9]{1,30})\b"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)
_MONEY_RE = re.compile(
    r"(?:₽|\$|€|£|RUB|USD|EUR)\s?[\d\s]{1,18}(?:[.,]\d{1,2})?"
    r"|[\d\s]{1,18}(?:[.,]\d{1,2})?\s?(?:₽|руб\.?|RUB|USD|EUR|\$|€)",
    re.IGNORECASE,
)
_DATE_RE = re.compile(
    r"\b(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2})\b"
)
_PHONE_RE = re.compile(
    r"(?:\+?\d[\d\-\s()]{8,}\d)"
)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class DocumentSurferError(RuntimeError):
    """Base class for every error raised by the document surfer."""


class DocumentUnsupportedError(DocumentSurferError):
    """Raised when a document type cannot be processed."""


class DocumentSafetyError(DocumentSurferError):
    """Raised when a document fails safety or size policy checks."""


class DocumentGenerationError(DocumentSurferError):
    """Raised when a generated artifact cannot be produced."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class DocumentSurferConfig:
    """Tunable behavior for :class:`JarvisDocumentSurfer`."""

    max_document_bytes: int = MAX_DOCUMENT_BYTES
    max_chars: int = 60_000
    max_search_hits: int = 80
    max_corpus_files: int = 40
    max_diffs: int = 120
    max_replacements: int = 50
    max_generated_chars: int = 500_000
    output_dir: Path | None = None
    allow_extended_formats: bool = True
    include_entity_signals: bool = True
    max_archive_members: int = 5_000
    max_archive_member_bytes: int = 50_000_000
    max_archive_total_bytes: int = 200_000_000

    def __post_init__(self) -> None:
        self.max_document_bytes = max(1_000, int(self.max_document_bytes))
        self.max_chars = max(500, min(500_000, int(self.max_chars)))
        self.max_search_hits = max(1, min(500, int(self.max_search_hits)))
        self.max_corpus_files = max(1, min(200, int(self.max_corpus_files)))
        self.max_diffs = max(10, min(2_000, int(self.max_diffs)))
        self.max_replacements = max(1, min(200, int(self.max_replacements)))
        self.max_generated_chars = max(1_000, min(5_000_000, int(self.max_generated_chars)))
        self.max_archive_members = max(1, min(50_000, int(self.max_archive_members)))
        self.max_archive_member_bytes = max(1_000, int(self.max_archive_member_bytes))
        self.max_archive_total_bytes = max(1_000, int(self.max_archive_total_bytes))
        if self.output_dir is not None:
            self.output_dir = Path(self.output_dir)

    def archive_config(self) -> ArchiveConfig:
        return ArchiveConfig(
            max_members=self.max_archive_members,
            max_member_bytes=self.max_archive_member_bytes,
            max_total_uncompressed_bytes=self.max_archive_total_bytes,
        )


def supported_document_kinds() -> dict[str, list[str]]:
    """Return supported kinds grouped by engine path."""

    core = sorted(ext.lstrip(".") for ext in DOCUMENT_EXTENSIONS)
    extended = sorted(ext.lstrip(".") for ext in _EXTENDED_EXTENSIONS)
    generatable = sorted(_GENERATABLE_FORMATS)
    return {
        "extract_core": core,
        "extract_extended": extended,
        "generate": generatable,
        "mime_core": sorted(DOCUMENT_MIME_TYPES),
        "archives": archive_kinds(),
        "documents": document_kinds(),
        "file_families": [
            "archive",
            "document",
            "text",
            "code",
            "image",
            "audio",
            "video",
            "font",
            "executable",
            "disk",
            "database",
            "certificate",
            "binary",
        ],
    }


def document_surfer_capabilities(*, path: Path | None = None) -> dict[str, Any]:
    """Probe host-side optional tools and format coverage."""

    tesseract = shutil.which("tesseract")
    pdftoppm = shutil.which("pdftoppm")
    libre = shutil.which("soffice") or shutil.which("libreoffice")
    whisper = shutil.which("whisper")
    pypdf_ok = False
    try:
        import pypdf  # type: ignore[import-not-found]  # noqa: F401

        pypdf_ok = True
    except Exception:  # noqa: BLE001
        pypdf_ok = False
    kinds = supported_document_kinds()
    payload: dict[str, Any] = {
        "formats": kinds,
        "archives": archive_capabilities(),
        "host_tools": {
            "tesseract": bool(tesseract),
            "pdftoppm": bool(pdftoppm),
            "libreoffice": bool(libre),
            "whisper": bool(whisper),
            "pypdf": pypdf_ok,
        },
        "ocr": {
            "available": bool(tesseract and pdftoppm),
            "engine": "tesseract+pdftoppm" if tesseract and pdftoppm else None,
        },
        "visual_diff": {"available": bool(libre)},
        "mutation": {
            "exact_replacements": [
                "docx", "xlsx", "xlsm", "txt", "md", "html", "csv", "tsv", "json", "xml", "log",
            ],
            "generate": kinds["generate"],
            "archives_create": archive_capabilities().get("create") or [],
            "never_overwrites_original": True,
        },
        "file_identify": True,
    }
    if path is not None:
        identified = identify_path(path)
        payload["path_supported"] = is_document_path_supported(path) or identified.is_archive
        payload["path_kind"] = identified.kind
        payload["path_type"] = identified.to_dict()
    return payload


def is_document_path_supported(path: str | Path, mime_type: str = "") -> bool:
    path_obj = Path(path)
    if is_supported_document(path_obj, mime_type):
        return True
    if path_obj.suffix.lower() in _EXTENDED_EXTENSIONS:
        return True
    try:
        info = identify_path(path_obj)
    except Exception:  # noqa: BLE001
        return False
    return bool(info.is_document or info.is_text or is_document_kind(info.kind))


def _kind_for_path(path: Path) -> str:
    try:
        return identify_path(path).kind
    except Exception:  # noqa: BLE001
        suffix = path.suffix.lower().lstrip(".")
        return suffix or "unknown"


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #
class JarvisDocumentSurfer:
    """Isolated document capability black box.

    Sync-first (documents are local I/O). Safe to call from tool handlers and
    agent loops. Never mutates source files in place.
    """

    def __init__(self, config: DocumentSurferConfig | None = None) -> None:
        self.config = config or DocumentSurferConfig()

    # -- lifecycle ------------------------------------------------------------ #
    def start(self) -> None:
        """Prepare output directory when configured."""

        if self.config.output_dir is not None:
            self.config.output_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        """No long-lived resources; kept for web_surfer API symmetry."""

        return None

    def __enter__(self) -> JarvisDocumentSurfer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- type recognition & generic files ------------------------------------ #
    def identify(self, path: str | Path) -> dict[str, Any]:
        """Magic-byte + extension type recognition for any file."""

        resolved = Path(path).resolve(strict=False)
        if not resolved.exists() or not resolved.is_file():
            raise DocumentSafetyError(f"File does not exist: {resolved}")
        info = identify_path(resolved)
        digest = ""
        size = resolved.stat().st_size
        if size <= min(self.config.max_document_bytes, 32_000_000):
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        else:
            # Hash first 1 MiB + size marker for oversized files.
            with resolved.open("rb") as handle:
                digest = hashlib.sha256(handle.read(1_048_576)).hexdigest() + ":partial"
        return {
            "ok": True,
            "mode": "identify",
            "path": str(resolved),
            "name": resolved.name,
            "size": size,
            "sha256": digest,
            "type": info.to_dict(),
            "is_archive": info.is_archive and not (
                info.is_document
                and info.kind in {"docx", "xlsx", "xlsm", "pptx", "odt", "ods", "odp", "epub"}
            ),
            "is_document": bool(info.is_document or info.is_text),
            "markdown": (
                f"# Identify: {resolved.name}\n\n"
                f"- kind: `{info.kind}`\n"
                f"- family: `{info.family}`\n"
                f"- mime: `{info.mime_type}`\n"
                f"- confidence: {info.confidence}\n"
                f"- source: {info.source}\n"
                f"- size: {size} bytes\n"
            ),
        }

    def probe(self, path: str | Path, *, max_chars: int | None = None) -> dict[str, Any]:
        """Unified entry: identify + document inspect and/or archive list."""

        identity = self.identify(path)
        type_info = identity.get("type") if isinstance(identity.get("type"), dict) else {}
        result: dict[str, Any] = {
            "ok": True,
            "mode": "probe",
            "identify": identity,
            "document": None,
            "archive": None,
        }
        path_obj = Path(path)
        if identity.get("is_document") or is_document_path_supported(path_obj):
            try:
                result["document"] = self.inspect(path_obj, max_chars=max_chars)
            except DocumentSurferError as exc:
                result["document_error"] = str(exc)
        if identity.get("is_archive") or is_archive_path(path_obj):
            try:
                result["archive"] = self.list_archive(path_obj)
            except DocumentSurferError as exc:
                result["archive_error"] = str(exc)
        kind = type_info.get("kind") or "unknown"
        result["markdown"] = (
            f"# Probe: {path_obj.name}\n\n"
            f"- kind: `{kind}`\n"
            f"- document: {'yes' if result.get('document') else 'no'}\n"
            f"- archive: {'yes' if result.get('archive') else 'no'}\n"
        )
        return result

    # -- archives ------------------------------------------------------------- #
    def list_archive(self, path: str | Path, *, prefix: str = "") -> dict[str, Any]:
        try:
            return archive_list(path, config=self.config.archive_config(), prefix=prefix)
        except (ArchiveError, OSError) as exc:
            raise DocumentSurferError(str(exc)) from exc

    def extract_archive(
        self,
        path: str | Path,
        *,
        members: Sequence[str] | None = None,
        output_dir: str | Path | None = None,
        output_name: str | None = None,
    ) -> dict[str, Any]:
        dest = self._archive_output_dir(path, output_dir=output_dir, output_name=output_name)
        try:
            return archive_extract(
                path,
                output_dir=dest,
                members=members,
                config=self.config.archive_config(),
            )
        except (ArchiveError, OSError) as exc:
            raise DocumentSurferError(str(exc)) from exc

    def read_archive_member(
        self,
        path: str | Path,
        member: str,
        *,
        max_bytes: int | None = None,
        as_document: bool = False,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        try:
            payload = archive_read_member(
                path,
                member,
                max_bytes=max_bytes,
                config=self.config.archive_config(),
            )
        except (ArchiveError, OSError) as exc:
            raise DocumentSurferError(str(exc)) from exc
        if as_document:
            # Materialize member under output dir, then run document load.
            extracted = self.extract_archive(path, members=[member])
            files = extracted.get("extracted") or []
            if not files:
                raise DocumentSurferError(f"Failed to materialize archive member: {member}")
            member_path = Path(str(files[0]["path"]))
            try:
                document = self._load(member_path, max_chars=max_chars)
                payload["document"] = _public_document(document, include_text=True)
            except DocumentSurferError as exc:
                payload["document_error"] = str(exc)
        return payload

    def create_archive(
        self,
        paths: Sequence[str | Path],
        *,
        archive_format: str = "zip",
        output_path: str | Path | None = None,
        output_name: str | None = None,
    ) -> dict[str, Any]:
        fmt = str(archive_format or "zip").strip().lower().lstrip(".")
        if fmt in {"tgz"}:
            fmt = "tar.gz"
        if fmt in {"tbz", "tbz2"}:
            fmt = "tar.bz2"
        if fmt in {"txz"}:
            fmt = "tar.xz"
        suffix_map = {
            "zip": ".zip",
            "tar": ".tar",
            "tar.gz": ".tar.gz",
            "tar.bz2": ".tar.bz2",
            "tar.xz": ".tar.xz",
            "gz": ".gz",
        }
        if fmt not in suffix_map:
            raise DocumentGenerationError(
                f"Unsupported archive create format '{fmt}'. "
                f"Supported: {', '.join(sorted(suffix_map))}"
            )
        default_suffix = suffix_map[fmt]
        destination = self._resolve_output_path(
            Path(f"archive{default_suffix}"),
            output_path=output_path,
            output_name=output_name or f"archive{default_suffix}",
            default_suffix=default_suffix,
            stem_suffix="",
        )
        try:
            return archive_create(
                paths,
                output_path=destination,
                archive_format=fmt,
                config=self.config.archive_config(),
            )
        except (ArchiveError, OSError) as exc:
            raise DocumentSurferError(str(exc)) from exc

    def search_archive(
        self,
        path: str | Path,
        query: str,
        *,
        regex: bool = False,
        case_sensitive: bool = False,
        max_members: int = 40,
        max_bytes_per_member: int = 1_000_000,
    ) -> dict[str, Any]:
        """Search text-like members inside an archive."""

        listing = self.list_archive(path)
        needle = str(query or "").strip()
        if not needle:
            raise DocumentSurferError("search query must not be empty")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(needle if regex else re.escape(needle), flags)
        except re.error as exc:
            raise DocumentSurferError(f"Invalid search pattern: {exc}") from exc
        hits: list[dict[str, Any]] = []
        scanned = 0
        for item in listing.get("members") or []:
            if item.get("is_dir") or item.get("unsafe") or item.get("skipped"):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            try:
                member = self.read_archive_member(
                    path,
                    name,
                    max_bytes=max_bytes_per_member,
                )
            except DocumentSurferError:
                continue
            scanned += 1
            text = str(member.get("text_preview") or "")
            if not text:
                # try decode raw if type looks textual
                type_info = member.get("type") if isinstance(member.get("type"), dict) else {}
                if not (type_info.get("is_text") or type_info.get("is_document")):
                    if scanned >= max_members:
                        break
                    continue
            for match in pattern.finditer(text):
                start = max(0, match.start() - 60)
                end = min(len(text), match.end() + 60)
                hits.append(
                    {
                        "member": name,
                        "match": match.group(0),
                        "offset": match.start(),
                        "snippet": text[start:end].replace("\n", " "),
                    }
                )
                if len(hits) >= self.config.max_search_hits:
                    break
            if len(hits) >= self.config.max_search_hits or scanned >= max_members:
                break
        return {
            "ok": True,
            "mode": "search_archive",
            "archive": str(Path(path)),
            "query": needle,
            "scanned_members": scanned,
            "hit_count": len(hits),
            "hits": hits,
            "markdown": (
                f"# Archive search: {needle}\n\n"
                f"- archive: `{Path(path).name}`\n"
                f"- scanned members: {scanned}\n"
                f"- hits: {len(hits)}\n"
            ),
        }

    # -- single document ------------------------------------------------------ #
    def inspect(self, path: str | Path, *, max_chars: int | None = None) -> dict[str, Any]:
        """Lightweight metadata + structure + capability snapshot."""

        # Archives get listing-oriented inspect without forcing document extract.
        if is_archive_path(path):
            listing = self.list_archive(path)
            identity = self.identify(path)
            return {
                "ok": True,
                "mode": "inspect",
                "document": None,
                "archive": listing,
                "type": identity.get("type"),
                "text_preview": "",
                "summary": (
                    f"Archive {listing.get('kind')}: "
                    f"{listing.get('member_count', 0)} member(s) listed."
                ),
                "capabilities": {"kind": "archive", "archive": True},
                "markdown": listing.get("markdown") or identity.get("markdown") or "",
            }

        document = self._load(path, max_chars=max_chars)
        text = str(document.get("text") or "")
        caps = self.capabilities_for(document, path=Path(document["path"]))
        identity = identify_path(Path(document["path"]))
        return {
            "ok": True,
            "mode": "inspect",
            "document": _public_document(document, include_text=False),
            "type": identity.to_dict(),
            "text_preview": _short(text, 1600),
            "summary": _summary_line(document),
            "capabilities": caps,
            "markdown": self.to_markdown(document, mode="inspect"),
        }

    def read(self, path: str | Path, *, max_chars: int | None = None) -> dict[str, Any]:
        """Full bounded extraction of text + structure."""

        document = self._load(path, max_chars=max_chars)
        text = str(document.get("text") or "")
        return {
            "ok": bool(text.strip()),
            "mode": "read",
            "document": _public_document(document, include_text=True),
            "text": text,
            "markdown": self.to_markdown(document, mode="read"),
        }

    def analyze(
        self,
        path: str | Path,
        *,
        max_chars: int | None = None,
        instruction: str = "",
    ) -> dict[str, Any]:
        """Deep analysis: structure, signals, OCR readiness, recommendations."""

        document = self._load(path, max_chars=max_chars)
        text = str(document.get("text") or "")
        caps = self.capabilities_for(document, path=Path(document["path"]))
        signals = self._entity_signals(text) if self.config.include_entity_signals else {}
        tables = self._tables_from_document(document)
        formulas = self._formulas_from_document(document)
        recommendations = self._recommendations(
            document,
            capabilities=caps,
            instruction=instruction,
            comparison=None,
        )
        return {
            "ok": True,
            "mode": "analyze",
            "document": _public_document(document, include_text=False),
            "text_preview": _short(text, 2400),
            "summary": _summary_line(document),
            "capabilities": caps,
            "signals": signals,
            "tables": tables[:12],
            "formulas": formulas[:40],
            "recommendations": recommendations,
            "instruction": " ".join(instruction.split()) if instruction else "",
            "markdown": self.to_markdown(document, mode="analyze", extra={
                "signals": signals,
                "recommendations": recommendations,
            }),
        }

    def review(
        self,
        path: str | Path,
        *,
        reference_path: str | Path | None = None,
        instruction: str = "",
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Review for edit readiness, OCR, Excel audit, optional reference diff."""

        document = self._load(path, max_chars=max_chars)
        reference = self._load(reference_path, max_chars=max_chars) if reference_path else None
        text = str(document.get("text") or "")
        caps = self.capabilities_for(document, path=Path(document["path"]))
        comparison = (
            compare_documents(document, reference, max_diffs=self.config.max_diffs)
            if reference is not None
            else None
        )
        review = {
            "capabilities": caps,
            "recommendations": self._recommendations(
                document,
                capabilities=caps,
                instruction=instruction,
                comparison=comparison,
            ),
            "redline": {
                "supported": str(document.get("kind") or "") == "docx",
                "mode": (
                    "planned_text_redline"
                    if str(document.get("kind") or "") == "docx"
                    else "text_diff_only"
                ),
                "can_apply_exact_replacements": str(document.get("kind") or "")
                in {
                    "docx", "txt", "md", "html", "csv", "tsv", "json", "xml", "log", "xlsx", "xlsm",
                },
                "track_changes_native": False,
            },
            "excel": {
                "supported": str(document.get("kind") or "") in {"xlsx", "xlsm"},
                "formula_count": int((document.get("structure") or {}).get("formula_count") or 0),
                "sample_formulas": self._formulas_from_document(document)[:20],
            },
            "ocr": {
                "needed": bool((caps.get("ocr") or {}).get("needed")),
                "available": bool((caps.get("ocr") or {}).get("available")),
                "page_count": int((document.get("structure") or {}).get("page_count") or 0),
                "text_chars": len(text),
            },
        }
        return {
            "ok": True,
            "mode": "review",
            "document": _public_document(document, include_text=False),
            "reference": _public_document(reference, include_text=False) if reference else None,
            "text_preview": _short(text, 2000),
            "comparison": comparison,
            "review": review,
            "markdown": self.to_markdown(document, mode="review", extra={"review": review}),
        }

    def compare(
        self,
        left_path: str | Path,
        right_path: str | Path,
        *,
        max_diffs: int | None = None,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        left = self._load(left_path, max_chars=max_chars)
        right = self._load(right_path, max_chars=max_chars)
        limit = max_diffs if max_diffs is not None else self.config.max_diffs
        comparison = compare_documents(left, right, max_diffs=limit)
        return {
            "ok": True,
            "mode": "compare",
            "left": _public_document(left, include_text=False),
            "right": _public_document(right, include_text=False),
            "comparison": comparison,
            "markdown": (
                f"# Compare: {left.get('name')} vs {right.get('name')}\n\n"
                f"- additions: {comparison['stats']['additions']}\n"
                f"- deletions: {comparison['stats']['deletions']}\n"
                f"- diff lines: {comparison['stats']['diff_lines']}\n"
            ),
        }

    def search(
        self,
        query: str,
        paths: Sequence[str | Path],
        *,
        regex: bool = False,
        case_sensitive: bool = False,
        max_chars: int | None = None,
        max_hits: int | None = None,
    ) -> dict[str, Any]:
        """Search one or many documents; returns bounded snippet hits."""

        needle = str(query or "").strip()
        if not needle:
            raise DocumentSurferError("search query must not be empty")
        hit_limit = max_hits if max_hits is not None else self.config.max_search_hits
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(needle if regex else re.escape(needle), flags)
        except re.error as exc:
            raise DocumentSurferError(f"Invalid search pattern: {exc}") from exc

        hits: list[dict[str, Any]] = []
        scanned = 0
        errors: list[dict[str, str]] = []
        for raw in list(paths)[: self.config.max_corpus_files]:
            try:
                document = self._load(raw, max_chars=max_chars)
            except DocumentSurferError as exc:
                errors.append({"path": str(raw), "error": str(exc)})
                continue
            scanned += 1
            text = str(document.get("text") or "")
            for match in pattern.finditer(text):
                start = max(0, match.start() - 80)
                end = min(len(text), match.end() + 80)
                hits.append(
                    {
                        "path": document.get("path"),
                        "name": document.get("name"),
                        "kind": document.get("kind"),
                        "match": match.group(0),
                        "offset": match.start(),
                        "snippet": text[start:end].replace("\n", " "),
                    }
                )
                if len(hits) >= hit_limit:
                    break
            if len(hits) >= hit_limit:
                break
        return {
            "ok": True,
            "mode": "search",
            "query": needle,
            "regex": regex,
            "scanned_files": scanned,
            "hit_count": len(hits),
            "hits": hits,
            "errors": errors,
            "truncated": len(hits) >= hit_limit,
            "markdown": _search_markdown(needle, hits, scanned),
        }

    def summarize_corpus(
        self,
        paths: Sequence[str | Path],
        *,
        focus: str | None = None,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Extractive multi-document brief (no LLM required)."""

        docs: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for raw in list(paths)[: self.config.max_corpus_files]:
            try:
                docs.append(self._load(raw, max_chars=max_chars))
            except DocumentSurferError as exc:
                errors.append({"path": str(raw), "error": str(exc)})

        focus_norm = " ".join(str(focus or "").split())
        themes: list[str] = []
        entities: list[str] = []
        per_file: list[dict[str, Any]] = []
        combined_parts: list[str] = []
        for document in docs:
            text = str(document.get("text") or "")
            signals = self._entity_signals(text)
            headings = list((document.get("structure") or {}).get("headings") or [])[:8]
            if not headings:
                headings = _top_lines(text, limit=5)
            if focus_norm:
                focused = [
                    line
                    for line in text.splitlines()
                    if focus_norm.casefold() in line.casefold()
                ][:6]
            else:
                focused = []
            per_file.append(
                {
                    "name": document.get("name"),
                    "path": document.get("path"),
                    "kind": document.get("kind"),
                    "chars": len(text),
                    "headings": headings,
                    "focus_hits": focused,
                    "entities": (signals.get("entities") or [])[:12],
                }
            )
            themes.extend(headings[:3])
            entities.extend((signals.get("entities") or [])[:8])
            combined_parts.append(f"## {document.get('name')}\n" + "\n".join(headings[:6]))

        theme_counts = _count_labels(themes)
        entity_counts = _count_labels(entities)
        summary = {
            "files": len(docs),
            "errors": len(errors),
            "focus": focus_norm or None,
            "themes": [item["label"] for item in theme_counts[:12]],
            "entities": [item["label"] for item in entity_counts[:24]],
            "theme_counts": theme_counts[:12],
            "entity_counts": entity_counts[:24],
            "total_chars": sum(int(item.get("chars") or 0) for item in per_file),
        }
        markdown = (
            f"# Corpus summary ({summary['files']} file(s))\n\n"
            + (f"Focus: {focus_norm}\n\n" if focus_norm else "")
            + "## Themes\n"
            + "\n".join(f"- {item}" for item in summary["themes"] or ["(none)"])
            + "\n\n## Files\n"
            + "\n".join(
                f"- **{item['name']}** ({item['kind']}, {item['chars']} chars)"
                for item in per_file
            )
            + "\n"
        )
        return {
            "ok": True,
            "mode": "summarize_corpus",
            "summary": summary,
            "files": per_file,
            "errors": errors,
            "combined_outline": "\n\n".join(combined_parts),
            "markdown": markdown,
        }

    def edit_plan(
        self,
        path: str | Path,
        instruction: str,
        *,
        reference_path: str | Path | None = None,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        instruction_clean = " ".join(str(instruction or "").split())
        if not instruction_clean:
            raise DocumentSurferError("edit plan requires a non-empty instruction")
        document = self._load(path, max_chars=max_chars)
        reference = self._load(reference_path, max_chars=max_chars) if reference_path else None
        comparison = (
            compare_documents(document, reference, max_diffs=80) if reference is not None else None
        )
        plan = self._edit_plan_payload(instruction_clean, document, reference, comparison)
        return {
            "ok": True,
            "mode": "edit_plan",
            "document": _public_document(document, include_text=False),
            "reference": _public_document(reference, include_text=False) if reference else None,
            "plan": plan,
            "comparison": comparison,
            "markdown": (
                f"# Edit plan: {document.get('name')}\n\n"
                f"Instruction: {instruction_clean}\n\n"
                + "\n".join(
                    f"{index}. {step}"
                    for index, step in enumerate(plan["recommended_steps"], 1)
                )
            ),
        }

    def apply_replacements(
        self,
        path: str | Path,
        replacements: Sequence[dict[str, str]],
        *,
        output_path: str | Path | None = None,
        output_name: str | None = None,
    ) -> dict[str, Any]:
        source = Path(path).resolve(strict=False)
        self._assert_readable(source)
        if not is_document_path_supported(source):
            raise DocumentUnsupportedError(f"Unsupported document type: {source.suffix}")
        pairs = _normalize_replacements(replacements, limit=self.config.max_replacements)
        if not pairs:
            raise DocumentSurferError("No valid replacements were provided")
        destination = self._resolve_output_path(
            source,
            output_path=output_path,
            output_name=output_name,
            default_suffix=source.suffix or ".txt",
            stem_suffix=".edited",
        )
        try:
            result = apply_document_replacements(source, pairs, destination)
        except DocumentRuntimeError as exc:
            raise DocumentSurferError(str(exc)) from exc
        return {
            "ok": True,
            "mode": "apply_replacements",
            "source": str(source),
            "output": {
                "path": str(destination),
                "changed": result["changed"],
                "kind": result.get("kind"),
                "size": destination.stat().st_size if destination.exists() else 0,
            },
            "replacements": len(pairs),
            "markdown": (
                f"# Edited copy\n\n"
                f"- source: `{source.name}`\n"
                f"- output: `{destination}`\n"
                f"- replacements applied: {result['changed']}\n"
            ),
        }

    def generate(
        self,
        *,
        title: str,
        body: str | Sequence[str] | Sequence[dict[str, Any]],
        output_format: str = "md",
        output_path: str | Path | None = None,
        output_name: str | None = None,
        metadata: dict[str, Any] | None = None,
        sections: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Generate a new document artifact (never writes over existing sources)."""

        fmt = str(output_format or "md").strip().lower().lstrip(".")
        if fmt == "markdown":
            fmt = "md"
        if fmt == "text":
            fmt = "txt"
        if fmt == "htm":
            fmt = "html"
        if fmt not in _GENERATABLE_FORMATS:
            raise DocumentGenerationError(
                f"Unsupported output format '{fmt}'. "
                f"Supported: {', '.join(sorted(_GENERATABLE_FORMATS))}"
            )
        title_clean = " ".join(str(title or "Document").split())[:200] or "Document"
        content = _normalize_body(body, sections=sections)
        if len(content) > self.config.max_generated_chars:
            raise DocumentGenerationError(
                f"Generated body exceeds max_generated_chars "
                f"({len(content)} > {self.config.max_generated_chars})"
            )
        suffix = f".{fmt}"
        destination = self._resolve_output_path(
            Path(f"{_safe_filename(title_clean)}{suffix}"),
            output_path=output_path,
            output_name=output_name,
            default_suffix=suffix,
            stem_suffix="",
        )
        meta = dict(metadata or {})
        meta.setdefault("generated_at", datetime.now(UTC).isoformat())
        meta.setdefault("generator", "jarvis.document_surfer")
        meta.setdefault("title", title_clean)

        try:
            if fmt == "md":
                destination.write_text(
                    _render_markdown(title_clean, content, meta),
                    encoding="utf-8",
                    newline="\n",
                )
            elif fmt == "txt":
                destination.write_text(
                    f"{title_clean}\n{'=' * len(title_clean)}\n\n{content}\n",
                    encoding="utf-8",
                    newline="\n",
                )
            elif fmt == "csv":
                destination.write_text(_render_csv(content), encoding="utf-8", newline="\n")
            elif fmt == "json":
                payload = {
                    "title": title_clean,
                    "metadata": meta,
                    "body": content,
                    "sections": list(sections or []),
                }
                destination.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
            elif fmt == "html":
                destination.write_text(
                    _render_html(title_clean, content, meta),
                    encoding="utf-8",
                    newline="\n",
                )
            elif fmt == "docx":
                _write_minimal_docx(destination, title_clean, content, meta)
            elif fmt == "xlsx":
                _write_minimal_xlsx(destination, title_clean, content)
            else:  # pragma: no cover - guarded above
                raise DocumentGenerationError(f"Unhandled format: {fmt}")
        except OSError as exc:
            raise DocumentGenerationError(f"Failed to write {destination}: {exc}") from exc

        preview = ""
        structure: dict[str, Any] = {}
        warnings: list[str] = []
        try:
            extracted = extract_document(destination, max_chars=min(self.config.max_chars, 20_000))
            preview = _short(str(extracted.get("text") or ""), 1200)
            structure = dict(extracted.get("structure") or {})
            warnings = list(extracted.get("warnings") or [])
        except DocumentRuntimeError as exc:
            warnings.append(f"Post-generate extract skipped: {exc}")

        return {
            "ok": True,
            "mode": "generate",
            "output": {
                "path": str(destination),
                "name": destination.name,
                "format": fmt,
                "size": destination.stat().st_size,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            },
            "title": title_clean,
            "metadata": meta,
            "structure": structure,
            "text_preview": preview,
            "warnings": warnings,
            "markdown": (
                f"# Generated {fmt.upper()}\n\n"
                f"- title: {title_clean}\n"
                f"- path: `{destination}`\n"
                f"- size: {destination.stat().st_size} bytes\n"
            ),
        }

    def convert(
        self,
        path: str | Path,
        *,
        output_format: str = "md",
        output_path: str | Path | None = None,
        output_name: str | None = None,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Extract source content and regenerate in another supported format."""

        document = self._load(path, max_chars=max_chars)
        title = str(document.get("name") or Path(path).stem)
        body = str(document.get("text") or "")
        if not body.strip():
            raise DocumentSurferError(
                f"Source document has no extractable text for conversion: {document.get('name')}"
            )
        generated = self.generate(
            title=title,
            body=body,
            output_format=output_format,
            output_path=output_path,
            output_name=output_name,
            metadata={
                "converted_from": document.get("path"),
                "source_kind": document.get("kind"),
                "source_mime": document.get("mime_type"),
            },
        )
        generated["mode"] = "convert"
        generated["source"] = _public_document(document, include_text=False)
        return generated

    def package(
        self,
        paths: Sequence[str | Path],
        *,
        output_path: str | Path | None = None,
        output_name: str | None = None,
    ) -> dict[str, Any]:
        """Zip multiple files into a deliverable package under the output dir."""

        members: list[Path] = []
        for raw in list(paths)[: self.config.max_corpus_files]:
            path = Path(raw).resolve(strict=False)
            if not path.exists() or not path.is_file():
                raise DocumentSafetyError(f"Package member does not exist: {path}")
            if path.stat().st_size > self.config.max_document_bytes:
                raise DocumentSafetyError(f"Package member too large: {path}")
            members.append(path)
        if not members:
            raise DocumentSurferError("package requires at least one file")
        destination = self._resolve_output_path(
            Path("documents-package.zip"),
            output_path=output_path,
            output_name=output_name or "documents-package.zip",
            default_suffix=".zip",
            stem_suffix="",
        )
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            used_names: set[str] = set()
            for member in members:
                name = member.name
                if name in used_names:
                    digest = hashlib.sha1(str(member).encode()).hexdigest()[:8]
                    name = f"{member.stem}-{digest}{member.suffix}"
                used_names.add(name)
                archive.write(member, arcname=name)
        return {
            "ok": True,
            "mode": "package",
            "output": {
                "path": str(destination),
                "size": destination.stat().st_size,
                "members": [str(item) for item in members],
                "count": len(members),
            },
            "markdown": f"# Package\n\n- path: `{destination}`\n- members: {len(members)}\n",
        }

    def capabilities(self, path: str | Path | None = None) -> dict[str, Any]:
        if path is None:
            return document_surfer_capabilities()
        resolved = Path(path).resolve(strict=False)
        base = document_surfer_capabilities(path=resolved)
        if resolved.exists() and resolved.is_file() and is_document_path_supported(resolved):
            try:
                document = self._load(resolved)
                base["document"] = self.capabilities_for(document, path=resolved)
            except DocumentSurferError as exc:
                base["document_error"] = str(exc)
        return base

    def capabilities_for(self, document: dict[str, Any], *, path: Path) -> dict[str, Any]:
        text = str(document.get("text") or "")
        kind = str(document.get("kind") or path.suffix.lower().lstrip(".") or "document")
        structure = document.get("structure") if isinstance(document.get("structure"), dict) else {}
        page_count = int(structure.get("page_count") or 0)
        ocr_needed = kind == "pdf" and len(" ".join(text.split())) < max(120, page_count * 80)
        host = document_surfer_capabilities()["host_tools"]
        styles = structure.get("styles") if isinstance(structure.get("styles"), list) else []
        return {
            "kind": kind,
            "text_chars": len(text),
            "has_text": bool(text.strip()),
            "truncated": bool(document.get("truncated")),
            "warnings": list(document.get("warnings") or []),
            "ocr": {
                "needed": ocr_needed,
                "available": bool(
                    host.get("tesseract") and (host.get("pdftoppm") or kind != "pdf")
                ),
                "tesseract": bool(host.get("tesseract")),
                "pdftoppm": bool(host.get("pdftoppm")),
            },
            "word": {
                "redline_plan_supported": kind == "docx",
                "exact_replacements_supported": kind
                in {"docx", "txt", "md", "html", "csv", "tsv", "json", "xml", "log"},
                "comments_detected": int(structure.get("comment_count") or 0),
                "style_count": len(styles),
            },
            "excel": {
                "supported": kind in {"xlsx", "xlsm"},
                "sheet_count": int(structure.get("sheet_count") or 0),
                "formula_count": int(structure.get("formula_count") or 0),
                "style_count": int(structure.get("style_count") or 0),
            },
            "slides": {
                "supported": kind in {"pptx", "odt"},
                "slide_count": int(
                    structure.get("slide_count") or structure.get("paragraph_count") or 0
                ),
            },
            "diff": {
                "text_diff_supported": True,
                "visual_diff_supported": bool(host.get("libreoffice")),
            },
            "generation": {
                "formats": sorted(_GENERATABLE_FORMATS),
            },
        }

    def to_markdown(
        self,
        document: dict[str, Any],
        *,
        mode: str = "read",
        extra: dict[str, Any] | None = None,
    ) -> str:
        name = document.get("name") or "document"
        kind = document.get("kind") or "unknown"
        lines = [
            f"# Document {mode}: {name}",
            "",
            f"- kind: `{kind}`",
            f"- size: {document.get('size', 0)} bytes",
            f"- mime: `{document.get('mime_type') or 'unknown'}`",
            f"- chars: {len(str(document.get('text') or ''))}",
        ]
        structure = document.get("structure") if isinstance(document.get("structure"), dict) else {}
        if structure:
            lines.append(f"- structure: `{json.dumps(structure, ensure_ascii=False)[:400]}`")
        warnings = document.get("warnings") or []
        if warnings:
            lines.append("- warnings:")
            lines.extend(f"  - {item}" for item in warnings[:8])
        if mode in {"read", "analyze"} and document.get("text"):
            lines.extend(["", "## Text preview", "", _short(str(document.get("text")), 3000)])
        if extra:
            if extra.get("recommendations"):
                lines.extend(["", "## Recommendations"])
                lines.extend(f"- {item}" for item in extra["recommendations"][:8])
            if extra.get("signals"):
                signals = extra["signals"]
                lines.extend(["", "## Signals"])
                for key in ("emails", "urls", "dates", "money", "entities"):
                    values = signals.get(key) or []
                    if values:
                        lines.append(f"- {key}: {', '.join(str(v) for v in values[:8])}")
        return "\n".join(lines).rstrip() + "\n"

    # -- internals ------------------------------------------------------------ #
    def _load(self, path: str | Path, *, max_chars: int | None = None) -> dict[str, Any]:
        resolved = Path(path).resolve(strict=False)
        self._assert_readable(resolved)
        limit = max_chars if max_chars is not None else self.config.max_chars
        limit = max(500, min(500_000, int(limit)))
        suffix = resolved.suffix.lower()

        if is_supported_document(resolved):
            try:
                return extract_document(resolved, max_chars=limit)
            except DocumentRuntimeError as exc:
                message = str(exc)
                if "too large" in message.lower():
                    raise DocumentSafetyError(message) from exc
                if "unsupported" in message.lower() or "legacy binary" in message.lower():
                    raise DocumentUnsupportedError(message) from exc
                raise DocumentSurferError(message) from exc

        if self.config.allow_extended_formats and suffix in _EXTENDED_EXTENSIONS:
            try:
                return self._extract_extended(resolved, max_chars=limit)
            except DocumentSurferError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise DocumentSurferError(
                    f"Extended extract failed for {resolved.name}: {exc}"
                ) from exc

        identity = identify_path(resolved)
        if identity.is_text or identity.family in {"code", "text"}:
            return _extract_generic_text(resolved, identity=identity.to_dict(), max_chars=limit)

        raise DocumentUnsupportedError(
            f"Unsupported document type: {suffix or resolved.name}"
        )

    def _assert_readable(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            raise DocumentSafetyError(f"Document does not exist: {path}")
        size = path.stat().st_size
        if size > self.config.max_document_bytes:
            raise DocumentSafetyError(
                f"Document is too large for safe parsing "
                f"({size} > {self.config.max_document_bytes} bytes)."
            )

    def _extract_extended(self, path: Path, *, max_chars: int) -> dict[str, Any]:
        suffix = path.suffix.lower()
        mime_type = document_mime_type(path)
        size = path.stat().st_size
        warnings: list[str] = []
        if suffix == ".pptx":
            payload = _extract_pptx(path)
        elif suffix == ".odt":
            payload = _extract_odt(path)
        elif suffix == ".rtf":
            payload = _extract_rtf(path)
        elif suffix in {".ppt", ".ods"}:
            raise DocumentUnsupportedError(
                f"Legacy/binary format {suffix} is recognized but requires conversion "
                f"to PPTX/ODS XML or export to a supported type before extraction."
            )
        else:
            raise DocumentUnsupportedError(f"Unsupported extended type: {suffix}")

        text = str(payload.get("text") or "").strip()
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

    def _archive_output_dir(
        self,
        source: str | Path,
        *,
        output_dir: str | Path | None,
        output_name: str | None,
    ) -> Path:
        if output_dir is not None:
            dest = Path(output_dir).resolve(strict=False)
            dest.mkdir(parents=True, exist_ok=True)
            return dest
        base = self.config.output_dir or (Path.cwd() / "document-outputs")
        base.mkdir(parents=True, exist_ok=True)
        label = _safe_filename(output_name or f"{Path(source).stem}-extracted")
        candidate = base / label
        if not candidate.exists():
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        dest = base / f"{label}-{stamp}"
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    def _resolve_output_path(
        self,
        source: Path,
        *,
        output_path: str | Path | None,
        output_name: str | None,
        default_suffix: str,
        stem_suffix: str,
    ) -> Path:
        if output_path is not None:
            destination = Path(output_path).resolve(strict=False)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.is_dir():
                raise DocumentGenerationError(f"output_path is a directory: {destination}")
            return destination

        base_dir = self.config.output_dir
        if base_dir is None:
            base_dir = Path.cwd() / "document-outputs"
        base_dir.mkdir(parents=True, exist_ok=True)

        raw_name = str(output_name or "").strip()
        if raw_name:
            safe_name = _safe_filename(Path(raw_name).name)
            if not Path(safe_name).suffix:
                safe_name = f"{safe_name}{default_suffix}"
        else:
            fallback_suffix = default_suffix or source.suffix or ".txt"
            safe_name = f"{_safe_filename(source.stem)}{stem_suffix}{fallback_suffix}"
        candidate = base_dir / safe_name[:180]
        if not candidate.exists():
            return candidate
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        return base_dir / f"{candidate.stem}.{stamp}{candidate.suffix}"

    def _entity_signals(self, text: str) -> dict[str, list[str]]:
        if not text:
            return {
                "emails": [], "urls": [], "dates": [], "money": [], "phones": [], "entities": [],
            }
        emails = _unique(_EMAIL_RE.findall(text), limit=20)
        urls = _unique(_URL_RE.findall(text), limit=20)
        dates = _unique(_DATE_RE.findall(text), limit=20)
        money = _unique(_MONEY_RE.findall(text), limit=20)
        phones = _unique(_PHONE_RE.findall(text), limit=12)
        stop = {
            "The", "This", "That", "With", "From", "Документ", "Table", "Sheet",
            "Page", "True", "False", "None", "Null", "HTTP", "HTTPS", "PDF", "DOCX",
        }
        entities = []
        for match in _ENTITY_RE.findall(text):
            if match in stop or len(match) < 3:
                continue
            entities.append(match)
        return {
            "emails": emails,
            "urls": urls,
            "dates": dates,
            "money": money,
            "phones": phones,
            "entities": _unique(entities, limit=40),
        }

    def _tables_from_document(self, document: dict[str, Any]) -> list[dict[str, Any]]:
        structure = document.get("structure") if isinstance(document.get("structure"), dict) else {}
        tables = structure.get("tables")
        if isinstance(tables, list):
            return [item for item in tables if isinstance(item, dict)]
        sheets = structure.get("sheets")
        if isinstance(sheets, list):
            result = []
            for sheet in sheets:
                if not isinstance(sheet, dict):
                    continue
                result.append(
                    {
                        "name": sheet.get("name"),
                        "rows": sheet.get("preview_rows") or [],
                        "row_count": sheet.get("rows"),
                        "col_count": sheet.get("cols"),
                    }
                )
            return result
        return []

    def _formulas_from_document(self, document: dict[str, Any]) -> list[str]:
        structure = document.get("structure") if isinstance(document.get("structure"), dict) else {}
        formulas: list[str] = []
        sheets = structure.get("sheets")
        if isinstance(sheets, list):
            for sheet in sheets:
                if not isinstance(sheet, dict):
                    continue
                for formula in sheet.get("formulas") or []:
                    text = str(formula).strip()
                    if text:
                        formulas.append(text)
        return formulas

    def _recommendations(
        self,
        document: dict[str, Any],
        *,
        capabilities: dict[str, Any],
        instruction: str,
        comparison: dict[str, Any] | None,
    ) -> list[str]:
        recommendations: list[str] = []
        kind = str(document.get("kind") or "document")
        ocr = capabilities.get("ocr") if isinstance(capabilities.get("ocr"), dict) else {}
        excel = capabilities.get("excel") if isinstance(capabilities.get("excel"), dict) else {}
        word = capabilities.get("word") if isinstance(capabilities.get("word"), dict) else {}
        if ocr.get("needed"):
            if ocr.get("available"):
                recommendations.append(
                    "Run OCR before answering detailed questions about this PDF."
                )
            else:
                recommendations.append(
                    "PDF looks scanned or text-poor; install tesseract + pdftoppm for OCR fallback."
                )
        if kind == "docx" and (instruction or comparison):
            recommendations.append(
                "Use documents.compare + documents.edit.plan before applying replacements."
            )
        if kind in {"xlsx", "xlsm"} and int(excel.get("formula_count") or 0) > 0:
            recommendations.append(
                "Preserve formulas; answer with sheet/formula references when editing."
            )
        if word.get("comments_detected"):
            recommendations.append("Review embedded Word comments before final edits.")
        if kind in {"pptx"}:
            recommendations.append(
                "Slide decks are text-extracted; regenerate DOCX/MD for heavy edits."
            )
        if comparison:
            stats = comparison.get("stats") if isinstance(comparison.get("stats"), dict) else {}
            if int(stats.get("diff_lines") or 0) > 0:
                recommendations.append(
                    "Reference comparison has differences; expose additions/deletions first."
                )
        if not recommendations:
            recommendations.append(
                "Document is text-readable; standard inspect/read/compare/generate flow is enough."
            )
        return recommendations[:8]

    def _edit_plan_payload(
        self,
        instruction: str,
        target: dict[str, Any],
        reference: dict[str, Any] | None,
        comparison: dict[str, Any] | None,
    ) -> dict[str, Any]:
        steps = [
            "Inspect target structure and preserve existing layout unless the instruction "
            "requires it.",
            "Use extracted text as evidence; do not invent content that is not in the "
            "document/reference.",
        ]
        if reference is not None:
            steps.append(
                "Compare target with reference and copy only the requested style/content pattern."
            )
        kind = str(target.get("kind") or "")
        if kind == "docx":
            steps.append(
                "For exact text edits, use documents.apply_replacements to create a DOCX copy."
            )
            steps.append(
                "For major rewrite, generate a new DOCX via documents.generate "
                "and keep the original."
            )
        elif kind in {"xlsx", "xlsm"}:
            steps.append(
                "Preserve formulas and workbook structure; exact shared-string edits can be copied."
            )
        elif kind == "pdf":
            steps.append(
                "Treat PDF as source/review material; create a new DOCX/PDF/MD artifact for edits."
            )
        elif kind == "pptx":
            steps.append("Extract slide text, then generate MD/DOCX deliverable for edits.")
        else:
            steps.append("For text-like files, exact replacements can create an edited copy.")
        return {
            "instruction": instruction,
            "target_summary": _summary_line(target),
            "reference_summary": _summary_line(reference) if reference else None,
            "recommended_steps": steps,
            "candidate_replacements": [],
            "comparison_stats": comparison.get("stats") if comparison else None,
            "tools": [
                "documents.inspect",
                "documents.read",
                "documents.analyze",
                "documents.compare",
                "documents.apply_replacements",
                "documents.generate",
            ],
        }


# --------------------------------------------------------------------------- #
# Extended extractors
# --------------------------------------------------------------------------- #
def _extract_generic_text(
    path: Path,
    *,
    identity: dict[str, Any],
    max_chars: int,
) -> dict[str, Any]:
    size = path.stat().st_size
    byte_limit = min(size, max(4_096, max_chars * 4))
    with path.open("rb") as handle:
        data = handle.read(byte_limit + 1)
    byte_truncated = len(data) > byte_limit
    data = data[:byte_limit]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    else:
        text = data.decode("utf-8-sig", errors="replace")
    char_truncated = len(text) > max_chars
    text = text[:max_chars].rstrip()
    kind = str(identity.get("kind") or path.suffix.lower().lstrip(".") or "text")
    return {
        "kind": kind,
        "path": str(path),
        "name": path.name,
        "mime_type": str(identity.get("mime_type") or "text/plain"),
        "size": size,
        "text": text,
        "truncated": byte_truncated or char_truncated,
        "warnings": (
            ["Text was truncated to the requested max_chars."]
            if byte_truncated or char_truncated
            else []
        ),
        "structure": {
            "line_count": len(text.splitlines()),
        },
    }


def _extract_pptx(path: Path) -> dict[str, Any]:
    if not zipfile.is_zipfile(path):
        raise DocumentSurferError("PPTX is not a valid ZIP package.")
    slides: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
        for name in names[:80]:
            try:
                info = archive.getinfo(name)
            except KeyError:
                continue
            if info.file_size > 2_000_000:
                continue
            xml = archive.read(info).decode("utf-8", errors="replace")
            if _UNSAFE_XML.search(xml):
                raise DocumentSafetyError("Unsafe PPTX slide XML rejected.")
            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                continue
            texts = [
                node.text.strip()
                for node in root.iter(f"{{{_A_DRAW}}}t")
                if node.text and node.text.strip()
            ]
            if texts:
                slides.append("\n".join(texts))
    lines: list[str] = []
    for index, slide in enumerate(slides, start=1):
        lines.append(f"Slide {index}:")
        lines.append(slide)
    return {
        "kind": "pptx",
        "text": "\n".join(lines),
        "structure": {"slide_count": len(slides)},
        "warnings": [],
    }


def _extract_odt(path: Path) -> dict[str, Any]:
    if not zipfile.is_zipfile(path):
        raise DocumentSurferError("ODT is not a valid ZIP package.")
    with zipfile.ZipFile(path) as archive:
        try:
            xml = archive.read("content.xml").decode("utf-8", errors="replace")
        except KeyError as exc:
            raise DocumentSurferError("ODT has no content.xml") from exc
    if _UNSAFE_XML.search(xml):
        raise DocumentSafetyError("Unsafe ODT content.xml rejected.")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DocumentSurferError(f"Invalid ODT XML: {exc}") from exc
    paragraphs = [
        "".join(node.itertext()).strip()
        for node in root.iter(f"{{{_ODF_TEXT_NS}}}p")
    ]
    paragraphs = [item for item in paragraphs if item]
    return {
        "kind": "odt",
        "text": "\n".join(paragraphs),
        "structure": {"paragraph_count": len(paragraphs)},
        "warnings": [],
    }


def _extract_rtf(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    text = raw.decode("latin1", errors="ignore")
    # Strip RTF control words / groups — best effort, not a full RTF parser.
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return {
        "kind": "rtf",
        "text": text,
        "structure": {},
        "warnings": ["Used best-effort RTF stripper; complex RTF may need conversion."],
    }


# --------------------------------------------------------------------------- #
# Generators
# --------------------------------------------------------------------------- #
def _write_minimal_docx(path: Path, title: str, body: str, metadata: dict[str, Any]) -> None:
    paragraphs = [title, *body.splitlines()]
    paragraph_xml = []
    for index, line in enumerate(paragraphs):
        text = html.escape(line, quote=False)
        if index == 0:
            paragraph_xml.append(
                f'<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr>'
                f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
            )
        elif not line.strip():
            paragraph_xml.append("<w:p/>")
        else:
            paragraph_xml.append(
                f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
            )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>'
        + "".join(paragraph_xml)
        + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
        "</w:sectPr></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{_CONTENT_TYPES_NS}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
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
    core = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<cp:coreProperties xmlns:cp="{_CP_NS}" xmlns:dc="{_DC_NS}">'
        f"<dc:title>{html.escape(title)}</dc:title>"
        f"<dc:creator>{html.escape(str(metadata.get('generator') or 'jarvis'))}</dc:creator>"
        f"<cp:lastModifiedBy>jarvis</cp:lastModifiedBy>"
        "</cp:coreProperties>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("docProps/core.xml", core)


def _write_minimal_xlsx(path: Path, title: str, body: str) -> None:
    rows = [["Title", title], ["Line", "Text"]]
    for index, line in enumerate(body.splitlines(), start=1):
        rows.append([str(index), line])
    shared: list[str] = []
    shared_index: dict[str, int] = {}

    def sid(value: str) -> int:
        if value not in shared_index:
            shared_index[value] = len(shared)
            shared.append(value)
        return shared_index[value]

    sheet_rows = []
    for r_index, row in enumerate(rows, start=1):
        cells = []
        for c_index, value in enumerate(row, start=1):
            col = _xlsx_col_name(c_index)
            ref = f"{col}{r_index}"
            idx = sid(str(value))
            cells.append(f'<c r="{ref}" t="s"><v>{idx}</v></c>')
        sheet_rows.append(f'<row r="{r_index}">{"".join(cells)}</row>')

    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{_A_NS}" count="{len(shared)}" uniqueCount="{len(shared)}">'
        + "".join(f"<si><t>{html.escape(item)}</t></si>" for item in shared)
        + "</sst>"
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_A_NS}"><sheetData>'
        + "".join(sheet_rows)
        + "</sheetData></worksheet>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_A_NS}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        "</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_REL_NS}">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{_CONTENT_TYPES_NS}">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        archive.writestr("xl/sharedStrings.xml", shared_xml)


def _xlsx_col_name(index: int) -> str:
    value = index
    letters: list[str] = []
    while value > 0:
        value, rem = divmod(value - 1, 26)
        letters.append(chr(ord("A") + rem))
    return "".join(reversed(letters)) or "A"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _public_document(
    document: dict[str, Any] | None, *, include_text: bool
) -> dict[str, Any] | None:
    if document is None:
        return None
    payload = {
        "path": document.get("path"),
        "name": document.get("name"),
        "kind": document.get("kind"),
        "mime_type": document.get("mime_type"),
        "size": document.get("size"),
        "truncated": document.get("truncated"),
        "warnings": list(document.get("warnings") or []),
        "structure": document.get("structure") or {},
    }
    if include_text:
        payload["text"] = document.get("text") or ""
    return payload


def _summary_line(document: dict[str, Any] | None) -> str:
    if not document:
        return ""
    structure = document.get("structure") if isinstance(document.get("structure"), dict) else {}
    kind = str(document.get("kind") or "document")
    if kind == "docx":
        return (
            f"DOCX: {structure.get('paragraph_count', 0)} paragraph(s), "
            f"{structure.get('table_count', 0)} table(s), "
            f"{structure.get('comment_count', 0)} comment(s)."
        )
    if kind in {"xlsx", "xlsm"}:
        return (
            f"Workbook: {structure.get('sheet_count', 0)} sheet(s), "
            f"{structure.get('formula_count', 0)} formula(s)."
        )
    if kind == "pdf":
        return f"PDF: {structure.get('page_count', 0)} page(s)."
    if kind == "pptx":
        return f"PPTX: {structure.get('slide_count', 0)} slide(s)."
    return f"{kind.upper()}: {document.get('size', 0)} byte(s)."


def _short(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^\w.\- ()\[\]]+", "_", value, flags=re.UNICODE).strip(" .")
    return cleaned[:160] or "document"


def _normalize_replacements(
    replacements: Sequence[dict[str, str]],
    *,
    limit: int,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in list(replacements)[:limit]:
        if not isinstance(item, dict):
            continue
        old = str(item.get("old") or "")
        new = str(item.get("new") or "")
        if old:
            result.append({"old": old[:10000], "new": new[:10000]})
    return result


def _normalize_body(
    body: str | Sequence[str] | Sequence[dict[str, Any]],
    *,
    sections: Sequence[dict[str, Any]] | None,
) -> str:
    parts: list[str] = []
    if isinstance(body, str):
        parts.append(body)
    elif isinstance(body, Sequence):
        for item in body:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                heading = str(item.get("heading") or item.get("title") or "").strip()
                text = str(item.get("body") or item.get("text") or "").strip()
                if heading:
                    parts.append(heading)
                if text:
                    parts.append(text)
            else:
                parts.append(str(item))
    if sections:
        for section in sections:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or section.get("title") or "").strip()
            text = str(section.get("body") or section.get("text") or "").strip()
            if heading:
                parts.append(heading)
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _render_markdown(title: str, body: str, metadata: dict[str, Any]) -> str:
    lines = [f"# {title}", ""]
    if metadata:
        lines.append("<!--")
        for key, value in metadata.items():
            lines.append(f"{key}: {value}")
        lines.append("-->")
        lines.append("")
    lines.append(body.rstrip())
    lines.append("")
    return "\n".join(lines)


def _render_html(title: str, body: str, metadata: dict[str, Any]) -> str:
    paragraphs = "".join(
        f"<p>{html.escape(line)}</p>" if line.strip() else "<p><br/></p>"
        for line in body.splitlines()
    )
    meta_rows = "".join(
        f"<meta name=\"{html.escape(str(key))}\" content=\"{html.escape(str(value))}\"/>"
        for key, value in metadata.items()
    )
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"/>"
        f"<title>{html.escape(title)}</title>{meta_rows}</head><body>"
        f"<h1>{html.escape(title)}</h1>{paragraphs}</body></html>\n"
    )


def _render_csv(body: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["line", "text"])
    for index, line in enumerate(body.splitlines(), start=1):
        writer.writerow([index, line])
    return buffer.getvalue()


def _unique(values: Iterable[str], *, limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _count_labels(values: Sequence[str]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        label = " ".join(str(value).split())
        if not label:
            continue
        counts[label] = counts.get(label, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    return [{"label": label, "count": count} for label, count in ranked]


def _top_lines(text: str, *, limit: int) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[:limit]


def _search_markdown(query: str, hits: Sequence[dict[str, Any]], scanned: int) -> str:
    lines = [
        f"# Search: {query}",
        "",
        f"- scanned files: {scanned}",
        f"- hits: {len(hits)}",
        "",
    ]
    for hit in hits[:20]:
        snippet = _short(str(hit.get("snippet") or ""), 160)
        lines.append(f"- `{hit.get('name')}` @ {hit.get('offset')}: {snippet}")
    return "\n".join(lines) + "\n"
