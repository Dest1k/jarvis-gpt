from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import zipfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .authorization import LEGACY_OWNER_USER_ID, current_user_id
from .config import JarvisSettings
from .document_runtime import (
    DOCUMENT_EXTENSION_MIME_TYPES,
    DocumentRuntimeError,
    extract_document,
    is_supported_document,
)
from .document_surfer import (
    DocumentSurferError,
    JarvisDocumentSurfer,
    is_document_path_supported,
)
from .storage import JarvisStorage

TEXT_EXTENSIONS = {
    ".cfg",
    ".csv",
    ".env",
    ".ini",
    ".json",
    ".log",
    ".md",
    ".py",
    ".ps1",
    ".rst",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/x-yaml",
}
EXTENSION_MIME_TYPES = {
    ".csv": "text/csv",
    **DOCUMENT_EXTENSION_MIME_TYPES,
    ".json": "application/json",
    ".md": "text/markdown",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".rtf": "application/rtf",
    ".toml": "text/toml",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
}
MAX_TEXT_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
CHUNK_CHARS = 1_800
CHUNK_OVERLAP = 180
SURFER_ONLY_DOCUMENT_EXTENSIONS = {".odt", ".pptx", ".rtf"}
OCR_IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


@dataclass(frozen=True)
class StoredFile:
    name: str
    path: Path
    sha256: str
    size: int
    mime_type: str


@dataclass(frozen=True)
class StagedUpload:
    intent_id: str
    name: str
    ready_path: Path
    final_path: Path
    sha256: str
    size: int
    mime_type: str


