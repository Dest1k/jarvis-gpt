# Turbo Artifact Validation

- Campaign: `20260713T002206Z_686424795712`
- Profile: `gemma4-turbo`
- Scope: `OP-0013`, `OP-0029`, `OP-0030`, `OP-0031`, `OP-0034`
- Evidence: current operator catalog, matching records 78-91 in `gui_operator_runs.jsonl`, fixture manifest, and the isolated campaign namespace only.
- Overall verdict: **FAIL**.

The isolated `document-outputs` directory contains nine in-scope files. Their names and SHA-256 hashes are unique, and every file is accounted for below. The campaign does not satisfy the catalog because requested artifacts are missing or misplaced, and both Markdown-to-DOCX conversions fail the heading/table requirements.

## Source integrity

All five uploaded sources still match their pre-run fixture-manifest SHA-256 values.

| Source | SHA-256 | Result |
|---|---|---|
| `1ab717580ce8_source-copy-1.docx` | `1ab717580ce8fdbfbe5a3e080d06bd3a5b7d90d8fc44c08a0da56021f3becef7` | PASS |
| `59fd5e4ef4c8_source-copy-2.docx` | `59fd5e4ef4c8f5207530920700d04cc0b6d174c35832e4416bfd3a3f1c03a7aa` | PASS |
| `eeb9f5e6cb9f_source-copy-3.docx` | `eeb9f5e6cb9fe173c22fa372bd00803323ebd55182cf0e8ee74d57d9981def67` | PASS |
| `5525442d9707_convert-1.md` | `5525442d9707fb4d3db88fab5ca35cf12e3dfe20baa40a34fb6925a30132338e` | PASS |
| `d84608bbcc89_convert-2.md` | `d84608bbcc898a657fc14e173ee8d8dd5cf66072eb4e46730c60c3a344f0017c` | PASS |

## Operation results

### OP-0013 — FAIL

All three requested paths under `document-outputs\functional-20260713\` are missing. Three files were instead created in the parent `document-outputs` directory:

| Repeat | Actual file SHA-256 | Content | Result |
|---|---|---|---|
| 1 | `OP-0013-1.md` — `97642043c8506ddb73111a5ec0f321ef90d98a0fa5ee87ac9e29e60e5642a21a` | Strict UTF-8; required heading and marker present; generator heading/metadata makes the body non-exact | FAIL |
| 2 | `OP-0013-2.md` — `dceb72468234215cf1d12be290961cb51df6e52468ce8a10264013a64ae8f3cb` | Same | FAIL |
| 3 | `OP-0013-3.md` — `4b9756bfdf5dc6f6779d2fd7b3af91fa8d37dd6cf6f1aa9c92e3a6d17feff1ad` | Same; raw GUI correction also names a different nonexistent file | FAIL |

### OP-0029 — FAIL

Repeat 1 created `functional-report-1.md` at the correct root (`fe39246d8a8d8ea814a3fc1fbfd7876121723ab67cea2b8d472a2f61736f0d27`). The required `# Итог` heading and `marker OP-0029-1` are present, but the generator heading/metadata makes the body non-exact. Repeats 2 and 3 produced no files; their GUI finals expose unevaluated tool-call text.

### OP-0030 — FAIL

Repeat 1 passes artifact validation. `source-copy-1_STATUS_NEW.docx` exists (`9193651c4ce38e4d160cf62199c90717fe2df513ba79f0fc7ea2c8a41d461993`), is a valid 17-entry DOCX ZIP, has no duplicate members or XML parse errors, contains exactly one `STATUS_NEW` and no `STATUS_OLD`, and differs from the source only in `word/document.xml`. Replacing the source XML's sole `STATUS_OLD` with `STATUS_NEW` yields the output XML exactly. Repeats 2 and 3 produced no output copies.

### OP-0031 — FAIL

Both source hashes are unchanged and both outputs are readable DOCX ZIPs with required package members, no duplicate members, and no XML parse errors:

| Repeat | Output | SHA-256 | Semantic result |
|---|---|---|---|
| 1 | `convert-1.docx` | `336399edcdbe051ab75cb0815ef5448228cc4f65a8eea92440369e37422dc4cf` | FAIL: source heading is a literal `# Conversion Fixture` paragraph; Word heading style is absent; table count is 0 |
| 2 | `d84608bbcc89_convert-2.md.docx` | `f9ffdece3db3d376aeb23ffac3bac80589e196d21ce6e531f43b8d6c1f9908f0` | FAIL: same; Markdown table rows remain literal text |

### OP-0034 — FAIL

Repeat 1 passes the collision check: `report.md` (`ee8a9c72c4f61c3bb637d903a32e319989023436b7098df6dca8fb13c8e2d6c0`) contains only marker `OP-0034-A-1`, while `report.20260713170348.md` (`44ff884ac651cca40e49aa223c18e29cbf55db2c5921a5f509414f8a709c39c3`) contains only marker `OP-0034-B-1`. Paths and hashes are distinct, with no cross-session marker mix or overwrite. Repeats 2 and 3 produced none of the four required artifacts, so the operation fails overall.

## DOCX visual QA limitation

The mandated `render_docx.py` workflow was attempted for all three generated DOCX files with the bundled Python runtime. Each attempt stopped before rendering with `FileNotFoundError: [WinError 2]` because `soffice`/LibreOffice is absent; no PNG was produced. ZIP/XML and semantic document-tree checks above completed successfully, but visual layout is therefore not independently certified.

The validator did not modify any isolated source or output artifact.
