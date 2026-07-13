from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


EXPECTED_HOME = Path(r"D:\jarvis\audit-functional\20260713T002206Z_686424795712")
EVIDENCE = Path(__file__).resolve().parents[1] / "evidence"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    output = EVIDENCE / args.output
    if output.exists():
        raise SystemExit(f"refusing to overwrite {output}")
    with httpx.Client(timeout=args.timeout, trust_env=False) as client:
        health = client.get("http://127.0.0.1:8000/health")
        health.raise_for_status()
        observed = health.json()
        if Path(observed.get("home", "")).resolve() != EXPECTED_HOME.resolve():
            raise SystemExit("unexpected runtime home")
        if observed.get("profile") != args.profile:
            raise SystemExit(f"unexpected profile: {observed.get('profile')}")
        probes: list[dict[str, object]] = []
        models_payload: dict[str, object] | None = None
        model_error: str | None = None
        try:
            models = client.get("http://127.0.0.1:8001/v1/models")
            models.raise_for_status()
            models_payload = models.json()
            for repeat in range(1, 4):
                started = time.perf_counter()
                response = client.post(
                    "http://127.0.0.1:8001/v1/chat/completions",
                    json={
                        "model": "dispatcher",
                        "messages": [
                            {
                                "role": "user",
                                "content": "Ответь одним русским словом: сколько будет два плюс два?",
                            }
                        ],
                        "max_tokens": 16,
                        "temperature": 0,
                    },
                )
                row: dict[str, object] = {
                    "repeat": repeat,
                    "status_code": response.status_code,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
                try:
                    row["response"] = response.json()
                except ValueError:
                    row["response_text"] = response.text[:2000]
                probes.append(row)
        except (httpx.HTTPError, ValueError) as exc:
            model_error = f"{type(exc).__name__}: {exc}"
    document = {
        "schema": "jarvis.functional-profile-model-probe.v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "profile": args.profile,
        "expected_home": str(EXPECTED_HOME),
        "health": observed,
        "models": models_payload,
        "model_error": model_error,
        "probes": probes,
    }
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps({"output": str(output), "profile": args.profile, "probes": len(probes)}))
    return 0 if model_error is None and len(probes) == 3 else 2


if __name__ == "__main__":
    raise SystemExit(main())
