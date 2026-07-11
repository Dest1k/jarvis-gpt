from __future__ import annotations

import contextlib
import os
import stat
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

FileIdentity = tuple[int, int]


def metadata_identity(metadata: os.stat_result) -> FileIdentity:
    """Return the stable filesystem identity exposed by the host kernel."""

    return metadata.st_dev, metadata.st_ino


def _descriptor_identity(descriptor: int) -> FileIdentity:
    if os.name != "nt":
        return metadata_identity(os.fstat(descriptor))
    import msvcrt

    attributes, _identity = _windows_handle_information(msvcrt.get_osfhandle(descriptor))
    if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError("refusing to open a Windows reparse point")
    return metadata_identity(os.fstat(descriptor))


@dataclass(slots=True)
class BoundPath:
    """A pathname whose parent is anchored by an open, no-follow directory handle."""

    path: Path
    parent_fd: int | None
    name: str
    windows_parent_handle: int | None = None
    windows_parent_identity: FileIdentity | None = None
    windows_anchor_path: Path | None = None
    expected_identity: FileIdentity | None = None

    def sibling(self, name: str) -> BoundPath:
        if not name or Path(name).name != name:
            raise ValueError("sibling name must be one path component")
        return BoundPath(
            self.path.parent / name,
            self.parent_fd,
            name,
            self.windows_parent_handle,
            self.windows_parent_identity,
            self.windows_anchor_path,
        )

    def _validate_parent(self) -> None:
        if os.name != "nt" or self.windows_parent_handle is None:
            return
        if _windows_handle_identity(self.windows_parent_handle) != (self.windows_parent_identity):
            raise RuntimeError("pinned Windows parent handle identity changed")
        current = _open_windows_directory(self.windows_anchor_path or self.path.parent)
        try:
            if _windows_handle_identity(current) != self.windows_parent_identity:
                raise RuntimeError("filesystem parent identity changed during mutation")
        finally:
            _close_windows_handles((current,))

    def lstat(self) -> os.stat_result:
        self._validate_parent()
        if self.parent_fd is None:
            return self.path.lstat()
        return os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)

    def exists(self) -> bool:
        try:
            self.lstat()
        except FileNotFoundError:
            return False
        return True

    def open(self, flags: int, mode: int = 0o666) -> int:
        self._validate_parent()
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if self.parent_fd is None:
            descriptor = os.open(self.path, flags | nofollow, mode)
        else:
            descriptor = os.open(self.name, flags | nofollow, mode, dir_fd=self.parent_fd)
        try:
            self._validate_parent()
            descriptor_identity = _descriptor_identity(descriptor)
            metadata = self.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("refusing to open a symbolic link or reparse point")
            if descriptor_identity != metadata_identity(metadata):
                raise RuntimeError("filesystem object identity changed while opening")
            if self.expected_identity is not None and descriptor_identity != self.expected_identity:
                raise RuntimeError("filesystem object identity changed after it was pinned")
            self.expected_identity = descriptor_identity
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def mkdir(self, mode: int = 0o777) -> None:
        self._validate_parent()
        if self.parent_fd is None:
            self.path.mkdir(mode=mode)
        else:
            os.mkdir(self.name, mode=mode, dir_fd=self.parent_fd)
        self._validate_parent()
        self.expected_identity = metadata_identity(self.lstat())

    def unlink(self) -> None:
        self._validate_parent()
        self._validate_expected_identity()
        if self.parent_fd is None:
            self.path.unlink()
        else:
            os.unlink(self.name, dir_fd=self.parent_fd)
        self._validate_parent()
        self.expected_identity = None

    def rmdir(self) -> None:
        self._validate_parent()
        self._validate_expected_identity()
        if self.parent_fd is None:
            self.path.rmdir()
        else:
            os.rmdir(self.name, dir_fd=self.parent_fd)
        self._validate_parent()
        self.expected_identity = None

    def replace_from(self, source: BoundPath) -> None:
        self._validate_parent()
        source._validate_parent()
        source._validate_expected_identity()
        source_identity = metadata_identity(source.lstat())
        if os.name == "nt":
            source_identity = _windows_rename_by_handle(
                source.path,
                self.path,
                expected_identity=source.expected_identity,
            )
        elif self.parent_fd is None or source.parent_fd is None:
            os.replace(source.path, self.path)
        else:
            os.replace(
                source.name,
                self.name,
                src_dir_fd=source.parent_fd,
                dst_dir_fd=self.parent_fd,
            )
        self._validate_parent()
        source._validate_parent()
        self.expected_identity = source_identity
        source.expected_identity = None

    def link_from(self, source: BoundPath) -> None:
        self._validate_parent()
        source._validate_parent()
        source._validate_expected_identity()
        source_descriptor: int | None = None
        if os.name == "nt":
            source_descriptor = source.open(os.O_RDONLY | getattr(os, "O_BINARY", 0))
        if self.parent_fd is None or source.parent_fd is None:
            try:
                os.link(source.path, self.path, follow_symlinks=False)
            finally:
                if source_descriptor is not None:
                    os.close(source_descriptor)
        else:
            os.link(
                source.name,
                self.name,
                src_dir_fd=source.parent_fd,
                dst_dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
        self._validate_parent()
        source._validate_parent()
        self.expected_identity = source.expected_identity or metadata_identity(source.lstat())

    def chmod(self, mode: int) -> None:
        self._validate_parent()
        if os.name == "nt":
            self.expected_identity = _windows_chmod_by_handle(
                self.path,
                mode,
                expected_identity=self.expected_identity,
            )
        elif self.parent_fd is None:
            os.chmod(self.path, mode, follow_symlinks=False)
        else:
            os.chmod(
                self.name,
                mode,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
        self._validate_parent()

    def _validate_expected_identity(self) -> None:
        if self.expected_identity is None:
            return
        if metadata_identity(self.lstat()) != self.expected_identity:
            raise RuntimeError("filesystem object identity changed after it was pinned")


@dataclass(frozen=True, slots=True)
class DirectoryEntrySnapshot:
    name: str
    kind: str
    size: int
    mtime_ns: int


class PathMutationGuard:
    """Pins allowed-root ancestry and supplies handle-relative path operations.

    On POSIX every lookup below an allowed root is performed with ``openat``
    semantics and ``O_NOFOLLOW``. Directory descriptors are retained for the
    lifetime of the transaction, so a concurrent rename cannot redirect later
    checkpoint, mutation, or rollback operations. On Windows, where Python has
    no ``dir_fd`` API, every directory from the volume root to the target is
    opened without ``FILE_SHARE_DELETE``. Windows then rejects renames,
    junction replacement, and deletion until the guard is released.
    """

    def __init__(
        self,
        roots: tuple[Path, ...],
        denied: tuple[Path, ...],
        root_identities: dict[Path, FileIdentity],
    ) -> None:
        self._roots = roots
        self._denied = denied
        self._root_identities = root_identities
        self._posix_fds: dict[tuple[Path, tuple[str, ...]], int] = {}
        self._windows_handles: dict[str, int] = {}
        self._lock = threading.RLock()
        self._closed = False
        for root in roots:
            self._pin_root(root)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if os.name == "nt":
                _close_windows_handles(tuple(self._windows_handles.values()))
                self._windows_handles.clear()
            else:
                for descriptor in reversed(tuple(self._posix_fds.values())):
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
                self._posix_fds.clear()

    def __enter__(self) -> PathMutationGuard:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def bind(
        self,
        path: Path,
        *,
        create_parents: bool = False,
        allow_root: bool = False,
    ) -> BoundPath:
        with self._lock:
            if self._closed:
                raise RuntimeError("filesystem mutation guard is closed")
            candidate, root, relative = self._lexical_path(path, allow_root=allow_root)
            parent_parts = relative.parts[:-1]
            if os.name == "nt":
                parent = self._pin_windows_chain(root, parent_parts, create_parents)
                handle = self._windows_handles[os.path.normcase(os.fspath(parent))]
                return BoundPath(
                    candidate,
                    None,
                    candidate.name if relative.parts else ".",
                    handle,
                    _windows_handle_identity(handle),
                    parent,
                )
            parent_fd = self._pin_posix_chain(root, parent_parts, create_parents)
            name = relative.parts[-1] if relative.parts else "."
            return BoundPath(candidate, parent_fd, name)

    def temporary_sibling(self, target: BoundPath, name: str) -> BoundPath:
        return target.sibling(name)

    def release_descendants(self, path: Path) -> None:
        """Release cached handles at/below a rollback target, retaining its parent."""

        with self._lock:
            candidate, _root, _relative = self._lexical_path(path, allow_root=True)
            if os.name == "nt":
                keys = [
                    key
                    for key in self._windows_handles
                    if Path(key) == candidate or Path(key).is_relative_to(candidate)
                ]
                for key in sorted(keys, key=len, reverse=True):
                    _close_windows_handles((self._windows_handles.pop(key),))
                return
            keys = [
                key
                for key in self._posix_fds
                if key[0].joinpath(*key[1]) == candidate
                or key[0].joinpath(*key[1]).is_relative_to(candidate)
            ]
            for key in sorted(keys, key=lambda item: len(item[1]), reverse=True):
                os.close(self._posix_fds.pop(key))

    def _lexical_path(self, path: Path, *, allow_root: bool) -> tuple[Path, Path, Path]:
        if not path.is_absolute() or "\x00" in os.fspath(path):
            raise ValueError("action paths must be absolute and cannot contain NUL")
        candidate = Path(os.path.abspath(os.path.normpath(path)))
        root = next(
            (item for item in self._roots if candidate == item or candidate.is_relative_to(item)),
            None,
        )
        if root is None:
            raise ValueError(f"path escapes allowed roots: {path}")
        if any(candidate == denied or candidate.is_relative_to(denied) for denied in self._denied):
            raise PermissionError(f"path is reserved for runtime secrets or state: {path}")
        if candidate == root and not allow_root:
            raise ValueError("the allowed root itself cannot be mutated")
        return candidate, root, candidate.relative_to(root)

    def _pin_root(self, root: Path) -> None:
        if os.name == "nt":
            self._pin_windows_chain(root, (), False)
            metadata = root.stat(follow_symlinks=False)
        else:
            descriptor = self._pin_posix_chain(root, (), False)
            metadata = os.fstat(descriptor)
        if metadata_identity(metadata) != self._root_identities[root]:
            raise RuntimeError(f"allowed root identity changed: {root}")

    def _pin_posix_chain(
        self,
        root: Path,
        relative_parts: tuple[str, ...],
        create: bool,
    ) -> int:
        root_key = (root, ())
        descriptor = self._posix_fds.get(root_key)
        if descriptor is None:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(root, flags)
            self._posix_fds[root_key] = descriptor
        prefix: tuple[str, ...] = ()
        for component in relative_parts:
            prefix = (*prefix, component)
            key = (root, prefix)
            cached = self._posix_fds.get(key)
            if cached is not None:
                descriptor = cached
                continue
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o777, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise NotADirectoryError(component)
            self._posix_fds[key] = child
            descriptor = child
        return descriptor

    def _pin_windows_chain(
        self,
        root: Path,
        relative_parts: tuple[str, ...],
        create: bool,
    ) -> Path:
        # Pin the full ancestry, not merely the configured root: otherwise an
        # ancestor of the root could be renamed and the absolute path recreated.
        current = Path(root.anchor)
        self._pin_windows_directory(current)
        for component in root.parts[1:]:
            current /= component
            self._pin_windows_directory(current)
        for component in relative_parts:
            current /= component
            try:
                self._pin_windows_directory(current)
            except FileNotFoundError:
                if not create:
                    raise
                current.mkdir()
                self._pin_windows_directory(current)
        return current

    def _pin_windows_directory(self, path: Path) -> None:
        key = os.path.normcase(os.fspath(path))
        if key in self._windows_handles:
            return
        handle = _open_windows_directory(path)
        self._windows_handles[key] = handle


def verified_binary_handle(bound: BoundPath) -> BinaryIO:
    descriptor = bound.open(os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        handle = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    descriptor_metadata = os.fstat(handle.fileno())
    try:
        path_metadata = bound.lstat()
    except BaseException:
        handle.close()
        raise
    if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(descriptor_metadata.st_mode):
        handle.close()
        raise ValueError("file became a symlink or non-regular object")
    if metadata_identity(descriptor_metadata) != metadata_identity(path_metadata):
        handle.close()
        raise RuntimeError("file identity changed while it was being opened")
    return handle


def directory_entries(
    bound: BoundPath, *, max_entries: int | None = None
) -> tuple[DirectoryEntrySnapshot, ...]:
    """Enumerate a directory through its opened kernel object, never its name."""

    bound._validate_parent()
    if os.name == "nt":
        handle = _open_windows_directory(bound.path)
        try:
            return _windows_directory_entries(handle, max_entries=max_entries)
        finally:
            _close_windows_handles((handle,))
    with pinned_directory(bound) as descriptor:
        if descriptor is None:  # pragma: no cover - Windows returned above.
            raise AssertionError("POSIX directory descriptor is unavailable")
        with os.scandir(descriptor) as iterator:
            result = []
            for entry in iterator:
                metadata = entry.stat(follow_symlinks=False)
                result.append(
                    DirectoryEntrySnapshot(
                        name=entry.name,
                        kind=(
                            "symlink"
                            if stat.S_ISLNK(metadata.st_mode)
                            else "directory"
                            if stat.S_ISDIR(metadata.st_mode)
                            else "file"
                        ),
                        size=metadata.st_size,
                        mtime_ns=metadata.st_mtime_ns,
                    )
                )
                if max_entries is not None and len(result) >= max_entries:
                    break
        return tuple(result)


@contextlib.contextmanager
def pinned_directory(bound: BoundPath) -> Iterator[int | None]:
    """Pin one directory identity while callers enumerate its absolute name."""

    if os.name == "nt":
        handle = _open_windows_directory(bound.path)
        try:
            yield None
        finally:
            _close_windows_handles((handle,))
        return
    descriptor = bound.open(os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = bound.lstat()
        if not stat.S_ISDIR(descriptor_metadata.st_mode):
            raise NotADirectoryError(bound.path)
        if metadata_identity(descriptor_metadata) != metadata_identity(path_metadata):
            raise RuntimeError("directory identity changed while it was being opened")
        yield descriptor
    finally:
        os.close(descriptor)


def _open_windows_directory(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    file_list_directory = 0x0001
    file_read_attributes = 0x0080
    share_read = 0x00000001
    share_write = 0x00000002
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    handle = create_file(
        os.fspath(path),
        file_list_directory | file_read_attributes,
        share_read | share_write,
        None,
        open_existing,
        backup_semantics | open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(error, os.strerror(error), os.fspath(path))
        raise OSError(error, os.strerror(error), os.fspath(path))
    attributes, _identity = _windows_handle_information(int(handle))
    if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)
        raise ValueError(f"directory ancestry contains a reparse point: {path}")
    return int(handle)


def _windows_rename_by_handle(
    source: Path,
    destination: Path,
    *,
    expected_identity: FileIdentity | None,
) -> FileIdentity:
    """Atomically rename one pinned non-reparse object by its kernel handle."""

    import ctypes
    from ctypes import wintypes

    destination_name = os.fspath(destination)
    if not destination.is_absolute():
        raise ValueError("Windows rename destination must be absolute")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    delete_access = 0x00010000
    file_read_attributes = 0x0080
    share_read = 0x00000001
    share_write = 0x00000002
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    handle = create_file(
        os.fspath(source),
        delete_access | file_read_attributes,
        share_read | share_write,
        None,
        open_existing,
        backup_semantics | open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error), os.fspath(source))
    try:
        attributes, _native_identity = _windows_handle_information(int(handle))
        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("refusing to rename a Windows reparse point")
        source_metadata = source.lstat()
        if stat.S_ISLNK(source_metadata.st_mode):
            raise ValueError("refusing to rename a Windows reparse point")
        identity = metadata_identity(source_metadata)
        if expected_identity is not None and identity != expected_identity:
            raise RuntimeError("rename source identity changed after it was pinned")

        class FileRenameInformation(ctypes.Structure):
            _fields_ = (
                ("replace_if_exists", wintypes.BOOL),
                ("root_directory", wintypes.HANDLE),
                ("file_name_length", wintypes.DWORD),
                ("file_name", wintypes.WCHAR * (len(destination_name) + 1)),
            )

        information = FileRenameInformation()
        information.replace_if_exists = True
        information.root_directory = None
        information.file_name_length = len(destination_name.encode("utf-16-le"))
        information.file_name = destination_name
        set_information = kernel32.SetFileInformationByHandle
        set_information.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        set_information.restype = wintypes.BOOL
        if not set_information(
            handle,
            3,  # FileRenameInfo
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error), os.fspath(source))
        return identity
    finally:
        _close_windows_handles((int(handle),))


def _windows_chmod_by_handle(
    path: Path,
    mode: int,
    *,
    expected_identity: FileIdentity | None,
) -> FileIdentity:
    """Apply Windows readonly semantics while an exact non-reparse object is pinned."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    file_read_attributes = 0x0080
    file_write_attributes = 0x0100
    share_read = 0x00000001
    share_write = 0x00000002
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    handle = create_file(
        os.fspath(path),
        file_read_attributes | file_write_attributes,
        share_read | share_write,
        None,
        open_existing,
        backup_semantics | open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error), os.fspath(path))
    try:
        attributes, _native_identity = _windows_handle_information(int(handle))
        if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("refusing to chmod a Windows reparse point")
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("refusing to chmod a Windows reparse point")
        identity = metadata_identity(metadata)
        if expected_identity is not None and identity != expected_identity:
            raise RuntimeError("chmod target identity changed after it was pinned")
        readonly = stat.FILE_ATTRIBUTE_READONLY
        updated_attributes = (
            attributes & ~readonly if mode & stat.S_IWRITE else attributes | readonly
        )
        set_attributes = kernel32.SetFileAttributesW
        set_attributes.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
        set_attributes.restype = wintypes.BOOL
        if not set_attributes(os.fspath(path), updated_attributes):
            error = ctypes.get_last_error()
            raise OSError(error, os.strerror(error), os.fspath(path))
        return identity
    finally:
        _close_windows_handles((int(handle),))


def _close_windows_handles(handles: tuple[int, ...]) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    for handle in reversed(handles):
        close_handle(handle)


def _windows_handle_identity(handle: int) -> FileIdentity:
    _attributes, identity = _windows_handle_information(handle)
    return identity


def _windows_handle_information(handle: int) -> tuple[int, FileIdentity]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        )

    get_information = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    information = ByHandleFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error))
    file_index = (information.file_index_high << 32) | information.file_index_low
    return information.file_attributes, (
        information.volume_serial_number,
        file_index,
    )


def _windows_directory_entries(
    handle: int, *, max_entries: int | None
) -> tuple[DirectoryEntrySnapshot, ...]:
    import ctypes
    from ctypes import wintypes

    get_information = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    file_id_both_directory_info = 10
    file_id_both_directory_restart_info = 11
    no_more_files = 18
    buffer_size = 64 * 1024
    result: list[DirectoryEntrySnapshot] = []
    information_class = file_id_both_directory_restart_info
    while True:
        buffer = ctypes.create_string_buffer(buffer_size)
        if not get_information(handle, information_class, buffer, buffer_size):
            error = ctypes.get_last_error()
            if error == no_more_files:
                break
            raise OSError(error, os.strerror(error))
        information_class = file_id_both_directory_info
        raw = memoryview(buffer).cast("B")
        offset = 0
        while True:
            next_offset = int.from_bytes(raw[offset : offset + 4], "little")
            end_of_file = int.from_bytes(raw[offset + 40 : offset + 48], "little")
            attributes = int.from_bytes(raw[offset + 56 : offset + 60], "little")
            filename_length = int.from_bytes(raw[offset + 60 : offset + 64], "little")
            last_write_ticks = int.from_bytes(raw[offset + 24 : offset + 32], "little")
            filename = bytes(raw[offset + 104 : offset + 104 + filename_length]).decode("utf-16-le")
            if filename not in {".", ".."}:
                kind = (
                    "symlink"
                    if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    else "directory"
                    if attributes & stat.FILE_ATTRIBUTE_DIRECTORY
                    else "file"
                )
                unix_mtime_ns = max(
                    0,
                    (last_write_ticks - 116_444_736_000_000_000) * 100,
                )
                result.append(
                    DirectoryEntrySnapshot(
                        name=filename,
                        kind=kind,
                        size=end_of_file,
                        mtime_ns=unix_mtime_ns,
                    )
                )
                if max_entries is not None and len(result) >= max_entries:
                    return tuple(result)
            if next_offset == 0:
                break
            offset += next_offset
    return tuple(result)
