from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

MAX_BODY_BYTES = 65_536
MAX_OUTPUT_CHARS = 40_000
MAX_ARGUMENTS = 128
MAX_ARGUMENT_CHARS = 2_048
MAX_ARGUMENTS_TOTAL_CHARS = 8_192
PROCESS_TOP_MAX_LIMIT = 50
PROCESS_TOP_SORTS = frozenset({"cpu", "memory", "name", "pid"})

ACTION_NAMES = frozenset(
    {
        "app.open_and_type",
        "capabilities",
        "chrome.launch",
        "console.show_processes",
        "keyboard.send",
        "process.start",
        "process.top",
        "screen.capture",
        "url.open",
        "window.focus",
        "window.list",
        "wmi.query",
    }
)

SCRIPT_HOST_NAMES = frozenset(
    {
        "bash",
        "bash.exe",
        "cmd",
        "cmd.exe",
        "cscript",
        "cscript.exe",
        "dotnet",
        "dotnet.exe",
        "java",
        "java.exe",
        "javaw",
        "javaw.exe",
        "mshta",
        "mshta.exe",
        "msiexec",
        "msiexec.exe",
        "node",
        "node.exe",
        "perl",
        "perl.exe",
        "php",
        "php.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "python",
        "python.exe",
        "pythonw",
        "pythonw.exe",
        "regsvr32",
        "regsvr32.exe",
        "ruby",
        "ruby.exe",
        "rundll32",
        "rundll32.exe",
        "sh",
        "sh.exe",
        "wscript",
        "wscript.exe",
        "wsl",
        "wsl.exe",
        "zsh",
        "zsh.exe",
    }
)
SCRIPT_EXTENSIONS = frozenset({".bat", ".cmd", ".hta", ".js", ".ps1", ".py", ".sh", ".vbs", ".wsf"})
NATIVE_APP_NAMES = frozenset(
    {
        "calc.exe",
        "chrome.exe",
        "code.exe",
        "control.exe",
        "devmgmt.msc",
        "excel.exe",
        "explorer.exe",
        "firefox.exe",
        "mspaint.exe",
        "msedge.exe",
        "notepad.exe",
        "powerpnt.exe",
        "services.msc",
        "taskmgr.exe",
        "telegram.exe",
        "winword.exe",
    }
)
MMC_CONSOLES = frozenset({"devmgmt.msc", "services.msc"})
CALCULATOR_APP_URI = r"shell:AppsFolder\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
BRIDGE_POLICY_REVISION = "native-app-v2"
APP_PATHS_ENV = "JARVIS_BRIDGE_APP_PATHS_JSON"
SENSITIVE_ARGUMENT_RE = re.compile(
    r"(?i)(?:^|[-_.])(api[-_]?key|authorization|bearer|credential(?:s)?|"
    r"pass(?:word|wd)?|pwd|secret|token)(?:$|[-_.])"
)
URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s@]+)@")
WINDOWS_BROAD_PRINCIPAL_SIDS = ("S-1-1-0", "S-1-5-11", "S-1-5-32-545")


class ActionValidationError(ValueError):
    """A request failed the bridge's closed action contract."""


class BridgeHandler(BaseHTTPRequestHandler):
    server: BridgeServer

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send({"ok": False, "summary": "Not found."}, status=404)
            return
        self._send(
            {
                "ok": True,
                "name": "windows_rpc_bridge",
                "pid": os.getpid(),
                "uptime_sec": round(time.monotonic() - self.server.started_at, 3),
                "host": self.server.server_address[0],
                "port": self.server.server_address[1],
                "token_required": True,
                "contract": "action.v1",
                "actions": sorted(ACTION_NAMES),
            }
        )

    def do_POST(self) -> None:
        if self.path == "/execute":
            self._send(
                {
                    "ok": False,
                    "summary": "The raw command endpoint has been permanently removed.",
                },
                status=410,
            )
            return
        if self.path != "/action":
            self._send({"ok": False, "summary": "Not found."}, status=404)
            return
        if not self._authorized():
            self._send({"ok": False, "summary": "Unauthorized."}, status=401)
            return

        payload = self._read_json()
        if payload is None:
            return
        result, status = execute_action(payload)
        self._send(result, status=status)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        provided = header[len(prefix) :].strip()
        return bool(provided) and secrets.compare_digest(provided, self.server.token)

    def _read_json(self) -> dict[str, Any] | None:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send(
                {"ok": False, "summary": "Content-Type must be application/json."},
                status=415,
            )
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send({"ok": False, "summary": "Invalid Content-Length."}, status=400)
            return None
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send({"ok": False, "summary": "Invalid request body size."}, status=413)
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send({"ok": False, "summary": f"Invalid JSON: {exc}"}, status=400)
            return None
        if not isinstance(payload, dict):
            self._send({"ok": False, "summary": "JSON object is required."}, status=400)
            return None
        return payload

    def _send(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], token: str) -> None:
        if not token:
            raise ValueError("Bridge token must not be empty.")
        super().__init__(address, BridgeHandler)
        self.token = token
        self.started_at = time.monotonic()


