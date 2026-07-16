from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import select
import shutil
import socket
import socketserver
import subprocess
import tempfile
import threading
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
        "browser.open_guarded",
        "capabilities",
        "chrome.attest_guarded",
        "chrome.launch_guarded",
        "console.show_processes",
        "keyboard.send",
        "process.start",
        "process.top",
        "screen.capture",
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
# Locale-invariant runtime process-name fragments for launcher-style apps whose
# visible window is NOT owned by the launched PID (UWP stubs like the modern
# Calculator, Office shells, MMC consoles). app.open_and_type focuses by this
# fragment when the caller supplied no explicit process_name/window_title, so the
# keystrokes reach the right window regardless of the Windows display language.
NATIVE_APP_FOCUS_HINTS = {
    "calc.exe": "Calculator",
    "winword.exe": "WINWORD",
    "excel.exe": "EXCEL",
    "powerpnt.exe": "POWERPNT",
    "notepad.exe": "Notepad",
    "mspaint.exe": "mspaint",
    "taskmgr.exe": "Taskmgr",
    "code.exe": "Code",
    "telegram.exe": "Telegram",
    "chrome.exe": "chrome",
    "msedge.exe": "msedge",
    "firefox.exe": "firefox",
    "devmgmt.msc": "mmc",
    "services.msc": "mmc",
}
BRIDGE_POLICY_REVISION = "native-app-v3"
BROWSER_NETWORK_GUARD = "public-proxy-v1"
BROWSER_GUARD_PROXY_HOST = "127.0.0.1"
BROWSER_GUARD_PROXY_PORT = 18_766
BROWSER_PROXY_MAX_HEADER_BYTES = 65_536
BROWSER_PROXY_CONNECT_TIMEOUT_SEC = 10.0
BROWSER_PROXY_IDLE_TIMEOUT_SEC = 60.0
APP_PATHS_ENV = "JARVIS_BRIDGE_APP_PATHS_JSON"
SENSITIVE_ARGUMENT_RE = re.compile(
    r"(?i)(?:^|[-_.])(api[-_]?key|authorization|bearer|credential(?:s)?|"
    r"pass(?:word|wd)?|pwd|secret|token)(?:$|[-_.])"
)
URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s@]+)@")
WINDOWS_BROAD_PRINCIPAL_SIDS = ("S-1-1-0", "S-1-5-11", "S-1-5-32-545")

_browser_guard_proxy_lock = threading.Lock()
_browser_guard_proxies: dict[
    tuple[str, ...], tuple[BrowserGuardProxy, threading.Thread]
] = {}
_guarded_chrome_attestations_lock = threading.Lock()
_guarded_chrome_persist_lock = threading.Lock()
_guarded_chrome_attestations: dict[int, dict[str, Any]] = {}
_bridge_attestation_hmac_key: bytes | None = None
_browser_open_profile_instance = f"bridge-{secrets.token_urlsafe(24)}"
_browser_guard_recovery_error = ""


class ActionValidationError(ValueError):
    """A request failed the bridge's closed action contract."""


class BrowserGuardProxy(socketserver.ThreadingTCPServer):
    """Loopback-only HTTP CONNECT proxy that pins every destination to a public IP."""

    allow_reuse_address = False
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        allowed_private_hosts: tuple[str, ...],
    ) -> None:
        self.allowed_private_hosts = frozenset(allowed_private_hosts)
        super().__init__(address, BrowserGuardProxyHandler)


class BrowserGuardProxyHandler(socketserver.BaseRequestHandler):
    """Validate each browser connection before opening its public-network socket."""

    server: BrowserGuardProxy

    def handle(self) -> None:
        self.request.settimeout(BROWSER_PROXY_IDLE_TIMEOUT_SEC)
        try:
            header, trailing = _read_proxy_header(self.request)
            method, target, version, headers = _parse_proxy_request(header)
            if method == "CONNECT":
                host, port = _parse_connect_target(target)
                upstream, _address = _connect_public_host(
                    host,
                    port,
                    allowed_private_hosts=self.server.allowed_private_hosts,
                )
                with upstream:
                    self.request.sendall(
                        b"HTTP/1.1 200 Connection Established\r\n"
                        b"Proxy-Agent: Jarvis-Public-Guard/1\r\n\r\n"
                    )
                    if trailing:
                        upstream.sendall(trailing)
                    _relay_proxy_streams(self.request, upstream)
                return

            parsed = urlsplit(target)
            if parsed.scheme != "http" or not parsed.hostname:
                raise ActionValidationError(
                    "The guarded proxy accepts CONNECT or absolute http URLs only."
                )
            if parsed.username is not None or parsed.password is not None:
                raise ActionValidationError("Proxy URL credentials are not allowed.")
            try:
                port = parsed.port or 80
            except ValueError as exc:
                raise ActionValidationError("Proxy URL port is invalid.") from exc
            host_header = _format_host_header(parsed.hostname, port, default_port=80)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            upstream, _address = _connect_public_host(
                parsed.hostname,
                port,
                allowed_private_hosts=self.server.allowed_private_hosts,
            )
            with upstream:
                forwarded = _forward_proxy_header(
                    method=method,
                    path=path,
                    version=version,
                    headers=headers,
                    host_header=host_header,
                )
                upstream.sendall(forwarded + trailing)
                _relay_proxy_streams(self.request, upstream)
        except (ActionValidationError, OSError, TimeoutError, ValueError):
            with suppress(OSError):
                self.request.sendall(
                    b"HTTP/1.1 403 Forbidden\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 35\r\n\r\n"
                    b"Jarvis blocked this network target."
                )


