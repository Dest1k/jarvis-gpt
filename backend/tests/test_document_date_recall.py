"""Date-scoped document recall: "какие документы были 15 июля" / "вывод за вчера".

Documents are recalled by their upload date (files.created_at), listed or concluded
over — not selected by relevance. Covers the date parser, the storage range query, and
the DocumentMemory list/empty/conclude behaviours.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.document_memory import DocumentMemory, parse_document_date_scope
from jarvis_gpt.document_surfer import DocumentSurferConfig, JarvisDocumentSurfer
from jarvis_gpt.ingest import FileIngestor
from jarvis_gpt.storage import JarvisStorage

_NOW = datetime(2026, 7, 17, 15, 30, tzinfo=timezone(timedelta(hours=3)))  # local +03


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


# --- date parser -----------------------------------------------------------------

def test_parse_specific_day_maps_local_day_to_utc_window():
    scope = parse_document_date_scope("какие документы были 15 июля?", now=_NOW)
    assert scope is not None
    # Local 15 July 00:00..24:00 (+03) -> 14 July 21:00 .. 15 July 21:00 UTC.
    assert scope.start_utc == "2026-07-14T21:00:00+00:00"
    assert scope.end_utc == "2026-07-15T21:00:00+00:00"
    assert scope.conclude is False


def test_parse_relative_and_conclude_flag():
    scope = parse_document_date_scope("сделай вывод из документов за вчера", now=_NOW)
    assert scope is not None
    assert scope.start_utc == "2026-07-15T21:00:00+00:00"  # 16 July local
    assert scope.end_utc == "2026-07-16T21:00:00+00:00"
    assert scope.conclude is True


def test_parse_formats_iso_dotted_and_month_name():
    assert parse_document_date_scope("документы за 2026-07-10", now=_NOW).label == "2026-07-10"
    assert parse_document_date_scope("файлы 10.07.2026", now=_NOW).label == "10.07.2026"
    last_week = parse_document_date_scope("какие файлы загружали на прошлой неделе", now=_NOW)
    assert last_week is not None and last_week.label == "прошлая неделя"


def test_parse_requires_document_noun_and_a_date():
    assert parse_document_date_scope("какая погода была 15 июля", now=_NOW) is None  # no noun
    assert parse_document_date_scope("покажи мои документы", now=_NOW) is None  # no date


# --- storage range ---------------------------------------------------------------

def test_list_files_in_range_filters_by_created_at(monkeypatch, tmp_path):
    _settings, storage, _surfer = _runtime(monkeypatch, tmp_path)
    path = tmp_path / "report.txt"
    path.write_text("hello", encoding="utf-8")
    record = storage.create_file_record(
        name=path.name, stored_path=path, sha256="a" * 64, size=5,
        mime_type="text/plain", status="stored", chunk_count=0,
    )
    # A window that spans "now" includes it; a past window excludes it.
    wide = storage.list_files_in_range("2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00")
    assert [row["id"] for row in wide] == [record["id"]]
    past = storage.list_files_in_range("2000-01-01T00:00:00+00:00", "2000-01-02T00:00:00+00:00")
    assert past == []
    storage.close()


# --- DocumentMemory date recall --------------------------------------------------

_WIDE = ("2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00")


def test_date_recall_lists_documents_without_reading(monkeypatch, tmp_path):
    _settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    path = tmp_path / "invoice-july.txt"
    path.write_text("Итого 5000 рублей.", encoding="utf-8")
    storage.create_file_record(
        name=path.name, stored_path=path, sha256="b" * 64, size=path.stat().st_size,
        mime_type="text/plain", status="stored", chunk_count=0,
    )
    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "какие документы были", date_from=_WIDE[0], date_to=_WIDE[1], list_only=True,
    )
    assert result["ok"] is True
    assert result["mode"] == "list"
    assert [doc["name"] for doc in result["documents"]] == ["invoice-july.txt"]
    assert result["analyses"] == []  # listing does not read file contents
    storage.close()


def test_date_recall_empty_range_is_honest(monkeypatch, tmp_path):
    _settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "документы", date_from="2000-01-01T00:00:00+00:00",
        date_to="2000-01-02T00:00:00+00:00", list_only=True,
    )
    assert result["ok"] is True
    assert result["documents"] == []


def test_date_recall_conclude_reads_and_analyzes(monkeypatch, tmp_path):
    settings, storage, surfer = _runtime(monkeypatch, tmp_path)
    path = tmp_path / "notes.md"
    path.write_text("# Итоги\n\nВыручка выросла на 12%.", encoding="utf-8")
    FileIngestor(settings, storage).ingest_path(path)
    result = DocumentMemory(storage=storage, surfer=surfer).recall(
        "сделай вывод", date_from=_WIDE[0], date_to=_WIDE[1], list_only=False,
    )
    assert result["ok"] is True
    assert result["mode"] == "conclude"
    assert [doc["name"] for doc in result["documents"]] == ["notes.md"]
    # Conclude mode read the file: passages/analyses are populated.
    assert result["sources"] and result["sources"][0]["name"] == "notes.md"
    assert "12%" in result["passages"][0]["content"]
    storage.close()
