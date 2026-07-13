from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from qa.evidence import (
    EvidenceStore,
    load_evidence,
    validate_evidence_file,
    validate_evidence_records,
)
from qa.models import (
    EXIT_FAIL,
    EXIT_HARNESS_ERROR,
    EXIT_INCOMPLETE,
    EXIT_PASS,
    AssertionResult,
    CampaignIdentity,
    CampaignSummary,
    CaseResult,
    Scenario,
    Verdict,
)
from qa.redaction import REDACTED, redact_value
from qa.replay import replay_file
from qa.runner import (
    AllowlistedCliExecutor,
    AssuranceRunner,
    BlockedBySpecification,
    LoopbackHttpExecutor,
    validate_loopback_url,
)
from qa.scenario_loader import load_suite
from qa.validators import run_validators
from qa.validators.format_contracts import validate_json_schema

ROOT = Path(__file__).resolve().parents[2]
CALIBRATION = Path(__file__).parent / "fixtures" / "calibration_evidence.jsonl"


def make_scenario(
    scenario_id: str = "TEST-CASE-001",
    *,
    observation: dict[str, object] | None = None,
    validators: list[dict[str, object]] | None = None,
    semantic: bool = False,
) -> Scenario:
    return Scenario.from_dict(
        {
            "scenario_id": scenario_id,
            "title": "test scenario",
            "transport": "offline",
            "request": {"observation": observation or {"final": "Готово"}},
            "expected_contract": {},
            "validators": validators
            or [{"kind": "format_contract", "field": "final", "exact": "Готово"}],
            "semantic_review_required": semantic,
        }
    )


def make_result(verdict: Verdict, assertions: tuple[AssertionResult, ...]) -> CaseResult:
    return CaseResult("TEST-CASE-001", verdict, assertions)


def test_campaign_identity_is_unique_and_separate() -> None:
    first = CampaignIdentity.create("qa-test")
    second = CampaignIdentity.create("qa-test")
    assert first.campaign_id != second.campaign_id
    assert first.namespace != second.namespace
    assert first.campaign_id != first.namespace


def test_empty_pass_and_assertionless_fail_are_rejected() -> None:
    with pytest.raises(ValueError, match="PASS requires"):
        make_result(Verdict.PASS, ())
    with pytest.raises(ValueError, match="FAIL requires"):
        make_result(Verdict.FAIL, (AssertionResult("ok", True),))


def test_campaign_exit_code_lattice() -> None:
    identity = CampaignIdentity.create()
    passed = make_result(Verdict.PASS, (AssertionResult("ok", True),))
    failed = make_result(Verdict.FAIL, (AssertionResult("bad", False),))
    incomplete = make_result(Verdict.INCONCLUSIVE, (AssertionResult("ok", True),))
    errored = make_result(Verdict.ERROR, (AssertionResult("error", False),))
    assert CampaignSummary(identity, (passed,)).exit_code == EXIT_PASS
    assert CampaignSummary(identity, (failed,)).exit_code == EXIT_FAIL
    assert CampaignSummary(identity, (incomplete,)).exit_code == EXIT_INCOMPLETE
    assert CampaignSummary(identity, (errored,)).exit_code == EXIT_HARNESS_ERROR


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8000", "http://127.0.0.1:8000/", "https://[::1]:8443"],
)
def test_loopback_url_accepts_only_loopback(url: str) -> None:
    assert validate_loopback_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "ftp://127.0.0.1/file",
        "http://user:pass@127.0.0.1:8000",
        "http://127.0.0.1:8000/api",
        "http://127.0.0.1:8000?token=x",
    ],
)
def test_loopback_url_rejects_unsafe_forms(url: str) -> None:
    with pytest.raises(ValueError):
        validate_loopback_url(url)


