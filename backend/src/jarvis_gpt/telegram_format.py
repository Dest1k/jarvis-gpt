"""Render the model's Markdown answers into Telegram-flavoured HTML.

Telegram's ``parse_mode="HTML"`` supports a small fixed tag set — ``<b> <i> <u>
<s> <code> <pre> <a> <blockquote>`` (and ``<pre><code class="language-x">`` for a
syntax-highlighted code block). Everything else must be plain text with ``& < >``
escaped. We convert the model's standard Markdown into exactly that subset.

Why HTML and not MarkdownV2: MarkdownV2 needs ~18 special characters escaped
everywhere and still cannot express tables or headings, so raw model output trips
Telegram's 400 parser constantly. Targeting HTML we control the output fully and
only escape three characters in text nodes; Markdown constructs Telegram cannot
show (tables, headings) degrade gracefully to a monospace block / bold line.

Public API:
- ``render_telegram_html(markdown_text)`` -> a single HTML string (blocks joined by
  blank lines). No tag ever spans a newline except inside a ``<pre>`` block.
- ``split_telegram_html(html, limit)`` -> a list of <=limit-char HTML pieces, each
  independently valid (never split inside a tag or a ``<pre>`` block).
- ``html_to_plain(html)`` -> a tag-stripped, entity-unescaped fallback for when
  Telegram still rejects a piece.
"""

from __future__ import annotations

import re
from html import escape as _escape
from html import unescape as _unescape

TG_MSG_LIMIT = 4096

_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+\-.#]*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}")

# Inline spans, longest markers first so ``***`` beats ``**`` beats ``*``.
_INLINE_RE = re.compile(
    r"(?P<code>`(?P<code_t>[^`]+)`)"
    r"|(?P<bi>\*\*\*(?P<bi_t>[^*]+)\*\*\*)"
    r"|(?P<b1>\*\*(?P<b1_t>[^*]+)\*\*)"
    r"|(?P<b2>__(?P<b2_t>[^_]+)__)"
    r"|(?P<i1>\*(?P<i1_t>[^*]+)\*)"
    r"|(?P<i2>_(?P<i2_t>[^_]+)_)"
    r"|(?P<strike>~~(?P<strike_t>[^~]+)~~)"
    r"|(?P<link>\[(?P<link_t>[^\]]+)\]\((?P<link_u>[^)\s]+)\))"
)

_SAFE_LINK_RE = re.compile(r"^(https?://|tg://|mailto:)", re.IGNORECASE)


def _esc(text: str) -> str:
    """Escape a text node for Telegram HTML (only & < > matter)."""

    return _escape(text, quote=False)


def _render_inline(text: str) -> str:
    """Render Markdown inline spans within one logical line to Telegram HTML."""

    out: list[str] = []
    pos = 0
    for match in _INLINE_RE.finditer(text):
        out.append(_esc(text[pos : match.start()]))
        if match.group("code") is not None:
            out.append(f"<code>{_esc(match.group('code_t'))}</code>")
        elif match.group("bi") is not None:
            out.append(f"<b><i>{_esc(match.group('bi_t'))}</i></b>")
        elif match.group("b1") is not None:
            out.append(f"<b>{_esc(match.group('b1_t'))}</b>")
        elif match.group("b2") is not None:
            out.append(f"<b>{_esc(match.group('b2_t'))}</b>")
        elif match.group("i1") is not None:
            out.append(f"<i>{_esc(match.group('i1_t'))}</i>")
        elif match.group("i2") is not None:
            out.append(f"<i>{_esc(match.group('i2_t'))}</i>")
        elif match.group("strike") is not None:
            out.append(f"<s>{_esc(match.group('strike_t'))}</s>")
        elif match.group("link") is not None:
            label = _esc(match.group("link_t"))
            url = match.group("link_u")
            if _SAFE_LINK_RE.match(url):
                out.append(f'<a href="{_escape(url, quote=True)}">{label}</a>')
            else:
                out.append(f"{label} ({_esc(url)})")
        pos = match.end()
    out.append(_esc(text[pos:]))
    return "".join(out)


def _render_code_block(code: str, lang: str) -> str:
    body = _esc(code)
    lang = re.sub(r"[^A-Za-z0-9_+\-.#]", "", lang).lower()
    if lang:
        return f'<pre><code class="language-{lang}">{body}</code></pre>'
    return f"<pre>{body}</pre>"


def _render_table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    grid = [row + [""] * (width - len(row)) for row in rows]
    col_widths = [max(len(grid[r][c]) for r in range(len(grid))) for c in range(width)]
    lines: list[str] = []
    for r, row in enumerate(grid):
        cells = [row[c].ljust(col_widths[c]) for c in range(width)]
        lines.append(" | ".join(cells).rstrip())
        if r == 0:
            lines.append("-+-".join("-" * col_widths[c] for c in range(width)))
    return f"<pre>{_esc(chr(10).join(lines))}</pre>"


