from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import qa.safe_paths as safe_paths
from qa import _trusted_jarvis_cli
from qa.evidence import (
    EVIDENCE_ALLOWED_FIELDS,
    EVIDENCE_REQUIRED_FIELDS,
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
    CliCommandSpec,
    LoopbackHttpExecutor,
    validate_loopback_url,
)
from qa.scenario_loader import load_scenario_file, load_suite
from qa.validators import run_validators
from qa.validators.context import ValidationContext
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
    executor = AllowlistedCliExecutor(
        {CliCommandSpec(("safe-cli", "status"), ("status",))}
    )
    with pytest.raises(BlockedBySpecification):
        executor.run({"args": ["safe-cli", "start"]})
    with pytest.raises(BlockedBySpecification):
        executor.run({"args": ["safe-cli", "status"], "cwd": "."})

    observed: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, '{"exit_code": 0}', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = executor.run({"args": ["safe-cli", "status"]})
    command = observed["args"]
    assert isinstance(command, list)
    assert command[0] == str(executor.interpreter)
    assert command[1:5] == ["-I", "-S", "-X", "utf8"]
    assert command[5] == str(executor.launcher)
    assert command[6:] == ["status"]
    assert observed["shell"] is False
    assert observed["check"] is False
    assert observed["cwd"] == str(ROOT)
    assert result["machine_result"] == {"exit_code": 0}


def test_cli_executor_ignores_hostile_process_and_import_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {
        "PATH": str(tmp_path),
        "PYTHONPATH": str(tmp_path / "imports"),
        "PYTHONHOME": str(tmp_path / "home"),
        "PYTHONSTARTUP": str(tmp_path / "startup.py"),
        "PYTHONUSERBASE": str(tmp_path / "user"),
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    captured: dict[str, object] = {}

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, '{"exit_code":0}', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = AllowlistedCliExecutor(
        {CliCommandSpec(("safe-cli", "status"), ("status",))}
    )
    with pytest.raises(BlockedBySpecification):
        executor.run(
            {
                "args": [
                    str(tmp_path / "python.exe"),
                    "-m",
                    "substituted.module",
                    "status",
                ]
            }
        )
    executor.run({"args": ["safe-cli", "status"]})

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    hostile_names = {"PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"}
    assert not hostile_names.intersection(child_env)
    command = captured["args"]
    assert isinstance(command, list)
    assert Path(command[0]).is_absolute()
    assert str(tmp_path) not in command
    assert captured["cwd"] == str(ROOT)


def test_trusted_launcher_drops_bootstrap_import_hijack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = tmp_path / "bootstrap-hijack"
    hostile.mkdir()
    monkeypatch.setattr(
        _trusted_jarvis_cli.sys,
        "path",
        [str(hostile), *list(_trusted_jarvis_cli.sys.path)],
    )
    _trusted_jarvis_cli._configure_trusted_imports()
    assert str(hostile) not in _trusted_jarvis_cli.sys.path
    assert _trusted_jarvis_cli.sys.path[0] == str(ROOT / "backend" / "src")


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


@pytest.mark.parametrize("non_json", ["NaN", "Infinity", "-Infinity"])
def test_format_contract_rejects_nonfinite_json_constants(non_json: str) -> None:
    results = run_validators(
        {"final": non_json},
        [{"kind": "format_contract", "json": True}],
    )
    outcomes = {result.name: result for result in results}
    assert outcomes["format.valid_json"].passed is False


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
                "path": "output.txt",
                "exists": False,
                "sha256": "fabricated-recorded-hash",
                "source_sha256_after": "fabricated-recorded-source-hash",
            }
        },
        [
            {
                "kind": "artifact",
                "root": "outputs",
                "expected_path": "output.txt",
                "expected_sha256": digest,
                "source_root": "sources",
                "source_path": "source.txt",
                "source_sha256_before": source_digest,
            }
        ],
        context=ValidationContext(
            artifact_roots={"outputs": tmp_path, "sources": tmp_path}
        ),
    )
    assert all(result.passed for result in results)