class FileIngestor:
    def __init__(self, settings: JarvisSettings, storage: JarvisStorage) -> None:
        self.settings = settings
        self.storage = storage
        self.files_dir = settings.data_dir / "files"

    def _files_dir_for_actor(self) -> Path:
        user_id = current_user_id()
        if user_id == LEGACY_OWNER_USER_ID:
            return self.files_dir
        return self.files_dir / "users" / user_id

    def ingest_path(self, source_path: str | Path) -> dict[str, Any]:
        path = Path(source_path).expanduser().resolve(strict=False)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"File does not exist: {path}")
        with path.open("rb") as stream:
            staged = self._store_stream(path.name, stream, source_path=path)
        return self._index_stored_file(staged)

    def ingest_directory(self, root_path: str | Path, *, max_files: int = 50) -> dict[str, Any]:
        root = _resolve_allowed_ingest_root(self.settings, root_path)
        if not root.exists() or not root.is_dir():
            raise FileNotFoundError(f"Directory does not exist: {root}")
        max_files = max(1, min(500, int(max_files)))
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        files_seen = 0
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            dirnames[:] = [
                name
                for name in dirnames
                if not _should_skip_directory(self.settings, current / name)
            ]
            for filename in sorted(filenames, key=str.lower):
                if len(results) >= max_files:
                    break
                path = current / filename
                if not _looks_indexable_path(path):
                    continue
                files_seen += 1
                try:
                    results.append(self.ingest_path(path))
                except Exception as exc:  # noqa: BLE001
                    errors.append({"path": str(path), "error": str(exc)})
            if len(results) >= max_files:
                break
        self.storage.add_event(
            kind="file.ingest_directory",
            title=f"Directory ingested: {root}",
            payload={
                "root": str(root),
                "files_seen": files_seen,
                "files_indexed": len(results),
                "files_failed": len(errors),
            },
        )
        return {
            "root": str(root),
            "files_seen": files_seen,
            "files_indexed": len(results),
            "files_failed": len(errors),
            "results": results,
            "errors": errors,
        }

    def ingest_upload(self, filename: str, stream: BinaryIO) -> dict[str, Any]:
        staged = self._store_stream(filename, stream, source_path=None)
        return self._index_stored_file(staged)

    def _store_stream(
        self,
        filename: str,
        stream: BinaryIO,
        *,
        source_path: Path | None,
    ) -> StagedUpload:
        safe_name = _safe_filename(filename)
        mime_type = _mime_type_for_name(safe_name)
        intent = self.storage.begin_file_upload(
            name=safe_name,
            mime_type=mime_type,
            source_path=source_path,
        )
        part_path = Path(str(intent["part_path"]))
        ready_path = Path(str(intent["ready_path"]))
        part_path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with part_path.open("xb") as target:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise OSError(
                            f"File is larger than the {MAX_UPLOAD_BYTES}-byte upload limit."
                        )
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            part_path.replace(ready_path)
        except BaseException as exc:
            part_path.unlink(missing_ok=True)
            with suppress(Exception):
                self.storage.fail_file_upload_intent(
                    str(intent["id"]),
                    f"Upload stream failed ({type(exc).__name__}): {exc}",
                )
            raise

        sha256 = digest.hexdigest()
        prepared = self.storage.prepare_file_upload(
            str(intent["id"]),
            sha256=sha256,
            size=size,
        )
        return StagedUpload(
            intent_id=str(intent["id"]),
            name=safe_name,
            ready_path=ready_path,
            final_path=Path(str(prepared["final_path"])),
            sha256=sha256,
            size=size,
            mime_type=mime_type,
        )

    def _index_stored_file(
        self,
        staged: StagedUpload,
    ) -> dict[str, Any]:
        preexisting = self.storage.get_file_by_sha256(staged.sha256)
        previous_path = (
            Path(str(preexisting["stored_path"])).resolve(strict=False)
            if preexisting is not None
            else staged.final_path.resolve(strict=False)
        )
        file_record = self.storage.commit_file_upload(staged.intent_id)
        blob_healed = bool(file_record.pop("_blob_healed", False))
        intent = self.storage.get_file_upload_intent(staged.intent_id)
        if intent is None or intent.get("status") != "committed":
            raise RuntimeError("upload blob was not durably committed")
        created = bool(intent.get("created_file"))
        stored = StoredFile(
            name=staged.name,
            path=Path(str(file_record["stored_path"])),
            sha256=staged.sha256,
            size=staged.size,
            mime_type=staged.mime_type,
        )

        owns_indexing = False
        if file_record.get("status") not in {"indexed", "indexing"}:
            file_record, owns_indexing = self.storage.begin_file_reindex(
                str(file_record["id"]),
                name=stored.name,
                source_path=(
                    Path(str(intent["source_path"])) if intent.get("source_path") else None
                ),
                stored_path=stored.path,
                size=stored.size,
                mime_type=stored.mime_type,
            )

        chunks: list[str] = []
        if owns_indexing:
            chunks, status, error = self._extract_index_safely(stored)
            try:
                updated = self.storage.reindex_file(
                    str(file_record["id"]),
                    chunks,
                    name=stored.name,
                    source_path=(
                        Path(str(intent["source_path"])) if intent.get("source_path") else None
                    ),
                    stored_path=stored.path,
                    size=stored.size,
                    mime_type=stored.mime_type,
                    status=status,
                    error=error,
                )
            except Exception as exc:  # noqa: BLE001 - keep the uploaded file retryable
                chunks = []
                detail = str(exc).strip()
                suffix = f": {detail}" if detail else ""
                updated = self.storage.fail_file_indexing(
                    str(file_record["id"]),
                    f"Index finalization failed ({type(exc).__name__}){suffix}",
                )
            file_record = updated or file_record

        active_path = Path(file_record["stored_path"]).resolve(strict=False)
        for obsolete_path in (previous_path, stored.path.resolve(strict=False)):
            self._remove_obsolete_managed_blob(
                obsolete_path,
                active_path=active_path,
                expected_sha256=stored.sha256,
            )

        ocr_job = self._enqueue_automatic_ocr(
            stored,
            file_record,
            verified_reupload=not created,
        )

        if not created:
            reindexed = owns_indexing and bool(chunks)
            if reindexed:
                self.storage.record_audit(
                    actor="operator",
                    action="file.reindex",
                    target_type="file",
                    target_id=str(file_record["id"]),
                    summary=(f"File reindexed: {file_record['name']} ({len(chunks)} chunk(s))."),
                    after={"file": file_record, "chunks_indexed": len(chunks)},
                )
            self.storage.add_event(
                kind="file.ingest.deduplicated",
                title=f"File already stored: {file_record['name']}",
                payload={
                    "file_id": file_record["id"],
                    "sha256": stored.sha256,
                    "indexing": file_record.get("status") == "indexing",
                    "reindexed": reindexed,
                    "blob_healed": blob_healed,
                },
            )
            return {
                "file": file_record,
                "chunks_indexed": int(file_record.get("chunk_count") or 0),
                "deduplicated": True,
                "reindexed": reindexed,
                "blob_healed": blob_healed,
                "ocr_job": ocr_job,
            }

        self.storage.record_audit(
            actor="operator",
            action="file.ingest",
            target_type="file",
            target_id=file_record["id"],
            summary=f"File ingested: {file_record['name']} ({len(chunks)} chunk(s)).",
            after={"file": file_record, "chunks_indexed": len(chunks)},
        )
        self.storage.add_event(
            kind="file.ingest",
            title=f"File ingested: {file_record['name']}",
            payload={"file_id": file_record["id"], "chunks_indexed": len(chunks)},
        )
        return {"file": file_record, "chunks_indexed": len(chunks), "ocr_job": ocr_job}

    def _remove_obsolete_managed_blob(
        self,
        candidate: Path,
        *,
        active_path: Path,
        expected_sha256: str,
    ) -> None:
        resolved = candidate.resolve(strict=False)
        if resolved == active_path or candidate.is_symlink() or not candidate.is_file():
            return
        managed_root = self._files_dir_for_actor().resolve(strict=False)
        try:
            resolved.relative_to(managed_root)
        except ValueError:
            return
        digest = hashlib.sha256()
        try:
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() == expected_sha256:
                candidate.unlink(missing_ok=True)
        except OSError:
            return

    def _enqueue_automatic_ocr(
        self,
        stored: StoredFile,
        file_record: dict[str, Any],
        *,
        verified_reupload: bool = False,
    ) -> dict[str, Any] | None:
        suffix = Path(stored.name).suffix.lower()
        is_image = stored.mime_type.startswith("image/") or suffix in OCR_IMAGE_EXTENSIONS
        is_pdf = stored.mime_type == "application/pdf" or suffix == ".pdf"
        if not is_image and not is_pdf:
            return None
        reason = "image_upload" if is_image else "pdf_completeness_upload"
        return self.storage.enqueue_file_ocr_job(
            str(file_record["id"]),
            reason=reason,
            allow_existing_index=is_pdf,
            restart_failed=verified_reupload,
        )

    def process_next_ocr_job(
        self,
        processor: Callable[[dict[str, Any]], str | dict[str, Any]],
        *,
        worker_id: str = "local-ocr-worker",
        lease_seconds: int = 300,
    ) -> dict[str, Any] | None:
        """Process one current tenant's job through an injected OCR implementation."""

        job = self.storage.claim_next_file_ocr_job(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        if job is None:
            return None
        try:
            raw_result = processor(job)
            if isinstance(raw_result, dict):
                text = str(raw_result.get("text") or "")
                source = str(raw_result.get("source") or "automatic_ocr")
                details = raw_result.get("details")
                warning = raw_result.get("warning")
            else:
                text = str(raw_result or "")
                source = "automatic_ocr"
                details = None
                warning = None
            completed = self.storage.complete_file_ocr_job(
                str(job["id"]),
                str(job["lease_token"]),
                text,
                source=source,
                details=details if isinstance(details, dict) else None,
                warning=str(warning) if warning else None,
            )
            return {"ok": True, "job": completed}
        except Exception as exc:  # noqa: BLE001 - durable retry is the processing contract
            failed = self.storage.fail_file_ocr_job(
                str(job["id"]),
                str(job["lease_token"]),
                f"OCR processor failed ({type(exc).__name__}): {exc}",
            )
            return {"ok": False, "job": failed, "error": str(exc)}

    @classmethod
    def _extract_index_safely(
        cls,
        stored: StoredFile,
    ) -> tuple[list[str], str, str | None]:
        try:
            return cls._extract_index(stored)
        except Exception as exc:  # noqa: BLE001 - the durable record must reach a terminal state
            detail = str(exc).strip()
            suffix = f": {detail}" if detail else ""
            return (
                [],
                "failed",
                f"Document indexing failed unexpectedly ({type(exc).__name__}){suffix}"[:4_000],
            )

    @staticmethod
    def _extract_index(stored: StoredFile) -> tuple[list[str], str, str | None]:
        chunks: list[str] = []
        status = "stored"
        error: str | None = None
        if _is_surfer_only_document(stored):
            try:
                result = JarvisDocumentSurfer().read(stored.path, max_chars=200_000)
                document = (
                    result.get("document") if isinstance(result.get("document"), dict) else {}
                )
                chunks = _chunk_text(str(result.get("text") or ""))
                status = "indexed" if chunks else "stored"
                warnings = (
                    document.get("warnings") if isinstance(document.get("warnings"), list) else []
                )
                error = "; ".join(str(item) for item in warnings[:3]) or None
                if not chunks and error is None:
                    error = "No extractable text found; OCR or document conversion may be required."
            except (DocumentSurferError, OSError, zipfile.BadZipFile) as exc:
                status = "failed"
                error = f"Document indexing failed: {exc}"
        elif _is_text_file(stored):
            if stored.size > MAX_TEXT_BYTES:
                status = "stored"
                error = f"Text indexing skipped: file is larger than {MAX_TEXT_BYTES} bytes."
            else:
                try:
                    text = stored.path.read_text(encoding="utf-8", errors="replace")
                    chunks = _chunk_text(text)
                    status = "indexed" if chunks else "stored"
                    if not chunks:
                        error = "Text file is empty or contains no indexable text."
                except OSError as exc:
                    status = "failed"
                    error = str(exc)
        elif is_supported_document(stored.name, stored.mime_type):
            try:
                document = extract_document(stored.path, max_chars=200_000)
                chunks = _chunk_text(str(document.get("text") or ""))
                status = "indexed" if chunks else "stored"
                warnings = (
                    document.get("warnings") if isinstance(document.get("warnings"), list) else []
                )
                error = "; ".join(str(item) for item in warnings[:3]) or None
                if not chunks and error is None:
                    error = "No extractable text found; OCR or document conversion may be required."
            except (DocumentRuntimeError, OSError, zipfile.BadZipFile) as exc:
                status = "failed"
                error = f"Document indexing failed: {exc}"
        else:
            error = "Binary or unsupported text format; file stored without chunks."
        return chunks, status, error


def extract_file_index(path: str | Path, mime_type: str = "") -> tuple[list[str], str, str | None]:
    """Extract index chunks through the same core/extended document policy as uploads."""

    resolved = Path(path).resolve(strict=False)
    stored = StoredFile(
        name=resolved.name,
        path=resolved,
        sha256="",
        size=resolved.stat().st_size,
        mime_type=mime_type or _mime_type_for_name(resolved.name),
    )
    return FileIngestor._extract_index(stored)


def _safe_filename(filename: str) -> str:
    raw = Path(filename or "upload.txt").name
    clean = re.sub(r"[^\w.\- ()\[\]]+", "_", raw, flags=re.UNICODE).strip(" .")
    if len(clean) <= 180:
        return clean or "upload.txt"
    suffix = Path(clean).suffix
    if suffix and len(suffix) <= 17:
        stem = clean[: 180 - len(suffix)].rstrip(" .")
        return f"{stem}{suffix}" if stem else f"upload{suffix}"
    return clean[:180] or "upload.txt"


def _mime_type_for_name(name: str) -> str:
    """Return a stable MIME type without trusting OS registry overrides first."""

    suffix = Path(name).suffix.lower()
    return (
        EXTENSION_MIME_TYPES.get(suffix)
        or mimetypes.guess_type(name)[0]
        or "application/octet-stream"
    )


def _is_text_file(stored: StoredFile) -> bool:
    suffix = Path(stored.name).suffix.lower()
    return (
        suffix in TEXT_EXTENSIONS
        or stored.mime_type.startswith("text/")
        or stored.mime_type in TEXT_MIME_TYPES
    )


def _is_surfer_only_document(stored: StoredFile) -> bool:
    return (
        Path(stored.name).suffix.lower() in SURFER_ONLY_DOCUMENT_EXTENSIONS
        and not is_supported_document(stored.name, stored.mime_type)
        and is_document_path_supported(stored.path, stored.mime_type)
    )


def _looks_indexable_path(path: Path) -> bool:
    suffix = path.suffix.lower()
    if (
        suffix in TEXT_EXTENSIONS
        or suffix in EXTENSION_MIME_TYPES
        or suffix in SURFER_ONLY_DOCUMENT_EXTENSIONS
        or is_supported_document(path)
    ):
        return True
    mime_type = mimetypes.guess_type(path.name)[0] or ""
    return mime_type.startswith("text/") or mime_type in TEXT_MIME_TYPES


def _resolve_allowed_ingest_root(settings: JarvisSettings, raw_path: str | Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    candidate = candidate.resolve(strict=False)
    roots = [Path.cwd().resolve(strict=False), settings.home.resolve(strict=False)]
    for root in roots:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    roots_text = ", ".join(str(root) for root in roots)
    raise ValueError(f"Directory is outside allowed roots: {roots_text}")


def _should_skip_directory(settings: JarvisSettings, path: Path) -> bool:
    candidate = path.resolve(strict=False)
    skip_roots = [
        settings.cache_dir,
        settings.log_dir,
        settings.model_root,
        settings.docker_dir,
        settings.state_dir,
        settings.data_dir / "files",
    ]
    for root in skip_roots:
        try:
            candidate.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            continue
    return path.name in {".git", ".next", "node_modules", "__pycache__", ".pytest_cache"}


def _chunk_text(
    text: str,
    *,
    chunk_size: int = CHUNK_CHARS,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + chunk_size)
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(normalized):
            break
        start = max(start + 1, end - overlap)
    return chunks
