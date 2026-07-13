"""Trusted out-of-band roots and limits for deterministic validators."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from types import MappingProxyType

from ..safe_paths import (
    DEFAULT_MAX_FILE_BYTES,
    MAX_CONFIGURABLE_FILE_BYTES,
    canonical_directory,
    validate_root_alias,
)


@dataclass(frozen=True, slots=True)
class ValidationContext:
    artifact_roots: Mapping[str, Path] = field(default_factory=dict)
    max_artifact_bytes: int = DEFAULT_MAX_FILE_BYTES

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_artifact_bytes, int)
            or isinstance(self.max_artifact_bytes, bool)
            or not 0 < self.max_artifact_bytes <= MAX_CONFIGURABLE_FILE_BYTES
        ):
            raise ValueError("max_artifact_bytes is outside the trusted limit")
        canonical: dict[str, Path] = {}
        for alias, root in self.artifact_roots.items():
            safe_alias = validate_root_alias(alias)
            if safe_alias in canonical:
                raise ValueError("duplicate artifact root alias")
            windows_root = PureWindowsPath(str(root))
            if (
                not Path(root).is_absolute()
                or str(root).startswith(("\\\\", "//"))
                or windows_root.drive.startswith("\\\\")
            ):
                raise ValueError("trusted artifact roots must be absolute")
            canonical[safe_alias] = canonical_directory(root)
        object.__setattr__(self, "artifact_roots", MappingProxyType(canonical))

    def artifact_root(self, alias: object) -> Path:
        safe_alias = validate_root_alias(alias)
        try:
            return self.artifact_roots[safe_alias]
        except KeyError as exc:
            raise ValueError("artifact root alias is not trusted") from exc
