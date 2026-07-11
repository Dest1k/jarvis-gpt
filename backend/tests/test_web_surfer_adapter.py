from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import sys
import time
import types
from contextlib import suppress
from pathlib import Path

import jarvis_gpt.web_surfer_adapter as adapter_module
import jarvis_gpt.web_surfer_worker as worker_module
import pytest
from jarvis_gpt.web_surfer_adapter import (
    WEB_ADAPTER_PROTOCOL,
    WebSurferAdapter,
    _require_public_http_url,
)


class FakeWebSurfer:
    async def fast_fact(self, query: str, language: str = "en"):
        return {"query": query, "language": language, "answer": "42"}

    async def deep_research(self, query: str, sources: int = 3):
        return {"query": query, "sources": sources, "verified": True}

    async def aggressive_shopping(self, query: str, currency: str = "USD"):
        return [{"product": query, "currency": currency, "price": 10}]


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and (
                exit_code.value == 259
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def _wait_pid_gone(pid: int) -> None:
    deadline = time.monotonic() + 2
    while _pid_exists(pid) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert not _pid_exists(pid)


def test_worker_pidfd_tracking_is_bounded_and_prunes_stale_handles(monkeypatch):
    handle = types.SimpleNamespace(
        process=types.SimpleNamespace(pid=100),
        tracked_pidfds={10: 1000, 11: 1001},
    )
    nodes = tuple(types.SimpleNamespace(pid=pid) for pid in range(100, 5000))
    closed: list[int] = []
    next_descriptor = iter(range(2000, 10_000))

    monkeypatch.setattr(adapter_module, "process_tree_snapshot", lambda _pid: nodes)
    monkeypatch.setattr(
        adapter_module.select,
        "select",
        lambda readers, _writers, _errors, _timeout: (
            tuple(item for item in readers if item == 1000),
            (),
            (),
        ),
    )
    monkeypatch.setattr(adapter_module.os, "close", closed.append)
    monkeypatch.setattr(
        adapter_module.os,
        "pidfd_open",
        lambda _pid, _flags=0: next(next_descriptor),
        raising=False,
    )

    WebSurferAdapter._remember_worker_tree(handle)

    assert 1000 in closed
    assert 10 not in handle.tracked_pidfds
    assert len(handle.tracked_pidfds) == 4096


def test_container_pid_one_is_a_valid_worker_parent_guard():
    assert worker_module._valid_posix_parent_guard(3, 1) is True
    assert worker_module._valid_posix_parent_guard(2, 1) is False
    assert worker_module._valid_posix_parent_guard(3, 0) is False


def test_injected_service_invokes_only_public_modes_and_normalizes_results():
    adapter = WebSurferAdapter(FakeWebSurfer(), unsafe_in_process=True)

    fact = asyncio.run(adapter.fast_fact("meaning", language="ru"))
    research = asyncio.run(adapter.deep_research("topic", sources=4))
    shopping = asyncio.run(adapter.aggressive_shopping("device", currency="EUR"))

    assert adapter.capabilities()["modes"] == [
        "fast_fact",
        "deep_research",
        "aggressive_shopping",
    ]
    assert fact.protocol == WEB_ADAPTER_PROTOCOL
    assert fact.ok and fact.data["answer"] == "42"
    assert research.ok and research.data["sources"] == 4
    assert shopping.ok and shopping.data[0]["currency"] == "EUR"


def test_direct_service_injection_requires_explicit_test_boundary():
    with pytest.raises(ValueError, match="test-only"):
        WebSurferAdapter(FakeWebSurfer())


def test_dynamic_module_service_and_clean_unavailable_result(monkeypatch):
    module = types.ModuleType("test_claude_web_surfer")
    service = FakeWebSurfer()
    module.fast_fact = service.fast_fact
    module.deep_research = service.deep_research
    module.aggressive_shopping = service.aggressive_shopping
    monkeypatch.setitem(sys.modules, module.__name__, module)

    loaded = WebSurferAdapter(module_names=(module.__name__,))
    result = asyncio.run(loaded.fast_fact("dynamic"))
    assert result.ok
    assert result.service == module.__name__

    unavailable = WebSurferAdapter(module_names=("module_that_does_not_exist_jarvis",))
    assert unavailable.available is False
    assert unavailable.capabilities()["worker_pid"] is None
    assert "not been probed" in unavailable.capabilities()["reason"]
    missing = asyncio.run(unavailable.fast_fact("query"))
    assert not missing.ok
    assert missing.unavailable
    assert missing.error["code"] == "service_unavailable"


def test_signature_result_and_mode_validation_fail_closed():
    adapter = WebSurferAdapter(FakeWebSurfer(), unsafe_in_process=True)

    mismatch = asyncio.run(adapter.invoke("fast_fact", {"unknown": "x"}))
    invalid_mode = asyncio.run(adapter.invoke("private_method", {}))

    assert not mismatch.ok and mismatch.error["code"] == "signature_mismatch"
    assert not invalid_mode.ok and invalid_mode.error["code"] == "invalid_mode"

    class BadResult(FakeWebSurfer):
        async def fast_fact(self, query: str):
            return {"not-json": object()}

    invalid_result = asyncio.run(
        WebSurferAdapter(BadResult(), unsafe_in_process=True).fast_fact("query")
    )
    assert not invalid_result.ok
    assert invalid_result.error["code"] == "service_error"

    invalid_timeout = asyncio.run(adapter.invoke("fast_fact", {"query": "x"}, timeout_sec="x"))
    assert not invalid_timeout.ok
    assert invalid_timeout.error["code"] == "invalid_arguments"

    class FailedOperation(FakeWebSurfer):
        async def fast_fact(self, query: str):
            return {"ok": False, "query": query, "error": "upstream unavailable"}

    failed_operation = asyncio.run(
        WebSurferAdapter(
            FailedOperation(), unsafe_in_process=True
        ).fast_fact("query")
    )
    assert failed_operation.ok is False
    assert failed_operation.data["ok"] is False
    assert failed_operation.error == {
        "code": "operation_failed",
        "message": "upstream unavailable",
    }


def test_shopping_target_policy_rejects_non_public_destinations(monkeypatch):
    assert _require_public_http_url("https://1.1.1.1/product") == (
        "https://1.1.1.1/product"
    )
    for target in (
        "file:///etc/passwd",
        "http://localhost/item",
        "http://127.0.0.1/item",
        "http://169.254.169.254/latest/meta-data",
        "http://user:secret@example.com/item",
    ):
        with pytest.raises(ValueError):
            _require_public_http_url(target)

    monkeypatch.setattr(
        adapter_module.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (adapter_module.socket.AF_INET, 1, 6, "", ("10.0.0.5", 443))
        ],
    )
    with pytest.raises(ValueError, match="public network"):
        _require_public_http_url("https://shop.example/product")


