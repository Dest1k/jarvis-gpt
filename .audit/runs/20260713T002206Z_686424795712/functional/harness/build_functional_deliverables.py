#!/usr/bin/env python3
"""Build final functional acceptance tables, reports, findings, and Spark queue."""

from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


RUN_ID = "20260713T002206Z_686424795712"
RUN_PATH = f".audit/runs/{RUN_ID}/functional"
TESTED_HEAD = "3fda655e4f723a0d8f58a4edfb4b3ee7dda079fe"
DIMENSIONS = (
    "intent_fidelity",
    "task_completion",
    "constraint_adherence",
    "truthfulness",
    "response_integrity",
    "state_consistency",
    "recovery_quality",
    "ux_clarity",
)


TECHNICAL = {
    "FUNC-0069": (
        "FAIL",
        "evidence/isolated-launch-turbo-process-v2.json;evidence/mono-startup-timeout-20m.json;evidence/mono-ready-after-27m.json",
        "Turbo reached ready; mono crossed the bounded 20-minute deadline and became usable only after extended two-stage autotuning.",
        "FUNC-FIND-013",
    ),
    "FUNC-0070": (
        "FAIL",
        "evidence/startup-gemma4-turbo-warm.json;evidence/startup-gemma4-turbo-warm-r2.json;evidence/startup-gemma4-turbo-warm-r3.json",
        "Three repeated starts exited nonzero with an executive-state lease error instead of an idempotent running result.",
        "FUNC-FIND-014",
    ),
    "FUNC-0071": (
        "INCONCLUSIVE",
        "evidence/stop-default-stack.json;evidence/status-turbo-final.json;evidence/stop-before-startup-faults.json;evidence/startup-fault-fixtures.json",
        "Owned stop/restart and cleanup worked; a deliberately unowned but compatible dispatcher reuse fixture was not run.",
        "",
    ),
    "FUNC-0072": (
        "PASS",
        "evidence/startup-fault-fixtures.json",
        "Owned port holder survived rejection; interrupted launcher was terminated by owned PID tree and standard cleanup closed all four ports.",
        "",
    ),
    "FUNC-0073": (
        "BLOCKED_BY_SPEC",
        "evidence/startup-unknown-profile.json",
        "Unknown profile rejection is actionable; no user-supplied runtime config surface exists for a separate malformed-config fixture.",
        "",
    ),
    "FUNC-0074": (
        "FAIL",
        "evidence/doctor-full-final-sanitized-v2.json;evidence/doctor-failure-targeted-clean-env.json",
        "Full doctor had 816 passed, 13 skipped, 1 failed, yet the launcher returned zero; targeted clean-env rerun passed.",
        "FUNC-FIND-016;FUNC-FIND-017",
    ),
    "FUNC-0075": (
        "INCONCLUSIVE",
        "evidence/jarvis-functional-turbo-v2-20260713T163821Z-88fd91140364.csv;evidence/technical-mono-ready.json",
        "Authoritative turbo REST contract passed; equivalent steady-state model-backed validation was not completed for both degraded mono profiles.",
        "",
    ),
    "FUNC-0076": (
        "INCONCLUSIVE",
        "evidence/jarvis-functional-turbo-v2-20260713T163821Z-88fd91140364.csv;evidence/gui_operator_runs.jsonl",
        "Normal NDJSON and retry paths passed, but true transport cancellation/late-terminal behavior remained incomplete.",
        "FUNC-FIND-011",
    ),
    "FUNC-0077": (
        "INCONCLUSIVE",
        "evidence/jarvis-functional-turbo-v2-20260713T163821Z-88fd91140364.csv;evidence/gui_operator_runs.jsonl",
        "One WebSocket event and hostile-origin denial passed; two-session reconnect interleaving was not fully exercised.",
        "",
    ),
    "FUNC-0078": (
        "FAIL",
        "evidence/mono-perf-direct-model-probes.json;evidence/mono-direct-model-probes-ready.json;evidence/gui_operator_runs.jsonl",
        "Configured identities matched, but both 31B profiles generated repeated 'cyclic' output and GUI paths timed out or fell back.",
        "FUNC-FIND-013",
    ),
    "FUNC-0079": (
        "BLOCKED_BY_ENV",
        "evidence/gui_operator_runs.jsonl",
        "Loopback-refused recovery surrogate passed; scoped external-link isolation was unavailable without unsafe global network mutation.",
        "",
    ),
    "FUNC-0080": (
        "INCONCLUSIVE",
        "evidence/technical-during-mono-startup.json;evidence/technical-mono-ready.json",
        "Backup, integrity, restored-copy equality, DB lock, read-only, and temp failure probes passed; pending mission restart/idempotency remained incomplete.",
        "",
    ),
    "FUNC-0081": (
        "INCONCLUSIVE",
        "evidence/technical-during-mono-startup.json;evidence/technical-mono-ready.json;evidence/jarvis-functional-turbo-v2-20260713T163821Z-88fd91140364.csv",
        "Two 15/16 read-oriented matrices passed with diag correctly lease-guarded; exact host-bridge/file-search coverage across all profiles was incomplete.",
        "",
    ),
    "FUNC-0082": (
        "PASS",
        "evidence/guarded-cli-matrix.json",
        "Four mutation commands were rejected by the API ownership lease and persona, approvals, policy, and filesystem state stayed unchanged.",
        "",
    ),
    "FUNC-0083": (
        "FAIL",
        "evidence/mono-perf-direct-model-probes.json;evidence/mono-direct-model-probes-ready.json;evidence/mono-startup-timeout-20m.json;evidence/bounded-soak-turbo.json",
        "Turbo remained interactive; mono startup and 0.1-0.4 tok/s generation were practically unusable and both 31B profiles emitted degenerate output.",
        "FUNC-FIND-013",
    ),
    "FUNC-0084": (
        "INCONCLUSIVE",
        "evidence/bounded-soak-turbo.json;evidence/gui_operator_runs.jsonl",
        "120-second watchdog soak produced 19/19 healthy samples and 4/4 chats, but a successful long-running mission could not be demonstrated.",
        "FUNC-FIND-007",
    ),
    "FUNC-0085": (
        "FAIL",
        "evidence/gui-stale-state-after-home-switch.json;evidence/backend-conversations-after-home-switch-v2.json;evidence/frontend-freshness.json;evidence/gui-final-keyboard-focus.json;evidence/gui-resize-zoom.json",
        "Fresh assets and keyboard submit passed, but the GUI retained an old transcript when the backend home changed and reported zero conversations.",
        "FUNC-FIND-015",
    ),
    "FUNC-0086": (
        "PASS",
        "evidence/final-normal-start.json;evidence/final-normal-smoke.json;evidence/final-normal-gui-smoke.json;evidence/final-cleanup.json;evidence/final-cleanup-inventory-v2.json;evidence/final-machine-baseline-restored.json",
        "Post-fault standard start, API/UI smoke, standard stop, final owned process/port inventory, and restoration of the initial offline Docker/WSL baseline all completed.",
        "",
    ),
}


