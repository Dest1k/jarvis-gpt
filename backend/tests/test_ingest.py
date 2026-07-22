from __future__ import annotations

import hashlib
import io
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from jarvis_gpt import ingest as ingest_module
from jarvis_gpt.authorization import ActorContext, bind_actor
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.document_runtime import write_pdf
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


def _write_minimal_pptx(path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            (
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<p:cSld><a:t>{text}</a:t></p:cSld></p:sld>"
            ),
        )


def _insert_test_users(storage: JarvisStorage, *user_ids: str) -> None:
    now = "2026-07-22T00:00:00+00:00"
    conn = storage.connect()
    for user_id in user_ids:
        conn.execute(
            """
            INSERT INTO users(
                id, status, display_name, locale, policy_epoch,
                created_at, updated_at, first_seen_at, last_seen_at
            ) VALUES (?, 'active', ?, 'en', 1, ?, ?, ?, ?)
            """,
            (user_id, user_id, now, now, now, now),
        )
    conn.commit()


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


def test_file_ingestor_indexes_extended_document_surfer_formats(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "roadmap.pptx"
    _write_minimal_pptx(source, "Roadmap Phoenix launch September")

    result = FileIngestor(settings=settings, storage=storage).ingest_path(source)
    hits = storage.search_file_chunks("Phoenix September", limit=5)

    assert result["file"]["status"] == "indexed"
    assert result["chunks_indexed"] == 1
    assert hits[0]["file_id"] == result["file"]["id"]
    assert "Roadmap Phoenix" in hits[0]["content"]
    storage.close()


def test_file_ingestor_reindexes_legacy_stored_extended_document(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "legacy-roadmap.pptx"
    _write_minimal_pptx(source, "Legacy Phoenix roadmap content")
    legacy = storage.create_file_record(
        name=source.name,
        stored_path=source,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        size=source.stat().st_size,
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        status="stored",
        error="Binary or unsupported text format; file stored without chunks.",
        chunk_count=0,
    )

    result = FileIngestor(settings=settings, storage=storage).ingest_path(source)
    hits = storage.search_file_chunks("Legacy Phoenix", limit=5)

    assert result["file"]["id"] == legacy["id"]
    assert result["deduplicated"] is True
    assert result["reindexed"] is True
    assert result["file"]["status"] == "indexed"
    assert hits[0]["file_id"] == legacy["id"]
    storage.close()


def test_file_ingestor_reindexes_duplicate_with_corrected_extension(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "jarvis_gpt.ingest.mimetypes.guess_type",
        lambda _name: ("application/octet-stream", None),
    )
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    corrected = tmp_path / "deck.pptx"
    disguised = tmp_path / "deck.bin"
    _write_minimal_pptx(corrected, "Corrected Phoenix presentation metadata")
    disguised.write_bytes(corrected.read_bytes())
    ingestor = FileIngestor(settings=settings, storage=storage)

    first = ingestor.ingest_path(disguised)
    old_stored_path = first["file"]["stored_path"]
    second = ingestor.ingest_path(corrected)
    hits = storage.search_file_chunks("Corrected Phoenix", limit=5)

    assert first["file"]["status"] == "stored"
    assert second["file"]["id"] == first["file"]["id"]
    assert second["deduplicated"] is True
    assert second["reindexed"] is True
    assert second["file"]["name"] == "deck.pptx"
    assert second["file"]["status"] == "indexed"
    assert second["file"]["mime_type"].endswith("presentationml.presentation")
    assert second["file"]["stored_path"].endswith(".pptx")
    assert not Path(old_stored_path).exists()
    assert hits[0]["file_id"] == first["file"]["id"]
    storage.close()


@pytest.mark.parametrize("filename", ["script.py", "config.yaml", "task.ps1"])
def test_file_ingestor_keeps_regular_text_formats_on_text_path(
    monkeypatch,
    tmp_path,
    filename,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / filename
    source.write_text("Phoenix regular text indexing marker", encoding="utf-8")

    result = FileIngestor(settings=settings, storage=storage).ingest_path(source)
    hits = storage.search_file_chunks("regular text indexing", limit=5)

    assert result["file"]["status"] == "indexed"
    assert result["chunks_indexed"] == 1
    assert hits[0]["file_id"] == result["file"]["id"]
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


def test_file_ingestor_records_indexing_before_extraction(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "durable.md"
    source.write_text("Durable extraction marker", encoding="utf-8")
    observed: dict[str, object] = {}

    def inspect_durable_record(stored):
        records = storage.list_files(limit=5)
        observed["records"] = records
        observed["stored_exists"] = stored.path.is_file()
        return ["Durable extraction marker"], "indexed", None

    monkeypatch.setattr(
        FileIngestor,
        "_extract_index",
        staticmethod(inspect_durable_record),
    )

    result = FileIngestor(settings=settings, storage=storage).ingest_path(source)

    records = observed["records"]
    assert isinstance(records, list)
    assert records[0]["status"] == "indexing"
    assert records[0]["chunk_count"] == 0
    assert observed["stored_exists"] is True
    assert result["file"]["status"] == "indexed"
    assert result["file"]["chunk_count"] == 1
    storage.close()


def test_file_ingestor_persists_unexpected_index_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "broken.md"
    source.write_text("The upload itself must survive.", encoding="utf-8")

    def fail_extraction(_stored):
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr(FileIngestor, "_extract_index", staticmethod(fail_extraction))

    result = FileIngestor(settings=settings, storage=storage).ingest_path(source)

    assert result["file"]["status"] == "failed"
    assert result["file"]["chunk_count"] == 0
    assert "RuntimeError" in result["file"]["error"]
    assert "extractor exploded" in result["file"]["error"]
    assert Path(result["file"]["stored_path"]).read_bytes() == source.read_bytes()
    assert storage.counters()["files"] == 1
    storage.close()


def test_file_ingestor_marks_finalization_failure_retryable(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "finalize.md"
    source.write_text("Finalization failure still keeps bytes", encoding="utf-8")

    def fail_finalization(*_args, **_kwargs):
        raise RuntimeError("SQLite index unavailable")

    monkeypatch.setattr(storage, "reindex_file", fail_finalization)

    result = FileIngestor(settings=settings, storage=storage).ingest_path(source)

    assert result["file"]["status"] == "failed"
    assert result["file"]["chunk_count"] == 0
    assert "Index finalization failed" in result["file"]["error"]
    assert "SQLite index unavailable" in result["file"]["error"]
    assert Path(result["file"]["stored_path"]).read_bytes() == source.read_bytes()
    storage.close()


def test_file_index_finalize_rolls_back_chunks_and_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "atomic.md"
    source.write_text("Original durable chunk", encoding="utf-8")
    record = FileIngestor(settings=settings, storage=storage).ingest_path(source)["file"]
    original_replace = storage._replace_file_chunks

    def fail_after_replacement(conn, file_id, chunks, *, now):
        original_replace(conn, file_id, chunks, now=now)
        raise RuntimeError("commit window")

    monkeypatch.setattr(storage, "_replace_file_chunks", fail_after_replacement)

    with pytest.raises(RuntimeError, match="commit window"):
        storage.reindex_file(
            record["id"],
            ["Uncommitted replacement"],
            name=record["name"],
            stored_path=Path(record["stored_path"]),
            size=record["size"],
            mime_type=record["mime_type"],
            status="indexed",
            error=None,
        )

    current = storage.get_file(record["id"])
    assert current["status"] == "indexed"
    assert current["chunk_count"] == 1
    assert storage.list_file_chunks(record["id"])[0]["content"] == "Original durable chunk"
    storage.close()


def test_storage_reconciles_interrupted_indexing_on_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    stored_path = settings.data_dir / "files" / "partial.md"
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_text("Authoritative uploaded bytes", encoding="utf-8")
    record, created = storage.claim_file_ingest(
        name="partial.md",
        stored_path=stored_path,
        sha256=hashlib.sha256(stored_path.read_bytes()).hexdigest(),
        size=stored_path.stat().st_size,
        mime_type="text/markdown",
    )
    assert created is True
    storage.add_file_chunks(record["id"], ["Untrusted partial chunk"])
    storage.close()

    reopened = JarvisStorage(settings.database_path)
    reopened.initialize()
    recovered = reopened.get_file(record["id"])

    assert recovered["status"] == "failed"
    assert recovered["chunk_count"] == 0
    assert "interrupted" in recovered["error"].lower()
    assert reopened.list_file_chunks(record["id"]) == []
    assert stored_path.read_text(encoding="utf-8") == "Authoritative uploaded bytes"
    reopened.close()


def test_file_ingest_claim_serializes_concurrent_same_sha(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    first_storage = JarvisStorage(settings.database_path)
    first_storage.initialize()
    second_storage = JarvisStorage(settings.database_path)
    second_storage.initialize()
    stored_path = settings.data_dir / "files" / "same.md"
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_text("Same tenant and hash", encoding="utf-8")
    sha256 = hashlib.sha256(stored_path.read_bytes()).hexdigest()
    barrier = threading.Barrier(2)

    def claim(storage):
        barrier.wait()
        return storage.claim_file_ingest(
            name="same.md",
            stored_path=stored_path,
            sha256=sha256,
            size=stored_path.stat().st_size,
            mime_type="text/markdown",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(claim, first_storage)
        second_future = executor.submit(claim, second_storage)
        first = first_future.result(timeout=10)
        second = second_future.result(timeout=10)

    assert first[0]["id"] == second[0]["id"]
    assert sorted((first[1], second[1])) == [False, True]
    assert first_storage.counters()["files"] == 1
    first_storage.close()
    second_storage.close()


def test_file_ingest_claim_is_scoped_per_tenant(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    now = "2026-07-22T00:00:00+00:00"
    for user_id in ("usr_ingest_a", "usr_ingest_b"):
        storage.connect().execute(
            """
            INSERT INTO users(
                id, status, display_name, locale, policy_epoch,
                created_at, updated_at, first_seen_at, last_seen_at
            ) VALUES (?, 'active', ?, 'en', 1, ?, ?, ?, ?)
            """,
            (user_id, user_id, now, now, now, now),
        )
    storage.connect().commit()
    stored_path = settings.data_dir / "files" / "tenant-shared.md"
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_text("Same bytes are allowed in separate tenants", encoding="utf-8")
    sha256 = hashlib.sha256(stored_path.read_bytes()).hexdigest()

    def claim_for(user_id):
        actor = ActorContext(user_id=user_id, preset_key="user", source="test")
        with bind_actor(actor):
            return storage.claim_file_ingest(
                name="tenant-shared.md",
                stored_path=stored_path,
                sha256=sha256,
                size=stored_path.stat().st_size,
                mime_type="text/markdown",
            )

    first = claim_for("usr_ingest_a")
    second = claim_for("usr_ingest_b")

    assert first[1] is True
    assert second[1] is True
    assert first[0]["id"] != second[0]["id"]
    claims = (
        storage.connect()
        .execute(
            "SELECT user_id, sha256 FROM file_ingest_claims WHERE sha256 = ?",
            (sha256,),
        )
        .fetchall()
    )
    assert {row["user_id"] for row in claims} == {"usr_ingest_a", "usr_ingest_b"}
    storage.close()


def test_unsupported_upload_is_durably_recorded(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    result = FileIngestor(settings=settings, storage=storage).ingest_upload(
        "opaque.bin",
        io.BytesIO(b"\x00\x01\x02"),
    )

    assert result["file"]["status"] == "stored"
    assert result["file"]["chunk_count"] == 0
    assert "unsupported" in result["file"]["error"].lower()
    assert Path(result["file"]["stored_path"]).read_bytes() == b"\x00\x01\x02"
    assert storage.search_files("opaque.bin")[0]["id"] == result["file"]["id"]
    storage.close()


def test_long_upload_name_preserves_type_without_long_physical_path(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()

    result = FileIngestor(settings=settings, storage=storage).ingest_upload(
        f"{'x' * 240}.md",
        io.BytesIO(b"Long filename remains searchable markdown"),
    )

    assert result["file"]["status"] == "indexed"
    assert result["file"]["name"].endswith(".md")
    assert len(result["file"]["name"]) <= 180
    assert Path(result["file"]["stored_path"]).suffix == ".md"
    assert len(Path(result["file"]["stored_path"]).name) == 67
    assert storage.search_file_chunks("searchable markdown")
    storage.close()


def test_persist_file_extracted_text_is_atomic_and_records_provenance(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    uploaded = FileIngestor(settings=settings, storage=storage).ingest_upload(
        "scan.png",
        io.BytesIO(b"not-a-real-image"),
    )["file"]
    ocr_text = "Invoice alpha. \u8acb\u6c42\u66f8\u756a\u53f7\u306f ZX-42 \u3067\u3059. " * 120

    indexed = storage.persist_file_extracted_text(
        uploaded["id"],
        ocr_text,
        source="vlm_ocr:qwen36-vl",
        details={"pages": 1, "language": "ja"},
        warning="OCR-derived text",
    )
    provenance = storage.get_file_index_metadata(uploaded["id"])

    assert indexed["status"] == "indexed"
    assert indexed["chunk_count"] > 1
    assert indexed["index_source"] == "vlm_ocr:qwen36-vl"
    assert indexed["error"] == "OCR-derived text"
    assert provenance["source"] == "vlm_ocr:qwen36-vl"
    assert provenance["details"] == {"pages": 1, "language": "ja"}
    assert storage.search_file_chunks("\u8acb\u6c42\u66f8\u756a\u53f7", limit=5)

    original_replace = storage._replace_file_chunks

    def fail_after_replacement(conn, file_id, chunks, *, now):
        original_replace(conn, file_id, chunks, now=now)
        raise RuntimeError("OCR commit failed")

    monkeypatch.setattr(storage, "_replace_file_chunks", fail_after_replacement)
    with pytest.raises(RuntimeError, match="OCR commit failed"):
        storage.persist_file_extracted_text(
            uploaded["id"],
            "Replacement that must roll back",
            source="vlm_ocr:replacement",
        )

    unchanged = storage.get_file(uploaded["id"])
    assert unchanged["chunk_count"] == indexed["chunk_count"]
    assert storage.get_file_index_metadata(uploaded["id"]) == provenance
    assert storage.search_file_chunks("\u8acb\u6c42\u66f8\u756a\u53f7", limit=5)
    storage.close()


def test_upload_intent_recovers_ready_marker_and_queues_ocr_after_restart(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    unrelated = settings.data_dir / "files" / "unrelated.keep"
    unrelated.write_bytes(b"must not be touched")

    def crash_before_prepare(*_args, **_kwargs):
        raise SystemExit("crash after completion marker")

    monkeypatch.setattr(storage, "prepare_file_upload", crash_before_prepare)
    with pytest.raises(SystemExit, match="completion marker"):
        FileIngestor(settings=settings, storage=storage).ingest_upload(
            "telegram-scan.png",
            io.BytesIO(b"durable scan bytes"),
        )

    pending = storage.connect().execute(
        "SELECT * FROM file_upload_intents ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert pending is not None
    assert pending["status"] == "receiving"
    assert Path(pending["ready_path"]).read_bytes() == b"durable scan bytes"
    intent_id = str(pending["id"])
    storage.close()

    reopened = JarvisStorage(settings.database_path)
    reopened.initialize()
    recovered_intent = reopened.get_file_upload_intent(intent_id)
    assert recovered_intent is not None
    assert recovered_intent["status"] == "committed"
    recovered_file = reopened.get_file(str(recovered_intent["file_id"]))
    assert recovered_file["status"] == "stored"
    assert Path(recovered_file["stored_path"]).read_bytes() == b"durable scan bytes"
    assert not Path(recovered_intent["ready_path"]).exists()
    assert unrelated.read_bytes() == b"must not be touched"
    queued = reopened.get_file_ocr_job_for_file(str(recovered_file["id"]))
    assert queued is not None
    assert queued["status"] == "pending"
    assert queued["reason"] == "image_upload_recovered"
    reopened.close()


def test_upload_intent_recovers_blob_promoted_before_database_commit(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    original_commit_blob = storage._commit_file_upload_blob_conn

    def crash_after_blob_promotion(conn, row):
        original_commit_blob(conn, row)
        raise SystemExit("crash before SQLite commit")

    monkeypatch.setattr(storage, "_commit_file_upload_blob_conn", crash_after_blob_promotion)
    with pytest.raises(SystemExit, match="SQLite commit"):
        FileIngestor(settings=settings, storage=storage).ingest_upload(
            "promoted-scan.png",
            io.BytesIO(b"already promoted bytes"),
        )

    uncommitted = storage.connect().execute(
        "SELECT * FROM file_upload_intents ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert uncommitted is not None
    intent_id = str(uncommitted["id"])
    final_path = Path(str(uncommitted["final_path"]))
    assert final_path.read_bytes() == b"already promoted bytes"
    storage.close()

    reopened = JarvisStorage(settings.database_path)
    reopened.initialize()
    recovered_intent = reopened.get_file_upload_intent(intent_id)
    assert recovered_intent is not None
    assert recovered_intent["status"] == "committed"
    recovered_file = reopened.get_file(str(recovered_intent["file_id"]))
    assert Path(recovered_file["stored_path"]).read_bytes() == b"already promoted bytes"
    assert reopened.get_file_ocr_job_for_file(str(recovered_file["id"])) is not None
    reopened.close()


def test_upload_recovery_removes_only_the_recorded_stale_part(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    intent = storage.begin_file_upload(name="partial.png", mime_type="image/png")
    part_path = Path(str(intent["part_path"]))
    part_path.parent.mkdir(parents=True, exist_ok=True)
    part_path.write_bytes(b"incomplete")
    unrelated = part_path.parent / "unrelated.part"
    unrelated.write_bytes(b"another process owns this")
    storage.connect().execute(
        "UPDATE file_upload_intents SET updated_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", intent["id"]),
    )
    storage.connect().commit()
    storage.close()

    reopened = JarvisStorage(settings.database_path)
    reopened.initialize()
    recovered = reopened.get_file_upload_intent(str(intent["id"]))
    assert recovered is not None
    assert recovered["status"] == "failed"
    assert not part_path.exists()
    assert unrelated.read_bytes() == b"another process owns this"
    reopened.close()


def test_ordinary_user_scan_is_queued_processed_and_searchable_per_tenant(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    _insert_test_users(storage, "usr_scan_owner", "usr_scan_other")
    actor = ActorContext(user_id="usr_scan_owner", preset_key="user", source="telegram")

    with bind_actor(actor):
        ingestor = FileIngestor(settings=settings, storage=storage)
        uploaded = ingestor.ingest_upload(
            "telegram-scan.png",
            io.BytesIO(b"fake telegram image"),
        )
        assert uploaded["file"]["status"] == "stored"
        assert uploaded["ocr_job"]["status"] == "pending"

        def fake_ocr(job):
            assert Path(job["stored_path"]).read_bytes() == b"fake telegram image"
            return {
                "text": (
                    "Project Atlas multilingual invoice. "
                    "\u53d1\u7968\u7f16\u53f7 CN-42. \u8acb\u6c42\u66f8\u756a\u53f7 JP-42. "
                    "\ud504\ub85c\uc81d\ud2b8 KR-42."
                ),
                "source": "fake_ocr:test",
                "details": {"pages": 1, "transport": "telegram"},
            }

        processed = ingestor.process_next_ocr_job(fake_ocr, worker_id="test-worker")
        assert processed is not None
        assert processed["ok"] is True
        assert processed["job"]["status"] == "completed"
        assert processed["job"]["result_status"] == "indexed"
        assert storage.search_file_chunks("Project Atlas", limit=5)
        assert storage.search_file_chunks("\u8acb\u6c42\u66f8\u756a\u53f7", limit=5)
        metadata = storage.get_file_index_metadata(str(uploaded["file"]["id"]))
        assert metadata["source"] == "fake_ocr:test"
        assert metadata["details"]["transport"] == "telegram"
        job_id = str(processed["job"]["id"])
        completion_token = str(processed["job"]["completion_token"])

    other_actor = ActorContext(user_id="usr_scan_other", preset_key="user", source="telegram")
    with bind_actor(other_actor):
        assert storage.get_file_ocr_job(job_id) is None
        assert storage.search_file_chunks("Project Atlas", limit=5) == []
        with pytest.raises(KeyError):
            storage.complete_file_ocr_job(
                job_id,
                completion_token,
                "cross-tenant overwrite",
                source="forbidden",
            )
    storage.close()


def test_ocr_retry_is_bounded_and_expired_lease_recovers(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    _insert_test_users(storage, "usr_ocr_retry")
    actor = ActorContext(user_id="usr_ocr_retry", preset_key="user", source="test")

    with bind_actor(actor):
        first = FileIngestor(settings=settings, storage=storage).ingest_upload(
            "retry.png",
            io.BytesIO(b"retry image"),
        )["ocr_job"]
        storage.connect().execute(
            "UPDATE file_ocr_jobs SET max_attempts = 2 WHERE id = ?",
            (first["id"],),
        )
        storage.connect().commit()
        lease_one = storage.claim_next_file_ocr_job(worker_id="retry-worker")
        retry = storage.fail_file_ocr_job(
            str(lease_one["id"]),
            str(lease_one["lease_token"]),
            "transient OCR failure",
            retry_delay_seconds=0,
        )
        assert retry["status"] == "retry"
        lease_two = storage.claim_next_file_ocr_job(worker_id="retry-worker")
        terminal = storage.fail_file_ocr_job(
            str(lease_two["id"]),
            str(lease_two["lease_token"]),
            "second OCR failure",
            retry_delay_seconds=0,
        )
        assert terminal["status"] == "failed"
        assert storage.claim_next_file_ocr_job(worker_id="retry-worker") is None

        second = FileIngestor(settings=settings, storage=storage).ingest_upload(
            "lease.png",
            io.BytesIO(b"different lease image"),
        )["ocr_job"]
        expired_lease = storage.claim_next_file_ocr_job(worker_id="crashed-worker")
        assert expired_lease["id"] == second["id"]
        storage.connect().execute(
            "UPDATE file_ocr_jobs SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", second["id"]),
        )
        storage.connect().commit()
    storage.close()

    reopened = JarvisStorage(settings.database_path)
    reopened.initialize()
    with bind_actor(actor):
        recovered = reopened.get_file_ocr_job(str(second["id"]))
        assert recovered["status"] == "retry"
        current_lease = reopened.claim_next_file_ocr_job(worker_id="replacement-worker")
        assert current_lease["id"] == second["id"]
        assert current_lease["lease_token"] != expired_lease["lease_token"]
        with pytest.raises(ValueError, match="no longer current"):
            reopened.complete_file_ocr_job(
                str(second["id"]),
                str(expired_lease["lease_token"]),
                "stale result",
                source="fake_ocr:test",
            )
        completed = reopened.complete_file_ocr_job(
            str(second["id"]),
            str(current_lease["lease_token"]),
            "Recovered OCR lease result",
            source="fake_ocr:test",
        )
        assert completed["status"] == "completed"
    reopened.close()


def test_ocr_completion_is_idempotent_and_preserves_existing_good_index(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    uploaded = FileIngestor(settings=settings, storage=storage).ingest_upload(
        "race.png",
        io.BytesIO(b"race image"),
    )
    file_id = str(uploaded["file"]["id"])
    first_lease = storage.claim_next_file_ocr_job(worker_id="first-worker")
    storage.persist_file_extracted_text(
        file_id,
        "Authoritative existing transcription",
        source="manual_verified",
    )
    failed = storage.fail_file_ocr_job(
        str(first_lease["id"]),
        str(first_lease["lease_token"]),
        "processor crashed after another index won",
        retry_delay_seconds=0,
    )
    assert failed["status"] == "retry"
    assert storage.list_file_chunks(file_id)[0]["content"] == (
        "Authoritative existing transcription"
    )

    second_lease = storage.claim_next_file_ocr_job(worker_id="second-worker")
    completed = storage.complete_file_ocr_job(
        str(second_lease["id"]),
        str(second_lease["lease_token"]),
        "Inferior late OCR text",
        source="automatic_ocr",
    )
    repeated = storage.complete_file_ocr_job(
        str(second_lease["id"]),
        str(second_lease["lease_token"]),
        "Different replay text",
        source="automatic_ocr:replay",
    )

    assert completed["result_status"] == "skipped_existing_index"
    assert repeated == completed
    assert storage.list_file_chunks(file_id)[0]["content"] == (
        "Authoritative existing transcription"
    )
    assert storage.get_file_index_metadata(file_id)["source"] == "manual_verified"
    storage.close()


def test_failed_ocr_can_restart_explicitly_and_by_verified_reupload_with_generation_cap(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    ingestor = FileIngestor(settings=settings, storage=storage)
    original = b"verified retry image"
    uploaded = ingestor.ingest_upload("retry-generations.png", io.BytesIO(original))
    file_id = str(uploaded["file"]["id"])
    job_id = str(uploaded["ocr_job"]["id"])
    storage.connect().execute(
        "UPDATE file_ocr_jobs SET max_attempts = 1, max_generations = 3 WHERE id = ?",
        (job_id,),
    )
    storage.connect().commit()

    first_lease = storage.claim_next_file_ocr_job(worker_id="generation-one")
    first_failed = storage.fail_file_ocr_job(
        job_id,
        str(first_lease["lease_token"]),
        "generation one failed",
        retry_delay_seconds=0,
    )
    assert first_failed["status"] == "failed"
    assert first_failed["generation"] == 1

    managed_path = Path(str(uploaded["file"]["stored_path"]))
    managed_path.write_bytes(b"corrupt retry source")
    with pytest.raises(ValueError, match="verified source blob"):
        storage.retry_file_ocr_job(file_id, reason="must_not_accept_corruption")
    managed_path.write_bytes(original)

    explicit = storage.retry_file_ocr_job(file_id, reason="operator_retry")
    assert explicit["status"] == "pending"
    assert explicit["generation"] == 2
    assert explicit["attempt_count"] == 0
    second_lease = storage.claim_next_file_ocr_job(worker_id="generation-two")
    storage.fail_file_ocr_job(
        job_id,
        str(second_lease["lease_token"]),
        "generation two failed",
        retry_delay_seconds=0,
    )

    reuploaded = ingestor.ingest_upload("retry-generations.png", io.BytesIO(original))
    restarted = reuploaded["ocr_job"]
    assert restarted["status"] == "pending"
    assert restarted["generation"] == 3
    assert len(restarted["result_metadata"]["retry_history"]) == 2
    third_lease = storage.claim_next_file_ocr_job(worker_id="generation-three")
    storage.fail_file_ocr_job(
        job_id,
        str(third_lease["lease_token"]),
        "generation three failed",
        retry_delay_seconds=0,
    )

    exhausted = ingestor.ingest_upload("retry-generations.png", io.BytesIO(original))
    assert exhausted["ocr_job"]["status"] == "failed"
    assert exhausted["ocr_job"]["generation"] == 3
    assert storage.claim_next_file_ocr_job(worker_id="must-stay-empty") is None
    storage.close()


def test_mixed_pdf_ocr_augments_native_index_without_dropping_text(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    source = tmp_path / "mixed.pdf"
    write_pdf(source, "Native searchable contract ID NATIVE-42", title="Mixed PDF")

    uploaded = FileIngestor(settings=settings, storage=storage).ingest_upload(
        "mixed.pdf",
        io.BytesIO(source.read_bytes()),
    )
    file_id = str(uploaded["file"]["id"])
    assert uploaded["file"]["status"] == "indexed"
    assert uploaded["ocr_job"]["reason"] == "pdf_completeness_upload"
    assert storage.search_file_chunks("NATIVE-42", limit=5)

    lease = storage.claim_next_file_ocr_job(worker_id="mixed-pdf-worker")
    completed = storage.complete_file_ocr_job(
        str(lease["id"]),
        str(lease["lease_token"]),
        "Scanned page annotation SCANNED-99",
        source="fake_ocr:mixed",
        details={"pages_total": 2, "pages_recognized": 1},
        warning="One scanned page was supplemental.",
    )

    assert completed["result_status"] == "augmented_existing_index"
    assert storage.search_file_chunks("NATIVE-42", limit=5)
    assert storage.search_file_chunks("SCANNED-99", limit=5)
    metadata = storage.get_file_index_metadata(file_id)
    assert metadata["source"] == "native_extraction+fake_ocr:mixed"
    assert metadata["details"]["ocr_merge"]["native_chunks_preserved"] > 0
    assert metadata["details"]["ocr_merge"]["ocr_chunks_added"] > 0
    storage.close()


def test_verified_reupload_atomically_heals_corrupt_managed_blob(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    ingestor = FileIngestor(settings=settings, storage=storage)
    original = b"authoritative document HEAL-42"
    first = ingestor.ingest_upload("healable.txt", io.BytesIO(original))
    file_id = str(first["file"]["id"])
    managed_path = Path(str(first["file"]["stored_path"]))
    managed_path.write_bytes(b"corrupt managed bytes")

    healed = ingestor.ingest_upload("healable.txt", io.BytesIO(original))

    assert healed["deduplicated"] is True
    assert healed["blob_healed"] is True
    assert healed["file"]["id"] == file_id
    assert managed_path.read_bytes() == original
    assert storage.search_file_chunks("HEAL-42", limit=5)[0]["file_id"] == file_id
    storage.close()