def test_contract_missing_method_is_unavailable_and_timeouts_are_bounded():
    class Missing:
        async def fast_fact(self, query: str):
            return query

    missing = WebSurferAdapter(Missing(), unsafe_in_process=True)
    assert not missing.available
    result = asyncio.run(missing.fast_fact("x"))
    assert result.unavailable

    class Slow(FakeWebSurfer):
        async def fast_fact(self, query: str):
            await asyncio.sleep(1)
            return query

    timed_out = asyncio.run(
        WebSurferAdapter(Slow(), timeout_sec=0.1, unsafe_in_process=True).fast_fact("x")
    )
    assert not timed_out.ok
    assert timed_out.error["code"] == "timeout"


def test_test_only_in_process_mode_opens_circuit_for_cancellation_resistance():
    class SlowAsync(FakeWebSurfer):
        def __init__(self) -> None:
            self.release = asyncio.Event()

        async def deep_research(self, query: str, sources: int = 3):
            del query, sources
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                await self.release.wait()
            return {"done": True}

    async def scenario():
        service = SlowAsync()
        adapter = WebSurferAdapter(
            service, timeout_sec=0.05, unsafe_in_process=True
        )
        started = time.monotonic()
        first = await adapter.deep_research("one")
        elapsed = time.monotonic() - started
        second = await adapter.deep_research("two")
        assert adapter.capabilities()["async_inflight"] == ["deep_research"]
        service.release.set()
        await asyncio.sleep(0)
        adapter.close()
        return first, second, elapsed

    first, second, elapsed = asyncio.run(scenario())

    assert first.error["code"] == "timeout"
    assert elapsed < 0.15
    assert second.error["code"] == "service_error"
    assert "still running" in second.error["message"]


