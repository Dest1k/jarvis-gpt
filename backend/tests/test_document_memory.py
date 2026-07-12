from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.document_memory import DOCUMENT_MEMORY_PROTOCOL, DocumentMemory
from jarvis_gpt.document_surfer import DocumentSurferConfig, JarvisDocumentSurfer
from jarvis_gpt.ingest import FileIngestor
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry


def _runtime(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    surfer = JarvisDocumentSurfer(
        DocumentSurferConfig(output_dir=settings.data_dir / "document-outputs")
    )
    return settings, storage, surfer


def _write_pptx(path: Path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            (
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                f"<p:cSld><a:t>{text}</a:t></p:cSld></p:sld>"
            ),
        )


def test_document_memory_recalls_content_and_returns_analysis(monkeypatch, tmp_path) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    contract = tmp_path / "alpha-contract.md"
    contract.write_text(
        "# Alpha Contract\n\nBudget: 1200 RUB.\nOwner: Alpha Team.\nDeadline: 2026-09-01.",
        encoding="utf-8",
    )
    unrelated = tmp_path / "garden-notes.md"
    unrelated.write_text("# Garden\nTomatoes and watering schedule.", encoding="utf-8")
    ingested = FileIngestor(settings, storage).ingest_path(contract)
    FileIngestor(settings, storage).ingest_path(unrelated)

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Достань из памяти договор Alpha и дай резюме",
        max_files=2,
    )

    assert result["protocol"] == DOCUMENT_MEMORY_PROTOCOL
    assert result["ok"] is True
    assert result["sources"][0]["file_id"] == ingested["file"]["id"]
    assert result["sources"][0]["name"] == "alpha-contract.md"
    assert "1200 RUB" in result["passages"][0]["content"]
    assert result["analyses"][0]["document"]["kind"] == "md"
    assert result["corpus"]["summary"]["files"] == 1
    assert "stored_path" not in result["sources"][0]
    storage.close()


def test_document_memory_finds_filename_without_indexed_chunks(monkeypatch, tmp_path) -> None:
    _settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    path = tmp_path / "quarterly-brief.txt"
    path.write_text("Quarterly revenue grew by 12 percent.", encoding="utf-8")
    record = storage.create_file_record(
        name=path.name,
        stored_path=path,
        sha256="f" * 64,
        size=path.stat().st_size,
        mime_type="text/plain",
        status="stored",
        chunk_count=0,
    )

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Summarize uploaded quarterly-brief document"
    )

    assert result["ok"] is True
    assert result["sources"][0]["file_id"] == record["id"]
    assert result["sources"][0]["match_sources"] == ["name"]
    assert "12 percent" in result["passages"][0]["content"]
    storage.close()


def test_document_memory_filename_lookup_is_unicode_casefolded(monkeypatch, tmp_path) -> None:
    _settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    path = tmp_path / "Договор-Альфа.txt"
    path.write_text("Условия договора Альфа.", encoding="utf-8")
    record = storage.create_file_record(
        name=path.name,
        stored_path=path,
        sha256="e" * 64,
        size=path.stat().st_size,
        mime_type="text/plain",
        status="stored",
        chunk_count=0,
    )

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Резюмируй сохраненный договор-альфа"
    )

    assert result["ok"] is True
    assert result["sources"][0]["file_id"] == record["id"]
    assert "Условия договора" in result["passages"][0]["content"]
    storage.close()


def test_document_memory_recalls_generic_code_text_by_exact_filename(monkeypatch, tmp_path) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    path = tmp_path / "phoenix.py"
    path.write_text("PHOENIX_STATUS = 'ready'\n", encoding="utf-8")
    record = FileIngestor(settings, storage).ingest_path(path)["file"]

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Summarize saved phoenix.py file"
    )

    assert result["ok"] is True
    assert result["sources"][0]["file_id"] == record["id"]
    assert "PHOENIX_STATUS" in result["passages"][0]["content"]
    assert result["analyses"][0]["document"]["kind"] == "py"
    storage.close()


def test_document_memory_specific_no_match_does_not_guess_recent(monkeypatch, tmp_path) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    source = tmp_path / "known.txt"
    source.write_text("Known contract terms and local notes.", encoding="utf-8")
    FileIngestor(settings, storage).ingest_path(source)

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Summarize Quantum Zebra contract"
    )

    assert result["ok"] is False
    assert result["selection"]["matched"] == 0
    assert result["sources"] == []
    storage.close()


