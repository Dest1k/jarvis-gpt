from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class MemoryVault:
    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def upsert_memory(self, memory: dict[str, Any]) -> Path:
        self.ensure()
        path = self._memory_path(memory)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_memory_note(memory), encoding="utf-8")
        return path

    def remove_memory(self, memory_id: str) -> None:
        for path in self.root.glob(f"**/{_safe_slug(memory_id)}.md"):
            try:
                path.unlink()
            except OSError:
                continue

    def sync(self, memories: list[dict[str, Any]]) -> dict[str, Any]:
        self.ensure()
        known_ids = {str(item.get("id") or "") for item in memories}
        written = 0
        for memory in memories:
            self.upsert_memory(memory)
            written += 1
        removed = 0
        for path in self.root.glob("**/*.md"):
            note = _parse_note(path, self.root)
            note_id = str(note.get("id") or "")
            if note_id and note_id not in known_ids:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return {"root": str(self.root), "written": written, "removed": removed}

    def graph(self) -> dict[str, Any]:
        self.ensure()
        notes = [_parse_note(path, self.root) for path in sorted(self.root.glob("**/*.md"))]
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
        top_nodes = sorted(
            (
                {**node, "degree": degree.get(node_id, 0)}
                for node_id, node in nodes.items()
            ),
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
        return self.root / namespace / f"{memory_id}.md"


def _render_memory_note(memory: dict[str, Any]) -> str:
    content = str(memory.get("content") or "").strip()
    tags = [str(tag).strip() for tag in memory.get("tags") or [] if str(tag).strip()]
    links = _extract_links(content)
    title = _note_title(memory)
    frontmatter = {
        "id": str(memory.get("id") or ""),
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
        "title": title_match.group(1).strip() if title_match else path.stem,
        "content": content,
        "links": links,
        "tags": tags,
        "path": str(path.relative_to(root)),
    }


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
    value = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE).strip("-")
    return value[:120] or "memory"


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
