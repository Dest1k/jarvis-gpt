#!/usr/bin/env python3
"""Refresh the evidence manifest after the final consistency gate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


RUN = Path(__file__).resolve().parents[2]
REPO = RUN.parents[2]
EVIDENCE = RUN / "evidence" / "static"


items = []
for path in sorted(EVIDENCE.rglob("*")):
    if path.is_file():
        data = path.read_bytes()
        items.append(
            {
                "path": str(path.relative_to(REPO)),
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
payload = {
    "schema_version": 1,
    "run_id": "20260713T002206Z_686424795712",
    "items": items,
    "snapshot_note": (
        "The command-runner metadata for the manifest refresh itself is created after this "
        "snapshot and is intentionally the sole self-referential exclusion."
    ),
}
(RUN / "EVIDENCE_MANIFEST.json").write_text(
    json.dumps(payload, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps({"status": "PASS", "manifest_items": len(items)}, indent=2))
