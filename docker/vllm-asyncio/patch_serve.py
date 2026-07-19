#!/usr/bin/env python3
"""Fail-closed, deterministic patch for vLLM's single-process HTTP runner."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

EXPECTED_SOURCE_SHA256 = "abaa0233f6e00ac8acbd528c3cc6a63d6e5ee09e5baedffd922d961eefe91af8"

_SOURCE_IMPORT = b"import argparse\n"
_PATCHED_IMPORT = b"import argparse\nimport asyncio\n"
_SOURCE_RUNNER = b"            uvloop.run(run_server(args))\n"
_PATCHED_RUNNER = b"            asyncio.run(run_server(args))\n"


class PatchError(RuntimeError):
    """The upstream file does not exactly match the reviewed patch contract."""


def _replace_exactly_once(source: bytes, old: bytes, new: bytes, *, label: str) -> bytes:
    count = source.count(old)
    if count != 1:
        raise PatchError(f"expected exactly one {label}; found {count}")
    return source.replace(old, new, 1)


def patch_source(source: bytes) -> bytes:
    """Return the reviewed patched bytes, rejecting any unreviewed upstream input."""

    actual_sha256 = hashlib.sha256(source).hexdigest()
    if actual_sha256 != EXPECTED_SOURCE_SHA256:
        raise PatchError(
            "unexpected upstream serve.py SHA256: "
            f"expected {EXPECTED_SOURCE_SHA256}, got {actual_sha256}"
        )
    if _PATCHED_IMPORT in source or _PATCHED_RUNNER in source:
        raise PatchError("serve.py already contains patched asyncio code")

    patched = _replace_exactly_once(
        source,
        _SOURCE_IMPORT,
        _PATCHED_IMPORT,
        label="argparse import anchor",
    )
    patched = _replace_exactly_once(
        patched,
        _SOURCE_RUNNER,
        _PATCHED_RUNNER,
        label="uvloop runner",
    )

    # Compile before writing so a malformed result cannot replace the image's source.
    compile(patched.decode("utf-8"), "serve.py", "exec")
    return patched


def patch_file(path: Path) -> None:
    source = path.read_bytes()
    patched = patch_source(source)
    path.write_bytes(patched)
    if path.read_bytes() != patched:
        raise PatchError("patched serve.py failed byte-for-byte write verification")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} /absolute/path/to/serve.py", file=sys.stderr)
        return 2
    try:
        patch_file(Path(argv[1]))
    except (OSError, PatchError, UnicodeError, SyntaxError) as exc:
        print(f"vLLM serve.py patch refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
