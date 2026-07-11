from __future__ import annotations

import ast
import asyncio
import base64
import binascii
import configparser
import contextlib
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import threading
import time
import tomllib
import xml.etree.ElementTree as element_tree
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any

from .execution_actions import (
    AtomicAction,
    CopyFileAction,
    CreateDirectoryAction,
    DeleteFileAction,
    ListDirectoryAction,
    MoveFileAction,
    PathPolicy,
    ProcessAction,
    ProcessSignal,
    ReadFileAction,
    RegistryDeleteValueAction,
    RegistryGetAction,
    RegistryHive,
    RegistrySetAction,
    RegistryValueKind,
    ResolveHostAction,
    StatPathAction,
    TcpProbeAction,
    TerminateOwnedProcessAction,
    WriteFileAction,
)
from .execution_filesystem import BoundPath, directory_entries, verified_binary_handle
from .execution_models import ActionFeedback
from .execution_session import SessionRegistry

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_NAME_RE = re.compile(
    r"(?i)(?:password|passwd|secret|token|credential|private.?key|api.?key)"
)


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class GateStatus(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    PERMIT_REQUIRED = "permit_required"


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    source: str
    assertion: str
    expected: Any
    observed: Any
    passed: bool
    captured_at: str
    error: str | None = None
    subject: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PathExpectation:
    path: Path
    exists: bool = True
    kind: str | None = None
    sha256: str | None = None
    syntax_valid: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            raise TypeError("path expectation path must be a Path")
        if self.kind not in {None, "file", "directory"}:
            raise ValueError("path expectation kind must be file, directory or None")
        if self.sha256 is not None and not _DIGEST_RE.fullmatch(self.sha256.lower()):
            raise ValueError("path expectation sha256 must be 64 hexadecimal characters")
        if not self.exists and any((self.kind, self.sha256, self.syntax_valid)):
            raise ValueError("an absent path cannot have content or kind assertions")


@dataclass(frozen=True, slots=True)
class TcpExpectation:
    host: str
    port: int
    reachable: bool = True
    timeout_seconds: float = 3.0

    def __post_init__(self) -> None:
        _validate_network_target(self.host, self.port)
        if not isinstance(self.reachable, bool):
            raise TypeError("TCP reachable expectation must be boolean")
        if not 0.05 <= self.timeout_seconds <= 60:
            raise ValueError("TCP expectation timeout must be between 0.05 and 60 seconds")


@dataclass(frozen=True, slots=True)
class ProcessExpectation:
    session_id: str
    pid: int
    running: bool

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("process expectation session_id cannot be empty")
        if isinstance(self.pid, bool) or not isinstance(self.pid, int) or self.pid <= 0:
            raise ValueError("process expectation PID must be positive")
        if not isinstance(self.running, bool):
            raise TypeError("process running expectation must be boolean")


@dataclass(frozen=True, slots=True)
class VerificationExpectation:
    paths: tuple[PathExpectation, ...] = ()
    tcp: tuple[TcpExpectation, ...] = ()
    processes: tuple[ProcessExpectation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.paths, tuple) or not all(
            isinstance(item, PathExpectation) for item in self.paths
        ):
            raise TypeError("paths must be a tuple of PathExpectation values")
        if not isinstance(self.tcp, tuple) or not all(
            isinstance(item, TcpExpectation) for item in self.tcp
        ):
            raise TypeError("tcp must be a tuple of TcpExpectation values")
        if not isinstance(self.processes, tuple) or not all(
            isinstance(item, ProcessExpectation) for item in self.processes
        ):
            raise TypeError("processes must be a tuple of ProcessExpectation values")


@dataclass(frozen=True, slots=True)
class ProcessBaselineObservation:
    """One normalized pre-execution state observation for a process postcondition."""

    source: str
    subject: str
    state_json: str


@dataclass(frozen=True, slots=True)
class ProcessVerificationBaseline:
    """Postcondition-bound state captured immediately before ``process.run``."""

    action_id: str
    expectation_sha256: str
    observations: tuple[ProcessBaselineObservation, ...]

    def __post_init__(self) -> None:
        if not self.action_id:
            raise ValueError("process baseline action_id cannot be empty")
        if not _DIGEST_RE.fullmatch(self.expectation_sha256):
            raise ValueError("process baseline expectation digest is invalid")
        if not self.observations:
            raise ValueError("process baseline requires at least one observation")


@dataclass(frozen=True, slots=True)
class VerificationResult:
    ok: bool
    status: VerificationStatus
    action_id: str
    action_kind: str
    summary: str
    evidence: tuple[VerificationEvidence, ...]
    error: str | None = None

    def __post_init__(self) -> None:
        if self.ok != (self.status is VerificationStatus.PASSED):
            raise ValueError("ok must be true exactly when verification status is passed")
        if self.ok and (not self.evidence or not all(item.passed for item in self.evidence)):
            raise ValueError("passed verification requires positive independent evidence")
        if self.status is VerificationStatus.FAILED and (
            not self.evidence or all(item.passed for item in self.evidence)
        ):
            raise ValueError("failed verification requires at least one failed assertion")
        if self.status is VerificationStatus.INCONCLUSIVE and self.evidence:
            raise ValueError("inconclusive verification cannot contain boolean assertions")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class GateEvidence:
    check: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class GateDecision:
    status: GateStatus
    risk: RiskLevel
    action_id: str
    summary: str
    simulation: tuple[GateEvidence, ...]
    permit_token: str | None = None
    expires_at: str | None = None

    def __post_init__(self) -> None:
        if self.status is GateStatus.PERMIT_REQUIRED:
            if self.permit_token is None or self.expires_at is None:
                raise ValueError("permit-required decisions must carry a token and expiry")
        elif self.permit_token is not None or self.expires_at is not None:
            raise ValueError("only permit-required decisions may carry a permit")

    @property
    def allowed(self) -> bool:
        return self.status is GateStatus.ALLOWED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SyntaxValidator = Callable[[bytes], None]


class StateVerifier:
    """Performs fresh state inspection without trusting an action's success flag."""

    def __init__(
        self,
        *,
        path_policy: PathPolicy,
        sessions: SessionRegistry | None = None,
        allow_private_network: bool = False,
        syntax_validators: Mapping[str, SyntaxValidator] | None = None,
    ) -> None:
        self.path_policy = path_policy
        self.sessions = sessions
        self.allow_private_network = allow_private_network
        self._syntax_validators = dict(_builtin_syntax_validators())
        for suffix, validator in (syntax_validators or {}).items():
            normalized = suffix.casefold()
            if not normalized.startswith(".") or not callable(validator):
                raise ValueError("syntax validators require a dotted suffix and callable")
            self._syntax_validators[normalized] = validator

    async def verify(
        self,
        action: AtomicAction,
        *,
        feedback: ActionFeedback | None = None,
        expectation: VerificationExpectation | None = None,
        process_baseline: ProcessVerificationBaseline | None = None,
        idempotent_replay: bool = False,
    ) -> VerificationResult:
        """Re-inspect target state; action feedback is never accepted as state evidence."""
        evidence: list[VerificationEvidence] = []
        try:
            expected_state = expectation or VerificationExpectation()
            await self._verify_action(
                action,
                feedback,
                expected_state,
                evidence,
                process_baseline=process_baseline,
                idempotent_replay=idempotent_replay,
            )
            await self._verify_expectation(expected_state, evidence)
        except Exception as exc:
            evidence.append(
                _evidence(
                    "verification",
                    "inspection completed without error",
                    "success",
                    f"{type(exc).__name__}: {exc}",
                    False,
                    error=f"{type(exc).__name__}: {exc}",
                    subject=action.action_id,
                )
            )
        if not evidence:
            return VerificationResult(
                ok=False,
                status=VerificationStatus.INCONCLUSIVE,
                action_id=action.action_id,
                action_kind=type(action).__name__,
                summary="No independent assertion was available for this action.",
                evidence=(),
                error="an explicit postcondition is required",
            )
        failed = tuple(item for item in evidence if not item.passed)
        status = VerificationStatus.FAILED if failed else VerificationStatus.PASSED
        return VerificationResult(
            ok=not failed,
            status=status,
            action_id=action.action_id,
            action_kind=type(action).__name__,
            summary=(
                f"All {len(evidence)} independent state assertion(s) passed."
                if not failed
                else f"{len(failed)} of {len(evidence)} state assertion(s) failed."
            ),
            evidence=tuple(evidence),
            error=failed[0].error if failed else None,
        )

    async def capture_process_baseline(
        self,
        action: AtomicAction,
        expectation: VerificationExpectation,
    ) -> ProcessVerificationBaseline:
        """Capture normalized state before a process can create its asserted side effect."""

        if not isinstance(action, ProcessAction):
            raise TypeError("process baselines can only be captured for process.run")
        if not (expectation.paths or expectation.tcp or expectation.processes):
            raise ValueError("process baseline requires an explicit postcondition")
        observations = await self._capture_process_observations(expectation)
        return ProcessVerificationBaseline(
            action_id=action.action_id,
            expectation_sha256=self.expectation_fingerprint(expectation),
            observations=observations,
        )

    @staticmethod
    def expectation_fingerprint(expectation: VerificationExpectation) -> str:
        """Return the canonical identity bound to an idempotent verification replay."""

        if not isinstance(expectation, VerificationExpectation):
            raise TypeError("expectation must be a VerificationExpectation")
        return _expectation_digest(expectation)

    async def _verify_action(
        self,
        action: AtomicAction,
        feedback: ActionFeedback | None,
        expectation: VerificationExpectation,
        evidence: list[VerificationEvidence],
        *,
        process_baseline: ProcessVerificationBaseline | None,
        idempotent_replay: bool,
    ) -> None:
        if isinstance(action, CreateDirectoryAction):
            await self._verify_path(PathExpectation(action.path, kind="directory"), evidence)
        elif isinstance(action, WriteFileAction):
            await self._verify_path(
                PathExpectation(
                    action.path,
                    kind="file",
                    sha256=hashlib.sha256(action.content).hexdigest(),
                    syntax_valid=self._has_syntax_validator(action.path),
                ),
                evidence,
            )
        elif isinstance(action, CopyFileAction):
            source = await asyncio.to_thread(self._file_snapshot, action.source)
            await self._verify_path(
                PathExpectation(
                    action.destination,
                    kind="file",
                    sha256=action.expected_sha256 or source["sha256"],
                    syntax_valid=self._has_syntax_validator(action.destination),
                ),
                evidence,
            )
        elif isinstance(action, MoveFileAction):
            await self._verify_path(PathExpectation(action.source, exists=False), evidence)
            source_before = (
                feedback.before.get("source")
                if feedback is not None and isinstance(feedback.before, dict)
                else None
            )
            source_sha256 = action.expected_sha256 or (
                source_before.get("sha256") if isinstance(source_before, dict) else None
            )
            if not isinstance(source_sha256, str) or not _DIGEST_RE.fullmatch(source_sha256):
                evidence.append(
                    _evidence(
                        "filesystem",
                        "move source snapshot is bound to destination content",
                        "pre-action source sha256",
                        source_sha256,
                        False,
                        error="move feedback lacks a trusted source content digest",
                        subject=str(action.source),
                    )
                )
            await self._verify_path(
                PathExpectation(
                    action.destination,
                    kind="file",
                    sha256=source_sha256 if isinstance(source_sha256, str) else None,
                    syntax_valid=self._has_syntax_validator(action.destination),
                ),
                evidence,
            )
        elif isinstance(action, DeleteFileAction):
            await self._verify_path(PathExpectation(action.path, exists=False), evidence)
        elif isinstance(action, StatPathAction):
            await self._verify_path(PathExpectation(action.path), evidence)
        elif isinstance(action, ReadFileAction):
            await self._verify_path(PathExpectation(action.path, kind="file"), evidence)
        elif isinstance(action, ListDirectoryAction):
            await self._verify_path(PathExpectation(action.path, kind="directory"), evidence)
            await asyncio.to_thread(self._verify_directory_access, action.path, evidence)
        elif isinstance(action, TcpProbeAction):
            await self._verify_tcp(
                TcpExpectation(action.host, action.port, True, action.timeout_seconds), evidence
            )
        elif isinstance(action, ResolveHostAction):
            await self._verify_resolution(action, evidence)
        elif isinstance(action, RegistrySetAction):
            await asyncio.to_thread(self._verify_registry_set, action, evidence)
        elif isinstance(action, RegistryDeleteValueAction):
            await asyncio.to_thread(self._verify_registry_delete, action, evidence)
        elif isinstance(action, RegistryGetAction):
            await asyncio.to_thread(self._verify_registry_read, action, evidence)
        elif isinstance(action, TerminateOwnedProcessAction):
            if action.signal is ProcessSignal.INTERRUPT:
                if not expectation.processes:
                    evidence.append(
                        _evidence(
                            "postcondition",
                            "interrupt has an explicit owned-process state assertion",
                            True,
                            False,
                            False,
                            error="interrupt delivery alone has no observable state postcondition",
                        )
                    )
            else:
                self._verify_owned_process(
                    ProcessExpectation(action.session_id, action.pid, False), evidence
                )
        elif isinstance(action, ProcessAction):
            await self._verify_process_action(
                action,
                feedback,
                expectation,
                evidence,
                baseline=process_baseline,
                idempotent_replay=idempotent_replay,
            )
        else:
            raise TypeError(f"no verifier exists for {type(action).__name__}")

    async def _verify_expectation(
        self,
        expectation: VerificationExpectation,
        evidence: list[VerificationEvidence],
    ) -> None:
        for path in expectation.paths:
            await self._verify_path(path, evidence)
        for target in expectation.tcp:
            await self._verify_tcp(target, evidence)
        for process in expectation.processes:
            self._verify_owned_process(process, evidence)

    async def _verify_path(
        self, expectation: PathExpectation, evidence: list[VerificationEvidence]
    ) -> None:
        if self.path_policy.active_mutation_guard is None:
            with self.path_policy.mutation_scope((expectation.path,)):
                await self._verify_path(expectation, evidence)
                return
        path = self.path_policy.bind_mutation_path(expectation.path, allow_root=True)
        snapshot = await asyncio.to_thread(
            self._path_snapshot,
            path,
            include_content=expectation.syntax_valid,
        )
        content = snapshot.pop("_content", None)
        evidence.append(
            _evidence(
                "filesystem",
                "path existence",
                expectation.exists,
                snapshot["exists"],
                expectation.exists == snapshot["exists"],
                subject=str(path.path),
            )
        )
        if not expectation.exists or not snapshot["exists"]:
            return
        if expectation.kind is not None:
            evidence.append(
                _evidence(
                    "filesystem",
                    "path kind",
                    expectation.kind,
                    snapshot["kind"],
                    expectation.kind == snapshot["kind"],
                    subject=str(path.path),
                )
            )
        if expectation.sha256 is not None:
            observed = snapshot.get("sha256")
            evidence.append(
                _evidence(
                    "filesystem",
                    "file sha256",
                    expectation.sha256.lower(),
                    observed,
                    hmac.compare_digest(expectation.sha256.lower(), observed or ""),
                    subject=str(path.path),
                )
            )
        if expectation.syntax_valid:
            await asyncio.to_thread(self._validate_syntax, path.path, evidence, content)

    async def _verify_tcp(
        self, target: TcpExpectation, evidence: list[VerificationEvidence]
    ) -> None:
        host, port = _validate_network_target(target.host, target.port)
        if not 0.05 <= target.timeout_seconds <= 60:
            raise ValueError("TCP verification timeout must be between 0.05 and 60 seconds")
        connected = False
        peer: Any = None
        error: str | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            addresses = await self._resolve_addresses(host, port)
            if not addresses:
                raise OSError("target did not resolve")
            deadline = time.monotonic() + target.timeout_seconds
            failures: list[str] = []
            for address in addresses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failures.append("verification deadline elapsed")
                    break
                try:
                    _reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(address, port), timeout=remaining
                    )
                except (OSError, TimeoutError) as exc:
                    failures.append(f"{address}: {type(exc).__name__}")
                    continue
                connected = True
                peer = writer.get_extra_info("peername")
                break
            if not connected:
                raise OSError("; ".join(failures) or "all resolved addresses failed")
        except (OSError, TimeoutError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        passed = connected is target.reachable
        evidence.append(
            _evidence(
                "socket",
                "TCP reachability",
                target.reachable,
                {"reachable": connected, "peer": list(peer) if peer else None},
                passed,
                error=None if passed else error or "reachability assertion failed",
                subject=f"{host}:{port}",
            )
        )

    async def _verify_resolution(
        self, action: ResolveHostAction, evidence: list[VerificationEvidence]
    ) -> None:
        host, port = _validate_network_target(action.host, action.port)
        addresses = await self._resolve_addresses(host, port)
        evidence.append(
            _evidence(
                "resolver",
                "fresh DNS resolution returned addresses",
                "one or more addresses",
                addresses,
                bool(addresses),
                subject=f"{host}:{port}",
            )
        )

    async def _resolve_addresses(self, host: str, port: int) -> list[str]:
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
        addresses = sorted({record[4][0] for record in records})
        if not self.allow_private_network:
            for raw in addresses:
                address = ipaddress.ip_address(raw.split("%", 1)[0])
                if not address.is_global:
                    raise PermissionError(
                        f"verification target resolved to a non-global address: {address}"
                    )
        return addresses

    async def _verify_process_action(
        self,
        action: ProcessAction,
        feedback: ActionFeedback | None,
        expectation: VerificationExpectation,
        evidence: list[VerificationEvidence],
        *,
        baseline: ProcessVerificationBaseline | None,
        idempotent_replay: bool,
    ) -> None:
        del feedback
        # Process exit or existence alone cannot establish the requested side effect.
        if not (expectation.paths or expectation.tcp or expectation.processes):
            evidence.append(
                _evidence(
                    "postcondition",
                    "process action has a state assertion independent of its exit code",
                    True,
                    False,
                    False,
                    error="provide a path, TCP, or owned-process VerificationExpectation",
                    subject=action.action_id,
                )
            )
            return
        if idempotent_replay:
            evidence.append(
                _evidence(
                    "idempotency_cache",
                    "cached process identity authorizes state-only replay verification",
                    action.action_id,
                    action.action_id,
                    True,
                    subject=action.action_id,
                )
            )
            return
        expected_digest = _expectation_digest(expectation)
        if (
            baseline is None
            or baseline.action_id != action.action_id
            or not hmac.compare_digest(baseline.expectation_sha256, expected_digest)
        ):
            evidence.append(
                _evidence(
                    "causal_baseline",
                    "process postcondition is bound to a matching pre-execution baseline",
                    {"action_id": action.action_id, "expectation_sha256": expected_digest},
                    (
                        None
                        if baseline is None
                        else {
                            "action_id": baseline.action_id,
                            "expectation_sha256": baseline.expectation_sha256,
                        }
                    ),
                    False,
                    error="a matching pre-execution process baseline is required",
                    subject=action.action_id,
                )
            )
            return
        current = await self._capture_process_observations(expectation)
        before = {
            (item.source, item.subject): item.state_json for item in baseline.observations
        }
        after = {(item.source, item.subject): item.state_json for item in current}
        if before.keys() != after.keys():
            evidence.append(
                _evidence(
                    "causal_baseline",
                    "process baseline subjects remain stable through verification",
                    sorted(f"{source}:{subject}" for source, subject in before),
                    sorted(f"{source}:{subject}" for source, subject in after),
                    False,
                    error="process postcondition subjects changed after baseline capture",
                    subject=action.action_id,
                )
            )
            return
        changed = sorted(
            f"{source}:{subject}"
            for (source, subject), state in before.items()
            if not hmac.compare_digest(state, after[(source, subject)])
        )
        evidence.append(
            _evidence(
                "causal_baseline",
                "process execution caused every asserted state transition",
                "all postcondition subjects changed after baseline capture",
                {
                    "changed_subjects": changed,
                    "unchanged_subject_count": len(before) - len(changed),
                },
                len(changed) == len(before),
                error=(
                    None
                    if len(changed) == len(before)
                    else "one or more asserted states were already present before process execution"
                ),
                subject=action.action_id,
            )
        )

    async def _capture_process_observations(
        self,
        expectation: VerificationExpectation,
    ) -> tuple[ProcessBaselineObservation, ...]:
        observations: list[ProcessBaselineObservation] = []
        for item in expectation.paths:
            if self.path_policy.active_mutation_guard is None:
                with self.path_policy.mutation_scope((item.path,)):
                    snapshot = await self._capture_path_process_state(item.path)
            else:
                snapshot = await self._capture_path_process_state(item.path)
            observations.append(
                ProcessBaselineObservation(
                    source="filesystem",
                    subject=str(self.path_policy.resolve(item.path, allow_root=True)),
                    state_json=_canonical_state(snapshot),
                )
            )
        for item in expectation.tcp:
            probe: list[VerificationEvidence] = []
            await self._verify_tcp(item, probe)
            observed = next(
                evidence.observed
                for evidence in probe
                if evidence.assertion == "TCP reachability"
            )
            observations.append(
                ProcessBaselineObservation(
                    source="socket",
                    subject=f"{item.host.casefold()}:{item.port}",
                    state_json=_canonical_state(
                        {
                            "reachable": bool(
                                isinstance(observed, dict) and observed.get("reachable")
                            )
                        }
                    ),
                )
            )
        for item in expectation.processes:
            if self.sessions is None:
                raise RuntimeError("owned process baseline requires a SessionRegistry")
            alive = self.sessions.owned_process_tree_alive(item.session_id, item.pid)
            observations.append(
                ProcessBaselineObservation(
                    source="session_registry",
                    subject=f"{item.session_id}:{item.pid}",
                    state_json=_canonical_state({"running": alive}),
                )
            )
        observations.sort(key=lambda item: (item.source, item.subject))
        if len({(item.source, item.subject) for item in observations}) != len(observations):
            raise ValueError("process verification contains duplicate postcondition subjects")
        return tuple(observations)

    async def _capture_path_process_state(self, path: Path) -> dict[str, Any]:
        bound = self.path_policy.bind_mutation_path(path, allow_root=True)
        snapshot = await asyncio.to_thread(self._path_snapshot, bound)
        normalized: dict[str, Any] = {"exists": bool(snapshot.get("exists"))}
        if normalized["exists"]:
            normalized["kind"] = snapshot.get("kind")
            if snapshot.get("kind") == "file":
                normalized["size"] = snapshot.get("size")
                normalized["sha256"] = snapshot.get("sha256")
        return normalized

    def _verify_owned_process(
        self, expectation: ProcessExpectation, evidence: list[VerificationEvidence]
    ) -> None:
        if self.sessions is None:
            raise RuntimeError("owned process verification requires a SessionRegistry")
        if expectation.pid <= 0:
            raise ValueError("process verification PID must be positive")
        alive = self.sessions.owned_process_tree_alive(
            expectation.session_id, expectation.pid
        )
        evidence.append(
            _evidence(
                "session_registry",
                "owned process tree state",
                {"running": expectation.running},
                {"running": alive, "pid": expectation.pid},
                alive is expectation.running,
                subject=f"{expectation.session_id}:{expectation.pid}",
            )
        )

    def _verify_registry_set(
        self, action: RegistrySetAction, evidence: list[VerificationEvidence]
    ) -> None:
        current = _read_registry_value(action.hive, action.key, action.name)
        expected = _registry_comparable(action.value)
        observed = _registry_comparable(current.get("value")) if current["exists"] else None
        expected_kind = _registry_kind_code(action.value_kind)
        passed = (
            current["exists"]
            and expected == observed
            and current.get("registry_kind") == expected_kind
        )
        evidence.append(
            _evidence(
                "winreg",
                "registry value readback",
                _safe_registry_evidence(action.name, expected),
                _safe_registry_evidence(action.name, observed),
                passed,
                subject=_registry_subject(action),
            )
        )
        evidence.append(
            _evidence(
                "winreg",
                "registry value kind readback",
                expected_kind,
                current.get("registry_kind"),
                current.get("registry_kind") == expected_kind,
                subject=_registry_subject(action),
            )
        )

    def _verify_directory_access(
        self, path: Path, evidence: list[VerificationEvidence]
    ) -> None:
        if self.path_policy.active_mutation_guard is None:
            with self.path_policy.mutation_scope((path,)):
                self._verify_directory_access(path, evidence)
                return
        bound = self.path_policy.bind_mutation_path(path, allow_root=True)
        try:
            directory_entries(bound)
        except OSError as exc:
            evidence.append(
                _evidence(
                    "filesystem",
                    "directory is independently readable",
                    True,
                    False,
                    False,
                    error=f"{type(exc).__name__}: {exc}",
                    subject=str(bound.path),
                )
            )
        else:
            evidence.append(
                _evidence(
                    "filesystem",
                    "directory is independently readable",
                    True,
                    True,
                    True,
                    subject=str(bound.path),
                )
            )

    def _verify_registry_delete(
        self, action: RegistryDeleteValueAction, evidence: list[VerificationEvidence]
    ) -> None:
        current = _read_registry_value(action.hive, action.key, action.name)
        evidence.append(
            _evidence(
                "winreg",
                "registry value absence",
                False,
                current["exists"],
                not current["exists"],
                subject=_registry_subject(action),
            )
        )

    def _verify_registry_read(
        self, action: RegistryGetAction, evidence: list[VerificationEvidence]
    ) -> None:
        current = _read_registry_value(action.hive, action.key, action.name)
        evidence.append(
            _evidence(
                "winreg",
                "registry inspection completed",
                "readable state",
                {"exists": current["exists"]},
                True,
                subject=_registry_subject(action),
            )
        )

    def _path_snapshot(
        self, path: BoundPath, *, include_content: bool = False
    ) -> dict[str, Any]:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return {"exists": False, "path": str(path.path)}
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("verification target became a symbolic link")
        kind = (
            "directory"
            if stat.S_ISDIR(metadata.st_mode)
            else "file"
            if stat.S_ISREG(metadata.st_mode)
            else "other"
        )
        result: dict[str, Any] = {
            "exists": True,
            "path": str(path.path),
            "kind": kind,
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
        }
        if kind == "file":
            file_state = self._bound_file_snapshot(path, include_content=include_content)
            result["sha256"] = file_state["sha256"]
            if include_content:
                result["_content"] = file_state["content"]
        return result

    def _file_snapshot(
        self, path: Path, *, include_content: bool = False
    ) -> dict[str, Any]:
        if self.path_policy.active_mutation_guard is None:
            with self.path_policy.mutation_scope((path,)):
                return self._file_snapshot(path, include_content=include_content)
        bound = self.path_policy.bind_mutation_path(path)
        return self._bound_file_snapshot(bound, include_content=include_content)

    @staticmethod
    def _bound_file_snapshot(
        path: BoundPath, *, include_content: bool = False
    ) -> dict[str, Any]:
        digest = hashlib.sha256()
        size = 0
        content = bytearray() if include_content else None
        with verified_binary_handle(path) as handle:
            opened = os.fstat(handle.fileno())
            current = path.lstat()
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise RuntimeError("file identity changed during verification")
            while chunk := handle.read(128 * 1024):
                size += len(chunk)
                digest.update(chunk)
                if content is not None:
                    if size > 16 * 1024 * 1024:
                        raise ValueError("syntax validation input exceeds 16 MiB")
                    content.extend(chunk)
            final = path.lstat()
            if (
                stat.S_ISLNK(final.st_mode)
                or (opened.st_dev, opened.st_ino) != (final.st_dev, final.st_ino)
            ):
                raise RuntimeError("file identity changed during verification")
        result: dict[str, Any] = {"sha256": digest.hexdigest(), "size": size}
        if content is not None:
            result["content"] = bytes(content)
        return result

    def _validate_syntax(
        self,
        path: Path,
        evidence: list[VerificationEvidence],
        content: bytes | None,
    ) -> None:
        suffix = ".env" if path.name.casefold() == ".env" else path.suffix.casefold()
        validator = self._syntax_validators.get(suffix)
        if validator is None:
            evidence.append(
                _evidence(
                    "syntax_validator",
                    f"{suffix or path.name} syntax validator is available",
                    True,
                    False,
                    False,
                    error="no syntax validator is registered for this file type",
                    subject=str(path),
                )
            )
            return
        try:
            if content is None:
                raise RuntimeError("syntax content snapshot is unavailable")
            validator(content)
        except Exception as exc:
            evidence.append(
                _evidence(
                    "syntax_validator",
                    f"{suffix} syntax is valid",
                    True,
                    False,
                    False,
                    error=f"{type(exc).__name__}: {exc}",
                    subject=str(path),
                )
            )

        else:
            evidence.append(
                _evidence(
                    "syntax_validator",
                    f"{suffix} syntax is valid",
                    True,
                    True,
                    True,
                    subject=str(path),
                )
            )

    def _has_syntax_validator(self, path: Path) -> bool:
        suffix = ".env" if path.name.casefold() == ".env" else path.suffix.casefold()
        return suffix in self._syntax_validators


class SafeGate:
    """Fail-closed preflight barrier with exact, expiring, one-use permits."""

    def __init__(
        self,
        *,
        path_policy: PathPolicy,
        sessions: SessionRegistry | None = None,
        protected_paths: tuple[Path, ...] = (),
        protected_path_exceptions: tuple[Path, ...] = (),
        permit_ttl_seconds: float = 30.0,
        secret: bytes | None = None,
    ) -> None:
        if not 1 <= permit_ttl_seconds <= 300:
            raise ValueError("permit TTL must be between 1 and 300 seconds")
        self.path_policy = path_policy
        self.sessions = sessions
        self.permit_ttl_seconds = permit_ttl_seconds
        defaults = _default_protected_paths()
        self.protected_paths = _normalize_paths((*defaults, *protected_paths))
        self.protected_path_exceptions = _normalize_paths(protected_path_exceptions)
        self._secret = secret or secrets.token_bytes(32)
        if len(self._secret) < 32:
            raise ValueError("safe-gate secret must contain at least 32 bytes")
        self._permits: dict[str, tuple[str, float, str]] = {}
        self._lock = threading.RLock()

    def classify(self, action: AtomicAction) -> RiskLevel:
        paths = _risk_paths(action)
        if paths and any(self._is_protected(path) for path in paths):
            return RiskLevel.CRITICAL
        if isinstance(action, RegistrySetAction | RegistryDeleteValueAction):
            return (
                RiskLevel.CRITICAL
                if action.hive is RegistryHive.LOCAL_MACHINE
                else RiskLevel.HIGH
            )
        if isinstance(action, DeleteFileAction | TerminateOwnedProcessAction):
            return RiskLevel.HIGH
        if isinstance(
            action,
            CreateDirectoryAction
            | WriteFileAction
            | CopyFileAction
            | MoveFileAction
            | ProcessAction,
        ):
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def prepare(self, action: AtomicAction) -> GateDecision:
        with self._lock:
            self._purge_expired()
            risk = self.classify(action)
            simulation = self._simulate(action)
            if not simulation or not all(item.passed for item in simulation):
                return GateDecision(
                    GateStatus.DENIED,
                    risk,
                    action.action_id,
                    "Preflight simulation failed closed.",
                    simulation,
                )
            if risk is RiskLevel.CRITICAL and any(
                self._is_protected(path) and not self._is_exception(path)
                for path in _risk_paths(action)
            ):
                return GateDecision(
                    GateStatus.DENIED,
                    risk,
                    action.action_id,
                    "Mutation of a protected critical path is not authorized.",
                    simulation,
                )
            if risk in {RiskLevel.LOW, RiskLevel.MEDIUM}:
                return GateDecision(
                    GateStatus.ALLOWED,
                    risk,
                    action.action_id,
                    "Preflight checks passed.",
                    simulation,
                )
            fingerprint = _action_fingerprint(action)
            simulation_fingerprint = _simulation_fingerprint(simulation)
            nonce = secrets.token_urlsafe(24)
            expires = time.time() + self.permit_ttl_seconds
            expires_ms = int(expires * 1000)
            payload = f"{nonce}.{expires_ms}.{fingerprint}.{simulation_fingerprint}"
            signature = hmac.new(
                self._secret, payload.encode("ascii"), hashlib.sha256
            ).digest()
            token = f"{nonce}.{expires_ms}.{_b64(signature)}"
            self._permits[nonce] = (
                fingerprint,
                expires_ms / 1000,
                simulation_fingerprint,
            )
            return GateDecision(
                GateStatus.PERMIT_REQUIRED,
                risk,
                action.action_id,
                "Dry-run passed; consume the exact one-use permit before execution.",
                simulation,
                permit_token=token,
                expires_at=datetime.fromtimestamp(
                    expires_ms / 1000, UTC
                ).isoformat(timespec="milliseconds"),
            )

    def consume(self, action: AtomicAction, permit_token: str) -> GateDecision:
        with self._lock:
            self._purge_expired()
            risk = self.classify(action)
            simulation: tuple[GateEvidence, ...] = ()
            try:
                nonce, raw_expires, raw_signature = permit_token.split(".", 2)
                expires_ms = int(raw_expires)
                signature = _unb64(raw_signature)
            except (AttributeError, ValueError, binascii.Error) as exc:
                return GateDecision(
                    GateStatus.DENIED,
                    risk,
                    action.action_id,
                    f"Malformed safe-gate permit: {type(exc).__name__}.",
                    simulation,
                )
            record = self._permits.get(nonce)
            fingerprint = _action_fingerprint(action)
            recorded_simulation = record[2] if record is not None else ""
            payload = f"{nonce}.{expires_ms}.{fingerprint}.{recorded_simulation}"
            expected = hmac.new(
                self._secret, payload.encode("ascii"), hashlib.sha256
            ).digest()
            expires = expires_ms / 1000
            cryptographic_valid = bool(
                record is not None
                and expires >= time.time()
                and abs(record[1] - expires) < 0.000001
                and hmac.compare_digest(record[0], fingerprint)
                and hmac.compare_digest(expected, signature)
            )
            if cryptographic_valid:
                self._permits.pop(nonce, None)
                simulation = self._simulate(action)
            valid = (
                cryptographic_valid
                and bool(simulation)
                and all(item.passed for item in simulation)
                and record is not None
                and hmac.compare_digest(
                    record[2],
                    _simulation_fingerprint(simulation),
                )
            )
            summary = (
                "One-use permit consumed."
                if valid
                else "Permit is invalid, expired, replayed, or bound to another action."
            )
            return GateDecision(
                GateStatus.ALLOWED if valid else GateStatus.DENIED,
                risk,
                action.action_id,
                summary,
                simulation,
            )

    def _simulate(self, action: AtomicAction) -> tuple[GateEvidence, ...]:
        checks: list[GateEvidence] = []
        try:
            for path in _mutation_paths(action):
                self.path_policy.resolve(path, must_exist=False, allow_root=False)
                checks.append(GateEvidence("path_policy", True, f"authorized target: {path}"))
            if isinstance(action, StatPathAction | ReadFileAction | ListDirectoryAction):
                target = self.path_policy.resolve(
                    action.path, must_exist=True, allow_root=True
                )
                checks.append(
                    GateEvidence("path_policy", True, f"authorized inspection: {target}")
                )
            if isinstance(action, DeleteFileAction):
                target = self.path_policy.resolve(action.path, must_exist=True)
                if not target.is_file() or target.is_symlink():
                    raise ValueError("delete target is not a regular file")
                if action.expected_sha256 is None:
                    raise PermissionError("destructive deletion requires expected_sha256")
                observed = _hash_file(target)
                if not hmac.compare_digest(observed, action.expected_sha256.lower()):
                    raise RuntimeError("delete precondition digest does not match")
                checks.append(GateEvidence("delete_precondition", True, observed))
            elif isinstance(action, MoveFileAction | CopyFileAction):
                source = self.path_policy.resolve(action.source, must_exist=True)
                if not source.is_file() or source.is_symlink():
                    raise ValueError("source is not a regular file")
                destination = self.path_policy.resolve(action.destination)
                if destination.exists() and not action.overwrite:
                    raise FileExistsError("destination exists and overwrite is false")
                if destination.exists() and not destination.is_file():
                    raise ValueError("destination is not a regular file")
                if action.create_parents:
                    self.path_policy.resolve(destination.parent, allow_root=True)
                elif not destination.parent.is_dir():
                    raise FileNotFoundError("destination parent does not exist")
                checks.append(GateEvidence("source_snapshot", True, _hash_file(source)))
            elif isinstance(action, WriteFileAction):
                target = self.path_policy.resolve(action.path)
                if action.require_absent and target.exists():
                    raise FileExistsError("write target must be absent")
                if action.create_parents:
                    self.path_policy.resolve(target.parent, allow_root=True)
                elif not target.parent.is_dir():
                    raise FileNotFoundError("write target parent does not exist")
                if action.expected_sha256 is not None:
                    target = self.path_policy.resolve(action.path, must_exist=True)
                    if not hmac.compare_digest(
                        _hash_file(target), action.expected_sha256.lower()
                    ):
                        raise RuntimeError("write precondition digest does not match")
                    checks.append(
                        GateEvidence("write_precondition", True, "digest matched")
                    )
            elif isinstance(action, CreateDirectoryAction):
                target = self.path_policy.resolve(action.path)
                if target.exists() and not target.is_dir():
                    raise ValueError("directory target exists as a non-directory")
                if not action.parents and not target.parent.is_dir():
                    raise FileNotFoundError("directory parent does not exist")
            elif isinstance(action, TerminateOwnedProcessAction):
                if self.sessions is None:
                    raise RuntimeError("process gate requires a SessionRegistry")
                self.sessions.require_owned_pid(action.session_id, action.pid)
                checks.append(GateEvidence("process_ownership", True, "live identity matched"))
            elif isinstance(action, ProcessAction):
                action.request.validate()
                if action.request.cwd is not None:
                    self.path_policy.resolve(action.request.cwd, must_exist=True, allow_root=True)
                for observed in action.request.observe_paths:
                    self.path_policy.resolve(observed, must_exist=False, allow_root=True)
                checks.append(
                    GateEvidence(
                        "process_envelope",
                        True,
                        "typed argv/cwd/observation scope validated; "
                        "capability policy remains mandatory",
                    )
                )
            elif isinstance(action, RegistrySetAction | RegistryDeleteValueAction):
                current = _read_registry_value(action.hive, action.key, action.name)
                checks.append(
                    GateEvidence(
                        "registry_readback",
                        True,
                        (
                            "target inspected; "
                            f"state_sha256={_state_fingerprint(current)}"
                        ),
                    )
                )
            if not checks:
                checks.append(GateEvidence("typed_action", True, "no destructive effect declared"))
        except Exception as exc:
            checks.append(
                GateEvidence("preflight", False, f"{type(exc).__name__}: {exc}")
            )
        return tuple(checks)

    def _is_protected(self, path: Path) -> bool:
        candidate = _normalized_path_text(path)
        return any(_path_contains(root, candidate) for root in self.protected_paths)

    def _is_exception(self, path: Path) -> bool:
        candidate = _normalized_path_text(path)
        return any(_path_contains(root, candidate) for root in self.protected_path_exceptions)

    def _purge_expired(self) -> None:
        now = time.time()
        for nonce in [
            key
            for key, (_fingerprint, expiry, _simulation) in self._permits.items()
            if expiry < now
        ]:
            self._permits.pop(nonce, None)


def _builtin_syntax_validators() -> dict[str, SyntaxValidator]:
    def json_validator(content: bytes) -> None:
        def reject_constant(value: str) -> None:
            raise ValueError(f"non-standard JSON constant: {value}")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON object key: {key}")
                result[key] = value
            return result

        json.loads(
            content.decode("utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )

    def toml_validator(content: bytes) -> None:
        tomllib.loads(content.decode("utf-8"))

    def python_validator(content: bytes) -> None:
        ast.parse(content.decode("utf-8-sig"))

    def xml_validator(content: bytes) -> None:
        element_tree.fromstring(content)

    def ini_validator(content: bytes) -> None:
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read_file(io.StringIO(content.decode("utf-8-sig")))

    def env_validator(content: bytes) -> None:
        keys: set[str] = set()
        for number, raw in enumerate(content.decode("utf-8-sig").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or not _ENV_KEY_RE.fullmatch(key):
                raise ValueError(f"invalid environment assignment at line {number}")
            if key in keys:
                raise ValueError(f"duplicate environment key at line {number}")
            keys.add(key)
            value = value.strip()
            if value[:1] in {'"', "'"} and not value.endswith(value[0]):
                raise ValueError(f"unterminated quoted value at line {number}")

    def yaml_validator(content: bytes) -> None:
        import yaml

        class UniqueKeyLoader(yaml.SafeLoader):
            def construct_mapping(self, node, deep=False):
                explicit: set[Any] = set()
                for key_node, _value_node in node.value:
                    if key_node.tag == "tag:yaml.org,2002:merge":
                        continue
                    key = self.construct_object(key_node, deep=deep)
                    try:
                        duplicate = key in explicit
                    except TypeError as exc:
                        raise ValueError("YAML mapping key is not hashable") from exc
                    if duplicate:
                        raise ValueError(f"duplicate YAML mapping key: {key}")
                    explicit.add(key)
                return super().construct_mapping(node, deep=deep)
        try:
            yaml.load(content.decode("utf-8-sig"), Loader=UniqueKeyLoader)
        except yaml.YAMLError as exc:
            raise ValueError("invalid YAML document") from exc

    def powershell_validator(content: bytes) -> None:
        if os.name != "nt":
            raise RuntimeError("PowerShell syntax validation is unavailable on this host")
        system_root = _windows_directory()
        executable = (
            system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        ).resolve(strict=True)
        static_parser = (
            "$tokens=$null;$errors=$null;"
            "[Console]::InputEncoding=[Text.UTF8Encoding]::new($false);"
            "$source=[Console]::In.ReadToEnd();"
            "[System.Management.Automation.Language.Parser]::ParseInput("
            "$source,[ref]$tokens,[ref]$errors)|Out-Null;"
            "if($errors.Count -ne 0){exit 2}"
        )
        try:
            completed = subprocess.run(
                (
                    str(executable),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    static_parser,
                ),
                input=content,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("PowerShell syntax validator timed out") from exc
        if completed.returncode != 0:
            raise SyntaxError("PowerShell parser rejected the document")

    return {
        ".json": json_validator,
        ".toml": toml_validator,
        ".py": python_validator,
        ".xml": xml_validator,
        ".ini": ini_validator,
        ".cfg": ini_validator,
        ".env": env_validator,
        ".yaml": yaml_validator,
        ".yml": yaml_validator,
        ".ps1": powershell_validator,
    }


def _evidence(
    source: str,
    assertion: str,
    expected: Any,
    observed: Any,
    passed: bool,
    *,
    error: str | None = None,
    subject: str | None = None,
) -> VerificationEvidence:
    return VerificationEvidence(
        source,
        assertion,
        expected,
        observed,
        passed,
        datetime.now(UTC).isoformat(timespec="milliseconds"),
        error,
        subject,
    )


def _validate_network_target(host: str, port: int) -> tuple[str, int]:
    if not isinstance(host, str) or not host or len(host) > 253 or "\x00" in host:
        raise ValueError("invalid verification host")
    if any(character.isspace() for character in host):
        raise ValueError("verification host contains whitespace")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("verification port must be between 1 and 65535")
    return host.rstrip("."), port


def _read_registry_value(hive: RegistryHive, key: str, name: str) -> dict[str, Any]:
    if os.name != "nt":
        raise OSError("registry verification is available only on Windows")
    import winreg

    hive_handle = {
        RegistryHive.CURRENT_USER: winreg.HKEY_CURRENT_USER,
        RegistryHive.LOCAL_MACHINE: winreg.HKEY_LOCAL_MACHINE,
    }[hive]
    try:
        with winreg.OpenKey(hive_handle, key, 0, winreg.KEY_QUERY_VALUE) as handle:
            value, kind = winreg.QueryValueEx(handle, name)
    except FileNotFoundError:
        return {"exists": False}
    return {"exists": True, "value": value, "registry_kind": int(kind)}


def _registry_comparable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    return value


def _registry_kind_code(kind: RegistryValueKind) -> int:
    # Stable Win32 REG_* constants; kept platform-neutral for testability.
    return {
        RegistryValueKind.STRING: 1,
        RegistryValueKind.EXPAND_STRING: 2,
        RegistryValueKind.BINARY: 3,
        RegistryValueKind.DWORD: 4,
        RegistryValueKind.QWORD: 11,
    }[kind]


def _safe_registry_evidence(name: str, value: Any) -> Any:
    if not _SECRET_NAME_RE.search(name) or value is None:
        return value
    encoded = json.dumps(value, default=str, sort_keys=True).encode("utf-8")
    return {"redacted": True, "sha256": hashlib.sha256(encoded).hexdigest()}


def _registry_subject(
    action: RegistryGetAction | RegistrySetAction | RegistryDeleteValueAction,
) -> str:
    return f"{action.hive.value}\\{action.key}::{action.name}"


def _mutation_paths(action: AtomicAction) -> tuple[Path, ...]:
    if isinstance(action, CreateDirectoryAction | WriteFileAction | DeleteFileAction):
        return (action.path,)
    if isinstance(action, CopyFileAction):
        return (action.destination,)
    if isinstance(action, MoveFileAction):
        return (action.source, action.destination)
    return ()


def _risk_paths(action: AtomicAction) -> tuple[Path, ...]:
    paths = list(_mutation_paths(action))
    if isinstance(action, ProcessAction):
        paths.extend(action.request.observe_paths)
    return tuple(dict.fromkeys(paths))


def _default_protected_paths() -> tuple[Path, ...]:
    if os.name == "nt":
        system_root = _windows_directory()
        system_drive = Path(f"{system_root.drive}\\")
        values = [
            system_root,
            system_drive / "Program Files",
            system_drive / "Program Files (x86)",
            system_drive / "ProgramData",
        ]
        return tuple(Path(value) for value in values)
    values = ("/etc", "/usr", "/bin", "/sbin", "/boot", "/var", "/opt", "/System", "/Library")
    return tuple(Path(value) for value in values)


def _windows_directory() -> Path:
    if os.name != "nt":
        raise OSError("Windows directory discovery is available only on Windows")
    import ctypes

    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise OSError("GetWindowsDirectoryW failed")
    path = Path(buffer.value)
    if not path.is_absolute():
        raise OSError("GetWindowsDirectoryW returned a non-absolute path")
    return path.resolve(strict=True)


def _normalize_paths(paths: tuple[Path, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_normalized_path_text(path) for path in paths))


def _normalized_path_text(path: Path) -> str:
    raw = os.path.abspath(os.fspath(path))
    return os.path.normcase(os.path.normpath(raw))


def _path_contains(root: str, candidate: str) -> bool:
    with contextlib.suppress(ValueError):
        return os.path.commonpath((root, candidate)) == root
    return False


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise RuntimeError("file identity changed during safe-gate preflight")
        while chunk := handle.read(128 * 1024):
            digest.update(chunk)
        final = path.lstat()
        if (
            stat.S_ISLNK(final.st_mode)
            or (opened.st_dev, opened.st_ino) != (final.st_dev, final.st_ino)
        ):
            raise RuntimeError("file identity changed during safe-gate preflight")
    return digest.hexdigest()


def _action_fingerprint(action: AtomicAction) -> str:
    canonical = json.dumps(
        _canonical(action), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _simulation_fingerprint(simulation: tuple[GateEvidence, ...]) -> str:
    canonical = json.dumps(
        [asdict(item) for item in simulation],
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _state_fingerprint(value: Any) -> str:
    canonical = json.dumps(
        _canonical(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _expectation_digest(expectation: VerificationExpectation) -> str:
    canonical = _canonical_state(expectation)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_state(value: Any) -> str:
    return json.dumps(
        _canonical(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _canonical(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, tuple | list):
        return [_canonical(item) for item in value]
    raise TypeError(f"unsupported permit value: {type(value).__name__}")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