def test_sync_service_contract_is_rejected_without_starting_threads():
    class SyncService(FakeWebSurfer):
        def deep_research(self, query: str, sources: int = 3):
            return {"query": query, "sources": sources}

    adapter = WebSurferAdapter(SyncService(), unsafe_in_process=True)

    assert adapter.available is False
    assert "must be async" in adapter.capabilities()["reason"]


def test_adapter_redacts_secrets_in_results_and_errors():
    class Secrets(FakeWebSurfer):
        async def fast_fact(self, query: str, language: str = "en"):
            return {
                "answer": "token=TOPSECRET https://user:password@example.test",
                "api_key": "HIDDEN",
            }

        async def aggressive_shopping(self, query: str, currency: str = "USD"):
            raise RuntimeError("Bearer ABCDEFGHIJKLMNOP")

    adapter = WebSurferAdapter(Secrets(), unsafe_in_process=True)
    result = asyncio.run(adapter.fast_fact("x"))
    failed = asyncio.run(adapter.aggressive_shopping("x"))
    serialized = str(result.to_dict()) + str(failed.to_dict())

    assert "TOPSECRET" not in serialized
    assert "password@example" not in serialized
    assert "HIDDEN" not in serialized
    assert "ABCDEFGHIJKLMNOP" not in serialized


def test_installed_module_runs_in_resident_worker_and_restarts_after_timeout():
    async def scenario():
        adapter = WebSurferAdapter(
            module_names=("backend.tests.web_worker_fixture",), timeout_sec=0.2
        )
        assert await adapter.start(timeout_sec=3)
        first = await adapter.fast_fact("one")
        second = await adapter.fast_fact("two")
        first_pid = first.data["worker_pid"]
        assert first.ok and second.ok
        assert first_pid == second.data["worker_pid"]
        assert (first.data["calls"], second.data["calls"]) == (1, 2)
        assert adapter.capabilities()["isolation"] == "process"
        assert adapter.capabilities()["worker_pid"] == first_pid

        stdio = await adapter.fast_fact("stdio-noise")
        assert stdio.ok
        assert stdio.data["stdin_eof"] is True
        assert stdio.data["worker_pid"] == first_pid

        started = time.monotonic()
        timed_out = await adapter.fast_fact("block")
        assert time.monotonic() - started < 1.5
        assert timed_out.error["code"] == "timeout"
        await _wait_pid_gone(first_pid)

        restarted = await adapter.invoke(
            "fast_fact", {"query": "restarted"}, timeout_sec=3
        )
        assert restarted.ok
        assert restarted.data["worker_pid"] != first_pid
        assert adapter.capabilities()["worker_generation"] == 2
        restarted_pid = restarted.data["worker_pid"]
        await adapter.aclose()
        await _wait_pid_gone(restarted_pid)

    asyncio.run(scenario())


