"""File type recognition for documents, archives, media, and generic binaries.

Uses magic-byte signatures first, then multi-suffix extension hints
(``.tar.gz``, ``.tar.xz``, …). Pure stdlib — no optional codecs required for
identification. Archive *extraction* lives in ``document_surfer``.
"""

from __future__ import annotations

import mimetypes
import re
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Family = Literal[
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
    "unknown",
]

__all__ = [
    "FileTypeInfo",
    "identify_bytes",
    "identify_path",
    "is_archive_kind",
    "is_document_kind",
    "archive_kinds",
    "document_kinds",
]


@dataclass(frozen=True)
class FileTypeInfo:
    """Normalized type verdict for a path or byte prefix."""

    kind: str
    family: Family
    mime_type: str
    extension: str
    confidence: float
    source: str  # magic | extension | content | unknown
    is_archive: bool = False
    is_document: bool = False
    is_text: bool = False
    is_compressed: bool = False
    container: str | None = None  # e.g. zip, tar, ole
    description: str = ""
    magic_hex: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Multi-part extensions checked longest-first.
_COMPOUND_EXTENSIONS: tuple[tuple[str, str, Family, str, bool, bool], ...] = (
    # ext, kind, family, mime, is_archive, is_document
    (".tar.gz", "tar.gz", "archive", "application/gzip", True, False),
    (".tar.bz2", "tar.bz2", "archive", "application/x-bzip2", True, False),
    (".tar.xz", "tar.xz", "archive", "application/x-xz", True, False),
    (".tar.zst", "tar.zst", "archive", "application/zstd", True, False),
    (".tar.lz", "tar.lz", "archive", "application/x-lzip", True, False),
    (".tar.lzma", "tar.lzma", "archive", "application/x-lzma", True, False),
    (".cpio.gz", "cpio.gz", "archive", "application/gzip", True, False),
)

