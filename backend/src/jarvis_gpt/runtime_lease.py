from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType


class RuntimeLeaseError(RuntimeError):
    """Raised when another primary Jarvis runtime owns the state directory."""


class PrimaryRuntimeLease:
    """Crash-safe, host-local single-primary lease backed by an OS file lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None
        self._directory_descriptor: int | None = None
        self._anchor_socket: socket.socket | None = None
        self._lock = threading.Lock()

    @property
    def acquired(self) -> bool:
        with self._lock:
            return self._descriptor is not None

    def acquire(self) -> None:
        with self._lock:
            if self._descriptor is not None:
                return
            anchor_socket = self._acquire_linux_anchor()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except BaseException:
                if anchor_socket is not None:
                    anchor_socket.close()
                raise
            directory_descriptor: int | None = None
            if os.name != "nt":
                directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_flags |= getattr(os, "O_CLOEXEC", 0)
                try:
                    directory_descriptor = os.open(self.path.parent, directory_flags)
                except BaseException:
                    if anchor_socket is not None:
                        anchor_socket.close()
                    raise
                try:
                    self._lock_directory_descriptor(directory_descriptor)
                except BaseException:
                    os.close(directory_descriptor)
                    if anchor_socket is not None:
                        anchor_socket.close()
                    raise
            if self.path.is_symlink():
                if directory_descriptor is not None:
                    self._unlock_directory_descriptor(directory_descriptor)
                    os.close(directory_descriptor)
                if anchor_socket is not None:
                    anchor_socket.close()
                raise RuntimeLeaseError(f"primary runtime lease cannot be a symlink: {self.path}")
            flags = os.O_RDWR | os.O_CREAT
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            if no_follow:
                flags |= no_follow
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except BaseException:
                if directory_descriptor is not None:
                    self._unlock_directory_descriptor(directory_descriptor)
                    os.close(directory_descriptor)
                if anchor_socket is not None:
                    anchor_socket.close()
                raise
            try:
                self._lock_descriptor(descriptor)
                metadata = json.dumps(
                    {
                        "protocol": "jarvis.primary-runtime-lease.v1",
                        "pid": os.getpid(),
                        "acquired_at": datetime.now(UTC).isoformat(),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8") + b"\n"
                os.ftruncate(descriptor, 0)
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, metadata)
                os.fsync(descriptor)
            except BaseException:
                os.close(descriptor)
                if directory_descriptor is not None:
                    self._unlock_directory_descriptor(directory_descriptor)
                    os.close(directory_descriptor)
                if anchor_socket is not None:
                    anchor_socket.close()
                raise
            self._descriptor = descriptor
            self._directory_descriptor = directory_descriptor
            self._anchor_socket = anchor_socket

    def release(self) -> None:
        with self._lock:
            descriptor = self._descriptor
            if descriptor is None:
                return
            directory_descriptor = self._directory_descriptor
            anchor_socket = self._anchor_socket
            self._descriptor = None
            self._directory_descriptor = None
            self._anchor_socket = None
            try:
                self._unlock_descriptor(descriptor)
            finally:
                try:
                    os.close(descriptor)
                finally:
                    if directory_descriptor is not None:
                        try:
                            self._unlock_directory_descriptor(directory_descriptor)
                        finally:
                            os.close(directory_descriptor)
                    if anchor_socket is not None:
                        anchor_socket.close()

    def _acquire_linux_anchor(self) -> socket.socket | None:
        if not sys.platform.startswith("linux"):
            return None
        identity = os.path.normcase(os.path.abspath(self.path))
        digest = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()
        address = f"\0jarvis-gpt.primary.{os.getuid()}.{digest}"
        anchor = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            anchor.bind(address)
        except OSError as exc:
            anchor.close()
            raise RuntimeLeaseError(
                f"another Jarvis primary runtime owns {self.path}"
            ) from exc
        return anchor

    def _lock_descriptor(self, descriptor: int) -> None:
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeLeaseError(
                f"another Jarvis primary runtime owns {self.path}"
            ) from exc

    @staticmethod
    def _unlock_descriptor(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _lock_directory_descriptor(self, descriptor: int) -> None:
        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeLeaseError(
                f"another Jarvis primary runtime owns {self.path.parent}"
            ) from exc

    @staticmethod
    def _unlock_directory_descriptor(descriptor: int) -> None:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)

    def __enter__(self) -> PrimaryRuntimeLease:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.release()
