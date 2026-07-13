#!/usr/bin/env python3
"""Fail the audit if traceability, paths, IDs or source isolation are inconsistent."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path


RUN = Path(__file__).resolve().parents[2]
REPO = RUN.parents[2]
MACHINE = RUN / "machine"
EVIDENCE = RUN / "evidence" / "static"
SOURCE = "686424795712cb0a562750b6dade13de18c48792"


def read_jsonl(name: str) -> list[dict[str, object]]:
    rows = []
    for number, line in enumerate((MACHINE / name).read_text(encoding="utf-8").splitlines(), 1):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AssertionError(f"{name}:{number}: {exc}") from exc
    return rows


def unique(rows: list[dict[str, object]], label: str) -> set[str]:
    ids = [str(row["id"]) for row in rows]
    duplicates = [item for item, count in Counter(ids).items() if count > 1]
    assert not duplicates, f"duplicate {label} IDs: {duplicates}"
    return set(ids)


def main() -> None:
    required = [
        "START_HERE.md", "PIPELINE_STATE.json", "AUDIT_STATE.md", "SOURCE_BASELINE.json",
        "PHASE_A_COMPLETION.md", "STATIC_EXECUTIVE_SUMMARY.md", "SYSTEM_MAP.md",
        "FEATURE_CATALOG.md", "CONFIGURATION_MAP.md", "BEHAVIORAL_CONTRACT.md",
        "DATA_AND_TRUST_BOUNDARIES.md", "TEST_INVENTORY.md", "STATIC_TEST_RESULTS.md",
        "STATIC_COVERAGE_REPORT.md", "STATIC_SCENARIO_MATRIX.csv",
        "STATIC_FINDINGS_INDEX.md", "SPEC_GAPS.md", "TEST_GAPS.md",
        "ARCHITECTURE_RISKS.md", "LIVE_AUDIT_PLAN.md", "LIVE_SCENARIO_QUEUE.csv",
        "SOURCE_DRIFT_POLICY.md", "AUDIT_JOURNAL.md", "EVIDENCE_MANIFEST.json",
        "handoff/PHASE_B_START_HERE.md", "machine/features.jsonl", "machine/requirements.jsonl",
        "machine/scenarios.jsonl", "machine/findings.jsonl", "machine/candidate_tasks.jsonl",
    ]
    missing = [path for path in required if not (RUN / path).is_file()]
    assert not missing, f"missing required artifacts: {missing}"

    features = read_jsonl("features.jsonl")
    requirements = read_jsonl("requirements.jsonl")
    scenarios = read_jsonl("scenarios.jsonl")
    findings = read_jsonl("findings.jsonl")
    tasks = read_jsonl("candidate_tasks.jsonl")
    feature_ids = unique(features, "feature")
    requirement_ids = unique(requirements, "requirement")
    scenario_ids = unique(scenarios, "scenario")
    finding_ids = unique(findings, "finding")
    task_ids = unique(tasks, "task")

    assert all(str(fid).startswith("FEAT-") for fid in feature_ids)
    assert all(str(rid).startswith("REQ-") for rid in requirement_ids)
    assert all(str(fid).startswith("JARVIS-") for fid in finding_ids)
    assert all(str(tid).startswith("CTASK-") for tid in task_ids)

    for feature in features:
        reqs = set(map(str, feature.get("requirement_ids") or []))
        assert reqs, f"feature without contract/spec gap: {feature['id']}"
        assert reqs <= requirement_ids, f"unknown requirement on {feature['id']}: {reqs - requirement_ids}"

    for requirement in requirements:
        if requirement.get("risk") != "high":
            continue
        coverage = [s for s in scenarios if str(requirement["id"]) in set(map(str, s.get("requirement_ids") or []))]
        assert coverage, f"high-risk requirement has no scenario: {requirement['id']}"
        modes = {mode for scenario in coverage for mode in scenario.get("validation_modes") or []}
        assert {"positive", "error", "recovery"} <= modes, f"missing scenario modes for {requirement['id']}: {modes}"

    evidence_ids = {
        path.name.split("_", 1)[0]
        for path in EVIDENCE.glob("EVID-STATIC-*_TEST-STATIC-*.json")
    }
    for finding in findings:
        fid = str(finding["id"])
        assert (RUN / "findings" / f"{fid}.md").is_file()
        evid = set(map(str, finding.get("evidence") or []))
        assert evid, f"finding without evidence: {fid}"
        assert evid <= evidence_ids, f"unknown evidence on {fid}: {evid - evidence_ids}"
        scenario = str(finding["scenario"])
        assert scenario in scenario_ids
        if finding.get("status") == "probable-runtime":
            assert scenario.startswith("SCN-LIVE-"), f"probable runtime finding without PHASE B: {fid}"
        for spec in finding.get("paths") or []:
            path = str(spec).split(":", 1)[0]
            assert (REPO / path).exists(), f"broken affected path {fid}: {path}"
        linked_tasks = set(map(str, finding.get("candidate_task_ids") or []))
        assert linked_tasks and linked_tasks <= task_ids

    for task in tasks:
        tid = str(task["id"])
        assert str(task["finding_id"]) in finding_ids
        assert (RUN / "candidate_tasks" / f"{tid}.md").is_file()

    with (RUN / "STATIC_SCENARIO_MATRIX.csv").open(newline="", encoding="utf-8") as handle:
        static_rows = list(csv.DictReader(handle))
    for row in static_rows:
        if row["status"] == "FAIL_HERMETIC":
            assert row["finding_ids"], f"FAIL without finding: {row['scenario_id']}"
    for scenario in scenarios:
        if scenario.get("phase") == "B":
            assert scenario.get("status") not in {"PASS_HERMETIC", "PASS"}, scenario["id"]

    manifest = json.loads((RUN / "EVIDENCE_MANIFEST.json").read_text(encoding="utf-8"))
    for item in manifest["items"]:
        path = REPO / str(item["path"])
        assert path.is_file(), f"manifest missing path: {path}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"], f"manifest hash mismatch: {path}"

    for forbidden in [
        REPO / ".audit/LATEST_COMPLETE_RUN.txt", REPO / ".audit/LATEST_RUN.txt",
        REPO / "spark/READY", REPO / "spark/safety/READY",
    ]:
        assert not forbidden.exists(), f"forbidden readiness marker: {forbidden}"

    diff = subprocess.run(
        ["git", "diff", "--name-only", SOURCE, "--", ".", ":(exclude).audit/**", ":(exclude)docs/audit/**"],
        cwd=REPO, text=True, stdout=subprocess.PIPE, check=True,
    ).stdout.splitlines()
    assert not diff, f"production tracked drift: {diff}"
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=REPO, text=True, stdout=subprocess.PIPE, check=True,
    ).stdout.splitlines()
    outside = [line for line in status if not line[3:].startswith(".audit/")]
    assert not outside, f"production worktree changes: {outside}"

    report = {
        "status": "PASS",
        "features": len(features),
        "requirements": len(requirements),
        "scenarios": len(scenarios),
        "findings": len(findings),
        "candidate_tasks": len(tasks),
        "manifest_items_verified": len(manifest["items"]),
        "production_diff": [],
        "production_untracked_or_modified": [],
    }
    (EVIDENCE / "consistency_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
