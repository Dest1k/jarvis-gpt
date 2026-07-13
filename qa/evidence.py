"""Append-only sanitized JSONL evidence and structural validation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import CampaignIdentity, CampaignSummary, CaseResult, Scenario, Verdict
from .redaction import credential_like_paths, redact_value

EVIDENCE_SCHEMA = "jarvis.qa.evidence.v1"
DETERMINISTIC_REPLAY_VERDICTS = frozenset(
    {Verdict.PASS, Verdict.FAIL, Verdict.INCONCLUSIVE}
)
CLASSIFICATION_REPLAY_VERDICTS = frozenset(
    {
        Verdict.BLOCKED_BY_ENV,
        Verdict.BLOCKED_BY_SPEC,
        Verdict.SKIP,
        Verdict.ERROR,
    }
)


def _replay_contract(result: CaseResult) -> dict[str, Any]:
    if result.verdict in DETERMINISTIC_REPLAY_VERDICTS:
        return {"mode": "deterministic"}
    if result.verdict in CLASSIFICATION_REPLAY_VERDICTS:
        if not result.error:
            raise ValueError(f"{result.verdict.value} requires a replay reason")
        return {
            "mode": "classification",
            "reason": result.error,
            "assertion_names": [assertion.name for assertion in result.assertions],
        }
    raise ValueError(f"unsupported replay verdict {result.verdict.value}")


def validate_replay_contract(record: Mapping[str, Any], verdict: Verdict) -> list[str]:
    """Validate the typed replay mode without trusting a free-form marker."""

    errors: list[str] = []
    replay = record.get("replay")
    if not isinstance(replay, Mapping):
        return ["replay must be an object"]
    mode = replay.get("mode")
    if mode == "deterministic":
        if set(replay) != {"mode"}:
            errors.append("deterministic replay has unexpected fields")
        if verdict not in DETERMINISTIC_REPLAY_VERDICTS:
            errors.append(f"{verdict.value} cannot use deterministic replay mode")
        return errors
    if mode != "classification":
        return ["replay mode must be deterministic or classification"]
    if set(replay) != {"mode", "reason", "assertion_names"}:
        errors.append("classification replay fields are incomplete or unexpected")
    if verdict not in CLASSIFICATION_REPLAY_VERDICTS:
        errors.append(f"{verdict.value} cannot use classification replay mode")

    reason = replay.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("classification replay reason must be non-empty")
    if record.get("error") != reason:
        errors.append("classification replay reason must match error")

    assertions = record.get("assertions")
    assertion_names = replay.get("assertion_names")
    if not isinstance(assertions, list) or not isinstance(assertion_names, list) or any(
        not isinstance(name, str) for name in assertion_names
    ):
        errors.append("classification replay assertion_names must be a string array")
        return errors
    actual_names = [
        str(assertion.get("name"))
        for assertion in assertions
        if isinstance(assertion, Mapping)
    ]
    if assertion_names != actual_names:
        errors.append("classification replay assertion_names mismatch")

    expected_names = {
        Verdict.BLOCKED_BY_ENV: {"runner.environment_available"},
        Verdict.BLOCKED_BY_SPEC: {"runner.specification_complete"},
        Verdict.SKIP: {"runner.optional_skip"},
        Verdict.ERROR: {"runner.assertions_present", "runner.completed_without_error"},
    }
    expected_passed = verdict is Verdict.SKIP
    matching = [
        assertion
        for assertion in assertions
        if isinstance(assertion, Mapping)
        and assertion.get("name") in expected_names.get(verdict, set())
        and assertion.get("passed") is expected_passed
    ]
    if len(matching) != 1 or len(assertions) != 1:
        errors.append(f"{verdict.value} lacks its exact runner classification assertion")
    if verdict is Verdict.SKIP and record.get("required") is not False:
        errors.append("SKIP requires required=false")
    return errors


def _bound_sanitized(value: Any, depth: int = 0) -> Any:
    if depth >= 10:
        return "[MAX_DEPTH]"
    if isinstance(value, str):
        return value if len(value) <= 20_000 else value[:20_000] + "[TRUNCATED]"
    if isinstance(value, Mapping):
        return {
            str(key): _bound_sanitized(item, depth + 1)
            for key, item in list(value.items())[:200]
        }
    if isinstance(value, list | tuple):
        return [_bound_sanitized(item, depth + 1) for item in value[:200]]
    return value


class EvidenceStore:
    """A campaign-owned JSONL file created once and only appended thereafter."""

    def __init__(
        self,
        output_root: Path,
        identity: CampaignIdentity,
        *,
        canaries: Iterable[str] = (),
    ) -> None:
        self.output_root = output_root.resolve()
        self.identity = identity
        self.canaries = tuple(canaries)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.path = self.output_root / f"{identity.campaign_id}.jsonl"
        self.manifest_path = self.output_root / f"{identity.campaign_id}.manifest.json"
        with self.path.open("x", encoding="utf-8", newline="\n"):
            pass

    def append(self, scenario: Scenario, result: CaseResult) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema": EVIDENCE_SCHEMA,
            "campaign_id": self.identity.campaign_id,
            "namespace": self.identity.namespace,
            "case_id": result.case_id,
            "title": scenario.title,
            "verdict": result.verdict.value,
            "required": result.required,
            "semantic_review_required": result.semantic_review_required,
            "sanitized_request": dict(scenario.request),
            "expected_contract": dict(scenario.expected_contract),
            "validators": [dict(item) for item in scenario.validators],
            "observation": dict(result.observation),
            "assertions": [assertion.to_dict() for assertion in result.assertions],
            "deterministic_failures": list(result.deterministic_failures),
            "bounded_evidence": dict(result.bounded_evidence),
            "replay": _replay_contract(result),
            "error": result.error,
            "observed_at_utc": result.observed_at_utc,
        }
        sanitized = redact_value(record, self.canaries)
        persisted = dict(_bound_sanitized(sanitized.value))
        persisted["redaction_event_count"] = len(sanitized.events)
        line = json.dumps(persisted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return persisted

    def write_manifest(self, summary: CampaignSummary) -> None:
        document = {
            "schema": "jarvis.qa.campaign-manifest.v1",
            "campaign_id": summary.identity.campaign_id,
            "namespace": summary.identity.namespace,
            "evidence_file": self.path.name,
            "counts": summary.counts,
            "exit_code": summary.exit_code,
        }
        sanitized = redact_value(document, self.canaries)
        with self.manifest_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(sanitized.value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")


def load_evidence(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise ValueError(f"{path}:{line_number}: blank JSONL record")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: record must be an object")
            records.append(record)
    if not records:
        raise ValueError(f"{path}: evidence is empty")
    return records


def validate_evidence_records(records: list[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    required = {
        "schema",
        "campaign_id",
        "namespace",
        "case_id",
        "verdict",
        "required",
        "semantic_review_required",
        "sanitized_request",
        "validators",
        "observation",
        "assertions",
        "deterministic_failures",
        "bounded_evidence",
        "replay",
    }
    for index, record in enumerate(records, start=1):
        missing = sorted(required - record.keys())
        if missing:
            errors.append(f"record {index}: missing {', '.join(missing)}")
            continue
        if record.get("schema") != EVIDENCE_SCHEMA:
            errors.append(f"record {index}: unsupported schema")
        try:
            verdict = Verdict(str(record["verdict"]))
        except ValueError:
            errors.append(f"record {index}: invalid verdict")
            continue
        if not isinstance(record.get("required"), bool):
            errors.append(f"record {index}: required must be boolean")
        if not isinstance(record.get("semantic_review_required"), bool):
            errors.append(f"record {index}: semantic_review_required must be boolean")
        validators = record.get("validators")
        if not isinstance(validators, list) or not validators:
            errors.append(f"record {index}: validators must be a non-empty array")
        if not isinstance(record.get("observation"), Mapping):
            errors.append(f"record {index}: observation must be an object")
        if not isinstance(record.get("bounded_evidence"), Mapping):
            errors.append(f"record {index}: bounded_evidence must be an object")
        if not isinstance(record.get("sanitized_request"), Mapping):
            errors.append(f"record {index}: sanitized_request must be an object")
        assertions = record.get("assertions")
        if not isinstance(assertions, list):
            errors.append(f"record {index}: assertions must be an array")
            continue
        if any(
            not isinstance(item, Mapping)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("passed"), bool)
            for item in assertions
        ):
            errors.append(f"record {index}: invalid assertion shape")
            continue
        if verdict is Verdict.PASS:
            if not assertions:
                errors.append(f"record {index}: empty PASS")
            elif any(not bool(item.get("passed")) for item in assertions if isinstance(item, dict)):
                errors.append(f"record {index}: PASS contains failed assertion")
        failures = [
            str(item.get("name"))
            for item in assertions
            if isinstance(item, dict) and not bool(item.get("passed"))
        ]
        declared_failures = record["deterministic_failures"]
        if not isinstance(declared_failures, list) or any(
            not isinstance(item, str) for item in declared_failures
        ):
            errors.append(f"record {index}: deterministic_failures must be a string array")
            continue
        if sorted(failures) != sorted(declared_failures):
            errors.append(f"record {index}: deterministic_failures mismatch")
        if verdict is Verdict.FAIL and not failures:
            errors.append(f"record {index}: FAIL has no failed assertion")
        if verdict in {Verdict.PASS, Verdict.INCONCLUSIVE} and failures:
            errors.append(f"record {index}: {verdict.value} has deterministic failures")
        errors.extend(
            f"record {index}: {error}"
            for error in validate_replay_contract(record, verdict)
        )
        key = (str(record["campaign_id"]), str(record["case_id"]))
        if key in seen:
            errors.append(f"record {index}: duplicate campaign/case key")
        seen.add(key)
        leaked = credential_like_paths(record)
        if leaked:
            errors.append(f"record {index}: unredacted credential-like data at {', '.join(leaked)}")
    return errors


def validate_evidence_file(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    records = load_evidence(path)
    return records, validate_evidence_records(records)


def result_record(result: CaseResult) -> dict[str, Any]:
    """Small helper for tests and adapters that do not own an EvidenceStore."""

    document = asdict(result)
    document["verdict"] = result.verdict.value
    return document
