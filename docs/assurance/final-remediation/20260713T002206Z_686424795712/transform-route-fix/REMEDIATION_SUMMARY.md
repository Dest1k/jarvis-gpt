# REMEDIATION SUMMARY — RB-5 deterministic transform routing

| Field | Value |
|-------|-------|
| RUN_ID | `20260713T002206Z_686424795712` |
| Blocker | **RB-5** — fully specified `TRANSFORM_EXISTING_DOCUMENT` non-deterministically missed the accepted transform route |
| Base candidate | `41c22de07c095c6a595589a57614cfbf55d33e48` |
| Fix branch | `fix/final-transform-route/20260713T002206Z_686424795712` |
| Worktree | `D:\jarvis-gpt-worktrees\final-transform-route-fix-20260713T002206Z_686424795712` |
| Status | **FINAL_TRANSFORM_ROUTE_CANDIDATE_FOR_REVIEW** |

## Root causes (exact base attribution)

On exact base, the Claude-style fully specified Russian request:

```text
на основе загруженного документа src-doc.txt подготовь markdown-файл
с именем <exact>.md в каталоге document-outputs
```

was classified **12/12** as `EXISTING_DOCUMENT_REFERENCE` (not transform):

1. Create-verb phrase lists required `подготовь файл` / `convert`, but the
   operator used `подготовь markdown-файл` (no exact phrase hit).
2. Recall heuristic matched `загруженн` and short-circuited durable-write
   detection (`filename + document-outputs`).
3. Task kernel fell into `reasoning` / `local_admin_advice`.
4. Generic LLM arbiter was consulted for that bucket and could rewrite to
   **mission**.
5. Free agentic tool-loop could emit model-generated tool JSON →
   **internal-payload guard** (correct for untrusted model text; wrong that
   a trusted convert path was never taken).

Evidence: `audit-backups/.../final-transform-route-fix/attribution/`.

RB-4 was **not** reopened: when convert ran, exact destination / source hash /
no false success already held. RB-5 is routing non-determinism only.

## Fix (structural, not phrase-specific)

### Complete transform contract

`TRANSFORM_EXISTING_DOCUMENT` is fully specified when **all** of these are
available from the operator message (structural operands):

- resolvable source reference (filename / uploaded / based-on)
- exact destination filename (≠ source unless explicit in-place)
- supported output format (explicit token or destination extension)
- allowed root / concrete destination (`document-outputs` or equivalent)
- transformation instruction (synthesized from source→format→destination)

Implemented as `_is_fully_specified_transform` and used by classification,
side-effect admission, and planning.

### Deterministic pre-routing path

1. Typed `TransformExistingDocumentIntent` via `_new_artifact_intent_from_message`.
2. `_try_direct_new_artifact_action` → trusted `documents.convert` with
   bound destination / `require_exact_path` / source identity (RB-4).
3. Generic intent arbiter **cannot** reclassify sealed
   `transform_document` / complete transform messages.
4. Mission planner is not applied to single-step complete transforms.
5. Tool arguments are built by typed runtime code, never free model JSON.
6. Internal-payload guard still blocks untrusted model envelopes; trusted
   DirectAction answers do not pass through it.
7. Incomplete transform → one clarification, zero side effects.
8. Tool failure fails closed — no mission/search fallback.
9. Final operator answer still comes only from `_verified_artifact_answer` (RB-4).

### Incomplete-transform gap fix

Source extensions (e.g. `src-doc.txt`) no longer satisfy destination/format
completeness for transform-shaped requests without an explicit non-source
destination.

## Non-goals / preserved

- RB-4 exact destination verification unchanged
- `EXISTING_DOCUMENT_REFERENCE` → recall
- `NEW_ARTIFACT_REQUEST` → direct generate
- No doctor / frontend / CI / lockfile changes
- No production user state
- No push / merge / attestation / READY

## Live acceptance (isolated temporary root)

Runtime home:
`D:\jarvis\audit-backups\20260713T002206Z_686424795712\final-transform-route-fix\runtime-home`

| Gate | Result |
|------|--------|
| Exact fully specified transform | **12/12 PASS** |
| Original Claude scenario | **6/6 PASS** |
| Incomplete transform clarification | **6/6 PASS** |
| Clarification follow-up artifact | **3/3 PASS** |
| Existing recall (no new artifacts) | **3/3 PASS** |
| Direct new artifact | **3/3 PASS** |
| Synthetic internal envelope blocked | **3/3 PASS** |
| Source hash unchanged | **True** |
| Zero mission / zero payload-guard on sealed transforms | **True** |

## Post-fix attribution

Same Claude prompt × 12 after fix: **12/12**
`TRANSFORM_EXISTING_DOCUMENT`, exact artifact, zero arbiter, zero mission.
