from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import threading
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
    ExecutionSession,
    SessionRegistry,
    _process_birth_marker,
    _signal_exact_process,
)


def test_posix_supervisor_environment_does_not_inherit_backend_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-wrapper")
    monkeypatch.setenv("JARVIS_API_TOKEN", "must-not-reach-wrapper")

    environment = process_module._posix_supervisor_environment()

    assert "OPENAI_API_KEY" not in environment
    assert "JARVIS_API_TOKEN" not in environment
    assert environment["PYTHONUNBUFFERED"] == "1"


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX containment behavior")
def test_process_runner_contains_child_that_escapes_with_setsid(tmp_path):
    child_pid_file = tmp_path / "escaped-child.pid"
    parent_script = (
        "import pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(p.pid)); "
        "time.sleep(30)"
    )
    result = asyncio.run(
        AsyncProcessRunner().run(
            ProcessRequest(
                executable=sys.executable,
                arguments=("-c", parent_script),
                cwd=tmp_path,
                timeout_seconds=0.4,
                interrupt_grace_seconds=0.1,
                kill_grace_seconds=2,
            )
        )
    )
    escaped_pid = int(child_pid_file.read_text())
    marker = _process_birth_marker(escaped_pid)
    deadline = time.monotonic() + 3
    while marker is not None and time.monotonic() < deadline:
        time.sleep(0.02)
        marker = _process_birth_marker(escaped_pid)
    if marker is not None:
        _signal_exact_process(escaped_pid, marker, signal.SIGKILL)

    assert result.ok is False
    assert result.termination_reason is TerminationReason.TIMED_OUT
    assert marker is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor start barrier")
def test_posix_registration_failure_aborts_suspended_target_without_deadlock(
    monkeypatch, tmp_path
):
    def reject_registration(self, **_kwargs):
        raise RuntimeError("simulated registration failure")

    monkeypatch.setattr(ExecutionSession, "register_process", reject_registration)
    session = SessionRegistry().create(session_id="registration_failure")
    started = time.monotonic()

    result = asyncio.run(
        AsyncProcessRunner().run(
            ProcessRequest(
                executable=sys.executable,
                arguments=("-c", "import time; time.sleep(30)"),
                cwd=tmp_path,
            ),
            session=session,
            reservation_id="registration-failure",
        )
    )

    assert result.ok is False
    assert result.termination_reason is TerminationReason.START_FAILED
    assert "simulated registration failure" in (result.error or "")
    assert time.monotonic() - started < 5


def test_cancellation_before_registration_never_resumes_suspended_target(tmp_path):
    marker = tmp_path / "must-not-run.txt"
    session = SessionRegistry().create(session_id="cancel_before_registration")
    original_register = session.register_process

    def cancel_then_register(**kwargs):
        session.request_process_cancellation()
        return original_register(**kwargs)

    session.register_process = cancel_then_register
    result = asyncio.run(
        AsyncProcessRunner().run(
            ProcessRequest(
                executable=sys.executable,
                arguments=(
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
                ),
                cwd=tmp_path,
            ),
            session=session,
            reservation_id="cancel-before-registration",
        )
    )

    assert result.ok is False
    assert result.termination_reason is TerminationReason.START_FAILED
    assert marker.exists() is False
    assert session.running_pids() == ()


def test_cancellation_after_registration_is_an_atomic_pre_resume_barrier(tmp_path):
    marker = tmp_path / "must-not-run-after-registration.txt"
    session = SessionRegistry().create(session_id="cancel_after_registration")
    original_authorize = session.authorize_process_resume

    def cancel_then_authorize(pid):
        session.request_process_cancellation()
        return original_authorize(pid)

    session.authorize_process_resume = cancel_then_authorize
    result = asyncio.run(
        AsyncProcessRunner().run(
            ProcessRequest(
                executable=sys.executable,
                arguments=(
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
                ),
                cwd=tmp_path,
            ),
            session=session,
            reservation_id="cancel-after-registration",
        )
    )

    assert result.ok is False
    assert result.termination_reason is TerminationReason.START_FAILED
    assert marker.exists() is False
    assert session.running_pids() == ()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink observation containment")
