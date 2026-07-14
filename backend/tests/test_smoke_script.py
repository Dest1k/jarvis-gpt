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


def test_default_doctor_test_timeout_exceeds_full_suite_duration():
    """RB-1-R: default full-suite timeout must be > real backend suite (~230–300s)."""
    assert smoke.DEFAULT_DOCTOR_TEST_TIMEOUT_SECONDS >= 600
    assert smoke.DEFAULT_DOCTOR_TEST_TIMEOUT_SECONDS > 300
    assert smoke.DEFAULT_DOCTOR_TEST_TIMEOUT_SECONDS <= smoke.MAX_TIMEOUT_SECONDS


def test_resolve_timeout_default_and_valid_override(monkeypatch):
    monkeypatch.delenv(smoke.DOCTOR_TEST_TIMEOUT_ENV, raising=False)
    assert (
        smoke.resolve_timeout_seconds(
            smoke.DOCTOR_TEST_TIMEOUT_ENV,
            default=smoke.DEFAULT_DOCTOR_TEST_TIMEOUT_SECONDS,
        )
        == smoke.DEFAULT_DOCTOR_TEST_TIMEOUT_SECONDS
    )

    monkeypatch.setenv(smoke.DOCTOR_TEST_TIMEOUT_ENV, "900")
    assert (
        smoke.resolve_timeout_seconds(
            smoke.DOCTOR_TEST_TIMEOUT_ENV,
            default=smoke.DEFAULT_DOCTOR_TEST_TIMEOUT_SECONDS,
        )
        == 900
    )


def test_resolve_timeout_rejects_invalid_zero_negative_out_of_range(monkeypatch):
    for raw in ("0", "-1", "abc", "10", "999999"):
        monkeypatch.setenv(smoke.DOCTOR_TEST_TIMEOUT_ENV, raw)
        try:
            smoke.resolve_timeout_seconds(
                smoke.DOCTOR_TEST_TIMEOUT_ENV,
                default=smoke.DEFAULT_DOCTOR_TEST_TIMEOUT_SECONDS,
            )
            raise AssertionError(f"expected ValueError for {raw!r}")
        except ValueError as exc:
            assert smoke.DOCTOR_TEST_TIMEOUT_ENV in str(exc)


def test_simulated_timeout_remains_required_failure(monkeypatch):
    def timed_out(*_args, **_kwargs):
        raise smoke.subprocess.TimeoutExpired(cmd=["pytest"], timeout=12)

    monkeypatch.setattr(smoke.subprocess, "run", timed_out)
    result = smoke.run("backend tests", [sys.executable, "-m", "pytest"], timeout=12)

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "timed out after 12" in str(result["error"])
    assert result.get("optional") is False


def test_main_uses_doctor_test_timeout_for_backend_tests(monkeypatch, capsys):
    captured: dict[str, object] = {}

    def fake_run(name, command, *, cwd=smoke.ROOT, optional=False, env=None, timeout=None):
        if name == "backend tests":
            captured["timeout"] = timeout
            captured["command"] = command
            return {
                "name": name,
                "ok": True,
                "optional": False,
                "status": "passed",
                "returncode": 0,
            }
        return {
            "name": name,
            "ok": True,
            "optional": optional,
            "status": "passed" if not optional else "skipped",
        }

    monkeypatch.setenv(smoke.DOCTOR_TEST_TIMEOUT_ENV, "777")
    monkeypatch.setattr(smoke, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["smoke.py", "--skip-frontend", "--skip-http"])

    exit_code = smoke.main()
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["ok"] is True
    assert captured["timeout"] == 777
    assert report["timeouts"]["backend_tests_seconds"] == 777


def test_main_invalid_timeout_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setenv(smoke.DOCTOR_TEST_TIMEOUT_ENV, "0")
    monkeypatch.setattr(sys, "argv", ["smoke.py", "--skip-frontend", "--skip-http"])

    exit_code = smoke.main()
    err = capsys.readouterr().err

    assert exit_code != 0
    assert smoke.DOCTOR_TEST_TIMEOUT_ENV in err


def test_frontend_build_reuses_proven_build_after_oom(monkeypatch, tmp_path):
    frontend = tmp_path / "frontend"
    next_dir = frontend / ".next"
    next_dir.mkdir(parents=True)
    (next_dir / "BUILD_ID").write_text("build-canary-1", encoding="utf-8")
    (next_dir / "prerender-manifest.json").write_text("{}", encoding="utf-8")
    (next_dir / "build-manifest.json").write_text("{}", encoding="utf-8")

    def oom_run(name, command, *, cwd=smoke.ROOT, optional=False, env=None, timeout=None):
        return {
            "name": name,
            "ok": False,
            "optional": False,
            "status": "failed",
            "returncode": 1,
            "stdout_tail": "",
            "stderr_tail": "memory allocation of 16 bytes failed",
            "timeout_seconds": timeout,
        }

    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    monkeypatch.setattr(smoke, "run", oom_run)
    result = smoke.run_frontend_build(timeout=180)

    assert result["ok"] is True
    assert result["status"] == "passed"
    assert result.get("reused_production_build") is True
    assert result.get("build_id") == "build-canary-1"


