# Audit state

- PHASE A: `COMPLETE_WITH_BLOCKERS`
- Source: `686424795712cb0a562750b6dade13de18c48792` (`main`)
- Audit branch: `audit/phase-a-20260713T002206Z_686424795712`
- Tracked source inventory: 167 files / 125,148 lines
- Features: 254; requirements: 22; static command records: 27
- Findings: 25 ({'high': 15, 'medium': 9, 'low': 1}; statuses {'static-confirmed': 17, 'probable-runtime': 4, 'spec-gap': 3, 'test-gap': 1})
- Live scenarios: 36 (`NOT_RUN`)
- Blockers: no PowerShell, Docker/Compose, Windows/live machine, GPU/models, real browser or GUI.
- Consistency gate: `PASS` (254 features, 22 requirements, 63 scenarios, 25 findings/tasks, clean production diff).