_EXTENSION_MAP: dict[str, tuple[str, Family, str, bool, bool]] = {
    # archives / packages
    ".zip": ("zip", "archive", "application/zip", True, False),
    ".zipx": ("zipx", "archive", "application/zip", True, False),
    ".jar": ("jar", "archive", "application/java-archive", True, False),
    ".war": ("war", "archive", "application/java-archive", True, False),
    ".ear": ("ear", "archive", "application/java-archive", True, False),
    ".apk": ("apk", "archive", "application/vnd.android.package-archive", True, False),
    ".ipa": ("ipa", "archive", "application/octet-stream", True, False),
    ".whl": ("whl", "archive", "application/zip", True, False),
    ".egg": ("egg", "archive", "application/zip", True, False),
    ".cbz": ("cbz", "archive", "application/vnd.comicbook+zip", True, False),
    ".cbr": ("cbr", "archive", "application/vnd.comicbook-rar", True, False),
    ".epub": ("epub", "document", "application/epub+zip", True, True),
    ".odt": ("odt", "document", "application/vnd.oasis.opendocument.text", True, True),
    ".ods": ("ods", "document", "application/vnd.oasis.opendocument.spreadsheet", True, True),
    ".odp": ("odp", "document", "application/vnd.oasis.opendocument.presentation", True, True),
    ".docx": (
        "docx",
        "document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        True,
        True,
    ),
    ".xlsx": (
        "xlsx",
        "document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        True,
        True,
    ),
    ".xlsm": (
        "xlsm",
        "document",
        "application/vnd.ms-excel.sheet.macroEnabled.12",
        True,
        True,
    ),
    ".pptx": (
        "pptx",
        "document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        True,
        True,
    ),
    ".doc": ("doc", "document", "application/msword", False, True),
    ".xls": ("xls", "document", "application/vnd.ms-excel", False, True),
    ".ppt": ("ppt", "document", "application/vnd.ms-powerpoint", False, True),
    ".rtf": ("rtf", "document", "application/rtf", False, True),
    ".pdf": ("pdf", "document", "application/pdf", False, True),
    ".tar": ("tar", "archive", "application/x-tar", True, False),
    ".tgz": ("tar.gz", "archive", "application/gzip", True, False),
    ".tbz": ("tar.bz2", "archive", "application/x-bzip2", True, False),
    ".tbz2": ("tar.bz2", "archive", "application/x-bzip2", True, False),
    ".txz": ("tar.xz", "archive", "application/x-xz", True, False),
    ".gz": ("gz", "archive", "application/gzip", True, False),
    ".bz2": ("bz2", "archive", "application/x-bzip2", True, False),
    ".xz": ("xz", "archive", "application/x-xz", True, False),
    ".lz": ("lz", "archive", "application/x-lzip", True, False),
    ".lzma": ("lzma", "archive", "application/x-lzma", True, False),
    ".zst": ("zst", "archive", "application/zstd", True, False),
    ".zstd": ("zst", "archive", "application/zstd", True, False),
    ".7z": ("7z", "archive", "application/x-7z-compressed", True, False),
    ".rar": ("rar", "archive", "application/vnd.rar", True, False),
    ".cab": ("cab", "archive", "application/vnd.ms-cab-compressed", True, False),
    ".iso": ("iso", "disk", "application/x-iso9660-image", True, False),
    ".dmg": ("dmg", "disk", "application/x-apple-diskimage", True, False),
    ".img": ("img", "disk", "application/octet-stream", True, False),
    ".vhd": ("vhd", "disk", "application/x-vhd", True, False),
    ".vmdk": ("vmdk", "disk", "application/x-vmdk", True, False),
    ".cpio": ("cpio", "archive", "application/x-cpio", True, False),
    ".ar": ("ar", "archive", "application/x-archive", True, False),
    ".deb": ("deb", "archive", "application/vnd.debian.binary-package", True, False),
    ".rpm": ("rpm", "archive", "application/x-rpm", True, False),
    # text / code / data
    ".txt": ("txt", "text", "text/plain", False, True),
    ".log": ("log", "text", "text/plain", False, True),
    ".md": ("md", "text", "text/markdown", False, True),
    ".markdown": ("md", "text", "text/markdown", False, True),
    ".rst": ("rst", "text", "text/x-rst", False, True),
    ".csv": ("csv", "text", "text/csv", False, True),
    ".tsv": ("tsv", "text", "text/tab-separated-values", False, True),
    ".json": ("json", "text", "application/json", False, True),
    ".jsonl": ("jsonl", "text", "application/x-ndjson", False, True),
    ".xml": ("xml", "text", "application/xml", False, True),
    ".html": ("html", "text", "text/html", False, True),
    ".htm": ("html", "text", "text/html", False, True),
    ".css": ("css", "code", "text/css", False, False),
    ".js": ("js", "code", "text/javascript", False, False),
    ".ts": ("ts", "code", "text/typescript", False, False),
    ".tsx": ("tsx", "code", "text/tsx", False, False),
    ".jsx": ("jsx", "code", "text/jsx", False, False),
    ".py": ("py", "code", "text/x-python", False, False),
    ".rs": ("rs", "code", "text/x-rust", False, False),
    ".go": ("go", "code", "text/x-go", False, False),
    ".java": ("java", "code", "text/x-java-source", False, False),
    ".c": ("c", "code", "text/x-c", False, False),
    ".cpp": ("cpp", "code", "text/x-c++", False, False),
    ".h": ("h", "code", "text/x-c", False, False),
    ".cs": ("cs", "code", "text/x-csharp", False, False),
    ".php": ("php", "code", "application/x-httpd-php", False, False),
    ".rb": ("rb", "code", "text/x-ruby", False, False),
    ".sh": ("sh", "code", "text/x-shellscript", False, False),
    ".ps1": ("ps1", "code", "text/x-powershell", False, False),
    ".bat": ("bat", "code", "application/x-bat", False, False),
    ".cmd": ("cmd", "code", "application/x-bat", False, False),
    ".yaml": ("yaml", "text", "application/yaml", False, True),
    ".yml": ("yaml", "text", "application/yaml", False, True),
    ".toml": ("toml", "text", "application/toml", False, True),
    ".ini": ("ini", "text", "text/plain", False, True),
    ".cfg": ("cfg", "text", "text/plain", False, True),
    ".conf": ("conf", "text", "text/plain", False, True),
    ".sql": ("sql", "code", "application/sql", False, False),
    ".svg": ("svg", "image", "image/svg+xml", False, False),
    # images
    ".png": ("png", "image", "image/png", False, False),
    ".jpg": ("jpg", "image", "image/jpeg", False, False),
    ".jpeg": ("jpg", "image", "image/jpeg", False, False),
    ".gif": ("gif", "image", "image/gif", False, False),
    ".webp": ("webp", "image", "image/webp", False, False),
    ".bmp": ("bmp", "image", "image/bmp", False, False),
    ".tif": ("tiff", "image", "image/tiff", False, False),
    ".tiff": ("tiff", "image", "image/tiff", False, False),
    ".ico": ("ico", "image", "image/x-icon", False, False),
    ".heic": ("heic", "image", "image/heic", False, False),
    # audio / video
    ".mp3": ("mp3", "audio", "audio/mpeg", False, False),
    ".wav": ("wav", "audio", "audio/wav", False, False),
    ".flac": ("flac", "audio", "audio/flac", False, False),
    ".ogg": ("ogg", "audio", "audio/ogg", False, False),
    ".m4a": ("m4a", "audio", "audio/mp4", False, False),
    ".aac": ("aac", "audio", "audio/aac", False, False),
    ".mp4": ("mp4", "video", "video/mp4", False, False),
    ".mkv": ("mkv", "video", "video/x-matroska", False, False),
    ".webm": ("webm", "video", "video/webm", False, False),
    ".avi": ("avi", "video", "video/x-msvideo", False, False),
    ".mov": ("mov", "video", "video/quicktime", False, False),
    ".wmv": ("wmv", "video", "video/x-ms-wmv", False, False),
    # fonts / certs / db / exec
    ".ttf": ("ttf", "font", "font/ttf", False, False),
    ".otf": ("otf", "font", "font/otf", False, False),
    ".woff": ("woff", "font", "font/woff", False, False),
    ".woff2": ("woff2", "font", "font/woff2", False, False),
    ".pem": ("pem", "certificate", "application/x-pem-file", False, False),
    ".crt": ("crt", "certificate", "application/x-x509-ca-cert", False, False),
    ".cer": ("cer", "certificate", "application/pkix-cert", False, False),
    ".der": ("der", "certificate", "application/x-x509-ca-cert", False, False),
    ".sqlite": ("sqlite", "database", "application/vnd.sqlite3", False, False),
    ".db": ("db", "database", "application/octet-stream", False, False),
    ".exe": ("exe", "executable", "application/vnd.microsoft.portable-executable", False, False),
    ".dll": ("dll", "executable", "application/vnd.microsoft.portable-executable", False, False),
    ".so": ("so", "executable", "application/x-sharedlib", False, False),
    ".dylib": ("dylib", "executable", "application/x-mach-binary", False, False),
    ".bin": ("bin", "binary", "application/octet-stream", False, False),
    ".dat": ("dat", "binary", "application/octet-stream", False, False),
}