def _read_proxy_header(client: socket.socket) -> tuple[bytes, bytes]:
    data = bytearray()
    marker = b"\r\n\r\n"
    while marker not in data:
        chunk = client.recv(8_192)
        if not chunk:
            raise OSError("Proxy client closed before sending headers.")
        data.extend(chunk)
        if len(data) > BROWSER_PROXY_MAX_HEADER_BYTES:
            raise ActionValidationError("Proxy request headers are too large.")
    boundary = data.index(marker) + len(marker)
    return bytes(data[:boundary]), bytes(data[boundary:])


def _parse_proxy_request(
    header: bytes,
) -> tuple[str, str, str, list[tuple[str, str]]]:
    try:
        text = header.decode("iso-8859-1")
    except UnicodeDecodeError as exc:
        raise ActionValidationError("Proxy request headers are invalid.") from exc
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise ActionValidationError("Proxy request line is invalid.")
    method, target, version = parts
    if not re.fullmatch(r"[A-Z]+", method) or version not in {"HTTP/1.0", "HTTP/1.1"}:
        raise ActionValidationError("Proxy request method or HTTP version is invalid.")
    if any(char in target for char in "\r\n\x00") or len(target) > 8_192:
        raise ActionValidationError("Proxy request target is invalid.")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name):
            raise ActionValidationError("Proxy request header is invalid.")
        if any(char in value for char in "\r\n\x00"):
            raise ActionValidationError("Proxy request header value is invalid.")
        headers.append((name, value.strip()))
    return method, target, version, headers


