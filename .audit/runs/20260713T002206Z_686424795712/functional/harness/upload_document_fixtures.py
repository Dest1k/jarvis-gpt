from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx


EXPECTED_HOME = Path(r"D:\jarvis\audit-functional\20260713T002206Z_686424795712")
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "evidence" / "gui-fixtures"
OUTPUT = Path(__file__).resolve().parents[1] / "evidence" / "document-upload-results.json"
BASE_URL = "http://127.0.0.1:8000"
NAMES = [
    "synthetic-summary-1.txt",
    "synthetic-summary-2.txt",
    "synthetic-summary-3.txt",
    "synthetic-fact-1.txt",
    "synthetic-fact-2.txt",
    "synthetic-fact-3.txt",
    "structured-1.md",
    "structured-2.md",
    "structured-3.md",
    "campaign-recall-1.txt",
    "campaign-recall-2.txt",
    "campaign-recall-3.txt",
    "source-copy-1.docx",
    "source-copy-2.docx",
    "source-copy-3.docx",
    "convert-1.md",
    "convert-2.md",
    "corrupt-1.pdf",
    "corrupt-2.pdf",
    "corrupt-3.pdf",
    "alpha-1.txt",
    "alpha-2.txt",
    "alpha-3.txt",
    "beta-1.txt",
    "beta-2.txt",
    "beta-3.txt",
    "mission-alpha.txt",
    "mission-beta.txt",
]


def main() -> int:
    token = os.environ.get("JARVIS_API_TOKEN", "").strip()
    if not token:
        raise SystemExit("JARVIS_API_TOKEN is required")
    headers = {"X-Jarvis-Api-Token": token, "Accept": "application/json"}
    results: list[dict[str, object]] = []
    with httpx.Client(
        base_url=BASE_URL,
        headers=headers,
        timeout=120,
        trust_env=False,
    ) as client:
        health = client.get("/health")
        health.raise_for_status()
        observed_home = Path(health.json().get("home", ""))
        if observed_home.resolve() != EXPECTED_HOME.resolve():
            raise SystemExit(f"unexpected home: {observed_home}")
        for name in NAMES:
            path = FIXTURE_ROOT / name
            payload = path.read_bytes()
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            response = client.post(
                "/api/files/upload",
                files={"file": (name, payload, content_type)},
            )
            item: dict[str, object] = {
                "name": name,
                "path": str(path),
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "content_type": content_type,
                "status_code": response.status_code,
            }
            try:
                item["response"] = response.json()
            except ValueError:
                item["response_text"] = response.text[:1000]
            results.append(item)
    document = {
        "schema": "jarvis.functional-document-uploads.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "expected_home": str(EXPECTED_HOME),
        "fixture_root": str(FIXTURE_ROOT),
        "count": len(results),
        "successful": sum(1 for item in results if item["status_code"] == 200),
        "results": results,
    }
    OUTPUT.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "count": len(results), "successful": document["successful"]}))
    return 0 if document["successful"] == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
