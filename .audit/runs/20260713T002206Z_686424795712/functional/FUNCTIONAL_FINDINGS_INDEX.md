# Functional findings index

| Finding | Priority | Category | Title |
|---|---|---|---|
| [FUNC-FIND-001](findings/FUNC-FIND-001.md) | P2 | RESULT_NOT_USEFUL | Direct DNS question is misrouted to shopping |
| [FUNC-FIND-002](findings/FUNC-FIND-002.md) | P2 | FORMAT_BREACH | Exact response constraints are not consistently enforced |
| [FUNC-FIND-003](findings/FUNC-FIND-003.md) | P1 | CLAIMED_ARTIFACT_MISSING | Artifact generation ignores exact paths or returns incomplete transforms |
| [FUNC-FIND-004](findings/FUNC-FIND-004.md) | P2 | CONTEXT_LOSS | Multi-turn references to prior options/files are lost |
| [FUNC-FIND-005](findings/FUNC-FIND-005.md) | P2 | UNNECESSARY_CLARIFICATION | Ambiguous request creates a mission instead of one precise question |
| [FUNC-FIND-006](findings/FUNC-FIND-006.md) | P1 | INTERNAL_OUTPUT_LEAK | Raw tool-call envelopes reach rendered assistant output |
| [FUNC-FIND-007](findings/FUNC-FIND-007.md) | P1 | RESULT_NOT_USEFUL | Uploaded document recall is unreliable and blocks missions |
| [FUNC-FIND-008](findings/FUNC-FIND-008.md) | P2 | ERROR_NOT_ACTIONABLE | Corrupt document recovery is inconsistent |
| [FUNC-FIND-009](findings/FUNC-FIND-009.md) | P1 | TOOL_STATE_MISMATCH | Approved safe action uses a non-canonical tool schema |
| [FUNC-FIND-010](findings/FUNC-FIND-010.md) | P2 | RESULT_NOT_USEFUL | Web synthesis does not reliably return a cited usable result |
| [FUNC-FIND-011](findings/FUNC-FIND-011.md) | P1 | STATE_RECOVERY_FAILURE | Interrupted GUI stream can leave an empty stale assistant bubble |
| [FUNC-FIND-012](findings/FUNC-FIND-012.md) | P2 | STATE_RECOVERY_FAILURE | Requested memory namespace is ignored |
| [FUNC-FIND-013](findings/FUNC-FIND-013.md) | P1 | PROFILE_MISMATCH | Both 31B profiles are functionally unusable on the certified machine |
| [FUNC-FIND-014](findings/FUNC-FIND-014.md) | P2 | STARTUP_FAILURE | Repeated start is not idempotent |
| [FUNC-FIND-015](findings/FUNC-FIND-015.md) | P1 | CROSS_SESSION_MIX | GUI transcript survives a runtime-home identity change |
| [FUNC-FIND-016](findings/FUNC-FIND-016.md) | P1 | FALSE_SUCCESS | Doctor returns success when a required test fails |
| [FUNC-FIND-017](findings/FUNC-FIND-017.md) | P1 | INTERNAL_OUTPUT_LEAK | Doctor output exposes the runtime API token |

All accepted FAIL rows in `RESULTS.csv` and `OPERATOR_ACCEPTANCE_RESULTS.csv` map to one or more findings.