def test_artifact_validator_rejects_fabricated_recorded_file_claims(tmp_path: Path) -> None:
    fabricated_hash = hashlib.sha256(b"fabricated").hexdigest()
    results = run_validators(
        {
            "artifact": {
                "path": "missing-output.txt",
                "exists": True,
                "sha256": fabricated_hash,
                "source_sha256_after": fabricated_hash,
            }
        },
        [
            {
                "kind": "artifact",
                "root": "outputs",
                "expected_path": "missing-output.txt",
                "expected_sha256": fabricated_hash,
                "source_root": "sources",
                "source_path": "missing-source.txt",
                "source_sha256_before": fabricated_hash,
            }
        ],
        context=ValidationContext(
            artifact_roots={"outputs": tmp_path, "sources": tmp_path}
        ),
    )
    outcomes = {result.name: result.passed for result in results}
    assert outcomes["artifact.contract_complete"] is True
    assert outcomes["artifact.exact_path"] is True
    assert outcomes["artifact.exists"] is False
    assert outcomes["artifact.sha256"] is False
    assert outcomes["artifact.source_unchanged"] is False


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.txt",
        "/absolute.txt",
        "C:/absolute.txt",
        "C:alternate.txt",
        "folder\\escape.txt",
        "//server/share.txt",
    ],
)
def test_artifact_validator_rejects_cross_platform_escapes_without_hash_disclosure(
    tmp_path: Path, unsafe_path: str
) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    results = run_validators(
        {"artifact": {"path": unsafe_path}},
        [
            {
                "kind": "artifact",
                "root": "outputs",
                "expected_path": unsafe_path,
                "expected_sha256": hashlib.sha256(b"outside").hexdigest(),
            }
        ],
        context=ValidationContext(artifact_roots={"outputs": tmp_path}),
    )
    outcomes = {result.name: result for result in results}
    assert outcomes["artifact.contract_complete"].passed is False
    assert outcomes["artifact.safe_regular_file"].passed is False
    assert outcomes["artifact.sha256"].actual is None


def test_validation_context_rejects_relative_and_unc_roots() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ValidationContext(artifact_roots={"root": Path("relative")})
    with pytest.raises(ValueError, match="absolute"):
        ValidationContext(artifact_roots={"root": Path(r"\\server\share")})


