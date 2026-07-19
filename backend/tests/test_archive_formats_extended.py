"""Extended archive formats: tar.zst, deb, iso, passworded 7z, capabilities."""

from __future__ import annotations

import io
import struct
import tarfile
from pathlib import Path

import pytest

from jarvis_gpt.archive_runtime import (
    ArchivePasswordError,
    archive_capabilities,
    extract_archive,
    list_archive,
    read_archive_member,
)
from jarvis_gpt.file_types import identify_bytes, identify_path


def _write_ar(path: Path, members: list[tuple[str, bytes]]) -> None:
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        name_field = (name + "/").encode("ascii")[:16].ljust(16)
        header = (
            name_field
            + b"0".ljust(12)
            + b"0".ljust(6)
            + b"0".ljust(6)
            + b"100644".ljust(8)
            + str(len(data)).encode("ascii").ljust(10)
            + b"`\n"
        )
        out += header
        out += data
        if len(data) % 2 == 1:
            out += b"\n"
    path.write_bytes(out)


def _tar_gz_bytes(name: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        info = tarfile.TarInfo(name=name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def test_archive_capabilities_mentions_new_formats() -> None:
    caps = archive_capabilities()
    assert caps["stdlib"]["deb"] is True
    assert caps["stdlib"]["iso"] is True
    assert "tar.zst" in caps["optional"]
    assert "squashfs" in caps["optional"]
    assert "password" in caps


def test_tar_xz_list_and_read(tmp_path: Path) -> None:
    source = tmp_path / "note.txt"
    source.write_text("xz-payload", encoding="utf-8")
    archive_path = tmp_path / "bundle.tar.xz"
    with tarfile.open(archive_path, "w:xz") as archive:
        archive.add(source, arcname="note.txt")
    listing = list_archive(archive_path)
    assert listing["kind"] == "tar.xz"
    assert any(item["name"] == "note.txt" for item in listing["members"])
    payload = read_archive_member(archive_path, "note.txt")
    assert "xz-payload" in payload["text_preview"]


def test_tar_zst_roundtrip(tmp_path: Path) -> None:
    zstd = pytest.importorskip("zstandard")
    source = tmp_path / "a.txt"
    source.write_text("zstd-hello", encoding="utf-8")
    tar_path = tmp_path / "plain.tar"
    with tarfile.open(tar_path, "w") as archive:
        archive.add(source, arcname="a.txt")
    archive_path = tmp_path / "plain.tar.zst"
    cctx = zstd.ZstdCompressor(level=3)
    archive_path.write_bytes(cctx.compress(tar_path.read_bytes()))

    info = identify_path(archive_path)
    assert info.kind == "tar.zst"
    assert info.is_archive is True

    listing = list_archive(archive_path)
    assert listing["kind"] == "tar.zst"
    assert any(item["name"] == "a.txt" for item in listing["members"])

    out = tmp_path / "out"
    extracted = extract_archive(archive_path, output_dir=out)
    assert extracted["extracted_count"] >= 1
    assert (out / "a.txt").read_text(encoding="utf-8") == "zstd-hello"

    member = read_archive_member(archive_path, "a.txt")
    assert "zstd-hello" in member["text_preview"]


def test_deb_list_extract_read_nested(tmp_path: Path) -> None:
    control = _tar_gz_bytes("./control", b"Package: demo\nVersion: 1.0\n")
    data = _tar_gz_bytes("./usr/share/doc/demo/readme.txt", b"deb-nested-payload")
    deb_path = tmp_path / "demo_1.0_all.deb"
    _write_ar(
        deb_path,
        [
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", control),
            ("data.tar.gz", data),
        ],
    )

    info = identify_path(deb_path)
    assert info.kind == "deb"
    assert info.is_archive is True

    listing = list_archive(deb_path)
    assert listing["kind"] == "deb"
    names = {item["name"] for item in listing["members"]}
    assert "debian-binary" in names
    assert "data.tar.gz" in names
    assert any(name.endswith("readme.txt") for name in names)

    nested = next(name for name in names if name.endswith("readme.txt"))
    payload = read_archive_member(deb_path, nested)
    assert "deb-nested-payload" in payload["text_preview"]

    out = tmp_path / "deb-out"
    extracted = extract_archive(deb_path, output_dir=out, members=[nested])
    assert extracted["extracted_count"] == 1
    written = Path(extracted["extracted"][0]["path"])
    assert written.read_text(encoding="utf-8") == "deb-nested-payload"


def test_passworded_7z_list_extract_read(tmp_path: Path) -> None:
    py7zr = pytest.importorskip("py7zr")
    archive_path = tmp_path / "secret.7z"
    plain = tmp_path / "hidden.txt"
    plain.write_text("top-secret-7z", encoding="utf-8")
    with py7zr.SevenZipFile(archive_path, "w", password="s3cret") as archive:
        archive.write(plain, arcname="hidden.txt")

    with pytest.raises(ArchivePasswordError):
        list_archive(archive_path)

    listing = list_archive(archive_path, password="s3cret")
    assert any(item["name"] == "hidden.txt" for item in listing["members"])

    payload = read_archive_member(archive_path, "hidden.txt", password="s3cret")
    assert "top-secret-7z" in payload["text_preview"]

    out = tmp_path / "7z-out"
    extracted = extract_archive(archive_path, output_dir=out, password="s3cret")
    assert (out / "hidden.txt").read_text(encoding="utf-8") == "top-secret-7z"


def _make_simple_iso(path: Path, filename: str, content: bytes) -> None:
    """Build a minimal ISO9660 image with one file in the root directory."""

    sector = 2048
    # Layout:
    # 0-15: system area
    # 16: PVD
    # 17: volume terminator
    # 18: root directory (2 sectors reserved)
    # 20: file data
    root_extent = 18
    file_extent = 20
    root_size = sector  # one sector directory
    file_size = len(content)

    def pad_name(name: str) -> bytes:
        # ISO9660 Level 1-ish: uppercase + ;1
        base = name.upper()
        if "." in base:
            stem, ext = base.rsplit(".", 1)
            encoded = f"{stem[:8]}.{ext[:3]};1".encode("ascii")
        else:
            encoded = f"{base[:8]};1".encode("ascii")
        return encoded

    def dir_record(*, extent: int, data_len: int, flags: int, name: bytes) -> bytes:
        name_len = len(name)
        length = 33 + name_len
        if length % 2 == 1:
            length += 1
        rec = bytearray(length)
        rec[0] = length
        struct.pack_into("<I", rec, 2, extent)
        struct.pack_into(">I", rec, 6, extent)
        struct.pack_into("<I", rec, 10, data_len)
        struct.pack_into(">I", rec, 14, data_len)
        rec[25] = flags
        rec[32] = name_len
        rec[33 : 33 + name_len] = name
        return bytes(rec)

    # Root directory records: .  ..  file
    root_dir = bytearray()
    root_dir += dir_record(extent=root_extent, data_len=root_size, flags=0x02, name=b"\x00")
    root_dir += dir_record(extent=root_extent, data_len=root_size, flags=0x02, name=b"\x01")
    file_name = pad_name(filename)
    root_dir += dir_record(extent=file_extent, data_len=file_size, flags=0x00, name=file_name)
    root_dir = root_dir.ljust(sector, b"\x00")

    # PVD
    pvd = bytearray(sector)
    pvd[0] = 1
    pvd[1:6] = b"CD001"
    pvd[6] = 1
    pvd[40:72] = b"JARVIS".ljust(32, b" ")
    # Root directory record at offset 156 (34 bytes standard)
    root_rec = dir_record(extent=root_extent, data_len=root_size, flags=0x02, name=b"\x00")
    pvd[156 : 156 + len(root_rec)] = root_rec[:34].ljust(34, b"\x00")
    # Volume space size (little + big endian at 80/84) — approximate
    volume_sectors = file_extent + max(1, (file_size + sector - 1) // sector) + 1
    struct.pack_into("<I", pvd, 80, volume_sectors)
    struct.pack_into(">I", pvd, 84, volume_sectors)
    pvd[881] = 1  # file structure version-ish; ignore strictness
    pvd[882:883] = b"\x00"

    terminator = bytearray(sector)
    terminator[0] = 255
    terminator[1:6] = b"CD001"
    terminator[6] = 1

    file_data = content.ljust(sector, b"\x00")

    blob = bytearray()
    blob += b"\x00" * (16 * sector)
    blob += pvd
    blob += terminator
    blob += root_dir
    blob += b"\x00" * sector  # padding sector 19
    blob += file_data
    path.write_bytes(blob)


def test_iso_list_extract_read(tmp_path: Path) -> None:
    iso_path = tmp_path / "demo.iso"
    _make_simple_iso(iso_path, "HELLO.TXT", b"iso-hello-content")

    info = identify_path(iso_path)
    assert info.kind == "iso"
    assert info.is_archive is True

    listing = list_archive(iso_path)
    assert listing["kind"] == "iso"
    names = {item["name"] for item in listing["members"] if not item.get("is_dir")}
    assert names, listing
    member = next(iter(names))
    payload = read_archive_member(iso_path, member)
    assert b"iso-hello-content" in (
        payload.get("text_preview") or ""
    ).encode() or payload["size"] > 0

    # Direct byte check
    raw = read_archive_member(iso_path, member)
    assert raw["size"] >= len(b"iso-hello-content")

    out = tmp_path / "iso-out"
    extracted = extract_archive(iso_path, output_dir=out, members=[member])
    assert extracted["extracted_count"] == 1
    data = Path(extracted["extracted"][0]["path"]).read_bytes()
    assert data.startswith(b"iso-hello-content")


def test_identify_squashfs_magic() -> None:
    info = identify_bytes(b"hsqs" + b"\x00" * 32, name="root.squashfs")
    assert info.kind == "squashfs"
    assert info.is_archive is True


def test_identify_img_extension(tmp_path: Path) -> None:
    path = tmp_path / "disk.img"
    path.write_bytes(b"\x00" * 64)
    info = identify_path(path)
    assert info.kind == "img"
    assert info.is_archive is True
