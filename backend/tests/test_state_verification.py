from __future__ import annotations

import asyncio
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from jarvis_gpt import state_verification
from jarvis_gpt.execution_actions import (
    AtomicActionExecutor,
    DeleteFileAction,
    MoveFileAction,
    PathPolicy,
    ProcessAction,
    ProcessSignal,
    ReadFileAction,
    RegistryHive,
    RegistrySetAction,
    RegistryValueKind,
    TcpProbeAction,
    TerminateOwnedProcessAction,
    WriteFileAction,
)
from jarvis_gpt.execution_process import ProcessRequest
from jarvis_gpt.state_verification import (
    GateStatus,
    PathExpectation,
    RiskLevel,
    SafeGate,
    StateVerifier,
    TcpExpectation,
    VerificationExpectation,
    VerificationStatus,
)


def _policy(tmp_path: Path) -> PathPolicy:
    return PathPolicy((tmp_path,))


def test_write_verification_restats_hashes_and_validates_json(tmp_path):
    target = tmp_path / "config.json"
    target.write_bytes(b'{"enabled": true}')
    verifier = StateVerifier(path_policy=_policy(tmp_path))

    result = asyncio.run(
        verifier.verify(WriteFileAction(target, content=b'{"enabled": true}'))
    )

    assert result.ok is True
    assert result.status is VerificationStatus.PASSED
    assert {item.source for item in result.evidence} == {
        "filesystem",
        "syntax_validator",
    }
    assert any(item.assertion == "file sha256" for item in result.evidence)


@pytest.mark.parametrize(
    ("name", "content"),
    (("config.json", b"{"), ("config.toml", b"value = ["), ("config.yaml", b"a: [")),
)
def test_invalid_config_syntax_fails_closed(tmp_path, name, content):
    target = tmp_path / name
    target.write_bytes(content)
    verifier = StateVerifier(path_policy=_policy(tmp_path))

    result = asyncio.run(verifier.verify(WriteFileAction(target, content=content)))

    assert result.ok is False
    assert result.status is VerificationStatus.FAILED
    assert any(
        item.source == "syntax_validator" and not item.passed for item in result.evidence
    )


def test_explicit_syntax_assertion_fails_when_no_validator_exists(tmp_path):
    target = tmp_path / "service.conf"
    target.write_text("opaque syntax", encoding="utf-8")
    verifier = StateVerifier(path_policy=_policy(tmp_path))

    result = asyncio.run(
        verifier.verify(
            ReadFileAction(target),
            expectation=VerificationExpectation(
                paths=(PathExpectation(target, syntax_valid=True),)
            ),
        )
    )

    assert result.ok is False
    assert any(
        item.source == "syntax_validator"
        and item.error == "no syntax validator is registered for this file type"
        for item in result.evidence
    )


def test_move_verification_binds_destination_to_pre_action_source_hash(tmp_path):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_bytes(b"original")
    policy = _policy(tmp_path)
    executor = AtomicActionExecutor(path_policy=policy)
    action = MoveFileAction(source, destination)
    feedback = asyncio.run(executor.execute(action))
    destination.write_bytes(b"replaced after move")

    result = asyncio.run(
        StateVerifier(path_policy=policy).verify(action, feedback=feedback)
    )

    assert result.ok is False
    assert any(item.assertion == "file sha256" and not item.passed for item in result.evidence)


def test_yaml_syntax_validator_accepts_safe_document(tmp_path):
    target = tmp_path / "compose.yml"
    content = b"services:\n  api:\n    image: example/api:1\n"
    target.write_bytes(content)
    verifier = StateVerifier(path_policy=_policy(tmp_path))

    result = asyncio.run(verifier.verify(WriteFileAction(target, content=content)))

    assert result.ok is True
    assert any(item.source == "syntax_validator" for item in result.evidence)


def test_yaml_syntax_validator_accepts_standard_merge_keys(tmp_path):
    target = tmp_path / "config.yaml"
    content = (
        b"defaults: &defaults\n  timeout: 10\n"
        b"service:\n  <<: *defaults\n  timeout: 20\n"
    )
    target.write_bytes(content)
    verifier = StateVerifier(path_policy=_policy(tmp_path))

    result = asyncio.run(verifier.verify(WriteFileAction(target, content=content)))

    assert result.ok is True


