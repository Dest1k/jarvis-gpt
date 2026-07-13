"""Isolated absolute launcher for the small JARVIS read-only CLI allowlist."""

from __future__ import annotations

import sys
import sysconfig
from pathlib import Path

_ALLOWED_ARGUMENTS = frozenset({("profiles",), ("status",), ("models",), ("llm-health",)})


def _configure_trusted_imports() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    module_root = (repository_root / "backend" / "src").resolve(strict=True)
    if not module_root.is_relative_to(repository_root) or not (
        module_root / "jarvis_gpt" / "__init__.py"
    ).is_file():
        raise RuntimeError("trusted JARVIS module root is unavailable")

    interpreter_roots = {
        Path(value).resolve(strict=True)
        for value in {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    }
    trusted_paths: list[str] = [str(module_root)]
    for value in sys.path:
        if not value:
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            continue
        resolved = candidate.resolve(strict=False)
        if any(resolved.is_relative_to(root) for root in interpreter_roots):
            trusted_paths.append(str(resolved))
    for key in ("purelib", "platlib"):
        value = sysconfig.get_path(key)
        if not value:
            continue
        candidate = Path(value).resolve(strict=False)
        if any(candidate.is_relative_to(root) for root in interpreter_roots) and candidate.is_dir():
            trusted_paths.append(str(candidate))
    sys.path[:] = list(dict.fromkeys(trusted_paths))


def main() -> int:
    arguments = tuple(sys.argv[1:])
    if arguments not in _ALLOWED_ARGUMENTS:
        raise SystemExit("CLI arguments are not allowlisted")
    _configure_trusted_imports()
    from jarvis_gpt.cli import main as jarvis_main

    jarvis_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