def test_artifact_validator_enforces_size_regular_file_and_independent_source(
    tmp_path: Path,
) -> None:
    (tmp_path / "large.bin").write_bytes(b"1234")
    (tmp_path / "source.txt").write_text("source", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    context = ValidationContext(artifact_roots={"root": tmp_path}, max_artifact_bytes=3)
    large = run_validators(
        {"artifact": {"path": "large.bin"}},
        [
            {
                "kind": "artifact",
                "root": "root",
                "expected_path": "large.bin",
                "expected_sha256": hashlib.sha256(b"1234").hexdigest(),
            }
        ],
        context=context,
    )
    assert not {item.name: item.passed for item in large}["artifact.safe_regular_file"]
    directory = run_validators(
        {"artifact": {"path": "folder"}},
        [
            {
                "kind": "artifact",
                "root": "root",
                "expected_path": "folder",
                "expected_sha256": hashlib.sha256(b"").hexdigest(),
            }
        ],
        context=context,
    )
    assert not {item.name: item.passed for item in directory}["artifact.safe_regular_file"]

    artifact = tmp_path / "artifact.txt"
    artifact.write_text("ok", encoding="utf-8")
    independent = run_validators(
        {"artifact": {"path": "artifact.txt"}},
        [
            {
                "kind": "artifact",
                "root": "root",
                "expected_path": "artifact.txt",
                "expected_sha256": hashlib.sha256(b"ok").hexdigest(),
                "source_root": "root",
                "source_path": "missing-source.txt",
                "source_sha256_before": hashlib.sha256(b"source").hexdigest(),
            }
        ],
        context=ValidationContext(artifact_roots={"root": tmp_path}),
    )
    independent_outcomes = {item.name: item.passed for item in independent}
    assert independent_outcomes["artifact.sha256"] is True
    assert independent_outcomes["artifact.source_safe_regular_file"] is False


def test_artifact_validator_rejects_symlink_or_reparse_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-reparse.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    results = run_validators(
        {"artifact": {"path": "linked.txt"}},
        [
            {
                "kind": "artifact",
                "root": "root",
                "expected_path": "linked.txt",
                "expected_sha256": hashlib.sha256(b"outside").hexdigest(),
            }
        ],
        context=ValidationContext(artifact_roots={"root": tmp_path}),
    )
    outcomes = {item.name: item for item in results}
    assert outcomes["artifact.safe_regular_file"].passed is False
    assert outcomes["artifact.sha256"].actual is None


def test_artifact_validator_rejects_simulated_reparse_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "component.txt"
    target.write_text("bounded", encoding="utf-8")
    target_stat = os.lstat(target)
    original = safe_paths._is_reparse

    def simulated(stat_result: os.stat_result) -> bool:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
        ) == (target_stat.st_dev, target_stat.st_ino) or original(stat_result)

    monkeypatch.setattr(safe_paths, "_is_reparse", simulated)
    results = run_validators(
        {"artifact": {"path": "component.txt"}},
        [
            {
                "kind": "artifact",
                "root": "root",
                "expected_path": "component.txt",
                "expected_sha256": hashlib.sha256(b"bounded").hexdigest(),
            }
        ],
        context=ValidationContext(artifact_roots={"root": tmp_path}),
    )
    outcomes = {item.name: item for item in results}
    assert outcomes["artifact.safe_regular_file"].passed is False
    assert outcomes["artifact.sha256"].actual is None


