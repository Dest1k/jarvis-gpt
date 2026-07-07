from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .config import JarvisSettings
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
    ".json": "application/json",
    ".md": "text/markdown",
    ".toml": "text/toml",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
}
MAX_TEXT_BYTES = 5 * 1024 * 1024
CHUNK_CHARS = 1_800
CHUNK_OVERLAP = 180


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

    def ingest_path(self, source_path: str | Path) -> dict[str, Any]:
        path = Path(source_path).expanduser().resolve(strict=False)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"File does not exist: {path}")
        with path.open("rb") as stream:
            stored = self._store_stream(path.name, stream)
        return self._index_stored_file(stored, source_path=path)

    def ingest_upload(self, filename: str, stream: BinaryIO) -> dict[str, Any]:
        stored = self._store_stream(filename, stream)
        return self._index_stored_file(stored, source_path=None)

    def _store_stream(self, filename: str, stream: BinaryIO) -> StoredFile:
        self.files_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(filename)
        temp_path = self.files_dir / f".{new_id('upload')}.tmp"
        digest = hashlib.sha256()
        size = 0
        with temp_path.open("wb") as target:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                target.write(chunk)

        sha256 = digest.hexdigest()
        stored_path = self.files_dir / f"{sha256[:12]}_{safe_name}"
        if stored_path.exists():
            temp_path.unlink(missing_ok=True)
        else:
            temp_path.replace(stored_path)

        suffix = Path(safe_name).suffix.lower()
        mime_type = (
            mimetypes.guess_type(safe_name)[0]
            or EXTENSION_MIME_TYPES.get(suffix)
            or "application/octet-stream"
        )
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
        chunks: list[str] = []
        status = "stored"
        error: str | None = None
        if _is_text_file(stored):
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
        else:
            error = "Binary or unsupported text format; file stored without chunks."

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


def _safe_filename(filename: str) -> str:
    raw = Path(filename or "upload.txt").name
    clean = re.sub(r"[^\w.\- ()\[\]]+", "_", raw, flags=re.UNICODE).strip(" .")
    return clean[:180] or "upload.txt"


def _is_text_file(stored: StoredFile) -> bool:
    suffix = Path(stored.name).suffix.lower()
    return (
        suffix in TEXT_EXTENSIONS
        or stored.mime_type.startswith("text/")
        or stored.mime_type in TEXT_MIME_TYPES
    )


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
