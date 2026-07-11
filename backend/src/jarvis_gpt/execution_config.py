from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .config import JarvisSettings
from .execution_kernel import ExecutionKernel, KernelCapabilities
from .execution_process import ExecutableRule

_CAPABILITIES_FILE_ENV = "JARVIS_EXECUTION_CAPABILITIES_FILE"
_ROOTS_ENV = "JARVIS_EXECUTION_ROOTS"
_MAX_CAPABILITIES_BYTES = 1024 * 1024
_ALLOWED_CAPABILITY_KEYS = frozenset(
    {
        "executables",
        "network_hosts",
        "allow_private_network",
        "registry_read_prefixes",
        "registry_write_prefixes",
        "allow_inherited_process_environment",
    }
)


def build_execution_kernel(
    settings: JarvisSettings,
    *,
    recover_checkpoints: bool = True,
) -> ExecutionKernel:
    roots = execution_roots(settings)
    capabilities = load_execution_capabilities(settings, roots=roots)
    return ExecutionKernel(
        allowed_roots=roots,
        state_dir=settings.state_dir,
        denied_paths=execution_denied_paths(settings, capabilities=capabilities),
        capabilities=capabilities,
        recover_checkpoints=recover_checkpoints,
    )


def execution_roots(settings: JarvisSettings) -> tuple[Path, ...]:
    configured = os.environ.get(_ROOTS_ENV, "").strip()
    raw_roots = (
        [item.strip() for item in configured.split(os.pathsep) if item.strip()]
        if configured
        else [str(Path.cwd()), str(settings.home)]
    )
    roots: list[Path] = []
    for raw in raw_roots:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{_ROOTS_ENV} entries must be absolute: {raw!r}")
        if path.is_symlink():
            raise ValueError(f"execution root cannot be a symlink: {path}")
        resolved = path.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"execution root is not a directory: {resolved}")
        if resolved.parent == resolved:
            raise ValueError("filesystem root cannot be an execution root")
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise ValueError("at least one execution root is required")
    return tuple(roots)


def execution_denied_paths(
    settings: JarvisSettings,
    *,
    capabilities: KernelCapabilities | None = None,
) -> tuple[Path, ...]:
    candidates = [
        settings.home / ".jarvis",
        settings.home / "bridge.token",
        settings.home / "host_profile.json",
        settings.state_dir,
        settings.log_dir,
        Path.cwd() / ".env",
        Path.home() / ".jarvis",
    ]
    capabilities_file = os.environ.get(_CAPABILITIES_FILE_ENV, "").strip()
    if capabilities_file:
        candidates.append(Path(capabilities_file).expanduser().resolve(strict=True))
    if capabilities is not None:
        candidates.extend(rule.executable for rule in capabilities.executable_rules)
    return tuple(dict.fromkeys(path.resolve(strict=False) for path in candidates))


def load_execution_capabilities(
    settings: JarvisSettings,
    *,
    roots: tuple[Path, ...] | None = None,
) -> KernelCapabilities:
    raw_path = os.environ.get(_CAPABILITIES_FILE_ENV, "").strip()
    if not raw_path:
        return KernelCapabilities()
    allowed_roots = roots or execution_roots(settings)
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{_CAPABILITIES_FILE_ENV} must be an absolute path")
    if path.is_symlink():
        raise ValueError("execution capabilities file cannot be a symlink")
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("execution capabilities file must be a regular non-symlink file")
    if not any(path == root or path.is_relative_to(root) for root in allowed_roots):
        raise ValueError("execution capabilities file is outside configured execution roots")
    if path.stat().st_size > _MAX_CAPABILITIES_BYTES:
        raise ValueError("execution capabilities file is too large")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid execution capabilities JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("execution capabilities JSON must be an object")
    unknown = set(payload) - _ALLOWED_CAPABILITY_KEYS
    if unknown:
        raise ValueError(f"unknown execution capability keys: {', '.join(sorted(unknown))}")
    return KernelCapabilities(
        executable_rules=_executable_rules(payload.get("executables")),
        network_hosts=frozenset(_string_list(payload.get("network_hosts"), "network_hosts")),
        allow_private_network=_strict_bool(
            payload.get("allow_private_network", False), "allow_private_network"
        ),
        registry_read_prefixes=_registry_prefixes(
            payload.get("registry_read_prefixes"), "registry_read_prefixes"
        ),
        registry_write_prefixes=_registry_prefixes(
            payload.get("registry_write_prefixes"), "registry_write_prefixes"
        ),
        allow_inherited_process_environment=_strict_bool(
            payload.get("allow_inherited_process_environment", False),
            "allow_inherited_process_environment",
        ),
    )