def test_cli_executor_is_exact_allowlist_and_shell_false(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = AllowlistedCliExecutor({("safe-cli", "status")})
    with pytest.raises(BlockedBySpecification):
        executor.run({"args": ["safe-cli", "start"]})

    observed: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, '{"exit_code": 0}', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = executor.run({"args": ["safe-cli", "status"]})
    assert observed["shell"] is False
    assert observed["check"] is False
    assert result["machine_result"] == {"exit_code": 0}


def test_loopback_http_disables_proxy_and_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    observed: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        def close(self) -> None:
            observed["closed"] = True

    monkeypatch.setattr(httpx, "Client", FakeClient)
    executor = LoopbackHttpExecutor("http://127.0.0.2:8000")
    executor.close()
    assert observed["trust_env"] is False
    assert observed["follow_redirects"] is False
    assert observed["closed"] is True


def test_recursive_redaction_handles_keys_text_and_dynamic_canary() -> None:
    canary = "canary" + "://" + "qa-credential-7f3a"
    bearer = "disposable" + "-bearer-value"
    document = {
        "token": canary,
        "stdout": f"Authorization: Bearer {bearer} {canary}",
        "nested": [
            {"password": "not-a-real-password"},
            {"X-Jarvis-Api-Token": "disposable-value"},
        ],
    }
    result = redact_value(document, [canary])
    serialized = json.dumps(result.value)
    assert canary not in serialized
    assert bearer not in serialized
    assert result.value["token"] == REDACTED
    assert result.value["nested"][1]["X-Jarvis-Api-Token"] == REDACTED
    assert result.events


def test_evidence_is_exclusive_append_only_and_sanitized(tmp_path: Path) -> None:
    identity = CampaignIdentity.create("evidence-test")
    canary = "canary" + "://" + "exclusive-secret"
    store = EvidenceStore(tmp_path, identity, canaries=[canary])
    scenario = make_scenario(observation={"final": "Готово", "token": canary})
    result = CaseResult(
        scenario.scenario_id,
        Verdict.PASS,
        (AssertionResult("format.exact", True, "Готово", "Готово"),),
        observation={"final": "Готово", "token": canary},
    )
    store.append(scenario, result)
    assert canary not in store.path.read_text(encoding="utf-8")
    records, errors = validate_evidence_file(store.path)
    assert len(records) == 1
    assert errors == []
    with pytest.raises(FileExistsError):
        EvidenceStore(tmp_path, identity)


def test_response_integrity_detects_known_failures() -> None:
    observation = {
        "final": 'call:runtime.status\n{"tool":"runtime.status","arguments":{}}',
        "finals": ["", ""],
        "finish_reason": "length",
        "stream_terminal": False,
    }
    results = run_validators(observation, [{"kind": "response_integrity"}])
    failures = {result.name for result in results if not result.passed}
    assert failures == {
        "response.no_internal_markers",
        "response.single_final",
        "response.not_truncated",
    }


def test_format_language_count_and_json_schema() -> None:
    schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string", "const": "да"}},
        "additionalProperties": False,
    }
    results = run_validators(
        {"final": '{"answer":"да"}'},
        [{"kind": "format_contract", "json": True, "json_schema": schema}],
    )
    assert all(result.passed for result in results)
    exact = run_validators(
        {"final": "один два"},
        [
            {
                "kind": "format_contract",
                "exact": "один два",
                "language": "ru",
                "word_count": 2,
                "line_count": 1,
            }
        ],
    )
    assert all(result.passed for result in exact)
    assert validate_json_schema({"answer": "нет"}, schema)
    assert validate_json_schema("value", {"type": "string", "oneOf": []})


def test_ndjson_reconstruction_and_terminal_consistency() -> None:
    ndjson = "\n".join(
        [
            '{"type":"meta"}',
            '{"type":"delta","delta":"abc"}',
            '{"type":"done","answer":"abc"}',
        ]
    )
    passed = run_validators(
        {"ndjson": ndjson, "persisted_final": "abc"}, [{"kind": "stream_integrity"}]
    )
    assert all(result.passed for result in passed)
    failed = run_validators(
        {"ndjson": ndjson, "persisted_final": "different"},
        [{"kind": "stream_integrity"}],
    )
    assert "stream.terminal_equals_persisted" in {
        result.name for result in failed if not result.passed
    }


