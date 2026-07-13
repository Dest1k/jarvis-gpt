#!/usr/bin/env python3
"""Final structural and gate validation for the functional namespace."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path


RUN_ID = "20260713T002206Z_686424795712"
RUN_PATH = f".audit/runs/{RUN_ID}/functional"
ALLOWED = {
    "PASS", "FAIL", "BLOCKED_BY_ENV", "BLOCKED_BY_SAFETY", "BLOCKED_BY_SPEC", "INCONCLUSIVE", "NOT_APPLICABLE"
}
REQUIRED = (
    "RESULTS.csv", "OPERATOR_ACCEPTANCE_RESULTS.csv", "INSTRUCTION_FOLLOWING_REPORT.md",
    "RESPONSE_INTEGRITY_REPORT.md", "REAL_WORLD_JOURNEYS_REPORT.md", "PROFILE_AND_MODEL_REPORT.md",
    "STARTUP_AND_RECOVERY_REPORT.md", "GUI_AND_STREAMING_REPORT.md", "DOCUMENT_AND_TOOL_REPORT.md",
    "MISSION_MEMORY_REPORT.md", "PERFORMANCE_REPORT.md", "LONG_RUN_REPORT.md",
    "FUNCTIONAL_FINDINGS_INDEX.md", "FUNCTIONAL_ASSURANCE_STATEMENT.md", "RESIDUAL_GAPS.md",
    "FUNCTIONAL_STATE.json", "FEATURE_JOURNEY_MAP.csv", "SCENARIO_QUEUE.csv", "OPERATOR_TASK_CATALOG.csv",
    "START_HERE.md", "RESUME_FROM_PARTIAL.md", "JOURNAL.md", "ENVIRONMENT_BASELINE.md",
)


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Recompute all checks without overwriting the immutable v2 evidence files.",
    )
    args = parser.parse_args()
    functional = Path(__file__).resolve().parents[1]
    repo = functional.parents[3]
    run_root = functional.parent
    checks: dict[str, bool] = {}
    details: dict[str, object] = {}

    checks["required_files"] = all((functional / item).is_file() for item in REQUIRED)
    results = rows(functional / "RESULTS.csv")
    operator = rows(functional / "OPERATOR_ACCEPTANCE_RESULTS.csv")
    queue = rows(functional / "SCENARIO_QUEUE.csv")
    catalog = rows(functional / "OPERATOR_TASK_CATALOG.csv")
    feature_map = rows(functional / "FEATURE_JOURNEY_MAP.csv")
    checks["results_86_unique"] = len(results) == 86 and len({row["scenario_id"] for row in results}) == 86
    checks["results_terminal"] = all(row["status"] in ALLOWED for row in results) and all(
        row["status"] != "NOT_RUN" for row in results
    )
    checks["queue_matches_results"] = {
        row["scenario_id"]: row["status"] for row in queue
    } == {row["scenario_id"]: row["status"] for row in results}
    checks["feature_map_terminal"] = (
        len(feature_map) == 47
        and len({row["feature_id"] for row in feature_map}) == 47
        and all(row["status"] in ALLOWED and row["status"] != "NOT_RUN" for row in feature_map)
    )
    operator_keys = {(row["operator_case_id"], row["repeat"]) for row in operator}
    expected_keys = {
        (row["operator_case_id"], str(repeat))
        for row in catalog
        for repeat in range(1, int(row["repeat_count"]) + 1)
    }
    checks["operator_169_unique"] = len(operator) == 169 and operator_keys == expected_keys
    checks["operator_terminal"] = all(row["accepted_status"] in ALLOWED for row in operator)
    checks["operator_two_reviews"] = all(row["pass_1_status"] and row["pass_2_status"] for row in operator)

    findings = sorted((functional / "findings").glob("FUNC-FIND-*.md"))
    finding_ids = {path.stem for path in findings}
    fail_rows = [row for row in results if row["status"] == "FAIL"]
    operator_fail_rows = [row for row in operator if row["accepted_status"] == "FAIL"]
    checks["all_scenario_fails_have_findings"] = all(
        row["finding_ids"] and set(row["finding_ids"].split(";")) <= finding_ids for row in fail_rows
    )
    checks["all_operator_fails_have_findings"] = all(
        row["finding_ids"] and set(row["finding_ids"].split(";")) <= finding_ids for row in operator_fail_rows
    )

    spark_rows = rows(functional / "spark" / "QUEUE.csv")
    spark_ids = {row["task_id"] for row in spark_rows}
    checks["spark_one_per_finding"] = len(spark_rows) == len(findings) and {
        row["finding_id"] for row in spark_rows
    } == finding_ids
    checks["spark_tasks_exist"] = all((functional / "spark" / row["task_path"]).is_file() for row in spark_rows)
    checks["spark_ready_valid"] = all(row["status"] == "READY" and not row["dependencies"] for row in spark_rows)
    task_documents = [
        (functional / "spark" / row["task_path"]).read_text(encoding="utf-8") for row in spark_rows
    ]
    required_task_sections = (
        "- Allowed files: ", "## Harmless reproduction", "## Regression test", "## Binary acceptance criteria"
    )
    checks["spark_task_contracts"] = all(
        all(section in document for section in required_task_sections) for document in task_documents
    )
    allowed_paths = [
        path.strip()
        for document in task_documents
        for line in document.splitlines()
        if line.startswith("- Allowed files: ")
        for path in line.removeprefix("- Allowed files: ").split(";")
    ]
    checks["spark_allowed_paths_exist"] = bool(allowed_paths) and all((repo / path).exists() for path in allowed_paths)
    checks["markers"] = not (functional / "READY").exists() and (functional / "spark" / "READY").is_file()

    state = json.loads((functional / "FUNCTIONAL_STATE.json").read_text(encoding="utf-8"))
    pipeline = json.loads((run_root / "PIPELINE_STATE.json").read_text(encoding="utf-8"))
    checks["functional_state"] = (
        state.get("status") == "COMPLETE_WITH_BLOCKERS"
        and state.get("operator_ready") is True
        and state.get("progress_percent") == 100
        and state.get("markers") == {"functional_ready": False, "spark_ready": True}
    )
    checks["pipeline_state"] = (
        pipeline.get("phase_b_functional", {}).get("status") == "COMPLETE_WITH_BLOCKERS"
        and pipeline.get("phase_b_functional", {}).get("operator_ready") is True
        and pipeline.get("phase_b_extended", {}).get("status") == "DEFERRED"
        and pipeline.get("spark_functional", {}).get("status") == "READY"
    )
    latest = repo / ".audit" / "LATEST_FUNCTIONAL_RUN.txt"
    checks["latest_pointer"] = latest.read_text(encoding="utf-8") == RUN_PATH + "\n"
    checks["no_old_complete_pointer"] = not (repo / ".audit" / "LATEST_COMPLETE_RUN.txt").exists()

    cleanup = json.loads((functional / "evidence" / "final-cleanup-inventory-v2.json").read_text(encoding="utf-8"))
    checks["final_ports_closed"] = cleanup.get("ports") == {
        "3000": False, "8000": False, "8001": False, "8765": False
    }
    checks["final_container_absent"] = not cleanup.get("docker_ps", {}).get("stdout", "").strip()
    machine = json.loads((functional / "evidence" / "final-machine-baseline-restored.json").read_text(encoding="utf-8"))
    checks["final_machine_baseline_restored"] = machine.get("summary") == {
        "ports_closed": True,
        "docker_desktop_stopped": True,
        "docker_engine_unavailable": True,
        "wsl_docker_desktop_stopped": True,
    }
    checks["raw_doctor_not_referenced"] = all(
        "doctor-full-final.json" not in path.read_text(encoding="utf-8", errors="replace")
        for path in (
            functional / "RESULTS.csv",
            functional / "FUNCTIONAL_FINDINGS_INDEX.md",
            functional / "FUNCTIONAL_ASSURANCE_STATEMENT.md",
        )
    )

    details.update(
        {
            "scenario_statuses": dict(sorted(__import__("collections").Counter(row["status"] for row in results).items())),
            "operator_statuses": dict(sorted(__import__("collections").Counter(row["accepted_status"] for row in operator).items())),
            "scenario_fail_rows": len(fail_rows),
            "operator_fail_rows": len(operator_fail_rows),
            "findings": len(findings),
            "spark_tasks": len(spark_rows),
            "failed_checks": [name for name, ok in checks.items() if not ok],
        }
    )
    document = {
        "schema": "jarvis.functional-final-consistency.v2",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "details": details,
        "ok": all(checks.values()),
    }
    output = functional / "evidence" / "final-consistency-check-v2.json"
    if output.exists():
        if not args.verify_only:
            raise SystemExit(f"refusing to overwrite {output}")
    else:
        if args.verify_only:
            raise SystemExit(f"verification evidence does not exist: {output}")
        output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (functional / "evidence" / "FINAL_CONSISTENCY_CHECK_V2.md").write_text(
            "# Final consistency check v2\n\n"
            + f"- Result: `{'PASS' if document['ok'] else 'FAIL'}`\n"
            + f"- Checks: {sum(checks.values())}/{len(checks)} passed.\n"
            + f"- Scenarios: {details['scenario_statuses']}\n"
            + f"- Operator repeats: {details['operator_statuses']}\n"
            + f"- Findings/Spark tasks: {len(findings)}/{len(spark_rows)}\n"
            + f"- Failed checks: {details['failed_checks']}\n",
            encoding="utf-8", newline="\n",
        )
    print(json.dumps({"ok": document["ok"], **details}, ensure_ascii=False))
    return 0 if document["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
