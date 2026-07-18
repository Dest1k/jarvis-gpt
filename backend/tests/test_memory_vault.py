from pathlib import Path

import jarvis_gpt.memory_vault as memory_vault_module
from jarvis_gpt.memory_vault import MemoryVault


def _memory(memory_id: str = "mem_managed") -> dict[str, object]:
    return {
        "id": memory_id,
        "namespace": "core",
        "content": "Managed memory [[shared]] #managed",
        "tags": [],
        "importance": 0.5,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }


def test_graph_from_memories_reads_only_unmanaged_disk_notes(monkeypatch, tmp_path):
    vault = MemoryVault(tmp_path / "vault")
    memory = _memory()
    managed_path = vault.upsert_memory(memory)
    manual_path = vault.root / "manual.md"
    manual_path.write_text("# Manual note\n\nHandwritten [[shared]] #manual\n", encoding="utf-8")
    unmanaged_path = vault.root / "unmanaged.md"
    unmanaged_path.write_text(
        "---\nid: handmade\nnamespace: notes\n---\n\n# Handmade\n\nUnmanaged note\n",
        encoding="utf-8",
    )
    stale_copy = vault.root / "moved-managed.md"
    stale_copy.write_text(
        "---\nid: mem_managed\nnamespace: core\n---\n\n# Stale\n\nOld copy\n",
        encoding="utf-8",
    )

    parsed_paths: list[Path] = []
    original_parse_note = memory_vault_module._parse_note

    def tracked_parse_note(path: Path, root: Path):
        assert path != managed_path
        parsed_paths.append(path)
        return original_parse_note(path, root)

    monkeypatch.setattr(memory_vault_module, "_parse_note", tracked_parse_note)

    graph = vault.graph_from_memories([memory])

    assert set(parsed_paths) == {manual_path, unmanaged_path, stale_copy}
    memory_nodes = {node["id"] for node in graph["nodes"] if node["kind"] == "memory"}
    assert "mem_managed" in memory_nodes
    assert "manual.md" in memory_nodes
    assert "handmade" in memory_nodes
    assert graph["stats"]["notes"] == 3
    assert set(graph["backlinks"]["shared"]) == {"mem_managed", "manual.md"}


def test_orphaned_managed_note_is_not_resurrected_as_manual(tmp_path):
    vault = MemoryVault(tmp_path / "vault")
    orphan_path = vault.upsert_memory(_memory("mem_orphan"))
    manual_path = vault.root / "manual-with-id.md"
    manual_path.write_text(
        "---\nid: handmade\nnamespace: notes\n---\n\n# Manual\n\nKeep me\n",
        encoding="utf-8",
    )

    graph = vault.graph_from_memories([])
    memory_nodes = {node["id"] for node in graph["nodes"] if node["kind"] == "memory"}

    assert "mem_orphan" not in memory_nodes
    assert "handmade" in memory_nodes

    result = vault.sync([])
    assert result["removed"] == 1
    assert not orphan_path.exists()
    assert manual_path.exists()


def test_graph_from_memories_tolerates_concurrent_manual_delete(monkeypatch, tmp_path):
    vault = MemoryVault(tmp_path / "vault")
    vault.ensure()
    manual_path = vault.root / "manual.md"
    manual_path.write_text("# Manual\n\nTransient\n", encoding="utf-8")
    original_parse_note = memory_vault_module._parse_note

    def delete_before_read(path: Path, root: Path):
        path.unlink()
        return original_parse_note(path, root)

    monkeypatch.setattr(memory_vault_module, "_parse_note", delete_before_read)

    graph = vault.graph_from_memories([])

    assert graph["stats"]["notes"] == 0