def execute_action(request: dict[str, Any]) -> tuple[dict[str, Any], int]:
    started = time.monotonic()
    try:
        action, payload, timeout_sec = validate_action_request(request)
    except ActionValidationError as exc:
        return {"ok": False, "summary": str(exc), "contract": "action.v1"}, 400

    try:
        if action == "capabilities":
            result = _capabilities_result()
        elif action == "console.show_processes":
            result = _show_process_console(action, payload)
        elif action == "process.start":
            result = _start_process(action, payload)
        elif action == "url.open":
            result = _open_url(action, payload)
        elif action == "chrome.launch":
            result = _launch_chrome(action, payload)
        elif action == "app.open_and_type":
            process_result = _start_process(action, payload)
            native_payload = {
                key: value
                for key, value in payload.items()
                if key not in {"executable", "arguments", "cwd"}
            }
            native_payload["process_id"] = process_result["pid"]
            result = _run_fixed_native_action(action, native_payload, timeout_sec)
            result.setdefault("pid", process_result["pid"])
            result.setdefault("argv", process_result["argv"])
        else:
            result = _run_fixed_native_action(action, payload, timeout_sec)
    except subprocess.TimeoutExpired as exc:
        stdout, stdout_truncated = trim_output(exc.stdout)
        stderr, stderr_truncated = trim_output(exc.stderr)
        return {
            "ok": False,
            "action": action,
            "summary": f"Action timed out after {timeout_sec}s.",
            "state": "timed_out",
            "exit_code": None,
            "stdout": stdout,
            "stderr": stderr,
            "output_truncated": stdout_truncated or stderr_truncated,
            "timed_out": True,
            "timeout_sec": timeout_sec,
            "elapsed_sec": round(time.monotonic() - started, 3),
        }, 408
    except (ActionValidationError, OSError) as exc:
        return {
            "ok": False,
            "action": action,
            "summary": str(exc),
            "state": "failed",
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc)[:MAX_OUTPUT_CHARS],
            "output_truncated": len(str(exc)) > MAX_OUTPUT_CHARS,
            "timed_out": False,
            "elapsed_sec": round(time.monotonic() - started, 3),
        }, 500

    result["elapsed_sec"] = round(time.monotonic() - started, 3)
    return result, 200


def validate_action_request(request: dict[str, Any]) -> tuple[str, dict[str, Any], int]:
    _reject_extra_keys(request, {"action", "payload", "timeout_sec"}, "request")
    action = request.get("action")
    if not isinstance(action, str) or action not in ACTION_NAMES:
        allowed = ", ".join(sorted(ACTION_NAMES))
        raise ActionValidationError(f"Unsupported action. Allowed: {allowed}.")
    payload = request.get("payload", {})
    if not isinstance(payload, dict):
        raise ActionValidationError("payload must be a JSON object.")
    timeout_sec = _strict_int(request.get("timeout_sec", 30), "timeout_sec", 1, 120)

    validators = {
        "app.open_and_type": _validate_app_open_and_type,
        "capabilities": _validate_empty_payload,
        "chrome.launch": _validate_chrome_launch,
        "console.show_processes": _validate_process_view,
        "keyboard.send": _validate_keyboard_send,
        "process.start": _validate_process_start,
        "process.top": _validate_process_view,
        "screen.capture": _validate_screen_capture,
        "url.open": _validate_url_open,
        "window.focus": _validate_window_target,
        "window.list": _validate_window_list,
        "wmi.query": _validate_wmi_query,
    }
    return action, validators[action](payload), timeout_sec


