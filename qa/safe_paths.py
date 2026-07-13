"""Fail-closed identifier, output-path, and bounded-file helpers."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_CONFIGURABLE_FILE_BYTES = 64 * 1024 * 1024
_REPARSE_POINT = 0x0400
_CASE_ID_RE = re.compile(r"^[A-Z0-9](?:[A-Z0-9]|[._-](?=[A-Z0-9])){2,79}$")
_CAMPAIGN_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|[._-](?=[a-z0-9])){7,127}$")
_ROOT_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


class SafePathError(ValueError):
    """A bounded path operation failed before untrusted bytes were consumed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BoundedFileDigest:
    sha256: str
    size: int


def _reject_windows_reserved(value: str, label: str) -> None:
    base = value.split(".", 1)[0].upper()
    if base in _WINDOWS_RESERVED:
        raise ValueError(f"{label} uses a reserved device name")


def validate_case_id(value: object, *, label: str = "case_id") -> str:
    if not isinstance(value, str) or not _CASE_ID_RE.fullmatch(value):
        raise ValueError(f"{label} must be a canonical uppercase identifier")
    if ".." in value or value.endswith((".", " ")):
        raise ValueError(f"{label} contains a traversal-like segment")
    _reject_windows_reserved(value, label)
    return value


def validate_campaign_identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _CAMPAIGN_ID_RE.fullmatch(value):
        raise ValueError(f"{label} must be a canonical lowercase identifier")
    if ".." in value or value.endswith((".", " ")):
        raise ValueError(f"{label} contains a traversal-like segment")
    _reject_windows_reserved(value, label)
    return value


def validate_root_alias(value: object, *, label: str = "root alias") -> str:
    if not isinstance(value, str) or not _ROOT_ALIAS_RE.fullmatch(value):
        raise ValueError(f"{label} is invalid")
    return value


