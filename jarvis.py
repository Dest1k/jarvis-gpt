#!/usr/bin/env python3
"""Repository-local launcher for the Jarvis CLI."""

from __future__ import annotations

import sys
from pathlib import Path


def _main() -> None:
    root = Path(__file__).resolve().parent
    source = root / "backend" / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

    from jarvis_gpt.cli import main

    main()


if __name__ == "__main__":
    _main()
