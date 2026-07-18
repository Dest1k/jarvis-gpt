from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

_MANAGED_BY = "jarvis-gpt"


class MemoryVault:
    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def upsert_memory(self, memory: dict[str, Any]) -> Path:
        self.ensure()
        path = self._memory_path(memory)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, _render_memory_note(memory))
        return path

    def remove_memory(self, memory_id: str) -> int:
        removed = 0
        failures: list[OSError] = []
        for path in self.root.glob(f"**/{_safe_slug(memory_id)}.md"):
            try:
                note = _parse_note(path, self.root)
                relative_path = str(path.relative_to(self.root))
                if not _is_managed_note(note, relative_path):
                    continue
                path.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                failures.append(exc)
        if failures:
            raise failures[0]
        return removed

    def sync(self, memories: list[dict[str, Any]]) -> dict[str, Any]:
        self.ensure()
        known_ids = {str(item.get("id") or "") for item in memories}
        expected_paths: dict[str, str] = {}
        written = 0
        for memory in memories:
            path = self.upsert_memory(memory)
            memory_id = str(memory.get("id") or "")
            if memory_id:
                expected_paths[memory_id] = os.path.normcase(str(path.relative_to(self.root)))
            written += 1
        removed = 0
        failures: list[OSError] = []
        for path in self.root.glob("**/*.md"):
            try:
                note = _parse_note(path, self.root)
            except FileNotFoundError:
                continue
            except OSError as exc:
                failures.append(exc)
                continue
            note_id = str(note.get("id") or "")
            relative_path = str(path.relative_to(self.root))
            expected_path = expected_paths.get(note_id)
            stale_managed_note = _is_managed_note(note, relative_path) and (
                note_id not in known_ids
                or expected_path is None
                or os.path.normcase(relative_path) != expected_path
            )
            if stale_managed_note:
                try:
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    failures.append(exc)
        if failures:
            raise failures[0]
        return {"root": str(self.root), "written": written, "removed": removed}

    def graph(self) -> dict[str, Any]:
        self.ensure()
        notes: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("**/*.md")):
            try:
                notes.append(_parse_note(path, self.root))
            except OSError:
                # The vault is a live mirror. A concurrent replace/remove or one
                # unreadable manual note must not take down the complete graph.
                continue
        return self._build_graph(notes)

    def graph_from_memories(self, memories: list[dict[str, Any]]) -> dict[str, Any]:
        """Build the managed part of the graph from DB rows and retain manual notes.

        Managed notes are rendered and parsed in memory, avoiding a cold disk read for each
        DB row. Hand-written ``.md`` files are also part of the vault contract: enumerate
        paths, skip every expected managed path without opening it, and parse only leftovers.
        A leftover carrying an ID already present in the DB is a moved/stale managed copy,
        not a second manual note, so it is ignored after parsing.
        """

        def _sort_key(memory: dict[str, Any]) -> tuple[str, str]:
            return (
                _safe_slug(str(memory.get("namespace") or "core")),
                _safe_slug(str(memory.get("id") or "memory")),
            )

        managed_ids = {str(memory.get("id") or "") for memory in memories}
        managed_ids.discard("")
        managed_paths: set[str] = set()
        notes_by_path: list[tuple[str, dict[str, Any]]] = []
        for memory in sorted(memories, key=_sort_key):
            namespace_slug = _safe_slug(str(memory.get("namespace") or "core"))
            id_slug = _safe_slug(str(memory.get("id") or "memory"))
            relative_path = str(Path(namespace_slug) / f"{id_slug}.md")
            managed_paths.add(os.path.normcase(relative_path))
            notes_by_path.append(
                (
                    relative_path,
                    _parse_note_text(
                        _render_memory_note(memory),
                        path_str=relative_path,
                        stem=id_slug,
                    ),
                )
            )

        for path in sorted(self.root.glob("**/*.md")):
            relative_path = str(path.relative_to(self.root))
            if os.path.normcase(relative_path) in managed_paths:
                continue
            try:
                note = _parse_note(path, self.root)
            except OSError:
                # Files can disappear between glob() and read_text() in another
                # process. Managed rows still come from the consistent DB snapshot.
                continue
            note_id = str(note.get("id") or "")
            if _is_managed_note(note, relative_path) or (
                note_id and note_id in managed_ids
            ):
                continue
            notes_by_path.append((relative_path, note))

        notes = [note for _, note in sorted(notes_by_path, key=lambda item: item[0])]
        return self._build_graph(notes)

    def _build_graph(self, notes: list[dict[str, Any]]) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []
        backlinks: dict[str, list[str]] = {}

        for note in notes:
            note_id = str(note.get("id") or note.get("path") or "")
            if not note_id:
                continue
            nodes[note_id] = {
                "id": note_id,
                "label": note.get("title") or note_id,
                "kind": "memory",
                "path": note.get("path"),
                "namespace": note.get("namespace"),
                "tags": note.get("tags") or [],
                "importance": note.get("importance"),
                "updated_at": note.get("updated_at"),
            }
            namespace = str(note.get("namespace") or "core")
            namespace_id = f"namespace:{namespace}"
            nodes.setdefault(
                namespace_id,
                {"id": namespace_id, "label": namespace, "kind": "namespace"},
            )
            edges.append({"source": note_id, "target": namespace_id, "kind": "namespace"})

            for tag in note.get("tags") or []:
                tag_id = f"tag:{tag}"
                nodes.setdefault(tag_id, {"id": tag_id, "label": f"#{tag}", "kind": "tag"})
                edges.append({"source": note_id, "target": tag_id, "kind": "tag"})

            for link in note.get("links") or []:
                link_id = f"link:{link}"
                nodes.setdefault(link_id, {"id": link_id, "label": str(link), "kind": "link"})
                edges.append({"source": note_id, "target": link_id, "kind": "link"})
                backlinks.setdefault(str(link), []).append(note_id)

        degree: dict[str, int] = {}
        for edge in edges:
            degree[edge["source"]] = degree.get(edge["source"], 0) + 1
            degree[edge["target"]] = degree.get(edge["target"], 0) + 1
        for node_id, node in nodes.items():
            node["degree"] = degree.get(node_id, 0)
        top_nodes = sorted(
            ({**node} for node in nodes.values()),
            key=lambda item: (int(item.get("degree") or 0), str(item.get("label") or "")),
            reverse=True,
        )[:12]
        return {
            "root": str(self.root),
            "notes": notes,
            "nodes": list(nodes.values()),
            "edges": edges,
            "backlinks": backlinks,
            "top_nodes": top_nodes,
            "stats": {
                "notes": len(notes),
                "nodes": len(nodes),
                "edges": len(edges),
                "backlinks": sum(len(value) for value in backlinks.values()),
            },
        }

    def _memory_path(self, memory: dict[str, Any]) -> Path:
        namespace = _safe_slug(str(memory.get("namespace") or "core"))
        memory_id = _safe_slug(str(memory.get("id") or "memory"))
        root = self.root.resolve()
        candidate = (root / namespace / f"{memory_id}.md").resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("memory note path escapes the configured vault")
        return candidate


