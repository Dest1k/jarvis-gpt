from __future__ import annotations

import io
import zipfile

import pytest
from jarvis_gpt import ingest as ingest_module
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.ingest import FileIngestor
from jarvis_gpt.storage import JarvisStorage


def _write_minimal_docx(path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
                f"{text}"
                "</w:t></w:r></w:p></w:body></w:document>"
            ),
        )


def _write_minimal_xlsx(path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            (
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/>'
                "</sheets></workbook>"
            ),
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                'relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                "</Relationships>"
            ),
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f"<si><t>{text}</t></si></sst>"
            ),
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            (
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
                '<c r="B1"><f>1+1</f><v>2</v></c></row></sheetData></worksheet>'
            ),
        )


def test_file_ingestor_indexes_text_and_records_audit(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "mission-notes.md"
    source.write_text(
        "# Mission\n\nJarvis should index architecture notes and mission context locally.",
        encoding="utf-8",
    )

    result = FileIngestor(settings=settings, storage=storage).ingest_path(source)
    duplicate = FileIngestor(settings=settings, storage=storage).ingest_path(source)
    hits = storage.search_file_chunks("architecture mission", limit=5)
    audit = storage.list_audit(target_type="file", target_id=result["file"]["id"])

    assert result["file"]["status"] == "indexed"
    assert result["chunks_indexed"] == 1
    assert duplicate["file"]["id"] == result["file"]["id"]
    assert duplicate["deduplicated"] is True
    assert storage.counters()["files"] == 1
    assert hits
    assert hits[0]["file_id"] == result["file"]["id"]
    assert hits[0]["relevance"] > 0
    assert hits[0]["matched_terms"] == ["architecture", "mission"]
    assert "architecture notes" in hits[0]["snippet"]
    assert audit[0]["action"] == "file.ingest"
    storage.close()


def test_file_ingestor_indexes_directory_with_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    root = settings.home / "docs"
    root.mkdir(parents=True)
    (root / "a.md").write_text("Jarvis directory ingestion alpha.", encoding="utf-8")
    (root / "b.txt").write_text("Jarvis directory ingestion beta.", encoding="utf-8")
    (root / "skip.bin").write_bytes(b"\x00\x01")

    result = FileIngestor(settings=settings, storage=storage).ingest_directory(root, max_files=1)
    hits = storage.search_file_chunks("directory ingestion", limit=5)

    assert result["root"] == str(root)
    assert result["files_indexed"] == 1
    assert result["files_failed"] == 0
    assert hits
    storage.close()


def test_file_ingestor_indexes_office_documents(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source_docx = tmp_path / "brief.docx"
    source_xlsx = tmp_path / "budget.xlsx"
    _write_minimal_docx(source_docx, "Jarvis Word attachment alpha")
    _write_minimal_xlsx(source_xlsx, "Jarvis Excel attachment beta")

    ingestor = FileIngestor(settings=settings, storage=storage)
    docx_result = ingestor.ingest_path(source_docx)
    xlsx_result = ingestor.ingest_path(source_xlsx)
    hits = storage.search_file_chunks("attachment", limit=10)

    assert docx_result["file"]["status"] == "indexed"
    assert xlsx_result["file"]["status"] == "indexed"
    assert docx_result["chunks_indexed"] == 1
    assert xlsx_result["chunks_indexed"] == 1
    assert {item["file_id"] for item in hits} == {
        docx_result["file"]["id"],
        xlsx_result["file"]["id"],
    }
    storage.close()


def test_file_lookup_prefers_specific_mime_for_duplicate_hash(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    sha256 = "a" * 64

    storage.create_file_record(
        name="unknown.md",
        stored_path=tmp_path / "unknown.md",
        sha256=sha256,
        size=10,
        mime_type="application/octet-stream",
        status="indexed",
    )
    preferred = storage.create_file_record(
        name="known.md",
        stored_path=tmp_path / "known.md",
        sha256=sha256,
        size=10,
        mime_type="text/markdown",
        status="indexed",
    )

    assert storage.get_file_by_sha256(sha256)["id"] == preferred["id"]
    storage.close()


def test_file_ingestor_rejects_oversized_stream_and_removes_temp(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(ingest_module, "MAX_UPLOAD_BYTES", 4)
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    ingestor = FileIngestor(settings=settings, storage=storage)

    with pytest.raises(OSError, match="upload limit"):
        ingestor.ingest_upload("large.bin", io.BytesIO(b"12345"))

    assert not list(ingestor.files_dir.glob(".upload_*.tmp"))
    storage.close()


def test_file_ingestor_removes_temp_when_stream_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    ingestor = FileIngestor(settings=settings, storage=storage)

    class BrokenStream:
        def read(self, _size):
            raise OSError("read failed")

    with pytest.raises(OSError, match="read failed"):
        ingestor.ingest_upload("broken.bin", BrokenStream())

    assert not list(ingestor.files_dir.glob(".upload_*.tmp"))
    storage.close()
