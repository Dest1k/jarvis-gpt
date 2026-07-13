# SPARK-0013 — Both 31B profiles are functionally unusable on the certified machine

- Status: `READY`
- Priority: `P1`
- Source finding: `FUNC-FIND-013`
- Dependencies: none
- Allowed files: backend/src/jarvis_gpt/config.py; backend/src/jarvis_gpt/model_catalog.py; backend/src/jarvis_gpt/dispatcher.py; docker-compose.yml; scripts/jarvis-launcher.ps1; backend/tests/test_config_storage.py; backend/tests/test_model_catalog.py; backend/tests/test_dispatcher.py; backend/tests/test_deployment_contracts.py

## Problem

Direct probes emitted repeated 'cyclic'; GUI returned 400 fallbacks or timed out; mono startup crossed 20 minutes and decoded near 0.1-0.4 tok/s.

## Harmless reproduction

Start each 31B profile from stopped state, record `/v1/models`, then issue three temperature-0 prompts asking 2+2 with `max_tokens=16` and run one GUI direct-answer case. Capture readiness, latency, finish reason, and exact output.

Use only the isolated functional home and controlled fixtures. Do not use production credentials or external mutable state.

## Implementation scope

Address the single hypothesis: 31B NVFP4 runtime/profile parameters are incompatible with model quality and practical context/latency on this host.

## Regression test

Add profile-owned readiness deadline fields and fake-clock launcher/dispatcher tests; add model-output health probes rejecting repeated-token degeneration. Run config, catalog, dispatcher, and deployment-contract tests.

## Validation and cleanup

- Run the focused regression test while iterating.
- Run the directly affected backend/frontend suite once after integration.
- Keep generated files under a temporary test directory and remove only task-owned fixtures.

## Binary acceptance criteria

Each profile declares a numeric readiness deadline; fake-clock tests enforce it; three live direct probes answer 4 (not repeated tokens), and a bounded GUI answer completes without fallback before the configured request timeout.