def execution_capabilities_snapshot(kernel: ExecutionKernel) -> dict[str, Any]:
    capabilities = kernel.capabilities
    return {
        "allowed_roots": [str(path) for path in kernel.path_policy.roots],
        "denied_paths": [str(path) for path in kernel.path_policy.denied_paths],
        "state_dir": str(kernel.checkpoints.checkpoint_root.parent),
        "process": {
            "enabled": bool(capabilities.executable_rules),
            "executables": [str(rule.executable) for rule in capabilities.executable_rules],
            "inherit_environment": capabilities.allow_inherited_process_environment,
            "shells_allowed": False,
        },
        "network": {
            "hosts": sorted(capabilities.network_hosts),
            "private_targets": capabilities.allow_private_network,
        },
        "registry": {
            "read_prefixes": [list(item) for item in capabilities.registry_read_prefixes],
            "write_prefixes": [list(item) for item in capabilities.registry_write_prefixes],
        },
        "startup_recovery": [
            {
                "checkpoint_id": item.checkpoint_id,
                "status": item.status.value,
                "rollback_errors": list(item.rollback_errors),
            }
            for item in kernel.recovered_checkpoints
        ],
        "mutation_gate": {
            "available": not kernel.rollback_degraded,
            "status": "rollback_degraded" if kernel.rollback_degraded else "ready",
            "unresolved_checkpoint_ids": list(kernel.rollback_degraded_checkpoint_ids),
        },
        "capabilities_file_env": _CAPABILITIES_FILE_ENV,
        "roots_env": _ROOTS_ENV,
    }


def _executable_rules(value: Any) -> tuple[ExecutableRule, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("executables must be a list with at most 64 rules")
    rules: list[ExecutableRule] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict) or set(raw) - {
            "path",
            "argument_patterns",
            "additional_argument_pattern",
            "environment_patterns",
        }:
            raise ValueError(f"executables[{index}] has an invalid shape")
        path = Path(str(raw.get("path") or "")).expanduser()
        if not path.is_absolute():
            raise ValueError(f"executables[{index}].path must be absolute")
        if path.is_symlink():
            raise ValueError(f"executables[{index}].path cannot be a symlink")
        path = path.resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"executables[{index}].path must be a regular executable")
        patterns = _string_list(
            raw.get("argument_patterns"),
            f"executables[{index}].argument_patterns",
            maximum=128,
        )
        tail = raw.get("additional_argument_pattern")
        if tail is not None and not isinstance(tail, str):
            raise ValueError(
                f"executables[{index}].additional_argument_pattern must be a string or null"
            )
        environment_patterns = _environment_patterns(
            raw.get("environment_patterns"),
            f"executables[{index}].environment_patterns",
        )
        for pattern in (
            *patterns,
            *((tail,) if tail is not None else ()),
            *(pattern for _name, pattern in environment_patterns),
        ):
            if len(pattern) > 1000:
                raise ValueError("executable argument regex is too long")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid executable argument regex: {exc}") from exc
        rules.append(
            ExecutableRule(
                executable=path,
                argument_patterns=patterns,
                additional_argument_pattern=tail,
                environment_patterns=environment_patterns,
            )
        )
    return tuple(rules)


def _environment_patterns(value: Any, field: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or len(value) > 128:
        raise ValueError(f"{field} must be an object with at most 128 entries")
    result: list[tuple[str, str]] = []
    for name, pattern in value.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_()]{0,127}", name)
            or not isinstance(pattern, str)
            or len(pattern) > 1000
        ):
            raise ValueError(f"{field} contains an invalid name or regex")
        result.append((name, pattern))
    return tuple(sorted(result))


def _registry_prefixes(value: Any, field: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError(f"{field} must be a list with at most 64 entries")
    prefixes: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(part, str) and part.strip() for part in item)
        ):
            raise ValueError(f"{field}[{index}] must be [hive, key_prefix]")
        hive, prefix = (part.strip() for part in item)
        if hive not in {"HKEY_CURRENT_USER", "HKEY_LOCAL_MACHINE"}:
            raise ValueError(f"{field}[{index}] has an unsupported registry hive")
        prefixes.append((hive, prefix))
    return tuple(prefixes)


def _string_list(value: Any, field: str, *, maximum: int = 256) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} must be a list with at most {maximum} strings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item or "\x00" in item:
            raise ValueError(f"{field}[{index}] must be a non-empty NUL-free string")
        result.append(item)
    return tuple(result)


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value