def _render_memory_note(memory: dict[str, Any]) -> str:
    content = str(memory.get("content") or "").strip()
    tags = [str(tag).strip() for tag in memory.get("tags") or [] if str(tag).strip()]
    links = _extract_links(content)
    title = _note_title(memory)
    frontmatter = {
        "id": str(memory.get("id") or ""),
        "managed_by": _MANAGED_BY,
        "namespace": str(memory.get("namespace") or "core"),
        "importance": memory.get("importance", 0.5),
        "created_at": str(memory.get("created_at") or ""),
        "updated_at": str(memory.get("updated_at") or ""),
        "tags": tags,
        "links": links,
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {_yaml_value(value)}")
    lines.extend(["---", "", f"# {title}", "", content])
    if links:
        lines.extend(["", "## Links", ""])
        lines.extend(f"- [[{link}]]" for link in links)
    if tags:
        lines.extend(["", "## Tags", ""])
        lines.append(" ".join(f"#{_safe_tag(tag)}" for tag in tags))
    return "\n".join(lines).rstrip() + "\n"


def _parse_note(path: Path, root: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return _parse_note_text(text, path_str=str(path.relative_to(root)), stem=path.stem)


def _parse_note_text(text: str, *, path_str: str, stem: str) -> dict[str, Any]:
    """Parse a rendered note from its text alone (no disk).

    Shared by the on-disk :func:`_parse_note` and the in-memory graph builder so both
    derive title/content/links/tags identically — tags mined from the note body and all.
    """

    frontmatter: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        _, raw_frontmatter, body = text.split("---", 2)
        frontmatter = _parse_frontmatter(raw_frontmatter)
    title_match = re.search(r"(?m)^#\s+(.+)$", body)
    content = re.sub(r"(?m)^#\s+.+$", "", body, count=1).strip()
    links = sorted(set([*_extract_links(text), *list(frontmatter.get("links") or [])]))
    tags = sorted(set([*_extract_tags(text), *list(frontmatter.get("tags") or [])]))
    return {
        **frontmatter,
        "title": title_match.group(1).strip() if title_match else stem,
        "content": content,
        "links": links,
        "tags": tags,
        "path": path_str,
    }


def _is_managed_note(note: dict[str, Any], path_str: str) -> bool:
    """Identify generated notes without consuming hand-written ID frontmatter.

    New notes carry an explicit marker. The narrow legacy fallback recognizes the
    historical ``namespace/mem_id.md`` layout so orphaned mirrors from an older
    release can be repaired without classifying arbitrary manual notes as managed.
    """

    if str(note.get("managed_by") or "") == _MANAGED_BY:
        return True
    note_id = str(note.get("id") or "")
    if not note_id.startswith("mem_"):
        return False
    relative_path = Path(path_str)
    namespace = _safe_slug(str(note.get("namespace") or "core"))
    return (
        relative_path.stem == _safe_slug(note_id)
        and relative_path.parent.name == namespace
        and bool(note.get("created_at"))
        and bool(note.get("updated_at"))
    )


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            data[key] = [item.strip().strip('"') for item in value[1:-1].split(",") if item.strip()]
        elif key == "importance":
            try:
                data[key] = float(value)
            except ValueError:
                data[key] = 0.5
        else:
            data[key] = value.strip('"')
    return data


def _note_title(memory: dict[str, Any]) -> str:
    namespace = str(memory.get("namespace") or "core")
    content = " ".join(str(memory.get("content") or "").split())
    if content:
        return f"{namespace}: {content[:64]}"
    return str(memory.get("id") or "memory")


def _extract_links(text: str) -> list[str]:
    return sorted(
        {match.strip() for match in re.findall(r"\[\[([^\]]+)\]\]", text) if match.strip()}
    )


def _extract_tags(text: str) -> list[str]:
    return sorted(
        {match.strip() for match in re.findall(r"(?<!\w)#([\w-]+)", text) if match.strip()}
    )


def _safe_slug(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE)
    value = value.strip(" .-")[:120].rstrip(" .")
    return value if value not in {"", ".", ".."} else "memory"


def _safe_tag(value: str) -> str:
    return re.sub(r"[^\w-]+", "-", value.strip(), flags=re.UNICODE).strip("-")


def _yaml_value(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(f'"{_escape_yaml(str(item))}"' for item in value) + "]"
    if isinstance(value, int | float):
        return str(value)
    return f'"{_escape_yaml(str(value))}"'


def _escape_yaml(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _atomic_write_text(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
