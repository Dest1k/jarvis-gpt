from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import zipfile
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
from .storage import JarvisStorage, new_id

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


@dataclass(frozen=True)
class StoredFile:
    name: str
    path: Path
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
            stored = self._store_stream(path.name, stream)
        return self._index_stored_file(stored, source_path=path)

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
        stored = self._store_stream(filename, stream)
        return self._index_stored_file(stored, source_path=None)

    def _store_stream(self, filename: str, stream: BinaryIO) -> StoredFile:
        files_dir = self._files_dir_for_actor()
        files_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(filename)
        temp_path = files_dir / f".{new_id('upload')}.tmp"
        digest = hashlib.sha256()
        size = 0
        try:
            with temp_path.open("wb") as target:
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
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        sha256 = digest.hexdigest()
        stored_path = files_dir / f"{sha256[:12]}_{safe_name}"
        if stored_path.exists():
            temp_path.unlink(missing_ok=True)
        else:
            temp_path.replace(stored_path)

        mime_type = _mime_type_for_name(safe_name)
        return StoredFile(
            name=safe_name,
            path=stored_path,
            sha256=sha256,
            size=size,
            mime_type=mime_type,
        )

    def _index_stored_file(
        self,
        stored: StoredFile,
        *,
        source_path: Path | None,
    ) -> dict[str, Any]:
        existing = self.storage.get_file_by_sha256(stored.sha256)
        if existing is not None:
            existing_path = Path(existing["stored_path"]).resolve(strict=False)
            reindexed = False
            if existing.get("status") != "indexed" and stored.path.is_file():
                chunks, status, error = self._extract_index(stored)
                updated = self.storage.reindex_file(
                    str(existing["id"]),
                    chunks,
                    name=stored.name,
                    source_path=source_path,
                    stored_path=stored.path,
                    size=stored.size,
                    mime_type=stored.mime_type,
                    status=status,
                    error=error,
                )
                existing = updated or existing
                reindexed = bool(chunks)
                if reindexed:
                    self.storage.record_audit(
                        actor="operator",
                        action="file.reindex",
                        target_type="file",
                        target_id=str(existing["id"]),
                        summary=(
                            f"File reindexed: {existing['name']} ({len(chunks)} chunk(s))."
                        ),
                            after={"file": existing, "chunks_indexed": len(chunks)},
                        )
            active_path = Path(existing["stored_path"]).resolve(strict=False)
            for obsolete_path in (existing_path, stored.path.resolve(strict=False)):
                if obsolete_path != active_path:
                    obsolete_path.unlink(missing_ok=True)
            self.storage.add_event(
                kind="file.ingest.deduplicated",
                title=f"File already indexed: {existing['name']}",
                payload={
                    "file_id": existing["id"],
                    "sha256": stored.sha256,
                    "reindexed": reindexed,
                },
            )
            return {
                "file": existing,
                "chunks_indexed": int(existing.get("chunk_count") or 0),
                "deduplicated": True,
                "reindexed": reindexed,
            }

        chunks, status, error = self._extract_index(stored)
        file_record = self.storage.create_file_record(
            name=stored.name,
            source_path=source_path,
            stored_path=stored.path,
            sha256=stored.sha256,
            size=stored.size,
            mime_type=stored.mime_type,
            status=status,
            error=error,
            chunk_count=len(chunks),
        )
        if chunks:
            self.storage.add_file_chunks(file_record["id"], chunks)
            file_record = self.storage.get_file(file_record["id"]) or file_record

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
        return {"file": file_record, "chunks_indexed": len(chunks)}

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