@pytest.mark.parametrize(
    ("name", "content"),
    (
        ("duplicate.json", b'{"mode": 1, "mode": 2}'),
        ("constant.json", b'{"value": NaN}'),
        ("duplicate.yaml", b"mode: one\nmode: two\n"),
    ),
)
def test_config_validators_reject_ambiguous_documents(tmp_path, name, content):
    target = tmp_path / name
    target.write_bytes(content)
    verifier = StateVerifier(path_policy=_policy(tmp_path))

    result = asyncio.run(verifier.verify(WriteFileAction(target, content=content)))

    assert result.ok is False
    assert any(
        item.source == "syntax_validator" and not item.passed for item in result.evidence
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell parser is Windows-only")
def test_powershell_uses_fixed_parser_for_syntax(tmp_path):
    target = tmp_path / "script.ps1"
    content = b"param([string]$Name)\nWrite-Output $Name\n"
    target.write_bytes(content)
    verifier = StateVerifier(path_policy=_policy(tmp_path))

    result = asyncio.run(verifier.verify(WriteFileAction(target, content=content)))

    assert result.ok is True
    assert any(item.source == "syntax_validator" for item in result.evidence)


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell parser is Windows-only")
def test_powershell_parser_rejects_invalid_syntax(tmp_path):
    target = tmp_path / "script.ps1"
    content = b"function Invoke-Test {\n"
    target.write_bytes(content)
    verifier = StateVerifier(path_policy=_policy(tmp_path))

    result = asyncio.run(verifier.verify(WriteFileAction(target, content=content)))

    assert result.ok is False
    assert any(
        item.source == "syntax_validator" and not item.passed for item in result.evidence
    )


def test_file_hash_mismatch_is_detected_independently(tmp_path):
    target = tmp_path / "value.txt"
    target.write_bytes(b"tampered")
    verifier = StateVerifier(path_policy=_policy(tmp_path))

    result = asyncio.run(verifier.verify(WriteFileAction(target, content=b"expected")))

    assert result.ok is False
    digest = next(item for item in result.evidence if item.assertion == "file sha256")
    assert digest.expected == hashlib.sha256(b"expected").hexdigest()
    assert digest.observed == hashlib.sha256(b"tampered").hexdigest()


def test_read_verification_requires_regular_file(tmp_path):
    verifier = StateVerifier(path_policy=_policy(tmp_path))

    result = asyncio.run(verifier.verify(ReadFileAction(tmp_path)))

    assert result.ok is False
    assert any(item.assertion == "path kind" and not item.passed for item in result.evidence)


def test_delete_verification_requires_observed_absence(tmp_path):
    target = tmp_path / "deleted.txt"
    verifier = StateVerifier(path_policy=_policy(tmp_path))
    action = DeleteFileAction(target, missing_ok=True)

    absent = asyncio.run(verifier.verify(action))
    target.write_text("still here", encoding="utf-8")
    present = asyncio.run(verifier.verify(action))

    assert absent.ok is True
    assert present.ok is False


def test_process_exit_code_never_substitutes_for_postcondition(tmp_path):
    verifier = StateVerifier(path_policy=_policy(tmp_path))
    action = ProcessAction(ProcessRequest(executable="tool", cwd=tmp_path))

    result = asyncio.run(verifier.verify(action))

    assert result.ok is False
    assert any(item.source == "postcondition" for item in result.evidence)


def test_process_postcondition_requires_a_pre_execution_transition(tmp_path):
    target = tmp_path / "generated.json"
    verifier = StateVerifier(path_policy=_policy(tmp_path))
    action = ProcessAction(ProcessRequest(executable="tool", cwd=tmp_path))
    expectation = VerificationExpectation(
        paths=(PathExpectation(target, kind="file", syntax_valid=True),)
    )

    async def scenario():
        baseline = await verifier.capture_process_baseline(action, expectation)
        target.write_text('{"ok": true}', encoding="utf-8")
        return await verifier.verify(
            action,
            expectation=expectation,
            process_baseline=baseline,
        )

    result = asyncio.run(scenario())

    assert result.ok is True
    assert any(
        item.source == "causal_baseline" and item.passed for item in result.evidence
    )


def test_preexisting_path_cannot_satisfy_a_fresh_process_run(tmp_path):
    target = tmp_path / "already-present.json"
    target.write_text('{"ok": true}', encoding="utf-8")
    verifier = StateVerifier(path_policy=_policy(tmp_path))
    action = ProcessAction(ProcessRequest(executable="tool", cwd=tmp_path))
    expectation = VerificationExpectation(
        paths=(PathExpectation(target, kind="file", syntax_valid=True),)
    )

    async def scenario():
        baseline = await verifier.capture_process_baseline(action, expectation)
        return await verifier.verify(
            action,
            expectation=expectation,
            process_baseline=baseline,
        )

    result = asyncio.run(scenario())

    assert result.ok is False
    causal = next(item for item in result.evidence if item.source == "causal_baseline")
    assert causal.passed is False
    assert "already present" in (causal.error or "")


def test_preexisting_tcp_listener_cannot_satisfy_a_fresh_process_run(tmp_path):
    async def scenario():
        server = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            verifier = StateVerifier(
                path_policy=_policy(tmp_path), allow_private_network=True
            )
            action = ProcessAction(ProcessRequest(executable="tool", cwd=tmp_path))
            expectation = VerificationExpectation(
                tcp=(TcpExpectation("127.0.0.1", port, timeout_seconds=1),)
            )
            baseline = await verifier.capture_process_baseline(action, expectation)
            return await verifier.verify(
                action,
                expectation=expectation,
                process_baseline=baseline,
            )
        finally:
            server.close()
            await server.wait_closed()

    result = asyncio.run(scenario())

    assert result.ok is False
    assert any(
        item.source == "causal_baseline" and not item.passed
        for item in result.evidence
    )


def test_tcp_verification_opens_an_independent_socket(tmp_path):
    async def scenario():
        server = await asyncio.start_server(lambda _reader, writer: writer.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            verifier = StateVerifier(
                path_policy=_policy(tmp_path), allow_private_network=True
            )
            return await verifier.verify(
                TcpProbeAction("127.0.0.1", port, timeout_seconds=1)
            )
        finally:
            server.close()
            await server.wait_closed()

    result = asyncio.run(scenario())

    assert result.ok is True
    assert result.evidence[-1].source == "socket"


def test_registry_set_verifies_value_and_kind_readback(tmp_path, monkeypatch):
    monkeypatch.setattr(
        state_verification,
        "_read_registry_value",
        lambda *_args: {"exists": True, "value": "enabled", "registry_kind": 1},
    )
    verifier = StateVerifier(path_policy=_policy(tmp_path))
    action = RegistrySetAction(
        RegistryHive.CURRENT_USER,
        r"Software\Jarvis",
        "Mode",
        "enabled",
        RegistryValueKind.STRING,
    )

    result = asyncio.run(verifier.verify(action))

    assert result.ok is True
    assert len(result.evidence) == 2


def test_registry_kind_mismatch_fails_even_when_value_matches(tmp_path, monkeypatch):
    monkeypatch.setattr(
        state_verification,
        "_read_registry_value",
        lambda *_args: {"exists": True, "value": 1, "registry_kind": 11},
    )
    verifier = StateVerifier(path_policy=_policy(tmp_path))
    action = RegistrySetAction(
        RegistryHive.CURRENT_USER,
        r"Software\Jarvis",
        "Mode",
        1,
        RegistryValueKind.DWORD,
    )

    result = asyncio.run(verifier.verify(action))

    assert result.ok is False
    assert any(item.assertion == "registry value kind readback" for item in result.evidence)


def test_owned_process_termination_is_read_back_from_session_registry(tmp_path):
    class Sessions:
        @staticmethod
        def owned_process_tree_alive(session_id, pid):
            assert session_id == "session_12345678"
            assert pid == 42
            return False

    verifier = StateVerifier(path_policy=_policy(tmp_path), sessions=Sessions())

    result = asyncio.run(
        verifier.verify(TerminateOwnedProcessAction("session_12345678", 42))
    )

    assert result.ok is True
    assert result.evidence[0].observed["running"] is False


def test_interrupt_requires_explicit_owned_process_postcondition(tmp_path):
    verifier = StateVerifier(path_policy=_policy(tmp_path))
    action = TerminateOwnedProcessAction(
        "session_12345678", 42, signal=ProcessSignal.INTERRUPT
    )

    result = asyncio.run(verifier.verify(action))

    assert result.ok is False
    assert result.evidence[0].source == "postcondition"


def test_safe_gate_allows_typed_non_destructive_write_after_preflight(tmp_path):
    gate = SafeGate(path_policy=_policy(tmp_path), secret=b"s" * 32)
    action = WriteFileAction(tmp_path / "new.txt", b"value")

    decision = gate.prepare(action)

    assert decision.status is GateStatus.ALLOWED
    assert decision.risk is RiskLevel.MEDIUM
    assert decision.permit_token is None


def test_safe_gate_fails_closed_for_read_outside_path_policy(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("value", encoding="utf-8")
    gate = SafeGate(path_policy=_policy(tmp_path), secret=b"s" * 32)

    decision = gate.prepare(ReadFileAction(outside))

    assert decision.risk is RiskLevel.LOW
    assert decision.status is GateStatus.DENIED


def test_safe_gate_validates_process_request_before_allowing_it(tmp_path):
    gate = SafeGate(path_policy=_policy(tmp_path), secret=b"s" * 32)
    action = ProcessAction(ProcessRequest(executable="", cwd=tmp_path))

    decision = gate.prepare(action)

    assert decision.status is GateStatus.DENIED


def test_safe_gate_denies_delete_without_compare_and_swap_digest(tmp_path):
    target = tmp_path / "value.txt"
    target.write_bytes(b"value")
    gate = SafeGate(path_policy=_policy(tmp_path), secret=b"s" * 32)

    decision = gate.prepare(DeleteFileAction(target))

    assert decision.status is GateStatus.DENIED
    assert any(not item.passed for item in decision.simulation)


def test_safe_gate_issues_exact_one_use_permit_for_delete(tmp_path):
    target = tmp_path / "value.txt"
    target.write_bytes(b"value")
    digest = hashlib.sha256(b"value").hexdigest()
    gate = SafeGate(path_policy=_policy(tmp_path), secret=b"s" * 32)
    action = DeleteFileAction(target, expected_sha256=digest)

    prepared = gate.prepare(action)
    wrong_action = replace(action, action_id="delete_other")
    wrong = gate.consume(wrong_action, prepared.permit_token)
    consumed = gate.consume(action, prepared.permit_token)
    replay = gate.consume(action, prepared.permit_token)

    assert prepared.status is GateStatus.PERMIT_REQUIRED
    assert wrong.status is GateStatus.DENIED
    assert consumed.status is GateStatus.ALLOWED
    assert replay.status is GateStatus.DENIED


def test_safe_gate_repeats_preflight_when_permit_is_consumed(tmp_path):
    target = tmp_path / "value.txt"
    target.write_bytes(b"value")
    digest = hashlib.sha256(b"value").hexdigest()
    gate = SafeGate(path_policy=_policy(tmp_path), secret=b"s" * 32)
    action = DeleteFileAction(target, expected_sha256=digest)
    prepared = gate.prepare(action)
    target.write_bytes(b"changed after dry-run")

    consumed = gate.consume(action, prepared.permit_token)

    assert consumed.status is GateStatus.DENIED
    assert any(not item.passed for item in consumed.simulation)


def test_safe_gate_permit_is_atomic_under_concurrent_consumers(tmp_path):
    target = tmp_path / "value.txt"
    target.write_bytes(b"value")
    digest = hashlib.sha256(b"value").hexdigest()
    gate = SafeGate(path_policy=_policy(tmp_path), secret=b"s" * 32)
    action = DeleteFileAction(target, expected_sha256=digest)
    token = gate.prepare(action).permit_token

    with ThreadPoolExecutor(max_workers=8) as executor:
        decisions = tuple(executor.map(lambda _index: gate.consume(action, token), range(8)))

    assert sum(item.status is GateStatus.ALLOWED for item in decisions) == 1


def test_safe_gate_blocks_critical_path_without_explicit_exception(tmp_path):
    critical = tmp_path / "critical"
    critical.mkdir()
    target = critical / "value.txt"
    target.write_bytes(b"value")
    digest = hashlib.sha256(b"value").hexdigest()
    gate = SafeGate(
        path_policy=_policy(tmp_path),
        protected_paths=(critical,),
        secret=b"s" * 32,
    )

    decision = gate.prepare(DeleteFileAction(target, expected_sha256=digest))

    assert decision.risk is RiskLevel.CRITICAL
    assert decision.status is GateStatus.DENIED


def test_safe_gate_treats_move_source_as_a_mutated_critical_path(tmp_path):
    critical = tmp_path / "critical"
    critical.mkdir()
    source = critical / "value.txt"
    source.write_bytes(b"value")
    gate = SafeGate(
        path_policy=_policy(tmp_path),
        protected_paths=(critical,),
        secret=b"s" * 32,
    )

    decision = gate.prepare(MoveFileAction(source, tmp_path / "moved.txt"))

    assert decision.risk is RiskLevel.CRITICAL
    assert decision.status is GateStatus.DENIED


def test_safe_gate_critical_exception_still_requires_dry_run_permit(tmp_path):
    critical = tmp_path / "critical"
    critical.mkdir()
    target = critical / "value.txt"
    target.write_bytes(b"value")
    digest = hashlib.sha256(b"value").hexdigest()
    gate = SafeGate(
        path_policy=_policy(tmp_path),
        protected_paths=(critical,),
        protected_path_exceptions=(target,),
        secret=b"s" * 32,
    )

    decision = gate.prepare(DeleteFileAction(target, expected_sha256=digest))

    assert decision.risk is RiskLevel.CRITICAL
    assert decision.status is GateStatus.PERMIT_REQUIRED


def test_safe_gate_permit_binds_critical_move_source_snapshot(tmp_path):
    critical = tmp_path / "critical"
    critical.mkdir()
    source = critical / "value.txt"
    source.write_bytes(b"before")
    gate = SafeGate(
        path_policy=_policy(tmp_path),
        protected_paths=(critical,),
        protected_path_exceptions=(source,),
        secret=b"s" * 32,
    )
    action = MoveFileAction(source, tmp_path / "moved.txt")
    prepared = gate.prepare(action)
    source.write_bytes(b"after")

    consumed = gate.consume(action, prepared.permit_token)

    assert prepared.status is GateStatus.PERMIT_REQUIRED
    assert consumed.status is GateStatus.DENIED
