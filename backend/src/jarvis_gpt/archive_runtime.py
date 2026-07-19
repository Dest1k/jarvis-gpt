"""Safe archive listing, extraction, creation, and member reading.

Supports common formats with stdlib first; optional engines degrade gracefully:

- ZIP family (zip, jar, war, wheel, …) via ``zipfile``
- TAR family (tar, tar.gz/tgz, tar.bz2, tar.xz) via ``tarfile``
- Single-stream compress (gz, bz2, xz) via ``gzip``/``bz2``/``lzma``
- 7z via optional ``py7zr``
- RAR via optional ``rarfile``

Safety policy:
- reject path traversal and absolute member paths
- cap member count, per-member size, and total uncompressed bytes
- never extract into source path; write only under an explicit output dir
"""

from __future__ import annotations

import bz2
import gzip
import hashlib
import lzma
import shutil
import tarfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    py7zr_ok = False
    rarfile_ok = False
    try:
        import py7zr  # type: ignore[import-not-found]  # noqa: F401

        py7zr_ok = True
    except Exception:  # noqa: BLE001
        py7zr_ok = False
    try:
        import rarfile  # type: ignore[import-not-found]  # noqa: F401

        rarfile_ok = True
    except Exception:  # noqa: BLE001
        rarfile_ok = False
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
        },
        "optional": {
            "7z": py7zr_ok,
            "rar": rarfile_ok,
            "engine_7z": "py7zr" if py7zr_ok else None,
            "engine_rar": "rarfile" if rarfile_ok else None,
        },
        "create": ["zip", "tar", "tar.gz", "tar.bz2", "tar.xz", "gz"],
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
    cfg = config or ArchiveConfig()
    if password is not None:
        cfg = ArchiveConfig(
            max_members=cfg.max_members,
            max_member_bytes=cfg.max_member_bytes,
            max_total_uncompressed_bytes=cfg.max_total_uncompressed_bytes,
            max_list_members=cfg.max_list_members,
            allow_symlinks=cfg.allow_symlinks,
            password=password,
        )
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
    cfg = config or ArchiveConfig()
    if password is not None:
        cfg = ArchiveConfig(
            max_members=cfg.max_members,
            max_member_bytes=cfg.max_member_bytes,
            max_total_uncompressed_bytes=cfg.max_total_uncompressed_bytes,
            max_list_members=cfg.max_list_members,
            allow_symlinks=cfg.allow_symlinks,
            password=password,
        )
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
    cfg = config or ArchiveConfig()
    if password is not None:
        cfg = ArchiveConfig(
            max_members=cfg.max_members,
            max_member_bytes=cfg.max_member_bytes,
            max_total_uncompressed_bytes=cfg.max_total_uncompressed_bytes,
            max_list_members=cfg.max_list_members,
            allow_symlinks=cfg.allow_symlinks,
            password=password,
        )
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
    if kind in {"gz", "bz2", "xz", "7z", "rar", "cab", "iso", "ar", "deb", "rpm", "cpio", "zst"}:
        return kind
    # extension fallback
    name = path.name.lower()
    if name.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if name.endswith((".tar.bz2", ".tbz2", ".tbz")):
        return "tar.bz2"
    if name.endswith((".tar.xz", ".txz")):
        return "tar.xz"
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
    if name.endswith(".7z"):
        return "7z"
    if name.endswith(".rar"):
        return "rar"
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
    if kind.startswith("tar"):
        return _list_tar(path, kind, cfg)
    if kind in {"gz", "bz2", "xz"}:
        return _list_stream(path, kind)
    if kind == "7z":
        return _list_7z(path, cfg)
    if kind == "rar":
        return _list_rar(path, cfg)
    raise ArchiveUnsupportedError(
        f"Listing not implemented for '{kind}'. "
        f"Install optional engines for 7z/rar if needed; cab/iso/rpm are identify-only."
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


def _list_tar(path: Path, kind: str, cfg: ArchiveConfig) -> list[dict[str, Any]]:
    mode = {
        "tar": "r:",
        "tar.gz": "r:gz",
        "tar.bz2": "r:bz2",
        "tar.xz": "r:xz",
    }.get(kind, "r:*")
    members: list[dict[str, Any]] = []
    with tarfile.open(path, mode) as archive:
        infos = archive.getmembers()
        if len(infos) > cfg.max_members:
            raise ArchiveSafetyError(
                f"Archive has too many members ({len(infos)} > {cfg.max_members})"
            )
        for info in infos:
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
    return members


def _list_stream(path: Path, kind: str) -> list[dict[str, Any]]:
    # Single-file compressed stream: synthetic member name = stem without compress suffix.
    name = path.name
    for suffix in (".gz", ".bz2", ".xz"):
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
        needs_pwd = any(
            getattr(info, "needs_password", lambda: False)() for info in infos
        )
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
    if kind.startswith("tar"):
        return _extract_tar(path, kind, dest_root, wanted=wanted, cfg=cfg)
    if kind in {"gz", "bz2", "xz"}:
        return _extract_stream(path, kind, dest_root, wanted=wanted, cfg=cfg)
    if kind == "7z":
        return _extract_7z(path, dest_root, wanted=wanted, cfg=cfg)
    if kind == "rar":
        return _extract_rar(path, dest_root, wanted=wanted, cfg=cfg)
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
                # zipfile raises RuntimeError on bad password.
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
    mode = {
        "tar": "r:",
        "tar.gz": "r:gz",
        "tar.bz2": "r:bz2",
        "tar.xz": "r:xz",
    }.get(kind, "r:*")
    extracted: list[dict[str, Any]] = []
    total = 0
    with tarfile.open(path, mode) as archive:
        for info in archive.getmembers():
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
    for suffix in (".gz", ".bz2", ".xz"):
        if member.lower().endswith(suffix):
            member = member[: -len(suffix)]
            break
    member = member or "payload"
    if wanted is not None and member not in wanted:
        return []
    dest = _safe_destination(dest_root, member)
    dest.parent.mkdir(parents=True, exist_ok=True)
    opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[kind]
    with opener(path, "rb") as src, dest.open("wb") as out:  # type: ignore[operator]
        written = _copy_limited(src, out, limit=cfg.max_member_bytes)
    _guard_total(0, written, cfg)
    return [{"name": member, "path": str(dest), "size": dest.stat().st_size}]


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
        # py7zr extracts with its own path checks; still re-validate listed names.
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
            # try exact and raw
            try:
                info = archive.getinfo(member)
            except KeyError:
                # search case-sensitive normalized names
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
            except RuntimeError as exc:
                msg = str(exc).lower()
                if "password" in msg or "encrypted" in msg:
                    raise ArchivePasswordError(
                        f"Неверный или отсутствующий пароль ZIP: {path.name}"
                    ) from exc
                raise
    if kind.startswith("tar"):
        mode = {
            "tar": "r:",
            "tar.gz": "r:gz",
            "tar.bz2": "r:bz2",
            "tar.xz": "r:xz",
        }.get(kind, "r:*")
        with tarfile.open(path, mode) as archive:
            target = None
            for info in archive.getmembers():
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
    if kind in {"gz", "bz2", "xz"}:
        opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[kind]
        with opener(path, "rb") as handle:  # type: ignore[operator]
            return handle.read(limit)
    if kind == "7z":
        try:
            import py7zr  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ArchiveUnsupportedError("7z requires py7zr") from exc
        with py7zr.SevenZipFile(path, mode="r") as archive:
            names = list(archive.getnames())
            if member not in names:
                raise ArchiveError(f"Member not found: {member}")
            data_map = archive.read([member])
            payload = data_map.get(member)
            if payload is None:
                raise ArchiveError(f"Member not found: {member}")
            if hasattr(payload, "read"):
                return payload.read(limit)
            return bytes(payload)[:limit]
    if kind == "rar":
        try:
            import rarfile  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ArchiveUnsupportedError("RAR requires rarfile") from exc
        with rarfile.RarFile(path) as archive, archive.open(member) as handle:
            return handle.read(limit)
    raise ArchiveUnsupportedError(f"Reading members not supported for '{kind}'")


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
