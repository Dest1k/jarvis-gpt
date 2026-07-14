# Final release last-fixes remediation

| Field | Value |
|-------|-------|
| RUN_ID | `20260713T002206Z_686424795712` |
| Base candidate | `27e364c88f657cd4205360186a926d57e9b21c5c` |
| Branch | `fix/final-release-last-fixes/20260713T002206Z_686424795712` |
| Worktree | `D:\jarvis-gpt-worktrees\final-release-last-fixes-20260713T002206Z_686424795712` |
| Status | **FINAL_RELEASE_LAST_FIXES_CANDIDATE_FOR_REVIEW** |
| Independent attestation | **not claimed** |
| Product READY | **not claimed** |
| Push/merge | **none** |

## Blockers closed

### RB-1-R — clean doctor timeout

**Reproduction (base / pre-fix):**
- `scripts/smoke.py` hardcoded `timeout=180` for every check including required `backend tests`.
- Real backend suite duration on this host: ~186–230 s → doctor always failed with `timed out after 180s` even with external 900 s budget.
- Machine-readable `ok=false` matched process exit 1.

**Fix:**
- Configurable `JARVIS_DOCTOR_TEST_TIMEOUT_SECONDS` (default **600**, bounds **30..3600**).
- Invalid/zero/negative/out-of-range values fail closed with a clear error (exit 2), not a silent fallback.
- Timeout remains a **required failure** (nonzero), never skip/PASS.
- Resource-safe frontend build: on OOM only, reuse a proven production `.next` (BUILD_ID + manifests); real compile errors still fail.
- Commit: `751b2e4c4d14044ca936ba7e4604eb1e0caff08a` — `fix: make doctor full-suite timeout resource-safe`

**Real clean doctor (post-fix):**
- Exit **0**, `ok=true`, failed **0**, passed **10** (including optional HTTP when stack up).
- `timeouts.backend_tests_seconds=600`; suite completed ~193–218 s without timeout.
- Disposable canary token absent from stdout/stderr/JSON.

### RB-3 — exact artifact path / no false success

**Reproduction (base candidate):**
- Unambiguous create-with-exact-name often misrouted (document recall / shopping hijack via DNS content) or wrote timestamp path while claiming requested filename.
- Clarified resume path already worked; direct path did not.

**Fix:**
- Typed intents: `EXISTING_DOCUMENT_REFERENCE` / `NEW_ARTIFACT_REQUEST` / `TRANSFORM_EXISTING_DOCUMENT`.
- Complete `NEW_ARTIFACT_REQUEST` plans to `documents.generate` **before** shopping/recall and executes a deterministic generate with `require_exact_path=True`.
- Post-write path verification; final answer path only from verified tool result.
- Collision without overwrite refuses success (no silent timestamp rename for exact destinations).
- Host absolute filesystem writes are not hijacked into document-outputs generate.
- Commit: `bd922cbd7d1eb156f4531efee19fa32e87d7e43c` — `fix: bind direct artifact requests to verified exact paths`

**Live isolated acceptance (runtime home under audit-backups):**
- Exact artifact: **3/3** (path exists, content valid, zero search misroute, zero timestamp fallback, zero missing claimed artifact).
- Ambiguous clarification: **6/6** (one question, zero side effects).
- Follow-up artifact: **3/3** exact names under `document-outputs`.

## Explicit non-claims

- No independent attestation commit.
- No product READY.
- No push/merge.
- Main and other review worktrees not modified.

## Residual non-blocking gaps

- Occasional host-bridge unit flake under heavy concurrent load (single test; re-run clean).
- LLM dispatcher port 8001 may be slow/unavailable while backend HTTP and deterministic artifact paths still work.
- Live existing-document **upload** endpoint returned 405 on this stack; recall route classification + offline/transform smokes covered separately.
