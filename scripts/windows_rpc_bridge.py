from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_BODY_BYTES = 65_536
MAX_COMMAND_CHARS = 30_000
MAX_OUTPUT_CHARS = 40_000


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
            }
        )

    def do_POST(self) -> None:
        if self.path != "/execute":
            self._send({"ok": False, "summary": "Not found."}, status=404)
            return
        if not self._authorized():
            self._send({"ok": False, "summary": "Unauthorized."}, status=401)
            return

        payload = self._read_json()
        if payload is None:
            return
        result, status = execute_command(payload)
        self._send(result, status=status)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        provided = header[len(prefix) :].strip()
        return secrets.compare_digest(provided, self.server.token)

    def _read_json(self) -> dict[str, Any] | None:
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
        self.end_headers()
        self.wfile.write(body)


class BridgeServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], token: str) -> None:
        super().__init__(address, BridgeHandler)
        self.token = token
        self.started_at = time.monotonic()


def execute_command(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    command = str(payload.get("command") or "").strip()
    if not command:
        return {"ok": False, "summary": "command is required."}, 400
    if len(command) > MAX_COMMAND_CHARS:
        return {"ok": False, "summary": "command is too long."}, 400

    timeout_sec = clamp_int(payload.get("timeout_sec"), default=30, minimum=1, maximum=120)
    cwd = payload.get("cwd")
    cwd_path = None
    if cwd:
        cwd_path = Path(str(cwd)).expanduser().resolve()
        if not cwd_path.is_dir():
            return {"ok": False, "summary": f"cwd is not a directory: {cwd_path}"}, 400

    powershell = powershell_path()
    if powershell is None:
        return {"ok": False, "summary": "powershell.exe or pwsh is not available."}, 500

    started = time.monotonic()
    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            cwd=str(cwd_path) if cwd_path else None,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "summary": f"Command timed out after {timeout_sec}s.",
            "stdout": trim_output(exc.stdout),
            "stderr": trim_output(exc.stderr),
            "timeout_sec": timeout_sec,
        }, 408

    elapsed_sec = round(time.monotonic() - started, 3)
    ok = completed.returncode == 0
    return {
        "ok": ok,
        "summary": "Command completed." if ok else f"Command exited {completed.returncode}.",
        "returncode": completed.returncode,
        "stdout": trim_output(completed.stdout),
        "stderr": trim_output(completed.stderr),
        "elapsed_sec": elapsed_sec,
    }, 200 if ok else 500


def powershell_path() -> str | None:
    return shutil.which("powershell.exe") or shutil.which("pwsh")


def trim_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return f"{text[:MAX_OUTPUT_CHARS]}\n...[truncated]"


def clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def default_token_file() -> Path:
    raw_home = os.environ.get("JARVIS_HOME")
    home = Path(raw_home) if raw_home else Path(r"D:\jarvis")
    return home / ".jarvis" / "bridge.token"


def ensure_token(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{token}\n", encoding="utf-8")
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local JARVIS Windows RPC bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file", type=Path, default=default_token_file())
    return parser


def main() -> None:
    args = build_parser().parse_args()
    token = ensure_token(args.token_file)
    server = BridgeServer((args.host, args.port), token)
    print(
        json.dumps(
            {
                "ok": True,
                "name": "windows_rpc_bridge",
                "host": args.host,
                "port": args.port,
                "token_file": str(args.token_file),
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
