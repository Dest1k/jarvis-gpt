#!/usr/bin/env python3
"""Repository-local launcher for the Jarvis CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _main() -> None:
    root = Path(__file__).resolve().parent
    source = root / "backend" / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

    # Recovery-critical launchers need the profile registry even when an optional
    # runtime dependency is broken. Keep this exact command on the lightweight config
    # import path instead of importing the full CLI/agent/tool stack first.
    if sys.argv[1:] == ["profiles"]:
        from jarvis_gpt.config import PROFILES, profile_public_dict

        print(
            json.dumps(
                {name: profile_public_dict(profile) for name, profile in PROFILES.items()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    from jarvis_gpt.cli import main

    main()


if __name__ == "__main__":
    _main()
