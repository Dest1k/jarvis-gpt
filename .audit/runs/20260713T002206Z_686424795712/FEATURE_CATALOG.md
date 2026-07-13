# Feature catalog

Machine-readable catalog: `machine/features.jsonl` (254 rows). It contains one record per 94 REST operations, one WebSocket, 32 CLI commands, 98 registered tools, three profiles, three services, 15 UI surfaces and eight subsystem features. Each row links at least one requirement and marks PHASE B need.

Tracked-file inventory with classification, entry symbols, imports, side-effect categories and test references: `evidence/static/tracked_file_inventory.csv` (167 rows). Summary: 64 production, 2 schema, 52 tests, 18 config, 13 scripts, 14 docs, 3 generated and 1 repository metadata/unknown.

No predeclared legacy count was assumed. Confirmed inactive/leaking items include disabled direct WS UI, legacy mission handler/localStorage compatibility, legacy Gemma-via-Qwen env naming and active stale `requirements-surfer.txt` guidance.