def _validate_empty_payload(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(payload, set(), "payload")
    return {}


def _validate_process_start(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(payload, {"arguments", "cwd", "executable"}, "process.start payload")
    executable = _required_string(payload.get("executable"), "executable", 500)
    _validate_executable_name(executable)
    arguments = _argument_list(payload.get("arguments", []))
    cwd = _optional_directory(payload.get("cwd"), "cwd")
    _validate_native_app_arguments(executable, arguments, cwd)
    return {
        "executable": executable,
        "arguments": arguments,
        "cwd": cwd,
    }


def _validate_process_view(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(payload, {"limit", "sort"}, "process view payload")
    limit = _strict_int(payload.get("limit", 10), "limit", 1, PROCESS_TOP_MAX_LIMIT)
    sort = _optional_string(payload.get("sort", "cpu"), "sort", 20).casefold()
    if sort not in PROCESS_TOP_SORTS:
        allowed = ", ".join(sorted(PROCESS_TOP_SORTS))
        raise ActionValidationError(f"sort must be one of: {allowed}.")
    return {"limit": limit, "sort": sort}


def _validate_app_open_and_type(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(
        payload,
        {
            "arguments",
            "cwd",
            "executable",
            "keys",
            "process_name",
            "text",
            "wait_ms",
            "window_title",
        },
        "app.open_and_type payload",
    )
    clean = _validate_process_start(
        {key: payload[key] for key in ("executable", "arguments", "cwd") if key in payload}
    )
    clean.update(
        {
            "keys": _optional_string(payload.get("keys"), "keys", 1_000),
            "text": _optional_string(payload.get("text"), "text", 4_000),
            "process_name": _optional_string(payload.get("process_name"), "process_name", 120),
            "window_title": _optional_string(payload.get("window_title"), "window_title", 200),
            "wait_ms": _strict_int(payload.get("wait_ms", 700), "wait_ms", 0, 5_000),
        }
    )
    if not clean["keys"] and not clean["text"]:
        raise ActionValidationError("app.open_and_type requires keys or text.")
    return clean


def _validate_url_open(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(payload, {"url"}, "url.open payload")
    return {"url": _validated_http_url(payload.get("url"), allow_about_blank=False)}


def _validate_chrome_launch(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(
        payload,
        {"debug_port", "headless", "profile_dir", "start_url"},
        "chrome.launch payload",
    )
    profile_dir = _required_string(payload.get("profile_dir"), "profile_dir", 500)
    if not _is_absolute_path(profile_dir):
        raise ActionValidationError("profile_dir must be an absolute path.")
    headless = payload.get("headless", False)
    if not isinstance(headless, bool):
        raise ActionValidationError("headless must be a boolean.")
    return {
        "debug_port": _strict_int(payload.get("debug_port", 9222), "debug_port", 1_024, 65_535),
        "profile_dir": profile_dir,
        "start_url": _validated_http_url(
            payload.get("start_url", "about:blank"),
            allow_about_blank=True,
        ),
        "headless": headless,
    }


def _validate_window_target(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(
        payload,
        {"process_id", "process_name", "window_title"},
        "window target payload",
    )
    clean = {
        "process_id": _strict_int(payload.get("process_id", 0), "process_id", 0, 4_294_967_295),
        "process_name": _optional_string(payload.get("process_name"), "process_name", 120),
        "window_title": _optional_string(payload.get("window_title"), "window_title", 200),
    }
    if not any(clean.values()):
        raise ActionValidationError("A process_id, process_name, or window_title is required.")
    return clean


def _validate_keyboard_send(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(
        payload,
        {"keys", "process_id", "process_name", "text", "window_title"},
        "keyboard.send payload",
    )
    target_keys = ("process_id", "process_name", "window_title")
    if any(payload.get(key) for key in target_keys):
        target = _validate_window_target(
            {key: payload[key] for key in target_keys if key in payload}
        )
    else:
        target = {"process_id": 0, "process_name": "", "window_title": ""}
    target["keys"] = _optional_string(payload.get("keys"), "keys", 1_000)
    target["text"] = _optional_string(payload.get("text"), "text", 4_000)
    if not target["keys"] and not target["text"]:
        raise ActionValidationError("keyboard.send requires keys or text.")
    return target


def _validate_window_list(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(payload, {"limit"}, "window.list payload")
    return {"limit": _strict_int(payload.get("limit", 50), "limit", 1, 200)}


def _validate_screen_capture(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(payload, {"limit", "ocr", "path"}, "screen.capture payload")
    path = _required_string(payload.get("path"), "path", 500)
    if not _is_absolute_path(path) or Path(path).suffix.lower() != ".png":
        raise ActionValidationError("screen.capture path must be an absolute .png path.")
    ocr = payload.get("ocr", False)
    if not isinstance(ocr, bool):
        raise ActionValidationError("ocr must be a boolean.")
    return {
        "path": path,
        "limit": _strict_int(payload.get("limit", 30), "limit", 1, 100),
        "ocr": ocr,
    }


def _validate_wmi_query(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(
        payload,
        {"class_name", "filter", "limit", "namespace", "properties"},
        "wmi.query payload",
    )
    namespace = _optional_string(payload.get("namespace", "root\\cimv2"), "namespace", 120)
    class_name = _required_string(payload.get("class_name"), "class_name", 120)
    if not re.fullmatch(r"[A-Za-z0-9_\\]+", namespace):
        raise ActionValidationError("WMI namespace contains unsupported characters.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", class_name):
        raise ActionValidationError("WMI class_name contains unsupported characters.")
    properties = payload.get("properties", [])
    if not isinstance(properties, list) or len(properties) > 40:
        raise ActionValidationError("properties must be a list with at most 40 items.")
    clean_properties: list[str] = []
    for value in properties:
        prop = _required_string(value, "property", 80)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", prop):
            raise ActionValidationError("WMI property contains unsupported characters.")
        clean_properties.append(prop)
    return {
        "namespace": namespace,
        "class_name": class_name,
        "properties": clean_properties,
        "filter": _optional_string(payload.get("filter"), "filter", 500),
        "limit": _strict_int(payload.get("limit", 20), "limit", 1, 200),
    }


def _start_process(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    requested_name = PureWindowsPath(payload["executable"]).name.lower()
    if requested_name in MMC_CONSOLES:
        executable = _windows_system_binary("mmc.exe")
        console = _windows_root() / "System32" / requested_name
        if not console.is_file():
            raise ActionValidationError(f"Windows console was not found: {requested_name}")
        argv = [executable, str(console.resolve())]
    else:
        executable = _resolve_executable(payload["executable"])
        argv = [executable, *payload["arguments"]]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(  # noqa: S603 - argv is validated and shell is never used
        argv,
        cwd=payload.get("cwd") or None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
        creationflags=creationflags,
    )
    return {
        "ok": True,
        "action": action,
        "summary": f"Started {Path(executable).name}.",
        "state": "started",
        "pid": process.pid,
        "argv": redact_process_argv(argv),
        "cwd": payload.get("cwd") or None,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "output_truncated": False,
        "timed_out": False,
    }


def _open_url(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    if os.name != "nt" or not hasattr(os, "startfile"):
        raise OSError("url.open is available only on Windows.")
    os.startfile(payload["url"])  # type: ignore[attr-defined]  # noqa: S606
    return {
        "ok": True,
        "action": action,
        "summary": "URL open requested through the Windows shell.",
        "state": "requested",
        "url": payload["url"],
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "output_truncated": False,
        "timed_out": False,
    }


def _launch_chrome(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    chrome = _find_chrome()
    profile_dir = Path(payload["profile_dir"])
    profile_dir.mkdir(parents=True, exist_ok=True)
    arguments = [
        f"--remote-debugging-port={payload['debug_port']}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if payload["headless"]:
        arguments.append("--headless=new")
    arguments.append(payload["start_url"])
    result = _start_process(
        action,
        {"executable": chrome, "arguments": arguments, "cwd": None},
    )
    result.update(
        {
            "debug_url": f"http://127.0.0.1:{payload['debug_port']}",
            "profile_dir": str(profile_dir),
            "start_url": payload["start_url"],
        }
    )
    return result


def _capabilities_result() -> dict[str, Any]:
    _configured_app_paths()
    available_apps: dict[str, str] = {}
    for name in sorted(NATIVE_APP_NAMES):
        try:
            if name in MMC_CONSOLES:
                console = _windows_root() / "System32" / name
                if not console.is_file():
                    continue
            available_apps[name] = _resolve_executable(name)
        except (ActionValidationError, OSError):
            continue
    return {
        "ok": True,
        "action": "capabilities",
        "summary": "Structured Windows host actions are available.",
        "state": "completed",
        "contract": "action.v1",
        "actions": sorted(ACTION_NAMES),
        "raw_command_execution": False,
        "process_policy": {
            "revision": BRIDGE_POLICY_REVISION,
            "allowed_apps": sorted(NATIVE_APP_NAMES),
            "available_apps": available_apps,
            "argument_grammars": {
                "default": "no_arguments",
                "explorer.exe": "no_arguments_or_calculator_app_uri",
                "notepad.exe": "no_arguments_or_existing-parent-home-txt",
                "devmgmt.msc": "fixed_mmc_console",
                "services.msc": "fixed_mmc_console",
            },
            "process_views": {
                "actions": ["console.show_processes", "process.top"],
                "limit": {"minimum": 1, "maximum": PROCESS_TOP_MAX_LIMIT},
                "sorts": sorted(PROCESS_TOP_SORTS),
            },
        },
        "policy_revision": BRIDGE_POLICY_REVISION,
        "app_paths_sha256": _app_paths_configuration_sha256(),
        "shell": False,
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "output_truncated": False,
        "timed_out": False,
    }


FIXED_NATIVE_POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Out($Ok, $Summary, $Data) {
  [pscustomobject]@{
    ok = [bool]$Ok
    summary = [string]$Summary
    action = $Action
    data = $Data
  } | ConvertTo-Json -Depth 8 -Compress
}

try {
  $Envelope = $env:JARVIS_BRIDGE_ACTION_JSON | ConvertFrom-Json
  $Action = [string]$Envelope.action
  $Payload = $Envelope.payload
  $Allowed = @(
    'app.open_and_type', 'keyboard.send', 'screen.capture',
    'process.top', 'window.focus', 'window.list', 'wmi.query'
  )
  if ($Allowed -notcontains $Action) { throw 'Unsupported fixed native action.' }

  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class JarvisBridgeWinApi {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int command);
}
'@

  function FindWindowProcess($ProcessId, $ProcessName, $WindowTitle) {
    if ([int64]$ProcessId -gt 0) {
      $candidate = Get-Process -Id ([int]$ProcessId) -ErrorAction SilentlyContinue
      if ($candidate -and $candidate.MainWindowHandle -ne 0) { return $candidate }
    }
    if ($ProcessName) {
      $candidate = Get-Process -Name ([string]$ProcessName) -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
      if ($candidate) { return $candidate }
    }
    if ($WindowTitle) {
      return Get-Process -ErrorAction SilentlyContinue |
        Where-Object {
          $_.MainWindowHandle -ne 0 -and
          $_.MainWindowTitle -like ('*' + [string]$WindowTitle + '*')
        } | Select-Object -First 1
    }
    return $null
  }

  function FocusWindow($ProcessId, $ProcessName, $WindowTitle) {
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
      $process = FindWindowProcess $ProcessId $ProcessName $WindowTitle
      if ($process) {
        [void][JarvisBridgeWinApi]::ShowWindowAsync($process.MainWindowHandle, 9)
        [void][JarvisBridgeWinApi]::SetForegroundWindow($process.MainWindowHandle)
        Start-Sleep -Milliseconds 180
        return $true
      }
      Start-Sleep -Milliseconds 250
    }
    return $false
  }

  function SendInput($Keys, $Text) {
    if ($Text) {
      Set-Clipboard -Value ([string]$Text)
      Start-Sleep -Milliseconds 80
      [System.Windows.Forms.SendKeys]::SendWait('^v')
      Start-Sleep -Milliseconds 80
    }
    if ($Keys) { [System.Windows.Forms.SendKeys]::SendWait([string]$Keys) }
  }

  switch ($Action) {
    'process.top' {
      $all = Get-Process -ErrorAction SilentlyContinue
      $sorted = switch ([string]$Payload.sort) {
        'memory' { $all | Sort-Object WorkingSet64, Id -Descending }
        'name' { $all | Sort-Object ProcessName, Id }
        'pid' { $all | Sort-Object Id -Descending }
        default { $all | Sort-Object CPU, Id -Descending }
      }
      $items = @(
        $sorted |
          Select-Object -First ([int]$Payload.limit) |
          ForEach-Object {
            [pscustomobject]@{
              ProcessId = [int]$_.Id
              Name = [string]$_.ProcessName
              CpuSeconds = $(
                if ($null -eq $_.CPU) { 0.0 }
                else { [math]::Round([double]$_.CPU, 2) }
              )
              WorkingSetBytes = [int64]$_.WorkingSet64
            }
          }
      )
      Out $true "Listed $($items.Count) process(es), sorted by $([string]$Payload.sort)." @{
        items = $items
        limit = [int]$Payload.limit
        sort = [string]$Payload.sort
      }
    }
    'window.list' {
      $items = Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } |
        Select-Object -First ([int]$Payload.limit) Id, ProcessName, MainWindowTitle
      Out $true "Listed $(@($items).Count) visible window(s)." @{ windows = @($items) }
    }
    'window.focus' {
      $focused = FocusWindow $Payload.process_id $Payload.process_name $Payload.window_title
      Out $focused $(if ($focused) { 'Window focused.' } else { 'Window was not found.' }) @{
        focused = $focused
      }
    }
    'keyboard.send' {
      $hasTarget = $Payload.process_id -or $Payload.process_name -or $Payload.window_title
      $focused = $false
      if ($hasTarget) {
        $focused = FocusWindow $Payload.process_id $Payload.process_name $Payload.window_title
        if (-not $focused) {
          Out $false 'Target window was not focused; input was not sent.' @{ focused = $false }
          exit 1
        }
      }
      SendInput $Payload.keys $Payload.text
      Out $true 'Native keyboard input sent.' @{ focused = $focused }
    }
    'app.open_and_type' {
      Start-Sleep -Milliseconds ([int]$Payload.wait_ms)
      $focused = FocusWindow $Payload.process_id $Payload.process_name $Payload.window_title
      if (-not $focused) {
        Out $false 'Target window was not focused; input was not sent.' @{
          focused = $false; pid = $Payload.process_id
        }
        exit 1
      }
      SendInput $Payload.keys $Payload.text
      Out $true 'Application focused and native input sent.' @{
        focused = $true; pid = $Payload.process_id
      }
    }
    'screen.capture' {
      $directory = Split-Path -Parent ([string]$Payload.path)
      if ($directory) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
      $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
      $bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
      $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
      try {
        $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
        $bitmap.Save([string]$Payload.path, [System.Drawing.Imaging.ImageFormat]::Png)
      } finally {
        $graphics.Dispose()
        $bitmap.Dispose()
      }
      $ocrText = ''
      $ocrAvailable = $false
      if ([bool]$Payload.ocr) {
        $tesseract = Get-Command tesseract -ErrorAction SilentlyContinue
        if ($tesseract) {
          $ocrText = (
            & $tesseract.Source ([string]$Payload.path) stdout --psm 6 2>$null |
              Out-String
          ).Trim()
          $ocrAvailable = $true
        }
      }
      Out $true 'Screen captured.' @{
        path = [string]$Payload.path
        width = $bounds.Width
        height = $bounds.Height
        ocrRequested = [bool]$Payload.ocr
        ocrAvailable = $ocrAvailable
        ocrText = $ocrText
      }
    }
    'wmi.query' {
      $query = @{
        Namespace = [string]$Payload.namespace
        ClassName = [string]$Payload.class_name
      }
      if ($Payload.filter) { $query.Filter = [string]$Payload.filter }
      $items = Get-CimInstance @query | Select-Object -First ([int]$Payload.limit)
      if (@($Payload.properties).Count -gt 0) {
        $items = $items | Select-Object -Property @($Payload.properties)
      }
      Out $true "WMI/CIM query returned $(@($items).Count) item(s)." @{
        items = @($items)
        className = [string]$Payload.class_name
        namespace = [string]$Payload.namespace
      }
    }
  }
} catch {
  Out $false $_.Exception.Message @{ error = $_.Exception.Message }
  exit 1
}
""".strip()


FIXED_PROCESS_CONSOLE_POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
$view = $env:JARVIS_PROCESS_VIEW_JSON | ConvertFrom-Json
$limit = [int]$view.limit
$sort = [string]$view.sort
$all = Get-Process -ErrorAction SilentlyContinue
$sorted = switch ($sort) {
  'memory' { $all | Sort-Object WorkingSet64, Id -Descending }
  'name' { $all | Sort-Object ProcessName, Id }
  'pid' { $all | Sort-Object Id -Descending }
  default { $all | Sort-Object CPU, Id -Descending }
}
$rows = @(
  $sorted |
    Select-Object -First $limit |
    ForEach-Object {
      [pscustomobject]@{
        PID = [int]$_.Id
        Name = [string]$_.ProcessName
        CPU_s = $(if ($null -eq $_.CPU) { 0.0 } else { [math]::Round([double]$_.CPU, 2) })
        Memory_MB = [math]::Round([double]$_.WorkingSet64 / 1MB, 1)
      }
    }
)
$Host.UI.RawUI.WindowTitle = 'Jarvis - Top Processes'
Write-Host "Jarvis: top $limit processes sorted by $sort" -ForegroundColor Cyan
$rows | Format-Table -AutoSize
Write-Host ''
[void](Read-Host 'Press Enter to close')
""".strip()


def _show_process_console(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    powershell = _canonical_windows_powershell()
    encoded = base64.b64encode(FIXED_PROCESS_CONSOLE_POWERSHELL.encode("utf-16-le")).decode(
        "ascii"
    )
    argv = [
        powershell,
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "RemoteSigned",
        "-EncodedCommand",
        encoded,
    ]
    env = os.environ.copy()
    env["JARVIS_PROCESS_VIEW_JSON"] = json.dumps(payload, separators=(",", ":"))
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
        subprocess, "CREATE_NEW_CONSOLE", 0
    )
    process = subprocess.Popen(  # noqa: S603 - fixed script and validated enum/int payload only
        argv,
        shell=False,
        close_fds=True,
        env=env,
        creationflags=creationflags,
    )
    return {
        "ok": True,
        "action": action,
        "summary": f"Opened a fixed top-{payload['limit']} process console.",
        "state": "started",
        "pid": process.pid,
        "limit": payload["limit"],
        "sort": payload["sort"],
        "argv": [*argv[:-1], "[FIXED_PROCESS_VIEW_SCRIPT]"],
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "output_truncated": False,
        "timed_out": False,
    }


def _run_fixed_native_action(
    action: str,
    payload: dict[str, Any],
    timeout_sec: int,
) -> dict[str, Any]:
    powershell = powershell_path()
    if powershell is None:
        raise OSError("powershell.exe or pwsh is not available.")
    env = os.environ.copy()
    env["JARVIS_BRIDGE_ACTION_JSON"] = json.dumps(
        {"action": action, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = subprocess.run(  # noqa: S603 - the fixed script contains no request text
        powershell_command(powershell),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=timeout_sec,
        check=False,
        env=env,
        creationflags=creationflags,
    )
    native = _parse_native_result(completed.stdout)
    stdout, stdout_truncated = trim_output(completed.stdout)
    stderr, stderr_truncated = trim_output(completed.stderr)
    native_ok = bool(native.get("ok")) if native else completed.returncode == 0
    summary = str(native.get("summary") or "Native action completed.") if native else (
        "Native action completed." if completed.returncode == 0 else "Native action failed."
    )
    return {
        "ok": completed.returncode == 0 and native_ok,
        "action": action,
        "summary": summary,
        "state": "completed" if completed.returncode == 0 and native_ok else "failed",
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "output_truncated": stdout_truncated or stderr_truncated,
        "timed_out": False,
        "result": native,
    }


def powershell_path() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("pwsh")


def powershell_command(powershell: str) -> list[str]:
    encoded = base64.b64encode(FIXED_NATIVE_POWERSHELL.encode("utf-16-le")).decode("ascii")
    executable = PureWindowsPath(powershell).name.lower() or Path(powershell).name.lower()
    args = [powershell, "-NoLogo", "-NoProfile"]
    if executable == "powershell.exe":
        args.extend(["-STA", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned"])
    else:
        args.append("-NonInteractive")
    args.extend(["-EncodedCommand", encoded])
    return args


def _parse_native_result(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _resolve_executable(value: str) -> str:
    _validate_executable_name(value)
    requested_name = PureWindowsPath(value).name.lower()
    if requested_name in MMC_CONSOLES:
        return _windows_system_binary("mmc.exe")
    if _is_absolute_path(value):
        candidate = Path(value).expanduser()
        if not candidate.is_file():
            raise ActionValidationError(f"Executable does not exist: {value}")
        resolved_candidate = candidate.resolve()
        _require_canonical_app_path(resolved_candidate, requested_name)
        return str(resolved_candidate)
    if any(separator in value for separator in ("/", "\\")):
        raise ActionValidationError("Relative executable paths are not allowed.")
    resolved = shutil.which(value)
    if resolved is not None:
        try:
            _require_canonical_app_path(Path(resolved).resolve(), requested_name)
        except ActionValidationError:
            resolved = None
    if resolved is None:
        resolved = next(
            (
                str(path.resolve())
                for path in _canonical_app_candidates(requested_name)
                if path.is_file()
            ),
            None,
        )
    if resolved is None:
        raise ActionValidationError(f"Executable was not found: {value}")
    _validate_executable_name(resolved)
    resolved_path = Path(resolved).resolve()
    _require_canonical_app_path(resolved_path, requested_name)
    return str(resolved_path)


def _validate_executable_name(value: str) -> None:
    windows_path = PureWindowsPath(value)
    name = windows_path.name.lower()
    suffix = windows_path.suffix.lower()
    if name in SCRIPT_HOST_NAMES or suffix in SCRIPT_EXTENSIONS:
        raise ActionValidationError("Shells, script hosts, and script files are not allowed.")
    if name not in NATIVE_APP_NAMES:
        raise ActionValidationError(
            "process.start accepts only the fixed native desktop application allowlist."
        )


def _validate_native_app_arguments(
    executable: str,
    arguments: list[str],
    cwd: str | None,
) -> None:
    if cwd is not None:
        raise ActionValidationError("Native desktop application actions do not accept cwd.")
    name = PureWindowsPath(executable).name.lower()
    if not arguments:
        return
    if name == "explorer.exe" and arguments == [CALCULATOR_APP_URI]:
        return
    if name == "notepad.exe" and len(arguments) == 1 and _valid_notepad_target(arguments[0]):
        return
    raise ActionValidationError(
        "Arguments are outside the fixed native desktop application grammar."
    )


def _valid_notepad_target(raw: str) -> bool:
    windows_path = PureWindowsPath(raw)
    normalized = raw.replace("/", "\\")
    if (
        not windows_path.is_absolute()
        or not re.fullmatch(r"[A-Za-z]:", windows_path.drive)
        or any(character in raw for character in '<>"|?*')
        or ":" in normalized[2:]
    ):
        return False
    reserved = re.compile(
        r"(?i)^(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$"
    )
    for component in windows_path.parts[1:]:
        if not component or component.endswith((" ", ".")):
            return False
        stem = component.split(".", 1)[0].rstrip(" .")
        if reserved.fullmatch(stem):
            return False
    target = Path(raw).expanduser()
    home = Path(os.environ.get("JARVIS_HOME", r"D:\jarvis")).resolve(strict=False)
    resolved = target.resolve(strict=False)
    return bool(
        resolved.is_relative_to(home)
        and resolved.suffix.lower() == ".txt"
        and not target.is_symlink()
        and target.parent.is_dir()
        and not target.parent.is_symlink()
        and (not target.exists() or target.is_file())
    )


def _require_canonical_app_path(path: Path, name: str) -> None:
    allowed = {
        candidate.resolve(strict=False)
        for candidate in _canonical_app_candidates(name)
    }
    if path.resolve(strict=False) not in allowed:
        raise ActionValidationError(
            f"Native desktop executable is not a canonical installation candidate: {path}"
        )


def _canonical_app_candidates(name: str) -> tuple[Path, ...]:
    windows = _windows_root()
    system32 = windows / "System32"
    syswow64 = windows / "SysWOW64"
    program_files = _absolute_environment_path("PROGRAMFILES")
    program_files_x86 = _absolute_environment_path("PROGRAMFILES(X86)")
    local = _absolute_environment_path("LOCALAPPDATA")
    roaming = _absolute_environment_path("APPDATA")

    def below(root: Path | None, relative: str) -> Path | None:
        return root / relative if root is not None else None

    def paths(*values: Path | None) -> tuple[Path, ...]:
        return tuple(value for value in values if value is not None)

    candidates: dict[str, tuple[Path, ...]] = {
        "calc.exe": paths(
            system32 / "calc.exe",
            syswow64 / "calc.exe",
            below(local, "Microsoft/WindowsApps/calc.exe"),
        ),
        "control.exe": paths(system32 / "control.exe", syswow64 / "control.exe"),
        "explorer.exe": paths(windows / "explorer.exe"),
        "mspaint.exe": paths(
            system32 / "mspaint.exe",
            below(local, "Microsoft/WindowsApps/mspaint.exe"),
        ),
        "notepad.exe": paths(
            system32 / "notepad.exe",
            windows / "notepad.exe",
            below(local, "Microsoft/WindowsApps/notepad.exe"),
        ),
        "taskmgr.exe": paths(system32 / "Taskmgr.exe", syswow64 / "Taskmgr.exe"),
        "chrome.exe": paths(
            below(program_files, "Google/Chrome/Application/chrome.exe"),
            below(program_files_x86, "Google/Chrome/Application/chrome.exe"),
            below(local, "Google/Chrome/Application/chrome.exe"),
        ),
        "msedge.exe": paths(
            below(program_files, "Microsoft/Edge/Application/msedge.exe"),
            below(program_files_x86, "Microsoft/Edge/Application/msedge.exe"),
        ),
        "firefox.exe": paths(
            below(program_files, "Mozilla Firefox/firefox.exe"),
            below(program_files_x86, "Mozilla Firefox/firefox.exe"),
        ),
        "code.exe": paths(
            below(local, "Programs/Microsoft VS Code/Code.exe"),
            below(program_files, "Microsoft VS Code/Code.exe"),
        ),
        "telegram.exe": paths(below(roaming, "Telegram Desktop/Telegram.exe")),
        "winword.exe": paths(
            below(program_files, "Microsoft Office/root/Office16/WINWORD.EXE"),
            below(program_files_x86, "Microsoft Office/root/Office16/WINWORD.EXE"),
        ),
        "excel.exe": paths(
            below(program_files, "Microsoft Office/root/Office16/EXCEL.EXE"),
            below(program_files_x86, "Microsoft Office/root/Office16/EXCEL.EXE"),
        ),
        "powerpnt.exe": paths(
            below(program_files, "Microsoft Office/root/Office16/POWERPNT.EXE"),
            below(program_files_x86, "Microsoft Office/root/Office16/POWERPNT.EXE"),
        ),
    }
    registered = _configured_app_candidate(name)
    values = list(candidates.get(name, ()))
    if registered is not None and registered not in values:
        values.append(registered)
    return tuple(values)


def _configured_app_candidate(name: str) -> Path | None:
    return _configured_app_paths().get(name.casefold())


def _configured_app_paths() -> dict[str, Path]:
    raw = os.environ.get(APP_PATHS_ENV, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActionValidationError(f"{APP_PATHS_ENV} must contain strict JSON: {exc}") from exc
    if not isinstance(payload, dict) or len(payload) > len(NATIVE_APP_NAMES):
        raise ActionValidationError(f"{APP_PATHS_ENV} must be an app-name to path object")
    unknown = {str(key).casefold() for key in payload} - NATIVE_APP_NAMES
    if unknown:
        raise ActionValidationError(f"{APP_PATHS_ENV} contains unsupported app names")
    result: dict[str, Path] = {}
    for raw_name, selected in payload.items():
        name = str(raw_name).casefold()
        if not isinstance(selected, str) or not selected or "\x00" in selected:
            raise ActionValidationError(f"{APP_PATHS_ENV} contains an invalid executable path")
        candidate = Path(selected)
        if not candidate.is_absolute() or candidate.name.casefold() != name:
            raise ActionValidationError(f"{APP_PATHS_ENV} executable path/name mismatch")
        if candidate.is_symlink() or not candidate.is_file():
            raise ActionValidationError(
                f"{APP_PATHS_ENV} executable must be an existing non-symlink file"
            )
        result[name] = candidate
    return result


def _app_paths_configuration_sha256() -> str:
    raw = os.environ.get(APP_PATHS_ENV, "")
    return hashlib.sha256(raw.encode("utf-8", errors="strict")).hexdigest()


def _absolute_environment_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else None


def _windows_root() -> Path:
    path = _absolute_environment_path("SYSTEMROOT") or _absolute_environment_path("WINDIR")
    if path is None:
        raise ActionValidationError("A canonical absolute Windows system root is required.")
    return path


def _canonical_windows_powershell() -> str:
    executable = _windows_root() / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not executable.is_file():
        raise ActionValidationError("Canonical Windows PowerShell was not found.")
    return str(executable.resolve())


def _find_chrome() -> str:
    try:
        return _resolve_executable("chrome.exe")
    except ActionValidationError as exc:
        raise OSError("Chrome executable was not found in canonical locations.") from exc


def _validated_http_url(value: Any, *, allow_about_blank: bool) -> str:
    url = _required_string(value, "url", 2_048)
    if allow_about_blank and url == "about:blank":
        return url
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ActionValidationError("URL must use http or https and include a host.")
    if parts.username is not None or parts.password is not None:
        raise ActionValidationError("URL credentials are not allowed.")
    return url


def _argument_list(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_ARGUMENTS:
        raise ActionValidationError(f"arguments must be a list with at most {MAX_ARGUMENTS} items.")
    result: list[str] = []
    total = 0
    for item in value:
        if not isinstance(item, str):
            raise ActionValidationError("Every process argument must be a string.")
        _reject_control_chars(item, "argument")
        if len(item) > MAX_ARGUMENT_CHARS:
            raise ActionValidationError("A process argument is too long.")
        total += len(item)
        if total > MAX_ARGUMENTS_TOTAL_CHARS:
            raise ActionValidationError("Combined process arguments are too long.")
        result.append(item)
    return result


def redact_process_argv(argv: list[str]) -> list[str]:
    """Return a display-safe argv while preserving the executed argv unchanged."""

    redacted: list[str] = []
    redact_next = False
    for raw in argv:
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue

        value = URL_USERINFO_RE.sub(r"\1[REDACTED]@", raw)
        prefix, separator, _secret = _split_sensitive_assignment(value)
        if separator:
            redacted.append(f"{prefix}{separator}[REDACTED]")
            continue
        redacted.append(value)
        if _is_sensitive_argument_flag(value):
            redact_next = True
    return redacted


def _split_sensitive_assignment(value: str) -> tuple[str, str, str]:
    for separator in ("=", ":"):
        prefix, found, secret = value.partition(separator)
        if found and _is_sensitive_argument_flag(prefix):
            return prefix, separator, secret
    return value, "", ""


def _is_sensitive_argument_flag(value: str) -> bool:
    normalized = value.strip().lstrip("-/").casefold()
    return bool(normalized and SENSITIVE_ARGUMENT_RE.search(normalized))


def _optional_directory(value: Any, field: str) -> str | None:
    if value in (None, ""):
        return None
    path = _required_string(value, field, 500)
    if not _is_absolute_path(path):
        raise ActionValidationError(f"{field} must be an absolute path.")
    candidate = Path(path).expanduser()
    if not candidate.is_dir():
        raise ActionValidationError(f"{field} is not a directory: {path}")
    return str(candidate.resolve())


def _is_absolute_path(value: str) -> bool:
    return Path(value).expanduser().is_absolute() or PureWindowsPath(value).is_absolute()


def _required_string(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionValidationError(f"{field} is required and must be a string.")
    text = value.strip()
    _reject_control_chars(text, field)
    if len(text) > max_length:
        raise ActionValidationError(f"{field} is too long.")
    return text


def _optional_string(value: Any, field: str, max_length: int) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ActionValidationError(f"{field} must be a string.")
    _reject_control_chars(value, field)
    if len(value) > max_length:
        raise ActionValidationError(f"{field} is too long.")
    return value.strip()


def _strict_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActionValidationError(f"{field} must be an integer.")
    if not minimum <= value <= maximum:
        raise ActionValidationError(f"{field} must be between {minimum} and {maximum}.")
    return value


def _reject_control_chars(value: str, field: str) -> None:
    if any(character in value for character in ("\0", "\r", "\n")):
        raise ActionValidationError(f"{field} contains unsupported control characters.")


def _reject_extra_keys(payload: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ActionValidationError(f"Unknown {context} field(s): {', '.join(unknown)}.")


def trim_output(value: str | bytes | None) -> tuple[str, bool]:
    if value is None:
        return "", False
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    if len(text) <= MAX_OUTPUT_CHARS:
        return text, False
    return f"{text[:MAX_OUTPUT_CHARS]}\n...[truncated]", True


def default_token_file() -> Path:
    raw_home = os.environ.get("JARVIS_HOME")
    home = Path(raw_home) if raw_home else Path(r"D:\jarvis")
    return home / ".jarvis" / "bridge.token"


def ensure_token(path: Path) -> str:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(3):
        if path.is_symlink():
            raise OSError(f"Bridge token path must not be a symbolic link: {path}")
        if path.exists():
            _protect_token_file(path)
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
            token = secrets.token_urlsafe(32)
            _publish_token_atomically(path, token, replace=True)
            _protect_token_file(path)
            return token

        token = secrets.token_urlsafe(32)
        try:
            _publish_token_atomically(path, token, replace=False)
        except FileExistsError:
            continue
        _protect_token_file(path)
        return token
    raise OSError(f"Could not create bridge token atomically: {path}")


def _publish_token_atomically(path: Path, token: str, *, replace: bool) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{token}\n")
            handle.flush()
            os.fsync(handle.fileno())
        _protect_token_file(temporary_path)
        if replace:
            os.replace(temporary_path, path)
        else:
            os.link(temporary_path, path)
    finally:
        with suppress(OSError):
            temporary_path.unlink()


def _protect_token_file(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o600)
        return

    icacls = _windows_system_binary("icacls.exe")
    sid = _current_user_sid()
    _run_security_command([icacls, str(path), "/grant:r", f"*{sid}:(F)", "/Q"])
    _run_security_command([icacls, str(path), "/inheritance:r", "/Q"])
    for broad_sid in WINDOWS_BROAD_PRINCIPAL_SIDS:
        _run_security_command(
            [icacls, str(path), "/remove:g", f"*{broad_sid}", "/Q"]
        )


def _current_user_sid() -> str:
    whoami = _windows_system_binary("whoami.exe")
    completed = _run_security_command([whoami, "/user", "/fo", "csv", "/nh"])
    match = re.search(r"S-\d+(?:-\d+)+", completed.stdout)
    if match is None:
        raise OSError("whoami.exe did not return the current Windows SID.")
    return match.group(0)


def _windows_system_binary(name: str) -> str:
    try:
        candidate = _windows_root() / "System32" / name
    except ActionValidationError as exc:
        raise OSError(str(exc)) from exc
    if candidate.is_file():
        return str(candidate.resolve())
    raise OSError(f"Required Windows system binary was not found: {name}")


def _run_security_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    completed = subprocess.run(  # noqa: S603 - fixed system binary and fixed switches
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=10,
        check=False,
        shell=False,
        creationflags=creationflags,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise OSError(
            f"Windows token ACL command failed with exit code {completed.returncode}: "
            f"{detail[:500]}"
        )
    return completed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local JARVIS structured Windows RPC bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file", type=Path, default=default_token_file())
    return parser


def _require_loopback(host: str) -> None:
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None)}
    except OSError as exc:
        raise ValueError(f"Bridge host cannot be resolved: {host}") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_loopback for address in addresses):
        raise ValueError("The host bridge may bind only to a loopback address.")


def main() -> None:
    args = build_parser().parse_args()
    _require_loopback(args.host)
    _configured_app_paths()
    token = ensure_token(args.token_file)
    server = BridgeServer((args.host, args.port), token)
    print(
        json.dumps(
            {
                "ok": True,
                "name": "windows_rpc_bridge",
                "contract": "action.v1",
                "host": args.host,
                "port": args.port,
                "token_file": str(args.token_file),
                "actions": sorted(ACTION_NAMES),
            },
            ensure_ascii=False,
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping windows_rpc_bridge")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
