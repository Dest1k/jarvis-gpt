from __future__ import annotations

import asyncio
import signal
import sys
import time
from pathlib import Path

import jarvis_gpt.execution_process as process_module
import pytest
from jarvis_gpt.execution_actions import (
    AtomicActionExecutor,
    PathPolicy,
    ProcessAction,
    ProcessSignal,
    TerminateOwnedProcessAction,
)
from jarvis_gpt.execution_process import (
    AsyncProcessRunner,
    ExecutablePolicy,
    ExecutableRule,
    ProcessRequest,
    TerminationReason,
)
from jarvis_gpt.execution_session import (
    SessionRegistry,
    _process_birth_marker,
    _signal_exact_process,
)


def test_process_runner_captures_streams_and_filesystem_diff(tmp_path):
    output = tmp_path / "created.txt"
    request = ProcessRequest(
        executable=sys.executable,
        arguments=(
            "-c",
            (
                "import pathlib,sys; "
                f"pathlib.Path({str(output)!r}).write_text('done'); "
                "print('out'); print('err', file=sys.stderr)"
            ),
        ),
        cwd=tmp_path,
        observe_paths=(tmp_path,),
        sensitive_argument_indices=frozenset({1}),
    )
    runner = AsyncProcessRunner(observation_roots=(tmp_path,))

    result = asyncio.run(runner.run(request))

    assert result.ok is True
    assert result.exit_code == 0
    assert result.stdout.text.strip() == "out"
    assert result.stderr.text.strip() == "err"
    assert result.argv[-1] == "<redacted>"
    assert any(item.path == str(output) for item in result.filesystem_diff.created)
    assert result.permissions.identity
    assert result.pid_tree


def test_process_runner_detects_stall_and_terminates_tree(tmp_path):
    request = ProcessRequest(
        executable=sys.executable,
        arguments=("-c", "import time; time.sleep(30)"),
        cwd=tmp_path,
        timeout_seconds=5,
        stall_timeout_seconds=0.15,
        interrupt_grace_seconds=0.2,
        kill_grace_seconds=1,
    )

    result = asyncio.run(AsyncProcessRunner().run(request))

    assert result.ok is False
    assert result.termination_reason is TerminationReason.STALLED
    assert result.interrupt_sent or result.kill_sent
    assert result.duration_ms < 3000


