from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_bridge_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "windows_rpc_bridge.py"
    spec = importlib.util.spec_from_file_location("windows_rpc_bridge_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_raw_execute_endpoint_is_gone_and_action_requires_authentication():
    bridge = _load_bridge_module()
    server = bridge.BridgeServer(("127.0.0.1", 0), "test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
    try:
        connection.request(
            "POST",
            "/execute",
            body=json.dumps({"command": "Write-Output unsafe"}),
            headers={"Content-Type": "application/json"},
        )
        removed = connection.getresponse()
        removed.read()
        assert removed.status == 410

        connection.request(
            "POST",
            "/action",
            body=json.dumps({"action": "capabilities", "payload": {}}),
            headers={"Content-Type": "application/json"},
        )
        unauthorized = connection.getresponse()
        unauthorized.read()
        assert unauthorized.status == 401
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_action_contract_rejects_unknown_fields_shells_and_string_arguments():
    bridge = _load_bridge_module()

    with pytest.raises(bridge.ActionValidationError, match="Unknown request"):
        bridge.validate_action_request(
            {"action": "capabilities", "payload": {}, "command": "whoami"}
        )
    with pytest.raises(bridge.ActionValidationError, match="Shells, script hosts"):
        bridge.validate_action_request(
            {
                "action": "process.start",
                "payload": {"executable": "powershell.exe", "arguments": []},
            }
        )
    with pytest.raises(bridge.ActionValidationError, match="arguments must be a list"):
        bridge.validate_action_request(
            {
                "action": "process.start",
                "payload": {"executable": "notepad.exe", "arguments": "file.txt"},
            }
        )
    with pytest.raises(bridge.ActionValidationError, match="Unknown process.start"):
        bridge.validate_action_request(
            {
                "action": "process.start",
                "payload": {
                    "executable": "notepad.exe",
                    "arguments": [],
                    "shell": True,
                },
            }
        )
    with pytest.raises(bridge.ActionValidationError, match="desktop application allowlist"):
        bridge.validate_action_request(
            {
                "action": "process.start",
                "payload": {"executable": "schtasks.exe", "arguments": []},
            }
        )
    with pytest.raises(bridge.ActionValidationError, match="application grammar"):
        bridge.validate_action_request(
            {
                "action": "process.start",
                "payload": {"executable": "notepad.exe", "arguments": ["--unsafe"]},
            }
        )
    with pytest.raises(bridge.ActionValidationError, match="Unknown process view"):
        bridge.validate_action_request(
            {
                "action": "process.top",
                "payload": {"limit": 10, "sort": "cpu", "command": "whoami"},
            }
        )


@pytest.mark.parametrize("action", ("process.top", "console.show_processes"))
def test_process_view_contract_is_bounded_and_enum_only(action):
    bridge = _load_bridge_module()

    validated, payload, timeout = bridge.validate_action_request(
        {"action": action, "payload": {"limit": 10, "sort": "memory"}}
    )

    assert validated == action
    assert payload == {"limit": 10, "sort": "memory"}
    assert timeout == 30
    for invalid_payload in (
        {"limit": 0, "sort": "cpu"},
        {"limit": 51, "sort": "cpu"},
        {"limit": "10", "sort": "cpu"},
        {"limit": 10, "sort": "cpu; Get-Process"},
    ):
        with pytest.raises(bridge.ActionValidationError):
            bridge.validate_action_request({"action": action, "payload": invalid_payload})


def test_native_app_policy_rejects_path_aliases_and_accepts_explicit_exact_paths(
    monkeypatch, tmp_path
):
    bridge = _load_bridge_module()
    untrusted = tmp_path / "Untrusted" / "notepad.exe"
    untrusted.parent.mkdir()
    untrusted.write_bytes(b"not-an-app")

    with pytest.raises(bridge.ActionValidationError, match="canonical installation"):
        bridge._resolve_executable(str(untrusted))

    custom_code = tmp_path / "Custom VS Code" / "Code.exe"
    custom_code.parent.mkdir()
    custom_code.write_bytes(b"operator-pinned")
    monkeypatch.setenv(
        bridge.APP_PATHS_ENV,
        json.dumps({"code.exe": str(custom_code)}),
    )

    assert bridge._resolve_executable(str(custom_code)) == str(custom_code.resolve())


def test_native_app_argument_grammars_reject_windows_devices_and_ads(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    valid = tmp_path / "note.txt"

    action, payload, _timeout = bridge.validate_action_request(
        {
            "action": "app.open_and_type",
            "payload": {
                "executable": "notepad.exe",
                "arguments": [str(valid)],
                "text": "hello",
            },
        }
    )

    assert action == "app.open_and_type"
    assert payload["arguments"] == [str(valid)]
    for invalid in (
        "CON.txt",
        "CON .txt",
        "CLOCK$.txt",
        "CONOUT$.txt",
        "bad?.txt",
        "file.txt:stream.txt",
    ):
        with pytest.raises(bridge.ActionValidationError, match="application grammar"):
            bridge.validate_action_request(
                {
                    "action": "process.start",
                    "payload": {
                        "executable": "notepad.exe",
                        "arguments": [str(tmp_path / invalid)],
                    },
                }
            )
    _action, calculator, _timeout = bridge.validate_action_request(
        {
            "action": "process.start",
            "payload": {
                "executable": "explorer.exe",
                "arguments": [bridge.CALCULATOR_APP_URI],
            },
        }
    )
    assert calculator["arguments"] == [bridge.CALCULATOR_APP_URI]


def test_capabilities_publish_versioned_native_app_policy():
    bridge = _load_bridge_module()

    capabilities = bridge._capabilities_result()

    assert capabilities["policy_revision"] == bridge.BRIDGE_POLICY_REVISION
    assert capabilities["process_policy"]["revision"] == bridge.BRIDGE_POLICY_REVISION
    assert len(capabilities["app_paths_sha256"]) == 64
    assert capabilities["process_policy"]["allowed_apps"] == sorted(bridge.NATIVE_APP_NAMES)
    assert "argument_grammars" in capabilities["process_policy"]
    assert capabilities["process_policy"]["process_views"] == {
        "actions": ["console.show_processes", "process.top"],
        "limit": {"minimum": 1, "maximum": 50},
        "sorts": ["cpu", "memory", "name", "pid"],
    }


@pytest.mark.parametrize(
    "raw",
    (
        "{bad",
        json.dumps({"unknown.exe": r"C:\Apps\unknown.exe"}),
        json.dumps({"code.exe": r"C:\Apps\not-code.exe"}),
    ),
)
def test_capabilities_fail_closed_on_invalid_configured_app_paths(monkeypatch, raw):
    bridge = _load_bridge_module()
    monkeypatch.setenv(bridge.APP_PATHS_ENV, raw)

    with pytest.raises(bridge.ActionValidationError):
        bridge._capabilities_result()


def test_configured_app_paths_treat_special_characters_as_literals(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    executable = tmp_path / "100% $tools ~ reviewed" / "code.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"reviewed executable fixture")
    monkeypatch.setenv(
        bridge.APP_PATHS_ENV,
        json.dumps({"code.exe": str(executable)}),
    )

    assert bridge._configured_app_paths() == {"code.exe": executable}


def test_process_start_uses_direct_validated_argv_without_shell(monkeypatch):
    bridge = _load_bridge_module()
    captured = {}

    class FakeProcess:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        bridge,
        "_resolve_executable",
        lambda _value: r"C:\Windows\System32\notepad.exe",
    )
    monkeypatch.setattr(bridge.subprocess, "Popen", fake_popen)

    result, status = bridge.execute_action(
        {
            "action": "process.start",
            "payload": {
                "executable": "notepad.exe",
                "arguments": [],
            },
            "timeout_sec": 10,
        }
    )

    assert status == 200
    assert result["ok"] is True
    assert result["pid"] == 4242
    assert captured["argv"] == [
        r"C:\Windows\System32\notepad.exe",
    ]
    assert result["argv"] == [
        r"C:\Windows\System32\notepad.exe",
    ]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL


def test_fixed_process_console_never_executes_request_text(monkeypatch):
    bridge = _load_bridge_module()
    captured = {}

    class FakeProcess:
        pid = 4343

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        bridge,
        "_canonical_windows_powershell",
        lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )
    monkeypatch.setattr(bridge.subprocess, "Popen", fake_popen)

    result, status = bridge.execute_action(
        {
            "action": "console.show_processes",
            "payload": {"limit": 10, "sort": "cpu"},
        }
    )

    assert status == 200
    assert result["ok"] is True
    assert result["pid"] == 4343
    assert captured["kwargs"]["shell"] is False
    assert "-Command" not in captured["argv"]
    encoded = captured["argv"][captured["argv"].index("-EncodedCommand") + 1]
    assert base64.b64decode(encoded).decode("utf-16-le") == bridge.FIXED_PROCESS_CONSOLE_POWERSHELL
    assert json.loads(captured["kwargs"]["env"]["JARVIS_PROCESS_VIEW_JSON"]) == {
        "limit": 10,
        "sort": "cpu",
    }
    process_block = bridge.FIXED_PROCESS_CONSOLE_POWERSHELL
    assert process_block.index("Sort-Object") < process_block.index("Select-Object -First")


def test_process_top_fixed_script_sorts_before_limiting():
    bridge = _load_bridge_module()
    process_block = bridge.FIXED_NATIVE_POWERSHELL.split("'process.top' {", 1)[1].split(
        "'window.list' {", 1
    )[0]

    assert process_block.index("Sort-Object") < process_block.index("Select-Object -First")


def test_process_argv_redaction_covers_split_inline_and_url_secrets():
    bridge = _load_bridge_module()

    assert bridge.redact_process_argv(
        [
            r"C:\Apps\Acme\acme.exe",
            "--mode",
            "safe value",
            "--password",
            "TOPSECRET",
            "--token=INLINESECRET",
            "https://alice:url-secret@example.test/path",
        ]
    ) == [
        r"C:\Apps\Acme\acme.exe",
        "--mode",
        "safe value",
        "--password",
        "[REDACTED]",
        "--token=[REDACTED]",
        "https://[REDACTED]@example.test/path",
    ]


def test_native_actions_execute_only_the_fixed_encoded_script(monkeypatch):
    bridge = _load_bridge_module()
    captured = {}
    marker = "'; Write-Output NEVER_EXECUTE; #"

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        native = {
            "ok": True,
            "summary": "WMI complete.",
            "action": "wmi.query",
            "data": {"items": []},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(native), "")

    monkeypatch.setattr(bridge, "powershell_path", lambda: "powershell.exe")
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)

    result, status = bridge.execute_action(
        {
            "action": "wmi.query",
            "payload": {
                "class_name": "Win32_OperatingSystem",
                "properties": ["Caption"],
                "filter": marker,
            },
        }
    )

    assert status == 200
    assert result["ok"] is True
    encoded = captured["argv"][captured["argv"].index("-EncodedCommand") + 1]
    decoded_script = base64.b64decode(encoded).decode("utf-16-le")
    assert decoded_script == bridge.FIXED_NATIVE_POWERSHELL
    assert marker not in decoded_script
    assert marker not in " ".join(captured["argv"])
    envelope = json.loads(captured["kwargs"]["env"]["JARVIS_BRIDGE_ACTION_JSON"])
    assert envelope["payload"]["filter"] == marker
    assert captured["kwargs"].get("shell", False) is False


def test_windows_powershell_fixed_invocation_uses_sta_and_no_bypass():
    bridge = _load_bridge_module()

    command = bridge.powershell_command(
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    )

    assert "-STA" in command
    assert "-NonInteractive" in command
    assert "Bypass" not in command
    assert "-EncodedCommand" in command
    assert "-Command" not in command


def test_existing_bridge_token_is_protected_on_every_read(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    token_path = tmp_path / ".jarvis" / "bridge.token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("existing-token\n", encoding="utf-8")
    protected = []
    monkeypatch.setattr(bridge, "_protect_token_file", protected.append)

    assert bridge.ensure_token(token_path) == "existing-token"
    assert protected == [token_path]


def test_bridge_token_is_published_only_after_complete_temp_write(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    token_path = tmp_path / ".jarvis" / "bridge.token"
    observed = []
    real_link = bridge.os.link

    def verified_link(source, destination):
        observed.append(Path(source).read_text(encoding="utf-8"))
        return real_link(source, destination)

    protected = []
    monkeypatch.setattr(bridge.secrets, "token_urlsafe", lambda _size: "atomic-token")
    monkeypatch.setattr(bridge.os, "link", verified_link)
    monkeypatch.setattr(bridge, "_protect_token_file", protected.append)

    assert bridge.ensure_token(token_path) == "atomic-token"
    assert token_path.read_text(encoding="utf-8") == "atomic-token\n"
    assert observed == ["atomic-token\n"]
    assert protected[-1] == token_path
    assert len(protected) == 2


def test_windows_token_acl_removes_broad_principals_with_fixed_argv(monkeypatch, tmp_path):
    bridge = _load_bridge_module()
    token_path = tmp_path / "bridge token.txt"
    calls = []
    monkeypatch.setattr(bridge, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        bridge,
        "_windows_system_binary",
        lambda name: rf"C:\Windows\System32\{name}",
    )
    monkeypatch.setattr(bridge, "_current_user_sid", lambda: "S-1-5-21-1000")
    monkeypatch.setattr(
        bridge,
        "_run_security_command",
        lambda argv: calls.append(argv),
    )

    bridge._protect_token_file(token_path)

    assert calls[0] == [
        r"C:\Windows\System32\icacls.exe",
        str(token_path),
        "/grant:r",
        "*S-1-5-21-1000:(F)",
        "/Q",
    ]
    assert calls[1][-2:] == ["/inheritance:r", "/Q"]
    removed = {call[3] for call in calls[2:]}
    assert removed == {"*S-1-1-0", "*S-1-5-11", "*S-1-5-32-545"}


def test_security_command_never_uses_a_shell(monkeypatch):
    bridge = _load_bridge_module()
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    result = bridge._run_security_command(["icacls.exe", r"C:\safe path\token", "/Q"])

    assert result.returncode == 0
    assert captured["argv"] == ["icacls.exe", r"C:\safe path\token", "/Q"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL integration test")
def test_windows_token_acl_commands_succeed_on_a_real_file(tmp_path):
    bridge = _load_bridge_module()
    token_path = tmp_path / "bridge token.txt"
    token_path.write_text("test-token\n", encoding="utf-8")

    bridge._protect_token_file(token_path)

    assert token_path.read_text(encoding="utf-8") == "test-token\n"


def test_focus_hint_process_name_maps_launcher_style_apps():
    bridge = _load_bridge_module()

    assert bridge._focus_hint_process_name("calc.exe", []) == "Calculator"
    assert bridge._focus_hint_process_name(r"C:\Windows\System32\notepad.exe", []) == "Notepad"
    assert bridge._focus_hint_process_name("winword.exe", []) == "WINWORD"
    assert (
        bridge._focus_hint_process_name("explorer.exe", [bridge.CALCULATOR_APP_URI])
        == "Calculator"
    )
    # explorer without the calculator URI, and unknown apps, own their own window.
    assert bridge._focus_hint_process_name("explorer.exe", []) == ""
    assert bridge._focus_hint_process_name("someapp.exe", []) == ""


def test_app_open_and_type_injects_focus_hint_and_prefers_focused_pid(monkeypatch):
    bridge = _load_bridge_module()
    captured = {}

    def fake_start(action, payload):
        return {"ok": True, "pid": 4242, "argv": [r"C:\Windows\System32\calc.exe"]}

    def fake_native(action, native_payload, timeout_sec):
        captured["action"] = action
        captured["native_payload"] = native_payload
        return {
            "ok": True,
            "action": action,
            "summary": "Application focused and native input sent.",
            "data": {
                "focused": True,
                "pid": 4242,
                "focus_pid": 9999,
                "focus_process": "Calculator",
                "foreground_confirmed": True,
            },
        }

    monkeypatch.setattr(bridge, "_start_process", fake_start)
    monkeypatch.setattr(bridge, "_run_fixed_native_action", fake_native)

    result, status = bridge.execute_action(
        {"action": "app.open_and_type", "payload": {"executable": "calc.exe", "text": "2+2="}}
    )

    assert status == 200
    assert result["ok"] is True
    # The calculator window belongs to a different, locale-invariant process, so the
    # bridge steers focus by name rather than the dead launcher PID.
    assert captured["native_payload"]["process_name"] == "Calculator"
    assert result["launch_pid"] == 4242
    assert result["pid"] == 9999  # the actually-focused window, not the launch stub


def test_app_open_and_type_respects_explicit_focus_target(monkeypatch):
    bridge = _load_bridge_module()
    captured = {}

    monkeypatch.setattr(
        bridge, "_start_process", lambda action, payload: {"pid": 10, "argv": ["notepad"]}
    )

    def fake_native(action, native_payload, timeout_sec):
        captured["native_payload"] = native_payload
        return {"ok": True, "action": action, "summary": "done", "data": {"focused": True}}

    monkeypatch.setattr(bridge, "_run_fixed_native_action", fake_native)

    bridge.execute_action(
        {
            "action": "app.open_and_type",
            "payload": {
                "executable": "notepad.exe",
                "text": "hi",
                "window_title": "Untitled",
            },
        }
    )

    # An explicit caller-supplied target is never overridden by the derived hint.
    assert captured["native_payload"].get("process_name", "") == ""
    assert captured["native_payload"]["window_title"] == "Untitled"


def test_focus_script_uses_robust_foreground_and_reports_focus():
    bridge = _load_bridge_module()
    script = bridge.FIXED_NATIVE_POWERSHELL

    # Robust foreground handling that defeats the OS foreground lock.
    for marker in (
        "AttachThreadInput",
        "BringWindowToTop",
        "GetForegroundWindow",
        "keybd_event",
    ):
        assert marker in script, marker
    # Locale-invariant window matching by process-name substring.
    assert "$_.ProcessName -like ('*' + $needle + '*')" in script
    # The focused window's identity is reported for downstream verification.
    for field in ("focus_pid", "focus_process", "foreground_confirmed"):
        assert field in script, field
