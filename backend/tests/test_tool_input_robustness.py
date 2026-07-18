"""Tools accept the natural argument shapes a model emits (understand-with-half-a-word).

Audit found system.inspect failing 7× because the model passed the WMI class as a
bare string or a WQL query instead of the strict {class_name, properties} dict.
"""

from __future__ import annotations

import pytest
from jarvis_gpt.tools import (
    _native_payload_from_args,
    _normalize_tool_action,
    _validate_wmi_payload,
    _wmi_payload_from_string,
)


def _action_after(tool: str, action: str) -> str:
    args = {"action": action}
    _normalize_tool_action(tool, args)
    return args["action"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # separator/case folds (underscore-for-dot is the model's most common tic)
        ("wmi_query", "wmi.query"),
        ("WMI_QUERY", "wmi.query"),
        ("wmi.query", "wmi.query"),  # already valid → untouched
        ("hardware_memory", "hardware.memory"),
        ("hardware_disk", "hardware.disk"),
        # bare resource words a "сколько памяти / места на диске" turn tends to emit
        ("memory", "hardware.memory"),
        ("ram", "hardware.memory"),
        ("cpu", "hardware.cpu"),
        ("disk", "hardware.disk"),
        ("gpu", "hardware.gpu"),
        # NOVEL typos the old hardcoded alias dict never enumerated — only the general
        # fuzzy normalizer catches these (proves it fixes the cause, not each symptom)
        ("wmiqery", "wmi.query"),
        ("memmory", "hardware.memory"),
    ],
)
def test_system_inspect_action_normalizes(given: str, expected: str) -> None:
    assert _action_after("system.inspect", given) == expected


def test_system_inspect_unknown_action_left_for_fail_closed() -> None:
    # A genuinely-unknown action must be left in place so the tool's own read-only
    # validation still rejects it (fail-closed preserved, no wrong snap).
    assert _action_after("system.inspect", "teleport") == "teleport"


def test_windows_native_folds_underscores() -> None:
    assert _action_after("windows.native", "window_focus") == "window.focus"
    assert _action_after("windows.native", "clipboard_write") == "clipboard.write"
    assert _action_after("windows.native", "app_open_and_type") == "app.open_and_type"


def test_windows_native_never_fuzzy_snaps_a_mutation() -> None:
    # process.stop is invalid and textually close to process.start, but a mutating tool
    # must NEVER fuzzy-substitute one action for an opposite-meaning one.
    assert _action_after("windows.native", "process.stop") == "process.stop"


def test_normalizer_is_noop_for_unregistered_tool() -> None:
    args = {"action": "anything"}
    _normalize_tool_action("documents.generate", args)
    assert args["action"] == "anything"


def test_wmi_accepts_bare_class_name():
    validated = _validate_wmi_payload(_wmi_payload_from_string("Win32_Processor"))
    assert validated["class_name"] == "Win32_Processor"


def test_wmi_accepts_wql_query_with_properties():
    payload = _wmi_payload_from_string(
        "SELECT Name, NumberOfCores, NumberOfLogicalProcessors FROM Win32_Processor"
    )
    validated = _validate_wmi_payload(payload)
    assert validated["class_name"] == "Win32_Processor"
    assert validated["properties"] == ["Name", "NumberOfCores", "NumberOfLogicalProcessors"]


def test_wmi_accepts_select_star():
    payload = _wmi_payload_from_string("SELECT * FROM Win32_PhysicalMemory")
    assert payload["class_name"] == "Win32_PhysicalMemory"
    assert payload["properties"] == []


def test_wmi_strips_trailing_semicolon_and_whitespace():
    payload = _wmi_payload_from_string("  Win32_OperatingSystem ; ")
    assert _validate_wmi_payload(payload)["class_name"] == "Win32_OperatingSystem"


def test_wmi_wql_where_clause_becomes_filter():
    payload = _wmi_payload_from_string(
        "SELECT FreeSpace, Size FROM Win32_LogicalDisk WHERE DeviceID='C:'"
    )
    validated = _validate_wmi_payload(payload)
    assert validated["class_name"] == "Win32_LogicalDisk"
    assert validated["properties"] == ["FreeSpace", "Size"]
    assert validated["filter"] == "DeviceID='C:'"


def test_wmi_empty_or_garbage_is_empty():
    assert _wmi_payload_from_string("") == {}
    assert _wmi_payload_from_string("   ") == {}


def test_native_payload_accepts_top_level_executable():
    payload = _native_payload_from_args(
        "process.start", {"action": "process.start", "executable": "notepad.exe"}
    )
    assert payload["executable"] == "notepad.exe"


def test_native_payload_accepts_bare_string_executable_with_args():
    payload = _native_payload_from_args(
        "process.start", {"payload": "powershell.exe -Command systeminfo"}
    )
    assert payload["executable"] == "powershell.exe"
    assert payload["arguments"] == ["-Command", "systeminfo"]


def test_native_payload_preserves_nested_dict():
    payload = _native_payload_from_args(
        "app.open_and_type", {"payload": {"executable": "calc.exe", "keys": "12+34="}}
    )
    assert payload["executable"] == "calc.exe"
    assert payload["keys"] == "12+34="
