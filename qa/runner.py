"""Safe, developer-only runner for offline, loopback HTTP, and allowlisted CLI cases.

Generalized from the committed functional harness for run
20260713T002206Z_686424795712. This module never imports from ``.audit`` and
does not start or stop JARVIS.
"""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .evidence import EvidenceStore
from .models import (
    AssertionResult,
    CampaignIdentity,
    CampaignSummary,
    CaseResult,
    Scenario,
    Verdict,
)
from .validators import run_validators
from .validators.context import ValidationContext

_JARVIS_CLI_ARGUMENTS = frozenset(
    {(command,) for command in ("profiles", "status", "models", "llm-health")}
)


@dataclass(frozen=True, slots=True)
class CliCommandSpec:
    """One logical request mapped to fixed arguments for the trusted launcher."""

    request_args: tuple[str, ...]
    jarvis_args: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not self.request_args
            or any(not isinstance(item, str) or not item for item in self.request_args)
            or self.jarvis_args not in _JARVIS_CLI_ARGUMENTS
        ):
            raise ValueError("CLI command specification is outside the fixed allowlist")


DEFAULT_CLI_SPECS = tuple(
    CliCommandSpec(
        ("py", "-3.11", "-m", "jarvis_gpt.cli", command),
        (command,),
    )
    for command in ("profiles", "status", "models", "llm-health")
)
DEFAULT_CLI_ALLOWLIST = frozenset(spec.request_args for spec in DEFAULT_CLI_SPECS)
DEFAULT_HTTP_ALLOWLIST = frozenset(
    {
        ("GET", "/health"),
        ("GET", "/api/status"),
        ("GET", "/api/models/profiles"),
    }
)


class BlockedByEnvironment(RuntimeError):
    pass


class BlockedBySpecification(RuntimeError):
    pass


def validate_loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base URL must use http or https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base URL cannot contain credentials, query, or fragment")
    hostname = parsed.hostname
    try:
        is_loopback_ip = bool(hostname and ipaddress.ip_address(hostname).is_loopback)
    except ValueError:
        is_loopback_ip = False
    if hostname != "localhost" and not is_loopback_ip:
        raise ValueError("base URL must target loopback")
    if parsed.path not in {"", "/"}:
        raise ValueError("base URL cannot contain an application path")
    return value.rstrip("/")


class LoopbackHttpExecutor:
    def __init__(
        self,
        base_url: str,
        *,
        allowed_routes: Iterable[tuple[str, str]] = DEFAULT_HTTP_ALLOWLIST,
        timeout: float = 30.0,
    ) -> None:
        import httpx

        self.base_url = validate_loopback_url(base_url)
        self.allowed_routes = frozenset((method.upper(), path) for method, path in allowed_routes)
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        method = str(request.get("method", "GET")).upper()
        path = str(request.get("path", ""))
        parsed = urlsplit(path)
        unsafe_path = parsed.scheme or parsed.netloc or parsed.query or parsed.fragment
        if unsafe_path or not path.startswith("/"):
            raise BlockedBySpecification("HTTP scenario path must be relative to loopback base URL")
        if (method, parsed.path) not in self.allowed_routes:
            raise BlockedBySpecification(f"HTTP route is not allowlisted: {method} {parsed.path}")
        response = self.client.request(
            method,
            path,
            json=request.get("json"),
            headers=dict(request.get("headers", {})),
        )
        content_type = response.headers.get("content-type", "")
        observation: dict[str, Any] = {
            "status_code": response.status_code,
            "content_type": content_type,
            "headers": {
                key: value
                for key, value in response.headers.items()
                if key.lower() in {"content-type", "content-length", "x-request-id"}
            },
        }
        if "json" in content_type:
            try:
                observation["json"] = response.json()
            except ValueError:
                observation["text"] = response.text
        else:
            observation["text"] = response.text
        return observation


class AllowlistedCliExecutor:
    def __init__(
        self,
        command_specs: Iterable[CliCommandSpec] = DEFAULT_CLI_SPECS,
        *,
        timeout: float = 60.0,
    ) -> None:
        commands: dict[tuple[str, ...], CliCommandSpec] = {}
        for spec in command_specs:
            if not isinstance(spec, CliCommandSpec):
                raise TypeError("CLI allowlist entries must be typed command specifications")
            if spec.request_args in commands:
                raise ValueError("duplicate CLI request specification")
            commands[spec.request_args] = spec
        if not commands:
            raise ValueError("CLI command specification allowlist cannot be empty")
        self.commands = commands
        self.timeout = timeout
        self.repository_root = Path(__file__).resolve().parents[1]
        self.interpreter = Path(sys.executable).resolve(strict=True)
        self.launcher = (self.repository_root / "qa" / "_trusted_jarvis_cli.py").resolve(
            strict=True
        )
        if (
            not self.interpreter.is_absolute()
            or not self.interpreter.is_file()
            or not self.launcher.is_relative_to(self.repository_root)
            or not self.launcher.is_file()
        ):
            raise RuntimeError("trusted CLI interpreter or launcher is unavailable")

    @staticmethod
    def _minimal_environment() -> dict[str, str]:
        inherited = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        environment = {
            key: os.environ[key]
            for key in inherited
            if key in os.environ and "\x00" not in os.environ[key]
        }
        environment["NO_COLOR"] = "1"
        return environment

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if set(request) != {"args"}:
            raise BlockedBySpecification("CLI request accepts only the fixed args field")
        args = request.get("args")
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise BlockedBySpecification("CLI args must be an explicit string array")
        requested = tuple(args)
        spec = self.commands.get(requested)
        if spec is None:
            raise BlockedBySpecification("CLI command is not on the exact allowlist")
        command = [
            str(self.interpreter),
            "-I",
            "-S",
            "-X",
            "utf8",
            str(self.launcher),
            *spec.jarvis_args,
        ]
        completed = subprocess.run(  # noqa: S603 - exact tuple allowlist above
            command,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            check=False,
            env=self._minimal_environment(),
            cwd=str(self.repository_root),
        )
        machine_result: dict[str, Any] = {}
        try:
            parsed = json.loads(completed.stdout)
            if isinstance(parsed, dict):
                machine_result = parsed
        except json.JSONDecodeError:
            pass
        return {
            "process_exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "machine_result": machine_result,
        }


