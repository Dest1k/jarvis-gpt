# RESIDUAL_GAPS

Non-blocking residual gaps after one good-faith remediation campaign.

| ID | Severity | Description | Disposition |
|----|----------|-------------|-------------|
| RG-LIVE-STACK | P2 | Full live turbo start/warm-repeat/GUI/doctor not re-executed in this campaign because no owned stack was running and isolation forbids production runtime mutation | Residual; run during independent acceptance review |
| RG-STREAM-3X | P2 | Stream interruption not exercised 3x live | Residual |
| RG-QA-REPLAY | P2 | Assurance QA validate/replay harness not re-run end-to-end | Residual |
| RG-31B-PERF | Research | 31B quality/latency not tuned; product decision marks research-only | PROFILE-RESEARCH backlog; not release blocker |
| RG-VISUAL-DOCX | P2 | LibreOffice visual DOCX render still environment-blocked | Residual env gap |

No residual secret leak, internal protocol leak, false success, cross-session mix, missing claimed artifact, or certified-turbo startup P0/P1 remains unaddressed at the contract/unit level.
| RG-SPARK-0005-OFFLINE-GAP | closed | Original SPARK-0005 was test-only FailLLM; live gate landed in release-blocker remediation | Closed by RB-2 fix commits |
| RG-DOCKER-DESKTOP-FLAKE | P2 | Docker Desktop engine intermittently disconnects on host during long LLM load | Residual host env; not product logic |