def test_document_memory_temporal_specific_no_match_does_not_guess_recent(
    monkeypatch,
    tmp_path,
) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    source = tmp_path / "unrelated.txt"
    source.write_text("Unrelated saved notes.", encoding="utf-8")
    FileIngestor(settings, storage).ingest_path(source)
    memory = DocumentMemory(storage=storage, surfer=surfer)

    for query in (
        "Summarize the latest Quantum Zebra document",
        "Дай резюме последнего документа Квант Зебра",
    ):
        result = memory.recall(query)
        assert result["ok"] is False
        assert result["selection"]["matched"] == 0
        assert result["sources"] == []
    storage.close()


def test_document_memory_rejects_partial_two_term_identity(monkeypatch, tmp_path) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    source = tmp_path / "alpha-gamma-contract.txt"
    source.write_text("Alpha Gamma contract terms.", encoding="utf-8")
    FileIngestor(settings, storage).ingest_path(source)

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Summarize saved Alpha Beta contract"
    )

    assert result["ok"] is False
    assert result["sources"] == []
    storage.close()


def test_document_memory_identity_terms_require_token_boundaries(monkeypatch, tmp_path) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    source = tmp_path / "alphabet-beta-contract.txt"
    source.write_text("Alphabet Beta contract terms.", encoding="utf-8")
    FileIngestor(settings, storage).ingest_path(source)

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Summarize saved Alpha Beta contract"
    )

    assert result["ok"] is False
    assert result["sources"] == []
    storage.close()


def test_document_memory_keeps_distinctive_terms_that_start_with_doc(
    monkeypatch,
    tmp_path,
) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    for name, content in (
        ("docker-roadmap.txt", "Docker roadmap uses container milestones."),
        ("kubernetes-roadmap.txt", "Kubernetes roadmap uses cluster milestones."),
    ):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        FileIngestor(settings, storage).ingest_path(path)

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Summarize saved Docker roadmap document"
    )

    assert result["ok"] is True
    assert result["selection"]["ambiguous"] is False
    assert result["sources"][0]["name"] == "docker-roadmap.txt"
    storage.close()


def test_document_memory_ambiguous_generic_match_requires_file_id(monkeypatch, tmp_path) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("Standard contract terms A.", encoding="utf-8")
    second.write_text("Standard contract terms B.", encoding="utf-8")
    FileIngestor(settings, storage).ingest_path(first)
    FileIngestor(settings, storage).ingest_path(second)

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Summarize saved contract"
    )

    assert result["ok"] is False
    assert result["selection"]["ambiguous"] is True
    assert len(result["sources"]) == 2
    assert all(not source["matched_chunks"] for source in result["sources"])
    assert result["passages"] == []
    assert "explicit file_id" in result["errors"][-1]["error"]
    storage.close()


def test_document_memory_recent_fallback_selects_latest_supported_document(
    monkeypatch,
    tmp_path,
) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    older = tmp_path / "older.txt"
    latest = tmp_path / "latest.txt"
    older.write_text("Older notes.", encoding="utf-8")
    latest.write_text("Latest release summary.", encoding="utf-8")
    FileIngestor(settings, storage).ingest_path(older)
    latest_result = FileIngestor(settings, storage).ingest_path(latest)
    for index in range(55):
        image = tmp_path / f"screen-{index}.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        storage.create_file_record(
            name=image.name,
            stored_path=image,
            sha256=f"{index + 1:064x}",
            size=image.stat().st_size,
            mime_type="image/png",
            status="stored",
        )

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Достань из памяти последний документ и резюмируй",
    )

    assert result["ok"] is True
    assert result["selection"]["mode"] == "recent"
    assert result["sources"][0]["file_id"] == latest_result["file"]["id"]
    assert "Latest release" in result["passages"][0]["content"]
    storage.close()