def test_frontend_build_does_not_hide_real_compile_failure(monkeypatch, tmp_path):
    frontend = tmp_path / "frontend"
    next_dir = frontend / ".next"
    next_dir.mkdir(parents=True)
    (next_dir / "BUILD_ID").write_text("stale", encoding="utf-8")
    (next_dir / "prerender-manifest.json").write_text("{}", encoding="utf-8")
    (next_dir / "build-manifest.json").write_text("{}", encoding="utf-8")

    def compile_fail(name, command, *, cwd=smoke.ROOT, optional=False, env=None, timeout=None):
        return {
            "name": name,
            "ok": False,
            "optional": False,
            "status": "failed",
            "returncode": 1,
            "stdout_tail": "Type error: Property x does not exist",
            "stderr_tail": "Failed to compile",
            "timeout_seconds": timeout,
        }

    monkeypatch.setattr(smoke, "ROOT", tmp_path)
    monkeypatch.setattr(smoke, "run", compile_fail)
    result = smoke.run_frontend_build(timeout=180)

    assert result["ok"] is False
    assert result["status"] == "failed"



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
    def fake_run(name, _command, *, cwd=smoke.ROOT, optional=False, env=None, timeout=None):
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
    def fake_run(name, _command, *, cwd=smoke.ROOT, optional=False, env=None, timeout=None):
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

    def fake_run(name, _command, *, cwd=smoke.ROOT, optional=False, env=None, timeout=None):
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


def test_backend_tests_receive_sanitized_deployment_env(monkeypatch, capsys):
    """SPARK-0016: pytest must not inherit launcher JARVIS_HOME/PROFILE/MODEL_ROOT."""
    captured: dict[str, object] = {}

    def fake_run(name, command, *, cwd=smoke.ROOT, optional=False, env=None, timeout=None):
        if name == "backend tests":
            captured["env"] = env
            captured["command"] = command
            return {
                "name": name,
                "ok": True,
                "optional": False,
                "status": "passed",
                "returncode": 0,
            }
        return {
            "name": name,
            "ok": True,
            "optional": optional,
            "status": "passed" if not optional else "skipped",
        }

    monkeypatch.setenv("JARVIS_HOME", r"D:\jarvis\audit-functional\canary-home")
    monkeypatch.setenv("JARVIS_MODEL_ROOT", r"D:\jarvis\data\models")
    monkeypatch.setenv("JARVIS_PROFILE", "gemma4-turbo")
    monkeypatch.setattr(smoke, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["smoke.py", "--skip-frontend", "--skip-http"])

    exit_code = smoke.main()
    report = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert report["ok"] is True
    assert captured["command"] == [sys.executable, "-m", "pytest"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert "JARVIS_HOME" not in env
    assert "JARVIS_MODEL_ROOT" not in env
    assert "JARVIS_PROFILE" not in env


def test_sanitized_test_env_strips_only_deployment_keys(monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", "deployment-home")
    monkeypatch.setenv("JARVIS_PROFILE", "gemma4-turbo")
    monkeypatch.setenv("JARVIS_MODEL_ROOT", "deployment-models")
    monkeypatch.setenv("PATH", "keep-me")

    cleaned = smoke.sanitized_test_env()

    assert "JARVIS_HOME" not in cleaned
    assert "JARVIS_PROFILE" not in cleaned
    assert "JARVIS_MODEL_ROOT" not in cleaned
    assert cleaned.get("PATH") == "keep-me"


def test_doctor_and_ci_share_pinned_ruff_lint_contract():
    """RB-1: doctor/smoke backend lint must match CI pinned ruff==0.8.4 contract."""
    from pathlib import Path

    root = Path(smoke.ROOT)
    req_dev = (root / "backend" / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "ruff==0.8.4" in req_dev

    smoke_src = (root / "scripts" / "smoke.py").read_text(encoding="utf-8")
    assert '"backend lint"' in smoke_src or "'backend lint'" in smoke_src
    assert '"-m", "ruff", "check", "backend/src", "backend/tests"' in smoke_src or (
        "'-m', 'ruff', 'check', 'backend/src', 'backend/tests'" in smoke_src
    )

    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "Lint backend" in ci
    assert "python -m ruff check backend/src backend/tests" in ci

    doctor = (root / "scripts" / "doctor.ps1").read_text(encoding="utf-8")
    assert "scripts\\smoke.py" in doctor or "scripts/smoke.py" in doctor
    assert "exit $smokeExit" in doctor


def test_doctor_ps1_propagates_smoke_nonzero_exit(tmp_path):
    """SPARK-0016: doctor.ps1 must exit nonzero when smoke reports required failure."""
    import os
    import subprocess
    from pathlib import Path

    doctor = Path(smoke.ROOT) / "scripts" / "doctor.ps1"
    text = doctor.read_text(encoding="utf-8")
    assert "exit $smokeExit" in text or "exit $LASTEXITCODE" in text

    # Isolated mini-doctor matching production exit propagation contract.
    stub_smoke = tmp_path / "smoke_fail.py"
    stub_smoke.write_text(
        "import json, sys\n"
        "print(json.dumps({'ok': False, 'summary': {'failed': 1}, "
        "'checks': [{'name': 'backend tests', 'ok': False}]}))\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    mini_doctor = tmp_path / "mini_doctor.ps1"
    mini_doctor.write_text(
        f'py -3.11 "{stub_smoke}"\n'
        "$smokeExit = $LASTEXITCODE\n"
        "if ($null -eq $smokeExit) { $smokeExit = 1 }\n"
        "exit $smokeExit\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(mini_doctor),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "JARVIS_HOME": r"D:\should-not-matter"},
    )
    assert completed.returncode != 0
    assert '"ok": false' in completed.stdout.lower() or '"ok": false' in completed.stdout
