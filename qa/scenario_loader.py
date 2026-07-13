"""Load compact JSON scenario suites without runtime imports."""

from __future__ import annotations

import json
from pathlib import Path

from .models import Scenario


def load_scenario_file(path: Path) -> list[Scenario]:
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value}")
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise ValueError(f"{path}: invalid JSON: {detail}") from exc
    items = document if isinstance(document, list) else [document]
    if not items or not all(isinstance(item, dict) for item in items):
        raise ValueError(f"{path}: expected a scenario object or non-empty array")
    return [Scenario.from_dict(item) for item in items]


def load_suite(path: Path) -> list[Scenario]:
    root = path.resolve()
    if not root.is_dir():
        raise ValueError(f"suite directory does not exist: {path}")
    files = sorted(root.glob("*.json"), key=lambda item: item.name)
    if not files:
        raise ValueError(f"suite has no JSON scenarios: {path}")
    scenarios = [scenario for file in files for scenario in load_scenario_file(file)]
    ids = [scenario.scenario_id for scenario in scenarios]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise ValueError(f"duplicate scenario ids: {', '.join(duplicates)}")
    return scenarios


def validate_suite(path: Path) -> tuple[list[Scenario], list[str]]:
    try:
        return load_suite(path), []
    except (OSError, ValueError) as exc:
        return [], [str(exc)]
