# Static findings index

| ID | Severity | Priority | Status | Title |
|---|---|---|---|---|
| [JARVIS-0001](findings/JARVIS-0001.md) | high | P1 | static-confirmed | Model activation accepts unverified directories and has no rollback |
| [JARVIS-0002](findings/JARVIS-0002.md) | high | P1 | static-confirmed | Launcher stop fails open when ownership state is missing or corrupt |
| [JARVIS-0003](findings/JARVIS-0003.md) | high | P1 | probable-runtime | Launcher process cleanup signature can match unrelated processes |
| [JARVIS-0004](findings/JARVIS-0004.md) | high | P1 | static-confirmed | Compose frontend waits for a backend healthcheck that does not exist |
| [JARVIS-0005](findings/JARVIS-0005.md) | high | P1 | static-confirmed | Bundled browser does not enforce public-only validation on every navigation hop |
| [JARVIS-0006](findings/JARVIS-0006.md) | high | P1 | static-confirmed | Browser worker inherits secrets/runtime access while Chromium sandbox is disabled |
| [JARVIS-0007](findings/JARVIS-0007.md) | high | P1 | static-confirmed | JarvisStorage operations can leave a poisoned transaction after exceptions |
| [JARVIS-0008](findings/JARVIS-0008.md) | high | P1 | static-confirmed | Autonomy job JSON-array RMW loses concurrency and detached start reports false success |
| [JARVIS-0009](findings/JARVIS-0009.md) | high | P1 | static-confirmed | Approval state/audit are non-atomic and raw payloads can retain credentials |
| [JARVIS-0010](findings/JARVIS-0010.md) | high | P1 | probable-runtime | Transport retries can repeat actions after replay retention or new chat authorization |
| [JARVIS-0011](findings/JARVIS-0011.md) | high | P1 | static-confirmed | Directory ingest can follow symlinks outside allowed roots and index sensitive files |
| [JARVIS-0012](findings/JARVIS-0012.md) | high | P1 | static-confirmed | Failed archive extraction leaves partial final outputs |
| [JARVIS-0013](findings/JARVIS-0013.md) | high | P1 | static-confirmed | User regex can block the async event loop beyond cancellation budgets |
| [JARVIS-0014](findings/JARVIS-0014.md) | medium | P2 | spec-gap | Command Center advertises a live WebSocket feed whose transport is disabled |
| [JARVIS-0015](findings/JARVIS-0015.md) | medium | P2 | probable-runtime | Frontend retains stale online/ready state after polling failures |
| [JARVIS-0016](findings/JARVIS-0016.md) | medium | P2 | probable-runtime | Frontend stream accepts EOF without terminal state and cannot cancel requests |
| [JARVIS-0017](findings/JARVIS-0017.md) | medium | P2 | static-confirmed | Generated document collision logic reuses an existing timestamped path |
| [JARVIS-0018](findings/JARVIS-0018.md) | medium | P2 | static-confirmed | Documented Compose quick start has no API token bootstrap |
| [JARVIS-0019](findings/JARVIS-0019.md) | high | P1 | spec-gap | Mutating document/watch tools default to safe and bypass approval |
| [JARVIS-0020](findings/JARVIS-0020.md) | medium | P2 | static-confirmed | Dependency/build/offline contract is not reproducible from immutable inputs |
| [JARVIS-0021](findings/JARVIS-0021.md) | high | P1 | static-confirmed | Main storage lacks versioned migration/integrity/retention and policy corruption fails open |
| [JARVIS-0022](findings/JARVIS-0022.md) | medium | P2 | static-confirmed | Web-watch persists digest before durable notification delivery |
| [JARVIS-0023](findings/JARVIS-0023.md) | medium | P2 | static-confirmed | Unauthenticated health response exposes absolute runtime path |
| [JARVIS-0024](findings/JARVIS-0024.md) | medium | P2 | spec-gap | Bundled web synthesis/TLS trust policy is weaker than the core path |
| [JARVIS-0025](findings/JARVIS-0025.md) | low | P3 | test-gap | Frontend accessibility and test harness leave interaction regressions unchecked |

Counts by severity: {'high': 15, 'medium': 9, 'low': 1}. Counts by status: {'static-confirmed': 17, 'probable-runtime': 4, 'spec-gap': 3, 'test-gap': 1}.
