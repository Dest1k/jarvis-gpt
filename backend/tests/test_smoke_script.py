from __future__ import annotations

import json
import sys
import urllib.error
from types import SimpleNamespace

from scripts import smoke


def test_optional_nonzero_command_is_reported_failed(monkeypatch):
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=7,
            stdout="partial output",
            stderr="command failed",
        ),
    )

    result = smoke.run("optional command", ["missing-tool"], optional=True)

    assert result["ok"] is False
    assert result["optional"] is True
    assert result["status"] == "failed"
    assert result["returncode"] == 7


def test_optional_missing_command_is_skipped_but_never_successful(monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("tool not installed")

    monkeypatch.setattr(smoke.subprocess, "run", missing)

    result = smoke.run("optional command", ["missing-tool"], optional=True)

    assert result["ok"] is False
    assert result["status"] == "skipped"
    assert "not installed" in result["error"]


def test_optional_unreachable_http_is_skipped_but_never_successful(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(smoke.urllib.request, "urlopen", unavailable)

    result = smoke.http("optional service", "http://127.0.0.1:1", optional=True)

    assert result["ok"] is False
    assert result["status"] == "skipped"


def test_main_exit_ignores_optional_gap_but_reports_degraded(monkeypatch, capsys):
    def fake_run(name, _command, *, cwd=smoke.ROOT, optional=False, env=None):
        if name == "docker compose config":
            assert env["JARVIS_QWEN_MODEL_PATH"] == (
                "/models/__jarvis_compose_config_check__"
            )
        if optional:
            return {
                "name": name,
                "ok": False,
                "optional": True,
                "status": "failed",
                "error": "docker unavailable",
            }
        return {"name": name, "ok": True, "optional": False, "status": "passed"}

    monkeypatch.setattr(smoke, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["smoke.py", "--skip-frontend", "--skip-http"])

    exit_code = smoke.main()
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["ok"] is True
    assert report["degraded"] is True
    assert report["summary"]["optional_gaps"] == 1


def test_main_required_failure_sets_nonzero_exit(monkeypatch, capsys):
    def fake_run(name, _command, *, cwd=smoke.ROOT, optional=False, env=None):
        if name == "docker compose config":
            assert env["JARVIS_QWEN_MODEL_PATH"] == (
                "/models/__jarvis_compose_config_check__"
            )
        if name == "backend tests":
            return {
                "name": name,
                "ok": False,
                "optional": False,
                "status": "failed",
                "returncode": 1,
            }
        return {
            "name": name,
            "ok": not optional,
            "optional": optional,
            "status": "passed" if not optional else "skipped",
        }

    monkeypatch.setattr(smoke, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["smoke.py", "--skip-frontend", "--skip-http"])

    exit_code = smoke.main()
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert report["ok"] is False


def test_compose_config_stdout_redacts_api_token_canary(monkeypatch):
    """SPARK-0017: doctor/smoke must never echo JARVIS_API_TOKEN values."""
    canary = "CANARY_TOKEN_SPARK0017_deadbeef"
    compose_stdout = (
        "name: jarvis-gpt\nservices:\n  backend:\n    environment:\n"
        f"      JARVIS_API_TOKEN: {canary}\n"
        "      JARVIS_BACKEND_URL: http://backend:8000\n"
        "  frontend:\n    environment:\n"
        f"      JARVIS_API_TOKEN: {canary}\n"
    )

    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=compose_stdout,
            stderr=f"warning token={canary}",
        ),
    )

    result = smoke.run(
        "docker compose config",
        ["docker", "compose", "--profile", "llm", "config"],
        optional=True,
    )
    payload = json.dumps(result)

    assert result["ok"] is True
    assert canary not in result["stdout_tail"]
    assert canary not in result["stderr_tail"]
    assert canary not in payload
    assert "JARVIS_API_TOKEN" in result["stdout_tail"]
    assert "[redacted]" in result["stdout_tail"]
    assert "[redacted]" in result["stderr_tail"]


def test_main_report_redacts_nested_compose_canary(monkeypatch, capsys):
    canary = "CANARY_TOKEN_SPARK0017_nested"

    def fake_run(name, _command, *, cwd=smoke.ROOT, optional=False, env=None):
        if name == "docker compose config":
            return {
                "name": name,
                "ok": True,
                "optional": True,
                "status": "passed",
                "returncode": 0,
                "stdout_tail": f"JARVIS_API_TOKEN: {canary}",
                "stderr_tail": "",
            }
        return {
            "name": name,
            "ok": True,
            "optional": optional,
            "status": "passed",
        }

    monkeypatch.setattr(smoke, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["smoke.py", "--skip-frontend", "--skip-http"])

    exit_code = smoke.main()
    raw = capsys.readouterr().out
    report = json.loads(raw)

    assert exit_code == 0
    assert canary not in raw
    assert canary not in json.dumps(report)
    compose = next(c for c in report["checks"] if c["name"] == "docker compose config")
    assert canary not in compose["stdout_tail"]
    assert "[redacted]" in compose["stdout_tail"]
