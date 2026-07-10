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
    def fake_run(name, _command, *, cwd=smoke.ROOT, optional=False):
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
    def fake_run(name, _command, *, cwd=smoke.ROOT, optional=False):
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
