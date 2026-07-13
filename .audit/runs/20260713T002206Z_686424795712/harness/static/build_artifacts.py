#!/usr/bin/env python3
"""Render the PHASE A report set from reviewed, evidence-backed audit records."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path


RUN_ID = "20260713T002206Z_686424795712"
SOURCE_COMMIT = "686424795712cb0a562750b6dade13de18c48792"
SOURCE_BRANCH = "main"
AUDIT_BRANCH = f"audit/phase-a-{RUN_ID}"
RUN = Path(__file__).resolve().parents[2]
REPO = RUN.parents[2]
EVIDENCE = RUN / "evidence" / "static"
MACHINE = RUN / "machine"


FINDINGS = [
    dict(id="JARVIS-0001", title="Model activation accepts unverified directories and has no rollback", kind="reliability", severity="high", priority="P1", status="static-confirmed", confidence=.99, repro="static-proof", reqs=["REQ-PROFILE-001", "REQ-IDEMPOTENCY-001"], paths=["backend/src/jarvis_gpt/model_hub.py:272-300", "backend/src/jarvis_gpt/model_catalog.py:138-144", "backend/src/jarvis_gpt/api.py:869-890", "frontend/app/page.tsx:3174-3214"], evidence=["EVID-STATIC-004", "EVID-STATIC-007"], scenario="SCN-LIVE-011", summary="An empty custom model directory is activatable; the UI persists the override, stops the working dispatcher and starts the replacement without checking either result or restoring the prior runtime.", impact="A failed switch can leave a durable invalid override and stop the previously working model while API/UI transport still looks successful.", root="Activation validates identity/name rather than artifacts and is implemented as several non-transactional state changes.", remediation="Introduce a manifest/shard/architecture compatibility gate and a staged switch with health confirmation and rollback."),
    dict(id="JARVIS-0002", title="Launcher stop fails open when ownership state is missing or corrupt", kind="reliability", severity="high", priority="P1", status="static-confirmed", confidence=.98, repro="static-proof", reqs=["REQ-LAUNCHER-001"], paths=["scripts/jarvis-launcher.ps1:1245-1288", "scripts/jarvis-launcher.ps1:1680-1684"], evidence=["EVID-STATIC-009"], scenario="SCN-LIVE-003", summary="Missing/corrupt launcher-state.json is interpreted as ownership of the dispatcher instead of unknown ownership.", impact="A stop invocation can terminate a dispatcher not started by this launcher run.", root="Ownership predicate defaults to true on absent evidence and state is written non-atomically.", remediation="Fail closed on absent/corrupt state; persist state atomically with runtime identity and verify it before stop."),
    dict(id="JARVIS-0003", title="Launcher process cleanup signature can match unrelated processes", kind="reliability", severity="high", priority="P1", status="probable-runtime", confidence=.91, repro="runtime-required", reqs=["REQ-LAUNCHER-001"], paths=["scripts/jarvis-launcher.ps1:894-910", "scripts/jarvis-launcher.ps1:1066-1083", "scripts/jarvis-launcher.ps1:1674"], evidence=["EVID-STATIC-009"], scenario="SCN-LIVE-004", summary="Any process whose executable or command line contains the repo/frontend path is treated as a Jarvis process and terminated.", impact="Editors, terminals or diagnostic commands mentioning the repo path may be killed.", root="Cleanup uses substring ownership inference instead of recorded PID plus birth identity.", remediation="Track exact child identities and terminate only verified descendants owned by the active launcher state."),
    dict(id="JARVIS-0004", title="Compose frontend waits for a backend healthcheck that does not exist", kind="defect", severity="high", priority="P1", status="static-confirmed", confidence=.99, repro="static-proof", reqs=["REQ-LAUNCHER-001", "REQ-OFFLINE-001"], paths=["docker-compose.yml:124-145", "docker-compose.yml:43-123"], evidence=["EVID-STATIC-013", "EVID-STATIC-021"], scenario="SCN-LIVE-007", summary="frontend depends_on backend with condition service_healthy, but backend defines no healthcheck.", impact="Compose startup can reject the dependency or leave frontend blocked, depending on Compose implementation/version.", root="Dependency condition and service contract drifted independently.", remediation="Add a pinned backend healthcheck or use a condition whose semantics match the service definition, then test rendered Compose in CI."),
    dict(id="JARVIS-0005", title="Bundled browser does not enforce public-only validation on every navigation hop", kind="defensive-design", severity="high", priority="P1", status="static-confirmed", confidence=.98, repro="static-proof", reqs=["REQ-URL-001", "REQ-TRUST-001"], paths=["backend/src/jarvis_gpt/web_surfer.py:522-569", "backend/src/jarvis_gpt/web_surfer.py:922-965", "backend/src/jarvis_gpt/web_surfer.py:1873-1894", "backend/src/jarvis_gpt/web_surfer_adapter.py:249-308"], evidence=["EVID-STATIC-004", "EVID-STATIC-014"], scenario="SCN-LIVE-023", summary="Deep research and shopping accept result strings by HTTP prefix and navigate without the core transport's DNS pinning and per-redirect validation; subresources are not uniformly guarded.", impact="Untrusted public content may cause browser requests to private/link-local destinations.", root="The hardened core HTTP path and the Playwright path implement different destination policies.", remediation="Apply one public-only resolver/redirect/subresource policy to every browser request and navigation."),
    dict(id="JARVIS-0006", title="Browser worker inherits secrets/runtime access while Chromium sandbox is disabled", kind="defensive-design", severity="high", priority="P1", status="static-confirmed", confidence=.97, repro="static-proof", reqs=["REQ-TRUST-001", "REQ-SECRET-001"], paths=["backend/src/jarvis_gpt/web_surfer_adapter.py:846-889", "backend/src/jarvis_gpt/web_surfer.py:462-476", "docker-compose.yml:82-122", "backend/tests/test_deployment_contracts.py:21-39"], evidence=["EVID-STATIC-004", "EVID-STATIC-014"], scenario="SCN-LIVE-024", summary="The worker receives the full environment (including API/search keys), retains writable /runtime access and launches Chromium with --no-sandbox.", impact="A browser compromise has a materially wider secret/data blast radius than documented containment implies.", root="Process-tree containment was implemented without an environment/filesystem capability boundary; deployment tests inspect the Dockerfile, not actual launch args.", remediation="Allowlist worker env, isolate writable roots, enable/verify browser sandboxing and test effective runtime privileges."),
    dict(id="JARVIS-0007", title="JarvisStorage operations can leave a poisoned transaction after exceptions", kind="data-integrity", severity="high", priority="P1", status="static-confirmed", confidence=.99, repro="static-proof", reqs=["REQ-STORAGE-001"], paths=["backend/src/jarvis_gpt/storage.py:940-998", "backend/src/jarvis_gpt/storage.py:1136-1194", "backend/src/jarvis_gpt/storage.py:1857-1932"], evidence=["EVID-STATIC-004", "EVID-STATIC-014"], scenario="SCN-LIVE-018", summary="Several DB+filesystem methods commit only after MemoryVault/file work and do not rollback when that later work throws.", impact="A failed call can be committed by a later unrelated operation; DB and vault may diverge.", root="One long-lived connection is managed by ad-hoc commits rather than an exception-safe unit-of-work boundary.", remediation="Wrap each logical mutation in explicit transaction/rollback and define ordering or an outbox for filesystem mirrors."),
    dict(id="JARVIS-0008", title="Autonomy job JSON-array RMW loses concurrency and detached start reports false success", kind="reliability", severity="high", priority="P1", status="static-confirmed", confidence=.99, repro="static-proof", reqs=["REQ-JOBS-001", "REQ-IDEMPOTENCY-001"], paths=["backend/src/jarvis_gpt/operations.py:238-393", "backend/src/jarvis_gpt/api.py:1083-1094", "backend/src/jarvis_gpt/autonomy_executor.py:73-109"], evidence=["EVID-STATIC-004", "EVID-STATIC-014"], scenario="SCN-LIVE-019", summary="Jobs/history are whole JSON arrays updated without CAS; /start emits started before admission, while paused/done/cancelled/already-running jobs are rejected silently by the executor.", impact="Concurrent updates can be lost and observers can see a started job that never ran or produced a terminal event.", root="Durable job state is not row/lease based and transport success is emitted before authoritative admission.", remediation="Use transactional row/CAS state transitions and return/publish the result of a single admission decision."),
    dict(id="JARVIS-0009", title="Approval state/audit are non-atomic and raw payloads can retain credentials", kind="data-integrity", severity="high", priority="P1", status="static-confirmed", confidence=.99, repro="static-proof", reqs=["REQ-APPROVAL-001", "REQ-SECRET-001", "REQ-STORAGE-001"], paths=["backend/src/jarvis_gpt/storage.py:1722-1765", "backend/src/jarvis_gpt/storage.py:2240-2292", "backend/src/jarvis_gpt/storage.py:2403-2435", "backend/src/jarvis_gpt/storage.py:2558-2589"], evidence=["EVID-STATIC-004", "EVID-STATIC-014"], scenario="SCN-LIVE-017", summary="Approval transitions commit separately from audit writes, and creation persists exact unredacted tool arguments in both approval and audit rows.", impact="Audit failure can strand/ambiguously report approval state; nested environment/argument credentials can persist and be returned by API.", root="Approval persistence lacks a transactional outbox and redaction is applied to terminal results but not creation/audit payloads.", remediation="Redact before persistence and commit transition plus durable event/outbox atomically."),
    dict(id="JARVIS-0010", title="Transport retries can repeat actions after replay retention or new chat authorization", kind="data-integrity", severity="high", priority="P1", status="probable-runtime", confidence=.97, repro="runtime-required", reqs=["REQ-IDEMPOTENCY-001", "REQ-STREAM-001"], paths=["backend/src/jarvis_gpt/models.py:23-30", "backend/src/jarvis_gpt/agent.py:679-941", "backend/src/jarvis_gpt/execution_replay.py:88-100", "backend/src/jarvis_gpt/execution_kernel.py:501-542", "backend/src/jarvis_gpt/tools.py:9598-9659"], evidence=["EVID-STATIC-004", "EVID-STATIC-014"], scenario="SCN-LIVE-016", summary="Chat has no request idempotency or per-conversation serialization; old durable execution keys are evicted with no tombstone.", impact="A lost response/retry can duplicate append/process/GUI effects; concurrent turns can interleave; a sufficiently old execution key can be reapplied.", root="Idempotency is scoped to short-lived authorization/retained results rather than a caller key with an explicit durable expiry contract.", remediation="Require request IDs, serialize conversation turns and retain non-replay tombstones beyond result-detail retention."),
    dict(id="JARVIS-0011", title="Directory ingest can follow symlinks outside allowed roots and index sensitive files", kind="defensive-design", severity="high", priority="P1", status="static-confirmed", confidence=.98, repro="static-proof", reqs=["REQ-FILES-001", "REQ-SECRET-001", "REQ-TRUST-001"], paths=["backend/src/jarvis_gpt/ingest.py:26-44", "backend/src/jarvis_gpt/ingest.py:81-134", "backend/src/jarvis_gpt/ingest.py:240-267", "backend/src/jarvis_gpt/ingest.py:370-402"], evidence=["EVID-STATIC-004", "EVID-STATIC-014"], scenario="SCN-LIVE-025", summary="Recursive ingest includes .env/config/log files and resolves file symlinks without rechecking containment after resolution.", impact="A file under an allowed directory may copy/index outside-root data; sensitive content is stored without redaction.", root="Directory enumeration and per-file canonicalization use different trust-boundary checks.", remediation="Revalidate resolved targets, reject symlink escapes and define explicit sensitive-file inclusion policy with redacted evidence."),
    dict(id="JARVIS-0012", title="Failed archive extraction leaves partial final outputs", kind="data-integrity", severity="high", priority="P1", status="static-confirmed", confidence=1.0, repro="hermetic-always", reqs=["REQ-ARCHIVE-001", "REQ-FILES-001"], paths=["backend/src/jarvis_gpt/archive_runtime.py:590-716", "backend/src/jarvis_gpt/archive_runtime.py:843-853"], evidence=["EVID-STATIC-019"], scenario="SCN-LIVE-026", summary="ZIP/TAR/stream extraction writes to final destinations before all limits succeed; 7z validates sizes after extraction.", impact="A rejected archive can leave a misleading or partially trusted tree that later workflows consume.", root="Extraction has no staging directory/transactional rename and exception cleanup.", remediation="Extract into a unique staging root, validate fully, atomically publish, and remove staging on every failure."),
    dict(id="JARVIS-0013", title="User regex can block the async event loop beyond cancellation budgets", kind="performance", severity="high", priority="P1", status="static-confirmed", confidence=.98, repro="static-proof", reqs=["REQ-RESOURCE-001", "REQ-JOBS-001"], paths=["backend/src/jarvis_gpt/tools.py:8601-8650", "backend/src/jarvis_gpt/autonomy_executor.py:311-350", "backend/src/jarvis_gpt/document_surfer.py:500-560", "backend/src/jarvis_gpt/document_surfer.py:756-805"], evidence=["EVID-STATIC-004", "EVID-STATIC-014"], scenario="SCN-LIVE-022", summary="web.watch and document/archive searches run arbitrary syntactically valid Python regex synchronously; asyncio timeouts cannot preempt catastrophic matching.", impact="A small input can stall chat, jobs, health and cancellation for an unbounded interval.", root="Regex complexity is not constrained and execution is not isolated in a killable worker.", remediation="Use a safe regex engine/subset or isolated subprocess with hard wall-clock and size budgets."),
    dict(id="JARVIS-0014", title="Command Center advertises a live WebSocket feed whose transport is disabled", kind="spec-gap", severity="medium", priority="P2", status="spec-gap", confidence=.99, repro="static-proof", reqs=["REQ-STREAM-001", "REQ-LEGACY-001", "REQ-DOC-001"], paths=["frontend/app/page.tsx:880-885", "frontend/app/page.tsx:1950-1955", "frontend/app/page.tsx:3940-3955", "README.md:26", "docs/assistant-notes.md:1266-1269"], evidence=["EVID-STATIC-007", "EVID-STATIC-008"], scenario="SCN-LIVE-013", summary="wsUrl always returns empty and the connection effect exits, yet active UI shows waiting for events and public docs promise /ws/events.", impact="Operators cannot distinguish disabled realtime transport from an idle healthy feed.", root="Direct browser WS was removed without a same-origin replacement or UX/docs contract update.", remediation="Implement authenticated same-origin realtime transport or remove/label the inactive surface consistently."),
    dict(id="JARVIS-0015", title="Frontend retains stale online/ready state after polling failures", kind="ux", severity="medium", priority="P2", status="probable-runtime", confidence=.96, repro="runtime-required", reqs=["REQ-FRONTEND-001"], paths=["frontend/app/page.tsx:1896-1926", "frontend/app/page.tsx:2136-2163", "frontend/app/page.tsx:3960-3962"], evidence=["EVID-STATIC-007", "EVID-STATIC-008"], scenario="SCN-LIVE-012", summary="Promise.allSettled rejections are never inspected and previous snapshots remain indefinitely; online is derived from the existence of old status.", impact="A stopped backend/dispatcher can remain displayed as online/ready with no stale timestamp.", root="Polling stores values but no freshness/error state machine.", remediation="Track last-success/failure per source, expire snapshots and render explicit stale/offline/degraded states."),
    dict(id="JARVIS-0016", title="Frontend stream accepts EOF without terminal state and cannot cancel requests", kind="ux", severity="medium", priority="P2", status="probable-runtime", confidence=.9, repro="runtime-required", reqs=["REQ-STREAM-001", "REQ-IDEMPOTENCY-001"], paths=["frontend/app/page.tsx:844-874", "frontend/app/page.tsx:2367-2480"], evidence=["EVID-STATIC-007", "EVID-STATIC-008"], scenario="SCN-LIVE-014", summary="Normal EOF is treated as success even without done/error, leaving a pending bubble; no AbortController exists for cancel/window switch.", impact="Interrupted turns can appear stuck or successful and late updates can reach the wrong UI state.", root="Transport parser lacks a required terminal-state contract and request lifecycle ownership.", remediation="Require exactly one terminal event, persist interrupted state and bind an AbortController to each turn/window."),
    dict(id="JARVIS-0017", title="Generated document collision logic reuses an existing timestamped path", kind="data-integrity", severity="medium", priority="P2", status="static-confirmed", confidence=1.0, repro="hermetic-always", reqs=["REQ-FILES-001"], paths=["backend/src/jarvis_gpt/document_surfer.py:1371-1427"], evidence=["EVID-STATIC-018"], scenario="SCN-LIVE-027", summary="After the base path exists, collision fallback has one-second precision and does not verify that the timestamped candidate is unused.", impact="Repeated/concurrent generation can overwrite an earlier artifact despite a never-overwrite claim.", root="The fallback computes a non-exclusive name instead of atomically reserving a unique path.", remediation="Use exclusive create/UUID/counter retry and apply the same rule to archive output directories."),
    dict(id="JARVIS-0018", title="Documented Compose quick start has no API token bootstrap", kind="defect", severity="medium", priority="P2", status="static-confirmed", confidence=.99, repro="static-proof", reqs=["REQ-DOC-001", "REQ-API-001"], paths=["README.md:181-187", ".env.example:52-57", "docker-compose.yml:97-99", "docker-compose.yml:134-136", "frontend/app/jarvis-api/[...path]/route.ts:46-53"], evidence=["EVID-STATIC-013", "EVID-STATIC-021"], scenario="SCN-LIVE-006", summary="README quick start leaves JARVIS_API_TOKEN empty; the proxy hard-returns 503 without a token and the container client is non-loopback to backend.", impact="The documented Compose path can build successfully but provide an unusable Command Center.", root="Token generation exists only in the PowerShell launcher, not in the documented Compose workflow.", remediation="Bootstrap a secret explicitly or fail preflight with an exact setup instruction; test both paths."),
    dict(id="JARVIS-0019", title="Mutating document/watch tools default to safe and bypass approval", kind="spec-gap", severity="high", priority="P1", status="spec-gap", confidence=.97, repro="static-proof", reqs=["REQ-APPROVAL-001", "REQ-FILES-001"], paths=["backend/src/jarvis_gpt/tools.py:319-327", "backend/src/jarvis_gpt/tools.py:1405-1626", "backend/src/jarvis_gpt/tools.py:1806-1842", "backend/src/jarvis_gpt/tools.py:548-649"], evidence=["EVID-STATIC-004", "EVID-STATIC-014"], scenario="SCN-LIVE-028", summary="Document mutation/archive extraction/create and recurring web.watch add/remove omit danger_level, inheriting safe and skipping registry approval.", impact="Durable external jobs and filesystem outputs can be created through a lower trust gate than file.write.", root="No explicit product contract classifies these side effects; default-safe masks omissions.", remediation="Define danger policy per action and make missing classification fail closed for mutating tools."),
    dict(id="JARVIS-0020", title="Dependency/build/offline contract is not reproducible from immutable inputs", kind="reliability", severity="medium", priority="P2", status="static-confirmed", confidence=.98, repro="static-proof", reqs=["REQ-DEPENDENCY-001", "REQ-OFFLINE-001"], paths=["backend/requirements-surfer.txt:10-13", "backend/src/jarvis_gpt/agent.py:3312-3323", "pyproject.toml:7-25", ".github/workflows/ci.yml:10-48", "backend/Dockerfile:1-47", "frontend/Dockerfile:1-10"], evidence=["EVID-STATIC-002", "EVID-STATIC-006", "EVID-STATIC-008"], scenario="SCN-LIVE-008", summary="An active hint installs stale conflicting surfer pins; unused httpx2 expands dev supply chain; CI ignores uv.lock and mutable image/action/apt inputs remain.", impact="Clean/offline builds can drift, downgrade browser dependencies or unexpectedly require registries.", root="Multiple dependency manifests and runtime paths are not governed by one lock/image identity policy.", remediation="Remove obsolete/unused deps, install from one lock, pin image/action digests and document cached offline start separately from rebuild."),
    dict(id="JARVIS-0021", title="Main storage lacks versioned migration/integrity/retention and policy corruption fails open", kind="data-integrity", severity="high", priority="P1", status="static-confirmed", confidence=.98, repro="static-proof", reqs=["REQ-STORAGE-001", "REQ-RESOURCE-001", "REQ-SECRET-001"], paths=["backend/src/jarvis_gpt/storage.py:101-107", "backend/src/jarvis_gpt/storage.py:386-437", "backend/src/jarvis_gpt/storage.py:681-858", "backend/src/jarvis_gpt/operations.py:18-25", "backend/src/jarvis_gpt/operations.py:67-70"], evidence=["EVID-STATIC-004", "EVID-STATIC-014", "EVID-STATIC-016"], scenario="SCN-LIVE-029", summary="Malformed JSON is silently defaulted (browser policy defaults open), the DB has no user_version/migration registry/integrity gate, and append-only audit/events/learning have no purge; conversation delete retains copied dialogue in learning.", impact="Corruption can weaken policy silently; upgrades/locks are under-specified; state grows without bound and delete semantics are privacy-ambiguous.", root="Storage evolved through CREATE IF NOT EXISTS and permissive decoders without an explicit lifecycle/retention contract.", remediation="Add versioned migrations, strict policy decoding/quarantine, integrity/restore checks and explicit retention/purge semantics."),
    dict(id="JARVIS-0022", title="Web-watch persists digest before durable notification delivery", kind="data-integrity", severity="medium", priority="P2", status="static-confirmed", confidence=.98, repro="static-proof", reqs=["REQ-JOBS-001", "REQ-STORAGE-001"], paths=["backend/src/jarvis_gpt/autonomy_executor.py:378-416"], evidence=["EVID-STATIC-014"], scenario="SCN-LIVE-021", summary="The new digest is committed before event, memory and bus notification; a crash/error afterward makes retry see no change.", impact="A page change alert can be lost permanently with no reconciliation evidence.", root="Observed state and notification delivery are not connected by an outbox/acknowledgement state machine.", remediation="Persist detection plus pending notification atomically and acknowledge only after durable delivery."),
    dict(id="JARVIS-0023", title="Unauthenticated health response exposes absolute runtime path", kind="defensive-design", severity="medium", priority="P2", status="static-confirmed", confidence=.99, repro="static-proof", reqs=["REQ-API-001", "REQ-SECRET-001"], paths=["backend/src/jarvis_gpt/api.py:507-551", "backend/src/jarvis_gpt/config.py:303-305"], evidence=["EVID-STATIC-013", "EVID-STATIC-021"], scenario="SCN-LIVE-030", summary="/health bypasses auth and includes settings.home; native defaults bind 0.0.0.0 with no loopback token requirement.", impact="A non-loopback deployment discloses host/runtime layout to unauthenticated clients.", root="Detailed diagnostics and minimal liveness share one public endpoint.", remediation="Keep unauthenticated liveness minimal; guard detailed health and fail startup on unsafe bind/token combinations."),
    dict(id="JARVIS-0024", title="Bundled web synthesis/TLS trust policy is weaker than the core path", kind="defensive-design", severity="medium", priority="P2", status="spec-gap", confidence=.95, repro="static-proof", reqs=["REQ-TRUST-001", "REQ-URL-001", "REQ-DOC-001"], paths=["backend/src/jarvis_gpt/agent.py:221-230", "backend/src/jarvis_gpt/agent.py:3600-3620", "backend/src/jarvis_gpt/agent.py:3812-3845", "backend/src/jarvis_gpt/web_surfer.py:536-550"], evidence=["EVID-STATIC-004", "EVID-STATIC-014"], scenario="SCN-LIVE-024", summary="The dedicated web synthesis prompt omits the core never-follow-source-instructions rule and surfer contexts ignore TLS certificate errors.", impact="Untrusted page instructions/provenance and invalid TLS are handled inconsistently across web paths.", root="The bundled surfer/synthesis path bypasses shared trust metadata and transport policy.", remediation="Reuse one untrusted-content prompt/provenance contract and fail closed on TLS unless a narrowly approved exception exists."),
    dict(id="JARVIS-0025", title="Frontend accessibility and test harness leave interaction regressions unchecked", kind="test-gap", severity="low", priority="P3", status="test-gap", confidence=.95, repro="static-proof", reqs=["REQ-FRONTEND-001", "REQ-TEST-001"], paths=["frontend/app/page.tsx:4467-4497", "frontend/app/page.tsx:4702-4736", "frontend/package.json:5-10", ".github/workflows/ci.yml:28-48"], evidence=["EVID-STATIC-007", "EVID-STATIC-008"], scenario="SCN-LIVE-031", summary="Tabs/resize lack complete keyboard/ARIA semantics and there are no frontend unit, component, E2E or accessibility tests.", impact="Keyboard, focus, reduced-motion, stream and stale-state regressions can ship while typecheck/build remain green.", root="The UI is a 5.8k-line monolith and frontend CI has no behavioral test layer.", remediation="Add component boundaries and browser/component accessibility tests for critical states."),
]


LIVE = [
    ("SCN-LIVE-001", "P0", "source-drift", "Compare production tree to source commit excluding .audit/** and docs/audit/**; if different create SOURCE_DRIFT.md and rerun affected checks.", "No unexplained production drift; conclusions explicitly revalidated."),
    ("SCN-LIVE-002", "P1", "powershell-static", "Run PowerShell parser/PSScriptAnalyzer on every tracked .ps1/.cmd wrapper from D:\\jarvis-gpt without executing service actions.", "All scripts parse; any analyzer suppression is reviewed."),
    ("SCN-LIVE-003", "P0", "launcher-ownership", "In a disposable launcher state and fake dispatcher identity, test absent, truncated and malformed launcher-state.json then invoke stop.", "Unknown ownership fails closed and fake foreign dispatcher remains running."),
    ("SCN-LIVE-004", "P0", "launcher-process-scope", "Start a harmless process whose command line mentions D:\\jarvis-gpt but is not launcher-owned; invoke stop and inspect exact PID/birth identity.", "Foreign process survives; only recorded descendants stop."),
    ("SCN-LIVE-005", "P1", "launcher-paths", "Copy only launcher harness to temp paths containing spaces, Cyrillic and apostrophe; invoke status/doctor from another cwd.", "Path resolution/quoting works and no unrelated path is touched."),
    ("SCN-LIVE-006", "P0", "compose-token", "Render isolated Compose project twice: empty JARVIS_API_TOKEN, then a synthetic token; do not use user data or production ports.", "Empty token gives actionable preflight failure; synthetic token yields authenticated proxy/backend path."),
    ("SCN-LIVE-007", "P0", "compose-health", "Run docker compose config and an isolated startup using cached images on alternate project name/ports.", "Backend has a valid health contract and frontend reaches ready deterministically."),
    ("SCN-LIVE-008", "P1", "offline-turbo", "After inventorying cached assets, disconnect network safely and start/restart gemma4-turbo without --build; capture Docker events and DNS/registry attempts.", "Cached turbo start does not pull or resolve registries; explicit network tools degrade independently."),
    ("SCN-LIVE-009", "P1", "offline-mono-perf", "Repeat cached offline start for gemma4-mono-perf and capture resolved model/image/flags.", "31B perf profile uses intended model/4k flags and no downloads."),
    ("SCN-LIVE-010", "P1", "offline-mono", "Repeat cached offline start for gemma4-mono with watchdog and cleanup.", "31B mono profile uses intended model/offload flags and no downloads."),
    ("SCN-LIVE-011", "P0", "model-switch", "Use disposable JARVIS_HOME, copied DB and fake dispatcher: activate empty/incompatible/complete custom model and inject stop/start failures.", "Invalid model rejected before state change; every failed switch restores prior override and runtime."),
    ("SCN-LIVE-012", "P1", "frontend-stale", "Load UI successfully, stop only launcher-owned backend, wait at least 6s and capture UI plus failed requests.", "UI shows offline/stale within bounded time and never keeps ready from old data."),
    ("SCN-LIVE-013", "P1", "websocket-contract", "Open Command Center with authenticated backend, capture WS connections/events, disconnect/reconnect and inspect event-feed state.", "Transport exists or UX explicitly says disabled; terminal events are ordered/recoverable."),
    ("SCN-LIVE-014", "P1", "stream-terminal-cancel", "Point UI at a local synthetic NDJSON stream that ends without terminal, then a slow stream cancelled by user/window close.", "Missing terminal becomes interrupted/error; cancellation reaches backend and no late UI mutation occurs."),
    ("SCN-LIVE-015", "P1", "stream-reconnect", "Exercise normal/slow/disconnected/reconnected API and WebSocket flows with unique correlation IDs.", "One ordered terminal state per request; no duplicates or cross-session events."),
    ("SCN-LIVE-016", "P0", "chat-idempotency", "With fake delayed LLM and temp append target, send two concurrent turns in one conversation; lose first HTTP response and retry same logical request.", "Turns serialize and append/side effects occur exactly once using caller idempotency key."),
    ("SCN-LIVE-017", "P0", "approval-atomicity-secrets", "Use nested synthetic sentinel in tool environment/arguments; inject audit failure after approve/claim/finalize on copied DB, restart and query API/audit.", "No sentinel persists; client/DB/audit agree; recovery cannot replay action."),
    ("SCN-LIVE-018", "P0", "db-transaction-poison", "On temp DB make MemoryVault throw after SQL in add_memory, then perform unrelated write and reopen DB.", "Failed memory is absent and later write cannot commit it."),
    ("SCN-LIVE-019", "P0", "job-race-start", "Create two benign jobs with barriers; concurrent start/update plus paused/done/already-running starts; capture rows/history/events.", "No lost fields; rejected starts never publish started; every admitted run has terminal record."),
    ("SCN-LIVE-020", "P0", "replay-eviction", "Use tiny temp replay ledger, evict an old committed key, change temp target and retry old transaction key.", "Retry is rejected/recognized as expired and mutation is not reapplied."),
    ("SCN-LIVE-021", "P1", "watch-outbox-time", "Use fake fetch and inject failure after digest before event/memory; also test malformed deadline and extreme cadence with unrelated due job.", "Alert reconciles/retries; bad job is quarantined without blocking others."),
    ("SCN-LIVE-022", "P0", "regex-watchdog", "In a disposable subprocess with heartbeat, run reviewed pathological regex against small synthetic text for watch/document/archive search.", "Hard budget aborts match and heartbeat/event loop remains responsive."),
    ("SCN-LIVE-023", "P0", "browser-public-only", "Use mock resolver/route and local synthetic redirect graph representing public-to-private, mixed DNS and rebinding; no external targets.", "Every redirect/subresource/private destination is blocked before connection."),
    ("SCN-LIVE-024", "P0", "browser-isolation-trust", "Start worker with synthetic secret-name env and sentinel runtime file; inspect effective env/mount/uid/caps/browser args; use self-signed local HTTPS and instruction sentinel.", "Secrets/files are not available, sandbox is active, invalid TLS fails closed and source instructions remain data."),
    ("SCN-LIVE-025", "P0", "ingest-boundary", "Temp allowed root contains normal file, .env sentinel and symlink to an outside temp sentinel; call directory ingest and inspect DB/files/audit/logs.", "Only intended contained files ingest; no sentinel content/path leaks."),
    ("SCN-LIVE-026", "P1", "archive-atomicity", "Repeat tiny ZIP/TAR/GZ/available-7z extraction with low limits into pre-snapshotted temp output.", "On error output snapshot is unchanged; success publishes a complete tree atomically."),
    ("SCN-LIVE-027", "P1", "document-collision", "Freeze clock and run repeated/concurrent same output name in temp output root.", "Each success gets a unique artifact or explicit collision; no overwrite/mixed directory."),
    ("SCN-LIVE-028", "P0", "tool-approval-policy", "Direct-run each mutating document/archive/watch tool without and with exact synthetic approval; inspect DB/files/jobs.", "Policy matches documented classification; denial causes zero side effects."),
    ("SCN-LIVE-029", "P1", "storage-lifecycle", "Copied/generated DB: corrupt locked policy JSON, simulate older schema, large histories, backup/restore, quick_check, purge and WAL checkpoint.", "Policy fails closed; migrations/restore are explicit; purge semantics and bounds hold."),
    ("SCN-LIVE-030", "P1", "health-exposure", "Isolated server on alternate port with synthetic local/remote client matrix and token states; query /health only.", "Unauthenticated response is minimal and never returns home/source/stored paths."),
    ("SCN-LIVE-031", "P2", "gui-accessibility", "Keyboard-only tabs/close/resize, focus order, reduced motion, 200-400% zoom, DPI changes, long Unicode/code/table/URL and long history.", "All controls operable/labeled; no clipped critical state or focus loss."),
    ("SCN-LIVE-032", "P2", "service-worker-upgrade", "Install old then new build in test browser; test online/offline root/trace navigation and inspect CacheStorage.", "New shell activates predictably; API data is never served stale from cache."),
    ("SCN-LIVE-033", "P0", "host-execution", "Synthetic execution roots and harmless exact actions: approve/deny/cancel/retry process/file/host bridge operations; capture birth identities and cleanup.", "Approval/action binding exact; denial/cancel/retry does not duplicate; no orphan resources."),
    ("SCN-LIVE-034", "P1", "profile-resolution", "For all three profiles capture launcher env, model catalog, dispatcher desired/actual identity, backend status and UI label.", "All five layers agree on profile/model/flags and reject unknown aliases."),
    ("SCN-LIVE-035", "P1", "start-stop-idempotency", "Repeat start/stop/restart five times on isolated project, including interrupted startup and stale state; inspect processes/containers/ports/locks.", "No orphan/foreign resource, pull surprise or accumulating state; cleanup bounded."),
    ("SCN-LIVE-036", "P2", "resource-bounds", "Synthetic large history/document/archive/tool output and slow disk/consumer with watchdog; record loop lag, memory, DB/WAL and render behavior.", "Configured bounds/backpressure/retention hold; cancellation and health stay responsive."),
]

REQ_SCENARIOS = {
    "REQ-API-001": ["SCN-LIVE-015", "SCN-LIVE-030"],
    "REQ-STREAM-001": ["SCN-LIVE-013", "SCN-LIVE-014", "SCN-LIVE-015"],
    "REQ-CLI-001": ["SCN-LIVE-005", "SCN-LIVE-033"],
    "REQ-TOOL-001": ["SCN-LIVE-028", "SCN-LIVE-033"],
    "REQ-APPROVAL-001": ["SCN-LIVE-017", "SCN-LIVE-028", "SCN-LIVE-033"],
    "REQ-IDEMPOTENCY-001": ["SCN-LIVE-016", "SCN-LIVE-020", "SCN-LIVE-033"],
    "REQ-STORAGE-001": ["SCN-LIVE-018", "SCN-LIVE-029"],
    "REQ-PROFILE-001": ["SCN-LIVE-011", "SCN-LIVE-034"],
    "REQ-OFFLINE-001": ["SCN-LIVE-008", "SCN-LIVE-009", "SCN-LIVE-010"],
    "REQ-LAUNCHER-001": ["SCN-LIVE-003", "SCN-LIVE-004", "SCN-LIVE-035"],
    "REQ-FRONTEND-001": ["SCN-LIVE-012", "SCN-LIVE-014", "SCN-LIVE-031"],
    "REQ-TRUST-001": ["SCN-LIVE-023", "SCN-LIVE-024", "SCN-LIVE-025"],
    "REQ-URL-001": ["SCN-LIVE-023", "SCN-LIVE-024"],
    "REQ-FILES-001": ["SCN-LIVE-025", "SCN-LIVE-026", "SCN-LIVE-027"],
    "REQ-ARCHIVE-001": ["SCN-LIVE-026"],
    "REQ-SECRET-001": ["SCN-LIVE-017", "SCN-LIVE-024", "SCN-LIVE-030"],
    "REQ-JOBS-001": ["SCN-LIVE-019", "SCN-LIVE-021", "SCN-LIVE-022"],
    "REQ-RESOURCE-001": ["SCN-LIVE-022", "SCN-LIVE-029", "SCN-LIVE-036"],
    "REQ-DEPENDENCY-001": ["SCN-LIVE-008", "SCN-LIVE-007"],
    "REQ-TEST-001": ["SCN-LIVE-002", "SCN-LIVE-031", "SCN-LIVE-036"],
    "REQ-DOC-001": ["SCN-LIVE-006", "SCN-LIVE-013", "SCN-LIVE-034"],
    "REQ-LEGACY-001": ["SCN-LIVE-013", "SCN-LIVE-034"],
}


TESTS = [
    ("TEST-STATIC-001", "Python compileall", "PASS_HERMETIC", "EVID-STATIC-001"),
    ("TEST-STATIC-002", "Ruff repository lint", "PASS_HERMETIC", "EVID-STATIC-002"),
    ("TEST-STATIC-003", "Broad pytest excluding prohibited host-bridge modules", "BLOCKED_BY_ENV", "EVID-STATIC-003"),
    ("TEST-STATIC-004", "Safe backend pytest subset", "PASS_HERMETIC", "EVID-STATIC-004"),
    ("TEST-STATIC-005", "Frontend npm setup with unwritable default cache", "BLOCKED_BY_ENV", "EVID-STATIC-005"),
    ("TEST-STATIC-006", "Pinned frontend npm ci in disposable copy", "PASS_HERMETIC", "EVID-STATIC-006"),
    ("TEST-STATIC-007", "Frontend TypeScript typecheck", "PASS_HERMETIC", "EVID-STATIC-007"),
    ("TEST-STATIC-008", "Frontend production build", "PASS_HERMETIC", "EVID-STATIC-008"),
    ("TEST-STATIC-009", "Tracked file inventory", "PASS_HERMETIC", "EVID-STATIC-009"),
    ("TEST-STATIC-010", "Initial static contract harness version", "INCONCLUSIVE", "EVID-STATIC-010"),
    ("TEST-STATIC-011", "Deterministic CLI help", "PASS_HERMETIC", "EVID-STATIC-011"),
    ("TEST-STATIC-012", "Deterministic CLI profile catalog", "PASS_HERMETIC", "EVID-STATIC-012"),
    ("TEST-STATIC-013", "JSON/TOML/YAML/OpenAPI/profile contract", "PASS_HERMETIC", "EVID-STATIC-013"),
    ("TEST-STATIC-014", "Safe backend subset under coverage", "PASS_HERMETIC", "EVID-STATIC-014"),
    ("TEST-STATIC-015", "Coverage JSON generation", "PASS_HERMETIC", "EVID-STATIC-015"),
    ("TEST-STATIC-016", "Coverage report", "PASS_HERMETIC", "EVID-STATIC-016"),
    ("TEST-STATIC-017", "Public feature/requirement extraction", "PASS_HERMETIC", "EVID-STATIC-017"),
    ("TEST-STATIC-018", "Document output collision oracle", "FAIL_HERMETIC", "EVID-STATIC-018"),
    ("TEST-STATIC-019", "Archive atomic failure oracle", "FAIL_HERMETIC", "EVID-STATIC-019"),
    ("TEST-STATIC-020", "Full pytest collection", "PASS_HERMETIC", "EVID-STATIC-020"),
    ("TEST-STATIC-021", "Static contracts plus credential signature scan", "PASS_HERMETIC", "EVID-STATIC-021"),
    ("TEST-STATIC-022", "PowerShell availability", "BLOCKED_BY_ENV", "EVID-STATIC-022"),
    ("TEST-STATIC-023", "Docker/Compose availability", "BLOCKED_BY_ENV", "EVID-STATIC-023"),
    ("TEST-STATIC-024", "Initial audit artifact rendering", "PASS_HERMETIC", "EVID-STATIC-024"),
    ("TEST-STATIC-025", "Final traceability artifact rendering", "PASS_HERMETIC", "EVID-STATIC-025"),
    ("TEST-STATIC-026", "Traceability and source-isolation consistency gate", "PASS_HERMETIC", "EVID-STATIC-026"),
    ("TEST-STATIC-027", "Final evidence manifest snapshot", "PASS_HERMETIC", "EVID-STATIC-027"),
]


def write(path: str | Path, content: str) -> None:
    target = RUN / path if not isinstance(path, Path) or not path.is_absolute() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content.rstrip() + "\n", encoding="utf-8")


def render_finding(item: dict[str, object], index: int) -> str:
    task = f"CTASK-{index:04d}"
    paths = "\n".join(f"- `{p}`" for p in item["paths"])
    evidence = ", ".join(item["evidence"])
    reqs = ", ".join(item["reqs"])
    runtime = "Not required to prove the code-level violation; PHASE B measures integrated impact." if item["repro"] != "runtime-required" else "Required before converting probability into a runtime-confirmed defect."
    return f'''---
id: {item['id']}
title: "{item['title']}"
kind: {item['kind']}
severity: {item['severity']}
priority: {item['priority']}
phase_a_status: {item['status']}
confidence: {item['confidence']:.2f}
reproducibility: {item['repro']}
components: ["{item['paths'][0].split(':')[0]}"]
feature_ids: []
requirement_ids: [{reqs}]
scenario_ids: [{item['scenario']}]
evidence_ids: [{evidence}]
affected_paths: [{', '.join(json.dumps(p.split(':')[0]) for p in item['paths'])}]
phase_b_scenarios: [{item['scenario']}]
candidate_task_ids: [{task}]
---

# {item['id']} — {item['title']}

## 1. Summary

{item['summary']}

## 2. Contract and impact

Requirements: {reqs}. {item['impact']}

## 3. Static evidence

{paths}

Evidence records: {evidence}.

## 4. Hermetic reproduction

{('See the failing hermetic oracle in ' + evidence + '.' if item['repro'] == 'hermetic-always' else 'Static control/data-flow proof; no production service was started.')}

## 5. Expected vs observed

Expected: the cited requirements hold atomically and fail closed. Observed: {item['summary']}

## 6. Runtime confirmation

{runtime}

## 7. Root cause

Confirmed design/code cause: {item['root']}

## 8. Affected flow

The operator/API/UI request traverses the cited component and can reach the impacted state before an authoritative error, rollback or trust gate is established.

## 9. Data/privacy/permission implications

Captured in kind `{item['kind']}` and impact above; PHASE B must use only synthetic roots, sentinel values and copied state.

## 10. Safe PHASE B procedure

Run `{item['scenario']}` from `LIVE_SCENARIO_QUEUE.csv`; capture the listed telemetry and perform its cleanup. Do not use real secrets or external targets.

## 11. Remediation direction

{item['remediation']}

## 12. Regression risks

State transitions, compatibility, error reporting and recovery ordering are coupled; preserve existing deny-by-default and exact-approval behaviors.

## 13. Acceptance criteria draft

- A regression test reproduces the current failure with synthetic inputs.
- The relevant positive, error and recovery variants pass.
- No production behavior outside the documented contract changes silently.

## 14. Related gaps

See `SPEC_GAPS.md`, `TEST_GAPS.md`, `{task}` and `{item['scenario']}`.
'''


def render_task(item: dict[str, object], index: int) -> str:
    status = "BLOCKED_BY_SPEC" if item["status"] in {"spec-gap", "test-gap"} else ("STATIC_ONLY_REVIEW_REQUIRED" if item["repro"] == "hermetic-always" else "AWAITING_PHASE_B")
    files = "\n".join(f"- `{p.split(':')[0]}`" for p in item["paths"])
    return f'''# CTASK-{index:04d} — {item['title']}

Status: `{status}`

Root finding: `{item['id']}`. Runtime check before READY: `{item['scenario']}`.

Context files:

{files}

Tentative allowed files are limited to the smallest owning subsystem plus a new regression test. Production scope must be re-approved after PHASE B/source-drift review.

Regression test: reproduce the finding with fake/synthetic state, then assert positive, error and recovery behavior.

Acceptance criteria:

- {item['remediation']}
- Existing safety/approval/idempotency tests remain green.
- PHASE B updates the finding to confirmed/refuted and only then may place this task in a Spark queue.
'''


def main() -> None:
    features = [json.loads(line) for line in (MACHINE / "features.jsonl").read_text().splitlines()]
    requirements = [json.loads(line) for line in (MACHINE / "requirements.jsonl").read_text().splitlines()]
    for index, item in enumerate(FINDINGS, 1):
        write(f"findings/{item['id']}.md", render_finding(item, index))
        write(f"candidate_tasks/CTASK-{index:04d}.md", render_task(item, index))

    finding_rows = []
    task_rows = []
    scenario_rows = []
    for index, item in enumerate(FINDINGS, 1):
        finding_rows.append({**item, "candidate_task_ids": [f"CTASK-{index:04d}"]})
        task_rows.append({"id": f"CTASK-{index:04d}", "finding_id": item["id"], "status": "BLOCKED_BY_SPEC" if item["status"] in {"spec-gap", "test-gap"} else ("STATIC_ONLY_REVIEW_REQUIRED" if item["repro"] == "hermetic-always" else "AWAITING_PHASE_B")})
    for order, (sid, priority, domain, actions, oracle) in enumerate(LIVE, 1):
        linked = [f["id"] for f in FINDINGS if f["scenario"] == sid]
        req_ids = sorted(req_id for req_id, scenario_ids in REQ_SCENARIOS.items() if sid in scenario_ids)
        scenario_rows.append({"id": sid, "phase": "B", "priority": priority, "domain": domain, "status": "NOT_RUN", "finding_ids": linked, "requirement_ids": req_ids, "validation_modes": ["positive", "error", "recovery"], "actions": actions, "oracle": oracle})
    for tid, label, status, evid in TESTS:
        scenario_rows.append({"id": tid, "phase": "A", "domain": "repository-static", "status": status, "evidence_ids": [evid], "label": label})
    (MACHINE / "findings.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in finding_rows), encoding="utf-8")
    (MACHINE / "candidate_tasks.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in task_rows), encoding="utf-8")
    (MACHINE / "scenarios.jsonl").write_text("".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in scenario_rows), encoding="utf-8")

    with (RUN / "STATIC_SCENARIO_MATRIX.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["scenario_id", "feature_ids", "requirement_ids", "domain", "component", "method", "preconditions", "state_before", "stimulus", "oracle", "invariants", "profile", "network_state", "resource_state", "storage_state", "concurrency", "permissions", "locale_encoding", "phase", "status", "evidence_ids", "finding_ids", "notes"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for tid, label, status, evid in TESTS:
            linked = [f["id"] for f in FINDINGS if evid in f["evidence"]]
            writer.writerow({"scenario_id": tid, "domain": "repository-static", "component": "checkout", "method": label, "preconditions": SOURCE_COMMIT, "state_before": "clean checkout", "stimulus": "exact argv in evidence metadata", "oracle": "documented command-specific oracle", "invariants": "no production changes; no live services/network", "profile": "none", "network_state": "disabled except pinned dependency setup", "resource_state": "cloud sandbox", "storage_state": "synthetic/temp", "concurrency": "as test defines", "permissions": "sandbox", "locale_encoding": "UTF-8", "phase": "A", "status": status, "evidence_ids": evid, "finding_ids": ";".join(linked), "notes": "Metadata JSON contains timestamp, cwd, duration, exit and stdout/stderr paths."})

    with (RUN / "LIVE_SCENARIO_QUEUE.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = ["order", "scenario_id", "priority", "risk", "domain", "feature_ids", "requirement_ids", "exact_preconditions", "exact_commands_or_actions", "required_profile", "required_services", "state_setup", "stimulus", "expected_oracle", "telemetry_to_capture", "safety_isolation", "cleanup", "repeat_count", "time_budget", "source_findings", "dependencies", "status", "notes"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for order, (sid, priority, domain, actions, oracle) in enumerate(LIVE, 1):
            linked_items = [f for f in FINDINGS if f["scenario"] == sid]
            req_ids = sorted(req_id for req_id, scenario_ids in REQ_SCENARIOS.items() if sid in scenario_ids)
            writer.writerow({"order": order, "scenario_id": sid, "priority": priority, "risk": "high" if priority in {"P0", "P1"} else "medium", "domain": domain, "feature_ids": "", "requirement_ids": ";".join(req_ids), "exact_preconditions": f"D:\\jarvis-gpt at {SOURCE_COMMIT} or drift reviewed; installed product; backups; alternate ports/project when applicable", "exact_commands_or_actions": actions, "required_profile": "scenario-specific; all profiles only in 008-010/034", "required_services": "scenario-specific; use fake/local fixture whenever possible", "state_setup": "synthetic roots, copied DB/state, unique IDs and sentinels", "stimulus": actions, "expected_oracle": oracle, "telemetry_to_capture": "UTC command log, exit codes, app/backend/launcher logs, relevant DB/API/UI state, process/container identities", "safety_isolation": "No external targets or real secrets; destructive/corruption checks only on copies/temp roots", "cleanup": "Stop only owned fixtures/project; remove temp roots; restore network/settings; verify no orphan process/container/port", "repeat_count": 3 if priority in {"P0", "P1"} else 1, "time_budget": "15m" if priority == "P0" else "10m", "source_findings": ";".join(f["id"] for f in linked_items), "dependencies": "preceding baseline/source-drift check", "status": "NOT_RUN", "notes": "Positive/error/recovery variants; functional defensive scenario; no exploit payloads."})

    severity = Counter(str(f["severity"]) for f in FINDINGS)
    statuses = Counter(str(f["status"]) for f in FINDINGS)
    test_status = Counter(t[2] for t in TESTS)
    write("START_HERE.md", f'''# JARVIS PHASE A — start here

Run `{RUN_ID}` audits source `{SOURCE_COMMIT}` from `{SOURCE_BRANCH}`. Read `STATIC_EXECUTIVE_SUMMARY.md`, then `STATIC_FINDINGS_INDEX.md`, `STATIC_TEST_RESULTS.md`, and `handoff/PHASE_B_START_HERE.md`.

PHASE A is repository-only. Runtime, Windows, Docker/WSL/GPU/LLM and visual behavior remain unverified. Spark is locked until PHASE B.
''')
    write("AUDIT_STATE.md", f'''# Audit state

- PHASE A: `COMPLETE_WITH_BLOCKERS`
- Source: `{SOURCE_COMMIT}` (`{SOURCE_BRANCH}`)
- Audit branch: `{AUDIT_BRANCH}`
- Tracked source inventory: 167 files / 125,148 lines
- Features: {len(features)}; requirements: {len(requirements)}; static command records: {len(TESTS)}
- Findings: {len(FINDINGS)} ({dict(severity)}; statuses {dict(statuses)})
- Live scenarios: {len(LIVE)} (`NOT_RUN`)
- Blockers: no PowerShell, Docker/Compose, Windows/live machine, GPU/models, real browser or GUI.
''')
    write("PHASE_A_COMPLETION.md", '''# PHASE A completion

All tracked production/config/test/script/doc files were indexed; architecture, entry points, persistence and trust boundaries were reviewed; public API/CLI/tools/profiles/UI were assigned feature records; repository-safe checks were executed; findings have evidence and PHASE B scenarios; handoff and source-drift policy exist. The consistency harness is the final gate.

Status is `COMPLETE_WITH_BLOCKERS` because platform/runtime/visual behavior cannot be verified here. No combined-audit or Spark READY marker exists.
''')
    write("STATIC_EXECUTIVE_SUMMARY.md", f'''# Static executive summary

PHASE A found a large, safety-conscious local agent runtime with strong typed execution, exact approval binding, process identity checks, hardened core HTTP transport, non-root/read-only Compose settings, substantial backend assertions and a buildable frontend.

The main risks are boundary inconsistency rather than absence of controls: browser paths bypass the hardened HTTP policy; browser worker containment exposes secrets/data; state mutations frequently lack one atomic unit; launcher ownership is inferred too broadly; autonomy/chat retries lack durable idempotency; frontend truth/realtime contracts have drifted; and archive/document failure paths are not atomic.

Evidence: 254 features, 22 requirements, 830 collected pytest cases, 394 safe cases passing, 50.37% line coverage for that deliberately restricted subset, clean lint/compile/typecheck/build/config/schema checks, and two intentional `FAIL_HERMETIC` reproductions. Findings: {dict(severity)}; status mix {dict(statuses)}.

Runtime has not been verified. Spark remains blocked until all applicable PHASE B scenarios are executed and findings are confirmed/refuted.
''')
    write("SYSTEM_MAP.md", '''# System map

```mermaid
flowchart TD
  U["Operator: launcher / CLI / Command Center"] --> A["FastAPI REST / NDJSON / WebSocket"]
  A --> G["Agent + planner + approvals + tool registry"]
  G --> L["Local OpenAI-compatible dispatcher"]
  G --> X["Execution / host / web / document workers"]
  G --> S["SQLite WAL + vault + files + replay journals"]
```

Startup: `jarvis.cmd` resolves repo, delegates to `jarvis-launcher.ps1`, prepares runtime/token/profile, controls dispatcher/backend/frontend, and records launcher state. Backend lifespan acquires the primary lease, initializes/reconciles storage/execution, starts supervisors and serves API. Shutdown must cancel supervisors/workers and release state.

Response path: UI/CLI -> API -> agent context/persona/memory -> LLM/tool loop -> approval/execution -> storage/audit/event bus -> NDJSON/UI. Web/document content is untrusted data but crosses separate core HTTP, Playwright worker, parser and synthesis paths. Persistent state includes main SQLite/WAL, playbook SQLite, vault Markdown, downloads/uploads/document outputs, launcher JSON, replay/checkpoint journals and model/runtime directories under `D:\\jarvis`.

Privilege transitions: browser/server input -> model prompt; model proposal -> schema/tool policy; tool -> approval; approval -> exact execution capability; container -> host bridge; browser worker -> network/runtime. Findings identify where those transitions differ across implementations.
''')
    write("FEATURE_CATALOG.md", f'''# Feature catalog

Machine-readable catalog: `machine/features.jsonl` ({len(features)} rows). It contains one record per 94 REST operations, one WebSocket, 32 CLI commands, 98 registered tools, three profiles, three services, 15 UI surfaces and eight subsystem features. Each row links at least one requirement and marks PHASE B need.

Tracked-file inventory with classification, entry symbols, imports, side-effect categories and test references: `evidence/static/tracked_file_inventory.csv` (167 rows). Summary: 64 production, 2 schema, 52 tests, 18 config, 13 scripts, 14 docs, 3 generated and 1 repository metadata/unknown.

No predeclared legacy count was assumed. Confirmed inactive/leaking items include disabled direct WS UI, legacy mission handler/localStorage compatibility, legacy Gemma-via-Qwen env naming and active stale `requirements-surfer.txt` guidance.
''')
    write("CONFIGURATION_MAP.md", '''# Configuration map

Precedence is split: explicit CLI profile -> `JARVIS_PROFILE` -> `gemma4-turbo`; launcher overwrites key runtime/profile/API values; Compose reads shell/.env interpolation; backend reads process env; frontend proxy reads server-only backend URL/token. This precedence is not documented as one contract.

Profiles: `gemma4-turbo` -> `gemma4-26b-a4b-nvfp4`; `gemma4-mono-perf` and `gemma4-mono` -> `gemma4-31b-it-nvfp4` with different length/offload flags. Static extraction confirms launcher/config/catalog agree. PHASE B must compare actual dispatcher/UI identity.

Drift: learning interval native default 120s vs 600s in `.env.example`, Compose and runtime docs. Images/actions/base packages use mutable tags; cached-offline start and rebuild/download are distinct but not fully specified. Compose frontend health dependency and token bootstrap have formal findings.
''')
    write("BEHAVIORAL_CONTRACT.md", '''# Behavioral contract

The 22 canonical requirements are in `machine/requirements.jsonl`. They cover API/stream truth, CLI outcomes, tool schemas, exact approvals, idempotency, atomic storage, profile identity, offline core, launcher ownership, frontend freshness, untrusted input, public-only URLs, file roots, archive atomicity, secret minimization, background jobs, resource bounds, reproducible dependencies, test oracles, docs coherence and legacy isolation.

Positive static support includes conditional approval claim, exact mission/action binding, fail-closed interrupted approval recovery, typed execution verification/rollback, durable checksummed replay, process birth identity, deny-by-default execution capabilities, hardened core HTTP public-only transport, upload size cleanup and React rendering without raw HTML.

Conflicts are not resolved by guessing: see `SPEC_GAPS.md`. Nondeterministic LLM behavior remains a semantic PHASE B contract, not exact-string equality.
''')
    write("DATA_AND_TRUST_BOUNDARIES.md", '''# Data and trust boundaries

| Data | Source/trust | Validation | Storage/sinks | Retention/exposure | PHASE B |
|---|---|---|---|---|---|
| Chat/tool args | operator/model; mixed | Pydantic + tool schema + approval | messages, audit, approvals, LLM | largely unbounded; approval payload gap | retry/isolation/secrets |
| Web pages/results | untrusted remote | hardened core HTTP; inconsistent Playwright policy | evidence, prompt, memory | provenance varies | redirect/subresource/injection/TLS |
| Documents/archives | untrusted local | type/path/size checks; symlink/atomicity gaps | copied files, chunks, generated output | no unified cleanup TTL | symlink, bombs, collision |
| Host/process actions | privileged | capability config, SafeGate, PID identity | session/replay/audit | bounded replay details | approve/cancel/retry/orphan |
| SQLite/runtime KV | trusted state, corruption possible | permissive JSON, no version/integrity gate | WAL, backup, API/UI | append-only growth | corruption/lock/full/restore |
| Secrets/tokens | protected env | redaction on some sinks | worker env, approvals/audit risk | undefined | synthetic sentinel scan |
| Profiles/models | operator config + filesystem | name/path checks, no artifact compatibility gate | override, dispatcher | durable override | all profiles/switch rollback |

Session isolation, deletion semantics and sensitive-data retention require live/copied-state confirmation.
''')
    write("TEST_INVENTORY.md", '''# Test inventory

- 52 tracked test/fixture files; `pytest --collect-only` expands to 830 cases.
- Safe subset: 394 passed. Broad sandbox run: 717 passed, 3 skipped, then 68 failures/16 errors dominated by denied AF_UNIX/socket/process containment; classified `BLOCKED_BY_ENV`, not product FAIL.
- Strong: approvals/execution/replay/planner/storage happy paths, model/dispatcher contracts, web parsing/core destination checks, document/tool routing.
- Weak/absent: frontend behavioral tests; PowerShell behavior; Docker/Compose render/start; browser effective sandbox; DB/vault/audit fault injection; concurrent jobs/chat/events; retry idempotency; stream disconnect; retention/migrations; symlink/sensitive ingest; archive rollback; hostile regex.
- 16 skip-decorated plus explicit skip cases create OS coverage gaps. CI is Windows/Python 3.11 while the container is Linux/Python 3.12.

Fixtures are predominantly fake/mock/synthetic. Host-bridge and process-tree modules were not executed in PHASE A because the campaign forbids those surfaces here.
''')
    write("STATIC_TEST_RESULTS.md", "# Static test results\n\n" + "\n".join(f"- `{tid}` — **{status}** — {label}; evidence `{evid}`." for tid, label, status, evid in TESTS) + f"\n\nStatus counts: {dict(test_status)}. TEST-STATIC-010 was a harness-selection error corrected by TEST-STATIC-013/021. TEST-STATIC-005 was an unwritable default npm cache corrected in a disposable copy by TEST-STATIC-006; neither is a product defect.")
    write("STATIC_COVERAGE_REPORT.md", '''# Static coverage report

The approved 394-test subset covered 16,103 of 31,972 executable backend statements: **50.37%**. Full per-file JSON is `evidence/static/coverage.json`; report stdout is EVID-STATIC-016.

High coverage examples: storage 87%, operations 86%, persona 88%, redaction 95%, verification 93%. Risk-heavy low coverage in this restricted run: tools 24%, runtime lease 16%, web-surfer adapter 27%, state verification 41%, web surfer 46%, model hub 0%, worker 0%. Several low values are caused by deliberately excluded socket/process/runtime suites, but they still define PHASE B priorities.

Frontend has zero unit/component/E2E coverage; only typecheck and production build passed.
''')
    write("STATIC_FINDINGS_INDEX.md", "# Static findings index\n\n| ID | Severity | Priority | Status | Title |\n|---|---|---|---|---|\n" + "\n".join(f"| [{f['id']}](findings/{f['id']}.md) | {f['severity']} | {f['priority']} | {f['status']} | {f['title']} |" for f in FINDINGS) + f"\n\nCounts by severity: {dict(severity)}. Counts by status: {dict(statuses)}.")
    write("SPEC_GAPS.md", '''# Specification gaps

1. Whether document/archive/watch mutations require review approval; current default-safe behavior conflicts with file.write.
2. Realtime event transport is simultaneously promised, disabled and presented as waiting.
3. Cached offline start versus clean rebuild/download guarantees and image pull policy.
4. Delete conversation versus erasure of learning/audit copies; global retention/purge policy.
5. Replay/idempotency horizon after bounded outcome eviction.
6. Detailed `/health` exposure for non-loopback deployments.
7. TLS exception policy and shared untrusted-source synthesis rules.
8. Environment precedence across launcher, shell/.env, Compose and backend defaults (including 120s/600s learning interval).
9. What constitutes a compatible/complete custom model and rollback semantics.
10. Active versus legacy `requirements-surfer.txt`, mission handler, localStorage keys and Qwen env names.
''')
    write("TEST_GAPS.md", '''# Test gaps

P0/P1 gaps: DB/vault transaction fault injection; approval audit failure and creation redaction; concurrent jobs and same-conversation requests; transport retry; replay eviction; launcher ownership/process scope; model-switch rollback; Compose health/token; browser redirect/subresource/env/sandbox; symlink ingest; hostile regex; archive failure atomicity.

P2 gaps: real stream disconnect/marker cleanup; concurrent WebSocket ordering/replay; frontend stale/error/cancel/service-worker/accessibility; malformed cadence/deadline; web-watch outbox; storage migration/integrity/retention/restore; document compressed-member/memory bounds.

CI gaps: no Linux/Python 3.12 job, Compose render/build, PowerShell behavioral harness, frontend tests/lint, coverage threshold, package build, lock-frozen Python install or deterministic offline gate.
''')
    write("ARCHITECTURE_RISKS.md", '''# Architecture risks

- `tools.py` (~15k lines), `page.tsx` (~5.8k) and ad-hoc storage commits concentrate unrelated behavior and make policy consistency hard.
- Core HTTP, Playwright browser, CDP and bundled synthesis have divergent trust policies.
- Main SQLite, vault, audit/event bus and file outputs lack a general transactional outbox/unit-of-work abstraction.
- Whole-array runtime KV and append-only histories create concurrency and growth hazards.
- Launcher cleanup/state owns cross-process lifecycle through heuristics rather than one supervisor identity model.
- Runtime Linux/browser/process hardening is not exercised by current Windows-only backend CI.
- Sync SQLite/filesystem/regex operations inside async flows can create loop lag under slow disk or hostile inputs.
''')
    write("LIVE_AUDIT_PLAN.md", f'''# PHASE B live audit plan

Execute `LIVE_SCENARIO_QUEUE.csv` in order after source drift check. There are {len(LIVE)} safe functional scenarios. Use `D:\\jarvis-gpt` for source and only copied/synthetic roots under an audit temp subtree of `D:\\jarvis`; preserve the real DB/models/user files. Use alternate Compose project/ports where possible, pre-record process/container/birth identities, capture UTC logs and always perform cleanup verification.

P0 items validate ownership, startup/token/health, model rollback, idempotency, approval atomicity/secrecy, DB transactions, job races, replay eviction, regex watchdog, public-only browser policy, worker isolation, ingest boundary and tool approval.

Never perform active attacks or contact external targets. Redirect/TLS/private-destination checks use local mocked resolvers/routes and synthetic fixtures only.
''')
    write("SOURCE_DRIFT_POLICY.md", f'''# Source drift policy

Baseline production commit: `{SOURCE_COMMIT}`.

PHASE B compares the current production tree to that commit while excluding `.audit/**` and `docs/audit/**`. If any other path differs, create `SOURCE_DRIFT.md` with paths/commits, reread each affected file, rerun relevant static/live checks and mark prior conclusions pending. Do not transfer findings or candidate tasks automatically across drift.
''')
    write("handoff/PHASE_B_START_HERE.md", f'''# PHASE B start here

Run path: `.audit/runs/{RUN_ID}`. Source: `{SOURCE_COMMIT}` (`{SOURCE_BRANCH}`).

PHASE A indexed 167 files, 254 public features and 22 requirements; collected 830 tests; passed compile/lint, 394 safe backend tests, frontend typecheck/build and schema/config checks; measured 50.37% restricted backend coverage; produced {len(FINDINGS)} findings and {len(LIVE)} live scenarios. Two failures are hermetic: JARVIS-0012 archive partial output and JARVIS-0017 document name collision.

Start with SCN-LIVE-001 source drift, then P0 in queue order. Prerequisites: installed product, safe access to Windows/Docker/WSL/GPU/local models, backups, copied/synthetic runtime roots, alternate ports/project names and a cleanup ledger. Real user data/secrets and external targets are forbidden.

Update scenario status/evidence, finding status/confidence, PIPELINE_STATE phase_b and final candidate-task disposition. Preserve PHASE A raw evidence and source baseline. Rerun traceability consistency after every material update.

Completion requires all applicable profiles/states, error/recovery/concurrency/storage/GUI/offline cases, source drift handling, evidence-backed confirm/refute decisions and a final atomic remediation queue. **Do not create Spark READY markers until PHASE B is complete.**
''')

    manifest = []
    for path in sorted(EVIDENCE.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            manifest.append({"path": str(path.relative_to(REPO)), "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    (RUN / "EVIDENCE_MANIFEST.json").write_text(json.dumps({"schema_version": 1, "run_id": RUN_ID, "items": manifest}, indent=2) + "\n", encoding="utf-8")

    state_path = RUN / "PIPELINE_STATE.json"
    state = json.loads(state_path.read_text())
    state["phase_a"]["status"] = "COMPLETE_WITH_BLOCKERS"
    state["phase_a"]["completed_utc"] = "2026-07-13T00:34:00Z"
    state["phase_a"]["counts"] = {"features": len(features), "requirements": len(requirements), "tests_collected": 830, "static_command_records": len(TESTS), "findings": len(FINDINGS), "live_scenarios": len(LIVE)}
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    journal = RUN / "AUDIT_JOURNAL.md"
    with journal.open("a", encoding="utf-8") as handle:
        handle.write("- `2026-07-13T00:34Z`: inventory/architecture/backend/frontend/web/document/defensive/test reviews reconciled; artifacts rendered; PHASE A set COMPLETE_WITH_BLOCKERS pending final consistency and Git checks.\n")

    print(json.dumps({"features": len(features), "requirements": len(requirements), "findings": len(FINDINGS), "live_scenarios": len(LIVE), "evidence_files": len(manifest)}, indent=2))


if __name__ == "__main__":
    main()
