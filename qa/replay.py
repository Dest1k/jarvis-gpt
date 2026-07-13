"""Offline deterministic replay of previously sanitized evidence."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from .evidence import (
    EvidenceIntegrity,
    canonical_record_sha256,
    validate_replay_contract,
    verify_evidence_bundle,
)
from .models import EXIT_FAIL, EXIT_HARNESS_ERROR, EXIT_PASS, Verdict
from .output import sanitize_output, write_json_exclusive
from .safe_paths import (
    MAX_CONFIGURABLE_FILE_BYTES,
    bounded_file_bytes,
    canonical_directory,
    safe_output_path,
    validate_case_id,
)
from .validators import run_validators
from .validators.context import ValidationContext

_REPLAY_VERIFICATION_TOKEN = object()


@dataclass(frozen=True, slots=True)
class ReplayCase:
    case_id: str
    recorded_verdict: Verdict
    replayed_verdict: Verdict
    deterministic_failures: tuple[str, ...]
    source_record_sha256: str
    source_record_canonical_sha256: str

    @property
    def matches(self) -> bool:
        return self.recorded_verdict is self.replayed_verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "recorded_verdict": self.recorded_verdict.value,
            "replayed_verdict": self.replayed_verdict.value,
            "deterministic_failures": list(self.deterministic_failures),
            "source_record_sha256": self.source_record_sha256,
            "source_record_canonical_sha256": self.source_record_canonical_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReplayCase:
        fields = {
            "case_id",
            "recorded_verdict",
            "replayed_verdict",
            "deterministic_failures",
            "source_record_sha256",
            "source_record_canonical_sha256",
        }
        if set(data) != fields:
            raise ValueError("replay case fields are incomplete or unexpected")
        case_id = validate_case_id(data.get("case_id"))
        failures = data.get("deterministic_failures")
        if (
            not isinstance(failures, list)
            or any(not isinstance(item, str) or not item for item in failures)
            or len(failures) != len(set(failures))
        ):
            raise ValueError("replay case deterministic failures are invalid")
        digests: list[str] = []
        for field in ("source_record_sha256", "source_record_canonical_sha256"):
            value = data.get(field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"replay case {field} is invalid")
            digests.append(value)
        return cls(
            case_id=case_id,
            recorded_verdict=Verdict(data["recorded_verdict"]),
            replayed_verdict=Verdict(data["replayed_verdict"]),
            deterministic_failures=tuple(failures),
            source_record_sha256=digests[0],
            source_record_canonical_sha256=digests[1],
        )


@dataclass(frozen=True, slots=True)
class ReplaySummary:
    cases: tuple[ReplayCase, ...]
    errors: tuple[str, ...] = ()
    evidence_sha256: str = ""
    manifest_sha256: str = ""
    terminal_chain_sha256: str = ""
    replay_digest: str = ""
    _verification_token: object | None = dataclass_field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

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

    def _digest_body(self) -> dict[str, Any]:
        return {
            "schema": "jarvis.qa.replay-result.v2",
            "evidence_sha256": self.evidence_sha256,
            "manifest_sha256": self.manifest_sha256,
            "terminal_chain_sha256": self.terminal_chain_sha256,
            "cases": [case.to_dict() for case in self.cases],
            "errors": list(self.errors),
        }

    def expected_digest(self) -> str:
        payload = json.dumps(
            self._digest_body(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @property
    def integrity_verified(self) -> bool:
        no_failure_verdicts = {Verdict.PASS, Verdict.INCONCLUSIVE, Verdict.SKIP}
        failure_verdicts = {
            Verdict.FAIL,
            Verdict.BLOCKED_BY_ENV,
            Verdict.BLOCKED_BY_SPEC,
            Verdict.ERROR,
        }
        return bool(
            not self.errors
            and self._verification_token is _REPLAY_VERIFICATION_TOKEN
            and bool(self.cases)
            and not self.mismatches
            and self.evidence_sha256
            and self.manifest_sha256
            and self.terminal_chain_sha256
            and self.replay_digest == self.expected_digest()
            and len({case.case_id for case in self.cases}) == len(self.cases)
            and all(
                case.source_record_sha256 and case.source_record_canonical_sha256
                for case in self.cases
            )
            and all(
                (
                    case.replayed_verdict in no_failure_verdicts
                    and not case.deterministic_failures
                )
                or (
                    case.replayed_verdict in failure_verdicts
                    and bool(case.deterministic_failures)
                )
                for case in self.cases
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._digest_body(),
            "counts": self.counts,
            "mismatches": list(self.mismatches),
            "exit_code": self.exit_code,
            "replay_digest": self.replay_digest,
        }

    @classmethod
    def create(
        cls,
        cases: tuple[ReplayCase, ...],
        errors: tuple[str, ...] = (),
        integrity: EvidenceIntegrity | None = None,
    ) -> ReplaySummary:
        summary = cls(
            cases=cases,
            errors=errors,
            evidence_sha256=integrity.evidence_sha256 if integrity else "",
            manifest_sha256=integrity.manifest_sha256 if integrity else "",
            terminal_chain_sha256=(integrity.terminal_chain_sha256 if integrity else ""),
        )
        return cls(
            cases=summary.cases,
            errors=summary.errors,
            evidence_sha256=summary.evidence_sha256,
            manifest_sha256=summary.manifest_sha256,
            terminal_chain_sha256=summary.terminal_chain_sha256,
            replay_digest=summary.expected_digest(),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReplaySummary:
        fields = {
            "schema",
            "evidence_sha256",
            "manifest_sha256",
            "terminal_chain_sha256",
            "cases",
            "errors",
            "counts",
            "mismatches",
            "exit_code",
            "replay_digest",
        }
        if set(data) != fields or data.get("schema") != "jarvis.qa.replay-result.v2":
            raise ValueError("replay report fields or schema are invalid")
        raw_cases = data.get("cases")
        raw_errors = data.get("errors")
        if not isinstance(raw_cases, list) or any(
            not isinstance(item, Mapping) for item in raw_cases
        ):
            raise ValueError("replay report cases must be objects")
        if not isinstance(raw_errors, list) or any(
            not isinstance(item, str) or not item for item in raw_errors
        ):
            raise ValueError("replay report errors must be non-empty strings")
        digest_values: list[str] = []
        for field in (
            "evidence_sha256",
            "manifest_sha256",
            "terminal_chain_sha256",
            "replay_digest",
        ):
            value = data.get(field)
            allow_empty = field != "replay_digest"
            if not isinstance(value, str) or (
                value == "" and not allow_empty
            ) or (
                value != ""
                and (
                    len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)
                )
            ):
                raise ValueError(f"replay report {field} is invalid")
            digest_values.append(value)
        summary = cls(
            cases=tuple(ReplayCase.from_dict(item) for item in raw_cases),
            errors=tuple(raw_errors),
            evidence_sha256=digest_values[0],
            manifest_sha256=digest_values[1],
            terminal_chain_sha256=digest_values[2],
            replay_digest=digest_values[3],
        )
        if summary.replay_digest != summary.expected_digest():
            raise ValueError("replay report digest mismatch")
        if data.get("counts") != summary.counts:
            raise ValueError("replay report counts mismatch")
        if data.get("mismatches") != list(summary.mismatches):
            raise ValueError("replay report mismatches mismatch")
        if data.get("exit_code") != summary.exit_code:
            raise ValueError("replay report exit code mismatch")
        return summary


def replay_record(
    record: Mapping[str, Any],
    *,
    source_record_sha256: str,
    source_record_canonical_sha256: str,
    context: ValidationContext | None = None,
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
    return ReplayCase(
        case_id,
        recorded,
        replayed,
        failures,
        source_record_sha256,
        source_record_canonical_sha256,
    )


def replay_records(
    records: list[Mapping[str, Any]],
    integrity: EvidenceIntegrity,
    *,
    context: ValidationContext | None = None,
) -> ReplaySummary:
    if not integrity.provenance_verified:
        return ReplaySummary.create(
            (),
            ("evidence integrity provenance is not verified",),
        )
    canonical_digests = tuple(canonical_record_sha256(record) for record in records)
    if (
        len(records) != len(integrity.record_sha256s)
        or canonical_digests != integrity.record_canonical_sha256s
    ):
        return ReplaySummary.create(
            (),
            ("verified evidence record binding mismatch",),
        )
    cases: list[ReplayCase] = []
    errors: list[str] = []
    for index, (record, record_digest, canonical_digest) in enumerate(
        zip(
            records,
            integrity.record_sha256s,
            integrity.record_canonical_sha256s,
            strict=True,
        ),
        start=1,
    ):
        try:
            cases.append(
                replay_record(
                    record,
                    source_record_sha256=record_digest,
                    source_record_canonical_sha256=canonical_digest,
                    context=context,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"record {index}: {type(exc).__name__}: {exc}")
    verified = ReplaySummary.create(tuple(cases), tuple(errors), integrity)
    object.__setattr__(
        verified,
        "_verification_token",
        _REPLAY_VERIFICATION_TOKEN,
    )
    return verified


def replay_file(
    path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    context: ValidationContext | None = None,
) -> ReplaySummary:
    try:
        records, validation_errors, integrity = verify_evidence_bundle(
            path,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        if validation_errors:
            return ReplaySummary.create((), tuple(validation_errors))
        if integrity is None:
            return ReplaySummary.create((), ("evidence integrity was not established",))
        return replay_records(records, integrity, context=context)
    except (OSError, ValueError) as exc:
        return ReplaySummary.create((), (f"{type(exc).__name__}: {exc}",))


def load_replay_report(
    path: Path,
    *,
    evidence_path: Path | None = None,
    expected_manifest_sha256: str | None = None,
    context: ValidationContext | None = None,
) -> ReplaySummary:
    """Load a replay report and optionally reverify it against anchored evidence."""

    root = canonical_directory(path.parent)
    payload = bounded_file_bytes(
        root,
        path.name,
        max_bytes=MAX_CONFIGURABLE_FILE_BYTES,
    )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("replay report is not valid UTF-8") from exc

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("replay report contains duplicate keys")
            document[key] = value
        return document

    try:
        document = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value}")
            ),
        )
    except (json.JSONDecodeError, ValueError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise ValueError(f"invalid replay report JSON: {detail}") from exc
    if not isinstance(document, Mapping):
        raise ValueError("replay report must be an object")
    persisted = ReplaySummary.from_dict(document)
    if evidence_path is None:
        if expected_manifest_sha256 is not None:
            raise ValueError("replay verification requires an evidence path")
        return persisted
    fresh = replay_file(
        evidence_path,
        expected_manifest_sha256=expected_manifest_sha256,
        context=context,
    )
    if not fresh.integrity_verified:
        raise ValueError("persisted replay source evidence is not verified")
    if persisted.to_dict() != fresh.to_dict():
        raise ValueError("persisted replay report substitution detected")
    return fresh


def write_replay_report(
    path: Path,
    summary: ReplaySummary,
    *,
    canaries: Iterable[str] = (),
) -> Path:
    """Persist a digest-stable replay report through the common safe boundary."""

    if not summary.integrity_verified:
        raise ValueError("replay report requires verified evidence provenance")
    document = summary.to_dict()
    canary_values = tuple(canaries)
    if sanitize_output(document, canaries=canary_values).value != document:
        raise ValueError("replay report was not sanitized before digest calculation")
    root = canonical_directory(path.parent, create=True)
    target = safe_output_path(root, path.name)
    write_json_exclusive(target, document, canaries=canary_values)
    return target