def test_process_runner_cleans_child_tree_after_root_exits(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    child_script = "import time; time.sleep(30)"
    parent_script = (
        "import pathlib,subprocess,sys,time; time.sleep(0.2); "
        f"p=subprocess.Popen([sys.executable,'-c',{child_script!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(p.pid)); "
    )
    request = ProcessRequest(
        executable=sys.executable,
        arguments=("-c", parent_script),
        cwd=tmp_path,
        timeout_seconds=10,
        interrupt_grace_seconds=0.2,
        kill_grace_seconds=1,
    )

    result = asyncio.run(AsyncProcessRunner().run(request))
    child_pid = int(child_pid_file.read_text())
    deadline = time.monotonic() + 3
    marker = _process_birth_marker(child_pid)
    while marker is not None and time.monotonic() < deadline:
        time.sleep(0.02)
        marker = _process_birth_marker(child_pid)
    if marker is not None:
        _signal_exact_process(child_pid, marker, getattr(signal, "SIGKILL", signal.SIGTERM))

    assert result.ok is True
    assert marker is None


def test_process_runner_rejects_shells_and_unscoped_observation(tmp_path):
    shell = "cmd.exe" if sys.platform == "win32" else "sh"
    with pytest.raises(ValueError, match="shell interpreters"):
        asyncio.run(AsyncProcessRunner().run(ProcessRequest(executable=shell)))

    with pytest.raises(ValueError, match="observation_roots"):
        asyncio.run(
            AsyncProcessRunner().run(
                ProcessRequest(executable=sys.executable, observe_paths=(tmp_path,))
            )
        )


def test_process_runner_returns_rich_feedback_when_containment_setup_fails(
    monkeypatch, tmp_path
):
    def fail_assignment(_cls, _pid):
        raise OSError("simulated containment failure")

    monkeypatch.setattr(
        process_module._WindowsJob,
        "assign",
        classmethod(fail_assignment),
    )
    result = asyncio.run(
        AsyncProcessRunner().run(
            ProcessRequest(
                executable=sys.executable,
                arguments=("--version",),
                cwd=tmp_path,
            )
        )
    )

    assert result.ok is False
    assert result.pid is not None
    assert result.termination_reason is TerminationReason.START_FAILED
    assert result.kill_sent is True
    assert "simulated containment failure" in (result.error or "")
    assert result.permissions.identity
    assert result.pid_tree[0].pid == result.pid


def test_executable_allowlist_is_enforced(tmp_path):
    runner = AsyncProcessRunner(
        executable_policy=ExecutablePolicy(allowed_names=frozenset({"never-this-name"}))
    )

    with pytest.raises(ValueError, match="allowlist"):
        asyncio.run(
            runner.run(
                ProcessRequest(
                    executable=sys.executable,
                    arguments=("-c", "print('no')"),
                    cwd=tmp_path,
                )
            )
        )


def test_process_feedback_auto_redacts_secret_flags(tmp_path):
    request = ProcessRequest(
        executable=sys.executable,
        arguments=("-c", "print('ok')", "--token", "do-not-record"),
        cwd=tmp_path,
    )

    result = asyncio.run(AsyncProcessRunner().run(request))

    assert result.ok is True
    assert result.argv[-2:] == ("<redacted>", "<redacted>")
    assert "do-not-record" not in " ".join(result.argv)


def test_executable_rule_enforces_positional_argv_grammar(tmp_path):
    runner = AsyncProcessRunner(
        executable_policy=ExecutablePolicy(
            rules=(
                ExecutableRule(
                    executable=Path(sys.executable),
                    argument_patterns=(r"--version",),
                ),
            )
        )
    )

    accepted = asyncio.run(
        runner.run(
            ProcessRequest(executable=sys.executable, arguments=("--version",), cwd=tmp_path)
        )
    )

    assert accepted.ok is True
    with pytest.raises(ValueError, match="capability grammar"):
        asyncio.run(
            runner.run(
                ProcessRequest(
                    executable=sys.executable,
                    arguments=("-c", "print('blocked')"),
                    cwd=tmp_path,
                )
            )
        )


def test_only_session_owned_exact_process_can_be_terminated(tmp_path):
    async def scenario():
        sessions = SessionRegistry()
        session = sessions.create(session_id="session_owned")
        executor = AtomicActionExecutor(
            path_policy=PathPolicy((tmp_path,)),
            process_runner=AsyncProcessRunner(observation_roots=(tmp_path,)),
            sessions=sessions,
        )
        running = asyncio.create_task(
            executor.execute(
                ProcessAction(
                    request=ProcessRequest(
                        executable=sys.executable,
                        arguments=("-c", "import time; time.sleep(30)"),
                        cwd=tmp_path,
                        timeout_seconds=10,
                    ),
                    session_id=session.session_id,
                )
            )
        )
        for _attempt in range(100):
            if session.running_pids():
                break
            await asyncio.sleep(0.01)
        pid = session.running_pids()[0]
        denied = await executor.execute(
            TerminateOwnedProcessAction(session_id="unknown", pid=pid)
        )
        terminated = await executor.execute(
            TerminateOwnedProcessAction(session_id=session.session_id, pid=pid)
        )
        process_result = await asyncio.wait_for(running, timeout=3)
        return denied, terminated, process_result, session

    denied, terminated, process_result, session = asyncio.run(scenario())

    assert denied.ok is False
    assert terminated.ok is True
    assert process_result.ok is False
    assert session.running_pids() == ()


def test_interrupt_keeps_ignored_signal_process_owned_until_kill(tmp_path):
    script = (
        "import signal,time; "
        "signal.signal(signal.SIGINT, lambda *_: None); "
        "hasattr(signal, 'SIGBREAK') and signal.signal(signal.SIGBREAK, lambda *_: None); "
        "time.sleep(30)"
    )

    async def scenario():
        sessions = SessionRegistry()
        session = sessions.create(session_id="session_interrupt")
        executor = AtomicActionExecutor(
            path_policy=PathPolicy((tmp_path,)),
            process_runner=AsyncProcessRunner(observation_roots=(tmp_path,)),
            sessions=sessions,
        )
        running = asyncio.create_task(
            executor.execute(
                ProcessAction(
                    request=ProcessRequest(
                        executable=sys.executable,
                        arguments=("-c", script),
                        cwd=tmp_path,
                        timeout_seconds=60,
                    ),
                    session_id=session.session_id,
                    action_id="interrupt-root",
                )
            )
        )
        for _attempt in range(200):
            if session.running_pids():
                break
            await asyncio.sleep(0.01)
        pid = session.running_pids()[0]
        await asyncio.sleep(0.2)
        interrupted = await executor.execute(
            TerminateOwnedProcessAction(
                session_id=session.session_id,
                pid=pid,
                signal=ProcessSignal.INTERRUPT,
            )
        )
        await asyncio.sleep(0.1)
        still_owned = session.running_pids()
        killed = await executor.execute(
            TerminateOwnedProcessAction(
                session_id=session.session_id,
                pid=pid,
                signal=ProcessSignal.KILL,
            )
        )
        process_result = await asyncio.wait_for(running, timeout=5)
        return interrupted, still_owned, killed, process_result

    interrupted, still_owned, killed, process_result = asyncio.run(scenario())

    assert interrupted.ok is True
    assert interrupted.after["still_running"] is True
    assert still_owned
    assert killed.ok is True
    assert process_result.ok is False