def validate_relative_path(value: object, *, label: str = "path") -> str:
    """Accept one canonical POSIX relative path and reject cross-platform escapes."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise SafePathError("INVALID_PATH", f"{label} must be a non-empty string")
    if value != value.strip() or "\\" in value or ":" in value or ".." in value:
        raise SafePathError("UNSAFE_PATH", f"{label} is not a canonical relative path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or value.startswith(("/", "//"))
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or str(posix) != value
    ):
        raise SafePathError("UNSAFE_PATH", f"{label} is not a canonical relative path")
    for part in posix.parts:
        if part.endswith((".", " ")):
            raise SafePathError("UNSAFE_PATH", f"{label} contains a noncanonical segment")
        try:
            _reject_windows_reserved(part, label)
        except ValueError as exc:
            raise SafePathError("UNSAFE_PATH", str(exc)) from exc
    return value


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    return stat.S_ISLNK(stat_result.st_mode) or bool(attributes & _REPARSE_POINT)


def _reject_reparse_ancestors(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if not os.path.lexists(current):
            continue
        try:
            current_stat = os.lstat(current)
        except OSError as exc:
            raise SafePathError("PATH_INACCESSIBLE", "path component is inaccessible") from exc
        if _is_reparse(current_stat):
            raise SafePathError("REPARSE_POINT", "path contains a reparse component")
        if current != absolute and not stat.S_ISDIR(current_stat.st_mode):
            raise SafePathError("NOT_DIRECTORY", "path ancestor is not a directory")
    return absolute


def canonical_directory(path: str | os.PathLike[str], *, create: bool = False) -> Path:
    root = _reject_reparse_ancestors(Path(path))
    if create:
        root.mkdir(parents=True, exist_ok=True)
        root = _reject_reparse_ancestors(root)
    try:
        before = os.lstat(root)
    except OSError as exc:
        raise SafePathError("ROOT_UNAVAILABLE", "allowed root is unavailable") from exc
    if _is_reparse(before) or not stat.S_ISDIR(before.st_mode):
        raise SafePathError("UNSAFE_ROOT", "allowed root must be a regular directory")
    try:
        resolved = root.resolve(strict=True)
        after = os.lstat(resolved)
    except OSError as exc:
        raise SafePathError("ROOT_UNAVAILABLE", "allowed root cannot be resolved") from exc
    if _is_reparse(after) or not stat.S_ISDIR(after.st_mode):
        raise SafePathError("UNSAFE_ROOT", "resolved root must be a regular directory")
    return resolved


def create_exclusive_directory(path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    parent = canonical_directory(target.parent, create=True)
    name = validate_relative_path(target.name, label="output directory")
    if len(PurePosixPath(name).parts) != 1:
        raise SafePathError("UNSAFE_OUTPUT", "output directory must be a direct child")
    candidate = parent / name
    candidate.mkdir(exist_ok=False)
    return canonical_directory(candidate)


def safe_output_path(root: str | os.PathLike[str], relative: object) -> Path:
    canonical_root = canonical_directory(root)
    safe_relative = validate_relative_path(relative, label="output path")
    candidate = canonical_root.joinpath(*PurePosixPath(safe_relative).parts)
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise SafePathError("OUTPUT_PARENT_UNAVAILABLE", "output parent is unavailable") from exc
    if resolved_parent != canonical_root:
        raise SafePathError("UNSAFE_OUTPUT", "output must be a direct child of its exact root")
    if candidate.exists() or candidate.is_symlink():
        raise FileExistsError(candidate)
    return candidate


def _rooted_target(root: Path, relative: object) -> Path:
    safe_relative = validate_relative_path(relative)
    target = root.joinpath(*PurePosixPath(safe_relative).parts)
    current = root
    for part in PurePosixPath(safe_relative).parts:
        current = current / part
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError as exc:
            raise SafePathError("FILE_MISSING", "bounded file is missing") from exc
        except OSError as exc:
            raise SafePathError("FILE_INACCESSIBLE", "bounded file is inaccessible") from exc
        if _is_reparse(current_stat):
            raise SafePathError("REPARSE_POINT", "bounded path contains a reparse point")
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise SafePathError("FILE_INACCESSIBLE", "bounded file cannot be resolved") from exc
    if not resolved.is_relative_to(root):
        raise SafePathError("PATH_ESCAPE", "bounded file escapes its allowed root")
    return target


def _opened_file_path(descriptor: int) -> Path | None:
    """Resolve an already-open file before consuming bytes when the OS supports it."""

    if os.name == "nt":
        import ctypes
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        get_final_path.restype = ctypes.c_ulong
        handle = msvcrt.get_osfhandle(descriptor)
        required = get_final_path(handle, None, 0, 0)
        if not required:
            raise SafePathError("FILE_OPEN_FAILED", "opened target identity is unavailable")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = get_final_path(handle, buffer, len(buffer), 0)
        if not written or written >= len(buffer):
            raise SafePathError("FILE_OPEN_FAILED", "opened target identity is unavailable")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return Path(os.path.abspath(value))
    descriptor_link = Path(f"/proc/self/fd/{descriptor}")
    if descriptor_link.exists():
        try:
            return descriptor_link.resolve(strict=True)
        except OSError as exc:
            raise SafePathError(
                "FILE_OPEN_FAILED", "opened target identity is unavailable"
            ) from exc
    if sys.platform == "darwin":
        return None
    return None


def bounded_file_digest(
    root: str | os.PathLike[str],
    relative: object,
    *,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> BoundedFileDigest:
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not 0 < max_bytes <= MAX_CONFIGURABLE_FILE_BYTES
    ):
        raise SafePathError("INVALID_SIZE_CAP", "bounded read size cap is invalid")
    canonical_root = canonical_directory(root)
    target = _rooted_target(canonical_root, relative)
    try:
        before = os.lstat(target)
    except OSError as exc:
        raise SafePathError("FILE_INACCESSIBLE", "bounded file is inaccessible") from exc
    if _is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise SafePathError("NOT_REGULAR", "bounded target is not a regular file")
    if before.st_size > max_bytes:
        raise SafePathError("FILE_TOO_LARGE", "bounded target exceeds the size cap")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except OSError as exc:
        raise SafePathError(
            "FILE_OPEN_FAILED", "bounded target could not be opened safely"
        ) from exc
    digest = hashlib.sha256()
    consumed = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SafePathError("NOT_REGULAR", "opened target is not a regular file")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise SafePathError("FILE_CHANGED", "bounded target changed during open")
        if opened.st_size > max_bytes:
            raise SafePathError("FILE_TOO_LARGE", "opened target exceeds the size cap")
        opened_path = _opened_file_path(descriptor)
        if opened_path is None:
            raise SafePathError(
                "FILE_OPEN_FAILED", "opened target identity cannot be verified safely"
            )
        if not opened_path.is_relative_to(canonical_root):
            raise SafePathError("PATH_ESCAPE", "opened target escapes its allowed root")
        while True:
            block = os.read(descriptor, min(1024 * 1024, max_bytes - consumed + 1))
            if not block:
                break
            consumed += len(block)
            if consumed > max_bytes:
                raise SafePathError("FILE_TOO_LARGE", "bounded target exceeded the size cap")
            digest.update(block)
        try:
            opened_path_after = _opened_file_path(descriptor)
            resolved_after = target.resolve(strict=True)
            path_after = os.lstat(target)
        except OSError as exc:
            raise SafePathError("FILE_CHANGED", "bounded target changed during hashing") from exc
        if (
            not resolved_after.is_relative_to(canonical_root)
            or (
                opened_path_after is None
                or not opened_path_after.is_relative_to(canonical_root)
            )
            or _is_reparse(path_after)
            or (path_after.st_dev, path_after.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise SafePathError("FILE_CHANGED", "bounded target changed during hashing")
        after = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise SafePathError("FILE_CHANGED", "bounded target changed during hashing")
    finally:
        os.close(descriptor)
    return BoundedFileDigest(digest.hexdigest(), consumed)
