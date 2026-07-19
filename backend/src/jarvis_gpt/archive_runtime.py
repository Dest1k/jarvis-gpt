"""Safe archive listing, extraction, creation, and member reading.

Supports common formats with stdlib first; optional engines and 7-Zip CLI
degrade gracefully:

- ZIP family (zip, jar, war, wheel, …) via ``zipfile``
- TAR family (tar, tar.gz/tgz, tar.bz2, tar.xz, tar.zst) via ``tarfile``
- Single-stream compress (gz, bz2, xz, zst) via stdlib / optional zstandard
- 7z via optional ``py7zr`` or 7-Zip CLI
- RAR via optional ``rarfile`` or 7-Zip CLI
- DEB via native Unix ``ar`` + nested tar (or 7-Zip CLI)
- ISO / IMG / RPM / SquashFS via 7-Zip CLI (ISO also has a pure ISO9660 reader)
- Password-protected zip / 7z / rar (and other formats 7-Zip can open)

Safety policy:
- reject path traversal and absolute member paths
- cap member count, per-member size, and total uncompressed bytes
- never extract into source path; write only under an explicit output dir
- never log archive passwords; 7z runs non-interactively (stdin closed)
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import io
import lzma
import os
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from .file_types import FileTypeInfo, identify_bytes, identify_path, is_archive_kind

__all__ = [
    "ArchiveError",
    "ArchiveSafetyError",
    "ArchiveUnsupportedError",
    "ArchivePasswordError",
    "ArchiveConfig",
    "list_archive",
    "extract_archive",
    "read_archive_member",
    "create_archive",
    "archive_capabilities",
    "is_archive_path",
]

_SEVENZIP_KINDS = frozenset(
    {
        "7z",
        "rar",
        "iso",
        "img",
        "squashfs",
        "rpm",
        "cab",
        "tar.zst",
        "zst",
        "deb",
        "ar",
        "cpio",
    }
)


class ArchiveError(RuntimeError):
    """Base archive error."""


class ArchiveSafetyError(ArchiveError):
    """Path traversal, bomb, or policy violation."""


class ArchiveUnsupportedError(ArchiveError):
    """Format not supported in this runtime."""


class ArchivePasswordError(ArchiveError):
    """Archive is encrypted and the password is missing or wrong."""


@dataclass
class ArchiveConfig:
    max_members: int = 5_000
    max_member_bytes: int = 50_000_000
    max_total_uncompressed_bytes: int = 200_000_000
    max_list_members: int = 500
    allow_symlinks: bool = False
    password: str | None = None


def archive_capabilities() -> dict[str, Any]:
    py7zr_ok = _module_available("py7zr")
    rarfile_ok = _module_available("rarfile")
    zstd_ok = _module_available("zstandard")
    seven = _find_7z()
    return {
        "stdlib": {
            "zip": True,
            "tar": True,
            "tar.gz": True,
            "tar.bz2": True,
            "tar.xz": True,
            "gz": True,
            "bz2": True,
            "xz": True,
            "deb": True,
            "ar": True,
            "iso": True,
        },
        "optional": {
            "7z": py7zr_ok or seven is not None,
            "rar": rarfile_ok or seven is not None,
            "tar.zst": zstd_ok or seven is not None,
            "zst": zstd_ok or seven is not None,
            "iso": True,
            "img": seven is not None,
            "rpm": seven is not None,
            "squashfs": seven is not None,
            "engine_7z": "py7zr" if py7zr_ok else ("7z-cli" if seven else None),
            "engine_rar": "rarfile" if rarfile_ok else ("7z-cli" if seven else None),
            "engine_zstd": "zstandard" if zstd_ok else ("7z-cli" if seven else None),
            "engine_cli": str(seven) if seven else None,
        },
        "create": ["zip", "tar", "tar.gz", "tar.bz2", "tar.xz", "gz"],
        "password": ["zip", "7z", "rar"],
        "limits_default": {
            "max_members": ArchiveConfig().max_members,
            "max_member_bytes": ArchiveConfig().max_member_bytes,
            "max_total_uncompressed_bytes": ArchiveConfig().max_total_uncompressed_bytes,
        },
    }


def is_archive_path(path: str | Path) -> bool:
    info = identify_path(path)
    # Office/OOXML are zip containers but treated as documents, not generic archives.
    if info.is_document and info.kind in {
        "docx",
        "xlsx",
        "xlsm",
        "pptx",
        "odt",
        "ods",
        "odp",
        "epub",
    }:
        return False
    return bool(info.is_archive) or is_archive_kind(info.kind)


def list_archive(
    path: str | Path,
    *,
    config: ArchiveConfig | None = None,
    prefix: str = "",
    password: str | None = None,
) -> dict[str, Any]:
    cfg = _with_password(config, password)
    path_obj = Path(path).resolve(strict=False)
    _assert_file(path_obj)
    info = identify_path(path_obj)
    kind = _archive_kind(path_obj, info)
    members = _list_members(path_obj, kind, cfg)
    if prefix:
        members = [m for m in members if str(m.get("name") or "").startswith(prefix)]
    truncated = len(members) > cfg.max_list_members
    members = members[: cfg.max_list_members]
    total_size = sum(int(m.get("size") or 0) for m in members)
    return {
        "ok": True,
        "path": str(path_obj),
        "name": path_obj.name,
        "kind": kind,
        "type": info.to_dict(),
        "member_count": len(members),
        "listed_count": len(members),
        "truncated": truncated,
        "total_listed_bytes": total_size,
        "members": members,
        "markdown": _list_markdown(path_obj.name, kind, members, truncated),
    }


def extract_archive(
    path: str | Path,
    *,
    output_dir: str | Path,
    members: Sequence[str] | None = None,
    config: ArchiveConfig | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    cfg = _with_password(config, password)
    path_obj = Path(path).resolve(strict=False)
    _assert_file(path_obj)
    dest_root = Path(output_dir).resolve(strict=False)
    dest_root.mkdir(parents=True, exist_ok=True)
    info = identify_path(path_obj)
    kind = _archive_kind(path_obj, info)
    wanted = {str(item) for item in (members or []) if str(item).strip()} or None
    extracted = _extract_members(path_obj, kind, dest_root, wanted=wanted, cfg=cfg)
    return {
        "ok": True,
        "path": str(path_obj),
        "kind": kind,
        "output_dir": str(dest_root),
        "extracted_count": len(extracted),
        "extracted": extracted,
        "markdown": (
            f"# Extracted {path_obj.name}\n\n"
            f"- kind: `{kind}`\n"
            f"- members: {len(extracted)}\n"
            f"- output: `{dest_root}`\n"
        ),
    }


def read_archive_member(
    path: str | Path,
    member: str,
    *,
    max_bytes: int | None = None,
    config: ArchiveConfig | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    cfg = _with_password(config, password)
    path_obj = Path(path).resolve(strict=False)
    _assert_file(path_obj)
    member_name = _safe_member_name(member)
    limit = max_bytes if max_bytes is not None else cfg.max_member_bytes
    limit = max(1, min(int(limit), cfg.max_member_bytes))
    info = identify_path(path_obj)
    kind = _archive_kind(path_obj, info)
    data = _read_member_bytes(path_obj, kind, member_name, limit=limit, cfg=cfg)
    member_type = identify_bytes(data, name=member_name)
    text_preview = ""
    if member_type.is_text or member_type.is_document or member_type.family in {"text", "code"}:
        text_preview = data.decode("utf-8", errors="replace")[:4000]
    return {
        "ok": True,
        "archive": str(path_obj),
        "kind": kind,
        "member": member_name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "type": member_type.to_dict(),
        "text_preview": text_preview,
        "truncated": len(data) >= limit,
    }


def create_archive(
    paths: Sequence[str | Path],
    *,
    output_path: str | Path,
    archive_format: str = "zip",
    config: ArchiveConfig | None = None,
) -> dict[str, Any]:
    cfg = config or ArchiveConfig()
    fmt = str(archive_format or "zip").strip().lower().lstrip(".")
    if fmt in {"tgz"}:
        fmt = "tar.gz"
    if fmt in {"tbz", "tbz2"}:
        fmt = "tar.bz2"
    if fmt in {"txz"}:
        fmt = "tar.xz"
    sources = [_assert_file(Path(p).resolve(strict=False)) for p in list(paths)[: cfg.max_members]]
    if not sources:
        raise ArchiveError("create_archive requires at least one source file")
    destination = Path(output_path).resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ArchiveSafetyError(f"Refusing to overwrite existing archive: {destination}")

    if fmt == "zip":
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source in sources:
                archive.write(source, arcname=source.name)
    elif fmt == "tar":
        with tarfile.open(destination, "w") as archive:
            for source in sources:
                archive.add(source, arcname=source.name)
    elif fmt == "tar.gz":
        with tarfile.open(destination, "w:gz") as archive:
            for source in sources:
                archive.add(source, arcname=source.name)
    elif fmt == "tar.bz2":
        with tarfile.open(destination, "w:bz2") as archive:
            for source in sources:
                archive.add(source, arcname=source.name)
    elif fmt == "tar.xz":
        with tarfile.open(destination, "w:xz") as archive:
            for source in sources:
                archive.add(source, arcname=source.name)
    elif fmt == "gz":
        if len(sources) != 1:
            raise ArchiveError("gz creation accepts exactly one source file")
        with sources[0].open("rb") as src, gzip.open(destination, "wb") as out:
            shutil.copyfileobj(src, out)
    else:
        raise ArchiveUnsupportedError(
            f"Cannot create archive format '{fmt}'. "
            f"Supported create formats: zip, tar, tar.gz, tar.bz2, tar.xz, gz"
        )

    return {
        "ok": True,
        "path": str(destination),
        "kind": fmt,
        "size": destination.stat().st_size,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "members": [source.name for source in sources],
        "member_count": len(sources),
        "markdown": (
            f"# Created {fmt} archive\n\n"
            f"- path: `{destination}`\n"
            f"- members: {len(sources)}\n"
            f"- size: {destination.stat().st_size} bytes\n"
        ),
    }


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def _with_password(config: ArchiveConfig | None, password: str | None) -> ArchiveConfig:
    cfg = config or ArchiveConfig()
    if password is None:
        return cfg
    return ArchiveConfig(
        max_members=cfg.max_members,
        max_member_bytes=cfg.max_member_bytes,
        max_total_uncompressed_bytes=cfg.max_total_uncompressed_bytes,
        max_list_members=cfg.max_list_members,
        allow_symlinks=cfg.allow_symlinks,
        password=password,
    )


def _assert_file(path: Path) -> Path:
    if not path.exists() or not path.is_file():
        raise ArchiveError(f"Archive does not exist: {path}")
    return path


def _archive_kind(path: Path, info: FileTypeInfo | None = None) -> str:
    info = info or identify_path(path)
    kind = info.kind
    if kind in {
        "zip",
        "jar",
        "war",
        "ear",
        "apk",
        "ipa",
        "whl",
        "egg",
        "cbz",
        "zipx",
    }:
        return "zip"
    if kind in {"tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst", "tgz"}:
        return kind if kind != "tgz" else "tar.gz"
    if kind in {
        "gz",
        "bz2",
        "xz",
        "zst",
        "7z",
        "rar",
        "cab",
        "iso",
        "img",
        "ar",
        "deb",
        "rpm",
        "cpio",
        "squashfs",
    }:
        return kind
    name = path.name.lower()
    if name.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if name.endswith((".tar.bz2", ".tbz2", ".tbz")):
        return "tar.bz2"
    if name.endswith((".tar.xz", ".txz")):
        return "tar.xz"
    if name.endswith((".tar.zst", ".tzst")):
        return "tar.zst"
    if name.endswith(".tar"):
        return "tar"
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".gz"):
        return "gz"
    if name.endswith(".bz2"):
        return "bz2"
    if name.endswith(".xz"):
        return "xz"
    if name.endswith((".zst", ".zstd")):
        return "zst"
    if name.endswith(".7z"):
        return "7z"
    if name.endswith(".rar"):
        return "rar"
    if name.endswith(".iso"):
        return "iso"
    if name.endswith(".img"):
        return "img"
    if name.endswith(".deb"):
        return "deb"
    if name.endswith(".rpm"):
        return "rpm"
    if name.endswith((".squashfs", ".sqfs", ".sfs")):
        return "squashfs"
    if info.is_archive:
        return kind
    raise ArchiveUnsupportedError(f"Not a supported archive: {path.name} ({kind})")


def _safe_member_name(name: str) -> str:
    raw = str(name or "").replace("\\", "/").strip()
    if not raw or raw.endswith("/"):
        raise ArchiveSafetyError(f"Invalid archive member name: {name!r}")
    if raw.startswith("/") or raw.startswith("~") or re_abs_windows(raw):
        raise ArchiveSafetyError(f"Absolute member paths are not allowed: {name}")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ArchiveSafetyError(f"Path traversal is not allowed: {name}")
    cleaned = "/".join(parts)
    if not cleaned:
        raise ArchiveSafetyError(f"Invalid archive member name: {name!r}")
    return cleaned


def re_abs_windows(value: str) -> bool:
    return len(value) >= 3 and value[1] == ":" and value[0].isalpha()


def _safe_destination(root: Path, member: str) -> Path:
    safe = _safe_member_name(member)
    dest = (root / safe).resolve(strict=False)
    try:
        dest.relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise ArchiveSafetyError(f"Member escapes output directory: {member}") from exc
    return dest


def _list_members(path: Path, kind: str, cfg: ArchiveConfig) -> list[dict[str, Any]]:
    if kind == "zip":
        return _list_zip(path, cfg)
    if kind == "tar.zst":
        try:
            return _list_tar(path, kind, cfg)
        except ArchiveUnsupportedError:
            return _list_via_7z(path, cfg)
        except Exception:
            if _find_7z() is not None:
                return _list_via_7z(path, cfg)
            raise
    if kind.startswith("tar"):
        return _list_tar(path, kind, cfg)
    if kind in {"gz", "bz2", "xz", "zst"}:
        return _list_stream(path, kind)
    if kind == "7z":
        if _module_available("py7zr"):
            try:
                return _list_7z(path, cfg)
            except ArchivePasswordError:
                raise
            except Exception:
                if _find_7z() is not None:
                    return _list_via_7z(path, cfg)
                raise
        return _list_via_7z(path, cfg)
    if kind == "rar":
        if _module_available("rarfile"):
            try:
                return _list_rar(path, cfg)
            except ArchivePasswordError:
                raise
            except Exception:
                if _find_7z() is not None:
                    return _list_via_7z(path, cfg)
                raise
        return _list_via_7z(path, cfg)
    if kind in {"deb", "ar"}:
        try:
            return _list_deb_or_ar(path, kind, cfg)
        except Exception:
            if _find_7z() is not None:
                return _list_via_7z(path, cfg)
            raise
    if kind == "iso":
        try:
            return _list_iso(path, cfg)
        except Exception:
            if _find_7z() is not None:
                return _list_via_7z(path, cfg)
            raise
    if kind in _SEVENZIP_KINDS:
        return _list_via_7z(path, cfg)
    raise ArchiveUnsupportedError(
        f"Listing not implemented for '{kind}'. "
        f"Install 7-Zip CLI or optional engines (py7zr/rarfile/zstandard) if needed."
    )


def _pwd_bytes(cfg: ArchiveConfig) -> bytes | None:
    if not cfg.password:
        return None
    return str(cfg.password).encode("utf-8")


def _list_zip(path: Path, cfg: ArchiveConfig) -> list[dict[str, Any]]:
    if not zipfile.is_zipfile(path):
        raise ArchiveError(f"Not a valid ZIP archive: {path}")
    members: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        pwd = _pwd_bytes(cfg)
        if pwd is not None:
            archive.setpassword(pwd)
        infos = archive.infolist()
        if len(infos) > cfg.max_members:
            raise ArchiveSafetyError(
                f"Archive has too many members ({len(infos)} > {cfg.max_members})"
            )
        encrypted = any(bool(info.flag_bits & 0x1) for info in infos)
        if encrypted and pwd is None:
            raise ArchivePasswordError(
                f"ZIP archive is password-protected: {path.name}. "
                "Передайте password для list/extract."
            )
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                members.append(
                    {
                        "name": name.rstrip("/"),
                        "is_dir": True,
                        "size": 0,
                        "compressed_size": info.compress_size,
                        "encrypted": bool(info.flag_bits & 0x1),
                    }
                )
                continue
            try:
                safe = _safe_member_name(name)
            except ArchiveSafetyError:
                members.append(
                    {
                        "name": name,
                        "is_dir": False,
                        "size": info.file_size,
                        "compressed_size": info.compress_size,
                        "unsafe": True,
                        "encrypted": bool(info.flag_bits & 0x1),
                    }
                )
                continue
            members.append(
                {
                    "name": safe,
                    "is_dir": False,
                    "size": info.file_size,
                    "compressed_size": info.compress_size,
                    "crc": info.CRC,
                    "encrypted": bool(info.flag_bits & 0x1),
                }
            )
    return members


def _tar_mode(kind: str) -> str:
    return {
        "tar": "r:",
        "tar.gz": "r:gz",
        "tar.bz2": "r:bz2",
        "tar.xz": "r:xz",
        "tar.zst": "r|",
    }.get(kind, "r:*")


def _open_tar(path: Path, kind: str):
    if kind == "tar.zst":
        return _open_tar_zst(path)
    mode = _tar_mode(kind)
    return tarfile.open(path, mode)


def _open_tar_zst(path: Path):
    try:
        import zstandard as zstd  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveUnsupportedError(
            "tar.zst requires optional dependency zstandard or 7-Zip CLI"
        ) from exc
    raw = path.open("rb")
    reader = zstd.ZstdDecompressor().stream_reader(raw)
    # Streaming tar; caller must close both.
    archive = tarfile.open(fileobj=reader, mode="r|")
    archive._jarvis_zstd_raw = raw  # type: ignore[attr-defined]
    archive._jarvis_zstd_reader = reader  # type: ignore[attr-defined]
    return archive


def _close_tar(archive: Any) -> None:
    try:
        archive.close()
    finally:
        reader = getattr(archive, "_jarvis_zstd_reader", None)
        raw = getattr(archive, "_jarvis_zstd_raw", None)
        if reader is not None:
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass
        if raw is not None:
            try:
                raw.close()
            except Exception:  # noqa: BLE001
                pass


def _list_tar(path: Path, kind: str, cfg: ArchiveConfig) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    archive = _open_tar(path, kind)
    try:
        # Streaming tar has no getmembers(); iterate.
        count = 0
        for info in archive:
            count += 1
            if count > cfg.max_members:
                raise ArchiveSafetyError(
                    f"Archive has too many members ({count} > {cfg.max_members})"
                )
            name = info.name.replace("\\", "/")
            if (info.issym() or info.islnk()) and not cfg.allow_symlinks:
                members.append(
                    {"name": name, "is_dir": False, "size": 0, "symlink": True, "skipped": True}
                )
                continue
            if info.isdir():
                members.append({"name": name.rstrip("/"), "is_dir": True, "size": 0})
                continue
            try:
                safe = _safe_member_name(name)
            except ArchiveSafetyError:
                members.append({"name": name, "is_dir": False, "size": info.size, "unsafe": True})
                continue
            members.append({"name": safe, "is_dir": False, "size": int(info.size or 0)})
    finally:
        _close_tar(archive)
    return members


def _list_stream(path: Path, kind: str) -> list[dict[str, Any]]:
    name = path.name
    for suffix in (".gz", ".bz2", ".xz", ".zst", ".zstd"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    size = path.stat().st_size
    return [
        {
            "name": name or "payload",
            "is_dir": False,
            "size": None,
            "compressed_size": size,
            "stream": kind,
        }
    ]


def _list_7z(path: Path, cfg: ArchiveConfig) -> list[dict[str, Any]]:
    try:
        import py7zr  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveUnsupportedError(
            "7z listing requires optional dependency py7zr (pip install py7zr)"
        ) from exc
    members: list[dict[str, Any]] = []
    password = cfg.password or None
    try:
        archive_cm = py7zr.SevenZipFile(path, mode="r", password=password)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "password" in msg or "encrypted" in msg:
            raise ArchivePasswordError(
                f"7z архив защищён паролем: {path.name}. Передайте password."
            ) from exc
        raise
    with archive_cm as archive:
        if getattr(archive, "needs_password", lambda: False)() and not password:
            raise ArchivePasswordError(
                f"7z архив защищён паролем: {path.name}. Передайте password."
            )
        infos = archive.list()
        if len(infos) > cfg.max_members:
            raise ArchiveSafetyError(
                f"Archive has too many members ({len(infos)} > {cfg.max_members})"
            )
        for info in infos:
            name = str(getattr(info, "filename", "") or "").replace("\\", "/")
            is_dir = bool(getattr(info, "is_directory", False))
            size = int(getattr(info, "uncompressed", 0) or 0)
            if is_dir:
                members.append({"name": name.rstrip("/"), "is_dir": True, "size": 0})
                continue
            try:
                safe = _safe_member_name(name)
            except ArchiveSafetyError:
                members.append({"name": name, "is_dir": False, "size": size, "unsafe": True})
                continue
            members.append({"name": safe, "is_dir": False, "size": size})
    return members


def _list_rar(path: Path, cfg: ArchiveConfig) -> list[dict[str, Any]]:
    try:
        import rarfile  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveUnsupportedError(
            "RAR listing requires optional dependency rarfile (and unrar backend)"
        ) from exc
    members: list[dict[str, Any]] = []
    with rarfile.RarFile(path) as archive:
        if cfg.password:
            archive.setpassword(cfg.password)
        infos = archive.infolist()
        if len(infos) > cfg.max_members:
            raise ArchiveSafetyError(
                f"Archive has too many members ({len(infos)} > {cfg.max_members})"
            )
        needs_pwd = any(getattr(info, "needs_password", lambda: False)() for info in infos)
        if needs_pwd and not cfg.password:
            raise ArchivePasswordError(
                f"RAR архив защищён паролем: {path.name}. Передайте password."
            )
        for info in infos:
            name = str(info.filename).replace("\\", "/")
            if info.is_dir():
                members.append({"name": name.rstrip("/"), "is_dir": True, "size": 0})
                continue
            try:
                safe = _safe_member_name(name)
            except ArchiveSafetyError:
                members.append(
                    {"name": name, "is_dir": False, "size": info.file_size, "unsafe": True}
                )
                continue
            members.append({"name": safe, "is_dir": False, "size": int(info.file_size or 0)})
    return members


def _extract_members(
    path: Path,
    kind: str,
    dest_root: Path,
    *,
    wanted: set[str] | None,
    cfg: ArchiveConfig,
) -> list[dict[str, Any]]:
    if kind == "zip":
        return _extract_zip(path, dest_root, wanted=wanted, cfg=cfg)
    if kind == "tar.zst":
        try:
            return _extract_tar(path, kind, dest_root, wanted=wanted, cfg=cfg)
        except ArchiveUnsupportedError:
            return _extract_via_7z(path, dest_root, wanted=wanted, cfg=cfg)
        except Exception:
            if _find_7z() is not None:
                return _extract_via_7z(path, dest_root, wanted=wanted, cfg=cfg)
            raise
    if kind.startswith("tar"):
        return _extract_tar(path, kind, dest_root, wanted=wanted, cfg=cfg)
    if kind in {"gz", "bz2", "xz", "zst"}:
        return _extract_stream(path, kind, dest_root, wanted=wanted, cfg=cfg)
    if kind == "7z":
        if _module_available("py7zr"):
            try:
                return _extract_7z(path, dest_root, wanted=wanted, cfg=cfg)
            except ArchivePasswordError:
                raise
            except Exception:
                if _find_7z() is not None:
                    return _extract_via_7z(path, dest_root, wanted=wanted, cfg=cfg)
                raise
        return _extract_via_7z(path, dest_root, wanted=wanted, cfg=cfg)
    if kind == "rar":
        if _module_available("rarfile"):
            try:
                return _extract_rar(path, dest_root, wanted=wanted, cfg=cfg)
            except ArchivePasswordError:
                raise
            except Exception:
                if _find_7z() is not None:
                    return _extract_via_7z(path, dest_root, wanted=wanted, cfg=cfg)
                raise
        return _extract_via_7z(path, dest_root, wanted=wanted, cfg=cfg)
    if kind in {"deb", "ar"}:
        try:
            return _extract_deb_or_ar(path, kind, dest_root, wanted=wanted, cfg=cfg)
        except Exception:
            if _find_7z() is not None:
                return _extract_via_7z(path, dest_root, wanted=wanted, cfg=cfg)
            raise
    if kind == "iso":
        try:
            return _extract_iso(path, dest_root, wanted=wanted, cfg=cfg)
        except Exception:
            if _find_7z() is not None:
                return _extract_via_7z(path, dest_root, wanted=wanted, cfg=cfg)
            raise
    if kind in _SEVENZIP_KINDS:
        return _extract_via_7z(path, dest_root, wanted=wanted, cfg=cfg)
    raise ArchiveUnsupportedError(f"Extraction not supported for '{kind}'")


def _guard_total(total: int, add: int, cfg: ArchiveConfig) -> int:
    new_total = total + add
    if new_total > cfg.max_total_uncompressed_bytes:
        raise ArchiveSafetyError(
            f"Archive uncompressed size exceeds limit "
            f"({new_total} > {cfg.max_total_uncompressed_bytes})"
        )
    if add > cfg.max_member_bytes:
        raise ArchiveSafetyError(
            f"Archive member exceeds size limit ({add} > {cfg.max_member_bytes})"
        )
    return new_total


def _extract_zip(
    path: Path,
    dest_root: Path,
    *,
    wanted: set[str] | None,
    cfg: ArchiveConfig,
) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    total = 0
    pwd = _pwd_bytes(cfg)
    with zipfile.ZipFile(path) as archive:
        if pwd is not None:
            archive.setpassword(pwd)
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            safe = _safe_member_name(name)
            if wanted is not None and safe not in wanted and name not in wanted:
                continue
            if bool(info.flag_bits & 0x1) and pwd is None:
                raise ArchivePasswordError(
                    f"ZIP member is password-protected: {safe}. Передайте password."
                )
            total = _guard_total(total, int(info.file_size or 0), cfg)
            dest = _safe_destination(dest_root, safe)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(info, "r", pwd=pwd) as src, dest.open("wb") as out:
                    _copy_limited(src, out, limit=cfg.max_member_bytes)
            except RuntimeError as exc:
                msg = str(exc).lower()
                if "password" in msg or "encrypted" in msg or "bad password" in msg:
                    raise ArchivePasswordError(
                        f"Неверный или отсутствующий пароль ZIP: {path.name}"
                    ) from exc
                raise
            extracted.append({"name": safe, "path": str(dest), "size": dest.stat().st_size})
            if len(extracted) >= cfg.max_members:
                break
    return extracted


def _extract_tar(
    path: Path,
    kind: str,
    dest_root: Path,
    *,
    wanted: set[str] | None,
    cfg: ArchiveConfig,
) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    total = 0
    archive = _open_tar(path, kind)
    try:
        for info in archive:
            if not info.isreg():
                continue
            name = info.name.replace("\\", "/")
            safe = _safe_member_name(name)
            if wanted is not None and safe not in wanted and name not in wanted:
                continue
            total = _guard_total(total, int(info.size or 0), cfg)
            dest = _safe_destination(dest_root, safe)
            dest.parent.mkdir(parents=True, exist_ok=True)
            handle = archive.extractfile(info)
            if handle is None:
                continue
            with handle, dest.open("wb") as out:
                _copy_limited(handle, out, limit=cfg.max_member_bytes)
            extracted.append({"name": safe, "path": str(dest), "size": dest.stat().st_size})
            if len(extracted) >= cfg.max_members:
                break
    finally:
        _close_tar(archive)
    return extracted


def _extract_stream(
    path: Path,
    kind: str,
    dest_root: Path,
    *,
    wanted: set[str] | None,
    cfg: ArchiveConfig,
) -> list[dict[str, Any]]:
    member = path.name
    for suffix in (".gz", ".bz2", ".xz", ".zst", ".zstd"):
        if member.lower().endswith(suffix):
            member = member[: -len(suffix)]
            break
    member = member or "payload"
    if wanted is not None and member not in wanted:
        return []
    dest = _safe_destination(dest_root, member)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if kind == "zst":
        with _open_zstd_stream(path) as src, dest.open("wb") as out:
            written = _copy_limited(src, out, limit=cfg.max_member_bytes)
    else:
        opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[kind]
        with opener(path, "rb") as src, dest.open("wb") as out:  # type: ignore[operator]
            written = _copy_limited(src, out, limit=cfg.max_member_bytes)
    _guard_total(0, written, cfg)
    return [{"name": member, "path": str(dest), "size": dest.stat().st_size}]


def _open_zstd_stream(path: Path) -> BinaryIO:
    try:
        import zstandard as zstd  # type: ignore[import-not-found]
    except ImportError:
        # Fall back: extract via 7z to a temp file and return a file handle.
        seven = _find_7z()
        if seven is None:
            raise ArchiveUnsupportedError(
                "zst requires optional dependency zstandard or 7-Zip CLI"
            ) from None
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
        try:
            data = _read_via_7z(path, member=None, limit=50_000_000, cfg=ArchiveConfig())
            tmp_path.write_bytes(data)
            return tmp_path.open("rb")
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
    raw = path.open("rb")
    return zstd.ZstdDecompressor().stream_reader(raw)  # type: ignore[return-value]


def _extract_7z(
    path: Path,
    dest_root: Path,
    *,
    wanted: set[str] | None,
    cfg: ArchiveConfig,
) -> list[dict[str, Any]]:
    try:
        import py7zr  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveUnsupportedError(
            "7z extraction requires optional dependency py7zr"
        ) from exc
    targets = list(wanted) if wanted else None
    password = cfg.password or None
    try:
        archive_cm = py7zr.SevenZipFile(path, mode="r", password=password)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "password" in msg or "encrypted" in msg:
            raise ArchivePasswordError(
                f"Неверный или отсутствующий пароль 7z: {path.name}"
            ) from exc
        raise
    with archive_cm as archive:
        all_names = [
            str(i.filename).replace("\\", "/")
            for i in archive.list()
            if not i.is_directory
        ]
        for name in all_names:
            _safe_member_name(name)
        try:
            archive.extract(path=dest_root, targets=targets)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "password" in msg or "encrypted" in msg:
                raise ArchivePasswordError(
                    f"Неверный или отсутствующий пароль 7z: {path.name}"
                ) from exc
            raise
    extracted: list[dict[str, Any]] = []
    total = 0
    for name in all_names:
        if wanted is not None and name not in wanted and _safe_member_name(name) not in wanted:
            continue
        dest = _safe_destination(dest_root, name)
        if not dest.exists() or not dest.is_file():
            continue
        size = dest.stat().st_size
        total = _guard_total(total, size, cfg)
        extracted.append({"name": _safe_member_name(name), "path": str(dest), "size": size})
    return extracted


def _extract_rar(
    path: Path,
    dest_root: Path,
    *,
    wanted: set[str] | None,
    cfg: ArchiveConfig,
) -> list[dict[str, Any]]:
    try:
        import rarfile  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveUnsupportedError(
            "RAR extraction requires optional dependency rarfile"
        ) from exc
    extracted: list[dict[str, Any]] = []
    total = 0
    with rarfile.RarFile(path) as archive:
        if cfg.password:
            archive.setpassword(cfg.password)
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = str(info.filename).replace("\\", "/")
            safe = _safe_member_name(name)
            if wanted is not None and safe not in wanted and name not in wanted:
                continue
            if getattr(info, "needs_password", lambda: False)() and not cfg.password:
                raise ArchivePasswordError(
                    f"RAR member is password-protected: {safe}. Передайте password."
                )
            total = _guard_total(total, int(info.file_size or 0), cfg)
            dest = _safe_destination(dest_root, safe)
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                with archive.open(info) as src, dest.open("wb") as out:
                    _copy_limited(src, out, limit=cfg.max_member_bytes)
            except rarfile.BadRarFile as exc:
                raise ArchivePasswordError(
                    f"Неверный или отсутствующий пароль RAR: {path.name}"
                ) from exc
            extracted.append({"name": safe, "path": str(dest), "size": dest.stat().st_size})
    return extracted


def _read_member_bytes(
    path: Path,
    kind: str,
    member: str,
    *,
    limit: int,
    cfg: ArchiveConfig,
) -> bytes:
    if kind == "zip":
        pwd = _pwd_bytes(cfg)
        with zipfile.ZipFile(path) as archive:
            if pwd is not None:
                archive.setpassword(pwd)
            try:
                info = archive.getinfo(member)
            except KeyError:
                match = None
                for item in archive.infolist():
                    try:
                        if _safe_member_name(item.filename) == member:
                            match = item
                            break
                    except ArchiveSafetyError:
                        continue
                if match is None:
                    raise ArchiveError(f"Member not found: {member}") from None
                info = match
            if info.file_size > cfg.max_member_bytes:
                raise ArchiveSafetyError(
                    f"Member too large ({info.file_size} > {cfg.max_member_bytes})"
                )
            if bool(info.flag_bits & 0x1) and pwd is None:
                raise ArchivePasswordError(
                    f"ZIP member is password-protected: {member}. Передайте password."
                )
            try:
                with archive.open(info, "r", pwd=pwd) as handle:
                    return handle.read(limit)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if (
                    isinstance(exc, NotImplementedError)
                    or "compression method" in msg
                    or "not supported" in msg
                ):
                    raise ArchiveUnsupportedError(
                        f"ZIP использует неподдерживаемое шифрование/сжатие "
                        f"(часто AES/WinZip AES): {path.name}. "
                        f"Пересоздайте архив с ZipCrypto или используйте 7z+py7zr."
                    ) from exc
                if "password" in msg or "encrypted" in msg or "bad password" in msg:
                    raise ArchivePasswordError(
                        f"Неверный или отсутствующий пароль ZIP: {path.name}"
                    ) from exc
                raise
    if kind.startswith("tar"):
        archive = _open_tar(path, kind)
        try:
            target = None
            for info in archive:
                if not info.isreg():
                    continue
                try:
                    safe = _safe_member_name(info.name)
                except ArchiveSafetyError:
                    continue
                if safe == member or info.name.replace("\\", "/") == member:
                    target = info
                    break
            if target is None:
                raise ArchiveError(f"Member not found: {member}")
            if int(target.size or 0) > cfg.max_member_bytes:
                raise ArchiveSafetyError(
                    f"Member too large ({target.size} > {cfg.max_member_bytes})"
                )
            handle = archive.extractfile(target)
            if handle is None:
                raise ArchiveError(f"Cannot read member: {member}")
            with handle:
                return handle.read(limit)
        finally:
            _close_tar(archive)
    if kind in {"gz", "bz2", "xz"}:
        opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[kind]
        with opener(path, "rb") as handle:  # type: ignore[operator]
            return handle.read(limit)
    if kind == "zst":
        with _open_zstd_stream(path) as handle:
            return handle.read(limit)
    if kind == "7z":
        if _module_available("py7zr"):
            try:
                return _read_7z_member(path, member, limit=limit, cfg=cfg)
            except ArchivePasswordError:
                raise
            except Exception:
                if _find_7z() is not None:
                    return _read_via_7z(path, member=member, limit=limit, cfg=cfg)
                raise
        return _read_via_7z(path, member=member, limit=limit, cfg=cfg)
    if kind == "rar":
        if _module_available("rarfile"):
            try:
                return _read_rar_member(path, member, limit=limit, cfg=cfg)
            except ArchivePasswordError:
                raise
            except Exception:
                if _find_7z() is not None:
                    return _read_via_7z(path, member=member, limit=limit, cfg=cfg)
                raise
        return _read_via_7z(path, member=member, limit=limit, cfg=cfg)
    if kind in {"deb", "ar"}:
        try:
            return _read_deb_or_ar_member(path, kind, member, limit=limit, cfg=cfg)
        except Exception:
            if _find_7z() is not None:
                return _read_via_7z(path, member=member, limit=limit, cfg=cfg)
            raise
    if kind == "iso":
        try:
            return _read_iso_member(path, member, limit=limit, cfg=cfg)
        except Exception:
            if _find_7z() is not None:
                return _read_via_7z(path, member=member, limit=limit, cfg=cfg)
            raise
    if kind in _SEVENZIP_KINDS:
        return _read_via_7z(path, member=member, limit=limit, cfg=cfg)
    raise ArchiveUnsupportedError(f"Reading members not supported for '{kind}'")


def _read_7z_member(path: Path, member: str, *, limit: int, cfg: ArchiveConfig) -> bytes:
    try:
        import py7zr  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveUnsupportedError("7z requires py7zr") from exc
    password = cfg.password or None
    try:
        with py7zr.SevenZipFile(path, mode="r", password=password) as archive:
            names = list(archive.getnames())
            if member not in names:
                # normalized match
                match = None
                for name in names:
                    try:
                        if _safe_member_name(name) == member:
                            match = name
                            break
                    except ArchiveSafetyError:
                        continue
                if match is None:
                    raise ArchiveError(f"Member not found: {member}")
                member = match
            data_map = archive.read([member])
            payload = data_map.get(member)
            if payload is None:
                raise ArchiveError(f"Member not found: {member}")
            if hasattr(payload, "read"):
                return payload.read(limit)
            return bytes(payload)[:limit]
    except ArchiveError:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "password" in msg or "encrypted" in msg:
            raise ArchivePasswordError(
                f"Неверный или отсутствующий пароль 7z: {path.name}"
            ) from exc
        raise


def _read_rar_member(path: Path, member: str, *, limit: int, cfg: ArchiveConfig) -> bytes:
    try:
        import rarfile  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ArchiveUnsupportedError("RAR requires rarfile") from exc
    try:
        with rarfile.RarFile(path) as archive:
            if cfg.password:
                archive.setpassword(cfg.password)
            with archive.open(member) as handle:
                return handle.read(limit)
    except ArchiveError:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "password" in msg or "encrypted" in msg or "bad password" in msg:
            raise ArchivePasswordError(
                f"Неверный или отсутствующий пароль RAR: {path.name}"
            ) from exc
        raise


def _copy_limited(src: Any, dest: Any, *, limit: int) -> int:
    written = 0
    while True:
        chunk = src.read(1024 * 256)
        if not chunk:
            break
        written += len(chunk)
        if written > limit:
            raise ArchiveSafetyError(f"Member exceeds size limit ({written} > {limit})")
        dest.write(chunk)
    return written


def _list_markdown(name: str, kind: str, members: Sequence[dict[str, Any]], truncated: bool) -> str:
    lines = [
        f"# Archive: {name}",
        "",
        f"- kind: `{kind}`",
        f"- members listed: {len(members)}" + (" (truncated)" if truncated else ""),
        "",
    ]
    for item in list(members)[:40]:
        mark = "/" if item.get("is_dir") else ""
        size = item.get("size")
        size_text = f" ({size} B)" if size not in (None, 0) and not item.get("is_dir") else ""
        flag = " [unsafe]" if item.get("unsafe") else ""
        lines.append(f"- `{item.get('name')}{mark}`{size_text}{flag}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Unix ar / Debian packages
# --------------------------------------------------------------------------- #
def _iter_ar_members(path: Path):
    data = path.read_bytes()
    if not data.startswith(b"!<arch>\n"):
        raise ArchiveError(f"Not a Unix ar archive: {path.name}")
    offset = 8
    while offset + 60 <= len(data):
        header = data[offset : offset + 60]
        if header.strip(b"\x00") == b"":
            break
        name = header[0:16].decode("ascii", errors="replace").strip()
        size_field = header[48:58].decode("ascii", errors="replace").strip()
        try:
            size = int(size_field)
        except ValueError as exc:
            raise ArchiveError(f"Corrupt ar header in {path.name}") from exc
        magic = header[58:60]
        if magic != b"`\n":
            raise ArchiveError(f"Corrupt ar magic in {path.name}")
        offset += 60
        payload = data[offset : offset + size]
        offset += size
        if size % 2 == 1:
            offset += 1
        # BSD long names: #1/N
        if name.startswith("#1/"):
            try:
                name_len = int(name[3:])
            except ValueError as exc:
                raise ArchiveError(f"Corrupt BSD ar name in {path.name}") from exc
            name = payload[:name_len].decode("utf-8", errors="replace").rstrip("\x00")
            payload = payload[name_len:]
            size = len(payload)
        else:
            name = name.rstrip("/")
        yield name, payload


def _nested_tar_kind(name: str) -> str | None:
    lower = name.lower()
    if lower.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if lower.endswith((".tar.xz", ".txz")):
        return "tar.xz"
    if lower.endswith((".tar.bz2", ".tbz2", ".tbz")):
        return "tar.bz2"
    if lower.endswith((".tar.zst", ".tzst")):
        return "tar.zst"
    if lower.endswith(".tar"):
        return "tar"
    return None


def _list_tar_bytes(payload: bytes, nested_kind: str, cfg: ArchiveConfig, *, prefix: str) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    fileobj: BinaryIO
    if nested_kind == "tar.gz":
        fileobj = gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb")  # type: ignore[assignment]
        mode = "r|"
    elif nested_kind == "tar.bz2":
        fileobj = bz2.BZ2File(io.BytesIO(payload), mode="rb")  # type: ignore[assignment]
        mode = "r|"
    elif nested_kind == "tar.xz":
        fileobj = lzma.LZMAFile(io.BytesIO(payload), mode="rb")  # type: ignore[assignment]
        mode = "r|"
    elif nested_kind == "tar.zst":
        try:
            import zstandard as zstd  # type: ignore[import-not-found]
        except ImportError:
            return [
                {
                    "name": f"{prefix.rstrip('/')}",
                    "is_dir": False,
                    "size": len(payload),
                    "nested": True,
                    "note": "install zstandard to expand nested tar.zst",
                }
            ]
        fileobj = zstd.ZstdDecompressor().stream_reader(io.BytesIO(payload))  # type: ignore[assignment]
        mode = "r|"
    else:
        fileobj = io.BytesIO(payload)
        mode = "r:"
    with tarfile.open(fileobj=fileobj, mode=mode) as archive:
        for info in archive:
            name = info.name.replace("\\", "/")
            full = f"{prefix}{name}"
            if info.isdir():
                members.append({"name": full.rstrip("/"), "is_dir": True, "size": 0, "nested": True})
                continue
            if not info.isreg():
                continue
            try:
                safe = _safe_member_name(full)
            except ArchiveSafetyError:
                members.append(
                    {"name": full, "is_dir": False, "size": int(info.size or 0), "unsafe": True}
                )
                continue
            members.append(
                {"name": safe, "is_dir": False, "size": int(info.size or 0), "nested": True}
            )
            if len(members) > cfg.max_members:
                raise ArchiveSafetyError(
                    f"Archive has too many members (>{cfg.max_members})"
                )
    return members


def _list_deb_or_ar(path: Path, kind: str, cfg: ArchiveConfig) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    for name, payload in _iter_ar_members(path):
        try:
            safe = _safe_member_name(name)
        except ArchiveSafetyError:
            members.append(
                {"name": name, "is_dir": False, "size": len(payload), "unsafe": True}
            )
            continue
        members.append({"name": safe, "is_dir": False, "size": len(payload), "ar_member": True})
        nested = _nested_tar_kind(name)
        if kind == "deb" and nested is not None:
            members.extend(
                _list_tar_bytes(payload, nested, cfg, prefix=f"{safe}/")
            )
        if len(members) > cfg.max_members:
            raise ArchiveSafetyError(
                f"Archive has too many members (>{cfg.max_members})"
            )
    return members


def _extract_deb_or_ar(
    path: Path,
    kind: str,
    dest_root: Path,
    *,
    wanted: set[str] | None,
    cfg: ArchiveConfig,
) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    total = 0
    for name, payload in _iter_ar_members(path):
        safe = _safe_member_name(name)
        nested = _nested_tar_kind(name) if kind == "deb" else None
        # Extract nested tar members when requested or when extracting all.
        if nested is not None:
            nested_members = _list_tar_bytes(payload, nested, cfg, prefix=f"{safe}/")
            need_nested = wanted is None or any(
                (m.get("name") in wanted) for m in nested_members if not m.get("is_dir")
            )
            if need_nested:
                fileobj: BinaryIO
                if nested == "tar.gz":
                    fileobj = gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb")  # type: ignore[assignment]
                    mode = "r|"
                elif nested == "tar.bz2":
                    fileobj = bz2.BZ2File(io.BytesIO(payload), mode="rb")  # type: ignore[assignment]
                    mode = "r|"
                elif nested == "tar.xz":
                    fileobj = lzma.LZMAFile(io.BytesIO(payload), mode="rb")  # type: ignore[assignment]
                    mode = "r|"
                elif nested == "tar.zst":
                    import zstandard as zstd  # type: ignore[import-not-found]

                    fileobj = zstd.ZstdDecompressor().stream_reader(io.BytesIO(payload))  # type: ignore[assignment]
                    mode = "r|"
                else:
                    fileobj = io.BytesIO(payload)
                    mode = "r:"
                with tarfile.open(fileobj=fileobj, mode=mode) as archive:
                    for info in archive:
                        if not info.isreg():
                            continue
                        full = _safe_member_name(f"{safe}/{info.name}")
                        if wanted is not None and full not in wanted:
                            continue
                        total = _guard_total(total, int(info.size or 0), cfg)
                        dest = _safe_destination(dest_root, full)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        handle = archive.extractfile(info)
                        if handle is None:
                            continue
                        with handle, dest.open("wb") as out:
                            _copy_limited(handle, out, limit=cfg.max_member_bytes)
                        extracted.append(
                            {"name": full, "path": str(dest), "size": dest.stat().st_size}
                        )
        if wanted is None or safe in wanted:
            total = _guard_total(total, len(payload), cfg)
            dest = _safe_destination(dest_root, safe)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if len(payload) > cfg.max_member_bytes:
                raise ArchiveSafetyError(
                    f"Archive member exceeds size limit ({len(payload)} > {cfg.max_member_bytes})"
                )
            dest.write_bytes(payload)
            extracted.append({"name": safe, "path": str(dest), "size": dest.stat().st_size})
        if len(extracted) >= cfg.max_members:
            break
    return extracted


def _read_deb_or_ar_member(
    path: Path,
    kind: str,
    member: str,
    *,
    limit: int,
    cfg: ArchiveConfig,
) -> bytes:
    for name, payload in _iter_ar_members(path):
        safe = _safe_member_name(name)
        if safe == member:
            return payload[:limit]
        nested = _nested_tar_kind(name) if kind == "deb" else None
        if nested is None or not member.startswith(f"{safe}/"):
            continue
        inner = member[len(safe) + 1 :]
        # scan nested tar
        if nested == "tar.gz":
            fileobj: BinaryIO = gzip.GzipFile(fileobj=io.BytesIO(payload), mode="rb")  # type: ignore[assignment]
            mode = "r|"
        elif nested == "tar.bz2":
            fileobj = bz2.BZ2File(io.BytesIO(payload), mode="rb")  # type: ignore[assignment]
            mode = "r|"
        elif nested == "tar.xz":
            fileobj = lzma.LZMAFile(io.BytesIO(payload), mode="rb")  # type: ignore[assignment]
            mode = "r|"
        elif nested == "tar.zst":
            import zstandard as zstd  # type: ignore[import-not-found]

            fileobj = zstd.ZstdDecompressor().stream_reader(io.BytesIO(payload))  # type: ignore[assignment]
            mode = "r|"
        else:
            fileobj = io.BytesIO(payload)
            mode = "r:"
        with tarfile.open(fileobj=fileobj, mode=mode) as archive:
            for info in archive:
                if not info.isreg():
                    continue
                try:
                    safe_inner = _safe_member_name(info.name)
                except ArchiveSafetyError:
                    continue
                if safe_inner == inner or info.name.replace("\\", "/") == inner:
                    if int(info.size or 0) > cfg.max_member_bytes:
                        raise ArchiveSafetyError(
                            f"Member too large ({info.size} > {cfg.max_member_bytes})"
                        )
                    handle = archive.extractfile(info)
                    if handle is None:
                        break
                    with handle:
                        return handle.read(limit)
    raise ArchiveError(f"Member not found: {member}")


# --------------------------------------------------------------------------- #
# ISO 9660 (basic primary volume / directory walk)
# --------------------------------------------------------------------------- #
_ISO_SECTOR = 2048


def _iso_read_sector(handle: BinaryIO, sector: int) -> bytes:
    handle.seek(sector * _ISO_SECTOR)
    data = handle.read(_ISO_SECTOR)
    if len(data) < _ISO_SECTOR:
        raise ArchiveError("Truncated ISO image")
    return data


def _iso_parse_dir_record(buf: bytes, offset: int) -> dict[str, Any] | None:
    if offset >= len(buf):
        return None
    length = buf[offset]
    if length == 0:
        return None
    if offset + length > len(buf):
        return None
    rec = buf[offset : offset + length]
    extent = struct.unpack_from("<I", rec, 2)[0]
    data_len = struct.unpack_from("<I", rec, 10)[0]
    flags = rec[25]
    name_len = rec[32]
    raw_name = rec[33 : 33 + name_len]
    if raw_name == b"\x00":
        name = "."
    elif raw_name == b"\x01":
        name = ".."
    else:
        name = raw_name.split(b";", 1)[0].decode("ascii", errors="replace")
    return {
        "extent": extent,
        "size": data_len,
        "is_dir": bool(flags & 0x02),
        "name": name,
        "record_len": length,
    }


def _iso_iter_dir(handle: BinaryIO, extent: int, size: int, cfg: ArchiveConfig):
    remaining = max(0, int(size))
    sector = extent
    while remaining > 0:
        data = _iso_read_sector(handle, sector)
        offset = 0
        consumed = 0
        while offset < _ISO_SECTOR and remaining - consumed > 0:
            if data[offset] == 0:
                # End of records in this sector; skip rest of sector.
                consumed = min(remaining, _ISO_SECTOR)
                break
            rec = _iso_parse_dir_record(data, offset)
            if rec is None:
                consumed = min(remaining, _ISO_SECTOR)
                break
            yield rec
            step = int(rec["record_len"])
            offset += step
            consumed += step
        if consumed <= 0:
            consumed = min(remaining, _ISO_SECTOR)
        remaining -= consumed
        sector += 1


def _iso_walk(handle: BinaryIO, extent: int, size: int, prefix: str, cfg: ArchiveConfig, out: list[dict[str, Any]]) -> None:
    if len(out) > cfg.max_members:
        raise ArchiveSafetyError(f"Archive has too many members (>{cfg.max_members})")
    for rec in _iso_iter_dir(handle, extent, size, cfg):
        name = str(rec["name"])
        if name in {".", ".."}:
            continue
        full = f"{prefix}{name}" if not prefix else f"{prefix}/{name}"
        if rec["is_dir"]:
            out.append({"name": full, "is_dir": True, "size": 0})
            _iso_walk(handle, int(rec["extent"]), int(rec["size"]), full, cfg, out)
        else:
            try:
                safe = _safe_member_name(full)
            except ArchiveSafetyError:
                out.append(
                    {"name": full, "is_dir": False, "size": int(rec["size"]), "unsafe": True}
                )
                continue
            out.append(
                {
                    "name": safe,
                    "is_dir": False,
                    "size": int(rec["size"]),
                    "extent": int(rec["extent"]),
                }
            )


def _iso_root(handle: BinaryIO) -> tuple[int, int]:
    # Primary Volume Descriptor at sector 16
    pvd = _iso_read_sector(handle, 16)
    if pvd[1:6] != b"CD001":
        # try a few nearby sectors for nonstandard images
        found = None
        for sector in range(16, 32):
            cand = _iso_read_sector(handle, sector)
            if cand[1:6] == b"CD001" and cand[0] == 1:
                pvd = cand
                found = sector
                break
        if found is None:
            raise ArchiveError("ISO9660 primary volume descriptor not found")
    root = pvd[156 : 156 + 34]
    extent = struct.unpack_from("<I", root, 2)[0]
    size = struct.unpack_from("<I", root, 10)[0]
    return extent, size


def _list_iso(path: Path, cfg: ArchiveConfig) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        extent, size = _iso_root(handle)
        _iso_walk(handle, extent, size, "", cfg, members)
    return members


def _iso_find(handle: BinaryIO, member: str, cfg: ArchiveConfig) -> dict[str, Any] | None:
    extent, size = _iso_root(handle)
    members: list[dict[str, Any]] = []
    _iso_walk(handle, extent, size, "", cfg, members)
    for item in members:
        if not item.get("is_dir") and item.get("name") == member:
            return item
    return None


def _read_iso_member(path: Path, member: str, *, limit: int, cfg: ArchiveConfig) -> bytes:
    with path.open("rb") as handle:
        item = _iso_find(handle, member, cfg)
        if item is None:
            raise ArchiveError(f"Member not found: {member}")
        size = int(item.get("size") or 0)
        if size > cfg.max_member_bytes:
            raise ArchiveSafetyError(f"Member too large ({size} > {cfg.max_member_bytes})")
        extent = int(item["extent"])
        handle.seek(extent * _ISO_SECTOR)
        return handle.read(min(size, limit))


def _extract_iso(
    path: Path,
    dest_root: Path,
    *,
    wanted: set[str] | None,
    cfg: ArchiveConfig,
) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    total = 0
    with path.open("rb") as handle:
        members: list[dict[str, Any]] = []
        extent, size = _iso_root(handle)
        _iso_walk(handle, extent, size, "", cfg, members)
        for item in members:
            if item.get("is_dir") or item.get("unsafe"):
                continue
            name = str(item["name"])
            if wanted is not None and name not in wanted:
                continue
            size_i = int(item.get("size") or 0)
            total = _guard_total(total, size_i, cfg)
            dest = _safe_destination(dest_root, name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            handle.seek(int(item["extent"]) * _ISO_SECTOR)
            data = handle.read(size_i)
            if len(data) > cfg.max_member_bytes:
                raise ArchiveSafetyError(
                    f"Member exceeds size limit ({len(data)} > {cfg.max_member_bytes})"
                )
            dest.write_bytes(data)
            extracted.append({"name": name, "path": str(dest), "size": dest.stat().st_size})
            if len(extracted) >= cfg.max_members:
                break
    return extracted


# --------------------------------------------------------------------------- #
# 7-Zip CLI backend (password-aware, non-interactive)
# --------------------------------------------------------------------------- #
def _find_7z() -> Path | None:
    env = (os.environ.get("JARVIS_7Z") or os.environ.get("SEVEN_ZIP") or "").strip()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    which = shutil.which("7z") or shutil.which("7z.exe") or shutil.which("7za")
    if which:
        candidates.append(Path(which))
    candidates.extend(
        [
            Path(r"C:\Program Files\7-Zip\7z.exe"),
            Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
            Path("/usr/bin/7z"),
            Path("/usr/local/bin/7z"),
            Path("/usr/bin/7za"),
        ]
    )
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _7z_password_args(cfg: ArchiveConfig) -> list[str]:
    # Always pass -p to avoid interactive password prompts when headers are encrypted.
    # Empty -p means "try empty password".
    if cfg.password:
        return [f"-p{cfg.password}"]
    return ["-p"]


def _looks_like_password_error(text: str) -> bool:
    lower = text.lower()
    needles = (
        "wrong password",
        "enter password",
        "encrypted",
        "break signaled",
        "cannot open encrypted",
        "password",
        "парол",
    )
    return any(n in lower for n in needles)


def _run_7z(
    args: Sequence[str],
    *,
    cfg: ArchiveConfig,
    binary_stdout: bool = False,
    timeout: int = 180,
) -> subprocess.CompletedProcess[Any]:
    seven = _find_7z()
    if seven is None:
        raise ArchiveUnsupportedError(
            "7-Zip CLI not found. Install 7-Zip or set JARVIS_7Z to 7z.exe"
        )
    cmd = [str(seven), *args]
    # Inject password switches after the command verb when present.
    if cmd:
        # args already may include password; ensure non-interactive defaults.
        pass
    try:
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArchiveError(f"7-Zip timed out on archive operation") from exc
    if binary_stdout:
        return completed
    # Decode for error mapping
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    text = f"{stdout}\n{stderr}"
    if completed.returncode not in (0,):
        if _looks_like_password_error(text):
            raise ArchivePasswordError(
                "Архив защищён паролем или пароль неверен. Передайте password."
            )
        # returncode 255 often means password prompt interrupted
        if completed.returncode in {255, 2} and (
            "enter password" in text.lower() or "break signaled" in text.lower()
        ):
            raise ArchivePasswordError(
                "Архив защищён паролем или пароль неверен. Передайте password."
            )
    return completed


def _parse_7z_slt(text: str) -> list[dict[str, Any]]:
    members: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    # Skip until first ---------- separator after archive headers
    body = text
    if "----------" in text:
        body = text.split("----------", 1)[1]
    for raw_line in body.splitlines():
        line = raw_line.strip("\r")
        if not line.strip():
            if current.get("Path"):
                members.append(_slt_entry_to_member(current))
                current = {}
            continue
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        current[key.strip()] = value.strip()
    if current.get("Path"):
        members.append(_slt_entry_to_member(current))
    return members


def _slt_entry_to_member(entry: dict[str, str]) -> dict[str, Any]:
    name = (entry.get("Path") or "").replace("\\", "/")
    attrs = entry.get("Attributes") or entry.get("Mode") or ""
    folder = (entry.get("Folder") or "").strip()
    is_dir = folder == "+" or attrs[:1].upper() == "D" or name.endswith("/")
    size_raw = entry.get("Size") or "0"
    try:
        size = int(size_raw) if size_raw not in {"", None} else 0
    except ValueError:
        size = 0
    encrypted = (entry.get("Encrypted") or "").strip() in {"+", "1", "true", "True"}
    try:
        safe = _safe_member_name(name.rstrip("/")) if name and name not in {".", ".."} else name
        unsafe = False
    except ArchiveSafetyError:
        safe = name
        unsafe = True
    return {
        "name": safe.rstrip("/") if is_dir else safe,
        "is_dir": is_dir,
        "size": 0 if is_dir else size,
        "encrypted": encrypted,
        "unsafe": unsafe,
        "engine": "7z-cli",
    }


def _list_via_7z(path: Path, cfg: ArchiveConfig) -> list[dict[str, Any]]:
    args = [
        "l",
        "-slt",
        "-bso1",
        "-bsp0",
        "-bse1",
        "-y",
        *_7z_password_args(cfg),
        "--",
        str(path),
    ]
    completed = _run_7z(args, cfg=cfg)
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    text = f"{stdout}\n{stderr}"
    if completed.returncode != 0:
        if _looks_like_password_error(text):
            raise ArchivePasswordError(
                f"Архив защищён паролем: {path.name}. Передайте password."
            )
        raise ArchiveError(f"7-Zip failed to list {path.name}: {stderr or stdout}")
    members = [m for m in _parse_7z_slt(stdout) if m.get("name")]
    # Drop the archive path itself if present as first Path in some formats
    cleaned = []
    archive_name = path.name
    for item in members:
        name = str(item.get("name") or "")
        if name == archive_name or name == str(path):
            continue
        cleaned.append(item)
    if len(cleaned) > cfg.max_members:
        raise ArchiveSafetyError(
            f"Archive has too many members ({len(cleaned)} > {cfg.max_members})"
        )
    return cleaned


def _extract_via_7z(
    path: Path,
    dest_root: Path,
    *,
    wanted: set[str] | None,
    cfg: ArchiveConfig,
) -> list[dict[str, Any]]:
    dest_root.mkdir(parents=True, exist_ok=True)
    # 7z -o switch must be glued to path, trailing sep preferred on Windows.
    out_switch = f"-o{dest_root}{os.sep}"
    args = [
        "x",
        "-y",
        "-bso0",
        "-bsp0",
        "-bse1",
        out_switch,
        *_7z_password_args(cfg),
        "--",
        str(path),
    ]
    if wanted:
        args.extend(sorted(wanted))
    completed = _run_7z(args, cfg=cfg)
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    text = f"{stdout}\n{stderr}"
    if completed.returncode != 0:
        if _looks_like_password_error(text):
            raise ArchivePasswordError(
                f"Неверный или отсутствующий пароль: {path.name}"
            )
        raise ArchiveError(f"7-Zip failed to extract {path.name}: {stderr or stdout}")

    extracted: list[dict[str, Any]] = []
    total = 0
    for file_path in sorted(dest_root.rglob("*")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(dest_root).as_posix()
        try:
            safe = _safe_member_name(rel)
        except ArchiveSafetyError as exc:
            # Remove escapes if any slipped through.
            try:
                file_path.unlink()
            except OSError:
                pass
            raise ArchiveSafetyError(f"Member escapes output directory: {rel}") from exc
        if wanted is not None and safe not in wanted and rel not in wanted:
            continue
        size = file_path.stat().st_size
        total = _guard_total(total, size, cfg)
        extracted.append({"name": safe, "path": str(file_path), "size": size})
        if len(extracted) >= cfg.max_members:
            break
    return extracted


def _read_via_7z(
    path: Path,
    *,
    member: str | None,
    limit: int,
    cfg: ArchiveConfig,
) -> bytes:
    args = [
        "e",
        "-so",
        "-y",
        "-bso0",
        "-bsp0",
        "-bse0",
        *_7z_password_args(cfg),
        "--",
        str(path),
    ]
    if member:
        args.append(member)
    completed = _run_7z(args, cfg=cfg, binary_stdout=True)
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        text = stderr + completed.stdout[:200].decode("utf-8", errors="replace")
        if _looks_like_password_error(text):
            raise ArchivePasswordError(
                f"Неверный или отсутствующий пароль: {path.name}"
            )
        if member:
            raise ArchiveError(f"7-Zip failed to read member {member!r} from {path.name}: {stderr}")
        raise ArchiveError(f"7-Zip failed to read {path.name}: {stderr}")
    data = completed.stdout
    if len(data) > limit:
        return data[:limit]
    if member and len(data) == 0:
        # Distinguish missing member vs empty file when possible
        listing = _list_via_7z(path, cfg)
        names = {str(m.get("name")) for m in listing if not m.get("is_dir")}
        if member not in names and _safe_member_name(member) not in names:
            raise ArchiveError(f"Member not found: {member}")
    return data