class AssuranceRunner:
    def __init__(
        self,
        identity: CampaignIdentity,
        evidence_store: EvidenceStore,
        *,
        http_executor: LoopbackHttpExecutor | None = None,
        cli_executor: AllowlistedCliExecutor | None = None,
        validation_context: ValidationContext | None = None,
    ) -> None:
        if evidence_store.identity != identity:
            raise ValueError("evidence store identity does not match runner identity")
        self.identity = identity
        self.evidence_store = evidence_store
        self.http_executor = http_executor
        self.cli_executor = cli_executor or AllowlistedCliExecutor()
        self.validation_context = validation_context

    def close(self) -> None:
        if self.http_executor is not None:
            self.http_executor.close()

    def _execute(self, scenario: Scenario) -> Mapping[str, Any]:
        if scenario.transport == "offline":
            observation = scenario.request.get("observation")
            if not isinstance(observation, Mapping):
                raise BlockedBySpecification("offline scenario requires request.observation")
            return dict(observation)
        if scenario.transport == "http":
            if self.http_executor is None:
                raise BlockedByEnvironment("loopback HTTP executor was not configured")
            return self.http_executor.run(scenario.request)
        if scenario.transport == "cli":
            return self.cli_executor.run(scenario.request)
        raise BlockedBySpecification(f"unsupported transport {scenario.transport}")

    def run_case(self, scenario: Scenario) -> CaseResult:
        if scenario.skip_reason is not None:
            observation = {}
            assertions = (
                AssertionResult(
                    "runner.optional_skip",
                    True,
                    "explicit optional skip",
                    scenario.skip_reason,
                ),
            )
            verdict = Verdict.SKIP
            error = scenario.skip_reason
        else:
            try:
                observation = self._execute(scenario)
                assertions = tuple(
                    run_validators(
                        observation,
                        scenario.validators,
                        context=self.validation_context,
                    )
                )
                if not assertions:
                    assertions = (
                        AssertionResult(
                            "runner.assertions_present",
                            False,
                            "at least one assertion",
                            0,
                        ),
                    )
                    verdict = Verdict.ERROR
                    error = "no assertions were produced"
                elif any(not assertion.passed for assertion in assertions):
                    verdict = Verdict.FAIL
                    error = None
                elif scenario.semantic_review_required:
                    verdict = Verdict.INCONCLUSIVE
                    error = None
                else:
                    verdict = Verdict.PASS
                    error = None
            except BlockedByEnvironment as exc:
                observation = {}
                assertions = (
                    AssertionResult("runner.environment_available", False, "available", str(exc)),
                )
                verdict = Verdict.BLOCKED_BY_ENV
                error = str(exc)
            except BlockedBySpecification as exc:
                observation = {}
                assertions = (
                    AssertionResult("runner.specification_complete", False, "complete", str(exc)),
                )
                verdict = Verdict.BLOCKED_BY_SPEC
                error = str(exc)
            except Exception as exc:  # harness boundary, never a false PASS
                observation = {}
                assertions = (
                    AssertionResult(
                        "runner.completed_without_error",
                        False,
                        "completed",
                        f"{type(exc).__name__}: {exc}",
                    ),
                )
                verdict = Verdict.ERROR
                error = f"{type(exc).__name__}: {exc}"
        result = CaseResult(
            case_id=scenario.scenario_id,
            verdict=verdict,
            assertions=assertions,
            observation=observation,
            bounded_evidence={"transport": scenario.transport, "tags": list(scenario.tags)},
            required=scenario.required,
            semantic_review_required=scenario.semantic_review_required,
            error=error,
        )
        self.evidence_store.append(scenario, result)
        return result

    def run_suite(self, scenarios: Iterable[Scenario]) -> CampaignSummary:
        results = tuple(self.run_case(scenario) for scenario in scenarios)
        summary = CampaignSummary(self.identity, results)
        self.evidence_store.write_manifest(summary)
        return summary


def run_offline_suite(
    scenarios: Iterable[Scenario],
    output_root: Path,
    *,
    canaries: Iterable[str] = (),
    validation_context: ValidationContext | None = None,
) -> CampaignSummary:
    identity = CampaignIdentity.create()
    store = EvidenceStore(output_root, identity, canaries=canaries)
    runner = AssuranceRunner(identity, store, validation_context=validation_context)
    try:
        return runner.run_suite(scenarios)
    finally:
        runner.close()
