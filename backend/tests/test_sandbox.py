"""Code/data sandbox: isolated Python execution + the code.run tool.

Uses stdlib-only snippets so the child interpreter runs anywhere (the numpy/matplotlib
capability is live-verified separately against the runtime interpreter).
"""

from __future__ import annotations

import asyncio

from jarvis_gpt import sandbox
from jarvis_gpt.config import ensure_runtime_dirs, load_settings
from jarvis_gpt.llm import LLMRouter
from jarvis_gpt.storage import JarvisStorage
from jarvis_gpt.tools import ToolRegistry

# --------------------------------------------------------------------------- #
# sandbox.run_python
# --------------------------------------------------------------------------- #


def test_run_python_compute(tmp_path):
    result = sandbox.run_python("print(2 + 2)", workdir=tmp_path / "sb")
    assert result.ok is True
    assert result.exit_code == 0
    assert result.stdout.strip() == "4"


def test_run_python_reports_error(tmp_path):
    result = sandbox.run_python("raise ValueError('boom')", workdir=tmp_path / "sb")
    assert result.ok is False
    assert result.exit_code != 0
    assert "ValueError" in result.stderr


def test_run_python_times_out(tmp_path):
    result = sandbox.run_python("while True:\n    pass", workdir=tmp_path / "sb", timeout_sec=2)
    assert result.timed_out is True
    assert result.ok is False


def test_run_python_writes_workdir_file(tmp_path):
    work = tmp_path / "sb"
    result = sandbox.run_python(
        "open('out.txt', 'w', encoding='utf-8').write('привет')", workdir=work
    )
    assert result.ok is True
    assert (work / "out.txt").read_text(encoding="utf-8") == "привет"


def test_output_is_bounded(tmp_path):
    result = sandbox.run_python(
        "print('x' * 100000)", workdir=tmp_path / "sb", max_output_bytes=1000
    )
    assert len(result.stdout.encode("utf-8")) <= 1000 + 64
    assert "обрезан" in result.stdout


def test_curated_env_drops_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_API_TOKEN", "supersecret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "botsecret")
    env = sandbox._curated_env(tmp_path)
    assert "JARVIS_API_TOKEN" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert env["MPLBACKEND"] == "Agg"
    assert env["TEMP"] == str(tmp_path)
    assert env["OPENBLAS_NUM_THREADS"] == "4"


def test_run_python_env_has_no_jarvis_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_API_TOKEN", "supersecret")
    result = sandbox.run_python(
        "import os; print('JARVIS_API_TOKEN' in os.environ)", workdir=tmp_path / "sb"
    )
    assert result.stdout.strip() == "False"  # secret never reaches the child


# --------------------------------------------------------------------------- #
# code.run tool
# --------------------------------------------------------------------------- #


def _registry(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("JARVIS_LLM_ENABLED", "0")
    settings = load_settings()
    ensure_runtime_dirs(settings)
    storage = JarvisStorage(settings.database_path)
    storage.initialize()
    return ToolRegistry(settings, storage, LLMRouter(settings)), storage


def test_code_run_tool_delivers_produced_file(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    code = "open('result.csv', 'w', encoding='utf-8').write('a,b\\n1,2\\n'); print('done')"
    result = asyncio.run(tools.run("code.run", {"code": code}, allow_danger=True))
    assert result.ok is True
    assert "done" in result.data["stdout"]
    files = result.data["files"]
    assert any(record["name"] == "result.csv" for record in files)
    storage.close()


def test_code_run_disabled_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SANDBOX_ENABLED", "0")
    tools, storage = _registry(monkeypatch, tmp_path)
    result = asyncio.run(tools.run("code.run", {"code": "print(1)"}, allow_danger=True))
    assert result.ok is False
    assert "отключена" in result.summary
    storage.close()


def test_code_run_empty_code(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    result = asyncio.run(tools.run("code.run", {"code": "   "}, allow_danger=True))
    assert result.ok is False
    storage.close()


def test_code_run_is_danger_gated(monkeypatch, tmp_path):
    tools, storage = _registry(monkeypatch, tmp_path)
    # Without authorization the danger tool must not execute.
    result = asyncio.run(tools.run("code.run", {"code": "print(1)"}))
    assert result.ok is False
    storage.close()
