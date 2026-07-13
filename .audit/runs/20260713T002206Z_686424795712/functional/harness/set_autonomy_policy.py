#!/usr/bin/env python3
"""Set and verify the isolated runtime autonomy policy with an evidence record."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-safe-tools", action=argparse.BooleanOptionalAction, required=True)
    parser.add_argument("--max-autonomous-steps", type=int, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite {args.output}")

    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=15, trust_env=False) as client:
        health = client.get("/health")
        health.raise_for_status()
        observed_health = health.json()
        if Path(observed_health.get("home", "")).resolve() != args.home.resolve():
            raise SystemExit("unexpected runtime home")
        before_response = client.get("/api/autonomy/policy")
        before_response.raise_for_status()
        before = before_response.json()
        desired = {
            "allow_safe_tools": args.allow_safe_tools,
            "allow_review_tools": False,
            "allow_danger_tools": False,
            "allow_background_learning": False,
            "allow_self_healing_suggestions": False,
            "max_autonomous_steps": args.max_autonomous_steps,
        }
        updated_response = client.patch("/api/autonomy/policy", json=desired)
        updated_response.raise_for_status()
        updated = updated_response.json()
        after_response = client.get("/api/autonomy/policy")
        after_response.raise_for_status()
        after = after_response.json()

    verified = all(after.get(key) == value for key, value in desired.items())
    document = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "home": str(args.home.resolve()),
        "health": observed_health,
        "before": before,
        "requested": desired,
        "updated": updated,
        "after": after,
        "verified": verified,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"verified": verified, "output": str(args.output)}, ensure_ascii=False))
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
