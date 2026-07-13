# Document and tool report

- 28 initial document fixtures and 14 profile fixtures were generated and hash-manifested; all 42 uploads returned HTTP 200.
- Three valid PDFs rendered cleanly and three corrupt PDFs were rejected as expected. LibreOffice was unavailable, so visual DOCX rendering remained BLOCKED_BY_ENV.
- Turbo document recall, exact paths, transformations, and concurrent output were inconsistent; deterministic artifact validation failed.
- Approved safe directory creation failed because a non-canonical tool name was bound to the approval.
- Copied database backup/integrity/lock/read-only/temp probes passed without touching production state.
