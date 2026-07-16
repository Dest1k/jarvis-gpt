"""Independent search-provider fallback (Mojeek) for the web surfer.

DuckDuckGo-only search meant a single rate-limit blanked out every web answer.
Mojeek is a keyless, JS-free, bot-tolerant fallback. These tests pin its HTML
parser against markup shaped like Mojeek's real output.
"""

from __future__ import annotations

from jarvis_gpt.web_surfer import _parse_mojeek_html

# Shaped exactly like a real Mojeek results page (verified against a live fetch):
# each result is <a class="title" ... href="URL">TITLE</a> then <p class="s">SNIPPET</p>.
_MOJEEK_HTML = """
<ul class="results">
<li><a title="https://nodejs.org/en/about/previous-releases"
    href="https://nodejs.org/en/about/previous-releases" class="ob"><p class="i">
    <span class="url">nodejs.org</span></p></a>
    <h2><a class="title" title="t" href="https://nodejs.org/en/about/previous-releases">
    Node.js &mdash; Releases</a></h2>
    <p class="s">Starting with <strong>Node</strong>.js 27, the release cycle
    will be annual and every major version moves to <strong>LTS</strong>.</p></li>
<li><h2><a class="title" title="t2" href="https://codeforgeek.com/nodejs-lts-versions/">
    What are LTS Versions of Node.js</a></h2>
    <p class="s">A detailed guide to <strong>LTS</strong> versions.</p></li>
</ul>
"""


def test_parse_mojeek_html_extracts_results():
    results = _parse_mojeek_html(_MOJEEK_HTML)
    assert len(results) == 2
    first = results[0]
    assert first["url"] == "https://nodejs.org/en/about/previous-releases"
    assert first["title"] == "Node.js — Releases"  # entity unescaped, tags stripped
    assert "release cycle" in first["snippet"]
    assert "<strong>" not in first["snippet"]  # inline tags stripped
    assert results[1]["url"] == "https://codeforgeek.com/nodejs-lts-versions/"


def test_parse_mojeek_html_ignores_non_http_and_empty():
    assert _parse_mojeek_html("") == []
    assert _parse_mojeek_html("<a class='title' href='ftp://x'>x</a>") == []