def _parse_connect_target(target: str) -> tuple[str, int]:
    parsed = urlsplit(f"//{target}")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ActionValidationError("CONNECT target must be a bare host and port.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ActionValidationError("CONNECT target port is invalid.") from exc
    if port is None or not 1 <= port <= 65_535:
        raise ActionValidationError("CONNECT target requires a valid port.")
    return parsed.hostname, port


def _format_host_header(host: str, port: int, *, default_port: int) -> str:
    formatted = f"[{host}]" if ":" in host else host
    return formatted if port == default_port else f"{formatted}:{port}"


def _forward_proxy_header(
    *,
    method: str,
    path: str,
    version: str,
    headers: list[tuple[str, str]],
    host_header: str,
) -> bytes:
    excluded = {"connection", "host", "proxy-authorization", "proxy-connection"}
    lines = [f"{method} {path} {version}", f"Host: {host_header}"]
    lines.extend(f"{name}: {value}" for name, value in headers if name.casefold() not in excluded)
    lines.extend(("Connection: close", "", ""))
    return "\r\n".join(lines).encode("iso-8859-1")


def _public_proxy_addresses(
    host: str,
    port: int,
    *,
    allowed_private_hosts: frozenset[str] = frozenset(),
) -> list[str]:
    normalized = host.strip().rstrip(".").casefold()
    if not normalized or any(char in normalized for char in "\r\n\x00"):
        raise ActionValidationError("Browser proxy host is invalid.")
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        try:
            answers = socket.getaddrinfo(
                normalized,
                port,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise ActionValidationError("Browser proxy DNS resolution failed.") from exc
        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for answer in answers:
            try:
                address = ipaddress.ip_address(str(answer[4][0]).split("%", 1)[0])
            except (IndexError, ValueError):
                continue
            if address not in addresses:
                addresses.append(address)
    else:
        addresses = [literal]
    if not addresses:
        raise ActionValidationError("Browser proxy DNS returned no usable addresses.")
    forbidden = [
        address
        for address in addresses
        if address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ]
    if forbidden:
        raise ActionValidationError(
            "Browser proxy blocked a private, local, reserved, or metadata address."
        )
    public = [address for address in addresses if address.is_global]
    private = [address for address in addresses if not address.is_global]
    if public and private:
        raise ActionValidationError("Browser proxy blocked mixed public/private DNS answers.")
    if public and allowed_private_hosts:
        raise ActionValidationError(
            "A private-only browser session cannot connect to public network targets."
        )
    if private and normalized not in allowed_private_hosts:
        raise ActionValidationError(
            "Browser proxy blocked a private, local, reserved, or metadata address."
        )
    return [str(address) for address in addresses]


def _connect_public_host(
    host: str,
    port: int,
    *,
    allowed_private_hosts: frozenset[str] = frozenset(),
) -> tuple[socket.socket, str]:
    last_error: OSError | None = None
    for address in _public_proxy_addresses(
        host,
        port,
        allowed_private_hosts=allowed_private_hosts,
    ):
        try:
            # Connect to the already-validated numeric address. This deliberately
            # avoids a second hostname lookup and closes the DNS-rebinding window.
            return (
                socket.create_connection(
                    (address, port),
                    timeout=BROWSER_PROXY_CONNECT_TIMEOUT_SEC,
                ),
                address,
            )
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise OSError("Browser proxy could not connect to a public address.")


def _relay_proxy_streams(client: socket.socket, upstream: socket.socket) -> None:
    client.settimeout(None)
    upstream.settimeout(None)
    sockets = [client, upstream]
    while sockets:
        readable, _writable, exceptional = select.select(
            sockets,
            [],
            sockets,
            BROWSER_PROXY_IDLE_TIMEOUT_SEC,
        )
        if exceptional or not readable:
            return
        for source in readable:
            try:
                data = source.recv(65_536)
            except OSError:
                return
            if not data:
                return
            destination = upstream if source is client else client
            destination.sendall(data)


def _ensure_browser_guard_proxy(
    allowed_private_hosts: tuple[str, ...] = (),
    *,
    requested_port: int | None = None,
) -> tuple[str, int]:
    policy_key = tuple(sorted(set(allowed_private_hosts)))
    with _browser_guard_proxy_lock:
        running = _browser_guard_proxies.get(policy_key)
        if running is not None:
            proxy, thread = running
            if thread.is_alive():
                if requested_port is not None and proxy.server_address[1] != requested_port:
                    raise OSError(
                        "The restored browser guard proxy port does not match its attestation."
                    )
                return proxy.server_address
            proxy.server_close()
            _browser_guard_proxies.pop(policy_key, None)
        # Public-only Chrome sessions use a stable port so they safely reconnect
        # after a bridge restart. Private-host exceptions get isolated ephemeral
        # proxies and distinct Chrome profiles, preventing a public tab from
        # inheriting a localhost-capable network policy.
        port = (
            requested_port
            if requested_port is not None
            else BROWSER_GUARD_PROXY_PORT
            if not policy_key
            else 0
        )
        try:
            proxy = BrowserGuardProxy(
                (BROWSER_GUARD_PROXY_HOST, port),
                policy_key,
            )
        except OSError as exc:
            raise OSError(
                "The browser network guard could not bind its fixed loopback port."
            ) from exc
        thread = threading.Thread(
            target=proxy.serve_forever,
            name="jarvis-browser-network-guard",
            daemon=True,
        )
        thread.start()
        if not thread.is_alive():
            proxy.server_close()
            raise OSError("The browser network guard did not start.")
        _browser_guard_proxies[policy_key] = (proxy, thread)
        return proxy.server_address


def _guarded_chrome_attestation_path() -> Path:
    raw_home = os.environ.get("JARVIS_HOME", "").strip()
    root = Path(raw_home) if raw_home else Path.home()
    return root / ".jarvis" / "browser-guard-attestations-v1.json"


def _attestation_signature(records: dict[str, Any]) -> str:
    if _bridge_attestation_hmac_key is None:
        raise OSError("Bridge attestation key is unavailable.")
    canonical = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(_bridge_attestation_hmac_key, canonical, hashlib.sha256).hexdigest()


def _persist_guarded_chrome_attestations() -> None:
    with _guarded_chrome_persist_lock:
        with _guarded_chrome_attestations_lock:
            records = {
                str(port): dict(record)
                for port, record in sorted(_guarded_chrome_attestations.items())
            }
        envelope = {"records": records, "signature": _attestation_signature(records)}
        path = _guarded_chrome_attestation_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(path.parent, 0o700)
        if path.is_symlink():
            raise OSError("Browser guard attestation path cannot be a symlink.")
        temporary = path.with_suffix(f".tmp-{os.getpid()}")
        if temporary.is_symlink():
            raise OSError("Browser guard temporary path cannot be a symlink.")
        temporary.write_text(
            json.dumps(envelope, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        with suppress(OSError):
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)


def _drop_guarded_chrome_attestation(debug_port: int) -> None:
    with _guarded_chrome_attestations_lock:
        removed = _guarded_chrome_attestations.pop(debug_port, None)
    if removed is not None:
        with suppress(OSError):
            _persist_guarded_chrome_attestations()


def _load_guarded_chrome_attestations() -> None:
    path = _guarded_chrome_attestation_path()
    if path.is_symlink():
        with _guarded_chrome_attestations_lock:
            _guarded_chrome_attestations.clear()
        return
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        with _guarded_chrome_attestations_lock:
            _guarded_chrome_attestations.clear()
        return
    records = envelope.get("records") if isinstance(envelope, dict) else None
    signature = envelope.get("signature") if isinstance(envelope, dict) else None
    if not isinstance(records, dict) or not isinstance(signature, str):
        records = {}
    else:
        expected = _attestation_signature(records)
        if not hmac.compare_digest(signature, expected):
            records = {}
    loaded: dict[int, dict[str, Any]] = {}
    for raw_port, record in records.items():
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            continue
        if 1_024 <= port <= 65_535 and isinstance(record, dict):
            loaded[port] = dict(record)
    with _guarded_chrome_attestations_lock:
        _guarded_chrome_attestations.clear()
        _guarded_chrome_attestations.update(loaded)


def _browser_guard_proxy_endpoint(record: dict[str, Any]) -> tuple[str, int]:
    parsed = urlsplit(str(record.get("proxy") or ""))
    if (
        parsed.scheme != "http"
        or parsed.hostname != BROWSER_GUARD_PROXY_HOST
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise OSError("Stored browser guard proxy endpoint is invalid.")
    return parsed.hostname, parsed.port


def _browser_guard_proxy_healthy(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _terminate_guarded_chrome_process(record: dict[str, Any]) -> None:
    owner_pid = _listening_tcp_owner_pid(int(record.get("debug_port") or 0))
    if owner_pid != record.get("owner_pid"):
        return
    identity = _windows_process_identity(owner_pid)
    command_hash = hashlib.sha256(
        str(identity.get("command_line") or "").encode("utf-8")
    ).hexdigest()
    if (
        identity.get("creation_utc") != record.get("creation_utc")
        or command_hash != record.get("command_line_sha256")
        or not _guarded_process_identity_matches(
            identity,
            launch_nonce=str(record.get("launch_nonce") or ""),
            profile_dir=str(record.get("profile_dir") or ""),
            proxy=str(record.get("proxy") or ""),
        )
    ):
        return
    if os.name != "nt":
        return
    taskkill = _windows_system_binary("taskkill.exe")
    subprocess.run(  # noqa: S603 - exact attested PID and canonical binary
        [taskkill, "/PID", str(owner_pid), "/T", "/F"],
        capture_output=True,
        timeout=10,
        check=False,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _restore_guarded_browser_proxies() -> None:
    with _guarded_chrome_attestations_lock:
        records = list(_guarded_chrome_attestations.items())
    stale_ports: list[int] = []
    for debug_port, record in records:
        try:
            host, proxy_port = _browser_guard_proxy_endpoint(record)
            raw_hosts = record.get("allowed_private_hosts")
            if not isinstance(raw_hosts, list) or not all(
                isinstance(item, str) for item in raw_hosts
            ):
                raise OSError("Stored browser guard host policy is invalid.")
            endpoint = _ensure_browser_guard_proxy(
                tuple(raw_hosts),
                requested_port=proxy_port,
            )
            if endpoint != (host, proxy_port) or not _browser_guard_proxy_healthy(*endpoint):
                raise OSError("Restored browser guard proxy failed its health probe.")
        except (OSError, ValueError):
            with suppress(OSError):
                _terminate_guarded_chrome_process(record)
            stale_ports.append(debug_port)
    if stale_ports:
        with _guarded_chrome_attestations_lock:
            for debug_port in stale_ports:
                _guarded_chrome_attestations.pop(debug_port, None)
        _persist_guarded_chrome_attestations()


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
        global _bridge_attestation_hmac_key, _browser_guard_recovery_error
        if not token:
            raise ValueError("Bridge token must not be empty.")
        _bridge_attestation_hmac_key = hashlib.sha256(
            b"jarvis-browser-guard-attestation-v1\0" + token.encode("utf-8")
        ).digest()
        try:
            _load_guarded_chrome_attestations()
            _restore_guarded_browser_proxies()
            _browser_guard_recovery_error = ""
        except OSError as exc:
            # Browser recovery is fail-closed, but a broken/stale attestation
            # file must not disable unrelated structured host actions.
            with _guarded_chrome_attestations_lock:
                _guarded_chrome_attestations.clear()
            _browser_guard_recovery_error = str(exc)[:500]
        super().__init__(address, BridgeHandler)
        self.token = token
        self.started_at = time.monotonic()


def _focus_hint_process_name(executable: str, arguments: list[str]) -> str:
    """Locale-invariant window process-name fragment for a launcher-style app.

    Returns '' when the launched executable already owns its own window (so the
    launch PID is a fine focus target) or the app is unknown.
    """

    name = PureWindowsPath(str(executable)).name.casefold()
    if name == "explorer.exe" and list(arguments) == [CALCULATOR_APP_URI]:
        return "Calculator"
    return NATIVE_APP_FOCUS_HINTS.get(name, "")


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
        elif action == "browser.open_guarded":
            result = _open_guarded_url(action, payload)
        elif action == "chrome.attest_guarded":
            result = _attest_guarded_chrome(action, payload)
        elif action == "chrome.launch_guarded":
            result = _launch_guarded_chrome(action, payload)
        elif action == "app.open_and_type":
            process_result = _start_process(action, payload)
            native_payload = {
                key: value
                for key, value in payload.items()
                if key not in {"executable", "arguments", "cwd"}
            }
            native_payload["process_id"] = process_result["pid"]
            # Launcher-style apps hand their window to a differently-named process,
            # so the launch PID cannot be focused. When the caller gave no focus
            # target, steer the bridge at the real window by its locale-invariant
            # process name instead of failing with "window was not focused".
            if not native_payload.get("process_name") and not native_payload.get("window_title"):
                hint = _focus_hint_process_name(
                    payload.get("executable", ""), payload.get("arguments") or []
                )
                if hint:
                    native_payload["process_name"] = hint
            result = _run_fixed_native_action(action, native_payload, timeout_sec)
            result.setdefault("argv", process_result["argv"])
            result["launch_pid"] = process_result["pid"]
            # Prefer the actually-focused window's PID for downstream state
            # verification; fall back to the launch PID for classic Win32 apps.
            # The PowerShell output is nested under result["result"]["data"].
            native = result.get("result")
            native_data = native.get("data") if isinstance(native, dict) else None
            focus_pid = (
                native_data.get("focus_pid") if isinstance(native_data, dict) else None
            )
            if isinstance(focus_pid, int) and not isinstance(focus_pid, bool) and focus_pid > 0:
                result["pid"] = focus_pid
            else:
                result.setdefault("pid", process_result["pid"])
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
        "browser.open_guarded": _validate_guarded_url_open,
        "capabilities": _validate_empty_payload,
        "chrome.attest_guarded": _validate_guarded_chrome_attestation,
        "chrome.launch_guarded": _validate_guarded_chrome_launch,
        "console.show_processes": _validate_process_view,
        "keyboard.send": _validate_keyboard_send,
        "process.start": _validate_process_start,
        "process.top": _validate_process_view,
        "screen.capture": _validate_screen_capture,
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


def _validated_guarded_profile_dir(value: Any) -> str:
    profile_dir = _required_string(value, "profile_dir", 500)
    if not _is_absolute_path(profile_dir):
        raise ActionValidationError("profile_dir must be an absolute path.")
    return profile_dir


def _validated_allowed_private_hosts(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 8:
        raise ActionValidationError("allowed_private_hosts must be a list of at most 8 hosts.")
    hosts: list[str] = []
    for item in value:
        host = _required_string(item, "allowed_private_host", 253).rstrip(".").casefold()
        if not re.fullmatch(r"[a-z0-9._:-]+", host):
            raise ActionValidationError("allowed_private_host contains unsupported characters.")
        if host not in hosts:
            hosts.append(host)
    return tuple(sorted(hosts))


def _validate_guarded_url_open(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(
        payload,
        {"allowed_private_hosts", "profile_dir", "url"},
        "browser.open_guarded payload",
    )
    return {
        "profile_dir": _validated_guarded_profile_dir(payload.get("profile_dir")),
        "url": _validated_http_url(payload.get("url"), allow_about_blank=False),
        "allowed_private_hosts": _validated_allowed_private_hosts(
            payload.get("allowed_private_hosts")
        ),
    }


def _validate_guarded_chrome_launch(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(
        payload,
        {
            "allowed_private_hosts",
            "debug_port",
            "headless",
            "launch_nonce",
            "profile_dir",
            "start_url",
        },
        "chrome.launch_guarded payload",
    )
    profile_dir = _validated_guarded_profile_dir(payload.get("profile_dir"))
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
        "allowed_private_hosts": _validated_allowed_private_hosts(
            payload.get("allowed_private_hosts")
        ),
        "launch_nonce": _validated_launch_nonce(payload.get("launch_nonce")),
    }


def _validated_launch_nonce(value: Any) -> str:
    nonce = _required_string(value, "launch_nonce", 128)
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", nonce):
        raise ActionValidationError("launch_nonce has invalid format.")
    return nonce


def _validate_guarded_chrome_attestation(payload: dict[str, Any]) -> dict[str, Any]:
    _reject_extra_keys(
        payload,
        {"debug_port", "launch_nonce", "profile_dir"},
        "chrome.attest_guarded payload",
    )
    return {
        "debug_port": _strict_int(payload.get("debug_port"), "debug_port", 1_024, 65_535),
        "launch_nonce": _validated_launch_nonce(payload.get("launch_nonce")),
        "profile_dir": _validated_guarded_profile_dir(payload.get("profile_dir")),
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


def _guarded_chrome_arguments(
    *,
    profile_dir: str,
    start_url: str,
    debug_port: int | None,
    headless: bool,
    allowed_private_hosts: tuple[str, ...],
    launch_nonce: str | None,
    profile_instance: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    proxy_host, proxy_port = _ensure_browser_guard_proxy(allowed_private_hosts)
    # A versioned child keeps a previously unguarded Jarvis profile from being
    # silently reused by Chrome, which would cause new command-line policy flags
    # to be ignored by an already-running browser process.
    policy_label = (
        hashlib.sha256("\0".join(allowed_private_hosts).encode("utf-8")).hexdigest()[:12]
        if allowed_private_hosts
        else "public"
    )
    effective_profile = Path(profile_dir) / BROWSER_NETWORK_GUARD / policy_label
    if profile_instance:
        effective_profile /= profile_instance
    effective_profile.mkdir(parents=True, exist_ok=True)
    arguments = [
        f"--user-data-dir={effective_profile}",
        f"--proxy-server=http://{proxy_host}:{proxy_port}",
        "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=MAP * ~NOTFOUND",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-quic",
        "--disable-extensions",
        "--enable-automation",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if debug_port is not None:
        arguments.append(f"--remote-debugging-port={debug_port}")
    if launch_nonce is not None:
        arguments.append(f"--jarvis-guard-nonce={launch_nonce}")
    if headless:
        arguments.append("--headless=new")
    arguments.append(start_url)
    return arguments, {
        "enforced": True,
        "version": BROWSER_NETWORK_GUARD,
        "proxy": f"http://{proxy_host}:{proxy_port}",
        "private_networks": "blocked",
        "redirects": "validated_per_connection",
        "dns_rebinding": "numeric_ip_pinned_per_connection",
        "direct_dns": "disabled",
        "non_proxied_udp": "disabled",
        "fail_closed": True,
        "profile_dir": str(effective_profile),
        "allowed_private_hosts": list(allowed_private_hosts),
        "session_class": "private-only" if allowed_private_hosts else "public-only",
    }


def _open_guarded_url(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    chrome = _find_chrome()
    open_nonce = secrets.token_urlsafe(32)
    arguments, guard = _guarded_chrome_arguments(
        profile_dir=payload["profile_dir"],
        start_url=payload["url"],
        debug_port=None,
        headless=False,
        allowed_private_hosts=payload["allowed_private_hosts"],
        launch_nonce=open_nonce,
        profile_instance=_browser_open_profile_instance,
    )
    result = _start_process(
        action,
        {"executable": chrome, "arguments": arguments, "cwd": None},
    )
    result.pop("argv", None)
    result.update(
        {
            "summary": "URL opened in a public-network-guarded Chrome session.",
            "url": payload["url"],
            "profile_dir": guard["profile_dir"],
            "network_guard": guard,
        }
    )
    return result


def _listening_tcp_owner_pid(port: int) -> int | None:
    if os.name != "nt":
        return None
    powershell = _canonical_windows_powershell()
    script = (
        "$ErrorActionPreference='Stop';"
        f"$owners=@(Get-NetTCPConnection -State Listen -LocalPort {port} "
        "-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique);"
        "@($owners)|ConvertTo-Json -Compress"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = subprocess.run(  # noqa: S603 - canonical binary and fixed argv
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    values = payload if isinstance(payload, list) else [payload]
    owners = {
        value for value in values if isinstance(value, int) and not isinstance(value, bool)
    }
    if len(owners) > 1:
        raise OSError("Multiple processes unexpectedly own the Chrome debug port.")
    return next(iter(owners), None)


def _wait_for_guarded_debug_owner(debug_port: int) -> int:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        owner_pid = _listening_tcp_owner_pid(debug_port)
        if owner_pid is not None:
            return owner_pid
        time.sleep(0.1)
    raise OSError("Guarded Chrome did not bind its debug port in time.")


def _windows_process_name(process_id: int) -> str:
    return str(_windows_process_identity(process_id).get("name") or "")


def _windows_process_identity(process_id: int) -> dict[str, Any]:
    if os.name != "nt":
        return {}
    powershell = _canonical_windows_powershell()
    script = (
        "$ErrorActionPreference='Stop';"
        f"$p=Get-CimInstance -ClassName Win32_Process -Filter \"ProcessId = {process_id}\";"
        "if($null -eq $p){exit 3};"
        "$created=([datetime]$p.CreationDate).ToUniversalTime().ToString('o');"
        "[pscustomobject]@{ProcessId=[int]$p.ProcessId;Name=[string]$p.Name;"
        "CreationUtc=$created;CommandLine=[string]$p.CommandLine}|ConvertTo-Json -Compress"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    completed = subprocess.run(  # noqa: S603 - canonical binary and fixed argv
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        "process_id": payload.get("ProcessId"),
        "name": str(payload.get("Name") or ""),
        "creation_utc": str(payload.get("CreationUtc") or ""),
        "command_line": str(payload.get("CommandLine") or ""),
    }


def _guarded_process_identity_matches(
    identity: dict[str, Any],
    *,
    launch_nonce: str,
    profile_dir: str,
    proxy: str,
) -> bool:
    command_line = str(identity.get("command_line") or "")
    required = (
        f"--jarvis-guard-nonce={launch_nonce}",
        f"--proxy-server={proxy}",
        "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=MAP * ~NOTFOUND",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-quic",
        "--enable-automation",
        f"--user-data-dir={profile_dir}",
    )
    return bool(
        identity.get("name", "").casefold() == "chrome.exe"
        and identity.get("creation_utc")
        and all(item in command_line for item in required)
    )


def _launch_guarded_chrome(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    debug_port = payload["debug_port"]
    if _listening_tcp_owner_pid(debug_port) is not None:
        raise OSError(
            "Chrome debug port is already owned; refusing to attest a pre-existing endpoint."
        )
    chrome = _find_chrome()
    arguments, guard = _guarded_chrome_arguments(
        profile_dir=payload["profile_dir"],
        start_url=payload["start_url"],
        debug_port=payload["debug_port"],
        headless=payload["headless"],
        allowed_private_hosts=payload["allowed_private_hosts"],
        launch_nonce=payload["launch_nonce"],
    )
    result = _start_process(
        action,
        {"executable": chrome, "arguments": arguments, "cwd": None},
    )
    result.pop("argv", None)
    owner_pid = _wait_for_guarded_debug_owner(debug_port)
    identity = _windows_process_identity(owner_pid)
    if not _guarded_process_identity_matches(
        identity,
        launch_nonce=payload["launch_nonce"],
        profile_dir=str(guard["profile_dir"]),
        proxy=str(guard["proxy"]),
    ):
        raise OSError(
            "Chrome debug port owner command line does not match the guarded launch."
        )
    attestation = {
        "debug_port": debug_port,
        "launch_nonce": payload["launch_nonce"],
        "owner_pid": owner_pid,
        "profile_dir": guard["profile_dir"],
        "proxy": guard["proxy"],
        "allowed_private_hosts": guard["allowed_private_hosts"],
        "session_class": guard["session_class"],
        "creation_utc": identity["creation_utc"],
        "command_line_sha256": hashlib.sha256(
            str(identity["command_line"]).encode("utf-8")
        ).hexdigest(),
    }
    with _guarded_chrome_attestations_lock:
        _guarded_chrome_attestations[debug_port] = attestation
    try:
        _persist_guarded_chrome_attestations()
    except OSError as exc:
        with suppress(OSError):
            _terminate_guarded_chrome_process(attestation)
        with _guarded_chrome_attestations_lock:
            _guarded_chrome_attestations.pop(debug_port, None)
        raise OSError(
            "Guarded Chrome attestation could not be persisted; the exact process was closed."
        ) from exc
    result.update(
        {
            "summary": "Chrome launched with the public-network guard enforced.",
            "debug_url": f"http://127.0.0.1:{payload['debug_port']}",
            "profile_dir": guard["profile_dir"],
            "start_url": payload["start_url"],
            "network_guard": guard,
            "attestation": {
                "verified": True,
                "debug_port": debug_port,
                "owner_pid": owner_pid,
                "profile_dir": guard["profile_dir"],
            },
        }
    )
    return result


def _attest_guarded_chrome(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    debug_port = payload["debug_port"]
    with _guarded_chrome_attestations_lock:
        expected = dict(_guarded_chrome_attestations.get(debug_port) or {})
    if not expected:
        raise OSError("No guarded Chrome launch is registered for this debug port.")
    if not secrets.compare_digest(expected["launch_nonce"], payload["launch_nonce"]):
        raise OSError("Guarded Chrome launch nonce does not match.")
    if os.path.normcase(expected["profile_dir"]) != os.path.normcase(payload["profile_dir"]):
        raise OSError("Guarded Chrome profile does not match.")
    try:
        proxy_host, proxy_port = _browser_guard_proxy_endpoint(expected)
        raw_hosts = expected.get("allowed_private_hosts")
        if not isinstance(raw_hosts, list) or not all(isinstance(item, str) for item in raw_hosts):
            raise OSError("Guarded Chrome host policy is malformed.")
        endpoint = _ensure_browser_guard_proxy(
            tuple(raw_hosts),
            requested_port=proxy_port,
        )
        if endpoint != (proxy_host, proxy_port) or not _browser_guard_proxy_healthy(*endpoint):
            raise OSError("Guarded Chrome proxy health verification failed.")
    except (OSError, ValueError) as exc:
        with suppress(OSError):
            _terminate_guarded_chrome_process(expected)
        _drop_guarded_chrome_attestation(debug_port)
        raise OSError(
            "Guarded Chrome proxy could not be restored; the exact stale process was closed."
        ) from exc
    owner_pid = _listening_tcp_owner_pid(debug_port)
    if owner_pid != expected["owner_pid"]:
        _drop_guarded_chrome_attestation(debug_port)
        raise OSError("Chrome debug port ownership changed after launch.")
    identity = _windows_process_identity(owner_pid)
    current_hash = hashlib.sha256(
        str(identity.get("command_line") or "").encode("utf-8")
    ).hexdigest()
    if not _guarded_process_identity_matches(
        identity,
        launch_nonce=payload["launch_nonce"],
        profile_dir=expected["profile_dir"],
        proxy=expected["proxy"],
    ):
        _drop_guarded_chrome_attestation(debug_port)
        raise OSError("Chrome process command line no longer matches the guarded launch.")
    if (
        identity.get("creation_utc") != expected.get("creation_utc")
        or current_hash != expected.get("command_line_sha256")
    ):
        _drop_guarded_chrome_attestation(debug_port)
        raise OSError("Chrome process identity changed after its guarded launch.")
    return {
        "ok": True,
        "action": action,
        "summary": "Guarded Chrome port owner and launch record match.",
        "state": "verified",
        "debug_port": debug_port,
        "owner_pid": owner_pid,
        "profile_dir": expected["profile_dir"],
        "proxy": expected["proxy"],
        "allowed_private_hosts": expected["allowed_private_hosts"],
        "session_class": expected["session_class"],
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "output_truncated": False,
        "timed_out": False,
    }


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
        "browser_network_guard": {
            "required_actions": [
                "browser.open_guarded",
                "chrome.attest_guarded",
                "chrome.launch_guarded",
            ],
            "version": BROWSER_NETWORK_GUARD,
            "proxy_host": BROWSER_GUARD_PROXY_HOST,
            "public_proxy_port": BROWSER_GUARD_PROXY_PORT,
            "private_networks": "blocked",
            "dns_rebinding": "numeric_ip_pinned_per_connection",
            "fail_closed": True,
            "recovery_error": _browser_guard_recovery_error,
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
using System.Text;
using System.Runtime.InteropServices;
public static class JarvisBridgeWinApi {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int command);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
  [DllImport("user32.dll", EntryPoint="SystemParametersInfoW", SetLastError=true)] public static extern bool SpiSet(uint uiAction, uint uiParam, IntPtr pvParam, uint fWinIni);
  [DllImport("user32.dll", EntryPoint="SystemParametersInfoW", SetLastError=true)] public static extern bool SpiGet(uint uiAction, uint uiParam, ref uint pvParam, uint fWinIni);

  // The foreground lock timeout makes SetForegroundWindow a silent no-op for a
  // background service. Zeroing it (the documented workaround) lets the bridge
  // raise a window reliably; the original value is restored afterwards so the
  // user's anti-focus-stealing setting is left untouched.
  public static uint DisableForegroundLock() {
    uint original = 0;
    SpiGet(0x2000, 0, ref original, 0);   // SPI_GETFOREGROUNDLOCKTIMEOUT
    SpiSet(0x2001, 0, IntPtr.Zero, 0);    // SPI_SETFOREGROUNDLOCKTIMEOUT -> 0
    return original;
  }
  public static void RestoreForegroundLock(uint original) {
    SpiSet(0x2001, 0, new IntPtr((long)original), 0x02);  // SPIF_SENDCHANGE
  }
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowEx(IntPtr parent, IntPtr child, string cls, string title);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, StringBuilder buffer, int max);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowTextLength(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

  // Resolve the visible top-level window for a process. Handles UWP apps whose
  // frame is hosted by ApplicationFrameHost.exe: the real app PID lives on a child
  // "Windows.UI.Core.CoreWindow", so a plain PID/MainWindowHandle lookup misses it.
  public static IntPtr FindTopWindowForPid(uint targetPid) {
    IntPtr result = IntPtr.Zero;
    EnumWindows((h, l) => {
      if (!IsWindowVisible(h)) return true;
      uint wpid; GetWindowThreadProcessId(h, out wpid);
      if (wpid == targetPid) { result = h; return false; }
      IntPtr core = FindWindowEx(h, IntPtr.Zero, "Windows.UI.Core.CoreWindow", null);
      if (core != IntPtr.Zero) {
        uint cpid; GetWindowThreadProcessId(core, out cpid);
        if (cpid == targetPid) { result = h; return false; }
      }
      return true;
    }, IntPtr.Zero);
    return result;
  }

  // Find a visible top-level window whose title contains the needle (case-insensitive).
  public static IntPtr FindTopWindowByTitle(string needle) {
    IntPtr result = IntPtr.Zero;
    string lowered = (needle ?? "").ToLowerInvariant();
    if (lowered.Length == 0) return result;
    EnumWindows((h, l) => {
      if (!IsWindowVisible(h)) return true;
      int length = GetWindowTextLength(h);
      if (length <= 0) return true;
      StringBuilder buffer = new StringBuilder(length + 1);
      GetWindowText(h, buffer, buffer.Capacity);
      if (buffer.ToString().ToLowerInvariant().Contains(lowered)) { result = h; return false; }
      return true;
    }, IntPtr.Zero);
    return result;
  }
}
'@

  $script:LastFocusPid = 0
  $script:LastFocusName = ''
  $script:LastForegroundConfirmed = $false

  function ResolveTargetWindow($ProcessId, $ProcessName, $WindowTitle) {
    # Returns @{ Handle; Pid; Name } for a focusable top-level window, or $null.
    # Candidate processes are matched even when their own MainWindowHandle is 0
    # (UWP apps host their window in ApplicationFrameHost), then the real frame
    # window is resolved by PID via WinAPI.
    $candidates = @()
    if ([int64]$ProcessId -gt 0) {
      $byId = Get-Process -Id ([int]$ProcessId) -ErrorAction SilentlyContinue
      if ($byId) { $candidates += $byId }
    }
    if ($ProcessName) {
      $needle = ([string]$ProcessName) -replace '\.exe$', ''
      $candidates += Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like ('*' + $needle + '*') } |
        Sort-Object { try { $_.StartTime.Ticks } catch { 0 } } -Descending
    }
    foreach ($proc in $candidates) {
      $handle = [JarvisBridgeWinApi]::FindTopWindowForPid([uint32]$proc.Id)
      if ($handle -ne [IntPtr]::Zero) {
        return @{ Handle = $handle; Pid = [int]$proc.Id; Name = [string]$proc.ProcessName }
      }
    }
    if ($WindowTitle) {
      $handle = [JarvisBridgeWinApi]::FindTopWindowByTitle([string]$WindowTitle)
      if ($handle -ne [IntPtr]::Zero) {
        $wpid = [uint32]0
        [void][JarvisBridgeWinApi]::GetWindowThreadProcessId($handle, [ref]$wpid)
        $name = (Get-Process -Id ([int]$wpid) -ErrorAction SilentlyContinue).ProcessName
        return @{ Handle = $handle; Pid = [int]$wpid; Name = [string]$name }
      }
    }
    return $null
  }

  function ForceForeground($Handle) {
    # Lift the OS foreground-steal lock for the duration of the raise, then restore.
    $origLock = [JarvisBridgeWinApi]::DisableForegroundLock()
    if ([JarvisBridgeWinApi]::IsIconic($Handle)) {
      [void][JarvisBridgeWinApi]::ShowWindow($Handle, 9)
    } else {
      [void][JarvisBridgeWinApi]::ShowWindow($Handle, 5)
    }
    $foreground = [JarvisBridgeWinApi]::GetForegroundWindow()
    $targetPid = [uint32]0
    $targetThread = [JarvisBridgeWinApi]::GetWindowThreadProcessId($Handle, [ref]$targetPid)
    $foregroundPid = [uint32]0
    $foregroundThread = [JarvisBridgeWinApi]::GetWindowThreadProcessId($foreground, [ref]$foregroundPid)
    $current = [JarvisBridgeWinApi]::GetCurrentThreadId()
    $attachedForeground = $false
    $attachedTarget = $false
    if ($foregroundThread -ne 0 -and $foregroundThread -ne $current) {
      $attachedForeground = [JarvisBridgeWinApi]::AttachThreadInput($current, $foregroundThread, $true)
    }
    if ($targetThread -ne 0 -and $targetThread -ne $current -and $targetThread -ne $foregroundThread) {
      $attachedTarget = [JarvisBridgeWinApi]::AttachThreadInput($current, $targetThread, $true)
    }
    # A synthetic Alt tap clears the OS foreground lock that otherwise makes
    # SetForegroundWindow a silent no-op for a background service.
    [JarvisBridgeWinApi]::keybd_event(0xA4, 0, 0, [UIntPtr]::Zero)
    [JarvisBridgeWinApi]::keybd_event(0xA4, 0, 2, [UIntPtr]::Zero)
    [void][JarvisBridgeWinApi]::BringWindowToTop($Handle)
    [void][JarvisBridgeWinApi]::ShowWindowAsync($Handle, 9)
    [void][JarvisBridgeWinApi]::SetForegroundWindow($Handle)
    if ($attachedTarget) { [void][JarvisBridgeWinApi]::AttachThreadInput($current, $targetThread, $false) }
    if ($attachedForeground) { [void][JarvisBridgeWinApi]::AttachThreadInput($current, $foregroundThread, $false) }
    [void][JarvisBridgeWinApi]::RestoreForegroundLock($origLock)
  }

  function FocusWindow($ProcessId, $ProcessName, $WindowTitle) {
    $script:LastFocusPid = 0
    $script:LastFocusName = ''
    $script:LastForegroundConfirmed = $false
    # Retry generously: a freshly launched UWP app can take a couple of seconds to
    # register its ApplicationFrameHost window.
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
      $target = ResolveTargetWindow $ProcessId $ProcessName $WindowTitle
      if ($target) {
        $handle = $target.Handle
        $script:LastFocusPid = $target.Pid
        $script:LastFocusName = $target.Name
        ForceForeground $handle
        Start-Sleep -Milliseconds 140
        $confirmed = ([JarvisBridgeWinApi]::GetForegroundWindow() -eq $handle)
        $script:LastForegroundConfirmed = $confirmed
        # Return as soon as the window is verifiably in front; after a few
        # attempts accept the located+raised window so input still lands.
        if ($confirmed -or $attempt -ge 4) { return $true }
      }
      Start-Sleep -Milliseconds 220
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
        focus_pid = $script:LastFocusPid
        focus_process = $script:LastFocusName
        foreground_confirmed = $script:LastForegroundConfirmed
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
      Out $true 'Native keyboard input sent.' @{
        focused = $focused
        focus_pid = $script:LastFocusPid
        focus_process = $script:LastFocusName
        foreground_confirmed = $script:LastForegroundConfirmed
      }
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
        focused = $true
        pid = $Payload.process_id
        focus_pid = $script:LastFocusPid
        focus_process = $script:LastFocusName
        foreground_confirmed = $script:LastForegroundConfirmed
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
    # The fixed script outgrew -EncodedCommand's command-line length limit, so it
    # runs from a temp file whose content is the constant FIXED_NATIVE_POWERSHELL.
    # The per-request payload still travels only in JARVIS_BRIDGE_ACTION_JSON, so no
    # request text ever reaches the command line or the executed script.
    script_path = _materialize_fixed_native_script()
    try:
        completed = subprocess.run(  # noqa: S603 - the fixed script contains no request text
            powershell_command(powershell, script_path),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=timeout_sec,
            check=False,
            env=env,
            creationflags=creationflags,
        )
    finally:
        with suppress(OSError):
            os.unlink(script_path)
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


def _materialize_fixed_native_script() -> str:
    """Write the constant fixed native script to a fresh temp .ps1 and return its path.

    Content is always FIXED_NATIVE_POWERSHELL — never request text — so running it
    with -File carries the same guarantee as the former -EncodedCommand path.
    """

    handle, path = tempfile.mkstemp(prefix="jarvis-native-", suffix=".ps1")
    try:
        with os.fdopen(handle, "w", encoding="utf-8-sig") as stream:
            stream.write(FIXED_NATIVE_POWERSHELL)
    except OSError:
        with suppress(OSError):
            os.unlink(path)
        raise
    return path


def powershell_command(powershell: str, script_path: str) -> list[str]:
    executable = PureWindowsPath(powershell).name.lower() or Path(powershell).name.lower()
    args = [powershell, "-NoLogo", "-NoProfile"]
    if executable == "powershell.exe":
        args.extend(["-STA", "-NonInteractive", "-ExecutionPolicy", "RemoteSigned"])
    else:
        args.append("-NonInteractive")
    args.extend(["-File", script_path])
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
