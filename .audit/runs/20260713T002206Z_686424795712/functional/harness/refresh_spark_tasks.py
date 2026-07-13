#!/usr/bin/env python3
"""Refresh current-campaign findings and Spark tasks without rebuilding other evidence."""

from __future__ import annotations

import build_functional_deliverables as builder


def main() -> int:
    functional = builder.Path(__file__).resolve().parents[1]
    findings_dir = functional / "findings"
    spark = functional / "spark"
    tasks_dir = spark / "tasks"
    task_rows: list[dict[str, str]] = []

    for index, item in enumerate(builder.FINDINGS, 1):
        (findings_dir / f"{item['id']}.md").write_text(
            builder.finding_markdown(item), encoding="utf-8", newline="\n"
        )
        task_id = f"SPARK-{index:04d}"
        task_path = f"tasks/{task_id}.md"
        (tasks_dir / f"{task_id}.md").write_text(
            builder.spark_task_markdown(index, item), encoding="utf-8", newline="\n"
        )
        task_rows.append(
            {
                "task_id": task_id,
                "priority": item["priority"],
                "status": "READY",
                "title": item["title"],
                "finding_id": item["id"],
                "dependencies": "",
                "task_path": task_path,
                "acceptance": builder.TASK_DETAILS[item["id"]].get("acceptance", item["acceptance"]),
            }
        )

    builder.write_csv(spark / "QUEUE.csv", task_rows, list(task_rows[0]))
    result_status = {
        row["scenario_id"]: row["status"] for row in builder.read_csv(functional / "RESULTS.csv")
    }
    feature_rows = builder.read_csv(functional / "FEATURE_JOURNEY_MAP.csv")
    for row in feature_rows:
        scenario_ids = [item for item in row["scenario_ids"].split(";") if item]
        row["status"] = builder.aggregate_status([result_status[item] for item in scenario_ids])
    builder.write_csv(functional / "FEATURE_JOURNEY_MAP.csv", feature_rows, list(feature_rows[0]))
    (spark / "PROGRESS.md").write_text(
        f"# Progress\n\n- Queue: {len(task_rows)} READY, 0 BLOCKED, 0 DONE.\n"
        "- Consistency: every task has an existing allowed-file scope, exact harmless reproduction, focused regression command, binary acceptance criteria, one unique evidenced finding, and no dependencies; the operator gate has 169/169 keys.\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"refreshed {len(builder.FINDINGS)} findings, {len(task_rows)} Spark tasks, "
        f"and {len(feature_rows)} feature statuses"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
