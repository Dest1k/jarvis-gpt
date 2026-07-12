from __future__ import annotations

import gzip
import tarfile
import zipfile
from pathlib import Path

import pytest

from jarvis_gpt.archive_runtime import (
    ArchiveSafetyError,
    create_archive,
    extract_archive,
    list_archive,
    read_archive_member,
)
from jarvis_gpt.document_surfer import DocumentSurferConfig, JarvisDocumentSurfer
from jarvis_gpt.file_types import identify_bytes, identify_path


def test_identify_magic_pdf_and_zip(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    info = identify_path(pdf)
    assert info.kind == "pdf"
    assert info.is_document is True
    assert info.source == "magic"

    zpath = tmp_path / "pack.zip"
    with zipfile.ZipFile(zpath, "w") as archive:
        archive.writestr("readme.txt", "hello archive")
    zinfo = identify_path(zpath)
    assert zinfo.kind in {"zip"}
    assert zinfo.is_archive is True


def test_identify_docx_by_zip_content(tmp_path: Path) -> None:
    path = tmp_path / "letter.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
        archive.writestr("[Content_Types].xml", "<Types/>")
    info = identify_path(path)
    assert info.kind == "docx"
    assert info.is_document is True


def test_identify_tar_gz_extension_and_magic(tmp_path: Path) -> None:
    inner = tmp_path / "payload.txt"
    inner.write_text("payload", encoding="utf-8")
    tar_path = tmp_path / "bundle.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        archive.add(inner, arcname="payload.txt")
    info = identify_path(tar_path)
    assert info.kind in {"tar.gz", "gz"}
    assert info.is_archive is True


def test_identify_text_content_without_extension() -> None:
    info = identify_bytes(b'{"a": 1, "b": 2}\n', name="data")
    assert info.kind == "json"
    assert info.is_text is True


def test_zip_list_extract_read_and_search(tmp_path: Path) -> None:
    archive_path = tmp_path / "docs.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("notes/a.txt", "alpha contract value")
        archive.writestr("notes/b.txt", "beta clause")
    listing = list_archive(archive_path)
    names = {item["name"] for item in listing["members"]}
    assert "notes/a.txt" in names

    out = tmp_path / "out"
    extracted = extract_archive(archive_path, output_dir=out, members=["notes/a.txt"])
    assert extracted["extracted_count"] == 1
    assert (out / "notes" / "a.txt").read_text(encoding="utf-8") == "alpha contract value"

    member = read_archive_member(archive_path, "notes/a.txt")
    assert "alpha" in member["text_preview"]
    assert member["type"]["kind"] in {"txt", "text"}

    surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=tmp_path / "s-out"))
    hits = surfer.search_archive(archive_path, "contract")
    assert hits["hit_count"] >= 1


def test_tar_gz_roundtrip(tmp_path: Path) -> None:
    source = tmp_path / "one.txt"
    source.write_text("one", encoding="utf-8")
    archive_path = tmp_path / "one.tar.gz"
    created = create_archive([source], output_path=archive_path, archive_format="tar.gz")
    assert Path(created["path"]).exists()
    listing = list_archive(archive_path)
    assert any(item["name"] == "one.txt" for item in listing["members"])


def test_gz_stream_list_and_extract(tmp_path: Path) -> None:
    raw = tmp_path / "log.txt"
    raw.write_text("line-1\nline-2\n", encoding="utf-8")
    gz_path = tmp_path / "log.txt.gz"
    with raw.open("rb") as src, gzip.open(gz_path, "wb") as out:
        out.write(src.read())
    listing = list_archive(gz_path)
    assert listing["kind"] == "gz"
    out_dir = tmp_path / "gz-out"
    extracted = extract_archive(gz_path, output_dir=out_dir)
    assert extracted["extracted_count"] == 1
    assert "line-1" in Path(extracted["extracted"][0]["path"]).read_text(encoding="utf-8")


def test_path_traversal_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.txt", "nope")
    listing = list_archive(archive_path)
    assert any(item.get("unsafe") for item in listing["members"])
    with pytest.raises(ArchiveSafetyError):
        extract_archive(archive_path, output_dir=tmp_path / "x")


def test_surfer_identify_probe_and_create(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("hello", encoding="utf-8")
    b.write_text("world", encoding="utf-8")
    surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=tmp_path / "out"))
    identity = surfer.identify(a)
    assert identity["type"]["kind"] == "txt"
    probe = surfer.probe(a)
    assert probe["document"] is not None
    created = surfer.create_archive([a, b], archive_format="zip", output_name="pair.zip")
    assert Path(created["path"]).exists()
    listing = surfer.list_archive(created["path"])
    assert listing["member_count"] >= 2


def test_surfer_inspect_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("x.txt", "x")
    surfer = JarvisDocumentSurfer(DocumentSurferConfig(output_dir=tmp_path / "out"))
    inspected = surfer.inspect(archive_path)
    assert inspected["archive"] is not None
    assert inspected["capabilities"]["kind"] == "archive"
