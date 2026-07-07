from __future__ import annotations

from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.ingest import FileIngestor
from jarvis_gpt.storage import JarvisStorage


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
    hits = storage.search_file_chunks("architecture mission", limit=5)
    audit = storage.list_audit(target_type="file", target_id=result["file"]["id"])

    assert result["file"]["status"] == "indexed"
    assert result["chunks_indexed"] == 1
    assert storage.counters()["files"] == 1
    assert hits
    assert hits[0]["file_id"] == result["file"]["id"]
    assert audit[0]["action"] == "file.ingest"
    storage.close()
