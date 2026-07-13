# DOCX Fixture Structural QA

Scope: `.audit/runs/20260713T002206Z_686424795712/functional/evidence/gui-fixtures` only. The fixtures were inspected read-only and were not modified.

## Result

- Structural/package QA: **PASS** for all three DOCX fixtures.
- Manifest parity: **PASS** — schema `jarvis.functional-fixtures.v1`, 28 declared payload files, 28 actual payload files, no missing or extra files.
- Manifest integrity: **PASS** — declared byte counts and SHA-256 hashes match all 28 payload files.
- Manifest SHA-256: `018bc0bf08a9911ef618d98bce58ca16ba88722e9fe923a19e6658fe4398a2e8`.

| Fixture | Bytes | SHA-256 | ZIP entries | `STATUS_OLD` | Expected source marker | Result |
|---|---:|---|---:|---:|---|---|
| `source-copy-1.docx` | 36,793 | `1ab717580ce8fdbfbe5a3e080d06bd3a5b7d90d8fc44c08a0da56021f3becef7` | 17 | 1 | `marker=SOURCE-1` | PASS |
| `source-copy-2.docx` | 36,793 | `59fd5e4ef4c8f5207530920700d04cc0b6d174c35832e4416bfd3a3f1c03a7aa` | 17 | 1 | `marker=SOURCE-2` | PASS |
| `source-copy-3.docx` | 36,794 | `eeb9f5e6cb9fe173c22fa372bd00803323ebd55182cf0e8ee74d57d9981def67` | 17 | 1 | `marker=SOURCE-3` | PASS |

## Package checks

For each DOCX:

- every ZIP entry was read through decompression without error;
- all XML and relationship parts are well-formed;
- `[Content_Types].xml`, `_rels/.rels`, `word/document.xml`, and `word/_rels/document.xml.rels` are present;
- no duplicate, traversal-style, absolute, or backslash ZIP member names were found;
- the package-level office-document relationship resolves to `word/document.xml`;
- all internal relationships from `word/document.xml.rels` resolve to existing parts;
- the main document content type is correct and every package part has a declared/default content type.

Each `word/document.xml` contains the title `Synthetic Copy-on-Write Fixture`, exactly one `STATUS_OLD` token, its matching `marker=SOURCE-N`, and the instruction that edits belong in a new copy. This is the expected source state for copy-on-write replacement cases.

## Visual QA limitation

LibreOffice/`soffice` is unavailable, so DOCX-to-PNG rendering and visual inspection are **BLOCKED_BY_ENV**. The empty `render-source-copy-1` staging directory contains no rendered pages and is not a manifest payload. This report does not claim that layout, clipping, fonts, pagination, or other visual properties passed the render gate.