def archive_kinds() -> list[str]:
    kinds = {
        kind
        for kind, family, _mime, is_arch, _doc in _EXTENSION_MAP.values()
        if is_arch and family in {"archive", "disk"}
    }
    kinds.update(
        {
            "zip",
            "tar",
            "tar.gz",
            "tar.bz2",
            "tar.xz",
            "gz",
            "bz2",
            "xz",
            "7z",
            "rar",
            "cab",
            "iso",
            "cpio",
            "ar",
            "deb",
            "rpm",
            "zst",
        }
    )
    return sorted(kinds)


def document_kinds() -> list[str]:
    kinds = {
        kind for kind, family, _mime, _arch, is_doc in _EXTENSION_MAP.values() if is_doc
    }
    kinds.update(
        {
            "pdf",
            "docx",
            "xlsx",
            "xlsm",
            "pptx",
            "odt",
            "ods",
            "odp",
            "rtf",
            "txt",
            "md",
            "csv",
            "tsv",
            "json",
            "xml",
            "html",
            "epub",
            "doc",
            "xls",
            "ppt",
        }
    )
    return sorted(kinds)


def is_archive_kind(kind: str) -> bool:
    return kind.lower().lstrip(".") in set(archive_kinds()) or kind.lower() in {
        "zip",
        "tar",
        "gzip",
        "bzip2",
        "xz",
        "7z",
        "rar",
    }


