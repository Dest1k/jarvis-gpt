# Ideal Jarvis Enhancements

**Branch:** `feature/ideal-jarvis-all-enhancements`

## Release status

| Area | Status | Notes |
|------|--------|-------|
| **document_surfer** | **Production** | Isolated black-box for documents + archives + file identify |
| **file_types** | **Production** | Magic-byte / extension type recognition |
| **archive_runtime** | **Production** | Safe list/extract/read/create for common archives |
| documents.* tools | Production | Wired into `ToolRegistry` + agent safe allowlist |
| document_runtime | Production | Low-level extract/compare/replace engine |
| document_agent | Production facade | Thin wrapper over document_surfer |
| vision / calendar / email / voice / plugins / briefing | Experimental | Safe stubs, no import side-effects, not auto-registered as core tools |
| knowledge_graph | Experimental | Extractive graph from document corpus signals |

## document_surfer (release core of this branch)

Module: `backend/src/jarvis_gpt/document_surfer.py`

Public surface (`JarvisDocumentSurfer`):

- `identify` / `probe` (any file)
- `inspect` / `read` / `analyze` / `review`
- `compare` / `search` / `summarize_corpus`
- `list_archive` / `extract_archive` / `read_archive_member` / `create_archive` / `search_archive`
- `edit_plan` / `apply_replacements` (copy-on-write only)
- `generate` (md, txt, csv, json, html, docx, xlsx)
- `convert` / `package` / `capabilities`

Tool names:

- `documents.inspect`, `documents.read`, `documents.review`, `documents.compare`
- `documents.edit.plan`, `documents.apply_replacements`
- `documents.analyze`, `documents.search`, `documents.corpus.summarize`
- `documents.generate`, `documents.convert`, `documents.capabilities`
- `documents.file.identify`, `documents.file.probe`
- `documents.archive.list|extract|read_member|create|search`

Formats:

- Core extract: docx, xlsx/xlsm, pdf, txt/md/csv/tsv/json/xml/html/log (+ legacy markers for doc/xls)
- Extended extract: pptx, odt, rtf (best-effort)
- Archives: zip/jar/whl, tar, tar.gz/tgz, tar.bz2, tar.xz, gz, bz2, xz; optional 7z/rar
- Generate: md, txt, csv, json, html, docx, xlsx; archive create: zip/tar/tar.*/gz

Safety:

- Never overwrites originals
- Size limits + XXE-safe Office/XML parsing
- Output under `JARVIS_HOME/data/document-outputs` via tools

## Integration

See `INTEGRATION_GUIDE.md`. Core document tools are already registered in `tools.py`;
`ideal_tools_registration.py` is diagnostics-only for experimental modules.

## Tests

```powershell
$env:PYTHONPATH = "backend/src"
python -m pytest backend/tests/test_document_surfer.py backend/tests/test_document_runtime.py -q
```
