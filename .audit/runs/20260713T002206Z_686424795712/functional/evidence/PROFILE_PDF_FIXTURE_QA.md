# Profile PDF fixture QA

- Scope: `mono-perf-good-{1..3}.pdf` and `mono-perf-bad-{1..3}.pdf`.
- Good fixtures: 3/3 are one-page Letter PDFs; `pdfinfo`, `pdfplumber`, and
  `pypdf` agree on structure and recover the expected unique marker and
  controlled conclusion.
- Visual QA: 3/3 latest Poppler PNG renders were inspected at 120 DPI. Text,
  margins, hierarchy, footer, and page boundaries are legible with no clipping,
  overlap, black boxes, or broken glyphs.
- Bad fixtures: 3/3 are intentionally truncated and rejected by `pypdf` with a
  missing EOF condition.
- Manifest: `gui-fixtures/profile-pdf-fixtures-manifest.json` records exclusive
  creation, byte counts, and SHA-256 values for all 14 profile fixtures.

Result: `PASS` for controlled good/bad PDF fixture quality.