def is_document_kind(kind: str) -> bool:
    return kind.lower().lstrip(".") in set(document_kinds())


def identify_path(path: str | Path, *, peek_bytes: int = 8192) -> FileTypeInfo:
    path_obj = Path(path)
    name = path_obj.name
    data = b""
    size = 0
    if path_obj.exists() and path_obj.is_file():
        size = path_obj.stat().st_size
        with path_obj.open("rb") as handle:
            data = handle.read(max(64, min(peek_bytes, 1_048_576)))
    info = identify_bytes(data, name=name)
    details = dict(info.details)
    details["size"] = size
    details["path"] = str(path_obj)
    return FileTypeInfo(
        kind=info.kind,
        family=info.family,
        mime_type=info.mime_type,
        extension=info.extension,
        confidence=info.confidence,
        source=info.source,
        is_archive=info.is_archive,
        is_document=info.is_document,
        is_text=info.is_text,
        is_compressed=info.is_compressed,
        container=info.container,
        description=info.description,
        magic_hex=info.magic_hex,
        details=details,
    )


def identify_bytes(data: bytes, *, name: str = "") -> FileTypeInfo:
    """Identify type from a byte prefix and optional filename."""

    magic_hex = data[:16].hex(" ") if data else ""
    magic = _match_magic(data)
    if magic is not None:
        # Refine ZIP-based office / open document containers.
        if magic.kind == "zip":
            refined = _refine_zip_container(data, name=name)
            if refined is not None:
                return refined
        # Refine gzip that may be tar.gz by name.
        if magic.kind == "gz" and _name_has_suffix(name, (".tar.gz", ".tgz")):
            return FileTypeInfo(
                kind="tar.gz",
                family="archive",
                mime_type="application/gzip",
                extension=_extension_of(name) or ".tar.gz",
                confidence=0.92,
                source="magic+extension",
                is_archive=True,
                is_compressed=True,
                container="tar",
                description="gzip-compressed tar archive",
                magic_hex=magic_hex,
            )
        if magic.kind == "bz2" and _name_has_suffix(name, (".tar.bz2", ".tbz2", ".tbz")):
            return FileTypeInfo(
                kind="tar.bz2",
                family="archive",
                mime_type="application/x-bzip2",
                extension=_extension_of(name) or ".tar.bz2",
                confidence=0.92,
                source="magic+extension",
                is_archive=True,
                is_compressed=True,
                container="tar",
                description="bzip2-compressed tar archive",
                magic_hex=magic_hex,
            )
        if magic.kind == "xz" and _name_has_suffix(name, (".tar.xz", ".txz")):
            return FileTypeInfo(
                kind="tar.xz",
                family="archive",
                mime_type="application/x-xz",
                extension=_extension_of(name) or ".tar.xz",
                confidence=0.92,
                source="magic+extension",
                is_archive=True,
                is_compressed=True,
                container="tar",
                description="xz-compressed tar archive",
                magic_hex=magic_hex,
            )
        return FileTypeInfo(
            kind=magic.kind,
            family=magic.family,
            mime_type=magic.mime_type,
            extension=_extension_of(name) or magic.default_ext,
            confidence=magic.confidence,
            source="magic",
            is_archive=magic.is_archive,
            is_document=magic.is_document,
            is_text=magic.is_text,
            is_compressed=magic.is_compressed,
            container=magic.container,
            description=magic.description,
            magic_hex=magic_hex,
            details=dict(magic.details),
        )

    ext_info = _match_extension(name)
    if ext_info is not None:
        return FileTypeInfo(
            kind=ext_info[0],
            family=ext_info[1],
            mime_type=ext_info[2],
            extension=_extension_of(name),
            confidence=0.72,
            source="extension",
            is_archive=ext_info[3],
            is_document=ext_info[4],
            is_text=ext_info[1] in {"text", "code"} or ext_info[4],
            is_compressed=ext_info[0]
            in {"gz", "bz2", "xz", "lz", "lzma", "zst", "7z", "rar", "tar.gz", "tar.bz2", "tar.xz"},
            description=f"{ext_info[0]} by extension",
            magic_hex=magic_hex,
        )

    # Content heuristics for text-like payloads without extension.
    if data and _looks_like_text(data):
        text_kind, mime = _sniff_text_kind(data)
        return FileTypeInfo(
            kind=text_kind,
            family="text" if text_kind not in {"js", "py", "css"} else "code",
            mime_type=mime,
            extension=_extension_of(name),
            confidence=0.55,
            source="content",
            is_document=text_kind in {"txt", "md", "html", "xml", "json", "csv"},
            is_text=True,
            description="text-like content",
            magic_hex=magic_hex,
        )

    guessed_mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return FileTypeInfo(
        kind="bin" if data else "empty",
        family="binary" if data else "unknown",
        mime_type=guessed_mime,
        extension=_extension_of(name),
        confidence=0.2 if data else 0.1,
        source="unknown",
        description="unrecognized binary" if data else "empty file",
        magic_hex=magic_hex,
    )