CASE_FINDINGS = {
    "OP-0006": "FUNC-FIND-001",
    "OP-0007": "FUNC-FIND-002",
    "OP-0010": "FUNC-FIND-002",
    "OP-0013": "FUNC-FIND-003",
    "OP-0014": "FUNC-FIND-004",
    "OP-0016": "FUNC-FIND-004;FUNC-FIND-007",
    "OP-0023": "FUNC-FIND-005",
    "OP-0024": "FUNC-FIND-002",
    "OP-0025": "FUNC-FIND-006",
    "OP-0026": "FUNC-FIND-007",
    "OP-0028": "FUNC-FIND-006",
    "OP-0029": "FUNC-FIND-003;FUNC-FIND-006",
    "OP-0030": "FUNC-FIND-003;FUNC-FIND-006",
    "OP-0031": "FUNC-FIND-003",
    "OP-0032": "FUNC-FIND-004;FUNC-FIND-007",
    "OP-0033": "FUNC-FIND-008",
    "OP-0034": "FUNC-FIND-003;FUNC-FIND-006",
    "OP-0036": "FUNC-FIND-006",
    "OP-0037": "FUNC-FIND-009",
    "OP-0038": "FUNC-FIND-010",
    "OP-0039": "FUNC-FIND-007",
    "OP-0040": "FUNC-FIND-011",
    "OP-0041": "FUNC-FIND-012",
    "OP-0044": "FUNC-FIND-006",
    "OP-0050": "FUNC-FIND-007;FUNC-FIND-013",
}