def test_public_class_factory_lifecycle_runs_only_inside_worker(monkeypatch, tmp_path):
    lifecycle_path = tmp_path / "class-lifecycle.log"
    monkeypatch.setenv("JARVIS_WEB_CLASS_LIFECYCLE_PATH", str(lifecycle_path))
    monkeypatch.setenv(
        "JARVIS_WEB_SURFER_FACTORY_KWARGS_JSON",
        '{"proxies":["http://user:secret@proxy.example:8080"],"headless":true}',
    )

    async def scenario():
        adapter = WebSurferAdapter(
            module_names=("backend.tests.web_worker_class_fixture",), timeout_sec=3
        )
        assert await adapter.start(timeout_sec=3)
        worker_pid = adapter.capabilities()["worker_pid"]
        fact = await adapter.fast_fact("fact")
        research = await adapter.deep_research("topic", max_depth=2)
        shopping = await adapter.aggressive_shopping("https://1.1.1.1/item")
        assert fact.ok and fact.data["query"] == "fact"
        assert fact.data["proxy_count"] == 1
        assert fact.data["headless"] is True
        assert research.ok and research.data["max_depth"] == 2
        assert shopping.ok
        assert shopping.data["product_url"] == "https://1.1.1.1/item"
        result_pids = {
            fact.data["worker_pid"],
            research.data["worker_pid"],
            shopping.data["worker_pid"],
        }
        assert result_pids == {
            worker_pid
        }
        await adapter.aclose()
        return worker_pid

    worker_pid = asyncio.run(scenario())
    events = lifecycle_path.read_text(encoding="utf-8").splitlines()
    assert events == [
        f"construct:{worker_pid}",
        f"start:{worker_pid}",
        f"fast_fact:{worker_pid}",
        f"deep_research:{worker_pid}",
        f"aggressive_shopping:{worker_pid}",
        f"close:{worker_pid}",
    ]
    assert worker_pid != os.getpid()


def test_class_factory_contract_and_start_failures_are_closed(monkeypatch, tmp_path):
    async def rejected(module_name: str, lifecycle_path: Path) -> str:
        monkeypatch.setenv("JARVIS_WEB_CLASS_LIFECYCLE_PATH", str(lifecycle_path))
        adapter = WebSurferAdapter(module_names=(module_name,))
        assert not await adapter.start(timeout_sec=3)
        reason = str(adapter.capabilities()["reason"])
        await adapter.aclose()
        return reason

    bad_contract_path = tmp_path / "bad-contract.log"
    contract_reason = asyncio.run(
        rejected("backend.tests.web_worker_bad_class_fixture", bad_contract_path)
    )
    contract_events = bad_contract_path.read_text(encoding="utf-8").splitlines()
    contract_pid = contract_events[0].split(":", 1)[1]
    assert "deep_research must be async" in contract_reason
    assert contract_events == [f"construct:{contract_pid}", f"close:{contract_pid}"]

    failed_start_path = tmp_path / "failed-start.log"
    monkeypatch.setenv("JARVIS_WEB_CLASS_FAIL_START", "1")
    start_reason = asyncio.run(
        rejected("backend.tests.web_worker_class_fixture", failed_start_path)
    )
    start_events = failed_start_path.read_text(encoding="utf-8").splitlines()
    start_pid = start_events[0].split(":", 1)[1]
    assert "fixture startup failed" in start_reason
    assert start_events == [
        f"construct:{start_pid}",
        f"start:{start_pid}",
        f"close:{start_pid}",
    ]


def test_worker_import_timeout_and_caller_cancellation_are_contained(
    monkeypatch, tmp_path
):
    package = tmp_path / "hanging_web_parent"
    package.mkdir()
    (package / "__init__.py").write_text(
        "import time\ntime.sleep(60)\n", encoding="utf-8"
    )
    (package / "service.py").write_text(
        "async def fast_fact(query): return {'query': query}\n"
        "async def deep_research(query): return {'query': query}\n"
        "async def aggressive_shopping(query): return {'query': query}\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    previous_pythonpath = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH",
        str(tmp_path)
        + (os.pathsep + previous_pythonpath if previous_pythonpath else ""),
    )

    async def scenario():
        discovered_at = time.monotonic()
        hanging_import = WebSurferAdapter(
            module_names=("hanging_web_parent.service",)
        )
        assert time.monotonic() - discovered_at < 0.5
        started = time.monotonic()
        assert not await hanging_import.start(timeout_sec=0.2)
        assert time.monotonic() - started < 1.5
        assert hanging_import.capabilities()["worker_pid"] is None
        await hanging_import.aclose()

        adapter = WebSurferAdapter(
            module_names=("backend.tests.web_worker_fixture",), timeout_sec=30
        )
        assert await adapter.start(timeout_sec=3)
        worker_pid = adapter.capabilities()["worker_pid"]
        original_terminate = adapter._terminate_worker
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def delayed_terminate(handle, *, graceful=False):
            cleanup_started.set()
            await release_cleanup.wait()
            await original_terminate(handle, graceful=graceful)

        adapter._terminate_worker = delayed_terminate
        invocation = asyncio.create_task(adapter.fast_fact("block"))
        await asyncio.sleep(0.1)
        invocation.cancel()
        await cleanup_started.wait()
        invocation.cancel()
        invocation.cancel()
        await asyncio.sleep(0)
        assert not invocation.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await invocation
        await _wait_pid_gone(worker_pid)
        assert adapter.capabilities()["worker_pid"] is None
        await adapter.aclose()

    asyncio.run(scenario())


