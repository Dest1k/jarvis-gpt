from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_bridge_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "windows_rpc_bridge.py"
    spec = importlib.util.spec_from_file_location("windows_rpc_bridge_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_windows_powershell_bridge_invocation_uses_sta_for_gui_automation():
    bridge = _load_bridge_module()

    command = bridge.powershell_command(
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "Write-Output ok",
    )

    assert "-STA" in command
    assert "-NonInteractive" not in command
    assert command[-2:] == ["-Command", "Write-Output ok"]


def test_pwsh_bridge_invocation_keeps_noninteractive_cli_mode():
    bridge = _load_bridge_module()

    command = bridge.powershell_command("pwsh", "Write-Output ok")

    assert "-STA" not in command
    assert "-NonInteractive" in command
    assert command[-2:] == ["-Command", "Write-Output ok"]