FINDINGS = [
    {
        "id": "FUNC-FIND-001", "category": "RESULT_NOT_USEFUL", "priority": "P2",
        "title": "Direct DNS question is misrouted to shopping",
        "cases": "OP-0006 repeats 1-2", "profiles": "gemma4-turbo", "surfaces": "GUI/Dialog",
        "request": "Resolve a public hostname and return exactly one sentence.",
        "response": "Shopping/catalog workflow ran and did not answer the DNS request.",
        "expected": "A direct DNS result or an exact actionable network error.",
        "evidence": "evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv",
        "hypothesis": "Intent classification overweights shopping/network catalog terms.",
        "acceptance": "Both deterministic repeats route to DNS/network lookup and return one factual sentence without shopping output.",
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/shop.py; backend/tests",
    },
    {
        "id": "FUNC-FIND-002", "category": "FORMAT_BREACH", "priority": "P2",
        "title": "Exact response constraints are not consistently enforced",
        "cases": "OP-0007, OP-0010, OP-0024", "profiles": "gemma4-turbo", "surfaces": "GUI/Dialog",
        "request": "Return exact count/JSON/default-assumption formats.",
        "response": "Rendered output violated requested count, schema, or assumption contract.",
        "expected": "Deterministically valid output matching the explicit constraint.",
        "evidence": "evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_PASS_1.csv; evidence/SEMANTIC_REVIEW_PASS_2.csv",
        "hypothesis": "Final answer validation does not cover ordinary non-tool format contracts.",
        "acceptance": "All listed cases pass their deterministic parser/count validators in three consecutive runs.",
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/verification.py; backend/tests",
    },
    {
        "id": "FUNC-FIND-003", "category": "CLAIMED_ARTIFACT_MISSING", "priority": "P1",
        "title": "Artifact generation ignores exact paths or returns incomplete transforms",
        "cases": "OP-0013, OP-0029..OP-0031, OP-0034", "profiles": "gemma4-turbo", "surfaces": "GUI/Documents/filesystem",
        "request": "Create or transform a controlled artifact at the exact requested destination.",
        "response": "Wrong parent path, raw pseudo-tool output, missing repeats, or literal Markdown in DOCX.",
        "expected": "Exact path/type, source preservation, distinct concurrent outputs, and valid native document structure.",
        "evidence": "evidence/turbo-artifact-validation.json; evidence/TURBO_ARTIFACT_VALIDATION.md; evidence/gui_operator_runs.jsonl",
        "hypothesis": "Artifact intent, path binding, and post-write verification are not one atomic contract.",
        "acceptance": "Exact-path, copy-only, conversion, and three-way collision tests produce validated artifacts with unchanged sources.",
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/tools.py; backend/src/jarvis_gpt/documents.py; backend/tests",
    },
    {
        "id": "FUNC-FIND-004", "category": "CONTEXT_LOSS", "priority": "P2",
        "title": "Multi-turn references to prior options/files are lost",
        "cases": "OP-0014, OP-0016, OP-0032", "profiles": "gemma4-turbo", "surfaces": "GUI/Dialog/Documents",
        "request": "Apply a short pronoun or prior-file follow-up inside one conversation.",
        "response": "The selected option or earlier uploaded file was not reliably resolved.",
        "expected": "Only the current conversation's referenced object is used.",
        "evidence": "evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv",
        "hypothesis": "Conversation grounding and file-reference resolution use inconsistent state sources.",
        "acceptance": "All reference cases resolve the exact prior object across three deterministic repeats without cross-window state.",
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/storage.py; backend/src/jarvis_gpt/files.py; backend/tests",
    },
    {
        "id": "FUNC-FIND-005", "category": "UNNECESSARY_CLARIFICATION", "priority": "P2",
        "title": "Ambiguous request creates a mission instead of one precise question",
        "cases": "OP-0023 repeats 1-2", "profiles": "gemma4-turbo", "surfaces": "GUI/Dialog/Missions",
        "request": "Ask exactly one question before creating the requested report.",
        "response": "A mission plan was created before the ambiguity was resolved.",
        "expected": "One concise question and no artifact or mission until answered.",
        "evidence": "evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv",
        "hypothesis": "Mission routing precedes clarification gating.",
        "acceptance": "No mission/artifact is created before one exact clarification; the follow-up resumes the original goal.",
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/executive.py; backend/tests",
    },
    {
        "id": "FUNC-FIND-006", "category": "INTERNAL_OUTPUT_LEAK", "priority": "P1",
        "title": "Raw tool-call envelopes reach rendered assistant output",
        "cases": "OP-0025, OP-0028..OP-0030, OP-0034, OP-0036, OP-0044", "profiles": "gemma4-turbo", "surfaces": "GUI/API stream",
        "request": "Perform document/runtime tasks and return a normal user answer.",
        "response": "Raw call:documents, call:llm.health, call:dispatcher.status, or JSON tool payloads were rendered.",
        "expected": "Only validated tool results and one natural final answer are visible.",
        "evidence": "evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv",
        "hypothesis": "Tool-shaped output bypasses the final response integrity classifier on some routes.",
        "acceptance": "Known marker scan finds zero tool envelopes in DOM, NDJSON deltas, and terminal answers over all affected cases.",
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/llm.py; backend/src/jarvis_gpt/api.py; backend/tests; frontend/app/page.tsx",
    },
    {
        "id": "FUNC-FIND-007", "category": "RESULT_NOT_USEFUL", "priority": "P1",
        "title": "Uploaded document recall is unreliable and blocks missions",
        "cases": "OP-0016, OP-0026, OP-0032, OP-0039, OP-0050", "profiles": "gemma4-turbo;gemma4-mono-perf", "surfaces": "GUI/Documents/Missions",
        "request": "Recall an uploaded controlled document, extract an exact fact, or compare mission inputs.",
        "response": "Existing documents were reported missing or the mission stopped at recall.",
        "expected": "Exact controlled fact/source IDs and a completed read-only mission report.",
        "evidence": "evidence/document-upload-results.json; evidence/profile-fixture-upload-results.json; evidence/gui_operator_runs.jsonl",
        "hypothesis": "Filename/source-ID lookup differs between upload, conversation, and agent document routes.",
        "acceptance": "Fresh and previously uploaded files resolve by exact name/ID and complete fact, comparison, retry, and mission cases.",
        "allowed": "backend/src/jarvis_gpt/files.py; backend/src/jarvis_gpt/storage.py; backend/src/jarvis_gpt/agent.py; backend/tests",
    },
    {
        "id": "FUNC-FIND-008", "category": "ERROR_NOT_ACTIONABLE", "priority": "P2",
        "title": "Corrupt document recovery is inconsistent",
        "cases": "OP-0033 repeat 3", "profiles": "gemma4-turbo", "surfaces": "GUI/Documents",
        "request": "Open a corrupt file, report the failure, then retry with a valid replacement.",
        "response": "At least one repeat did not provide a clean actionable error/retry result.",
        "expected": "No false success or stale partial output; valid replacement succeeds.",
        "evidence": "evidence/gui_operator_runs.jsonl; evidence/DOCUMENT_FIXTURE_QA.md",
        "hypothesis": "Parser failure and retry state are not consistently normalized.",
        "acceptance": "Three corrupt-to-valid retries show one actionable error followed by one clean result with no stale content.",
        "allowed": "backend/src/jarvis_gpt/documents.py; backend/src/jarvis_gpt/files.py; backend/tests",
    },
    {
        "id": "FUNC-FIND-009", "category": "TOOL_STATE_MISMATCH", "priority": "P1",
        "title": "Approved safe action uses a non-canonical tool schema",
        "cases": "OP-0037 repeats 1-3", "profiles": "gemma4-turbo", "surfaces": "GUI/Approvals/Tools",
        "request": "Create one controlled directory through the exact approval flow.",
        "response": "Approval execution referenced filesystem.mkdir while the allowed action is fs.mkdir; no directory was created.",
        "expected": "One pending approval, exact operator approval, one execution, and verified state change.",
        "evidence": "evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_PASS_1.csv; evidence/SEMANTIC_REVIEW_PASS_2.csv",
        "hypothesis": "Model-facing aliases are not canonicalized before approval schema validation.",
        "acceptance": "Three end-to-end approvals bind the canonical action and create only the approved path exactly once.",
        "allowed": "backend/src/jarvis_gpt/approvals.py; backend/src/jarvis_gpt/tools.py; backend/src/jarvis_gpt/agent.py; backend/tests",
    },
    {
        "id": "FUNC-FIND-010", "category": "RESULT_NOT_USEFUL", "priority": "P2",
        "title": "Web synthesis does not reliably return a cited usable result",
        "cases": "OP-0038 repeats 1-3", "profiles": "gemma4-turbo", "surfaces": "GUI/Internet",
        "request": "Synthesize a public result with reachable citations.",
        "response": "The requested cited synthesis failed the usability/evidence rubric.",
        "expected": "A concise factual synthesis with direct citations or an exact blocker.",
        "evidence": "evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv",
        "hypothesis": "Web result grounding is not enforced at final-answer validation.",
        "acceptance": "Three runs contain supported claims with direct URLs, or one precise actionable unavailability message.",
        "allowed": "backend/src/jarvis_gpt/web_surfer.py; backend/src/jarvis_gpt/agent.py; backend/tests",
    },
    {
        "id": "FUNC-FIND-011", "category": "STATE_RECOVERY_FAILURE", "priority": "P1",
        "title": "Interrupted GUI stream can leave an empty stale assistant bubble",
        "cases": "OP-0040 repeats 1-3", "profiles": "gemma4-turbo", "surfaces": "GUI/stream/reconnect",
        "request": "Interrupt navigation during a stream, reconnect, and retry.",
        "response": "Observed runs included an empty 0 ms assistant bubble before retry.",
        "expected": "Cancelled partial state is removed or labelled; retry produces one terminal answer.",
        "evidence": "evidence/gui_operator_runs.jsonl; evidence/SEMANTIC_REVIEW_RECONCILIATION.csv",
        "hypothesis": "Frontend stream teardown commits a placeholder before terminal reconciliation.",
        "acceptance": "Navigation interruption and retry yield no empty/duplicate/stale final in three repeats and persisted history matches DOM.",
        "allowed": "frontend/app/page.tsx; backend/src/jarvis_gpt/api.py; backend/tests; frontend tests",
    },
    {
        "id": "FUNC-FIND-012", "category": "STATE_RECOVERY_FAILURE", "priority": "P2",
        "title": "Requested memory namespace is ignored",
        "cases": "OP-0041 repeats 1-2", "profiles": "gemma4-turbo", "surfaces": "GUI/Memory/API",
        "request": "Store a controlled marker only in audit.functional.20260713.",
        "response": "API records used namespace operator and persisted the marker/persona instruction there.",
        "expected": "Exact requested namespace, isolated recall, and no persona bleed.",
        "evidence": "evidence/gui_operator_runs.jsonl; evidence/api-baseline-turbo.json",
        "hypothesis": "Persona/memory write route applies a hard-coded default namespace.",
        "acceptance": "Writes and recall use the requested namespace exactly and operator/default namespaces remain unchanged.",
        "allowed": "backend/src/jarvis_gpt/memory.py; backend/src/jarvis_gpt/persona.py; backend/src/jarvis_gpt/api.py; backend/tests",
    },
    {
        "id": "FUNC-FIND-013", "category": "PROFILE_MISMATCH", "priority": "P1",
        "title": "Both 31B profiles are functionally unusable on the certified machine",
        "cases": "OP-0045..OP-0068; FUNC-0069; FUNC-0078; FUNC-0083", "profiles": "gemma4-mono-perf;gemma4-mono", "surfaces": "Launcher/provider/API/GUI",
        "request": "Run ordinary profile-specific answers, tools, documents, missions, and web tasks.",
        "response": "Direct probes emitted repeated 'cyclic'; GUI returned 400 fallbacks or timed out; mono startup crossed 20 minutes and decoded near 0.1-0.4 tok/s.",
        "expected": "Correct bounded answer, exact profile identity, and practically usable readiness/latency.",
        "evidence": "evidence/mono-perf-direct-model-probes.json; evidence/mono-direct-model-probes-ready.json; evidence/mono-startup-timeout-20m.json; evidence/PROFILE_SEMANTIC_REVIEW_RECONCILIATION.csv",
        "hypothesis": "31B NVFP4 runtime/profile parameters are incompatible with model quality and practical context/latency on this host.",
        "acceptance": "Three direct probes per profile answer correctly; all profile gate cases complete without fallback; startup and latency meet an explicit contract.",
        "allowed": "backend/src/jarvis_gpt/config.py; backend/src/jarvis_gpt/model_catalog.py; docker-compose.yml; scripts/jarvis-launcher.ps1; backend/tests",
    },
    {
        "id": "FUNC-FIND-014", "category": "STARTUP_FAILURE", "priority": "P2",
        "title": "Repeated start is not idempotent",
        "cases": "FUNC-0070, three warm repeats", "profiles": "gemma4-turbo", "surfaces": "Launcher/CLI",
        "request": "Start an already running owned stack.",
        "response": "All three repeats exited 1 with the API executive-state lease message.",
        "expected": "Idempotent success/no-op with truthful already-running status, or an explicit documented contract.",
        "evidence": "evidence/startup-gemma4-turbo-warm.json; evidence/startup-gemma4-turbo-warm-r2.json; evidence/startup-gemma4-turbo-warm-r3.json",
        "hypothesis": "Launcher runs a mutating CLI verification after the API acquires the lease.",
        "acceptance": "Three repeat starts return zero, preserve PIDs/container identity, and report already running without lease errors.",
        "allowed": "scripts/jarvis-launcher.ps1; backend/src/jarvis_gpt/runtime_lease.py; backend/tests",
    },
    {
        "id": "FUNC-FIND-015", "category": "CROSS_SESSION_MIX", "priority": "P1",
        "title": "GUI transcript survives a runtime-home identity change",
        "cases": "FUNC-0085 preflight", "profiles": "gemma4-turbo", "surfaces": "GUI/history/runtime switch",
        "request": "Open the new isolated runtime home with zero backend conversations.",
        "response": "The GUI retained a prior transcript while authoritative backend conversation count was zero.",
        "expected": "Client state is cleared or keyed by backend home/runtime identity.",
        "evidence": "evidence/gui-stale-state-after-home-switch.json; evidence/backend-conversations-after-home-switch-v2.json",
        "hypothesis": "Browser-local chat state is not namespaced by runtime home or backend identity.",
        "acceptance": "Three old-home to new-home switches show zero old messages and DOM/history equal the new backend state.",
        "allowed": "frontend/app/page.tsx; frontend tests",
    },
    {
        "id": "FUNC-FIND-016", "category": "FALSE_SUCCESS", "priority": "P1",
        "title": "Doctor returns success when a required test fails",
        "cases": "FUNC-0074", "profiles": "gemma4-turbo", "surfaces": "jarvis.cmd doctor/scripts/doctor.ps1",
        "request": "Run the full doctor smoke suite.",
        "response": "Smoke JSON reported ok=false with one required failure, while jarvis.cmd doctor returned 0.",
        "expected": "Nonzero exit whenever required_ok is false, with the failing check named.",
        "evidence": "evidence/doctor-full-final-sanitized-v2.json; evidence/doctor-failure-targeted-clean-env.json",
        "hypothesis": "PowerShell doctor wrapper does not propagate smoke.py LASTEXITCODE and launcher injects live JARVIS_HOME into tests.",
        "acceptance": "A forced required failure makes doctor exit nonzero; clean full suite exits zero; tests do not inherit deployment home/profile variables.",
        "allowed": "scripts/doctor.ps1; scripts/smoke.py; scripts/jarvis-launcher.ps1; backend/tests/test_smoke_script.py",
    },
    {
        "id": "FUNC-FIND-017", "category": "INTERNAL_OUTPUT_LEAK", "priority": "P1",
        "title": "Doctor output exposes the runtime API token",
        "cases": "FUNC-0074 docker compose config check", "profiles": "gemma4-turbo", "surfaces": "Doctor/CLI logs",
        "request": "Run diagnostics without exposing secrets.",
        "response": "Compose stdout included JARVIS_API_TOKEN; committed evidence uses a redacted derivative only.",
        "expected": "Tokens and authorization values are redacted before console/log/report output.",
        "evidence": "evidence/doctor-full-final-sanitized-v2.json",
        "hypothesis": "Smoke captures raw docker compose config output without secret filtering.",
        "acceptance": "Canary credentials never appear in doctor stdout/stderr/JSON or persisted logs; regression scan passes.",
        "allowed": "scripts/smoke.py; scripts/doctor.ps1; backend/src/jarvis_gpt/redaction.py; backend/tests",
    },
]