def test_artifact_path_hash_and_source_checks(tmp_path: Path) -> None:
    artifact = tmp_path / "output.txt"
    source = tmp_path / "source.txt"
    artifact.write_text("ok", encoding="utf-8")
    source.write_text("source", encoding="utf-8")
    digest = hashlib.sha256(b"ok").hexdigest()
    source_digest = hashlib.sha256(b"source").hexdigest()
    results = run_validators(
        {
            "artifact": {
                "path": str(artifact),
                "exists": False,
                "sha256": "fabricated-recorded-hash",
                "source_sha256_after": "fabricated-recorded-source-hash",
            }
        },
        [
            {
                "kind": "artifact",
                "expected_path": str(artifact),
                "expected_sha256": digest,
                "source_path": str(source),
                "source_sha256_before": source_digest,
            }
        ],
    )
    assert all(result.passed for result in results)


def test_artifact_validator_rejects_fabricated_recorded_file_claims(tmp_path: Path) -> None:
    artifact = tmp_path / "missing-output.txt"
    source = tmp_path / "missing-source.txt"
    fabricated_hash = hashlib.sha256(b"fabricated").hexdigest()
    results = run_validators(
        {
            "artifact": {
                "path": str(artifact),
                "exists": True,
                "sha256": fabricated_hash,
                "source_sha256_after": fabricated_hash,
            }
        },
        [
            {
                "kind": "artifact",
                "expected_path": str(artifact),
                "expected_sha256": fabricated_hash,
                "source_path": str(source),
                "source_sha256_before": fabricated_hash,
            }
        ],
    )
    outcomes = {result.name: result.passed for result in results}
    assert outcomes["artifact.contract_complete"] is True
    assert outcomes["artifact.exact_path"] is True
    assert outcomes["artifact.exists"] is False
    assert outcomes["artifact.sha256"] is False
    assert outcomes["artifact.source_unchanged"] is False


def test_state_identity_claim_canary_and_exit_mismatch() -> None:
    identity = run_validators(
        {
            "runtime_id": "new",
            "conversation_id": "c-new",
            "transcript_runtime_id": "old",
            "transcript_conversation_id": "c-old",
        },
        [{"kind": "identity", "expected_runtime_id": "new"}],
    )
    assert "identity.transcript_runtime" in {item.name for item in identity if not item.passed}
    claims = run_validators(
        {"claimed_state": {"created": True}, "observed_state": {"created": False}},
        [{"kind": "claimed_state"}],
    )
    assert not claims[0].passed
    secret = run_validators(
        {"stdout": REDACTED, "prewrite_redaction_events": 1},
        [{"kind": "canary_absence"}],
    )
    assert not secret[0].passed
    exit_result = run_validators(
        {"process_exit_code": 0, "machine_result": {"exit_code": 1}},
        [{"kind": "exit_consistency"}],
    )
    assert not exit_result[0].passed


def test_runner_writes_after_each_case_and_classifies_semantic(tmp_path: Path) -> None:
    identity = CampaignIdentity.create("runner-test")
    store = EvidenceStore(tmp_path, identity)
    runner = AssuranceRunner(identity, store)
    passed = runner.run_case(make_scenario("RUN-PASS-001"))
    semantic = runner.run_case(make_scenario("RUN-SEMANTIC-001", semantic=True))
    failed = runner.run_case(
        make_scenario("RUN-FAIL-001", validators=[{"kind": "unknown-validator"}])
    )
    assert [passed.verdict, semantic.verdict, failed.verdict] == [
        Verdict.PASS,
        Verdict.INCONCLUSIVE,
        Verdict.FAIL,
    ]
    assert len(load_evidence(store.path)) == 3


