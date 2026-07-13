from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx


EXPECTED_HOME = Path(r"D:\jarvis\audit-functional\20260713T002206Z_686424795712")
ROOT = Path(__file__).resolve().parents[1] / "evidence" / "gui-fixtures"
MANIFEST = ROOT / "profile-pdf-fixtures-manifest.json"
OUTPUT = Path(__file__).resolve().parents[1] / "evidence" / "profile-fixture-upload-results.json"


def main() -> int:
    if OUTPUT.exists():
        raise SystemExit(f"refusing to overwrite {OUTPUT}")
    token = os.environ.get("JARVIS_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("JARVIS_API_TOKEN is required")
    names = [item["name"] for item in json.loads(MANIFEST.read_text(encoding="utf-8"))["files"]]
    rows: list[dict[str, object]] = []
    with httpx.Client(
        base_url="http://127.0.0.1:8000",
        headers={"X-Jarvis-Api-Token": token, "Accept": "application/json"},
        timeout=120,
        trust_env=False,
    ) as client:
        health = client.get("/health")
        health.raise_for_status()
        if Path(health.json().get("home", "")).resolve() != EXPECTED_HOME.resolve():
            raise SystemExit("unexpected runtime home")
        for name in names:
            path = ROOT / name
            payload = path.read_bytes()
            response = client.post(
                "/api/files/upload",
                files={
                    "file": (
                        name,
                        payload,
                        mimetypes.guess_type(name)[0] or "application/octet-stream",
                    )
                },
            )
            rows.append(
                {
                    "name": name,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "status_code": response.status_code,
                    "response": response.json(),
                }
            )
    document = {
        "schema": "jarvis.functional-profile-fixture-uploads.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "expected_home": str(EXPECTED_HOME),
        "profile": "gemma4-mono-perf",
        "count": len(rows),
        "successful": sum(1 for row in rows if row["status_code"] == 200),
        "results": rows,
    }
    with OUTPUT.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"output": str(OUTPUT), "count": len(rows), "successful": document["successful"]}))
    return 0 if document["successful"] == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
