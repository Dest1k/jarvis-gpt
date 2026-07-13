"""Offline deterministic replay of previously sanitized evidence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .evidence import validate_evidence_file, validate_replay_contract
from .models import EXIT_FAIL, EXIT_HARNESS_ERROR, EXIT_PASS, Verdict
from .validators import run_validators
from .validators.context import ValidationContext


@dataclass(frozen=True, slots=True)
class ReplayCase:
    case_id: str
    recorded_verdict: Verdict
    replayed_verdict: Verdict
    deterministic_failures: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return self.recorded_verdict is self.replayed_verdict


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    cases: tuple[ReplayCase, ...]
    errors: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        counter = Counter(case.replayed_verdict.value for case in self.cases)
        return {verdict.value: counter.get(verdict.value, 0) for verdict in Verdict}

    @property
    def mismatches(self) -> tuple[str, ...]:
        return tuple(case.case_id for case in self.cases if not case.matches)

    @property
    def exit_code(self) -> int:
        if self.errors:
            return EXIT_HARNESS_ERROR
        if self.mismatches:
            return EXIT_FAIL
        return EXIT_PASS


def replay_record(
    record: Mapping[str, Any], *, context: ValidationContext | None = None
) -> ReplayCase:
    case_id = str(record.get("case_id", "<missing>"))
    recorded = Verdict(str(record["verdict"]))
    contract_errors = validate_replay_contract(record, recorded)
    if contract_errors:
        raise ValueError("; ".join(contract_errors))
    replay_mode = record["replay"]["mode"]
    observation = record.get("observation")
    validators = record.get("validators")
    if not isinstance(observation, Mapping) or not isinstance(validators, list):
        replayed = Verdict.ERROR
        failures = ("replay.record_shape",)
    elif replay_mode == "classification":
        assertions = record["assertions"]
        failures = tuple(
            str(assertion["name"])
            for assertion in assertions
            if isinstance(assertion, Mapping) and not bool(assertion["passed"])
        )
        replayed = recorded
    else:
        assertions = run_validators(observation, validators, context=context)
        failures = tuple(assertion.name for assertion in assertions if not assertion.passed)
        if not assertions:
            replayed = Verdict.ERROR
        elif failures:
            replayed = Verdict.FAIL
        elif bool(record.get("semantic_review_required", False)):
            replayed = Verdict.INCONCLUSIVE
        else:
            replayed = Verdict.PASS
    return ReplayCase(case_id, recorded, replayed, failures)


def replay_records(
    records: list[Mapping[str, Any]], *, context: ValidationContext | None = None
) -> ReplaySummary:
    cases: list[ReplayCase] = []
    errors: list[str] = []
    for index, record in enumerate(records, start=1):
        try:
            cases.append(replay_record(record, context=context))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"record {index}: {type(exc).__name__}: {exc}")
    return ReplaySummary(tuple(cases), tuple(errors))


def replay_file(path: Path, *, context: ValidationContext | None = None) -> ReplaySummary:
    try:
        records, validation_errors = validate_evidence_file(path)
        if validation_errors:
            return ReplaySummary((), tuple(validation_errors))
        return replay_records(records, context=context)
    except (OSError, ValueError) as exc:
        return ReplaySummary((), (f"{type(exc).__name__}: {exc}",))