TASK_DETAILS = {
    "FUNC-FIND-001": {
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/tools.py; backend/src/jarvis_gpt/shop_registry.py; backend/tests/test_shop_routing.py; backend/tests/test_tools.py",
        "reproduction": "Start the isolated turbo profile and submit OP-0006 exactly twice in separate new chats. Verify the trace route is shopping/catalog and neither final contains the DNS answer.",
        "test": "Add `test_dns_question_does_not_route_to_shop` to `backend/tests/test_shop_routing.py`; assert the selected intent is DNS/network and no shop tool is present. Run `py -3.11 -m pytest backend/tests/test_shop_routing.py backend/tests/test_tools.py -q`.",
    },
    "FUNC-FIND-002": {
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/verification.py; backend/tests/test_agent.py; backend/tests/test_verification.py",
        "reproduction": "Replay OP-0007, OP-0010, and OP-0024 from `OPERATOR_TASK_CATALOG.csv`; apply the exact bullet-count, JSON parse/schema, and assumption validators recorded in the catalog.",
        "test": "Add parameterized exact-format cases to `backend/tests/test_verification.py` and agent finalization coverage to `backend/tests/test_agent.py`. Run `py -3.11 -m pytest backend/tests/test_verification.py backend/tests/test_agent.py -q`.",
    },
    "FUNC-FIND-003": {
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/tools.py; backend/src/jarvis_gpt/document_agent.py; backend/src/jarvis_gpt/document_runtime.py; backend/tests/test_document_runtime.py; backend/tests/test_tools.py",
        "reproduction": "In a temporary directory, replay OP-0013 and OP-0029..OP-0031, then launch the three OP-0034 windows. Compare exact destination paths, source hashes, artifact hashes, DOCX ZIP/XML structure, and collision-free filenames.",
        "test": "Extend `backend/tests/test_document_runtime.py` with exact-destination, copy-only, Markdown-to-DOCX, and concurrent-name cases; assert source hashes remain unchanged. Run `py -3.11 -m pytest backend/tests/test_document_runtime.py backend/tests/test_tools.py -q`.",
    },
    "FUNC-FIND-004": {
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/storage.py; backend/src/jarvis_gpt/document_memory.py; backend/tests/test_agent.py; backend/tests/test_document_memory.py",
        "reproduction": "Create one conversation per OP-0014, OP-0016, and OP-0032, complete the first turn, then send the catalog follow-up without restating the object. Record selected option/file ID and final answer.",
        "test": "Add pronoun, selected-option, and prior-file-ID tests to `backend/tests/test_agent.py` and `backend/tests/test_document_memory.py`; assert only the conversation-local object resolves. Run both files with pytest.",
    },
    "FUNC-FIND-005": {
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/executive_planner.py; backend/tests/test_agent.py; backend/tests/test_executive_planner.py",
        "reproduction": "Submit OP-0023 in a clean conversation and inspect conversations, missions, and files before answering. The pre-fix run creates a mission instead of returning exactly one question.",
        "test": "Add `test_ambiguity_blocks_mission_until_one_clarification` to `backend/tests/test_agent.py`; assert zero mission/artifact writes before the answer and correct resume afterward. Run agent and executive-planner tests.",
    },
    "FUNC-FIND-006": {
        "allowed": "backend/src/jarvis_gpt/agent.py; backend/src/jarvis_gpt/llm.py; backend/src/jarvis_gpt/api.py; frontend/app/page.tsx; backend/tests/test_agent.py; backend/tests/test_llm.py; backend/tests/test_api_smoke.py",
        "reproduction": "Replay OP-0025, OP-0028, OP-0036, and OP-0044; scan every NDJSON delta, terminal answer, and rendered DOM string for `call:`, tool JSON keys, roles, tracebacks, and internal schemas.",
        "test": "Add tool-shaped output fixtures to `backend/tests/test_llm.py` and stream/terminal assertions to `backend/tests/test_agent.py` and `test_api_smoke.py`. Run those three files and require zero marker matches.",
    },
    "FUNC-FIND-007": {
        "allowed": "backend/src/jarvis_gpt/document_memory.py; backend/src/jarvis_gpt/document_runtime.py; backend/src/jarvis_gpt/storage.py; backend/src/jarvis_gpt/agent.py; backend/tests/test_document_memory.py; backend/tests/test_document_runtime.py; backend/tests/test_agent.py",
        "reproduction": "Upload the controlled fixture once, retain its returned ID, then replay OP-0026, OP-0032, OP-0039, and OP-0050 by exact name and ID. Compare upload storage, conversation references, retrieval result, and mission final state.",
        "test": "Add fresh/prior-upload lookup and mission document-binding cases to document-memory/runtime tests; assert exact source ID, controlled token, and completed report. Run the three listed test files.",
    },
    "FUNC-FIND-008": {
        "allowed": "backend/src/jarvis_gpt/document_runtime.py; backend/src/jarvis_gpt/file_types.py; backend/tests/test_document_runtime.py; backend/tests/test_file_types_and_archives.py",
        "reproduction": "Replay OP-0033 three times using `corrupt-{repeat}.pdf`, capture the first error, attach the corresponding valid replacement, and compare the retry final against stale partial output.",
        "test": "Add corrupt-to-valid retry cases to `backend/tests/test_document_runtime.py`; assert one normalized actionable error, no persisted partial result, and one clean retry. Run document-runtime and file-type tests.",
    },
    "FUNC-FIND-009": {
        "allowed": "backend/src/jarvis_gpt/approval_executor.py; backend/src/jarvis_gpt/tools.py; backend/src/jarvis_gpt/agent.py; backend/tests/test_approval_executor.py; backend/tests/test_tools.py; backend/tests/test_agent.py",
        "reproduction": "With a temporary target path, submit OP-0037, inspect the pending action name/payload, approve it once, execute it once, and verify the directory plus approval/tool audit records.",
        "test": "Add alias-to-canonical `fs.mkdir` approval tests to `backend/tests/test_approval_executor.py` and agent/tool coverage; reject unknown aliases before creating a pending approval. Run the three listed test files.",
    },
    "FUNC-FIND-010": {
        "allowed": "backend/src/jarvis_gpt/web_surfer.py; backend/src/jarvis_gpt/web_orchestrator.py; backend/src/jarvis_gpt/agent.py; backend/tests/test_web_surfer_integration.py; backend/tests/test_web_orchestrator.py; backend/tests/test_agent.py",
        "reproduction": "Replay OP-0038 three times against the controlled public query; validate every cited URL is present, reachable, and supports its adjacent claim, or that one precise unavailability blocker is returned.",
        "test": "Add deterministic cited-result and unavailable-adapter fixtures to web-surfer/orchestrator tests plus final-answer assertions in `test_agent.py`. Run the three listed test files.",
    },
    "FUNC-FIND-011": {
        "allowed": "frontend/app/page.tsx; frontend/package.json; backend/src/jarvis_gpt/api.py; backend/tests/test_api_smoke.py",
        "reproduction": "Start OP-0040, navigate to `about:blank` before terminal state, return to the Command Center, and retry. Compare DOM bubbles with persisted messages and require no empty 0 ms assistant entry.",
        "test": "Add an API stream disconnect/retry assertion to `backend/tests/test_api_smoke.py` and the smallest frontend test foundation in `frontend/package.json` covering placeholder rollback and terminal deduplication. Run the API test and frontend typecheck/test command.",
    },
    "FUNC-FIND-012": {
        "allowed": "backend/src/jarvis_gpt/cognitive_memory.py; backend/src/jarvis_gpt/persona.py; backend/src/jarvis_gpt/api.py; backend/tests/test_cognitive_memory.py; backend/tests/test_persona.py; backend/tests/test_api_smoke.py",
        "reproduction": "Replay OP-0041 repeats 1-2 with namespace `audit.functional.20260713`; query memory/persona APIs before and after and compare stored namespace plus default `operator` state.",
        "test": "Add explicit-namespace write/recall/isolation cases to `backend/tests/test_cognitive_memory.py` and API/persona tests; assert no default-namespace or persona mutation. Run the three listed test files.",
    },
    "FUNC-FIND-013": {
        "allowed": "backend/src/jarvis_gpt/config.py; backend/src/jarvis_gpt/model_catalog.py; backend/src/jarvis_gpt/dispatcher.py; docker-compose.yml; scripts/jarvis-launcher.ps1; backend/tests/test_config_storage.py; backend/tests/test_model_catalog.py; backend/tests/test_dispatcher.py; backend/tests/test_deployment_contracts.py",
        "reproduction": "Start each 31B profile from stopped state, record `/v1/models`, then issue three temperature-0 prompts asking 2+2 with `max_tokens=16` and run one GUI direct-answer case. Capture readiness, latency, finish reason, and exact output.",
        "test": "Add profile-owned readiness deadline fields and fake-clock launcher/dispatcher tests; add model-output health probes rejecting repeated-token degeneration. Run config, catalog, dispatcher, and deployment-contract tests.",
        "acceptance": "Each profile declares a numeric readiness deadline; fake-clock tests enforce it; three live direct probes answer 4 (not repeated tokens), and a bounded GUI answer completes without fallback before the configured request timeout.",
    },
    "FUNC-FIND-014": {
        "allowed": "scripts/jarvis-launcher.ps1; backend/src/jarvis_gpt/runtime_lease.py; backend/tests/test_runtime_lease.py; backend/tests/test_deployment_contracts.py",
        "reproduction": "Start the owned isolated turbo stack once, capture PIDs/container ID, then invoke the identical start command three times and compare exit codes, identities, and status output.",
        "test": "Add an already-running launcher fixture to `backend/tests/test_deployment_contracts.py`; assert three zero exits, unchanged identities, and no mutating CLI call after lease acquisition. Run deployment and runtime-lease tests.",
    },
    "FUNC-FIND-015": {
        "allowed": "frontend/app/page.tsx; frontend/package.json; backend/src/jarvis_gpt/api.py; backend/tests/test_api_smoke.py",
        "reproduction": "Open a runtime home containing a marked transcript, stop it, start an empty isolated home, reload the same browser tab three times, and compare DOM/history with `GET /api/conversations`.",
        "test": "Add runtime-identity-keyed client-state coverage using the frontend test foundation in `frontend/package.json`; assert a home/profile identity change clears stale messages. Retain an API empty-history assertion in `test_api_smoke.py`.",
    },
    "FUNC-FIND-016": {
        "allowed": "scripts/doctor.ps1; scripts/smoke.py; scripts/jarvis-launcher.ps1; backend/tests/test_smoke_script.py; backend/tests/test_config_storage.py",
        "reproduction": "Run `jarvis.cmd doctor` with the isolated `JARVIS_HOME`; parse the JSON and compare `.ok` with the process exit code. Then run the failing storage test with JARVIS_HOME/PROFILE/MODEL_ROOT removed.",
        "test": "Extend `backend/tests/test_smoke_script.py` to force one required failure and assert nonzero propagation through doctor; assert test subprocesses receive a sanitized environment. Run smoke-script and config-storage tests.",
    },
    "FUNC-FIND-017": {
        "allowed": "scripts/smoke.py; scripts/doctor.ps1; backend/src/jarvis_gpt/redaction.py; backend/tests/test_smoke_script.py; backend/tests/test_redaction.py",
        "reproduction": "Set a canary `JARVIS_API_TOKEN`, run the Compose-config doctor check, and scan stdout, stderr, JSON report, and saved logs for the exact canary. Use only a disposable canary.",
        "test": "Add a canary-token Compose-output fixture to `backend/tests/test_smoke_script.py` and shared redaction assertions to `test_redaction.py`; require the canary absent and `<redacted>` present. Run both files.",
    },
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def pass_status(row: dict[str, str]) -> str:
    return row.get("accepted_status") or row.get("status") or ""


def selected_line(row: dict[str, str]) -> str:
    return row.get("selected_record_line") or row.get("jsonl_line") or row.get("selected_attempt") or ""


def score(row: dict[str, str], dimension: str) -> str:
    raw = row.get(dimension, "")
    return raw if raw in {"0", "1", "2"} else ""


def aggregate_status(statuses: list[str]) -> str:
    for candidate in ("FAIL", "INCONCLUSIVE", "BLOCKED_BY_SAFETY", "BLOCKED_BY_ENV", "BLOCKED_BY_SPEC"):
        if candidate in statuses:
            return candidate
    if statuses and all(item == "NOT_APPLICABLE" for item in statuses):
        return "NOT_APPLICABLE"
    return "PASS" if statuses and all(item == "PASS" for item in statuses) else "INCONCLUSIVE"


def finding_for_case(case_id: str, status: str) -> str:
    if case_id in CASE_FINDINGS:
        return CASE_FINDINGS[case_id]
    if status == "FAIL" and int(case_id.split("-")[1]) >= 45:
        return "FUNC-FIND-013"
    return ""


def build_operator_rows(functional: Path) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    catalog = read_csv(functional / "OPERATOR_TASK_CATALOG.csv")
    catalog_by_id = {row["operator_case_id"]: row for row in catalog}
    pass1 = read_csv(functional / "evidence" / "SEMANTIC_REVIEW_PASS_1.csv") + read_csv(
        functional / "evidence" / "PROFILE_SEMANTIC_REVIEW_PASS_1.csv"
    )
    pass2 = read_csv(functional / "evidence" / "SEMANTIC_REVIEW_PASS_2.csv") + read_csv(
        functional / "evidence" / "PROFILE_SEMANTIC_REVIEW_PASS_2.csv"
    )
    reconciliation = read_csv(functional / "evidence" / "SEMANTIC_REVIEW_RECONCILIATION.csv") + read_csv(
        functional / "evidence" / "PROFILE_SEMANTIC_REVIEW_RECONCILIATION.csv"
    )
    by1 = {(row["operator_case_id"], row["repeat"]): row for row in pass1}
    by2 = {(row["operator_case_id"], row["repeat"]): row for row in pass2}
    accepted = {(row["case_id"], row["repeat"]): row for row in reconciliation}
    expected = {
        (row["operator_case_id"], str(repeat))
        for row in catalog
        for repeat in range(1, int(row["repeat_count"]) + 1)
    }
    if set(by1) != expected or set(by2) != expected or set(accepted) != expected:
        raise RuntimeError("semantic review key sets do not match the 169-key operator catalog")

    rows: list[dict[str, Any]] = []
    per_case: dict[str, list[str]] = defaultdict(list)
    for key in sorted(expected):
        case_id, repeat = key
        catalog_row = catalog_by_id[case_id]
        one = by1[key]
        two = by2[key]
        rec = accepted[key]
        accepted_status = rec["accepted_status"]
        row: dict[str, Any] = {
            "operator_case_id": case_id,
            "scenario_id": catalog_row["scenario_id"],
            "profile": catalog_row["profile"],
            "repeat": repeat,
            "accepted_status": accepted_status,
            "pass_1_status": pass_status(one),
            "pass_2_status": pass_status(two),
            "pass_1_selected_record": selected_line(one),
            "pass_2_selected_record": selected_line(two),
            "finding_ids": finding_for_case(case_id, accepted_status),
            "evidence_refs": "evidence/gui_operator_runs.jsonl;" + (
                "evidence/SEMANTIC_REVIEW_RECONCILIATION.csv"
                if int(case_id.split("-")[1]) <= 44
                else "evidence/PROFILE_SEMANTIC_REVIEW_RECONCILIATION.csv"
            ),
        }
        for dimension in DIMENSIONS:
            first = score(one, dimension)
            second = score(two, dimension)
            numeric = [int(value) for value in (first, second) if value]
            row[f"pass_1_{dimension}"] = first
            row[f"pass_2_{dimension}"] = second
            row[dimension] = min(numeric) if numeric else ""
        rows.append(row)
        per_case[case_id].append(accepted_status)
    return rows, per_case


def finding_markdown(item: dict[str, str]) -> str:
    return f"""# {item['id']} — {item['title']}

- Category: `{item['category']}`
- Priority: `{item['priority']}`
- Affected cases: {item['cases']}
- Profiles: {item['profiles']}
- Surfaces: {item['surfaces']}

## Sanitized reproduction

- Request: {item['request']}
- Observed: {item['response']}
- Expected: {item['expected']}
- Evidence: {item['evidence']}

## Root-cause hypothesis

{item['hypothesis']}

## Binary acceptance criteria

{item['acceptance']}
"""


def spark_task_markdown(index: int, item: dict[str, str]) -> str:
    task_id = f"SPARK-{index:04d}"
    details = TASK_DETAILS[item["id"]]
    acceptance = details.get("acceptance", item["acceptance"])
    return f"""# {task_id} — {item['title']}

- Status: `READY`
- Priority: `{item['priority']}`
- Source finding: `{item['id']}`
- Dependencies: none
- Allowed files: {details['allowed']}

## Problem

{item['response']}

## Harmless reproduction

{details['reproduction']}

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: {item['hypothesis']}

## Regression test

{details['test']}

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

{acceptance}
"""


def main() -> int:
    parser_root = Path(__file__).resolve().parents[1]
    functional = parser_root
    run_root = functional.parent
    repo = functional.parents[3]
    completed_at = datetime.now(timezone.utc).isoformat()

    operator_rows, per_case = build_operator_rows(functional)
    operator_fields = [
        "operator_case_id", "scenario_id", "profile", "repeat", "accepted_status",
        "pass_1_status", "pass_2_status", "pass_1_selected_record", "pass_2_selected_record",
        *[name for dimension in DIMENSIONS for name in (f"pass_1_{dimension}", f"pass_2_{dimension}", dimension)],
        "finding_ids", "evidence_refs",
    ]
    write_csv(functional / "OPERATOR_ACCEPTANCE_RESULTS.csv", operator_rows, operator_fields)

    catalog = read_csv(functional / "OPERATOR_TASK_CATALOG.csv")
    for row in catalog:
        statuses = per_case[row["operator_case_id"]]
        row["status"] = aggregate_status(statuses)
        row["evidence_path"] = "evidence/gui_operator_runs.jsonl"
    write_csv(functional / "OPERATOR_TASK_CATALOG.csv", catalog, list(catalog[0]))

    queue = read_csv(functional / "SCENARIO_QUEUE.csv")
    results: list[dict[str, Any]] = []
    for row in queue:
        scenario_id = row["scenario_id"]
        if row["operator_case_id"]:
            case_id = row["operator_case_id"]
            status = aggregate_status(per_case[case_id])
            evidence = "evidence/gui_operator_runs.jsonl;" + (
                "evidence/SEMANTIC_REVIEW_RECONCILIATION.csv"
                if int(case_id.split("-")[1]) <= 44
                else "evidence/PROFILE_SEMANTIC_REVIEW_RECONCILIATION.csv"
            )
            notes = "Aggregated from all required repeats; any repeat failure dominates the scenario status."
            findings = finding_for_case(case_id, status)
        else:
            status, evidence, notes, findings = TECHNICAL[scenario_id]
        row["status"] = status
        row["evidence_path"] = evidence
        results.append(
            {
                "scenario_id": scenario_id,
                "group_id": row["group_id"],
                "title": row["title"],
                "profile": row["profile"],
                "surface": row["surface"],
                "critical": row["critical"],
                "operator_case_id": row["operator_case_id"],
                "status": status,
                "evidence_refs": evidence,
                "finding_ids": findings,
                "notes": notes,
            }
        )
    if any(row["status"] == "NOT_RUN" for row in results):
        raise RuntimeError("NOT_RUN remains in final results")
    write_csv(functional / "SCENARIO_QUEUE.csv", queue, list(queue[0]))
    write_csv(
        functional / "RESULTS.csv",
        results,
        ["scenario_id", "group_id", "title", "profile", "surface", "critical", "operator_case_id", "status", "evidence_refs", "finding_ids", "notes"],
    )

    repeat_counts = Counter(row["accepted_status"] for row in operator_rows)
    scenario_counts = Counter(row["status"] for row in results)
    profile_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in operator_rows:
        profile_counts[row["profile"]][row["accepted_status"]] += 1

    findings_dir = functional / "findings"
    findings_dir.mkdir(exist_ok=True)
    for item in FINDINGS:
        (findings_dir / f"{item['id']}.md").write_text(finding_markdown(item), encoding="utf-8", newline="\n")

    findings_index_rows = "\n".join(
        f"| [{item['id']}](findings/{item['id']}.md) | {item['priority']} | {item['category']} | {item['title']} |"
        for item in FINDINGS
    )
    (functional / "FUNCTIONAL_FINDINGS_INDEX.md").write_text(
        "# Functional findings index\n\n"
        "| Finding | Priority | Category | Title |\n|---|---|---|---|\n"
        + findings_index_rows
        + "\n\nAll accepted FAIL rows in `RESULTS.csv` and `OPERATOR_ACCEPTANCE_RESULTS.csv` map to one or more findings.\n",
        encoding="utf-8", newline="\n",
    )

    spark = functional / "spark"
    tasks_dir = spark / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    task_rows: list[dict[str, str]] = []
    for index, item in enumerate(FINDINGS, 1):
        task_id = f"SPARK-{index:04d}"
        task_path = f"tasks/{task_id}.md"
        (tasks_dir / f"{task_id}.md").write_text(spark_task_markdown(index, item), encoding="utf-8", newline="\n")
        task_rows.append(
            {
                "task_id": task_id,
                "priority": item["priority"],
                "status": "READY",
                "title": item["title"],
                "finding_id": item["id"],
                "dependencies": "",
                "task_path": task_path,
                "acceptance": TASK_DETAILS[item["id"]].get("acceptance", item["acceptance"]),
            }
        )
    write_csv(spark / "QUEUE.csv", task_rows, list(task_rows[0]))
    (spark / "START_HERE.md").write_text(
        "# Functional Spark queue\n\n"
        "Run only through `docs/audit/07_JARVIS_FUNCTIONAL_SPARK_REMEDIATION_PROMPT.md`. "
        "This queue contains only defects reproduced by the current functional campaign.\n",
        encoding="utf-8", newline="\n",
    )
    (spark / "TASK_SCHEMA.md").write_text(
        "# Task schema\n\nEach task must retain: READY status, one source finding with evidence, harmless reproduction, allowed files, focused regression test, cleanup, and binary acceptance criteria. Dependencies must reference existing task IDs and remain acyclic.\n",
        encoding="utf-8", newline="\n",
    )
    (spark / "PROGRESS.md").write_text(
        f"# Progress\n\n- Queue: {len(task_rows)} READY, 0 BLOCKED, 0 DONE.\n"
        "- Consistency: all task files exist; finding IDs are unique and evidenced; dependencies are empty/acyclic; operator gate has 169/169 keys.\n",
        encoding="utf-8", newline="\n",
    )
    (spark / "READY").write_text("READY\n", encoding="utf-8", newline="\n")

    profile_table = "\n".join(
        f"| {profile} | {counts.get('PASS', 0)} | {counts.get('FAIL', 0)} | {counts.get('INCONCLUSIVE', 0)} | {counts.get('BLOCKED_BY_ENV', 0) + counts.get('BLOCKED_BY_SPEC', 0) + counts.get('BLOCKED_BY_SAFETY', 0)} |"
        for profile, counts in sorted(profile_counts.items())
    )
    scenario_summary = ", ".join(f"{key}={value}" for key, value in sorted(scenario_counts.items()))
    repeat_summary = ", ".join(f"{key}={value}" for key, value in sorted(repeat_counts.items()))

    reports = {
        "INSTRUCTION_FOLLOWING_REPORT.md": f"""# Instruction following report

Operator gate completed: 68 cases, 169/169 required repeat keys. Accepted repeat totals: {repeat_summary}.

| Profile | PASS | FAIL | INCONCLUSIVE | blocked |
|---|---:|---:|---:|---:|
{profile_table}

Turbo completed useful direct answers in a substantial subset, but exact count/JSON constraints, follow-up references, ambiguity handling, document work, citations, and runtime status tasks failed repeatably. Both independent profile reviews disagreed on classifying 60 degraded 31B outcomes, so the prompt rule records them as INCONCLUSIVE; direct provider probes still independently establish degenerate `cyclic` output.
""",
        "RESPONSE_INTEGRITY_REPORT.md": """# Response integrity report

- Authoritative synthetic turbo harness: 52 PASS, 0 FAIL/ERROR, 2 optional SKIP.
- GUI semantic review exposed raw `call:*`/tool JSON on document and runtime routes.
- Mono GUI attempts frequently produced no terminal answer within the bounded 15-second window; direct probes emitted repeated `cyclic` tokens.
- Navigation interruption produced empty stale assistant state in the GUI.
- No functional READY marker is allowed while response leaks, empty finals, and profile degeneration remain.
""",
        "REAL_WORLD_JOURNEYS_REPORT.md": """# Real-world journeys report

The campaign covered all 14 functional groups through 47 mapped journeys and 86 scenarios. Direct turbo chat, navigation, API contracts, backup integrity, guarded CLI mutation, keyboard submit, and bounded soak were usable. Exact artifact work, previously uploaded document recall, cited web synthesis, mission completion, 31B profiles, and runtime-home UI isolation were not functionally reliable.

Scenario totals: """ + scenario_summary + ".\n",
        "PROFILE_AND_MODEL_REPORT.md": """# Profile and model report

| Profile | Resolved model | Context/config | Observed result |
|---|---|---|---|
| gemma4-turbo | `/models/gemma4-26b-a4b-nvfp4` served as `dispatcher` | 32768, CUDA graph, FP8 KV, max seqs 16 | Interactive; final keyboard smoke answered in about 1.1 s. |
| gemma4-mono-perf | `/models/gemma4-31b-it-nvfp4` served as `dispatcher` | 4096, eager, 2.5 GiB CPU offload | Three direct probes returned repeated `cyclic`; GUI fallback/timeout. |
| gemma4-mono | `/models/gemma4-31b-it-nvfp4` served as `dispatcher` | 16384, eager, 24 GiB CPU and 16 GiB KV offload, max seqs 1 | Readiness crossed the 20-minute deadline; one direct completion took 47.25 s and returned repeated `cyclic`; observed decode 0.1-0.4 tok/s. |

Launcher selection, resolved API settings, container command, provider `/v1/models`, loaded model root, and GUI display were captured for every profile. Identity mapping was truthful; functional quality/readiness for both 31B profiles failed.
""",
        "STARTUP_AND_RECOVERY_REPORT.md": """# Startup and recovery report

- Standard turbo cold start reached readiness in about 376 seconds.
- Mono-perf became ready after roughly 3.5 minutes but failed model-quality probes.
- Mono required extended two-stage autotuning and crossed the bounded 20-minute deadline before later becoming healthy.
- Three warm turbo starts returned nonzero due to the API executive-state lease.
- Unknown profile failed quickly with the valid profile list.
- Owned occupied-port and interrupted-start fixtures preserved the owned listener/process boundary and standard cleanup closed ports 3000/8000/8001/8765.
- Final post-fault turbo start, smoke, stop, and inventory passed.
""",
        "GUI_AND_STREAMING_REPORT.md": """# GUI and streaming report

All eight primary navigation sections and six dialog side tabs rendered. Final turbo keyboard submit completed normally and displayed the truthful profile/model. Served Next assets, service worker, and manifest matched local source/build artifacts. Programmatic viewport/zoom controls were unavailable in the in-app browser and remain an environment gap.

Failures: a runtime-home switch retained a stale transcript while the backend had zero conversations; stream navigation interruption could leave an empty bubble. Synthetic NDJSON parsing/order/persistence passed, while complete real cancellation/late-terminal and two-session WebSocket reconnect coverage remains inconclusive.
""",
        "DOCUMENT_AND_TOOL_REPORT.md": """# Document and tool report

- 28 initial document fixtures and 14 profile fixtures were generated and hash-manifested; all 42 uploads returned HTTP 200.
- Three valid PDFs rendered cleanly and three corrupt PDFs were rejected as expected. LibreOffice was unavailable, so visual DOCX rendering remained BLOCKED_BY_ENV.
- Turbo document recall, exact paths, transformations, and concurrent output were inconsistent; deterministic artifact validation failed.
- Approved safe directory creation failed because a non-canonical tool name was bound to the approval.
- Copied database backup/integrity/lock/read-only/temp probes passed without touching production state.
""",
        "MISSION_MEMORY_REPORT.md": """# Mission and memory report

Mission creation/progress surfaces rendered, but controlled document-comparison missions did not reach a truthful final result. Ambiguous report requests created mission plans before clarification. The requested memory namespace `audit.functional.20260713` was ignored and records appeared under `operator`, demonstrating namespace/state mismatch. Persona and autonomy policy were restored before cleanup.
""",
        "PERFORMANCE_REPORT.md": """# Performance report

Turbo remained interactive during API, GUI, and the 120-second soak. The soak captured 19/19 healthy samples and 4/4 successful non-empty chats. Mono-perf direct probes took about 7 seconds but returned degenerate content. Mono crossed a 20-minute startup deadline, one 16-token direct probe took 47.25 seconds, and provider logs showed 0.1-0.4 tok/s. These 31B observations meet the prompt's practical-unusability threshold for a performance finding.
""",
        "LONG_RUN_REPORT.md": """# Long-run report

The campaign ran across multiple profile cycles and hundreds of GUI/API interactions. A dedicated 120-second watchdog soak captured 19 valid health/resource samples and four successful turbo chats without health loss. Startup fault cleanup and final normal smoke passed. A successful long-running mission soak could not be demonstrated because mission/document recall failed, so FUNC-0084 remains INCONCLUSIVE.
""",
        "FUNCTIONAL_ASSURANCE_STATEMENT.md": f"""# Functional assurance statement

Status: `COMPLETE_WITH_BLOCKERS`.

The campaign executed all 86 scenarios to a terminal status and all 169 operator repeat keys with two independent semantic reviews. Scenario totals: {scenario_summary}. Operator repeat totals: {repeat_summary}. The tested production source HEAD was `{TESTED_HEAD}`; source drift from the static baseline was limited to audit documentation, and this campaign changed no production runtime code.

`functional/READY` is intentionally absent because accepted failures and critical profile/integrity defects remain. The functional Spark queue is internally consistent and `functional/spark/READY` is present for remediation work only.
""",
        "RESIDUAL_GAPS.md": """# Residual gaps

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
""",
    }
    for name, content in reports.items():
        (functional / name).write_text(content, encoding="utf-8", newline="\n")

    (functional / "START_HERE.md").write_text(
        "# Functional runtime acceptance\n\n"
        f"Run: `{RUN_PATH}`  \nStatus: `COMPLETE_WITH_BLOCKERS`  \n"
        "Start with `FUNCTIONAL_ASSURANCE_STATEMENT.md`, `RESULTS.csv`, `OPERATOR_ACCEPTANCE_RESULTS.csv`, and `FUNCTIONAL_FINDINGS_INDEX.md`. "
        "Do not treat `spark/READY` as product readiness; `functional/READY` is absent.\n",
        encoding="utf-8", newline="\n",
    )
    (functional / "RESUME_FROM_PARTIAL.md").write_text(
        "# Resume state\n\nThe new functional namespace is complete. Previous partial audit artifacts remain untouched and externally checkpointed. Resume remediation only through `docs/audit/07_JARVIS_FUNCTIONAL_SPARK_REMEDIATION_PROMPT.md` and `spark/QUEUE.csv`.\n",
        encoding="utf-8", newline="\n",
    )

    state = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "functional_run_path": RUN_PATH,
        "status": "COMPLETE_WITH_BLOCKERS",
        "operator_ready": True,
        "started_at_utc": "2026-07-13T16:08:36Z",
        "completed_at_utc": completed_at,
        "source_head_at_start": TESTED_HEAD,
        "source_head_tested": TESTED_HEAD,
        "source_drift": "AUDIT_DOCS_AND_FUNCTIONAL_ARTIFACTS_ONLY",
        "checkpoint_path": r"D:\jarvis\audit-backups\20260713T002206Z_686424795712\pre-functional-resume\checkpoint-20260713T160611Z",
        "progress_percent": 100,
        "counts": {
            "scenarios": len(results),
            "scenario_statuses": dict(sorted(scenario_counts.items())),
            "operator_cases": len(per_case),
            "operator_repeats": len(operator_rows),
            "operator_repeat_statuses": dict(sorted(repeat_counts.items())),
            "findings": len(FINDINGS),
            "spark_tasks": len(task_rows),
        },
        "profiles": ["gemma4-turbo", "gemma4-mono-perf", "gemma4-mono"],
        "markers": {"functional_ready": False, "spark_ready": True},
    }
    (functional / "FUNCTIONAL_STATE.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    pipeline_path = run_root / "PIPELINE_STATE.json"
    pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    pipeline["phase_b_functional"] = {
        "status": "COMPLETE_WITH_BLOCKERS",
        "operator_ready": True,
        "functional_run_path": RUN_PATH,
    }
    pipeline["phase_b_extended"] = {"status": "DEFERRED"}
    pipeline["spark_functional"] = {"status": "READY"}
    pipeline_path.write_text(json.dumps(pipeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    latest = repo / ".audit" / "LATEST_FUNCTIONAL_RUN.txt"
    latest.parent.mkdir(exist_ok=True)
    latest.write_text(RUN_PATH + "\n", encoding="utf-8", newline="\n")

    with (functional / "JOURNAL.md").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            f"\n## {completed_at} — campaign complete\n\n"
            f"- Completed 86 scenarios and all 169 operator repeat keys.\n"
            f"- Accepted operator repeat totals: {repeat_summary}.\n"
            f"- Scenario totals: {scenario_summary}.\n"
            f"- Created {len(FINDINGS)} reproduced findings and {len(task_rows)} consistent Spark tasks.\n"
            "- `functional/READY` remains absent; `functional/spark/READY` is present.\n"
            "- Isolated runtime was returned to the initial offline state; generated home retained for evidence.\n"
        )

    print(
        json.dumps(
            {
                "operator_repeats": len(operator_rows),
                "operator_statuses": dict(sorted(repeat_counts.items())),
                "scenarios": len(results),
                "scenario_statuses": dict(sorted(scenario_counts.items())),
                "findings": len(FINDINGS),
                "spark_tasks": len(task_rows),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