def _is_fence_close(line: str, marker: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 3 and set(stripped) == {marker}


def render_telegram_html(markdown_text: str) -> str:
    """Convert Markdown to a single Telegram-HTML string (blocks joined by ``\\n\\n``)."""

    src = str(markdown_text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = src.split("\n")
    blocks: list[str] = []
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]

        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)[0]
            lang = fence.group(2) or ""
            code_lines: list[str] = []
            i += 1
            while i < total and not _is_fence_close(lines[i], marker):
                code_lines.append(lines[i])
                i += 1
            i += 1  # consume the closing fence (or run off the end)
            blocks.append(_render_code_block("\n".join(code_lines), lang))
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            blocks.append(f"<b>{_render_inline(heading.group(2).strip())}</b>")
            i += 1
            continue

        if _HR_RE.match(line):
            blocks.append("<b>────────</b>")
            i += 1
            continue

        if (
            "|" in line
            and i + 1 < total
            and _TABLE_SEP_RE.match(lines[i + 1])
        ):
            table_lines = [line]
            i += 1
            while i < total and "|" in lines[i]:
                table_lines.append(lines[i])
                i += 1
            rows = _markdown_table_rows(table_lines)
            if rows:
                blocks.append(_render_table(rows))
            continue

        quote = _QUOTE_RE.match(line)
        if quote and line.lstrip().startswith(">"):
            quote_lines: list[str] = []
            while i < total:
                inner = _QUOTE_RE.match(lines[i])
                if inner and lines[i].lstrip().startswith(">"):
                    quote_lines.append(inner.group(1))
                    i += 1
                else:
                    break
            rendered = "\n".join(_render_inline(q) for q in quote_lines)
            blocks.append(f"<blockquote>{rendered}</blockquote>")
            continue

        bullet = _BULLET_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if bullet or ordered:
            item_lines: list[str] = []
            while i < total:
                b = _BULLET_RE.match(lines[i])
                o = _ORDERED_RE.match(lines[i])
                if b:
                    indent = "  " * (len(b.group(1)) // 2)
                    item_lines.append(f"{indent}• {_render_inline(b.group(2).strip())}")
                    i += 1
                elif o:
                    indent = "  " * (len(o.group(1)) // 2)
                    item_lines.append(
                        f"{indent}{o.group(2)}. {_render_inline(o.group(3).strip())}"
                    )
                    i += 1
                else:
                    break
            blocks.append("\n".join(item_lines))
            continue

        if not line.strip():
            i += 1
            continue

        # Paragraph: keep accumulating until a blank line or a new block starts.
        para: list[str] = [line]
        i += 1
        while i < total:
            nxt = lines[i]
            if (
                not nxt.strip()
                or _FENCE_RE.match(nxt)
                or _HEADING_RE.match(nxt)
                or _HR_RE.match(nxt)
                or _BULLET_RE.match(nxt)
                or _ORDERED_RE.match(nxt)
                or (nxt.lstrip().startswith(">"))
                or ("|" in nxt and i + 1 < total and _TABLE_SEP_RE.match(lines[i + 1]))
            ):
                break
            para.append(nxt)
            i += 1
        rendered = "\n".join(_render_inline(p) for p in para)
        blocks.append(rendered)

    return "\n\n".join(block for block in blocks if block)


def _markdown_table_rows(table_lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line_index, line in enumerate(table_lines):
        stripped = line.strip().strip("|")
        cells = [cell.strip() for cell in stripped.split("|")]
        if line_index == 1 and cells and all(re.match(r"^:?-{3,}:?$", cell) for cell in cells):
            continue
        if any(cells):
            rows.append(cells)
    return rows


# ---------------------------------------------------------------------------
# Length-safe chunking
# ---------------------------------------------------------------------------

# Longer variant first: alternation is ordered, so match <pre><code ...> before bare <pre>.
_PRE_OPEN_RE = re.compile(r'^<pre><code(?: class="[^"]*")?>|^<pre>')


def _split_pre_block(block: str, limit: int) -> list[str]:
    """Split an over-long <pre> block into several valid <pre> blocks by line."""

    open_match = _PRE_OPEN_RE.match(block)
    if not open_match:
        return _split_plain(block, limit)
    open_tag = open_match.group(0)
    close_tag = "</code></pre>" if "<code" in open_tag else "</pre>"
    inner = block[len(open_tag) : -len(close_tag)]
    budget = limit - len(open_tag) - len(close_tag)
    if budget <= 0:
        return _split_plain(block, limit)
    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in inner.split("\n"):
        add = len(line) + (1 if current else 0)
        if current and current_len + add > budget:
            pieces.append(open_tag + "\n".join(current) + close_tag)
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += add
    if current:
        pieces.append(open_tag + "\n".join(current) + close_tag)
    return pieces or [block]


def _split_plain(block: str, limit: int) -> list[str]:
    """Split a non-<pre> block. Newlines are always tag-safe (no tag spans one)."""

    if len(block) <= limit:
        return [block]
    pieces: list[str] = []
    current = ""
    for line in block.split("\n"):
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
            current = ""
        while len(line) > limit:
            cut = _safe_cut(line, limit)
            pieces.append(line[:cut])
            line = line[cut:]
        current = line
    if current:
        pieces.append(current)
    return pieces


def _safe_cut(line: str, limit: int) -> int:
    """Find a cut index <= limit that does not land inside a ``<...>`` tag."""

    cut = line.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    # Back off if the cut would sit inside an unclosed tag.
    head = line[:cut]
    if head.rfind("<") > head.rfind(">"):
        safe = head.rfind("<")
        if safe > 0:
            return safe
    return cut


def _split_block(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]
    if _PRE_OPEN_RE.match(block):
        return _split_pre_block(block, limit)
    return _split_plain(block, limit)


def split_telegram_html(html: str, limit: int = TG_MSG_LIMIT) -> list[str]:
    """Split rendered HTML into <=limit pieces, each independently valid."""

    blocks = html.split("\n\n")
    chunks: list[str] = []
    current = ""
    for block in blocks:
        for piece in _split_block(block, limit):
            if not current:
                current = piece
            elif len(current) + 2 + len(piece) <= limit:
                current = current + "\n\n" + piece
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks or [""]


_TAG_RE = re.compile(r"<[^>]+>")


def html_to_plain(html: str) -> str:
    """Strip Telegram HTML back to plain text (fallback when HTML is rejected)."""

    return _unescape(_TAG_RE.sub("", html))
