# REMEDIATION SUMMARY — RB-4 transform exact destination

| Field | Value |
|-------|-------|
| RUN_ID | `20260713T002206Z_686424795712` |
| Blocker | **RB-4** — `TRANSFORM_EXISTING_DOCUMENT` ignored requested destination and declared success for another file |
| Base candidate | `38f606fcef9e7947c14b62a9a815da6445f4196e` |
| Fix branch | `fix/final-transform-path/20260713T002206Z_686424795712` |
| Worktree | `D:\jarvis-gpt-worktrees\final-transform-fix-20260713T002206Z_686424795712` |
| Production commit | `18f15c0dcb96d096a0e484d0d5a22f78e424c6ff` |
| Status | **FINAL_TRANSFORM_PATH_CANDIDATE_FOR_REVIEW** |

## Root cause

Claude re-review (FINAL_LAST_REVIEW_BLOCKED) proved that complete transform
requests still:

1. Bound the **source** filename (or a format label) as the destination.
2. Invented subdirectories from the word `markdown` (`markdown/`, `markdown-/`).
3. Wrote a stray file named `document-outputs`.
4. Declared **Артефакт создан** for a path the operator never requested, because
   post-write verification only checked that *some* returned path existed.

Source integrity was already intact; the failure was destination binding and
success gating, not source mutation.

## Fix (typed, phrase-agnostic)

### Intent contract (`agent.py`)

- Keep typed intents:
  - `EXISTING_DOCUMENT_REFERENCE`
  - `NEW_ARTIFACT_REQUEST`
  - `TRANSFORM_EXISTING_DOCUMENT`
- Expand transform/create verbs (`transform`, `convert`, `преобразуй`,
  `сделай markdown`, …) without phrase-specific hardcoding of full prompts.
- **Separate** source and destination extractors:
  - `_source_filename_from_message`
  - `_destination_filename_from_message`
- Directory extraction never treats format labels (`markdown`, `md`, `docx`, …)
  or bare `document-outputs` as subdirectories.
- Complete transform intent carries:
  - exact source identity fields
  - `requested_destination` / `output_name` / `filename` (destination only)
  - `output_format`
  - `transformation_instruction`
  - `collision_policy` / `overwrite` (default fail closed)
  - `allowed_root` = `document-outputs`
  - `allow_in_place=False` (copy-on-write default)

### Direct route (`_try_direct_new_artifact_action`)

- `TRANSFORM_EXISTING_DOCUMENT` resolves source identity **before** the tool call
  (file hits → storage list by name).
- Calls `documents.convert` with absolute bound destination under the allowed
  root, `require_exact_path=True`, and explicit `source_identity`.
- Source == destination without explicit in-place request → fail closed.
- Final operator answer uses **only** `_verified_artifact_answer` (no invented path).

### Tool contract (`tools.py` — `documents.convert` / `documents.generate`)

Minimum transform result fields:

- `requested_destination`
- `actual_path`
- `source_identity`
- `source_hash_before` / `source_hash_after`
- `output_hash` / `output_size`
- `validation_result` / `path_verification`

Exact success requires:

- `actual_path ==` bound requested destination
- path under `document-outputs`
- regular non-empty file
- not the source path
- source hash unchanged
- no timestamp fallback
- no invented format subdirectory

Named destinations default to exact binding; collision without `overwrite`
fails closed and leaves the existing file unchanged.

### Verification (`_verified_artifact_answer`)

- Path may come only from tool result (`actual_path` / verified output).
- Rejects basename mismatch, full-path mismatch, outside allowed root, source
  path as output, timestamp fallback names, and format-label subdirectories.
- Success answer embeds only the verified path.

## Non-goals / preserved behaviour

- `EXISTING_DOCUMENT_REFERENCE` → recall/search (unchanged).
- `NEW_ARTIFACT_REQUEST` → direct `documents.generate` exact-path route (RB-3).
- No doctor/frontend/CI/lockfile changes.
- No production user documents; temporary allowed root only.
- No push/merge/attestation/READY.

## Live acceptance (isolated temporary root)

Runtime home:
`D:\jarvis\audit-backups\20260713T002206Z_686424795712\final-transform-fix\runtime-home`

| Gate | Result |
|------|--------|
| Transform exact destination | **6/6 PASS** |
| Negative mismatched tool path | **3/3 PASS** (no success claim) |
| Existing-document recall | **3/3 PASS** |
| Direct new artifact exact path | **3/3 PASS** |
| Ambiguous clarification | **3/3 PASS** (no side effects) |
| Follow-up artifact | **3/3 PASS** |
| Source hash unchanged | **True** |

Evidence: `...\final-transform-fix\live-results\live-acceptance.json`
