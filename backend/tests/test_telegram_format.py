"""Tests for the Markdown -> Telegram-HTML renderer (telegram_format)."""

from jarvis_gpt.telegram_format import (
    html_to_plain,
    render_telegram_html,
    split_telegram_html,
)


def test_fenced_code_block_becomes_pre_code_with_language():
    md = "Смотри:\n\n```python\nprint('hi')\nx = 1 < 2\n```"
    html = render_telegram_html(md)
    assert '<pre><code class="language-python">' in html
    assert "print(&#x27;hi&#x27;)" in html or "print('hi')" in html
    # The literal < inside code must be escaped so Telegram does not parse it.
    assert "x = 1 &lt; 2" in html
    assert html.count("</code></pre>") == 1


def test_fenced_code_without_language_is_plain_pre():
    html = render_telegram_html("```\njust code\n```")
    assert html.startswith("<pre>just code</pre>")
    assert "<code" not in html


def test_inline_spans_render_to_html():
    html = render_telegram_html("Use `run()` and **bold** and *italic* and ~~old~~.")
    assert "<code>run()</code>" in html
    assert "<b>bold</b>" in html
    assert "<i>italic</i>" in html
    assert "<s>old</s>" in html


def test_text_special_chars_are_escaped():
    html = render_telegram_html("a < b & c > d")
    assert "a &lt; b &amp; c &gt; d" in html
    assert "<b" not in html  # no stray tags introduced


def test_safe_link_renders_anchor_unsafe_stays_text():
    ok = render_telegram_html("[docs](https://example.com/x)")
    assert '<a href="https://example.com/x">docs</a>' in ok
    unsafe = render_telegram_html("[x](javascript:alert(1))")
    assert "<a " not in unsafe
    assert "javascript:alert(1)" in unsafe


def test_heading_becomes_bold():
    html = render_telegram_html("## Заголовок")
    assert html == "<b>Заголовок</b>"


def test_lists_render_bullets_and_numbers():
    html = render_telegram_html("- one\n- two")
    assert "• one" in html and "• two" in html
    ordered = render_telegram_html("1. first\n2. second")
    assert "1. first" in ordered and "2. second" in ordered


def test_blockquote_renders():
    html = render_telegram_html("> quoted line")
    assert html == "<blockquote>quoted line</blockquote>"


def test_table_becomes_aligned_monospace_pre():
    md = "| id | name |\n| --- | --- |\n| 1 | Bob |"
    html = render_telegram_html(md)
    assert html.startswith("<pre>")
    assert "id | name" in html
    assert "1  | Bob" in html  # 'id' column width pads '1' -> '1 '
    assert "-+-" in html  # separator row


def test_large_code_block_splits_into_valid_pre_pieces():
    body = "\n".join(f"line number {i:04d} with padding" for i in range(200))
    md = f"```text\n{body}\n```"
    html = render_telegram_html(md)
    pieces = split_telegram_html(html, limit=300)
    assert len(pieces) > 1
    for piece in pieces:
        assert len(piece) <= 300
        assert piece.startswith('<pre><code class="language-text">')
        assert piece.endswith("</code></pre>")
    # Every original line survives across the pieces.
    recovered = "".join(html_to_plain(p) for p in pieces)
    for i in range(200):
        assert f"line number {i:04d}" in recovered


def test_no_piece_exceeds_limit_for_mixed_document():
    md = (
        "# Title\n\nSome **bold** intro paragraph.\n\n"
        + "```python\n"
        + "\n".join(f"a = {i}" for i in range(500))
        + "\n```\n\n- bullet one\n- bullet two\n"
    )
    html = render_telegram_html(md)
    pieces = split_telegram_html(html, limit=500)
    assert pieces
    assert all(len(p) <= 500 for p in pieces)


def test_plain_paragraph_splits_on_newlines():
    md = "\n".join(f"sentence {i}" for i in range(100))
    html = render_telegram_html(md)
    pieces = split_telegram_html(html, limit=120)
    assert all(len(p) <= 120 for p in pieces)
    assert "".join(html_to_plain(p) for p in pieces).count("sentence") == 100


def test_html_to_plain_strips_tags_and_unescapes():
    html = '<pre><code class="language-py">x &lt; 1</code></pre>'
    assert html_to_plain(html) == "x < 1"


def test_plain_text_is_unchanged_after_render():
    # A message with no markdown should read the same (just escaped).
    assert render_telegram_html("Привет, как дела?") == "Привет, как дела?"