def test_worker_timeout_kills_spawned_child_tree(tmp_path):
    async def scenario():
        child_pid_path = tmp_path / "child.pid"
        adapter = WebSurferAdapter(
            module_names=("backend.tests.web_worker_fixture",), timeout_sec=0.4
        )
        assert await adapter.start(timeout_sec=3)
        result = await adapter.fast_fact(
            "spawn-child", child_pid_path=str(child_pid_path)
        )
        assert result.error["code"] == "timeout"
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        await _wait_pid_gone(child_pid)
        await adapter.aclose()

    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="setsid containment is POSIX-specific")
def test_worker_timeout_kills_descendant_that_escaped_process_group(tmp_path):
    async def scenario():
        child_pid_path = tmp_path / "detached-child.pid"
        adapter = WebSurferAdapter(
            module_names=("backend.tests.web_worker_fixture",), timeout_sec=0.4
        )
        assert await adapter.start(timeout_sec=3)
        result = await adapter.fast_fact(
            "spawn-detached-child", child_pid_path=str(child_pid_path)
        )
        assert result.error["code"] == "timeout"
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        await _wait_pid_gone(child_pid)
        await adapter.aclose()

    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="setsid containment is POSIX-specific")
def test_worker_close_kills_returned_descendant_that_escaped_process_group(tmp_path):
    async def scenario():
        child_pid_path = tmp_path / "detached-return-child.pid"
        adapter = WebSurferAdapter(
            module_names=("backend.tests.web_worker_fixture",), timeout_sec=3
        )
        assert await adapter.start(timeout_sec=3)
        result = await adapter.fast_fact(
            "spawn-detached-return", child_pid_path=str(child_pid_path)
        )
        assert result.ok and child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        assert _pid_exists(child_pid)
        await adapter.aclose()
        await _wait_pid_gone(child_pid)

    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="double-fork containment is POSIX-specific")
def test_worker_close_kills_double_forked_daemon_descendant(tmp_path):
    async def scenario():
        child_pid_path = tmp_path / "double-fork-child.pid"
        adapter = WebSurferAdapter(
            module_names=("backend.tests.web_worker_fixture",), timeout_sec=3
        )
        assert await adapter.start(timeout_sec=3)
        result = await adapter.fast_fact(
            "spawn-double-fork-return", child_pid_path=str(child_pid_path)
        )
        assert result.ok and child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        assert _pid_exists(child_pid)
        await adapter.aclose()
        await _wait_pid_gone(child_pid)

    asyncio.run(scenario())


@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor crash containment")
def test_unexpected_supervisor_exit_kills_service_and_escaped_descendant(tmp_path):
    async def scenario():
        child_pid_path = tmp_path / "crashed-supervisor-child.pid"
        adapter = WebSurferAdapter(
            module_names=("backend.tests.web_worker_fixture",), timeout_sec=3
        )
        assert await adapter.start(timeout_sec=3)
        result = await adapter.fast_fact(
            "spawn-double-fork-return", child_pid_path=str(child_pid_path)
        )
        assert result.ok and child_pid_path.exists()
        capabilities = adapter.capabilities()
        supervisor_pid = capabilities["supervisor_pid"]
        service_pid = capabilities["worker_pid"]
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        assert all(_pid_exists(pid) for pid in (supervisor_pid, service_pid, child_pid))

        os.kill(supervisor_pid, signal.SIGKILL)
        await _wait_pid_gone(supervisor_pid)
        await _wait_pid_gone(service_pid)
        await _wait_pid_gone(child_pid)
        await adapter.aclose()

    asyncio.run(scenario())