def test_document_memory_temporal_query_selects_newest_validated_match(
    monkeypatch,
    tmp_path,
) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    file_ids = []
    for name, content in (
        ("alpha-old.txt", "Alpha old decision."),
        ("alpha-new.txt", "Alpha newest decision."),
    ):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        file_ids.append(FileIngestor(settings, storage).ingest_path(path)["file"]["id"])
    connection = storage.connect()
    connection.execute("UPDATE files SET created_at = ? WHERE id = ?", ("2026-01-01", file_ids[0]))
    connection.execute("UPDATE files SET created_at = ? WHERE id = ?", ("2026-02-01", file_ids[1]))
    connection.commit()

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Summarize the latest Alpha document"
    )

    assert result["ok"] is True
    assert result["selection"]["mode"] == "search"
    assert result["sources"][0]["file_id"] == file_ids[1]
    assert "newest decision" in result["passages"][0]["content"]
    storage.close()


def test_document_memory_unspecified_saved_document_is_ambiguous(monkeypatch, tmp_path) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    for name in ("one.txt", "two.txt"):
        path = tmp_path / name
        path.write_text(f"Saved content from {name}.", encoding="utf-8")
        FileIngestor(settings, storage).ingest_path(path)

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Summarize a saved document"
    )

    assert result["ok"] is False
    assert result["selection"]["mode"] == "memory"
    assert result["selection"]["ambiguous"] is True
    assert len(result["sources"]) == 2
    storage.close()


def test_document_memory_reports_bounded_multi_document_scope(monkeypatch, tmp_path) -> None:
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    for index in range(4):
        path = tmp_path / f"saved-{index}.txt"
        path.write_text(f"Saved document {index}.", encoding="utf-8")
        FileIngestor(settings, storage).ingest_path(path)

    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "Summarize all saved documents",
        max_files=3,
    )

    assert result["ok"] is True
    assert result["selection"]["matched"] == 3
    assert result["selection"]["truncated"] is True
    assert result["selection"]["limit"] == 3
    storage.close()


def test_documents_recall_tool_runs_full_persisted_flow(monkeypatch, tmp_path) -> None:
    settings, storage, _surfer = _runtime(monkeypatch, tmp_path)
    source = tmp_path / "operations-report.txt"
    source.write_text(
        "Operations report: migration completed. Remaining risk: backup validation.",
        encoding="utf-8",
    )
    ingested = FileIngestor(settings, storage).ingest_path(source)
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    result = asyncio.run(
        tools.run(
            "documents.recall",
            {"query": "дай резюме сохраненного operations report"},
        )
    )

    assert result.ok is True
    assert result.data["sources"][0]["file_id"] == ingested["file"]["id"]
    assert "backup validation" in result.data["passages"][0]["content"]
    assert result.data["analyses"]
    storage.close()


def test_file_search_keeps_exact_content_hit_older_than_recent_window(
    monkeypatch,
    tmp_path,
) -> None:
    settings, storage, _surfer = _runtime(monkeypatch, tmp_path)
    source = tmp_path / "old-source.txt"
    source.write_text("unique-nebula-needle", encoding="utf-8")
    oldest = FileIngestor(settings, storage).ingest_path(source)["file"]
    for index in range(205):
        storage.create_file_record(
            name=f"newer-{index}.bin",
            stored_path=tmp_path / f"newer-{index}.bin",
            sha256=f"{index:064x}",
            size=0,
            mime_type="application/octet-stream",
            status="stored",
        )

    files = storage.search_files("unique-nebula-needle", limit=3)

    assert files[0]["id"] == oldest["id"]
    assert files[0]["match_sources"] == ["content"]
    storage.close()


def test_archive_extracted_pptx_is_indexed_and_recallable(monkeypatch, tmp_path) -> None:
    settings, storage, _surfer = _runtime(monkeypatch, tmp_path)
    deck = tmp_path / "phoenix-deck.pptx"
    _write_pptx(deck, "Phoenix deck launch checklist")
    bundle = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.write(deck, arcname=deck.name)
    bundle_record = FileIngestor(settings, storage).ingest_path(bundle)["file"]
    tools = ToolRegistry(settings, storage, LLMRouter(settings))

    extracted = asyncio.run(
        tools.run("documents.archive.extract", {"file_id": bundle_record["id"]})
    )
    recalled = asyncio.run(
        tools.run("documents.recall", {"query": "Phoenix deck"})
    )

    assert extracted.ok is True
    indexed = extracted.data["indexed_files"][0]
    assert indexed["status"] == "indexed"
    assert indexed["chunk_count"] == 1
    assert recalled.ok is True
    assert recalled.data["sources"][0]["file_id"] == indexed["id"]
    assert "launch checklist" in recalled.data["passages"][0]["content"]
    storage.close()