def test_runner_classifications_have_typed_replay_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = CampaignIdentity.create("classification-test")
    store = EvidenceStore(tmp_path, identity)
    runner = AssuranceRunner(identity, store)

    def scenario(
        scenario_id: str,
        transport: str,
        request: dict[str, object],
        **extra: object,
    ) -> Scenario:
        return Scenario.from_dict(
            {
                "scenario_id": scenario_id,
                "title": "classification replay",
                "transport": transport,
                "request": request,
                "expected_contract": {},
                "validators": [{"kind": "response_integrity"}],
                **extra,
            }
        )

    blocked_env = runner.run_case(
        scenario("CLASS-ENV-001", "http", {"method": "GET", "path": "/health"})
    )
    runner.http_executor = LoopbackHttpExecutor(
        "http://127.0.0.1:1", allowed_routes=set()
    )
    blocked_spec = runner.run_case(
        scenario("CLASS-SPEC-001", "http", {"method": "GET", "path": "/health"})
    )
    skipped = runner.run_case(
        scenario(
            "CLASS-SKIP-001",
            "offline",
            {"observation": {"final": "unused"}},
            required=False,
            skip_reason="optional fixture is unavailable",
        )
    )
    monkeypatch.setattr("qa.runner.run_validators", lambda *_args: [])
    errored = runner.run_case(
        scenario(
            "CLASS-ERROR-001",
            "offline",
            {"observation": {"final": "no assertions"}},
        )
    )
    runner.close()

    assert [blocked_env.verdict, blocked_spec.verdict, skipped.verdict, errored.verdict] == [
        Verdict.BLOCKED_BY_ENV,
        Verdict.BLOCKED_BY_SPEC,
        Verdict.SKIP,
        Verdict.ERROR,
    ]
    records, errors = validate_evidence_file(store.path)
    assert errors == []
    assert {record["replay"]["mode"] for record in records} == {"classification"}
    replay = replay_file(store.path)
    assert replay.errors == ()
    assert replay.mismatches == ()
    assert replay.counts == {
        "PASS": 0,
        "FAIL": 0,
        "INCONCLUSIVE": 0,
        "BLOCKED_BY_ENV": 1,
        "BLOCKED_BY_SPEC": 1,
        "SKIP": 1,
        "ERROR": 1,
    }

    records[0]["replay"]["reason"] = "fabricated classification"
    assert any("reason must match error" in error for error in validate_evidence_records(records))


def test_skip_reason_requires_an_optional_scenario() -> None:
    base = {
        "scenario_id": "CLASS-SKIP-INVALID",
        "title": "invalid skip",
        "transport": "offline",
        "request": {"observation": {"final": "unused"}},
        "expected_contract": {},
        "validators": [{"kind": "response_integrity"}],
        "skip_reason": "requested skip",
    }
    with pytest.raises(ValueError, match="optional scenario"):
        Scenario.from_dict(base)
    with pytest.raises(ValueError, match="non-empty"):
        Scenario.from_dict({**base, "required": False, "skip_reason": ""})


def test_committed_suite_and_schema_files_are_valid_json() -> None:
    scenarios = load_suite(ROOT / "qa" / "suites" / "operator_core")
    assert [scenario.scenario_id for scenario in scenarios] == ["CORE-RESPONSE-001"]
    for path in sorted((ROOT / "qa" / "schemas").glob("*.json")):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


def test_calibration_evidence_validates_and_replays_exactly() -> None:
    records, errors = validate_evidence_file(CALIBRATION)
    assert len(records) == 8
    assert errors == []
    summary = replay_file(CALIBRATION)
    assert summary.exit_code == EXIT_PASS
    assert summary.mismatches == ()
    assert summary.counts["PASS"] == 1
    assert summary.counts["FAIL"] == 6
    assert summary.counts["INCONCLUSIVE"] == 1
    failures = {case.case_id: set(case.deterministic_failures) for case in summary.cases}
    assert "response.no_internal_markers" in failures["CAL-FAIL-TOOL-ENVELOPE"]
    assert "response.non_empty_final" in failures["CAL-FAIL-EMPTY-DUPLICATE"]
    assert "state.canary_absent" in failures["CAL-FAIL-CANARY"]
    assert "state.exit_code_matches_result" in failures["CAL-FAIL-EXIT-MISMATCH"]
    assert "artifact.exact_path" in failures["CAL-FAIL-ARTIFACT"]
    assert "identity.transcript_runtime" in failures["CAL-FAIL-CROSS-RUNTIME"]