def test_bounded_digest_rejects_open_handle_escape_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "inside.txt"
    outside = tmp_path.parent / "outside-open-handle.txt"
    target.write_text("inside", encoding="utf-8")
    outside.write_text("outside", encoding="utf-8")
    real_open = safe_paths.os.open
    real_lstat = safe_paths.os.lstat
    outside_descriptor: list[int] = []

    def substituted_open(path: object, flags: int) -> int:
        descriptor = real_open(outside, flags)
        outside_descriptor.append(descriptor)
        return descriptor

    read_called = False
    real_read = safe_paths.os.read

    def observed_read(descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return real_read(descriptor, size)

    def substituted_lstat(path: object) -> os.stat_result:
        if Path(path) == target:
            return real_lstat(outside)
        return real_lstat(path)

    monkeypatch.setattr(safe_paths.os, "open", substituted_open)
    monkeypatch.setattr(safe_paths.os, "read", observed_read)
    monkeypatch.setattr(safe_paths.os, "lstat", substituted_lstat)
    with pytest.raises(safe_paths.SafePathError, match="escapes"):
        safe_paths.bounded_file_digest(tmp_path, "inside.txt")
    assert outside_descriptor
    assert read_called is False


def test_bounded_digest_fails_closed_without_open_handle_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "identity-required.txt"
    target.write_text("bounded", encoding="utf-8")
    read_called = False
    real_read = safe_paths.os.read

    def observed_read(descriptor: int, size: int) -> bytes:
        nonlocal read_called
        read_called = True
        return real_read(descriptor, size)

    monkeypatch.setattr(safe_paths, "_opened_file_path", lambda descriptor: None)
    monkeypatch.setattr(safe_paths.os, "read", observed_read)
    with pytest.raises(safe_paths.SafePathError, match="cannot be verified"):
        safe_paths.bounded_file_digest(tmp_path, "identity-required.txt")
    assert read_called is False


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
        make_scenario(
            "RUN-FAIL-001",
            validators=[{"kind": "format_contract", "exact": "different"}],
        )
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


def test_scenario_contract_rejects_unknown_fields_and_type_coercion() -> None:
    valid = {
        "scenario_id": "STRICT-SCENARIO-001",
        "title": "strict scenario",
        "transport": "offline",
        "request": {"observation": {"final": "ok"}},
        "expected_contract": {},
        "validators": [{"kind": "format_contract", "exact": "ok"}],
        "required": True,
        "semantic_review_required": False,
    }
    assert Scenario.from_dict(valid).required is True
    for malformed in (
        {**valid, "semantic_reveiw_required": True},
        {**valid, "required": "true"},
        {
            **valid,
            "validators": [{"kind": "format_contract", "exact": "ok", "typo": True}],
        },
        {**valid, "validators": {"kind": "format_contract", "exact": "ok"}},
    ):
        with pytest.raises(ValueError, match="invalid scenario contract"):
            Scenario.from_dict(malformed)


def test_strict_json_schema_rejects_malformed_contracts_and_nested_unknowns() -> None:
    assert validate_json_schema({}, {"type": "object", "required": "must_exist"})
    assert validate_json_schema(True, {"type": "integer"})
    assert validate_json_schema(
        {"outer": {"known": 1, "unknown": 2}},
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["outer"],
            "properties": {
                "outer": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"known": {"type": "integer"}},
                }
            },
        },
    )
    assert validate_json_schema({}, {"type": "object", "mystery": True})
    assert validate_json_schema([1, 1.0], {"type": "array", "uniqueItems": True})
    assert validate_json_schema(
        [{"value": False}, {"value": 0}],
        {"type": "array", "uniqueItems": True},
    ) == []
    assert validate_json_schema("value", {"enum": [1, 1.0]})
    assert validate_json_schema("ok", {"type": "string", "title": 7})
    assert validate_json_schema(
        "ok", {"type": "string", "$id": "not an absolute uri"}
    )
    assert validate_json_schema("ok", {"type": "string", "format": ["uri"]})
    assert validate_json_schema("ok", {"type": "string", "minimum": 1})
    assert validate_json_schema("ok", {"type": "string", "enum": [1]})
    malformed_results = run_validators(
        {"final": '"ok"'},
        [
            {
                "kind": "format_contract",
                "json": True,
                "json_schema": {"type": "string", "title": 7},
            }
        ],
    )
    malformed_outcomes = {item.name: item.passed for item in malformed_results}
    assert malformed_outcomes["format.valid_json"] is True
    assert malformed_outcomes["format.json_schema"] is False


def test_scenario_loader_rejects_nonfinite_json(tmp_path: Path) -> None:
    path = tmp_path / "nonfinite.json"
    path.write_text(
        '{"scenario_id":"NONFINITE-001","title":"bad","transport":"offline",'
        '"request":{"observation":{"unused":NaN}},"expected_contract":{},'
        '"validators":[{"kind":"format_contract","exact":"ok"}]}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-finite JSON constant"):
        load_scenario_file(path)


def test_evidence_loader_rejects_nonfinite_json(tmp_path: Path) -> None:
    record = json.loads(CALIBRATION.read_text(encoding="utf-8").splitlines()[0])
    record["bounded_evidence"]["unused"] = float("nan")
    path = tmp_path / "nonfinite.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite JSON constant"):
        validate_evidence_file(path)


def test_committed_suite_and_schema_files_are_valid_json() -> None:
    scenarios = load_suite(ROOT / "qa" / "suites" / "operator_core")
    assert [scenario.scenario_id for scenario in scenarios] == ["CORE-RESPONSE-001"]
    for path in sorted((ROOT / "qa" / "schemas").glob("*.json")):
        assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    evidence_schema = json.loads(
        (ROOT / "qa" / "schemas" / "evidence.schema.json").read_text(encoding="utf-8")
    )
    assert set(evidence_schema["required"]) == EVIDENCE_REQUIRED_FIELDS
    assert set(evidence_schema["properties"]) == EVIDENCE_ALLOWED_FIELDS


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
