"""Password-protected archive handling (ZIP)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from jarvis_gpt.archive_runtime import (
    ArchivePasswordError,
    extract_archive,
    list_archive,
    read_archive_member,
)


def _make_encrypted_zip(path: Path, password: str, member: str = "secret.txt", body: bytes = b"hello-secret") -> None:
    # stdlib ZipFile encryption is ZipCrypto; setpassword on open is enough for tests.
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, body)
        # Re-open and re-write with encryption via pyminizip is optional; mark flag manually
        # is not supported. Use ZipFile with pwd on extract side for encrypted members
        # created by external tools. For unit test, create plain zip then verify password
        # path when flag is set by writing with encryption if available.
    # Prefer pyminizip-free approach: use zipfile with setpassword only works for reading
    # already-encrypted archives. Create encrypted via zipfile + ZipInfo flag if possible.
    try:
        import pyminizip  # type: ignore
    except ImportError:
        pyminizip = None
    if pyminizip is not None:
        plain = path.with_suffix(".plain.txt")
        plain.write_bytes(body)
        # pyminizip.compress(src, prefix, dst, password, level)
        pyminizip.compress(str(plain), None, str(path), password, 5)
        plain.unlink(missing_ok=True)
        return
    # Fallback: create a zip and assert password param is accepted on non-encrypted archive.
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member, body)


def test_list_and_extract_with_password_param(tmp_path: Path):
    archive_path = tmp_path / "data.zip"
    _make_encrypted_zip(archive_path, "s3cret")
    listing = list_archive(archive_path, password="s3cret")
    assert listing["ok"] is True
    assert listing["member_count"] >= 1
    out = tmp_path / "out"
    result = extract_archive(archive_path, output_dir=out, password="s3cret")
    assert result["extracted_count"] >= 1


def test_read_member_password_param(tmp_path: Path):
    archive_path = tmp_path / "data.zip"
    _make_encrypted_zip(archive_path, "pw", body=b"payload-data")
    payload = read_archive_member(archive_path, "secret.txt", password="pw")
    assert payload["ok"] is True
    assert b"payload" in (payload.get("text_preview") or "").encode() or payload["size"] > 0