@dataclass(frozen=True)
class _MagicHit:
    kind: str
    family: Family
    mime_type: str
    default_ext: str
    confidence: float
    is_archive: bool = False
    is_document: bool = False
    is_text: bool = False
    is_compressed: bool = False
    container: str | None = None
    description: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def _match_magic(data: bytes) -> _MagicHit | None:
    if not data:
        return None
    # PDF
    if data.startswith(b"%PDF"):
        return _MagicHit("pdf", "document", "application/pdf", ".pdf", 0.99, is_document=True, description="PDF document")
    # ZIP family
    if data[:4] in {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}:
        return _MagicHit(
            "zip",
            "archive",
            "application/zip",
            ".zip",
            0.95,
            is_archive=True,
            container="zip",
            description="ZIP container",
        )
    # GZIP
    if data.startswith(b"\x1f\x8b"):
        return _MagicHit(
            "gz",
            "archive",
            "application/gzip",
            ".gz",
            0.97,
            is_archive=True,
            is_compressed=True,
            container="gzip",
            description="gzip compressed stream",
        )
    # BZIP2
    if data.startswith(b"BZh"):
        return _MagicHit(
            "bz2",
            "archive",
            "application/x-bzip2",
            ".bz2",
            0.97,
            is_archive=True,
            is_compressed=True,
            container="bzip2",
            description="bzip2 compressed stream",
        )
    # XZ
    if data.startswith(b"\xfd7zXZ\x00"):
        return _MagicHit(
            "xz",
            "archive",
            "application/x-xz",
            ".xz",
            0.98,
            is_archive=True,
            is_compressed=True,
            container="xz",
            description="xz compressed stream",
        )
    # Zstd
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        return _MagicHit(
            "zst",
            "archive",
            "application/zstd",
            ".zst",
            0.97,
            is_archive=True,
            is_compressed=True,
            container="zstd",
            description="zstd compressed stream",
        )
    # 7z
    if data.startswith(b"7z\xbc\xaf'\x1c"):
        return _MagicHit(
            "7z",
            "archive",
            "application/x-7z-compressed",
            ".7z",
            0.99,
            is_archive=True,
            is_compressed=True,
            container="7z",
            description="7-Zip archive",
        )
    # RAR
    if data.startswith(b"Rar!\x1a\x07\x00") or data.startswith(b"Rar!\x1a\x07\x01\x00"):
        return _MagicHit(
            "rar",
            "archive",
            "application/vnd.rar",
            ".rar",
            0.99,
            is_archive=True,
            is_compressed=True,
            container="rar",
            description="RAR archive",
        )
    # CAB
    if data.startswith(b"MSCF"):
        return _MagicHit(
            "cab",
            "archive",
            "application/vnd.ms-cab-compressed",
            ".cab",
            0.96,
            is_archive=True,
            container="cab",
            description="Microsoft Cabinet archive",
        )
    # TAR ustar
    if len(data) >= 262 and data[257:262] == b"ustar":
        return _MagicHit(
            "tar",
            "archive",
            "application/x-tar",
            ".tar",
            0.96,
            is_archive=True,
            container="tar",
            description="POSIX tar archive",
        )
    # OLE compound (legacy Office)
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return _MagicHit(
            "ole",
            "document",
            "application/x-ole-storage",
            ".doc",
            0.9,
            is_document=True,
            container="ole",
            description="OLE compound document (legacy Office)",
        )
    # RTF
    if data.startswith(b"{\\rtf"):
        return _MagicHit(
            "rtf",
            "document",
            "application/rtf",
            ".rtf",
            0.98,
            is_document=True,
            is_text=True,
            description="Rich Text Format",
        )
    # PNG
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _MagicHit("png", "image", "image/png", ".png", 0.99, description="PNG image")
    # JPEG
    if data.startswith(b"\xff\xd8\xff"):
        return _MagicHit("jpg", "image", "image/jpeg", ".jpg", 0.99, description="JPEG image")
    # GIF
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return _MagicHit("gif", "image", "image/gif", ".gif", 0.99, description="GIF image")
    # WEBP
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _MagicHit("webp", "image", "image/webp", ".webp", 0.98, description="WebP image")
    # BMP
    if data.startswith(b"BM"):
        return _MagicHit("bmp", "image", "image/bmp", ".bmp", 0.95, description="BMP image")
    # TIFF
    if data.startswith(b"II*\x00") or data.startswith(b"MM\x00*"):
        return _MagicHit("tiff", "image", "image/tiff", ".tiff", 0.97, description="TIFF image")
    # WAV
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return _MagicHit("wav", "audio", "audio/wav", ".wav", 0.98, description="WAV audio")
    # AVI
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return _MagicHit("avi", "video", "video/x-msvideo", ".avi", 0.97, description="AVI video")
    # MP3 ID3 or frame sync
    if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        if data.startswith(b"ID3"):
            return _MagicHit("mp3", "audio", "audio/mpeg", ".mp3", 0.9, description="MP3 audio")
    # FLAC
    if data.startswith(b"fLaC"):
        return _MagicHit("flac", "audio", "audio/flac", ".flac", 0.99, description="FLAC audio")
    # OGG
    if data.startswith(b"OggS"):
        return _MagicHit("ogg", "audio", "audio/ogg", ".ogg", 0.95, description="Ogg container")
    # Matroska / WebM
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return _MagicHit("mkv", "video", "video/x-matroska", ".mkv", 0.9, description="Matroska/WebM container")
    # ISO9660
    if len(data) >= 0x8006 and data[0x8001:0x8006] == b"CD001":
        return _MagicHit(
            "iso",
            "disk",
            "application/x-iso9660-image",
            ".iso",
            0.95,
            is_archive=True,
            container="iso",
            description="ISO 9660 disk image",
        )
    # ELF
    if data.startswith(b"\x7fELF"):
        return _MagicHit("elf", "executable", "application/x-executable", "", 0.99, description="ELF binary")
    # PE / MZ
    if data.startswith(b"MZ"):
        return _MagicHit(
            "exe",
            "executable",
            "application/vnd.microsoft.portable-executable",
            ".exe",
            0.85,
            description="DOS/PE executable",
        )
    # Mach-O
    if data[:4] in {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"}:
        return _MagicHit("macho", "executable", "application/x-mach-binary", "", 0.97, description="Mach-O binary")
    # SQLite
    if data.startswith(b"SQLite format 3\x00"):
        return _MagicHit(
            "sqlite",
            "database",
            "application/vnd.sqlite3",
            ".sqlite",
            0.99,
            description="SQLite database",
        )
    # RPM
    if data.startswith(b"\xed\xab\xee\xdb"):
        return _MagicHit(
            "rpm",
            "archive",
            "application/x-rpm",
            ".rpm",
            0.98,
            is_archive=True,
            container="rpm",
            description="RPM package",
        )
    # Debian ar (.deb starts with !<arch>)
    if data.startswith(b"!<arch>\n"):
        return _MagicHit(
            "ar",
            "archive",
            "application/x-archive",
            ".ar",
            0.95,
            is_archive=True,
            container="ar",
            description="Unix ar archive (often .deb)",
        )
    # WASM
    if data.startswith(b"\x00asm"):
        return _MagicHit("wasm", "executable", "application/wasm", ".wasm", 0.99, description="WebAssembly module")
    # UTF-8/UTF-16 BOM text
    if data.startswith(b"\xef\xbb\xbf") or data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return _MagicHit(
            "txt",
            "text",
            "text/plain",
            ".txt",
            0.8,
            is_text=True,
            is_document=True,
            description="text with BOM",
        )
    return None


def _refine_zip_container(data: bytes, *, name: str) -> FileTypeInfo | None:
    """Best-effort classification of ZIP-based formats from name + local headers."""

    lower = name.lower()
    mapping = (
        (".docx", "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", True),
        (".xlsx", "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", True),
        (".xlsm", "xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12", True),
        (".pptx", "pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation", True),
        (".odt", "odt", "application/vnd.oasis.opendocument.text", True),
        (".ods", "ods", "application/vnd.oasis.opendocument.spreadsheet", True),
        (".odp", "odp", "application/vnd.oasis.opendocument.presentation", True),
        (".epub", "epub", "application/epub+zip", True),
        (".jar", "jar", "application/java-archive", False),
        (".apk", "apk", "application/vnd.android.package-archive", False),
        (".whl", "whl", "application/zip", False),
        (".cbz", "cbz", "application/vnd.comicbook+zip", False),
    )
    for suffix, kind, mime, is_doc in mapping:
        if lower.endswith(suffix):
            return FileTypeInfo(
                kind=kind,
                family="document" if is_doc else "archive",
                mime_type=mime,
                extension=suffix,
                confidence=0.97,
                source="magic+extension",
                is_archive=True,
                is_document=is_doc,
                container="zip",
                description=f"ZIP-based {kind}",
                magic_hex=data[:16].hex(" "),
            )
    # Peek local file names inside the first LOCs when available.
    names = _zip_local_names(data)
    joined = " ".join(names).lower()
    if "word/document.xml" in joined:
        kind, mime, is_doc = "docx", mapping[0][2], True
    elif "xl/workbook.xml" in joined:
        kind, mime, is_doc = "xlsx", mapping[1][2], True
    elif "ppt/slides/" in joined:
        kind, mime, is_doc = "pptx", mapping[3][2], True
    elif "mimetype" in joined and "opendocument.text" in joined:
        kind, mime, is_doc = "odt", mapping[4][2], True
    elif "meta-inf/container.xml" in joined:
        kind, mime, is_doc = "epub", mapping[7][2], True
    else:
        return None
    return FileTypeInfo(
        kind=kind,
        family="document" if is_doc else "archive",
        mime_type=mime,
        extension=_extension_of(name) or f".{kind}",
        confidence=0.93,
        source="magic+content",
        is_archive=True,
        is_document=is_doc,
        container="zip",
        description=f"ZIP-based {kind} (content signature)",
        magic_hex=data[:16].hex(" "),
        details={"zip_names_sample": names[:12]},
    )


def _zip_local_names(data: bytes, *, limit: int = 24) -> list[str]:
    names: list[str] = []
    offset = 0
    while offset + 30 <= len(data) and len(names) < limit:
        if data[offset : offset + 4] != b"PK\x03\x04":
            break
        name_len = struct.unpack_from("<H", data, offset + 26)[0]
        extra_len = struct.unpack_from("<H", data, offset + 28)[0]
        start = offset + 30
        end = start + name_len
        if end > len(data):
            break
        try:
            names.append(data[start:end].decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            break
        # compressed size may be zero with data descriptor; stop early if unsure
        comp_size = struct.unpack_from("<I", data, offset + 18)[0]
        offset = end + extra_len + comp_size
        if comp_size == 0 and len(names) >= 1:
            break
    return names


def _match_extension(name: str) -> tuple[str, Family, str, bool, bool] | None:
    lower = name.lower()
    for suffix, kind, family, mime, is_arch, is_doc in _COMPOUND_EXTENSIONS:
        if lower.endswith(suffix):
            return kind, family, mime, is_arch, is_doc
    # double suffix fallback: .tar.gz already covered; handle .csv.gz etc.
    path = Path(name)
    suffixes = "".join(path.suffixes).lower()
    if suffixes in _EXTENSION_MAP:
        return _EXTENSION_MAP[suffixes]
    for compound, kind, family, mime, is_arch, is_doc in _COMPOUND_EXTENSIONS:
        if suffixes.endswith(compound):
            return kind, family, mime, is_arch, is_doc
    ext = path.suffix.lower()
    if ext in _EXTENSION_MAP:
        return _EXTENSION_MAP[ext]
    return None


def _extension_of(name: str) -> str:
    lower = name.lower()
    for suffix, *_rest in _COMPOUND_EXTENSIONS:
        if lower.endswith(suffix):
            return suffix
    suffixes = "".join(Path(name).suffixes).lower()
    if len(Path(name).suffixes) >= 2 and suffixes:
        # prefer full multi-suffix for tar.* style
        if suffixes.count(".") >= 2:
            return suffixes if len(suffixes) <= 16 else Path(name).suffix.lower()
    return Path(name).suffix.lower()


def _name_has_suffix(name: str, suffixes: tuple[str, ...]) -> bool:
    lower = name.lower()
    return any(lower.endswith(item) for item in suffixes)


def _looks_like_text(data: bytes) -> bool:
    sample = data[:4096]
    if not sample:
        return False
    if b"\x00" in sample:
        return False
    # high ratio of printable / whitespace
    printable = sum(1 for b in sample if 32 <= b <= 126 or b in {9, 10, 13})
    return printable / max(1, len(sample)) >= 0.85


def _sniff_text_kind(data: bytes) -> tuple[str, str]:
    head = data[:2048].lstrip().lower()
    try:
        text = data[:4096].decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        text = ""
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head[:200]:
        return "html", "text/html"
    if head.startswith(b"<?xml") or head.startswith(b"<svg"):
        return "xml", "application/xml"
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return "json", "application/json"
    if re.search(r"^#{1,6}\s+\S", text, re.M) or ("```" in text and re.search(r"^#\s+\w", text, re.M)):
        return "md", "text/markdown"
    if text.count(",") >= 3 and "\n" in text:
        return "csv", "text/csv"
    return "txt", "text/plain"
