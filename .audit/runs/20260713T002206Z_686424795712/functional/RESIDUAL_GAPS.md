# Residual gaps

| Gap | Status | Reason |
|---|---|---|
| Scoped external internet removal with loopback preserved | BLOCKED_BY_ENV | No reversible per-process/network namespace was available without unsafe global settings. |
| Real DPI/zoom and viewport resize matrix | BLOCKED_BY_ENV | The in-app browser exposed no viewport API and keyboard zoom dispatch could not target a stable body locator. |
| Visual DOCX rendering | BLOCKED_BY_ENV | LibreOffice/soffice was absent; structural ZIP/XML checks passed. |
| Separate malformed user config | BLOCKED_BY_SPEC | Active profiles are built-in and the launcher exposes no separate user config input. |
| Unowned compatible dispatcher reuse | DEFERRED_REVIEW | Avoided mutating or impersonating unrelated processes. |
| Pending request/mission restart and idempotency | DEFERRED_REVIEW | Copy backup/lock/recovery passed, but a successful model-backed mission could not be established. |
| Mono document fixture names | INCONCLUSIVE | OP-0062/0063 requested `mono-*` names while generated profile fixtures used `mono-perf-*`; independent reviews disagreed on environment vs specification classification. |
| Full two-session WebSocket reconnect interleave | DEFERRED_REVIEW | Origin/event smoke passed; deterministic dual-session transport fixture remains. |

No gap was converted into a Spark task unless the campaign reproduced a product defect.