def test_worker_close_is_idempotent_and_survives_repeated_cancellation():
    async def scenario():
        adapter = WebSurferAdapter(
            module_names=("backend.tests.web_worker_fixture",), timeout_sec=3
        )
        assert await adapter.start(timeout_sec=3)
        worker_pid = adapter.capabilities()["worker_pid"]
        original_terminate = adapter._terminate_worker
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def delayed_terminate(handle, *, graceful=False):
            cleanup_started.set()
            await release_cleanup.wait()
            await original_terminate(handle, graceful=graceful)

        adapter._terminate_worker = delayed_terminate
        closing = asyncio.create_task(adapter.aclose())
        await cleanup_started.wait()
        closing.cancel()
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        closing.cancel()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await _wait_pid_gone(worker_pid)
        await adapter.aclose()

    asyncio.run(scenario())


def test_active_worker_requires_awaited_close_inside_running_loop(tmp_path):
    async def scenario():
        child_pid_path = tmp_path / "sync-close-child.pid"
        adapter = WebSurferAdapter(
            module_names=("backend.tests.web_worker_fixture",), timeout_sec=3
        )
        assert await adapter.start(timeout_sec=3)
        worker_pid = adapter.capabilities()["worker_pid"]
        result = await adapter.fast_fact(
            "spawn-child-return", child_pid_path=str(child_pid_path)
        )
        assert result.ok and child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        with pytest.raises(RuntimeError, match="await adapter.aclose"):
            adapter.close()
        await adapter.aclose()
        assert adapter.capabilities()["closed"] is True
        return worker_pid, child_pid

    worker_pid, child_pid = asyncio.run(scenario())
    asyncio.run(_wait_pid_gone(worker_pid))
    asyncio.run(_wait_pid_gone(child_pid))


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent-supervisor containment")
def test_worker_generation_dies_when_parent_runtime_is_killed(tmp_path):
    worker_path = tmp_path / "worker.pid"
    child_path = tmp_path / "worker.child"
    parent_script = """
import asyncio
import sys
from pathlib import Path
from jarvis_gpt.web_surfer_adapter import WebSurferAdapter

async def main():
    worker_path = Path(sys.argv[1])
    child_path = Path(sys.argv[2])
    adapter = WebSurferAdapter(
        module_names=("backend.tests.web_worker_fixture",), timeout_sec=30
    )
    assert await adapter.start(timeout_sec=3)
    result = await adapter.fast_fact(
        "spawn-double-fork-return", child_pid_path=str(child_path)
    )
    assert result.ok
    capabilities = adapter.capabilities()
    worker_path.write_text(
        f"{capabilities['supervisor_pid']},{capabilities['worker_pid']}",
        encoding="ascii",
    )
    await adapter.fast_fact("block")

asyncio.run(main())
"""
    environment = dict(os.environ)
    repository = str(Path(__file__).resolve().parents[2])
    import_roots = [
        str(Path(repository) / "backend" / "src"),
        repository,
        environment.get("PYTHONPATH", ""),
    ]
    environment["PYTHONPATH"] = os.pathsep.join(item for item in import_roots if item)
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_script, str(worker_path), str(child_path)],
        cwd=repository,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tracked: tuple[int, ...] = ()
    try:
        deadline = time.monotonic() + 8
        while (
            (not worker_path.exists() or not child_path.exists())
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert worker_path.exists() and child_path.exists()
        supervisor_pid, service_pid = (
            int(value) for value in worker_path.read_text(encoding="ascii").split(",")
        )
        child_pid = int(child_path.read_text(encoding="ascii"))
        tracked = (supervisor_pid, service_pid, child_pid)
        assert all(_pid_exists(pid) for pid in tracked)
        parent.kill()
        parent.wait(timeout=3)
        deadline = time.monotonic() + 5
        while any(_pid_exists(pid) for pid in tracked) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not any(_pid_exists(pid) for pid in tracked)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)
        for pid in tracked:
            if _pid_exists(pid):
                with suppress(ProcessLookupError):
                    os.kill(pid, 9)
