# Static test results

- `TEST-STATIC-001` — **PASS_HERMETIC** — Python compileall; evidence `EVID-STATIC-001`.
- `TEST-STATIC-002` — **PASS_HERMETIC** — Ruff repository lint; evidence `EVID-STATIC-002`.
- `TEST-STATIC-003` — **BLOCKED_BY_ENV** — Broad pytest excluding prohibited host-bridge modules; evidence `EVID-STATIC-003`.
- `TEST-STATIC-004` — **PASS_HERMETIC** — Safe backend pytest subset; evidence `EVID-STATIC-004`.
- `TEST-STATIC-005` — **BLOCKED_BY_ENV** — Frontend npm setup with unwritable default cache; evidence `EVID-STATIC-005`.
- `TEST-STATIC-006` — **PASS_HERMETIC** — Pinned frontend npm ci in disposable copy; evidence `EVID-STATIC-006`.
- `TEST-STATIC-007` — **PASS_HERMETIC** — Frontend TypeScript typecheck; evidence `EVID-STATIC-007`.
- `TEST-STATIC-008` — **PASS_HERMETIC** — Frontend production build; evidence `EVID-STATIC-008`.
- `TEST-STATIC-009` — **PASS_HERMETIC** — Tracked file inventory; evidence `EVID-STATIC-009`.
- `TEST-STATIC-010` — **INCONCLUSIVE** — Initial static contract harness version; evidence `EVID-STATIC-010`.
- `TEST-STATIC-011` — **PASS_HERMETIC** — Deterministic CLI help; evidence `EVID-STATIC-011`.
- `TEST-STATIC-012` — **PASS_HERMETIC** — Deterministic CLI profile catalog; evidence `EVID-STATIC-012`.
- `TEST-STATIC-013` — **PASS_HERMETIC** — JSON/TOML/YAML/OpenAPI/profile contract; evidence `EVID-STATIC-013`.
- `TEST-STATIC-014` — **PASS_HERMETIC** — Safe backend subset under coverage; evidence `EVID-STATIC-014`.
- `TEST-STATIC-015` — **PASS_HERMETIC** — Coverage JSON generation; evidence `EVID-STATIC-015`.
- `TEST-STATIC-016` — **PASS_HERMETIC** — Coverage report; evidence `EVID-STATIC-016`.
- `TEST-STATIC-017` — **PASS_HERMETIC** — Public feature/requirement extraction; evidence `EVID-STATIC-017`.
- `TEST-STATIC-018` — **FAIL_HERMETIC** — Document output collision oracle; evidence `EVID-STATIC-018`.
- `TEST-STATIC-019` — **FAIL_HERMETIC** — Archive atomic failure oracle; evidence `EVID-STATIC-019`.
- `TEST-STATIC-020` — **PASS_HERMETIC** — Full pytest collection; evidence `EVID-STATIC-020`.
- `TEST-STATIC-021` — **PASS_HERMETIC** — Static contracts plus credential signature scan; evidence `EVID-STATIC-021`.
- `TEST-STATIC-022` — **BLOCKED_BY_ENV** — PowerShell availability; evidence `EVID-STATIC-022`.
- `TEST-STATIC-023` — **BLOCKED_BY_ENV** — Docker/Compose availability; evidence `EVID-STATIC-023`.
- `TEST-STATIC-024` — **PASS_HERMETIC** — Initial audit artifact rendering; evidence `EVID-STATIC-024`.
- `TEST-STATIC-025` — **PASS_HERMETIC** — Final traceability artifact rendering; evidence `EVID-STATIC-025`.
- `TEST-STATIC-026` — **PASS_HERMETIC** — Traceability and source-isolation consistency gate; evidence `EVID-STATIC-026`.
- `TEST-STATIC-027` — **PASS_HERMETIC** — Final evidence manifest snapshot; evidence `EVID-STATIC-027`.

Status counts: {'PASS_HERMETIC': 20, 'BLOCKED_BY_ENV': 4, 'INCONCLUSIVE': 1, 'FAIL_HERMETIC': 2}. TEST-STATIC-010 was a harness-selection error corrected by TEST-STATIC-013/021. TEST-STATIC-005 was an unwritable default npm cache corrected in a disposable copy by TEST-STATIC-006; neither is a product defect. EVID-STATIC-027 is intentionally self-excluded from the manifest snapshot it created.