def test_process_observation_never_follows_a_symlink_created_by_target(tmp_path):
    root = tmp_path / "root"
    observed = root / "observed"
    outside = tmp_path / "outside"
    observed.mkdir(parents=True)
    outside.mkdir()
    outside.joinpath("secret.txt").write_text("secret", encoding="utf-8")
    script = (
        "import os,pathlib; "
        f"p=pathlib.Path({str(observed)!r}); p.rmdir(); "
        f"os.symlink({str(outside)!r}, p, target_is_directory=True)"
    )

    result = asyncio.run(
        AsyncProcessRunner(observation_roots=(root,)).run(
            ProcessRequest(
                executable=sys.executable,
                arguments=("-c", script),
                cwd=root,
                observe_paths=(observed,),
            )
        )
    )

    all_entries = (
        *result.filesystem_diff.created,
        *result.filesystem_diff.modified,
        *result.filesystem_diff.deleted,
    )
    assert result.ok is True
    assert any(item.path == str(observed) and item.kind == "symlink" for item in all_entries)
    assert all("secret.txt" not in item.path for item in all_entries)


@pytest.mark.skipif(os.name == "nt", reason="POSIX containment behavior")
def test_posix_supervisor_cleans_escaped_tree_after_runner_parent_crash(tmp_path):
    child_pid_file = tmp_path / "crash-escaped-child.pid"
    source_root = Path(process_module.__file__).resolve().parents[1]
    target_script = (
        "import pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(p.pid)); "
        "time.sleep(30)"
    )
    outer_script = (
        "import asyncio,sys\n"
        "from pathlib import Path\n"
        "from jarvis_gpt.execution_process import AsyncProcessRunner,ProcessRequest\n"
        "async def main():\n"
        " await AsyncProcessRunner().run(ProcessRequest("
        f"executable=sys.executable,arguments=('-c',{target_script!r}),"
        f"cwd=Path({str(tmp_path)!r}),timeout_seconds=60))\n"
        "asyncio.run(main())"
    )
    environment = dict(os.environ)
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(source_root), existing_pythonpath) if item
    )
    outer = subprocess.Popen(  # noqa: S603 - validated interpreter and fixed test argv
        [sys.executable, "-c", outer_script],
        cwd=tmp_path,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tracked: dict[int, str] = {}
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_file.exists()
        escaped_pid = int(child_pid_file.read_text())
        for node in process_module.process_tree_snapshot(outer.pid):
            marker = _process_birth_marker(node.pid)
            if marker is not None:
                tracked[node.pid] = marker
        escaped_marker = _process_birth_marker(escaped_pid)
        assert escaped_marker is not None
        tracked[escaped_pid] = escaped_marker

        os.kill(outer.pid, signal.SIGKILL)
        outer.wait(timeout=3)
        deadline = time.monotonic() + 5
        while tracked and time.monotonic() < deadline:
            tracked = {
                pid: marker
                for pid, marker in tracked.items()
                if _process_birth_marker(pid) == marker
            }
            time.sleep(0.02)
    finally:
        if outer.poll() is None:
            outer.kill()
            outer.wait(timeout=3)
        for pid, marker in tracked.items():
            with contextlib.suppress(OSError, PermissionError):
                _signal_exact_process(pid, marker, signal.SIGKILL)

    assert tracked == {}


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


@pytest.mark.skipif(os.name == "nt", reason="Linux fd-based executable pinning")
def test_posix_runner_executes_pinned_inode_after_path_replacement(monkeypatch, tmp_path):
    replacement_source = shutil.which("false")
    if replacement_source is None:
        pytest.skip("false executable is unavailable")
    executable = tmp_path / "pinned-python"
    replacement = tmp_path / "replacement"
    shutil.copy2(sys.executable, executable)
    shutil.copy2(replacement_source, replacement)
    executable.chmod(0o755)
    replacement.chmod(0o755)
    original_spawn = process_module._spawn_posix_supervised_process

    async def replace_after_pin(**kwargs):
        replacement.replace(executable)
        return await original_spawn(**kwargs)

    monkeypatch.setattr(process_module, "_spawn_posix_supervised_process", replace_after_pin)
    result = asyncio.run(
        AsyncProcessRunner().run(
            ProcessRequest(
                executable=executable,
                arguments=("-c", "print('pinned-image')"),
                cwd=tmp_path,
            )
        )
    )

    assert result.ok is True
    assert result.stdout.text.strip() == "pinned-image"


@pytest.mark.skipif(os.name != "nt", reason="Windows image-handle replacement lock")
def test_windows_runner_pins_image_until_create_process_returns(monkeypatch, tmp_path):
    executable = tmp_path / "pinned-python.exe"
    replacement = tmp_path / "replacement.exe"
    shutil.copy2(sys.executable, executable)
    shutil.copy2(sys.executable, replacement)
    original_spawn = asyncio.create_subprocess_exec
    replacement_blocked = False

    async def attempt_replacement(*args, **kwargs):
        nonlocal replacement_blocked
        try:
            replacement.replace(executable)
        except PermissionError:
            replacement_blocked = True
        return await original_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", attempt_replacement)
    result = asyncio.run(
        AsyncProcessRunner().run(
            ProcessRequest(
                executable=executable,
                arguments=("-c", "print('pinned-image')"),
                cwd=tmp_path,
            )
        )
    )

    assert replacement_blocked is True
    assert result.pid is not None
    assert result.termination_reason is TerminationReason.EXITED


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


def test_executable_rule_rejects_content_changed_after_policy_load(tmp_path):
    executable = tmp_path / ("mutable.exe" if os.name == "nt" else "mutable")
    shutil.copy2(sys.executable, executable)
    if os.name != "nt":
        executable.chmod(0o755)
    rule = ExecutableRule(executable=executable)
    with executable.open("ab") as handle:
        handle.write(b"\0")
    runner = AsyncProcessRunner(executable_policy=ExecutablePolicy(rules=(rule,)))

    with pytest.raises(ValueError, match="content changed"):
        asyncio.run(
            runner.run(
                ProcessRequest(
                    executable=executable,
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


def test_repeated_cancellation_cannot_interrupt_process_cleanup(monkeypatch, tmp_path):
    original_stop = process_module._stop_process

    async def scenario():
        stop_completed = asyncio.Event()
        release_cleanup = asyncio.Event()
        stopped_returncodes: list[int | None] = []

        async def delayed_stop(*args, **kwargs):
            result = await original_stop(*args, **kwargs)
            stopped_returncodes.append(args[0].returncode)
            stop_completed.set()
            await release_cleanup.wait()
            return result

        monkeypatch.setattr(process_module, "_stop_process", delayed_stop)
        sessions = SessionRegistry()
        session = sessions.create(session_id="repeated_cancel")
        runner = AsyncProcessRunner(observation_roots=(tmp_path,))
        running = asyncio.create_task(
            runner.run(
                ProcessRequest(
                    executable=sys.executable,
                    arguments=("-c", "import time; print('ready'); time.sleep(30)"),
                    cwd=tmp_path,
                    timeout_seconds=60,
                    interrupt_grace_seconds=0.2,
                    kill_grace_seconds=1,
                ),
                session=session,
                reservation_id="run-process",
            )
        )
        for _attempt in range(200):
            if session.running_pids():
                break
            await asyncio.sleep(0.01)
        assert session.running_pids()

        running.cancel()
        await asyncio.wait_for(stop_completed.wait(), timeout=3)
        for _attempt in range(3):
            running.cancel()
            await asyncio.sleep(0)
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=3)
        return session, stopped_returncodes

    session, stopped_returncodes = asyncio.run(scenario())

    assert stopped_returncodes and stopped_returncodes[0] is not None
    assert session.running_pids() == ()


def test_cancellation_after_exit_waits_for_snapshot_and_session_finalization(
    monkeypatch, tmp_path
):
    original_snapshot = process_module.filesystem_snapshot
    after_snapshot_started = threading.Event()
    release_snapshot = threading.Event()
    snapshot_calls = 0
    snapshot_lock = threading.Lock()

    def delayed_snapshot(*args, **kwargs):
        nonlocal snapshot_calls
        with snapshot_lock:
            snapshot_calls += 1
            call_number = snapshot_calls
        if call_number == 2:
            after_snapshot_started.set()
            if not release_snapshot.wait(timeout=5):
                raise TimeoutError("test did not release the after snapshot")
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(process_module, "filesystem_snapshot", delayed_snapshot)

    async def scenario():
        sessions = SessionRegistry()
        session = sessions.create(session_id="cancel_after_exit")
        runner = AsyncProcessRunner(observation_roots=(tmp_path,))
        running = asyncio.create_task(
            runner.run(
                ProcessRequest(
                    executable=sys.executable,
                    arguments=("-c", "print('finished')"),
                    cwd=tmp_path,
                    observe_paths=(tmp_path,),
                ),
                session=session,
                reservation_id="run-process",
            )
        )
        started = await asyncio.to_thread(after_snapshot_started.wait, 5)
        assert started is True
        for _attempt in range(3):
            running.cancel()
            await asyncio.sleep(0)
        assert session.running_pids()
        release_snapshot.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=3)
        return session

    session = asyncio.run(scenario())

    assert snapshot_calls == 2
    assert session.running_pids() == ()
